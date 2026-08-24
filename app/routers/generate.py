"""图像生成路由：文生图 / 图生图。

前端只需提交 prompt / model / aspect_ratio / resolution / image_url/image_urls(可选，可由上传接口生成)，
后端选号 + 调用 StackAI 工作流模板，并把结果回传。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Iterator, List, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Account, GenerationLog, get_session
from app.services.deps import GenerationPrincipal, require_generation_principal
from app.services import app_settings
from app.services.guard import get_generation_guard
from app.services.account_pool import (
    NoAvailableAccountError,
    NoCapacityError,
    get_account_pool_service,
)
from app.services.failure_classifier import (
    ACCOUNT_SCOPE,
    ROUTE_CONFIG_SCOPE,
    UPSTREAM_ROUTE_SCOPE,
    classify_stackai_error,
    route_key as build_route_key,
)
from app.services.outbound_url import (
    UnsafeOutboundURLError,
    ensure_safe_outbound_url,
    open_safe_stream,
)
from app.services.stackai_client import (
    StackAIError,
    extract_image_urls,
    get_stackai_client,
)
from app.services.upstream_redaction import (
    redact_upstream_data,
    redact_upstream_event_text,
    redact_upstream_text,
)
from app.services.user_auth import (
    UserConcurrencyExceededError,
    UserDisabledError,
    UserExpiredError,
    UserQuotaExceededError,
    get_user_auth_service,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["generate"])


# ---- 上游错误抽取 ---------------------------------------------------------
# StackAI 在节点失败时会把错误文字塞到事件的某个字段（实际位置不固定，
# 可能是顶层 error/error_message、progress_data.error、outputs 内部，
# 也可能是 text/delta 里以 "Error in Node X: ..." 形式出现）。
# 我们用关键词扫描所有字符串叶子，取最长且最具体的一段作为"上游真实错误"。

_ERROR_HINTS = (
    "Error in Node",
    "Network or HTTP error",
    "Client error",
    "HTTP error",
    "error during",
    "Failed to ",
    "Forbidden",
)


def _walk_strings(obj: Any) -> Iterator[str]:
    """递归 yield 所有字符串叶子节点。"""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def _looks_like_error(s: str) -> bool:
    return any(h in s for h in _ERROR_HINTS)


def _concise_upstream_error(text: str) -> str:
    """从上游完整错误中提取最关键的一句给前端展示。

    示例输入:
      "Error in Node **Image-to-Image Transform** (`action-3`): Network or HTTP error
       during image transformation: Client error '403 Forbidden' for url 'https://...'
       For more information check: https://developer.mozilla.org/..."
    示例输出:
      "Network or HTTP error during image transformation: Client error '403 Forbidden'
       for url 'https://...'"
    """
    s = text.strip()
    # 去掉 markdown 加粗 / 反引号
    s = re.sub(r"\*\*", "", s)
    s = re.sub(r"`", "", s)
    # 去掉 "For more information check: ..." 行（含其后所有内容）
    s = re.sub(r"\s*For more information check:.*$", "", s, flags=re.DOTALL)
    s = s.strip()
    # 若形如 "Error in Node ... (...): <core>"，取冒号之后的核心
    m = re.search(r"Error in Node[^:]*:\s*(.+)", s, flags=re.DOTALL)
    if m:
        return redact_upstream_event_text(m.group(1).strip())
    return redact_upstream_event_text(s)


def _is_zero_stage_failure(total_nodes: Optional[int], completed_nodes: Optional[int]) -> bool:
    return (
        isinstance(total_nodes, int)
        and total_nodes > 0
        and isinstance(completed_nodes, int)
        and completed_nodes <= 0
    )


async def _rollback_request_session_before_release(session: AsyncSession) -> None:
    """End any request-scoped transaction so its SQLite connection returns to the pool."""
    if not session.in_transaction():
        return
    try:
        await session.rollback()
    except asyncio.CancelledError:
        logger.warning("request session rollback before release cancelled")
        raise
    except Exception as exc:
        logger.warning("request session rollback before release failed: %s", exc)


# 工作流模板支持的模型/比例/分辨率（前端展示用）。
GPT_IMAGE_2_MODEL = "GPT Image 2"
TEXT2IMG_MODELS: List[str] = [
    "Nano Banana Pro",
    GPT_IMAGE_2_MODEL,
]
# 图生图：UI 显示名字，传入参为模型 ID。
IMG2IMG_MODELS: List[Dict[str, str]] = [
    {"label": "Nano Banana Pro", "value": "gemini-3-pro-image-preview"},
    {"label": "gpt-image-1.5", "value": "gpt-image-1.5"},
]
# 画幅按数字和（w+h）从小到大排列，同组竖版在前
DEFAULT_ASPECT_RATIOS = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]
DEFAULT_RESOLUTIONS = ["1K", "2K", "4K"]
# label 仅用于前端展示，提交与 in-4 传参始终用标准 value
GPT_IMAGE_2_SIZES: List[Dict[str, str]] = [
    {"label": "auto (自动)", "value": "auto"},
    {"label": "1024x1024 (正方形)", "value": "1024x1024"},
    {"label": "1536x1024 (横向)", "value": "1536x1024"},
    {"label": "1024x1536 (竖屏)", "value": "1024x1536"},
    {"label": "2048x2048 (2K 方形)", "value": "2048x2048"},
    {"label": "3840x2160 (4K 横向)", "value": "3840x2160"},
]
GPT_IMAGE_2_QUALITIES = ["auto", "low", "medium", "high"]
# 图生图下 UI 不让用户选比例/分辨率了，但 Nano Banana Pro 工作流字段仍需字符串占位，
# 这里给个最安全的默认值。
IMG2IMG_DEFAULT_ASPECT_RATIO = "1:1"
IMG2IMG_DEFAULT_RESOLUTION = "2K"
REFERENCE_UPLOAD_MAX_BYTES = int(os.getenv("REFERENCE_UPLOAD_MAX_BYTES", str(20 * 1024 * 1024)))
# 参考图上传流式落盘的分块大小：内存占用与文件大小解耦
UPLOAD_STREAM_CHUNK_BYTES = 1024 * 1024
REFERENCE_UPLOAD_MAX_FILES = int(os.getenv("REFERENCE_UPLOAD_MAX_FILES", "5"))
REFERENCE_UPLOAD_DIR = Path(
    os.getenv("UPLOADS_DIR") or (Path(__file__).resolve().parents[2] / "data" / "uploads")
).resolve()
GENERATED_IMAGE_DIR = (REFERENCE_UPLOAD_DIR / "generated").resolve()
GENERATED_IMAGE_MAX_BYTES = int(os.getenv("GENERATED_IMAGE_MAX_BYTES", str(50 * 1024 * 1024)))
GENERATED_IMAGE_MIN_FREE_BYTES = max(
    0,
    int(os.getenv("GENERATED_IMAGE_MIN_FREE_BYTES", str(8 * 1024 * 1024 * 1024))),
)
GENERATED_IMAGE_TIMEOUT_SECONDS = float(os.getenv("GENERATED_IMAGE_TIMEOUT_SECONDS", "60"))
GENERATED_IMAGE_SAVE_TOTAL_TIMEOUT_SECONDS = max(
    GENERATED_IMAGE_TIMEOUT_SECONDS,
    float(
        os.getenv(
            "GENERATED_IMAGE_SAVE_TOTAL_TIMEOUT_SECONDS",
            str(max(75.0, GENERATED_IMAGE_TIMEOUT_SECONDS)),
        )
    ),
)
GENERATE_STREAM_IDLE_TIMEOUT_SECONDS = float(
    os.getenv("GENERATE_STREAM_IDLE_TIMEOUT_SECONDS", "150")
)
GENERATE_STREAM_TOTAL_TIMEOUT_SECONDS = max(
    GENERATE_STREAM_IDLE_TIMEOUT_SECONDS + 1.0,
    float(os.getenv("GENERATE_STREAM_TOTAL_TIMEOUT_SECONDS", "230")),
)
GENERATE_STREAM_KEEPALIVE_INTERVAL_SECONDS = max(
    1.0,
    float(os.getenv("GENERATE_STREAM_KEEPALIVE_INTERVAL_SECONDS", "15")),
)
GENERATE_STREAM_TEXT2IMG_4K_IDLE_TIMEOUT_SECONDS = max(
    GENERATE_STREAM_IDLE_TIMEOUT_SECONDS,
    float(os.getenv("GENERATE_STREAM_TEXT2IMG_4K_IDLE_TIMEOUT_SECONDS", "200")),
)
GENERATE_STREAM_GPT_IMAGE_2_IDLE_TIMEOUT_SECONDS = max(
    GENERATE_STREAM_IDLE_TIMEOUT_SECONDS,
    float(os.getenv("GENERATE_STREAM_GPT_IMAGE_2_IDLE_TIMEOUT_SECONDS", "200")),
)
ACCOUNT_FAILURE_ISOLATION_SECONDS = max(
    1.0,
    float(os.getenv("ACCOUNT_FAILURE_ISOLATION_SECONDS", "300")),
)
GENERATED_IMAGE_DOWNLOAD_ATTEMPTS = max(1, int(os.getenv("GENERATED_IMAGE_DOWNLOAD_ATTEMPTS", "3")))
GENERATED_IMAGE_DOWNLOAD_RETRY_BACKOFF_SECONDS = float(
    os.getenv("GENERATED_IMAGE_DOWNLOAD_RETRY_BACKOFF_SECONDS", "1.0")
)
UPLOAD_CLEANUP_INTERVAL_SECONDS = max(
    5.0,
    float(os.getenv("UPLOAD_CLEANUP_INTERVAL_SECONDS", "300")),
)
REFERENCE_UPLOAD_RETENTION_DAYS = max(
    0.0,
    float(os.getenv("REFERENCE_UPLOAD_RETENTION_DAYS", "0")),
)
GENERATED_IMAGE_RETENTION_DAYS = max(
    0.0,
    float(os.getenv("GENERATED_IMAGE_RETENTION_DAYS", "0")),
)
REFERENCE_UPLOAD_DIR_MAX_FILES = max(
    0,
    int(os.getenv("REFERENCE_UPLOAD_DIR_MAX_FILES", "0")),
)
GENERATED_IMAGE_DIR_MAX_FILES = max(
    0,
    int(os.getenv("GENERATED_IMAGE_DIR_MAX_FILES", "0")),
)
_upload_cleanup_lock = asyncio.Lock()
_last_upload_cleanup_monotonic = 0.0


def _prune_upload_dir(directory: Path, *, retention_days: float, max_files: int) -> int:
    if not directory.exists():
        return 0

    removed = 0
    files = [path for path in directory.iterdir() if path.is_file()]
    now_ts = time.time()
    keep: List[Path] = []

    cutoff_ts = now_ts - (retention_days * 86400.0) if retention_days > 0 else None
    for path in files:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if cutoff_ts is not None and stat.st_mtime < cutoff_ts:
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass
            continue
        keep.append(path)

    if max_files > 0 and len(keep) > max_files:
        decorated = []
        for path in keep:
            try:
                decorated.append((path.stat().st_mtime, path))
            except FileNotFoundError:
                continue
        decorated.sort(key=lambda item: (item[0], item[1].name))
        for _mtime, path in decorated[: max(0, len(decorated) - max_files)]:
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass

    return removed


async def _effective_retention_settings() -> Dict[str, float]:
    """清理用保留期：管理后台 DB 覆盖优先，未设置回退 env 默认。"""
    return {
        "reference_retention": await app_settings.get_effective_float(
            app_settings.SETTING_REFERENCE_UPLOAD_RETENTION_DAYS,
            REFERENCE_UPLOAD_RETENTION_DAYS,
        ),
        "generated_retention": await app_settings.get_effective_float(
            app_settings.SETTING_GENERATED_IMAGE_RETENTION_DAYS,
            GENERATED_IMAGE_RETENTION_DAYS,
        ),
    }


async def _maybe_cleanup_uploads(*, force: bool = False) -> None:
    global _last_upload_cleanup_monotonic

    retention = await _effective_retention_settings()
    cleanup_enabled = any(
        value > 0
        for value in (
            retention["reference_retention"],
            retention["generated_retention"],
            REFERENCE_UPLOAD_DIR_MAX_FILES,
            GENERATED_IMAGE_DIR_MAX_FILES,
        )
    )
    if not cleanup_enabled:
        return

    now_monotonic = time.monotonic()
    if (
        not force
        and now_monotonic - _last_upload_cleanup_monotonic < UPLOAD_CLEANUP_INTERVAL_SECONDS
    ):
        return

    async with _upload_cleanup_lock:
        now_monotonic = time.monotonic()
        if (
            not force
            and now_monotonic - _last_upload_cleanup_monotonic < UPLOAD_CLEANUP_INTERVAL_SECONDS
        ):
            return

        # 拿到锁后再读一次，尽量用上最新设置
        retention = await _effective_retention_settings()
        removed_root = await asyncio.to_thread(
            _prune_upload_dir,
            REFERENCE_UPLOAD_DIR,
            retention_days=retention["reference_retention"],
            max_files=REFERENCE_UPLOAD_DIR_MAX_FILES,
        )
        removed_generated = await asyncio.to_thread(
            _prune_upload_dir,
            GENERATED_IMAGE_DIR,
            retention_days=retention["generated_retention"],
            max_files=GENERATED_IMAGE_DIR_MAX_FILES,
        )
        _last_upload_cleanup_monotonic = time.monotonic()
        if removed_root or removed_generated:
            logger.info(
                "upload cleanup removed files: reference=%s generated=%s",
                removed_root,
                removed_generated,
            )


def _split_env_list(name: str, fallback: List[str]) -> List[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    return [item.strip() for item in raw.split(",") if item.strip()]


def _model_options_for_text2img() -> List[Dict[str, str]]:
    names = _split_env_list("UI_MODELS", TEXT2IMG_MODELS)
    return [{"label": name, "value": name} for name in names]


def _model_options_for_img2img() -> List[Dict[str, str]]:
    raw = os.getenv("UI_IMG2IMG_MODELS", "").strip()
    if not raw:
        return list(IMG2IMG_MODELS)
    # 格式："Nano Banana Pro=gemini-3-pro-image-preview,gpt-image-1.5=gpt-image-1.5"
    items: List[Dict[str, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        label, value = chunk.split("=", 1)
        label, value = label.strip(), value.strip()
        if label and value:
            items.append({"label": label, "value": value})
    return items or list(IMG2IMG_MODELS)


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    model: str = Field(min_length=1, max_length=100)
    aspect_ratio: str = Field(default="1:1", max_length=20)
    resolution: str = Field(default="2K", max_length=20)
    size: str = Field(default="1024x1024", max_length=20)
    quality: str = Field(default="auto", max_length=20)
    image_url: Optional[str] = Field(default=None, max_length=2000)
    image_urls: List[str] = Field(default_factory=list)
    mode: str = Field(default="text2img")  # text2img / img2img
    max_failover: int = Field(default=2, ge=0, le=5)


def _generation_log_dimensions(req: GenerateRequest) -> tuple[str, str]:
    """Return the two dimension slots shown in creation history.

    The database keeps the original two generic slots for compatibility:
    Nano Banana Pro uses aspect ratio/resolution, while GPT Image 2 uses
    size/quality in those same positions.
    """
    if req.mode == "text2img" and req.model == GPT_IMAGE_2_MODEL:
        return req.size.strip(), (req.quality or "auto").strip().lower()
    return req.aspect_ratio.strip(), req.resolution.strip()


class GenerateResponse(BaseModel):
    success: bool
    images: List[str] = []
    account_id: Optional[str] = None
    response_time_ms: Optional[int] = None
    message: Optional[str] = None
    retry_after: Optional[int] = None
    failure_scope: Optional[str] = None


class ReferenceUrlRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class GeneratedImageSaveError(Exception):
    """生成成功后，回存本地图片失败。"""

    def __init__(self, message: str, status_code: int = 502) -> None:
        self.message = redact_upstream_text(message)
        super().__init__(self.message)
        self.status_code = status_code


def _public_base_url(request: Optional[Request]) -> str:
    configured = (os.getenv("PUBLIC_BASE_URL") or "").strip()
    if configured:
        return configured.rstrip("/") + "/"

    if request is None:
        return "http://127.0.0.1:8001/"
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip()
    if forwarded_host:
        scheme = forwarded_proto or request.url.scheme
        return f"{scheme}://{forwarded_host}/"

    return str(request.base_url)


def _detect_uploaded_image_extension(data: bytes) -> Optional[str]:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _image_extension_from_content_type(content_type: str) -> Optional[str]:
    normalized = content_type.lower().split(";", 1)[0].strip()
    if normalized == "image/jpeg":
        return ".jpg"
    if normalized == "image/jpg":
        return ".jpg"
    if normalized == "image/png":
        return ".png"
    if normalized == "image/gif":
        return ".gif"
    if normalized == "image/webp":
        return ".webp"
    if normalized == "image/avif":
        return ".avif"
    if normalized == "image/bmp":
        return ".bmp"
    if normalized == "image/svg+xml":
        return ".svg"
    return None


def _image_extension_from_url(url: str) -> Optional[str]:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return None


def _generated_disk_snapshot() -> Dict[str, Any]:
    """Return a cheap local-disk admission snapshot for generated images."""
    try:
        GENERATED_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(GENERATED_IMAGE_DIR)
        writable_bytes = max(0, int(usage.free) - GENERATED_IMAGE_MIN_FREE_BYTES)
        return {
            "free_bytes": int(usage.free),
            "min_free_bytes": GENERATED_IMAGE_MIN_FREE_BYTES,
            "writable_bytes": writable_bytes,
            "can_write": writable_bytes >= GENERATED_IMAGE_MAX_BYTES,
        }
    except OSError as exc:
        logger.warning("generated image disk probe failed: %s", exc)
        return {
            "free_bytes": 0,
            "min_free_bytes": GENERATED_IMAGE_MIN_FREE_BYTES,
            "writable_bytes": 0,
            "can_write": False,
            "error": "disk_probe_failed",
        }


# 生成图下载共享 client：避免每张图/每次重试都重建 TCP/TLS 连接。
# 超时按请求覆盖（open_safe_stream 的 timeout 参数）。
_downloads_client: Optional[httpx.AsyncClient] = None
_downloads_client_lock = asyncio.Lock()


async def get_downloads_client() -> httpx.AsyncClient:
    global _downloads_client
    if _downloads_client is None:
        async with _downloads_client_lock:
            if _downloads_client is None:
                _downloads_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(GENERATED_IMAGE_TIMEOUT_SECONDS, connect=10.0),
                    follow_redirects=False,
                    trust_env=False,
                )
    return _downloads_client


async def close_downloads_client() -> None:
    global _downloads_client
    if _downloads_client is not None:
        client, _downloads_client = _downloads_client, None
        await client.aclose()


async def _save_generated_image(
    request: Optional[Request],
    source_url: str,
    *,
    public_base_url: Optional[str] = None,
) -> str:
    url = str(source_url or "").strip()
    if not url:
        raise GeneratedImageSaveError("生成成功，但保存图片失败：图片地址为空")
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        raise GeneratedImageSaveError("生成成功，但保存图片失败：图片地址不是有效的 http(s) 链接")
    try:
        await ensure_safe_outbound_url(url)
    except UnsafeOutboundURLError as exc:
        raise GeneratedImageSaveError(f"生成成功，但保存图片失败：下载地址不安全（{exc}）") from exc

    deadline = time.monotonic() + GENERATED_IMAGE_SAVE_TOTAL_TIMEOUT_SECONDS
    headers = {"User-Agent": "stackai-image-gen/1.0 (+generated-image-downloader)"}
    content_type = ""
    temp_path: Optional[Path] = None
    file_handle = None
    prefix = bytearray()
    total_bytes = 0

    async def _discard_temp_file() -> None:
        nonlocal temp_path, file_handle
        if file_handle is not None:
            file_handle.close()
            file_handle = None
        if temp_path is not None:
            with suppress(FileNotFoundError):
                temp_path.unlink()
            temp_path = None

    # disk_usage is a single statvfs call; keeping it synchronous avoids
    # consuming the default thread pool for every completed image.
    disk_snapshot = _generated_disk_snapshot()
    if not disk_snapshot["can_write"]:
        raise GeneratedImageSaveError(
            "生成成功，但保存图片失败：服务器磁盘空间不足，请稍后重试",
            status_code=503,
        )

    for attempt in range(1, GENERATED_IMAGE_DOWNLOAD_ATTEMPTS + 1):
        await _discard_temp_file()
        prefix = bytearray()
        total_bytes = 0
        remaining_budget = deadline - time.monotonic()
        if remaining_budget <= 0:
            raise GeneratedImageSaveError(
                "生成成功，但保存图片失败：下载图片超时（超过总预算）"
            )
        per_attempt_timeout = min(GENERATED_IMAGE_TIMEOUT_SECONDS, remaining_budget)
        timeout = httpx.Timeout(
            per_attempt_timeout,
            connect=min(per_attempt_timeout, 10.0),
        )
        try:
            client = await get_downloads_client()
            resp = await open_safe_stream(client, "GET", url, headers=headers, timeout=timeout)
            try:
                if resp.status_code >= 400:
                    err = GeneratedImageSaveError(
                        f"生成成功，但保存图片失败：下载上游图片返回 HTTP {resp.status_code}"
                    )
                    if resp.status_code == 429 or resp.status_code >= 500:
                        if attempt < GENERATED_IMAGE_DOWNLOAD_ATTEMPTS:
                            sleep_seconds = min(
                                GENERATED_IMAGE_DOWNLOAD_RETRY_BACKOFF_SECONDS * attempt,
                                max(0.0, deadline - time.monotonic()),
                            )
                            logger.warning(
                                "generated image download retryable http status: attempt=%s/%s status=%s url=%s",
                                attempt,
                                GENERATED_IMAGE_DOWNLOAD_ATTEMPTS,
                                resp.status_code,
                                url,
                            )
                            if sleep_seconds > 0:
                                await asyncio.sleep(sleep_seconds)
                            continue
                    raise err

                content_type = (resp.headers.get("content-type") or "").strip()
                fd, temp_name = tempfile.mkstemp(
                    prefix=".gen-",
                    dir=str(GENERATED_IMAGE_DIR),
                )
                os.close(fd)
                temp_path = Path(temp_name)
                file_handle = temp_path.open("wb")
                async for chunk in resp.aiter_bytes():
                    total_bytes += len(chunk)
                    if total_bytes > GENERATED_IMAGE_MAX_BYTES:
                        limit_mb = max(1, GENERATED_IMAGE_MAX_BYTES // (1024 * 1024))
                        raise GeneratedImageSaveError(
                            f"生成成功，但保存图片失败：图片超过 {limit_mb} MB 上限"
                        )
                    if len(prefix) < 32:
                        prefix.extend(chunk[: 32 - len(prefix)])
                    # The chunk is bounded by httpx and the write is local;
                    # yielding between network chunks keeps 60 streams
                    # responsive without creating one executor task per chunk.
                    file_handle.write(chunk)
                file_handle.flush()
                os.fsync(file_handle.fileno())
                file_handle.close()
                file_handle = None
                break
            finally:
                await resp.aclose()
            if total_bytes:
                break
        except UnsafeOutboundURLError as exc:
            await _discard_temp_file()
            raise GeneratedImageSaveError(
                f"生成成功，但保存图片失败：下载地址不安全（{exc}）"
            ) from exc
        except GeneratedImageSaveError:
            await _discard_temp_file()
            raise
        except httpx.TimeoutException as exc:
            await _discard_temp_file()
            if attempt < GENERATED_IMAGE_DOWNLOAD_ATTEMPTS and deadline - time.monotonic() > 0:
                sleep_seconds = min(
                    GENERATED_IMAGE_DOWNLOAD_RETRY_BACKOFF_SECONDS * attempt,
                    max(0.0, deadline - time.monotonic()),
                )
                logger.warning(
                    "generated image download timeout retry: attempt=%s/%s url=%s error=%s",
                    attempt,
                    GENERATED_IMAGE_DOWNLOAD_ATTEMPTS,
                    url,
                    exc,
                )
                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)
                continue
            raise GeneratedImageSaveError(f"生成成功，但保存图片失败：下载图片超时（{exc}）") from exc
        except httpx.HTTPError as exc:
            await _discard_temp_file()
            if attempt < GENERATED_IMAGE_DOWNLOAD_ATTEMPTS and deadline - time.monotonic() > 0:
                sleep_seconds = min(
                    GENERATED_IMAGE_DOWNLOAD_RETRY_BACKOFF_SECONDS * attempt,
                    max(0.0, deadline - time.monotonic()),
                )
                logger.warning(
                    "generated image download transport retry: attempt=%s/%s url=%s error=%s",
                    attempt,
                    GENERATED_IMAGE_DOWNLOAD_ATTEMPTS,
                    url,
                    exc,
                )
                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)
                continue
            raise GeneratedImageSaveError(f"生成成功，但保存图片失败：下载图片异常（{exc}）") from exc
        except Exception:
            await _discard_temp_file()
            raise

    if not temp_path or total_bytes <= 0:
        await _discard_temp_file()
        raise GeneratedImageSaveError("生成成功，但保存图片失败：下载到的图片为空")

    detected_suffix = _detect_uploaded_image_extension(bytes(prefix))
    content_type_suffix = _image_extension_from_content_type(content_type)
    normalized_content_type = content_type.lower().split(";", 1)[0].strip()
    if detected_suffix is None and normalized_content_type and not normalized_content_type.startswith("image/"):
        await _discard_temp_file()
        raise GeneratedImageSaveError(
            f"生成成功，但保存图片失败：下载内容不是图片（Content-Type={normalized_content_type}）"
        )

    suffix = detected_suffix or content_type_suffix or _image_extension_from_url(url)
    if suffix is None:
        await _discard_temp_file()
        raise GeneratedImageSaveError("生成成功，但保存图片失败：无法识别图片格式")

    filename = f"gen-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex}{suffix}"
    target = GENERATED_IMAGE_DIR / filename
    try:
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            with suppress(FileNotFoundError):
                temp_path.unlink()
    await _maybe_cleanup_uploads()
    base_url = (public_base_url or _public_base_url(request)).rstrip("/") + "/"
    return f"{base_url}uploads/generated/{filename}"


GENERATED_IMAGE_DOWNLOAD_CONCURRENCY = max(
    1, int(os.getenv("GENERATED_IMAGE_DOWNLOAD_CONCURRENCY", "32"))
)
_downloads_semaphore: Optional[asyncio.Semaphore] = None


def _get_downloads_semaphore() -> asyncio.Semaphore:
    global _downloads_semaphore
    if _downloads_semaphore is None:
        _downloads_semaphore = asyncio.Semaphore(GENERATED_IMAGE_DOWNLOAD_CONCURRENCY)
    return _downloads_semaphore


async def _save_generated_images(
    request: Optional[Request],
    image_urls: List[str],
    *,
    public_base_url: Optional[str] = None,
) -> List[str]:
    # 并行下载多张图（并发受信号量限制）；任一失败则整体失败。
    if not image_urls:
        return []

    async def _save_one(url: str) -> str:
        async with _get_downloads_semaphore():
            return await _save_generated_image(request, url, public_base_url=public_base_url)

    results = await asyncio.gather(*[_save_one(u) for u in image_urls], return_exceptions=True)
    first_error: Optional[BaseException] = None
    saved_urls: List[str] = []
    for item in results:
        if isinstance(item, BaseException):
            if first_error is None:
                first_error = item
        else:
            saved_urls.append(item)
    if first_error is not None:
        # Do not leave orphaned files when one image in a multi-image result
        # fails after another image was already committed.
        for saved_url in saved_urls:
            parsed_path = urlparse(str(saved_url))
            relative_path = parsed_path.path.removeprefix("/uploads/")
            candidate = (REFERENCE_UPLOAD_DIR / relative_path).resolve()
            if candidate.parent == GENERATED_IMAGE_DIR:
                with suppress(FileNotFoundError):
                    await asyncio.to_thread(candidate.unlink)
        raise first_error
    return saved_urls


_image_log_tasks: set[asyncio.Task] = set()
_image_log_started = 0
_image_log_completed = 0
_image_log_failed = 0


def image_persistence_snapshot() -> Dict[str, Any]:
    """Return local-storage status for the admin console.

    Image files are saved in the request coroutine; only history rows are
    best-effort background work.
    """
    return {
        "enabled": True,
        "history_log_async": True,
        "active_tasks": len(_image_log_tasks),
        "started": _image_log_started,
        "completed": _image_log_completed,
        "failed": _image_log_failed,
    }


async def _stop_image_persistence() -> None:
    """Wait briefly for currently scheduled history writes during shutdown."""
    if _image_log_tasks:
        await asyncio.gather(*list(_image_log_tasks), return_exceptions=True)


async def _write_generation_log(job: Dict[str, Any], saved_images: List[str], status: str, error_message: Optional[str]) -> None:
    global _image_log_completed, _image_log_failed
    try:
        from app.models.database import get_session_factory

        factory = get_session_factory()
        async with factory() as persist_session:
            persist_session.add(
                GenerationLog(
                    id=job["generation_id"],
                    timestamp=datetime.utcnow(),
                    user_id=job["user_id"],
                    account_id=job["account_id"],
                    mode=job["mode"],
                    model=job["model"],
                    aspect_ratio=job["aspect_ratio"],
                    resolution=job["resolution"],
                    prompt_preview=job["prompt_preview"],
                    image_url=job["reference_url"],
                    output_preview=(saved_images[0] if saved_images else None),
                    output_images=_serialize_output_images(saved_images),
                    response_time_ms=job["response_time_ms"],
                    status=status,
                    error_message=error_message,
                    is_stream=job["is_stream"],
                )
            )
            await persist_session.commit()
            _image_log_completed += 1
    except asyncio.CancelledError:
        raise
    except Exception:
        _image_log_failed += 1
        logger.exception("best-effort generation log write failed")


def _schedule_generation_log(job: Dict[str, Any], saved_images: List[str], status: str, error_message: Optional[str]) -> None:
    global _image_log_started
    try:
        task = asyncio.create_task(
            _write_generation_log(job, saved_images, status, error_message),
            name="generation-log-write",
        )
    except RuntimeError:
        return
    _image_log_started += 1
    _image_log_tasks.add(task)
    task.add_done_callback(_image_log_tasks.discard)


async def _save_and_log_images(job: Dict[str, Any]) -> List[str]:
    """Save generated images locally, then schedule only the history row.

    The caller waits only for the image files to be atomically committed; the
    history row is best-effort.
    """
    try:
        saved_images = await _save_generated_images(
            None,
            job["source_images"],
            public_base_url=job["public_base_url"],
        )
    except GeneratedImageSaveError as exc:
        _schedule_generation_log(job, [], "error", exc.message[:1000])
        raise
    except Exception as exc:
        error = GeneratedImageSaveError("生成成功，但保存图片失败：本地持久化异常")
        _schedule_generation_log(job, [], "error", str(exc)[:1000])
        raise error from exc
    _schedule_generation_log(job, saved_images, "success", None)
    return saved_images


def _normalize_reference_urls(req: GenerateRequest) -> List[str]:
    urls: List[str] = []
    for raw in [*(req.image_urls or []), req.image_url]:
        if raw is None:
            continue
        url = str(raw).strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def _validate_reference_count(urls: List[str]) -> None:
    if len(urls) > REFERENCE_UPLOAD_MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"参考图最多支持 {REFERENCE_UPLOAD_MAX_FILES} 张",
        )


def _reference_payload_value(urls: List[str]) -> str:
    if not urls:
        return ""
    if len(urls) == 1:
        return urls[0]
    return "\n".join(urls)


def _reference_log_value(urls: List[str]) -> Optional[str]:
    if not urls:
        return None
    if len(urls) == 1:
        return urls[0]
    return json.dumps(urls, ensure_ascii=False)


def _serialize_output_images(images: List[str]) -> Optional[str]:
    normalized: List[str] = []
    for raw in images or []:
        url = str(raw or "").strip()
        if url and url not in normalized:
            normalized.append(url)
    if not normalized:
        return None
    return json.dumps(normalized, ensure_ascii=False)


def _decode_output_images(raw: Optional[str], fallback: Optional[str] = None) -> List[str]:
    urls: List[str] = []
    text = str(raw or "").strip()
    if text:
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = text
        if isinstance(parsed, list):
            for item in parsed:
                url = str(item or "").strip()
                if url and url not in urls:
                    urls.append(url)
        elif isinstance(parsed, str):
            url = parsed.strip()
            if url:
                urls.append(url)

    fallback_url = str(fallback or "").strip()
    if fallback_url and fallback_url not in urls:
        urls.append(fallback_url)
    return urls


def _parse_content_length(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


@router.get("/options")
async def options() -> Dict[str, Any]:
    """前端启动时拉取可选项。按模式返回，同一结构：
    {
      "text2img": {
        "models": [{label, value}, ...],
        "aspect_ratios": [...],
        "resolutions": [...]
      },
      "img2img": {
        "models": [{label, value}, ...]
      }
    }
    """
    return {
        "text2img": {
            "models": _model_options_for_text2img(),
            "aspect_ratios": _split_env_list("UI_ASPECT_RATIOS", DEFAULT_ASPECT_RATIOS),
            "resolutions": _split_env_list("UI_RESOLUTIONS", DEFAULT_RESOLUTIONS),
            "sizes": GPT_IMAGE_2_SIZES,
            "qualities": GPT_IMAGE_2_QUALITIES,
        },
        "img2img": {
            "models": _model_options_for_img2img(),
        },
    }


@router.get("/generate/capacity")
async def generation_capacity(
    session: AsyncSession = Depends(get_session),
    current_principal: GenerationPrincipal = Depends(require_generation_principal),
) -> Dict[str, Any]:
    """Read-only capacity hint for the UI; submit-time admission is authoritative."""
    pool = get_account_pool_service()
    guard = get_generation_guard()
    rows = await session.execute(select(Account).where(Account.status == "active"))
    accounts = list(rows.scalars().all())
    account_capacity = sum(
        max(0, int(account.max_inflight or 1)) - pool.runtime_in_flight(account.id)
        for account in accounts
    )
    admission = guard.generation_admission.snapshot()
    available = max(
        0,
        min(
            account_capacity,
            max(0, guard.generation_admission.max_concurrent - int(admission["in_flight"])),
        ),
    )
    user_available = True
    if current_principal.user is not None:
        user = current_principal.user
        user_available = (
            get_user_auth_service().runtime_in_flight(user.id) < max(1, int(user.max_inflight or 1))
        )
    disk = _generated_disk_snapshot()
    return {
        "available_slots": available,
        "in_flight": int(admission["in_flight"]),
        "max_concurrent": guard.generation_admission.max_concurrent,
        "active_accounts": len(accounts),
        "account_available": max(0, account_capacity),
        "user_available": user_available,
        "disk_free_bytes": disk["free_bytes"],
        "disk_min_free_bytes": disk["min_free_bytes"],
        "disk_writable_bytes": disk["writable_bytes"],
        "disk_available": disk["can_write"],
    }


@router.get("/recent-images")
async def recent_images(
    limit: int = 24,
    session: AsyncSession = Depends(get_session),
    current_principal: GenerationPrincipal = Depends(require_generation_principal),
) -> Dict[str, Any]:
    limit = max(1, min(120, int(limit)))
    log_limit = min(max(limit * 4, 50), 300)

    stmt = (
        select(GenerationLog)
        .where(GenerationLog.status == "success")
        .where(
            or_(
                GenerationLog.output_images.is_not(None),
                GenerationLog.output_preview.is_not(None),
            )
        )
    )
    if current_principal.kind == "user" and current_principal.user_id:
        stmt = stmt.where(GenerationLog.user_id == current_principal.user_id)
    else:
        stmt = stmt.where(GenerationLog.user_id.is_(None))

    rows = await session.execute(
        stmt.order_by(GenerationLog.timestamp.desc()).limit(log_limit)
    )

    items: List[Dict[str, Any]] = []
    for log in rows.scalars().all():
        images = _decode_output_images(
            getattr(log, "output_images", None),
            getattr(log, "output_preview", None),
        )
        if not images:
            continue
        for idx, image_url in enumerate(images):
            is_gpt_image_2 = log.model == GPT_IMAGE_2_MODEL
            items.append(
                {
                    "id": f"{log.id}:{idx}",
                    "generation_id": log.id,
                    "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                    "image_url": image_url,
                    "prompt_preview": log.prompt_preview,
                    "mode": log.mode,
                    "model": log.model,
                    "aspect_ratio": log.aspect_ratio,
                    "resolution": log.resolution,
                    "size": log.aspect_ratio if is_gpt_image_2 else None,
                    "quality": log.resolution if is_gpt_image_2 else None,
                    "response_time_ms": log.response_time_ms,
                }
            )
            if len(items) >= limit:
                return {"items": items, "total": len(items)}

    return {"items": items, "total": len(items)}


@router.post("/reference-image")
async def upload_reference_image(
    request: Request,
    current_principal: GenerationPrincipal = Depends(require_generation_principal),
    file: UploadFile = File(...),
) -> Dict[str, Any]:
    del current_principal
    content_type = (file.content_type or "").lower().strip()
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="只支持上传图片文件")

    REFERENCE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # 流式分块落盘：内存占用恒定（一个 chunk），不再整文件读入。
    # 先写 .tmp，魔数校验通过后改名为最终文件；失败则清理半成品。
    token = uuid.uuid4().hex
    tmp_target = REFERENCE_UPLOAD_DIR / f"ref-{token}.tmp"
    limit_mb = max(1, REFERENCE_UPLOAD_MAX_BYTES // (1024 * 1024))
    suffix: Optional[str] = None
    total_bytes = 0
    fh = await asyncio.to_thread(open, tmp_target, "wb")
    try:
        try:
            while True:
                chunk = await file.read(UPLOAD_STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > REFERENCE_UPLOAD_MAX_BYTES:
                    raise HTTPException(status_code=413, detail=f"参考图不能超过 {limit_mb} MB")
                if suffix is None:
                    suffix = _detect_uploaded_image_extension(chunk)
                await asyncio.to_thread(fh.write, chunk)
        finally:
            await file.close()
            await asyncio.to_thread(fh.close)
    except Exception:
        await asyncio.to_thread(tmp_target.unlink, True)
        raise

    if total_bytes == 0:
        await asyncio.to_thread(tmp_target.unlink, True)
        raise HTTPException(status_code=400, detail="上传文件不能为空")
    if suffix is None:
        await asyncio.to_thread(tmp_target.unlink, True)
        raise HTTPException(status_code=400, detail="暂只支持 JPG、PNG、WEBP、GIF 图片")

    filename = f"ref-{token}{suffix}"
    target = REFERENCE_UPLOAD_DIR / filename
    await asyncio.to_thread(tmp_target.rename, target)
    await _maybe_cleanup_uploads()

    return {
        "url": f"{_public_base_url(request)}uploads/{filename}",
        "filename": file.filename or filename,
        "content_type": content_type or "application/octet-stream",
        "size_bytes": total_bytes,
    }


@router.post("/reference-url/validate")
async def validate_reference_url(
    req: ReferenceUrlRequest,
    current_principal: GenerationPrincipal = Depends(require_generation_principal),
) -> Dict[str, Any]:
    del current_principal
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="参考图 URL 不能为空")
    await _validate_image_url(url)
    return {"url": url, "ok": True}


async def _validate_image_url(url: str) -> None:
    """图生图参考图 URL 预检：必须可访问且 Content-Type 是 image/*。

    用 HEAD 优先；若拿不到可靠信息则回退到 GET 流式读取。
    任何明确判定不是图片的情况都抛 HTTPException(400)，给用户清晰报错。
    若图片超过 REFERENCE_UPLOAD_MAX_BYTES，也直接拦截。
    上游不可达 / 超时也直接报 400，这样就不会浪费一次 StackAI 调用。
    """
    if os.getenv("SKIP_IMAGE_URL_VALIDATION", "").lower() in {"1", "true", "yes"}:
        return

    try:
        await ensure_safe_outbound_url(url)
    except UnsafeOutboundURLError as exc:
        raise HTTPException(status_code=400, detail=f"参考图 URL 不安全：{exc}") from exc

    timeout = httpx.Timeout(8.0, connect=5.0)
    headers = {"User-Agent": "stackai-image-gen/1.0 (+image-url-validator)"}
    size_limit_bytes = REFERENCE_UPLOAD_MAX_BYTES
    limit_mb = max(1, size_limit_bytes // (1024 * 1024))
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers=headers,
            trust_env=False,
        ) as c:
            content_type: Optional[str] = None
            status_code = 0
            content_length: Optional[int] = None
            try:
                resp = await open_safe_stream(c, "HEAD", url, headers=headers)
                try:
                    status_code = resp.status_code
                    content_type = (resp.headers.get("content-type") or "").lower().split(";", 1)[0].strip()
                    content_length = _parse_content_length(resp.headers.get("content-length"))
                finally:
                    await resp.aclose()
            except httpx.RequestError:
                content_type = None
                content_length = None
            except UnsafeOutboundURLError as exc:
                raise HTTPException(status_code=400, detail=f"参考图 URL 不安全：{exc}") from exc

            if content_length is not None and content_length > size_limit_bytes:
                raise HTTPException(status_code=413, detail=f"参考图不能超过 {limit_mb} MB")

            # HEAD 不可信（部分 CDN/源站不支持），或没有 content-length 时，
            # 回退到 GET 流式读取，既能兜底 content-type，也能检测实际大小。
            if status_code >= 400 or not content_type or not content_type.startswith("image/") or content_length is None:
                resp = await open_safe_stream(c, "GET", url, headers=headers)
                try:
                    status_code = resp.status_code
                    content_type = (resp.headers.get("content-type") or "").lower().split(";", 1)[0].strip()
                    content_length = _parse_content_length(resp.headers.get("content-length"))

                    if status_code >= 400:
                        raise HTTPException(
                            status_code=400,
                            detail=f"参考图 URL 不可访问：HTTP {status_code}（请确认是直链且公网可访问）",
                        )
                    if not content_type.startswith("image/"):
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"参考图 URL 不是有效的图片直链："
                                f"Content-Type={content_type or '<空>'}。"
                                f"请使用以 .jpg/.png/.webp 结尾、直接返回图片字节的 URL"
                                f"（例如 imgur/i.imgur.com 直链；不要使用网页地址）"
                            ),
                        )
                    if content_length is not None and content_length > size_limit_bytes:
                        raise HTTPException(status_code=413, detail=f"参考图不能超过 {limit_mb} MB")

                    bytes_read = 0
                    read_cap = size_limit_bytes + 1 if content_length is None else min(size_limit_bytes + 1, 4096)
                    async for chunk in resp.aiter_bytes():
                        bytes_read += len(chunk)
                        if bytes_read > size_limit_bytes:
                            raise HTTPException(status_code=413, detail=f"参考图不能超过 {limit_mb} MB")
                        if bytes_read >= read_cap:
                            break
                finally:
                    await resp.aclose()

            if status_code >= 400:
                raise HTTPException(
                    status_code=400,
                    detail=f"参考图 URL 不可访问：HTTP {status_code}（请确认是直链且公网可访问）",
                )
            if not content_type.startswith("image/"):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"参考图 URL 不是有效的图片直链："
                        f"Content-Type={content_type or '<空>'}。"
                        f"请使用以 .jpg/.png/.webp 结尾、直接返回图片字节的 URL"
                        f"（例如 imgur/i.imgur.com 直链；不要使用网页地址）"
                    ),
                )
    except HTTPException:
        raise
    except UnsafeOutboundURLError as exc:
        raise HTTPException(status_code=400, detail=f"参考图 URL 不安全：{exc}") from exc
    except httpx.TimeoutException:
        raise HTTPException(status_code=400, detail="参考图 URL 访问超时（>8s），上游无法拉取，请换一个稳定的图片直链")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=400, detail=f"参考图 URL 无法访问：{type(exc).__name__}")


async def _validate_reference_urls(urls: List[str]) -> None:
    for url in urls:
        await _validate_image_url(url)


def _build_payload(req: GenerateRequest) -> Dict[str, str]:
    image_url = _reference_payload_value(_normalize_reference_urls(req))
    is_gpt_image_2 = req.mode == "text2img" and req.model == GPT_IMAGE_2_MODEL

    if req.mode == "img2img":
        # 图生图模式：比例/分辨率 UI 不暴露，但 Nano Banana Pro 工作流字段仍需占位。
        aspect = (req.aspect_ratio or IMG2IMG_DEFAULT_ASPECT_RATIO).strip() or IMG2IMG_DEFAULT_ASPECT_RATIO
        resolution = (req.resolution or IMG2IMG_DEFAULT_RESOLUTION).strip() or IMG2IMG_DEFAULT_RESOLUTION
    elif is_gpt_image_2:
        aspect = ""
        resolution = ""
    else:
        aspect = req.aspect_ratio.strip()
        resolution = req.resolution.strip()

    size = req.size.strip() if is_gpt_image_2 else ""
    quality = (req.quality or "auto").strip().lower() if is_gpt_image_2 else ""
    return {
        "in-0": req.prompt,
        "in-1": aspect,
        "in-2": resolution,
        "in-3": req.model,
        "in-4": size,
        "in-5": quality,
        "in-6": image_url,
    }


@router.post("/generate", response_model=GenerateResponse)
async def generate(
    request: Request,
    req: GenerateRequest,
    session: AsyncSession = Depends(get_session),
    current_principal: GenerationPrincipal = Depends(require_generation_principal),
) -> GenerateResponse:
    pool = get_account_pool_service()
    client = get_stackai_client()
    user_auth = get_user_auth_service()
    current_user_id = current_principal.user_id
    principal_log_name = _principal_log_name(current_principal)
    reference_urls = _normalize_reference_urls(req)
    reference_log_value = _reference_log_value(reference_urls)
    log_aspect_ratio, log_resolution = _generation_log_dimensions(req)
    user_slot_acquired = False
    user_should_count_usage = False

    # Authentication performs a read through the request-scoped session. End
    # that read transaction before URL validation, admission, or
    # upstream call so pooled SQLite connections are not held by idle requests.
    await _rollback_request_session_before_release(session)

    if req.mode not in {"text2img", "img2img"}:
        raise HTTPException(status_code=400, detail="mode 只能是 text2img 或 img2img")
    if req.mode == "img2img":
        _validate_reference_count(reference_urls)
        if not reference_urls:
            raise HTTPException(status_code=400, detail="img2img 必须提供参考图")
        await _validate_reference_urls(reference_urls)

    payload = _build_payload(req)
    # —— 流量守卫：熔断快速失败 → 每用户 RPM → 全局并发闸门 ——
    guard = get_generation_guard()
    breaker_remaining = guard.check_upstream()
    if breaker_remaining > 0:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "上游服务暂时不可用，请稍后重试",
                "retry_after": int(breaker_remaining) + 1,
                "failure_scope": UPSTREAM_ROUTE_SCOPE,
            },
        )
    if current_user_id is not None and guard.user_rpm.enabled:
        rpm_retry_after = await guard.check_user_rate(f"user:{current_user_id}")
        if rpm_retry_after > 0:
            raise HTTPException(
                status_code=429,
                detail={
                    "message": f"请求过于频繁，请 {int(rpm_retry_after) + 1} 秒后再试",
                    "retry_after": int(rpm_retry_after) + 1,
                    "failure_scope": "user_rate",
                },
            )
    # One shared admission controls both model scheduling and
    # global generation capacity.  Image downloads happen after this slot is
    # released and therefore do not consume generation capacity.
    generation_slot_acquired = False

    try:
        await guard.load_persisted_route_health()
        # Reject at the cheapest process-local gate before touching the user
        # concurrency row or the account pool. This keeps a saturated worker
        # from creating avoidable SQLite work.
        if not await guard.generation_admission.try_acquire(req.model):
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "GENERATION_BUSY",
                    "message": "生成调度繁忙，请稍后重试",
                    "retry_after": 5,
                    "failure_scope": "generation_capacity",
                },
                headers={"Retry-After": "5"},
            )
        generation_slot_acquired = True
        if not _generated_disk_snapshot()["can_write"]:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "IMAGE_STORAGE_BUSY",
                    "message": "服务器图片存储空间不足，请稍后重试",
                    "failure_scope": "image_storage",
                },
            )
        if current_user_id is not None:
            try:
                await user_auth.acquire_generation_slot(
                    session,
                    current_user_id,
                    user=current_principal.user,
                )
                user_slot_acquired = True
            except UserDisabledError as exc:
                raise HTTPException(status_code=403, detail=str(exc))
            except UserExpiredError as exc:
                raise HTTPException(status_code=403, detail=str(exc))
            except UserQuotaExceededError as exc:
                raise HTTPException(status_code=429, detail={"message": str(exc), "retry_after": 86400})
            except UserConcurrencyExceededError as exc:
                raise HTTPException(status_code=429, detail={"message": str(exc), "retry_after": 5})

        tried_ids: List[str] = []
        last_error: Optional[StackAIError] = None
        started = time.time()

        # A credential/configuration fault may use one alternate account. A
        # public route fault never fans out across the whole pool.
        for attempt in range(min(req.max_failover, 1) + 1):
            if not generation_slot_acquired and not await guard.generation_admission.try_acquire(req.model):
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "GENERATION_BUSY",
                        "message": "生成调度繁忙，请稍后重试",
                        "retry_after": 5,
                        "failure_scope": "generation_capacity",
                    },
                    headers={"Retry-After": "5"},
                )
            generation_slot_acquired = True
            try:
                account = await pool.select_account(
                    session,
                    exclude_ids=tried_ids,
                )
            except NoAvailableAccountError as exc:
                guard.generation_admission.release(req.model)
                generation_slot_acquired = False
                if last_error is not None:
                    await session.commit()
                    raise HTTPException(
                        status_code=last_error.status_code or 502,
                        detail={
                            "message": last_error.message,
                            "retry_after": _retry_after_for_stackai_error(last_error),
                            "failure_scope": _failure_scope_for_stackai_error(last_error),
                        },
                    )
                raise HTTPException(
                    status_code=503,
                    detail={"message": str(exc), "failure_scope": exc.failure_scope},
                )
            except NoCapacityError as exc:
                guard.generation_admission.release(req.model)
                generation_slot_acquired = False
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "GENERATION_BUSY",
                        "message": str(exc),
                        "retry_after": max(1, int(float(exc.retry_after or 5) + 0.999)),
                        "failure_scope": exc.failure_scope,
                    },
                    headers={"Retry-After": str(max(1, int(float(exc.retry_after or 5) + 0.999)))},
                )

            tried_ids.append(account.id)
            account_id = account.id
            request_started = time.time()
            route = build_route_key(
                base_url=client.base_url,
                org_id=account.org_id,
                flow_id=account.flow_id,
                mode=req.mode,
                model=req.model,
            )
            route_remaining = guard.check_upstream(route)
            if route_remaining > 0:
                await _rollback_request_session_before_release(session)
                await pool.release_account(
                    session,
                    account.id,
                    count_request=False,
                    slot_token=getattr(account, "_slot_token", None),
                )
                account_id = None
                raise HTTPException(
                    status_code=503,
                    detail={
                        "message": "该模型路由暂时不可用，请稍后重试",
                        "retry_after": max(1, int(route_remaining + 0.999)),
                        "failure_scope": UPSTREAM_ROUTE_SCOPE,
                    },
                )
            try:
                api_key = pool.decrypt_api_key(account)
            except ValueError as exc:
                decrypt_message = _account_decrypt_error_message()
                last_error = StackAIError(
                    decrypt_message,
                    status_code=500,
                    payload={"account_id": account.id, "account_name": account.name},
                )
                logger.error(
                    "generate account decrypt failed: principal=%s account=%s error=%s",
                    principal_log_name,
                    account.id[:8],
                    exc,
                )
                _isolate_account(
                    pool,
                    account.id,
                    account.name,
                    seconds=ACCOUNT_FAILURE_ISOLATION_SECONDS,
                    reason=f"decrypt_failed:{type(exc).__name__}",
                )
                session.add(
                    GenerationLog(
                        id=str(uuid.uuid4()),
                        timestamp=datetime.utcnow(),
                        user_id=current_user_id,
                        account_id=account.id,
                        mode=req.mode,
                        model=req.model,
                        aspect_ratio=log_aspect_ratio,
                        resolution=log_resolution,
                        prompt_preview=req.prompt,
                        image_url=reference_log_value,
                        output_preview=None,
                        response_time_ms=None,
                        status="error",
                        error_message=f"{decrypt_message}: {exc}"[:1000],
                        is_stream=False,
                    )
                )
                await session.commit()
                await _rollback_request_session_before_release(session)
                await pool.release_account(
                    session,
                    account.id,
                    count_request=False,
                    slot_token=getattr(account, "_slot_token", None),
                )
                account_id = None
                if generation_slot_acquired:
                    guard.generation_admission.release(req.model)
                    generation_slot_acquired = False
                continue
            # Private API Key 仅用于失败后拉 analytics；stackai 对 inference key 返回 401
            private_api_key = pool.decrypt_private_api_key(account)
            try:
                user_should_count_usage = True
                raw = await client.run_inference(
                    org_id=account.org_id,
                    flow_id=account.flow_id,
                    api_key=api_key,
                    payload=payload,
                )
                elapsed_ms = int((time.time() - request_started) * 1000)
                images = extract_image_urls(raw)
            # 上游 200 但没拿到图片：判定为失败（典型场景：参考图被 StackAI 节点拉取失败）
                if not images:
                # 优先从上游响应体里抽出真实错误
                    upstream_err: Optional[str] = None
                    upstream_err_len = 0
                    for s in _walk_strings(raw):
                        if _looks_like_error(s) and len(s) > upstream_err_len:
                            upstream_err = s
                            upstream_err_len = len(s)

                # 响应体里没线索 → 用 run_id 调 stackai analytics 拉 run detail，
                # 从中扫错误字段（即 stackai 控制台运行详情页里展示的 Errors）。
                    run_id_from_raw = raw.get("run_id") if isinstance(raw, dict) else None
                    run_detail_sync: Optional[Dict[str, Any]] = None
                    if upstream_err is None and run_id_from_raw and private_api_key:
                        try:
                            run_detail_sync = await client.fetch_run_detail(
                                org_id=account.org_id,
                                flow_id=account.flow_id,
                                api_key=private_api_key,
                                run_id=str(run_id_from_raw),
                            )
                        except Exception as exc:
                            logger.warning("fetch_run_detail unexpected error: %s", exc)
                    elif upstream_err is None and run_id_from_raw and not private_api_key:
                        logger.info(
                            "skip analytics fetch: account=%s has no private_api_key configured",
                            account.id[:8],
                        )
                    if isinstance(run_detail_sync, dict):
                        for s in _walk_strings(run_detail_sync):
                            if _looks_like_error(s) and len(s) > upstream_err_len:
                                upstream_err = s
                                upstream_err_len = len(s)

                    if upstream_err:
                        log_msg = redact_upstream_text(upstream_err)
                        shown_msg = _concise_upstream_error(upstream_err)
                    else:
                        if req.mode == "img2img":
                            log_msg = "图像未生成。可能是参考图链接无法被上游拉取——请确认链接是公网可直接访问的图片直链（非分享页、非需要登录的链接）。"
                        else:
                            log_msg = "图像未生成。请稍后重试，或调整提示词后再试。"
                        shown_msg = log_msg

                    logger.warning(
                        "generate failed: principal=%s account=%s mode=%s model=%s run_id=%s upstream_error=%s",
                        principal_log_name,
                        account.id[:8],
                        req.mode,
                        req.model,
                        run_id_from_raw,
                        log_msg,
                    )
                # 诊断：仍没扫到错误关键词时，dump run_detail 与原始响应，
                # 方便定位 stackai 错误字段实际叫什么
                    if upstream_err is None:
                        if run_detail_sync is not None:
                            try:
                                logger.warning(
                                    "st run_detail (no error hint matched):\n%s",
                                    json.dumps(redact_upstream_data(run_detail_sync), ensure_ascii=False)[:4000],
                                )
                            except Exception:
                                pass
                        try:
                            logger.warning(
                                "st sync raw response (no error hint matched):\n%s",
                                json.dumps(redact_upstream_data(raw), ensure_ascii=False)[:4000],
                            )
                        except Exception:
                            pass

                    session.add(
                        GenerationLog(
                            id=str(uuid.uuid4()),
                            timestamp=datetime.utcnow(),
                            user_id=current_user_id,
                            account_id=account.id,
                            mode=req.mode,
                            model=req.model,
                            aspect_ratio=log_aspect_ratio,
                            resolution=log_resolution,
                            prompt_preview=req.prompt,
                            image_url=reference_log_value,
                            output_preview=None,
                            response_time_ms=elapsed_ms,
                            status="error",
                            error_message=log_msg[:1000],
                            is_stream=False,
                        )
                    )
                    await session.commit()
                    # A successful HTTP response without an image is a route
                    # diagnostic, never proof that this account is broken.
                    guard.record_upstream_failure(route)
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "message": shown_msg,
                            "elapsed_ms": elapsed_ms,
                            "failure_scope": UPSTREAM_ROUTE_SCOPE,
                        },
                    )

                # 成功：先释放账号槽（统计记为成功），下载落盘不再占用账号容量。
                # 上游已出图，后续保存失败也算一次真实消耗。
                await _rollback_request_session_before_release(session)
                await pool.release_account(
                    session,
                    account.id,
                    count_request=True,
                    slot_token=getattr(account, "_slot_token", None),
                )
                account_id = None
                guard.record_upstream_success(route)
                if generation_slot_acquired:
                    guard.generation_admission.release(req.model)
                    generation_slot_acquired = False

                if user_slot_acquired and current_user_id is not None:
                    await user_auth.release_generation_slot(
                        session,
                        current_user_id,
                        count_usage=user_should_count_usage,
                    )
                    user_slot_acquired = False

                generation_id = str(uuid.uuid4())
                try:
                    local_images = await _save_and_log_images(
                        {
                            "generation_id": generation_id,
                            "source_images": list(images),
                            "public_base_url": _public_base_url(request),
                            "user_id": current_user_id,
                            "account_id": account.id,
                            "mode": req.mode,
                            "model": req.model,
                            "aspect_ratio": log_aspect_ratio,
                            "resolution": log_resolution,
                            "prompt_preview": req.prompt,
                            "reference_url": reference_log_value,
                            "response_time_ms": elapsed_ms,
                            "is_stream": False,
                        }
                    )
                except GeneratedImageSaveError as exc:
                    raise HTTPException(
                        status_code=exc.status_code,
                        detail={
                            "message": exc.message,
                            "failure_scope": "image_persistence",
                            "elapsed_ms": elapsed_ms,
                        },
                    ) from exc
                return GenerateResponse(
                    success=True,
                    images=local_images,
                    account_id=account.id,
                    response_time_ms=elapsed_ms,
                )
            except StackAIError as exc:
                elapsed_ms = int((time.time() - request_started) * 1000)
                last_error = exc
                decision = classify_stackai_error(exc)
                if decision.count_route_failure:
                    guard.record_upstream_failure(route, retry_after=exc.retry_after)
                if decision.isolate_account:
                    _isolate_account(
                        pool,
                        account.id,
                        account.name,
                        seconds=ACCOUNT_FAILURE_ISOLATION_SECONDS,
                        reason=f"{exc.status_code or '?'} {exc.message}",
                    )
                session.add(
                    GenerationLog(
                        id=str(uuid.uuid4()),
                        timestamp=datetime.utcnow(),
                        user_id=current_user_id,
                        account_id=account.id,
                        mode=req.mode,
                        model=req.model,
                        aspect_ratio=log_aspect_ratio,
                        resolution=log_resolution,
                        prompt_preview=req.prompt,
                        image_url=reference_log_value,
                        output_preview=None,
                        response_time_ms=elapsed_ms,
                        status="error",
                        error_message=f"{exc.status_code or '?'} {exc.message}"[:1000],
                        is_stream=False,
                    )
                )
                # 仅保留明确的请求参数错误在当前账号直接返回；
                # 401/403/404/429/5xx 更像账号配置或上游瞬时故障，允许切号。
                if not decision.failover:
                    # 先 commit 失败日志（避免被 dep 的 rollback 冲掉）
                    await session.commit()
                    raise HTTPException(
                        status_code=exc.status_code or 502,
                        detail={
                            "message": exc.message,
                            "retry_after": _retry_after_for_stackai_error(exc),
                            "failure_scope": decision.scope,
                        },
                    )
                logger.warning(
                    "generate failed on principal=%s account=%s attempt=%s: %s %s",
                    principal_log_name,
                    account.id,
                    attempt + 1,
                    exc.status_code,
                    exc.message,
                )
                await session.commit()
                continue
            finally:
                # 无论 return / raise / continue，都释放在途名额，避免 in_flight 泄漏。
                # 成功路径已提前释放（account_id 置 None）；到这里的一律记一次失败尝试统计。
                if account_id is not None:
                    try:
                        await _rollback_request_session_before_release(session)
                    finally:
                        await pool.release_account(
                            session,
                            account_id,
                            count_request=False,
                            slot_token=getattr(account, "_slot_token", None),
                        )
                if generation_slot_acquired:
                    guard.generation_admission.release(req.model)
                    generation_slot_acquired = False

        # 所有尝试失败
        total_ms = int((time.time() - started) * 1000)
        if last_error is not None:
            # 先 commit 失败日志（避免被 dep 的 rollback 冲掉）
            await session.commit()
            raise HTTPException(
                status_code=last_error.status_code or 502,
                detail={
                    "message": last_error.message,
                    "elapsed_ms": total_ms,
                    "retry_after": _retry_after_for_stackai_error(last_error),
                    "failure_scope": _failure_scope_for_stackai_error(last_error),
                },
            )
        raise HTTPException(status_code=500, detail="生成失败：未知错误")
    finally:
        if generation_slot_acquired:
            guard.generation_admission.release(req.model)
            generation_slot_acquired = False
        if user_slot_acquired and current_user_id is not None:
            await user_auth.release_generation_slot(
                session,
                current_user_id,
                count_usage=user_should_count_usage,
            )


# ===================== 流式生图 (SSE) =====================

def _sse(data: Dict[str, Any]) -> str:
    """打包成一条 SSE 数据帧。"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_comment(comment: str = "keepalive") -> str:
    """打包成一条 SSE 注释帧，用于维持长连接活跃。"""
    safe_comment = str(comment or "keepalive").replace("\n", " ").strip() or "keepalive"
    return f": {safe_comment}\n\n"


def _strip_email_suffix(name: str) -> str:
    if not name:
        return name
    return name.split("@", 1)[0] if "@" in name else name


def _principal_log_name(principal: GenerationPrincipal) -> str:
    if principal.kind == "admin":
        return f"admin:{principal.username}"
    return principal.username


def _account_decrypt_error_message() -> str:
    return "账号密钥解密失败，请检查后台账号配置"


def _stream_idle_timeout_message(timeout_seconds: float) -> str:
    timeout_text = int(timeout_seconds) if float(timeout_seconds).is_integer() else round(timeout_seconds, 1)
    return f"上游长时间无进度更新（>{timeout_text}s），请稍后重试"


def _stream_total_timeout_message(timeout_seconds: float) -> str:
    timeout_text = int(timeout_seconds) if float(timeout_seconds).is_integer() else round(timeout_seconds, 1)
    return f"生成总耗时超过 {timeout_text}s，已停止本次请求，请稍后重试"


def _stream_idle_timeout_seconds_for_request(req: GenerateRequest) -> float:
    timeout_seconds = GENERATE_STREAM_IDLE_TIMEOUT_SECONDS
    if req.mode == "text2img":
        if req.model == GPT_IMAGE_2_MODEL:
            timeout_seconds = max(timeout_seconds, GENERATE_STREAM_GPT_IMAGE_2_IDLE_TIMEOUT_SECONDS)
        if str(req.resolution or "").strip().upper() == "4K":
            timeout_seconds = max(timeout_seconds, GENERATE_STREAM_TEXT2IMG_4K_IDLE_TIMEOUT_SECONDS)
    return timeout_seconds


async def _iter_upstream_line_with_keepalive(
    upstream_stream: AsyncGenerator[str, None],
    *,
    request_started_monotonic: float,
    idle_timeout_seconds: float,
) -> AsyncGenerator[Optional[str], None]:
    """等待上游下一条事件；等待期间定期产出 None，供外层发送 SSE keepalive。"""
    wait_started_monotonic = time.monotonic()
    line_task = asyncio.create_task(anext(upstream_stream))
    try:
        while True:
            remaining_total_seconds = (
                GENERATE_STREAM_TOTAL_TIMEOUT_SECONDS
                - (time.monotonic() - request_started_monotonic)
            )
            if remaining_total_seconds <= 0:
                raise StackAIError(
                    _stream_total_timeout_message(GENERATE_STREAM_TOTAL_TIMEOUT_SECONDS),
                    status_code=504,
                    payload={
                        "reason": "total_timeout",
                        "total_timeout_seconds": GENERATE_STREAM_TOTAL_TIMEOUT_SECONDS,
                    },
                )

            waited_seconds = time.monotonic() - wait_started_monotonic
            remaining_idle_seconds = idle_timeout_seconds - waited_seconds
            if remaining_idle_seconds <= 0:
                raise StackAIError(
                    _stream_idle_timeout_message(idle_timeout_seconds),
                    status_code=504,
                    payload={
                        "reason": "idle_timeout",
                        "idle_timeout_seconds": idle_timeout_seconds,
                    },
                )

            wait_timeout = min(
                GENERATE_STREAM_KEEPALIVE_INTERVAL_SECONDS,
                remaining_total_seconds,
                remaining_idle_seconds,
            )
            done, _pending = await asyncio.wait(
                {line_task},
                timeout=wait_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if done:
                try:
                    yield line_task.result()
                except StopAsyncIteration:
                    return
                return
            yield None
    finally:
        if not line_task.done():
            line_task.cancel()
            with suppress(asyncio.CancelledError):
                await line_task


def _failure_scope_for_stackai_error(exc: StackAIError) -> str:
    return classify_stackai_error(exc).scope


def _retry_after_for_stackai_error(exc: StackAIError) -> Optional[int]:
    if exc.retry_after is None:
        return None
    return max(1, int(float(exc.retry_after) + 0.999))


def _isolate_account(
    pool,
    account_id: Optional[str],
    account_name: Optional[str],
    *,
    seconds: float,
    reason: str,
) -> None:
    normalized_id = str(account_id or "").strip()
    if not normalized_id:
        return
    pool.mark_account_isolated(
        normalized_id,
        seconds=seconds,
        reason=reason[:200],
    )
    logger.warning(
        "account failure isolated: account=%s name=%s seconds=%.1f reason=%s",
        normalized_id[:8],
        account_name or "<unknown>",
        seconds,
        reason[:200],
    )


@router.post("/generate/stream")
async def generate_stream(
    request: Request,
    req: GenerateRequest,
    session: AsyncSession = Depends(get_session),
    current_principal: GenerationPrincipal = Depends(require_generation_principal),
):
    """流式生图：通过 SSE 把上游进度事件转发给前端，最终事件携带图片 URL。

    与 /api/generate 行为差异：
    - 一旦开始流式输出就不再切号；但在首个 SSE 事件发出前，允许跳过明显损坏的账号配置。
    - 4xx / 5xx 都以一条 `type=error` 事件结束流。
    """
    pool = get_account_pool_service()
    client = get_stackai_client()
    user_auth = get_user_auth_service()
    current_user_id = current_principal.user_id
    principal_log_name = _principal_log_name(current_principal)
    reference_urls = _normalize_reference_urls(req)
    reference_log_value = _reference_log_value(reference_urls)
    log_aspect_ratio, log_resolution = _generation_log_dimensions(req)

    # Release the auth lookup transaction before returning StreamingResponse.
    # The stream body can live for minutes; it must not inherit a checked-out DB
    # connection from dependency resolution.
    await _rollback_request_session_before_release(session)

    if req.mode not in {"text2img", "img2img"}:
        raise HTTPException(status_code=400, detail="mode 只能是 text2img 或 img2img")
    if req.mode == "img2img":
        _validate_reference_count(reference_urls)
        if not reference_urls:
            raise HTTPException(status_code=400, detail="img2img 必须提供参考图")
        await _validate_reference_urls(reference_urls)

    payload = _build_payload(req)
    # —— 流量守卫：在返回 StreamingResponse 前完成全部容量准入 ——
    guard = get_generation_guard()

    generation_slot_acquired = False
    user_slot_acquired = False
    user_should_count_usage = False
    admitted_account = None
    admitted_route: Optional[str] = None

    await guard.load_persisted_route_health()
    breaker_remaining = guard.check_upstream()
    if breaker_remaining > 0:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "上游服务暂时不可用，请稍后重试",
                "retry_after": int(breaker_remaining) + 1,
                "failure_scope": UPSTREAM_ROUTE_SCOPE,
            },
        )
    if current_user_id is not None and guard.user_rpm.enabled:
        rpm_retry_after = await guard.check_user_rate(f"user:{current_user_id}")
        if rpm_retry_after > 0:
            raise HTTPException(
                status_code=429,
                detail={
                    "message": f"请求过于频繁，请 {int(rpm_retry_after) + 1} 秒后再试",
                    "retry_after": int(rpm_retry_after) + 1,
                    "failure_scope": "user_rate",
                },
                headers={"Retry-After": str(int(rpm_retry_after) + 1)},
            )

    try:
        # Global admission is the first mutable gate. A full worker returns
        # before acquiring per-user or account state from SQLite.
        if not await guard.generation_admission.try_acquire(req.model):
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "GENERATION_BUSY",
                    "message": "生成调度繁忙，请稍后重试",
                    "retry_after": 5,
                    "failure_scope": "generation_capacity",
                },
                headers={"Retry-After": "5"},
            )
        generation_slot_acquired = True
        if not _generated_disk_snapshot()["can_write"]:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "IMAGE_STORAGE_BUSY",
                    "message": "服务器图片存储空间不足，请稍后重试",
                    "failure_scope": "image_storage",
                },
            )
        if current_user_id is not None:
            try:
                await user_auth.acquire_generation_slot(
                    session,
                    current_user_id,
                )
                user_slot_acquired = True
            except UserDisabledError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except UserExpiredError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except UserQuotaExceededError as exc:
                raise HTTPException(
                    status_code=429,
                    detail={"message": str(exc), "retry_after": 86400},
                    headers={"Retry-After": "86400"},
                ) from exc
            except UserConcurrencyExceededError as exc:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "USER_GENERATION_BUSY",
                        "message": str(exc),
                        "retry_after": 5,
                        "failure_scope": "user_capacity",
                    },
                    headers={"Retry-After": "5"},
                ) from exc

        # The global slot was acquired before user/account admission.
        assert generation_slot_acquired
        admitted_account = await get_account_pool_service().select_account(
            session,
        )
        admitted_route = build_route_key(
            base_url=client.base_url,
            org_id=admitted_account.org_id,
            flow_id=admitted_account.flow_id,
            mode=req.mode,
            model=req.model,
        )
        admitted_route_remaining = guard.check_upstream(admitted_route)
        if admitted_route_remaining > 0:
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "该模型路由暂时不可用，请稍后重试",
                    "retry_after": max(1, int(admitted_route_remaining + 0.999)),
                    "failure_scope": UPSTREAM_ROUTE_SCOPE,
                },
            )
    except HTTPException:
        if admitted_account is not None:
            await get_account_pool_service().release_account(
                session,
                admitted_account.id,
                count_request=False,
                slot_token=getattr(admitted_account, "_slot_token", None),
            )
            admitted_account = None
        if generation_slot_acquired:
            guard.generation_admission.release(req.model)
            generation_slot_acquired = False
        if user_slot_acquired and current_user_id is not None:
            await user_auth.release_generation_slot(
                session,
                current_user_id,
                count_usage=False,
            )
            user_slot_acquired = False
        raise
    except NoAvailableAccountError as exc:
        if generation_slot_acquired:
            guard.generation_admission.release(req.model)
            generation_slot_acquired = False
        if user_slot_acquired and current_user_id is not None:
            await user_auth.release_generation_slot(session, current_user_id, count_usage=False)
            user_slot_acquired = False
        raise HTTPException(status_code=503, detail={"message": str(exc), "failure_scope": exc.failure_scope}) from exc
    except NoCapacityError as exc:
        if generation_slot_acquired:
            guard.generation_admission.release(req.model)
            generation_slot_acquired = False
        if user_slot_acquired and current_user_id is not None:
            await user_auth.release_generation_slot(session, current_user_id, count_usage=False)
            user_slot_acquired = False
        retry_after = max(1, int(float(exc.retry_after or 5) + 0.999))
        raise HTTPException(
            status_code=429,
            detail={
                "code": "GENERATION_BUSY",
                "message": str(exc),
                "retry_after": retry_after,
                "failure_scope": exc.failure_scope,
            },
            headers={"Retry-After": str(retry_after)},
        ) from exc

    async def event_source() -> AsyncGenerator[str, None]:
        nonlocal generation_slot_acquired, user_slot_acquired, user_should_count_usage, admitted_account

        async def _release_user_slot_if_needed() -> None:
            nonlocal user_slot_acquired
            if not user_slot_acquired or current_user_id is None:
                return
            # The client may cancel an SSE task while this cleanup is running.
            # Keep the volatile counter release alive independently of the
            # cancelled response task.
            release_task = asyncio.create_task(
                user_auth.release_generation_slot(
                    session,
                    current_user_id,
                    count_usage=user_should_count_usage,
                )
            )
            try:
                await asyncio.shield(release_task)
            except asyncio.CancelledError:
                logger.debug("SSE client cancelled while releasing user slot")
            user_slot_acquired = False

        try:
            async def _release_account_if_needed(account_obj, *, count_request=None, slot_token=None):
                if account_obj is None:
                    return None
                account_id = str(account_obj).strip()
                if not account_id:
                    return None
                try:
                    await _rollback_request_session_before_release(session)
                finally:
                    # Account capacity is in memory and must be returned even
                    # when the browser closes the SSE connection mid-request.
                    release_task = asyncio.create_task(
                        pool.release_account(
                            session,
                            account_id,
                            count_request=count_request,
                            slot_token=slot_token,
                        )
                    )
                    try:
                        await asyncio.shield(release_task)
                    except asyncio.CancelledError:
                        logger.debug("SSE client cancelled while releasing account slot")
                return None

            tried_ids: List[str] = []
            for attempt in range(min(req.max_failover, 1) + 1):
                account = admitted_account if attempt == 0 else None
                account_id = account.id if account is not None else None
                account_name = None
                account_short = None
                generation_slot_acquired = account is not None and attempt == 0
                try:
                    if account is None:
                        if not await guard.generation_admission.try_acquire(req.model):
                            await _release_user_slot_if_needed()
                            yield _sse(
                                {
                                    "type": "error",
                                    "message": "生成调度繁忙，请稍后重试",
                                    "status_code": 429,
                                    "retry_after": 5,
                                    "failure_scope": "generation_capacity",
                                }
                            )
                            return
                        generation_slot_acquired = True
                        try:
                            account = await pool.select_account(
                                session,
                                exclude_ids=tried_ids,
                            )
                        except NoAvailableAccountError as exc:
                            guard.generation_admission.release(req.model)
                            generation_slot_acquired = False
                            await _release_user_slot_if_needed()
                            yield _sse(
                                {
                                    "type": "error",
                                    "message": str(exc),
                                    "status_code": 503,
                                    "failure_scope": exc.failure_scope,
                                }
                            )
                            return
                        except NoCapacityError as exc:
                            guard.generation_admission.release(req.model)
                            generation_slot_acquired = False
                            await _release_user_slot_if_needed()
                            yield _sse(
                                {
                                    "type": "error",
                                    "message": str(exc),
                                    "status_code": 429,
                                    "retry_after": 5,
                                    "failure_scope": exc.failure_scope,
                                }
                            )
                            return

                    tried_ids.append(account.id)
                    account_id = account.id
                    account_name = account.name
                    route = build_route_key(
                        base_url=client.base_url,
                        org_id=account.org_id,
                        flow_id=account.flow_id,
                        mode=req.mode,
                        model=req.model,
                    )
                    # The first account was admitted before StreamingResponse
                    # was returned.  Re-checking it here would consume/deny a
                    # half-open route-breaker probe twice.  Failover accounts
                    # are checked normally after they are selected.
                    if attempt == 0 and admitted_route == route:
                        route_remaining = 0.0
                    else:
                        route_remaining = guard.check_upstream(route)
                    if route_remaining > 0:
                        yield _sse(
                            {
                                "type": "error",
                                "status_code": 503,
                                "message": "该模型路由暂时不可用，请稍后重试",
                                "retry_after": max(1, int(route_remaining + 0.999)),
                                "failure_scope": UPSTREAM_ROUTE_SCOPE,
                            }
                        )
                        return
                    try:
                        api_key = pool.decrypt_api_key(account)
                    except ValueError as exc:
                        decrypt_message = _account_decrypt_error_message()
                        logger.error(
                            "generate stream account decrypt failed: principal=%s account=%s error=%s",
                            principal_log_name,
                            account.id[:8],
                            exc,
                        )
                        _isolate_account(
                            pool,
                            account.id,
                            account.name,
                            seconds=ACCOUNT_FAILURE_ISOLATION_SECONDS,
                            reason=f"decrypt_failed:{type(exc).__name__}",
                        )
                        session.add(
                            GenerationLog(
                                id=str(uuid.uuid4()),
                                timestamp=datetime.utcnow(),
                                user_id=current_user_id,
                                account_id=account.id,
                                mode=req.mode,
                                model=req.model,
                                aspect_ratio=log_aspect_ratio,
                                resolution=log_resolution,
                                prompt_preview=req.prompt,
                                image_url=reference_log_value,
                                output_preview=None,
                                response_time_ms=None,
                                status="error",
                                error_message=f"{decrypt_message}: {exc}"[:1000],
                                is_stream=True,
                            )
                        )
                        await session.commit()
                        continue

                    private_api_key = pool.decrypt_private_api_key(account)
                    account_short = _strip_email_suffix(account_name or "")
                    started = time.time()
                    started_monotonic = time.monotonic()
                    start_emitted = False
                    saw_upstream_event = False
                    last_outputs: Optional[Dict[str, Any]] = None
                    last_event: Optional[Dict[str, Any]] = None
                    last_total_nodes: Optional[int] = None
                    last_completed_nodes: Optional[int] = None
                    collected_urls: List[str] = []
                    seen_urls: set = set()
                    upstream_error_full: Optional[str] = None
                    upstream_error_max_len: int = 0
                    recent_raw_lines: List[str] = []
                    raw_lines_limit = 80
                    stream_idle_timeout_seconds = _stream_idle_timeout_seconds_for_request(req)

                    try:
                        user_should_count_usage = True
                        upstream_stream = client.stream_inference(
                            org_id=account.org_id,
                            flow_id=account.flow_id,
                            api_key=api_key,
                            payload=payload,
                        )
                        try:
                            if not start_emitted:
                                start_event = {
                                    "type": "start",
                                    "account_id": account_id,
                                    "account_name": account_name,
                                    "account_short": account_short,
                                    "mode": req.mode,
                                    "model": req.model,
                                }
                                yield _sse(start_event)
                                start_emitted = True
                            while True:
                                line_received = False
                                async for line in _iter_upstream_line_with_keepalive(
                                    upstream_stream,
                                    request_started_monotonic=started_monotonic,
                                    idle_timeout_seconds=stream_idle_timeout_seconds,
                                ):
                                    if line is None:
                                        yield _sse_comment()
                                        continue
                                    line_received = True
                                    break
                                if not line_received:
                                    break
                                saw_upstream_event = True
                                safe_line = redact_upstream_event_text(line)
                                yield _sse({"type": "upstream", "line": safe_line})
                                if len(recent_raw_lines) >= raw_lines_limit:
                                    recent_raw_lines.pop(0)
                                recent_raw_lines.append(safe_line)
                                try:
                                    # 内部继续使用原始事件提取图片 URL；仅发送到浏览器和日志的副本脱敏。
                                    parsed = json.loads(line)
                                except Exception:
                                    parsed = None
                                if isinstance(parsed, dict):
                                    last_event = parsed
                                    outputs = parsed.get("outputs")
                                    if (
                                        isinstance(outputs, dict)
                                        and outputs
                                        and outputs.get("type") != "stream_complete"
                                    ):
                                        last_outputs = outputs
                                    for url in extract_image_urls(parsed):
                                        if url not in seen_urls:
                                            seen_urls.add(url)
                                            collected_urls.append(url)
                                    for s in _walk_strings(parsed):
                                        if _looks_like_error(s) and len(s) > upstream_error_max_len:
                                            upstream_error_full = s
                                            upstream_error_max_len = len(s)
                                    pd = parsed.get("progress_data")
                                    if isinstance(pd, dict):
                                        if isinstance(pd.get("total_nodes"), int):
                                            last_total_nodes = pd["total_nodes"]
                                        if isinstance(pd.get("completed_nodes"), int):
                                            last_completed_nodes = pd["completed_nodes"]
                        finally:
                            await upstream_stream.aclose()
                    except StackAIError as exc:
                        elapsed_ms = int((time.time() - started) * 1000)
                        decision = classify_stackai_error(exc)
                        if decision.count_route_failure:
                            guard.record_upstream_failure(route, retry_after=exc.retry_after)
                        if decision.isolate_account:
                            _isolate_account(
                                pool,
                                account.id,
                                account_name,
                                seconds=ACCOUNT_FAILURE_ISOLATION_SECONDS,
                                reason=f"{exc.status_code or '?'} {exc.message}",
                            )
                        session.add(
                            GenerationLog(
                                id=str(uuid.uuid4()),
                                timestamp=datetime.utcnow(),
                                user_id=current_user_id,
                                account_id=account.id,
                                mode=req.mode,
                                model=req.model,
                                aspect_ratio=log_aspect_ratio,
                                resolution=log_resolution,
                                prompt_preview=req.prompt,
                                image_url=reference_log_value,
                                output_preview=None,
                                response_time_ms=elapsed_ms,
                                status="error",
                                error_message=f"{exc.status_code or '?'} {exc.message}"[:1000],
                                is_stream=True,
                            )
                        )
                        await session.commit()
                        if not saw_upstream_event and decision.failover and attempt < min(req.max_failover, 1):
                            logger.warning(
                                "generate stream prestart failover: principal=%s account=%s attempt=%s status=%s message=%s",
                                principal_log_name,
                                account.id[:8],
                                attempt + 1,
                                exc.status_code,
                                exc.message,
                            )
                            continue
                        account_id = await _release_account_if_needed(
                            account_id,
                            count_request=False,
                            slot_token=getattr(account, "_slot_token", None),
                        )
                        account = None
                        await _release_user_slot_if_needed()
                        yield _sse(
                            {
                                "type": "error",
                                "status_code": exc.status_code,
                                "message": exc.message,
                                "elapsed_ms": elapsed_ms,
                                "retry_after": _retry_after_for_stackai_error(exc),
                                "failure_scope": decision.scope,
                            }
                        )
                        return

                    elapsed_ms = int((time.time() - started) * 1000)
                    final: Dict[str, Any] = {}
                    if last_outputs is not None:
                        final["outputs"] = last_outputs
                    if last_event is not None:
                        for key in ("run_id", "metadata", "citations", "text", "delta", "files"):
                            if key in last_event:
                                final[key] = last_event[key]

                    images = collected_urls if collected_urls else extract_image_urls(final)
                    if not images:
                        run_id_from_stream = final.get("run_id") if isinstance(final, dict) else None
                        run_detail: Optional[Dict[str, Any]] = None
                        if upstream_error_full is None and run_id_from_stream and private_api_key:
                            try:
                                run_detail = await client.fetch_run_detail(
                                    org_id=account.org_id,
                                    flow_id=account.flow_id,
                                    api_key=private_api_key,
                                    run_id=str(run_id_from_stream),
                                )
                            except Exception as exc:
                                logger.warning("fetch_run_detail unexpected error: %s", exc)
                        elif upstream_error_full is None and run_id_from_stream and not private_api_key:
                            logger.info(
                                "skip analytics fetch: account=%s has no private_api_key configured",
                                account.id[:8],
                            )
                        if isinstance(run_detail, dict):
                            for s in _walk_strings(run_detail):
                                if _looks_like_error(s) and len(s) > upstream_error_max_len:
                                    upstream_error_full = s
                                    upstream_error_max_len = len(s)

                        is_zero_stage_failure = _is_zero_stage_failure(
                            last_total_nodes,
                            last_completed_nodes,
                        )
                        if upstream_error_full:
                            log_msg = redact_upstream_text(upstream_error_full)
                            shown_msg = _concise_upstream_error(upstream_error_full)
                        else:
                            stage_hint = ""
                            if (
                                isinstance(last_total_nodes, int)
                                and isinstance(last_completed_nodes, int)
                                and last_completed_nodes < last_total_nodes
                            ):
                                stage_hint = f"（已完成 {last_completed_nodes}/{last_total_nodes} 阶段）"
                            if is_zero_stage_failure:
                                log_msg = "Image not generated. Plan credit exhausted."
                                shown_msg = log_msg
                            elif req.mode == "img2img":
                                log_msg = (
                                    f"图像未生成{stage_hint}。可能是参考图链接无法被上游拉取——"
                                    f"请确认链接是公网可直接访问的图片直链（非分享页、非需要登录的链接）。"
                                )
                                shown_msg = (
                                    "图像未生成。可能是参考图链接无法被上游拉取——"
                                    "请确认链接是公网可直接访问的图片直链（非分享页、非需要登录的链接）。"
                                )
                            else:
                                log_msg = (
                                    f"图像未生成{stage_hint}。请稍后重试，或调整提示词后再试。"
                                )
                                shown_msg = "图像未生成。请稍后重试，或调整提示词后再试。"

                        logger.warning(
                            "generate stream failed: principal=%s account=%s mode=%s model=%s run_id=%s upstream_error=%s",
                            principal_log_name,
                            account.id[:8],
                            req.mode,
                            req.model,
                            run_id_from_stream,
                            log_msg,
                        )
                        if upstream_error_full is None:
                            if run_detail is not None:
                                try:
                                    logger.warning(
                                        "st run_detail (no error hint matched):\n%s",
                                        json.dumps(redact_upstream_data(run_detail), ensure_ascii=False)[:4000],
                                    )
                                except Exception:
                                    pass
                            if recent_raw_lines:
                                tail = recent_raw_lines[-30:]
                                logger.warning(
                                    "st stream tail (%d events):\n%s",
                                    len(tail),
                                    "\n".join(tail),
                                )
                        # Zero-stage/no-image results are ambiguous route/model
                        # failures, not evidence that the selected account is bad.
                        guard.record_upstream_failure(route)

                        session.add(
                            GenerationLog(
                                id=str(uuid.uuid4()),
                                timestamp=datetime.utcnow(),
                                user_id=current_user_id,
                                account_id=account.id,
                                mode=req.mode,
                                model=req.model,
                                aspect_ratio=log_aspect_ratio,
                                resolution=log_resolution,
                                prompt_preview=req.prompt,
                                image_url=reference_log_value,
                                output_preview=None,
                                response_time_ms=elapsed_ms,
                                status="error",
                                error_message=log_msg[:1000],
                                is_stream=True,
                            )
                        )
                        await session.commit()
                        account_id = await _release_account_if_needed(
                            account_id,
                            count_request=False,
                            slot_token=getattr(account, "_slot_token", None),
                        )
                        account = None
                        await _release_user_slot_if_needed()
                        yield _sse(
                            {
                                "type": "error",
                                "status_code": 502,
                                "message": shown_msg,
                                "elapsed_ms": elapsed_ms,
                                "failure_scope": UPSTREAM_ROUTE_SCOPE,
                            }
                        )
                        return

                    # 成功：先释放账号槽（统计记为成功），下载落盘不再占用账号容量。
                    # 上游已出图，后续保存失败也算一次真实消耗。
                    completed_account_id = account_id
                    completed_slot_token = getattr(account, "_slot_token", None)
                    account_id = await _release_account_if_needed(
                        account_id,
                            count_request=True,
                        slot_token=completed_slot_token,
                    )
                    guard.record_upstream_success(route)
                    if generation_slot_acquired:
                        guard.generation_admission.release(req.model)
                        generation_slot_acquired = False

                    await _release_user_slot_if_needed()
                    try:
                        local_images = await _save_and_log_images(
                            {
                                "generation_id": str(uuid.uuid4()),
                                "source_images": list(images),
                                "public_base_url": _public_base_url(request),
                                "user_id": current_user_id,
                                "account_id": completed_account_id,
                                "mode": req.mode,
                                "model": req.model,
                                "aspect_ratio": log_aspect_ratio,
                                "resolution": log_resolution,
                                "prompt_preview": req.prompt,
                                "reference_url": reference_log_value,
                                "response_time_ms": elapsed_ms,
                                "is_stream": True,
                            }
                        )
                    except GeneratedImageSaveError as exc:
                        yield _sse(
                            {
                                "type": "error",
                                "status_code": exc.status_code,
                                "message": exc.message,
                                "failure_scope": "image_persistence",
                                "elapsed_ms": elapsed_ms,
                            }
                        )
                        return
                    complete_event = {
                        "type": "complete",
                        "images": local_images,
                        "account_id": completed_account_id,
                        "account_name": account_name,
                        "account_short": account_short,
                        "response_time_ms": elapsed_ms,
                    }
                    yield _sse(complete_event)
                    return
                finally:
                    if account_id is not None:
                        account_id = await _release_account_if_needed(
                            account_id,
                            count_request=False,
                            slot_token=getattr(account, "_slot_token", None),
                        )
                    if generation_slot_acquired:
                        guard.generation_admission.release(req.model)
                        generation_slot_acquired = False
            await _release_user_slot_if_needed()
            yield _sse(
                {
                    "type": "error",
                    "status_code": 500,
                    "message": _account_decrypt_error_message(),
                }
            )
            return
        finally:
            if generation_slot_acquired:
                guard.generation_admission.release(req.model)
                generation_slot_acquired = False
            await _release_user_slot_if_needed()
    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁用某些反代的缓冲（nginx）
        },
    )
