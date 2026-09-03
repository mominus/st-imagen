#!/usr/bin/env python3
"""60 并发混合负载压测：GPT Image 2（各尺寸, quality=high）+ Nano Banana Pro（各画幅×清晰度）。

- 30 个访客用户（邀请码直登，max_inflight=2），每人 2 个并发请求 = 60 路
- SSE 解析 start/complete/error，记录账号分配、延迟、错误细节
- 采样 docker stats；输出逐请求 JSON + 汇总
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

GPT_SIZES = ["auto", "1024x1024", "1536x1024", "1024x1536", "2048x2048", "3840x2160"]
NANO_COMBOS = [
    (ratio, res)
    for ratio in ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]
    for res in ["1K", "2K", "4K"]
]

PROMPTS = [
    "a small red apple on a white marble table, soft natural light",
    "a tiny robot watering a potted plant in a sunlit kitchen",
    "an astronaut riding a horse on mars, cinematic",
    "a cozy bookstore at dusk with warm lamps, photorealistic",
    "a steaming bowl of ramen on a wooden table, top-down shot",
    "a paper crane beside a vintage typewriter, soft focus",
    "a hummingbird near a hibiscus flower, hyper detailed",
    "a vintage red bicycle against a brick wall in autumn",
    "a glass jar of honey beside blueberries, studio lighting",
    "a wooden treehouse in a misty forest at dawn",
]


@dataclass
class TaskResult:
    task_id: str
    user_idx: int
    model: str
    spec: str
    status: str = "pending"  # success / error / timeout
    error_message: Optional[str] = None
    error_status_code: Optional[int] = None
    retry_after: Optional[float] = None
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    ttfb_ms: Optional[float] = None
    total_ms: Optional[float] = None
    images: int = 0
    upstream_events: int = 0


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    sv = sorted(values)
    return float(sv[int((len(sv) - 1) * p)])


def stats_of(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": round(min(values) / 1000, 1),
        "p50": round(percentile(values, 0.5) / 1000, 1),
        "p95": round(percentile(values, 0.95) / 1000, 1),
        "max": round(max(values) / 1000, 1),
    }


def sample_docker_stats(container: str) -> Optional[Dict[str, str]]:
    try:
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}|{{.MemUsage}}", container],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if "|" not in out:
            return None
        cpu, mem = out.split("|", 1)
        return {"cpu": cpu.strip(), "mem": mem.strip()}
    except Exception:
        return None


async def run_task(
    user_idx: int,
    task_idx: int,
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    spec: Dict[str, str],
    spec_label: str,
    start_gate: asyncio.Event,
    timeout_sec: float,
) -> TaskResult:
    payload = {
        "mode": "text2img",
        "prompt": f"{PROMPTS[task_idx % len(PROMPTS)]} (load-{uuid.uuid4().hex[:6]})",
        "model": model,
        "max_failover": 2,
        **spec,
    }
    res = TaskResult(task_id=f"t{task_idx:02d}", user_idx=user_idx, model=model, spec=spec_label)
    await start_gate.wait()
    t0 = time.monotonic()
    saw_event = False
    try:
        async with client.stream(
            "POST", f"{base_url}/api/generate/stream", json=payload,
            timeout=httpx.Timeout(timeout_sec, connect=15.0),
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "ignore")[:400]
                res.status = "error"
                res.error_status_code = resp.status_code
                res.error_message = body
                try:
                    det = json.loads(body).get("detail")
                    if isinstance(det, dict):
                        res.error_message = det.get("message", body)
                        res.retry_after = det.get("retry_after")
                except Exception:
                    pass
                res.total_ms = (time.monotonic() - t0) * 1000
                return res

            async for raw_line in resp.aiter_lines():
                if not raw_line or not raw_line.startswith("data:"):
                    continue
                try:
                    evt = json.loads(raw_line[5:].strip())
                except Exception:
                    continue
                kind = evt.get("type")
                if not saw_event and kind in ("start", "upstream", "error"):
                    saw_event = True
                    res.ttfb_ms = (time.monotonic() - t0) * 1000
                if kind == "start":
                    res.account_id = evt.get("account_id")
                    res.account_name = evt.get("account_name")
                elif kind == "upstream":
                    res.upstream_events += 1
                elif kind == "complete":
                    res.status = "success"
                    res.images = len(evt.get("images") or [])
                elif kind == "error":
                    res.status = "error"
                    res.error_message = evt.get("message")
                    res.error_status_code = evt.get("status_code")
                    res.retry_after = evt.get("retry_after")
            if res.status == "pending":
                res.status = "timeout"
                res.error_message = "SSE 流结束但未收到终态事件"
            res.total_ms = (time.monotonic() - t0) * 1000
            return res
    except Exception as exc:
        res.status = "timeout" if isinstance(exc, (httpx.TimeoutException, httpx.ReadTimeout)) else "error"
        res.error_message = f"{type(exc).__name__}: {exc}"
        res.total_ms = (time.monotonic() - t0) * 1000
        return res


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18002")
    parser.add_argument("--codes-file", default="/tmp/sti-loadtest/codes.txt")
    parser.add_argument("--output-dir", default="/tmp/sti-loadtest/reports")
    parser.add_argument("--per-task-timeout", type=float, default=330.0)
    parser.add_argument("--container", default="sti-loadtest")
    args = parser.parse_args()

    codes = [c.strip() for c in Path(args.codes_file).read_text().splitlines() if c.strip()]
    assert len(codes) >= 30, "需要 30 个邀请码"

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1: 30 个访客登录（各自独立 cookie 会话）
    clients: List[httpx.AsyncClient] = []
    async with httpx.AsyncClient(base_url=args.base_url, timeout=15.0, trust_env=False) as boot:
        for i, code in enumerate(codes[:30]):
            resp = await boot.post("/api/auth/invite-login", json={"invite_code": code})
            if resp.status_code != 200:
                print(f"[boot] 用户 {i} 登录失败: {resp.status_code} {resp.text[:120]}")
                return 1
            token = resp.cookies.get("imagen_session")
            client = httpx.AsyncClient(base_url=args.base_url, timeout=None, trust_env=False)
            client.cookies.set("imagen_session", token)
            clients.append(client)
    print("[boot] 30 个访客会话就绪")

    # Phase 2: 任务矩阵 = 30 GPT (quality=high) + 30 Nano (10 画幅 × 3 清晰度)
    specs: List[tuple] = []
    for i in range(30):
        specs.append(("GPT Image 2", {"size": GPT_SIZES[i % len(GPT_SIZES)], "quality": "high"}, f"size={GPT_SIZES[i % len(GPT_SIZES)]}/high"))
    for ratio, resn in NANO_COMBOS:
        specs.append(("Nano Banana Pro", {"aspect_ratio": ratio, "resolution": resn}, f"{ratio}/{resn}"))
    assert len(specs) == 60

    gate = asyncio.Event()
    stats_samples: List[Dict[str, Any]] = []
    stop_sampling = asyncio.Event()

    async def sampler_loop():
        t0 = time.monotonic()
        while not stop_sampling.is_set():
            s = sample_docker_stats(args.container)
            if s:
                s["t"] = round(time.monotonic() - t0, 1)
                stats_samples.append(s)
            await asyncio.sleep(2.0)

    sampler_task = asyncio.create_task(sampler_loop())
    # 每个用户的第 2 个请求延迟 1s 再放行，避免同一用户两请求完全同拍（贴近真实）
    tasks = []
    for idx, (model, spec, label) in enumerate(specs):
        user_idx = idx % 30
        delay = 0.0 if idx < 30 else 1.0
        async def runner(idx=idx, user_idx=user_idx, model=model, spec=spec, label=label, delay=delay):
            await asyncio.sleep(delay)
            return await run_task(user_idx, idx, clients[user_idx], args.base_url, model, spec, label, gate, args.per_task_timeout)
        tasks.append(asyncio.create_task(runner()))

    print("[run] 60 路并发请求开始（GPT×30 + Nano×30，200ms 内全部放行）")
    run_started_wall = datetime.now(timezone.utc).isoformat()
    gate.set()
    results = await asyncio.gather(*tasks)
    stop_sampling.set()
    await sampler_task
    run_finished_wall = datetime.now(timezone.utc).isoformat()
    for c in clients:
        await c.aclose()

    # ---- 汇总 ----
    ok = [r for r in results if r.status == "success"]
    err = [r for r in results if r.status == "error"]
    tmo = [r for r in results if r.status == "timeout"]

    def group_by(fn, items):
        out: Dict[str, int] = {}
        for it in items:
            key = fn(it)
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    by_model = group_by(lambda r: r.model, results)
    ok_by_model = group_by(lambda r: f"{r.model} {r.spec}", ok)
    err_by_model = group_by(lambda r: f"{r.model} {r.spec}", err)

    # 错误分类
    def classify(r: TaskResult) -> str:
        msg = r.error_message or ""
        sc = r.error_status_code
        if "所有账号均达" in msg or "in_flight" in msg:
            return "账号池容量满(429 NoCapacity)"
        if "当前账号并发已达上限" in msg:
            return "用户并发上限(429)"
        if "今日生成额度" in msg:
            return "用户日配额(429)"
        if "上游服务暂时不可用" in msg:
            return "熔断器打开(503)"
        if "服务繁忙" in msg:
            return "全局并发闸门(429)"
        if "请求过于频繁" in msg:
            return "用户RPM限速(429)"
        if sc and 500 <= sc < 600:
            return f"上游/服务端错误({sc})"
        if sc == 401:
            return "认证失败(401)"
        if r.status == "timeout":
            return "客户端超时"
        return f"其他({sc or '?'})"

    err_classes = group_by(classify, err + tmo)

    # 账号分布
    acc_usage = group_by(lambda r: r.account_name or "(未分配)", [r for r in results if r.account_id])
    acc_success = group_by(lambda r: r.account_name or "(未分配)", ok)

    cpu_vals = [float(s["cpu"].rstrip("%")) for s in stats_samples if s.get("cpu")]

    summary = {
        "run_started_utc": run_started_wall,
        "run_finished_utc": run_finished_wall,
        "total": len(results),
        "success": len(ok),
        "error": len(err),
        "timeout": len(tmo),
        "success_rate_pct": round(len(ok) / len(results) * 100, 1),
        "latency_success": stats_of([r.total_ms for r in ok]) if ok else {},
        "ttfb_success": stats_of([r.ttfb_ms for r in ok if r.ttfb_ms]) if ok else {},
        "error_latency": stats_of([r.total_ms for r in err + tmo]) if (err or tmo) else {},
        "by_model": by_model,
        "success_by_spec": ok_by_model,
        "error_by_spec": err_by_model,
        "error_classes": err_classes,
        "account_usage": acc_usage,
        "account_success": acc_success,
        "resources": {
            "samples": len(stats_samples),
            "cpu_max_pct": round(max(cpu_vals), 1) if cpu_vals else None,
            "cpu_mean_pct": round(sum(cpu_vals) / len(cpu_vals), 1) if cpu_vals else None,
            "mem_max": max((s.get("mem", "?") for s in stats_samples), key=lambda x: x, default=None),
        },
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out_dir / f"mixed60_tasks_{stamp}.json").write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=1)
    )
    (out_dir / f"mixed60_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1)
    )

    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"\n报告已写入 {out_dir}/mixed60_*_{stamp}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
