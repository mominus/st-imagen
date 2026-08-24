#!/usr/bin/env python3
"""混合模型真实生图压测。

本脚本只把请求元数据、SSE 事件的结构化摘要和脱敏错误写入报告，不保存
上游原始事件、图片 URL、API token 或完整账号名。每个阶段的请求会同时
发起，GPT Image 2 与 Nano Banana Pro 交错提交。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import statistics
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values


GPT_SIZES = [
    "auto",
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "2048x2048",
    "3840x2160",
]
NANO_COMBOS = [
    (ratio, resolution)
    for ratio in ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]
    for resolution in ["1K", "2K", "4K"]
]
PROMPTS = [
    "a small red apple on white marble, soft natural light",
    "a tiny robot watering a plant in a sunlit kitchen",
    "an astronaut riding a horse on mars, cinematic",
    "a cozy bookstore at dusk with warm lamps",
    "a steaming bowl of ramen on a wooden table",
    "a paper crane beside a vintage typewriter, soft focus",
    "a hummingbird near a hibiscus flower, detailed",
    "a vintage red bicycle against an autumn brick wall",
    "a glass jar of honey beside blueberries, studio light",
    "a wooden treehouse in a misty forest at dawn",
]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_iso_now() -> str:
    # GenerationLog uses naive datetime.utcnow() in SQLite.
    return datetime.utcnow().isoformat(timespec="milliseconds")


def digest(value: Any) -> Optional[str]:
    if value is None or str(value) == "":
        return None
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:12]


_URL_RE = re.compile(r"https?://[^\s\"']+")
_BEARER_RE = re.compile(r"(?i)bearer\s+[^\s,;]+")
_TOKEN_RE = re.compile(r"(?i)(?:api[_ -]?key|token|secret|password)[=: ]+[^\s,;]{8,}")


def safe_text(value: Any, limit: int = 360) -> Optional[str]:
    if value is None:
        return None
    text = str(value).replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    text = _URL_RE.sub("<url>", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _TOKEN_RE.sub(lambda m: m.group(0).split(" ", 1)[0] + "=<redacted>", text)
    return text[:limit]


def percentile(values: Iterable[float], p: float) -> Optional[float]:
    ordered = sorted(float(v) for v in values if v is not None)
    if not ordered:
        return None
    return round(ordered[int((len(ordered) - 1) * p)], 1)


def stats(values: Iterable[float]) -> Dict[str, Any]:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"count": 0}
    return {
        "count": len(vals),
        "min_ms": round(min(vals), 1),
        "mean_ms": round(statistics.fmean(vals), 1),
        "p50_ms": percentile(vals, 0.50),
        "p95_ms": percentile(vals, 0.95),
        "p99_ms": percentile(vals, 0.99),
        "max_ms": round(max(vals), 1),
    }


def load_credentials(project_root: Path) -> Tuple[str, str]:
    values = dotenv_values(project_root / ".env")
    username = str(os.getenv("ST_IMAGEN_ADMIN_USERNAME") or os.getenv("ADMIN_USERNAME") or values.get("ADMIN_USERNAME") or "admin")
    password = str(os.getenv("ST_IMAGEN_ADMIN_PASSWORD") or os.getenv("ADMIN_PASSWORD") or values.get("ADMIN_PASSWORD") or "")
    if not password or password.startswith("replace-with-"):
        raise RuntimeError("管理员密码未配置；请设置 ST_IMAGEN_ADMIN_PASSWORD 或 .env 中的 ADMIN_PASSWORD")
    return username, password


def read_system_sample(db_path: Path) -> Dict[str, Any]:
    result: Dict[str, Any] = {"t_utc": iso_utc()}
    try:
        load = Path("/proc/loadavg").read_text().split()
        result["load_1m"] = float(load[0])
        result["load_5m"] = float(load[1])
        result["load_15m"] = float(load[2])
    except Exception:
        pass
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, raw = line.split(":", 1)
            mem[key] = int(raw.strip().split()[0])
        result["memory_available_mb"] = round(mem.get("MemAvailable", 0) / 1024, 1)
        result["memory_used_mb"] = round((mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)) / 1024, 1)
    except Exception:
        pass
    for suffix, path in (("db", db_path), ("wal", Path(str(db_path) + "-wal")), ("shm", Path(str(db_path) + "-shm"))):
        try:
            result[f"{suffix}_bytes"] = path.stat().st_size
        except FileNotFoundError:
            result[f"{suffix}_bytes"] = 0
    return result


def build_specs(count: int, offset: int) -> List[Dict[str, Any]]:
    """混合规格，保证两个阶段累计覆盖全部 GPT size 与 Nano 组合。"""
    gpt_count = (count + 1) // 2
    nano_count = count - gpt_count
    specs: List[Dict[str, Any]] = []
    for i in range(gpt_count):
        size = GPT_SIZES[(offset + i) % len(GPT_SIZES)]
        specs.append({
            "model": "GPT Image 2",
            "size": size,
            "quality": "high",
            "aspect_ratio": "1:1",
            "resolution": "2K",
            "label": f"GPT Image 2/quality=high/size={size}",
        })
    for i in range(nano_count):
        ratio, resolution = NANO_COMBOS[(offset + i) % len(NANO_COMBOS)]
        specs.append({
            "model": "Nano Banana Pro",
            "size": "auto",
            "quality": "auto",
            "aspect_ratio": ratio,
            "resolution": resolution,
            "label": f"Nano Banana Pro/aspect_ratio={ratio}/resolution={resolution}",
        })
    # 让调度器看到混合模型到达，而非先把整个模型批次提交完。
    interleaved: List[Dict[str, Any]] = []
    gpt = [item for item in specs if item["model"] == "GPT Image 2"]
    nano = [item for item in specs if item["model"] == "Nano Banana Pro"]
    while gpt or nano:
        if gpt:
            interleaved.append(gpt.pop(0))
        if nano:
            interleaved.append(nano.pop(0))
    return interleaved


def extract_detail(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    detail = payload.get("detail")
    if isinstance(detail, dict):
        return detail
    return payload


class StageRunner:
    def __init__(self, args: argparse.Namespace, stage: int, count: int, offset: int, token: str, out_dir: Path):
        self.args = args
        self.stage = stage
        self.count = count
        self.token = token
        self.out_dir = out_dir
        self.specs = build_specs(count, offset)
        self.snapshots: List[Dict[str, Any]] = []
        self.system_samples: List[Dict[str, Any]] = []
        self.stop_sampling = asyncio.Event()
        self.admin_headers = {"Authorization": f"Bearer {token}"}
        self.db_path = Path(args.db_path).resolve()
        self.stage_start_db = db_iso_now()
        self.stage_end_db = self.stage_start_db

    async def sample_runtime(self, client: httpx.AsyncClient) -> None:
        while not self.stop_sampling.is_set():
            started = time.monotonic()
            try:
                response = await client.get("/api/admin/runtime-status", headers=self.admin_headers, timeout=5.0)
                item: Dict[str, Any] = {
                    "t_utc": iso_utc(),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    "http_status": response.status_code,
                }
                if response.status_code == 200:
                    item["runtime"] = response.json()
                else:
                    item["error"] = safe_text(response.text)
                self.snapshots.append(item)
            except Exception as exc:
                self.snapshots.append({"t_utc": iso_utc(), "error": safe_text(f"{type(exc).__name__}: {exc}")})
            self.system_samples.append(read_system_sample(self.db_path))
            try:
                await asyncio.wait_for(self.stop_sampling.wait(), timeout=self.args.sample_interval)
            except asyncio.TimeoutError:
                pass

    async def run_one(self, client: httpx.AsyncClient, index: int, spec: Dict[str, Any], barrier: asyncio.Barrier) -> Dict[str, Any]:
        request_id = f"stress-{self.stage}-{index:03d}-{uuid.uuid4().hex[:10]}"
        result: Dict[str, Any] = {
            "stage": self.stage,
            "task_index": index,
            "request_id": request_id,
            "model": spec["model"],
            "spec": spec["label"],
            "size": spec["size"],
            "quality": spec["quality"],
            "aspect_ratio": spec["aspect_ratio"],
            "resolution": spec["resolution"],
            "status": "pending",
            "events": [],
            "event_counts": {},
            "comment_count": 0,
            "response_headers": {},
            "upstream_url_leak": False,
            "non_local_image_urls": 0,
        }
        prompt = f"{PROMPTS[index % len(PROMPTS)]} [stress-{self.stage}-{index}-{uuid.uuid4().hex[:8]}]"
        payload = {
            "mode": "text2img",
            "prompt": prompt,
            "model": spec["model"],
            "aspect_ratio": spec["aspect_ratio"],
            "resolution": spec["resolution"],
            "size": spec["size"],
            "quality": spec["quality"],
            "max_failover": 2,
        }
        try:
            await barrier.wait()
            started = time.monotonic()
            result["started_at_utc"] = iso_utc()
            async with client.stream(
                "POST",
                "/api/generate/stream",
                json=payload,
                headers={"Authorization": f"Bearer {self.token}", "X-Request-ID": request_id},
                timeout=httpx.Timeout(self.args.timeout, connect=15.0),
            ) as response:
                result["http_status"] = response.status_code
                result["response_headers"] = {
                    key: response.headers[key]
                    for key in ("retry-after", "x-request-id", "content-type")
                    if key in response.headers
                }
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    parsed: Any = None
                    try:
                        parsed = json.loads(body)
                    except Exception:
                        pass
                    detail = extract_detail(parsed)
                    result.update({
                        "status": "error",
                        "error_status_code": response.status_code,
                        "error_message": safe_text(detail.get("message") if isinstance(detail, dict) else body),
                        "failure_scope": detail.get("failure_scope") if isinstance(detail, dict) else None,
                        "retry_after": detail.get("retry_after") if isinstance(detail, dict) else None,
                    })
                    result["total_ms"] = round((time.monotonic() - started) * 1000, 1)
                    return result

                first_event: Optional[float] = None
                first_upstream: Optional[float] = None
                first_image: Optional[float] = None
                async for raw_line in response.aiter_lines():
                    now_ms = round((time.monotonic() - started) * 1000, 1)
                    if raw_line.startswith(":"):
                        result["comment_count"] += 1
                        continue
                    if not raw_line.startswith("data:"):
                        continue
                    raw_data = raw_line[5:].strip()
                    if not raw_data:
                        continue
                    try:
                        event = json.loads(raw_data)
                    except Exception:
                        result["events"].append({"t_ms": now_ms, "type": "invalid_json", "line_len": len(raw_data)})
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind = str(event.get("type") or "unknown")
                    if first_event is None:
                        first_event = now_ms
                    if kind == "upstream" and first_upstream is None:
                        first_upstream = now_ms
                    summary: Dict[str, Any] = {"t_ms": now_ms, "type": kind}
                    if kind == "start":
                        summary.update({
                            "account_hash": digest(event.get("account_id") or event.get("account_name")),
                            "mode": event.get("mode"),
                            "model": event.get("model") or result["model"],
                        })
                        result.update({
                            "account_hash": summary.get("account_hash"),
                        })
                    elif kind == "upstream":
                        line = event.get("line")
                        summary["line_len"] = len(str(line or ""))
                        summary["line_present"] = bool(line)
                        if isinstance(line, str):
                            if _URL_RE.search(line):
                                result["upstream_url_leak"] = True
                            try:
                                upstream_obj = json.loads(line)
                                if isinstance(upstream_obj, dict):
                                    summary["upstream_keys"] = sorted(str(key) for key in upstream_obj.keys())[:40]
                                    summary["upstream_type"] = upstream_obj.get("type")
                                    summary["has_outputs"] = bool(upstream_obj.get("outputs"))
                                    if any("error" in str(key).lower() for key in upstream_obj.keys()):
                                        summary["has_error_key"] = True
                            except Exception:
                                summary["upstream_parseable"] = False
                    elif kind == "complete":
                        image_values = event.get("images") if isinstance(event.get("images"), list) else []
                        image_count = len(image_values)
                        local_images_only = all(
                            str(value or "").strip()
                            and urlparse(str(value)).path.startswith("/uploads/generated/")
                            and "stack-ai" not in str(value).lower()
                            for value in image_values
                        )
                        if image_count and first_image is None:
                            first_image = now_ms
                        summary.update({
                            "images_count": image_count,
                            "local_images_only": local_images_only,
                            "response_time_ms": event.get("response_time_ms"),
                            "account_hash": digest(event.get("account_id") or event.get("account_name")),
                        })
                        result.update({
                            "images_count": image_count,
                            "upstream_response_time_ms": event.get("response_time_ms"),
                            "account_hash": summary.get("account_hash") or result.get("account_hash"),
                            "image_delivery_local_only": local_images_only,
                        })
                        if not local_images_only:
                            result["non_local_image_urls"] = image_count
                    elif kind == "error":
                        summary.update({
                            "status_code": event.get("status_code"),
                            "message": safe_text(event.get("message")),
                            "failure_scope": event.get("failure_scope"),
                            "retry_after": event.get("retry_after"),
                        })
                        result.update({
                            "error_status_code": event.get("status_code"),
                            "error_message": safe_text(event.get("message")),
                            "failure_scope": event.get("failure_scope"),
                            "retry_after": event.get("retry_after"),
                        })
                    result["events"].append(summary)
                    result["event_counts"][kind] = result["event_counts"].get(kind, 0) + 1
                    if kind == "error":
                        result["status"] = "error"
                        break
                    if kind == "complete":
                        result["status"] = "success" if result.get("images_count", 0) > 0 else "error"
                        if result["status"] == "error":
                            result["error_message"] = "complete 事件未携带图片"
                        break
                result["first_event_ms"] = first_event
                result["first_upstream_ms"] = first_upstream
                result["first_image_ms"] = first_image
                if result["status"] == "pending":
                    result["status"] = "stream_ended_without_terminal_event"
                    result["error_message"] = "SSE 流结束但未收到 complete/error"
        except httpx.TimeoutException as exc:
            result["status"] = "timeout"
            result["error_type"] = type(exc).__name__
            result["error_message"] = safe_text(str(exc) or "客户端超时")
        except Exception as exc:
            result["status"] = "client_error"
            result["error_type"] = type(exc).__name__
            result["error_message"] = safe_text(str(exc))
        result["total_ms"] = round((time.monotonic() - started) * 1000, 1) if "started" in locals() else None
        return result

    async def run(self) -> Dict[str, Any]:
        stage_started_wall = iso_utc()
        stage_started_mono = time.monotonic()
        self.stage_start_db = db_iso_now()
        barrier = asyncio.Barrier(self.count + 1)
        limits = httpx.Limits(max_connections=self.count + 8, max_keepalive_connections=self.count + 4)
        async with httpx.AsyncClient(base_url=self.args.base_url, limits=limits, trust_env=False) as client:
            sampler = asyncio.create_task(self.sample_runtime(client))
            tasks = [asyncio.create_task(self.run_one(client, i, spec, barrier)) for i, spec in enumerate(self.specs)]
            await barrier.wait()
            print(f"[stage {self.stage}] {self.count} 路已同时放行，规格 GPT={sum(s['model'] == 'GPT Image 2' for s in self.specs)} Nano={sum(s['model'] == 'Nano Banana Pro' for s in self.specs)}", flush=True)
            results = await asyncio.gather(*tasks)
            self.stop_sampling.set()
            await sampler
        self.stage_end_db = db_iso_now()
        stage_finished_wall = iso_utc()
        report = build_stage_report(self.stage, self.count, stage_started_wall, stage_finished_wall, stage_started_mono, results, self.snapshots, self.system_samples)
        report["database_generation_logs"] = query_generation_logs(self.db_path, self.stage_start_db, self.stage_end_db)
        stamp = utc_stamp()
        (self.out_dir / f"stage_{self.stage}_{self.count}_tasks.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        (self.out_dir / f"stage_{self.stage}_{self.count}_runtime.json").write_text(json.dumps({"snapshots": self.snapshots, "system_samples": self.system_samples}, ensure_ascii=False, indent=1), encoding="utf-8")
        (self.out_dir / f"stage_{self.stage}_{self.count}_summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[stage {self.stage}] 完成 success={report['success']} error={report['error']} timeout={report['timeout']} p95_success_ms={report['latency_success'].get('p95_ms')}", flush=True)
        return report


def query_generation_logs(db_path: Path, started: str, ended: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"window_start_db_utc": started, "window_end_db_utc": ended, "count": 0, "by_status": {}, "by_model_status": {}, "errors": []}
    # GenerationLog stores naive ``datetime.utcnow()`` values in SQLite, whose
    # textual form uses a space instead of the ISO ``T`` separator.
    started_db = started.replace("T", " ", 1).split("+", 1)[0]
    ended_db = ended.replace("T", " ", 1).split("+", 1)[0]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        rows = conn.execute(
            "SELECT timestamp, status, model, response_time_ms, error_message FROM generation_logs WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
            (started_db, ended_db),
        ).fetchall()
        conn.close()
    except Exception as exc:
        result["query_error"] = safe_text(f"{type(exc).__name__}: {exc}")
        return result
    result["count"] = len(rows)
    status_counter = Counter()
    model_counter = Counter()
    for timestamp, status, model, response_ms, error_message in rows:
        status_counter[str(status)] += 1
        model_counter[f"{model or '?'}|{status}"] += 1
        if str(status) != "success":
            result["errors"].append({"timestamp": timestamp, "model": model, "response_time_ms": response_ms, "message": safe_text(error_message)})
    result["by_status"] = dict(status_counter)
    result["by_model_status"] = dict(model_counter)
    result["error_count"] = len(result["errors"])
    return result


def build_stage_report(stage: int, count: int, started: str, ended: str, started_mono: float, results: List[Dict[str, Any]], snapshots: List[Dict[str, Any]], system_samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    statuses = Counter(str(r.get("status")) for r in results)
    event_counts = Counter()
    by_model: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_spec: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    error_codes = Counter()
    failure_scopes = Counter()
    account_usage = Counter()
    for item in results:
        model = str(item.get("model"))
        spec = str(item.get("spec"))
        by_model[model][str(item.get("status"))] += 1
        by_spec[spec][str(item.get("status"))] += 1
        account = item.get("account_hash")
        if account:
            account_usage[account] += 1
        for kind, value in (item.get("event_counts") or {}).items():
            event_counts[kind] += int(value or 0)
        if item.get("error_status_code") is not None:
            error_codes[str(item["error_status_code"])] += 1
        if item.get("failure_scope"):
            failure_scopes[str(item["failure_scope"])] += 1

    def lat(name: str, selected: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        return stats(item.get(name) for item in selected)

    successes = [r for r in results if r.get("status") == "success"]
    failures = [r for r in results if r.get("status") != "success"]
    max_gate = 0
    max_model_inflight: Dict[str, int] = defaultdict(int)
    for sample in snapshots:
        runtime = sample.get("runtime") or {}
        gate = ((runtime.get("guard") or {}).get("generation_admission") or {})
        max_gate = max(max_gate, int(gate.get("in_flight") or 0))
        models = ((runtime.get("guard") or {}).get("generation_admission") or {}).get("models") or {}
        for model, values in models.items():
            max_model_inflight[model] = max(max_model_inflight[model], int(values.get("in_flight") or 0))

    report = {
        "stage": stage,
        "requested_concurrency": count,
        "stage_started_utc": started,
        "stage_finished_utc": ended,
        "wall_time_seconds": round(time.monotonic() - started_mono, 2),
        "total": len(results),
        "success": statuses.get("success", 0),
        "error": len(results) - statuses.get("success", 0) - statuses.get("timeout", 0),
        "timeout": statuses.get("timeout", 0),
        "status_counts": dict(statuses),
        "success_rate_pct": round(statuses.get("success", 0) * 100 / len(results), 1) if results else 0,
        "latency_all": lat("total_ms", results),
        "latency_success": lat("total_ms", successes),
        "latency_failure": lat("total_ms", failures),
        "first_event": lat("first_event_ms", results),
        "first_upstream": lat("first_upstream_ms", results),
        "first_image": lat("first_image_ms", successes),
        "upstream_response_time": lat("upstream_response_time_ms", successes),
        "event_counts": dict(event_counts),
        "by_model": {key: dict(value) for key, value in by_model.items()},
        "by_spec": {key: dict(value) for key, value in by_spec.items()},
        "error_status_codes": dict(error_codes),
        "failure_scopes": dict(failure_scopes),
        "account_usage_hashed": dict(account_usage),
        "security_checks": {
            "upstream_url_leaks": sum(1 for item in results if item.get("upstream_url_leak")),
            "responses_with_non_local_images": sum(1 for item in results if item.get("non_local_image_urls")),
        },
        "admission_observed_max": {
            "shared_generation_in_flight": max_gate,
            "per_model_in_flight": dict(max_model_inflight),
        },
        "runtime_snapshot_count": len(snapshots),
        "system_sample_count": len(system_samples),
        "system": {
            "load_1m_max": max((x.get("load_1m", 0) for x in system_samples), default=None),
            "memory_available_mb_min": min((x.get("memory_available_mb") for x in system_samples if x.get("memory_available_mb") is not None), default=None),
            "wal_bytes_max": max((x.get("wal_bytes", 0) for x in system_samples), default=0),
        },
    }
    return report


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--output-dir", default="/tmp/sti-loadtest/reports")
    parser.add_argument("--db-path", default="data/image_gen.db")
    parser.add_argument("--timeout", type=float, default=360.0)
    parser.add_argument("--sample-interval", type=float, default=0.5)
    parser.add_argument("--between-stages", type=float, default=5.0)
    parser.add_argument("--stages", default="48,53")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    username, password = load_credentials(project_root)
    out_dir = Path(args.output_dir).resolve() / f"mixed_{utc_stamp()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    stage_sizes = [max(1, int(value.strip())) for value in args.stages.split(",") if value.strip()]
    if not stage_sizes or any(value < 1 for value in stage_sizes):
        raise RuntimeError(f"压测阶段必须是正整数，当前为 {stage_sizes}")

    async with httpx.AsyncClient(base_url=args.base_url, timeout=20.0, trust_env=False) as client:
        health = await client.get("/health")
        if health.status_code != 200:
            raise RuntimeError(f"服务健康检查失败: HTTP {health.status_code}")
        login = await client.post("/api/admin/login", json={"username": username, "password": password})
        if login.status_code >= 400:
            raise RuntimeError(f"管理员登录失败: HTTP {login.status_code} {safe_text(login.text)}")
        login_payload = login.json()
        token = str(login_payload.get("token") or "")
        if not token:
            raise RuntimeError("管理员登录响应缺少 token")
    print(f"[boot] {args.base_url} health=ok，管理员鉴权成功；报告目录 {out_dir}", flush=True)

    reports = []
    offset = 0
    for index, count in enumerate(stage_sizes, start=1):
        runner = StageRunner(args, index, count, offset, token, out_dir)
        reports.append(await runner.run())
        offset += count
        if index < len(stage_sizes) and args.between_stages > 0:
            print(f"[pause] 阶段 {index} 完成，等待 {args.between_stages:.1f}s 后进入下一阶段", flush=True)
            await asyncio.sleep(args.between_stages)

    combined = {
        "test": "mixed_generation_" + "_".join(str(value) for value in stage_sizes),
        "base_url": args.base_url,
        "started_utc": reports[0]["stage_started_utc"],
        "finished_utc": reports[-1]["stage_finished_utc"],
        "configuration": {
            "stages": stage_sizes,
            "gpt_quality": "high",
            "gpt_sizes": GPT_SIZES,
            "nano_resolutions": ["1K", "2K", "4K"],
            "nano_aspect_ratios": sorted({ratio for ratio, _ in NANO_COMBOS}),
            "max_failover_sent": 2,
        },
        "stages": reports,
        "comparison": (
            {
                "success_rate_delta_stage2_minus_stage1_pct": round(reports[1]["success_rate_pct"] - reports[0]["success_rate_pct"], 1),
                "p95_success_delta_ms": (reports[1]["latency_success"].get("p95_ms") or 0) - (reports[0]["latency_success"].get("p95_ms") or 0),
                "shared_slot_peak_stage1": reports[0]["admission_observed_max"]["shared_generation_in_flight"],
                "shared_slot_peak_stage2": reports[1]["admission_observed_max"]["shared_generation_in_flight"],
            }
            if len(reports) == 2
            else {
                "stage": reports[0]["stage"],
                "success_rate_pct": reports[0]["success_rate_pct"],
                "shared_slot_peak": reports[0]["admission_observed_max"]["shared_generation_in_flight"],
            }
        ),
        "report_files": sorted(path.name for path in out_dir.iterdir()),
    }
    combined_path = out_dir / "combined_summary.json"
    combined_path.write_text(json.dumps(combined, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(combined["comparison"], ensure_ascii=False), flush=True)
    print(f"[done] 详细报告: {combined_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
