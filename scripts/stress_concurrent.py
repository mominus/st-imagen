#!/usr/bin/env python3
"""并发压测：直接打 st-imagen 的 SSE 生图接口。

用法示例：
    python scripts/stress_concurrent.py --concurrency 10
    python scripts/stress_concurrent.py --concurrency 50 --mode text2img --output-dir data/stress_reports

每个并发任务：
  POST /api/generate/stream  → 解析 SSE 流，识别 start / complete / error 事件
统计：成功率、首事件延迟、首图延迟、总耗时（p50 / p95 / max）、账号轮转分布、错误分类
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


# ---------- 提示词池（每次请求随机选一条 + 加 uuid 抑制 stackai 缓存）----------
TEXT2IMG_PROMPTS = [
    "a small red apple on a white marble table, soft natural light",
    "a tiny robot watering a potted plant in a sunlit kitchen",
    "an astronaut riding a horse on the surface of mars, cinematic",
    "a cozy bookstore at dusk with warm lamps, photorealistic",
    "a steaming bowl of ramen on a wooden table, top-down shot",
    "a paper crane sitting beside a vintage typewriter, soft focus",
    "a hummingbird hovering near a hibiscus flower, hyper detailed",
    "a vintage red bicycle leaning against a brick wall in autumn",
    "a glass jar of honey beside fresh blueberries, studio lighting",
    "a wooden treehouse hidden in a misty forest at dawn",
]


def utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    sv = sorted(values)
    idx = int((len(sv) - 1) * p)
    return float(sv[idx])


def stats_dict(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {"count": 0, "min": 0.0, "max": 0.0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}
    return {
        "count": len(values),
        "min": round(min(values), 1),
        "max": round(max(values), 1),
        "mean": round(statistics.fmean(values), 1),
        "p50": round(percentile(values, 0.50), 1),
        "p95": round(percentile(values, 0.95), 1),
        "p99": round(percentile(values, 0.99), 1),
    }


@dataclass
class TaskResult:
    task_id: str
    prompt: str
    status: str = "pending"  # success / error / timeout
    error_message: Optional[str] = None
    error_status_code: Optional[int] = None
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    started_at: float = 0.0
    first_event_ms: Optional[float] = None
    first_image_ms: Optional[float] = None
    total_ms: Optional[float] = None
    upstream_response_time_ms: Optional[int] = None  # 由上游返回（complete 事件携带）
    images_count: int = 0
    sse_event_count: int = 0


def make_payload(
    mode: str,
    prompt: str,
    *,
    model: str,
    aspect_ratio: str,
    resolution: str,
    size: str,
    quality: str,
) -> Dict[str, Any]:
    if mode == "text2img":
        return {
            "mode": "text2img",
            "prompt": prompt,
            "model": model,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "size": size,
            "quality": quality,
        }
    raise ValueError(f"unsupported mode: {mode}")


async def run_one(
    client: httpx.AsyncClient,
    base_url: str,
    mode: str,
    timeout_sec: float,
    task_idx: int,
    authorization: Optional[str],
    model: str,
    aspect_ratio: str,
    resolution: str,
    size: str,
    quality: str,
    stagger_ms: float,
) -> TaskResult:
    if stagger_ms > 0:
        await asyncio.sleep(random.uniform(0.0, stagger_ms / 1000.0))
    prompt_base = random.choice(TEXT2IMG_PROMPTS)
    prompt = f"{prompt_base} (test-{uuid.uuid4().hex[:8]})"
    res = TaskResult(task_id=f"t{task_idx:03d}", prompt=prompt)
    payload = make_payload(
        mode,
        prompt,
        model=model,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        size=size,
        quality=quality,
    )
    headers = {}
    if authorization:
        headers["Authorization"] = authorization

    res.started_at = time.monotonic()
    deadline = res.started_at + timeout_sec
    url = f"{base_url}/api/generate/stream"
    saw_start = False

    try:
        async with client.stream(
            "POST",
            url,
            json=payload,
            headers=headers,
            timeout=httpx.Timeout(timeout_sec, connect=10.0),
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "ignore")[:500]
                res.status = "error"
                res.error_message = f"http {resp.status_code}: {body}"
                res.error_status_code = resp.status_code
                res.total_ms = (time.monotonic() - res.started_at) * 1000
                return res

            async for raw_line in resp.aiter_lines():
                if time.monotonic() > deadline:
                    res.status = "timeout"
                    res.error_message = f"client timeout after {timeout_sec}s"
                    break
                if not raw_line or not raw_line.startswith("data:"):
                    continue
                data_str = raw_line[5:].strip()
                if not data_str:
                    continue
                try:
                    evt = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                res.sse_event_count += 1
                etype = evt.get("type")
                now_ms = (time.monotonic() - res.started_at) * 1000
                if not saw_start:
                    res.first_event_ms = round(now_ms, 1)
                    saw_start = True
                if etype == "start":
                    res.account_id = evt.get("account_id")
                    res.account_name = evt.get("account_name") or evt.get("account_short")
                elif etype == "complete":
                    images = evt.get("images") or []
                    if isinstance(images, list):
                        res.images_count = len(images)
                    if res.images_count > 0 and res.first_image_ms is None:
                        res.first_image_ms = round(now_ms, 1)
                    rtime = evt.get("response_time_ms")
                    if isinstance(rtime, (int, float)):
                        res.upstream_response_time_ms = int(rtime)
                    res.status = "success" if res.images_count > 0 else "error"
                    if res.status == "error" and not res.error_message:
                        res.error_message = "complete with no images"
                    break
                elif etype == "error":
                    res.status = "error"
                    res.error_message = str(evt.get("message") or "unknown error")
                    sc = evt.get("status_code")
                    if isinstance(sc, int):
                        res.error_status_code = sc
                    break
    except httpx.TimeoutException as exc:
        res.status = "timeout"
        res.error_message = f"httpx timeout: {type(exc).__name__}: {exc}"
    except Exception as exc:
        res.status = "error"
        res.error_message = f"{type(exc).__name__}: {exc}"

    res.total_ms = round((time.monotonic() - res.started_at) * 1000, 1)
    if res.status == "pending":
        res.status = "error"
        res.error_message = res.error_message or "stream ended without complete/error event"
    return res


async def run_round(
    base_url: str,
    concurrency: int,
    mode: str,
    timeout_sec: float,
    authorization: Optional[str],
    model: str,
    aspect_ratio: str,
    resolution: str,
    size: str,
    quality: str,
    stagger_ms: float,
) -> Dict[str, Any]:
    started = time.monotonic()
    started_iso = datetime.now(timezone.utc).isoformat()

    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(limits=limits, trust_env=False) as client:
        tasks = [
            asyncio.create_task(
                run_one(
                    client,
                    base_url,
                    mode,
                    timeout_sec,
                    i,
                    authorization,
                    model,
                    aspect_ratio,
                    resolution,
                    size,
                    quality,
                    stagger_ms,
                )
            )
            for i in range(concurrency)
        ]
        results: List[TaskResult] = await asyncio.gather(*tasks)

    total_wall_ms = round((time.monotonic() - started) * 1000, 1)

    # 聚合
    successes = [r for r in results if r.status == "success"]
    errors = [r for r in results if r.status != "success"]
    error_buckets: Counter = Counter()
    for r in errors:
        bucket = r.status if r.status != "error" else "error"
        if r.error_status_code:
            bucket = f"http_{r.error_status_code}"
        elif r.error_message:
            short = r.error_message.split(":", 1)[0][:48]
            bucket = f"{r.status}:{short}"
        error_buckets[bucket] += 1

    account_dist = Counter()
    for r in results:
        if r.account_name:
            account_dist[r.account_name] += 1

    summary = {
        "started_at": started_iso,
        "concurrency": concurrency,
        "mode": mode,
        "model": model,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "size": size,
        "quality": quality,
        "stagger_ms": round(stagger_ms, 1),
        "total_wall_ms": total_wall_ms,
        "throughput_per_sec": round(len(successes) / max(total_wall_ms / 1000, 0.001), 2),
        "success": len(successes),
        "error": len(errors),
        "success_rate": round(len(successes) / len(results), 3) if results else 0.0,
        "first_event_ms": stats_dict([r.first_event_ms for r in results if r.first_event_ms is not None]),
        "first_image_ms": stats_dict([r.first_image_ms for r in successes if r.first_image_ms is not None]),
        "total_ms": stats_dict([r.total_ms for r in results if r.total_ms is not None]),
        "upstream_response_time_ms": stats_dict(
            [float(r.upstream_response_time_ms) for r in successes if r.upstream_response_time_ms is not None]
        ),
        "account_distribution": dict(account_dist.most_common()),
        "error_buckets": dict(error_buckets.most_common()),
    }
    return {"summary": summary, "tasks": [asdict(r) for r in results]}


def print_round_summary(stage: str, summary: Dict[str, Any]) -> None:
    s = summary
    bar = "─" * 56
    print(f"\n{bar}")
    print(f" {stage}  concurrency={s['concurrency']}  mode={s['mode']}")
    print(bar)
    print(
        f"  model={s.get('model')}  aspect={s.get('aspect_ratio')}  "
        f"resolution={s.get('resolution')}  size={s.get('size')}  quality={s.get('quality')}"
    )
    print(f"  wall={s['total_wall_ms']/1000:.1f}s  success={s['success']}/{s['concurrency']}"
          f"  rate={s['success_rate']*100:.1f}%  throughput={s['throughput_per_sec']}/s")
    t = s["total_ms"]
    print(f"  total_ms     p50={t['p50']}  p95={t['p95']}  max={t['max']}")
    fe = s["first_event_ms"]
    print(f"  first_event  p50={fe['p50']}  p95={fe['p95']}  max={fe['max']}")
    if s["upstream_response_time_ms"]["count"]:
        u = s["upstream_response_time_ms"]
        print(f"  upstream_ms  p50={u['p50']}  p95={u['p95']}  max={u['max']}")
    if s["account_distribution"]:
        dist = "  ".join(f"{k}={v}" for k, v in s["account_distribution"].items())
        print(f"  accounts     {dist}")
    if s["error_buckets"]:
        print(f"  errors       {dict(s['error_buckets'])}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--mode", choices=["text2img"], default="text2img")
    parser.add_argument("--model", default="Nano Banana Pro")
    parser.add_argument("--aspect-ratio", default="1:1")
    parser.add_argument("--resolution", default="1K")
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--quality", default="auto")
    parser.add_argument(
        "--auth-token",
        default=os.getenv("ST_IMAGEN_AUTH_TOKEN", ""),
        help="JWT token; sent as Authorization: Bearer <token>",
    )
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="单请求最大等待秒数")
    parser.add_argument("--output-dir", default="data/stress_reports")
    parser.add_argument("--label", default="", help="报告文件名后缀，便于阶梯标记")
    parser.add_argument("--stagger-ms", type=float, default=0.0, help="请求发起错峰窗口，单位毫秒")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    label = (args.label + "_") if args.label else ""
    stamp = utc_now_compact()
    report_path = out_dir / f"stress_{label}c{args.concurrency:03d}_{stamp}.json"
    authorization = f"Bearer {args.auth_token}" if args.auth_token else None

    print(
        f"[stress] base_url={args.base_url}  concurrency={args.concurrency}  mode={args.mode}  "
        f"model={args.model}  aspect={args.aspect_ratio}  resolution={args.resolution}  "
        f"size={args.size}  quality={args.quality}  "
        f"auth={'yes' if authorization else 'no'}  timeout={args.timeout}s  stagger={args.stagger_ms}ms"
    )
    print(f"[stress] report → {report_path}")

    try:
        result = asyncio.run(
            run_round(
                args.base_url,
                args.concurrency,
                args.mode,
                args.timeout,
                authorization,
                args.model,
                args.aspect_ratio,
                args.resolution,
                args.size,
                args.quality,
                args.stagger_ms,
            )
        )
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print_round_summary(f"c={args.concurrency}", result["summary"])
    print(f"\nreport saved: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
