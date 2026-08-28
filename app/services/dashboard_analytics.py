"""概览页的周期聚合与模型/失败归一化。"""
from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import GenerationLog
from app.services.failure_classifier import classify_dashboard_failure


UTC = timezone.utc
PERIOD_DELTAS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
# A rolling window can begin and end in the middle of a calendar bucket. Keep
# both edge buckets so the timeline total always reconciles with the summary.
PERIOD_BUCKETS = {"24h": ("hour", 25), "7d": ("day", 8), "30d": ("day", 31)}
DASHBOARD_CACHE_TTL_SECONDS = 15.0
_dashboard_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_dashboard_cache_locks = {period: asyncio.Lock() for period in PERIOD_DELTAS}

FAILURE_CATEGORIES = (
    ("capacity", "容量 / 限流"),
    ("account_config", "账号 / 配置"),
    ("reference_input", "参考图 / 输入"),
    ("upstream", "上游服务"),
    ("storage", "存储 / 落盘"),
    ("other", "其他"),
)

MODEL_LABELS = {
    "gpt_image_2": "GPT Image 2",
    "nano_banana_pro": "Nano Banana Pro",
    "other": "其他",
}
KNOWN_MODEL_KEYS = ("gpt_image_2", "nano_banana_pro")

_MODEL_TOKEN_RE = re.compile(r"[^a-z0-9]+")
_DIMENSION_RE = re.compile(r"^(\d+)\s*[x×]\s*(\d+)$", re.IGNORECASE)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _utc_datetime(value: Optional[datetime]) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def period_window(period: str, *, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """Return an aware UTC window for a supported dashboard period."""
    if period not in PERIOD_DELTAS:
        raise ValueError("period 只能是 24h、7d 或 30d")
    end = _utc_datetime(now)
    return end - PERIOD_DELTAS[period], end


def _floor_bucket(value: datetime, unit: str) -> datetime:
    value = _utc_datetime(value)
    if unit == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _iso(value: datetime) -> str:
    return _utc_datetime(value).isoformat().replace("+00:00", "Z")


def normalize_model_key(model: Any) -> str:
    """Normalize display labels and provider values without guessing unknown models."""
    token = _MODEL_TOKEN_RE.sub(" ", _text(model).lower()).strip()
    if token in {"gpt image 2", "gpt image-2", "gptimage2"}:
        return "gpt_image_2"
    if token in {
        "nano banana pro",
        "gemini 3 pro image preview",
        "gemini 3 pro image",
    }:
        return "nano_banana_pro"
    return "other"


def normalize_failure_category(error_message: Any) -> str:
    """Map an error to one stable dashboard category using a fixed priority."""
    return classify_dashboard_failure(error_message)


def normalize_nano_resolution(value: Any) -> str:
    value = _text(value).upper()
    return value if value in {"1K", "2K", "4K"} else "unknown"


def normalize_gpt_size(value: Any) -> str:
    value = _text(value).lower()
    if value == "auto":
        return "auto"
    match = _DIMENSION_RE.match(value)
    if not match:
        return "unknown"
    width, height = (int(part) for part in match.groups())
    if (width, height) in {(1024, 1024), (1536, 1024), (1024, 1536)}:
        return "1k"
    if (width, height) == (2048, 2048):
        return "2k"
    if (width, height) == (3840, 2160):
        return "4k"
    return "unknown"


def _spec_items(labels: Iterable[tuple[str, str]]) -> list[dict[str, Any]]:
    return [{"key": key, "label": label, "requests": 0, "failure": 0} for key, label in labels]


def _spec_item(items: list[dict[str, Any]], key: str) -> dict[str, Any]:
    return next(item for item in items if item["key"] == key)


def _model_template(key: str) -> dict[str, Any]:
    if key == "nano_banana_pro":
        specs = [{
            "key": "resolution",
            "label": "分辨率",
            "items": _spec_items((("1k", "1K"), ("2k", "2K"), ("4k", "4K"))),
        }]
    elif key == "gpt_image_2":
        specs = [
            {
                "key": "quality",
                "label": "Quality",
                "items": _spec_items((("auto", "Auto"), ("low", "Low"), ("medium", "Medium"), ("high", "High"))),
            },
            {
                "key": "size",
                "label": "Size",
                "items": _spec_items((("1k", "1K"), ("2k", "2K"), ("4k", "4K"), ("auto", "Auto"))),
            },
        ]
    else:
        specs = []
    return {
        "key": key,
        "label": MODEL_LABELS[key],
        "requests": 0,
        "request_share": 0.0,
        "success": 0,
        "failure": 0,
        "success_rate": 0.0,
        "avg_response_ms": 0.0,
        "specs": specs,
    }


def _row_value(row: Any, name: str, index: int, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    try:
        return getattr(row, name)
    except AttributeError:
        try:
            return row[index]
        except (IndexError, TypeError):
            return default


def aggregate_dashboard_rows(
    rows: Iterable[Any],
    period: str,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Aggregate lightweight GenerationLog rows already restricted to the period."""
    start, end = period_window(period, now=now)
    unit, bucket_count = PERIOD_BUCKETS[period]
    bucket_delta = timedelta(hours=1) if unit == "hour" else timedelta(days=1)
    first_bucket = _floor_bucket(start, unit)
    timeline = [
        {
            "start": _iso(first_bucket + index * bucket_delta),
            "requests": 0,
            "success": 0,
            "failure": 0,
        }
        for index in range(bucket_count)
    ]

    durations: list[float] = []
    summary = {"requests": 0, "success": 0, "failure": 0}
    failure_counts = defaultdict(int)
    model_rows: dict[str, dict[str, Any]] = {
        key: _model_template(key) for key in KNOWN_MODEL_KEYS
    }
    text2img_requests = 0

    for row in rows:
        timestamp = _row_value(row, "timestamp", 0)
        if not isinstance(timestamp, datetime):
            continue
        timestamp = _utc_datetime(timestamp)
        if timestamp < start or timestamp > end:
            continue
        status = _text(_row_value(row, "status", 1)).lower()
        success = status == "success"
        summary["requests"] += 1
        summary["success" if success else "failure"] += 1
        duration = _row_value(row, "response_time_ms", 2)
        try:
            if duration is not None and float(duration) >= 0:
                durations.append(float(duration))
        except (TypeError, ValueError):
            pass

        bucket_index = int((timestamp - first_bucket).total_seconds() // bucket_delta.total_seconds())
        if 0 <= bucket_index < bucket_count:
            bucket = timeline[bucket_index]
            bucket["requests"] += 1
            bucket["success" if success else "failure"] += 1

        if not success:
            category = _text(_row_value(row, "failure_category", 8)) or normalize_failure_category(_row_value(row, "error_message", 3))
            failure_counts[category] += 1

        mode = _text(_row_value(row, "mode", 4)).lower()
        if mode != "text2img":
            continue
        model_key = normalize_model_key(_row_value(row, "model", 5))
        if model_key not in KNOWN_MODEL_KEYS:
            continue
        text2img_requests += 1
        model = model_rows[model_key]
        model["requests"] += 1
        model["success" if success else "failure"] += 1
        if duration is not None:
            try:
                if float(duration) >= 0:
                    model.setdefault("_durations", []).append(float(duration))
            except (TypeError, ValueError):
                pass

        if model_key == "nano_banana_pro":
            resolution_key = normalize_nano_resolution(_row_value(row, "resolution", 7)).lower()
            if resolution_key != "unknown":
                spec = _spec_item(model["specs"][0]["items"], resolution_key)
                spec["requests"] += 1
                spec["failure"] += int(not success)
        elif model_key == "gpt_image_2":
            quality_key = _text(_row_value(row, "resolution", 7)).lower()
            if quality_key in {"auto", "low", "medium", "high"}:
                quality_spec = _spec_item(model["specs"][0]["items"], quality_key)
                quality_spec["requests"] += 1
                quality_spec["failure"] += int(not success)
            size_key = normalize_gpt_size(_row_value(row, "aspect_ratio", 6))
            if size_key != "unknown":
                size_spec = _spec_item(model["specs"][1]["items"], size_key)
                size_spec["requests"] += 1
                size_spec["failure"] += int(not success)

    summary["avg_response_ms"] = round(sum(durations) / len(durations), 1) if durations else 0.0
    failures = [
        {
            "key": key,
            "label": label,
            "count": int(failure_counts[key]),
            "share": round((failure_counts[key] / summary["failure"] * 100), 1) if summary["failure"] else 0.0,
        }
        for key, label in FAILURE_CATEGORIES
    ]

    model_result = []
    for key in KNOWN_MODEL_KEYS:
        model = model_rows[key]
        durations_for_model = model.pop("_durations", [])
        model["request_share"] = round((model["requests"] / text2img_requests * 100), 1) if text2img_requests else 0.0
        model["success_rate"] = round((model["success"] / model["requests"] * 100), 1) if model["requests"] else 0.0
        model["avg_response_ms"] = round(sum(durations_for_model) / len(durations_for_model), 1) if durations_for_model else 0.0
        model_result.append(model)

    return {
        "period": period,
        "from": _iso(start),
        "to": _iso(end),
        "summary": summary,
        "timeline": timeline,
        "failures": failures,
        "models": model_result,
    }


async def get_dashboard_analytics(
    session: AsyncSession,
    period: str,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Read only the small GenerationLog columns needed for aggregation."""
    if period not in PERIOD_DELTAS:
        raise ValueError("period 只能是 24h、7d 或 30d")
    use_cache = now is None
    if use_cache:
        cached = _dashboard_cache.get(period)
        if cached and time.monotonic() - cached[0] < DASHBOARD_CACHE_TTL_SECONDS:
            return cached[1]

        # Serialize cache refreshes so a busy admin page cannot trigger several
        # identical full-window SQLite scans at the same time.
        async with _dashboard_cache_locks.setdefault(period, asyncio.Lock()):
            cached = _dashboard_cache.get(period)
            if cached and time.monotonic() - cached[0] < DASHBOARD_CACHE_TTL_SECONDS:
                return cached[1]
            return await _query_dashboard_analytics(session, period, now=None, use_cache=True)

    return await _query_dashboard_analytics(session, period, now=now, use_cache=False)


async def _query_dashboard_analytics(
    session: AsyncSession,
    period: str,
    *,
    now: Optional[datetime],
    use_cache: bool,
) -> dict[str, Any]:
    """Execute one dashboard scan; callers coordinate cache refreshes."""

    start, end = period_window(period, now=now)
    rows = await session.execute(
        select(
            GenerationLog.timestamp,
            GenerationLog.status,
            GenerationLog.response_time_ms,
            GenerationLog.error_message,
            GenerationLog.mode,
            GenerationLog.model,
            GenerationLog.aspect_ratio,
            GenerationLog.resolution,
            GenerationLog.failure_category,
            GenerationLog.error_code,
        )
        .where(GenerationLog.timestamp >= start.replace(tzinfo=None))
        .where(GenerationLog.timestamp <= end.replace(tzinfo=None))
    )
    result = aggregate_dashboard_rows(rows, period, now=end)
    if use_cache:
        _dashboard_cache[period] = (time.monotonic(), result)
    return result
