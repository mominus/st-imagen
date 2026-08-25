#!/usr/bin/env python3
"""2c2g VPS-like stress orchestrator for st-imagen.

Runs the app inside a Docker container limited to 2 CPUs / 2 GiB RAM,
then drives the existing stress scripts from the host while sampling
`docker stats` into JSONL reports.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = PROJECT_ROOT / "data" / "stress_reports"
COMPOSE_FILE = PROJECT_ROOT / "compose.vps-stress.yml"
CONTAINER_NAME = "st-imagen-vps-2c2g"
DEFAULT_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_STATS_INTERVAL = 1.0


def utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def python_bin() -> str:
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def run_command(
    cmd: List[str],
    *,
    cwd: Path = PROJECT_ROOT,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        check=check,
        capture_output=capture_output,
    )


def compose_cmd(*args: str) -> List[str]:
    return ["docker", "compose", "-f", str(COMPOSE_FILE), *args]


def require_file(path: Path, message: str) -> None:
    if not path.exists():
        raise SystemExit(message)


def ensure_report_dir() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def parse_percentage(value: str) -> Optional[float]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("%"):
        raw = raw[:-1]
    try:
        return float(raw)
    except ValueError:
        return None


def parse_size_to_bytes(value: str) -> Optional[int]:
    raw = str(value or "").strip()
    if not raw:
        return None

    units = {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "tb": 1000**4,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
    }
    split_at = 0
    while split_at < len(raw) and (raw[split_at].isdigit() or raw[split_at] in ".-"):
        split_at += 1
    number = raw[:split_at].strip()
    unit = raw[split_at:].strip().lower()
    if not number:
        return None
    if not unit:
        unit = "b"
    multiplier = units.get(unit)
    if multiplier is None:
        return None
    try:
        return int(float(number) * multiplier)
    except ValueError:
        return None


def parse_io_pair(value: str) -> Dict[str, Optional[int]]:
    raw = str(value or "")
    left, _, right = raw.partition("/")
    return {
        "input_bytes": parse_size_to_bytes(left),
        "output_bytes": parse_size_to_bytes(right),
    }


def parse_mem_usage(value: str) -> Dict[str, Optional[int]]:
    raw = str(value or "")
    used, _, limit = raw.partition("/")
    return {
        "used_bytes": parse_size_to_bytes(used),
        "limit_bytes": parse_size_to_bytes(limit),
    }


def sample_stats_once() -> Dict[str, Any]:
    result = run_command(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            CONTAINER_NAME,
        ],
        check=True,
        capture_output=True,
    )
    line = result.stdout.strip()
    if not line:
        raise RuntimeError("docker stats returned no data")

    raw = json.loads(line)
    mem = parse_mem_usage(raw.get("MemUsage", ""))
    net = parse_io_pair(raw.get("NetIO", ""))
    block = parse_io_pair(raw.get("BlockIO", ""))
    return {
        "timestamp_utc": utc_now_iso(),
        "container": raw.get("Name") or CONTAINER_NAME,
        "cpu_percent": parse_percentage(raw.get("CPUPerc", "")),
        "mem_percent": parse_percentage(raw.get("MemPerc", "")),
        "mem_usage_bytes": mem["used_bytes"],
        "mem_limit_bytes": mem["limit_bytes"],
        "net_input_bytes": net["input_bytes"],
        "net_output_bytes": net["output_bytes"],
        "block_input_bytes": block["input_bytes"],
        "block_output_bytes": block["output_bytes"],
        "pids": int(raw["PIDs"]) if str(raw.get("PIDs", "")).isdigit() else None,
        "raw": raw,
    }


def collect_stats(
    output_path: Path,
    stop_event: threading.Event,
    interval_seconds: float,
    errors: List[str],
) -> None:
    with output_path.open("w", encoding="utf-8") as fh:
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                sample = sample_stats_once()
            except Exception as exc:  # pragma: no cover - operational path
                errors.append(f"{type(exc).__name__}: {exc}")
                sample = {
                    "timestamp_utc": utc_now_iso(),
                    "container": CONTAINER_NAME,
                    "sampling_error": f"{type(exc).__name__}: {exc}",
                }
            fh.write(json.dumps(sample, ensure_ascii=False) + "\n")
            fh.flush()

            elapsed = time.monotonic() - started
            wait_seconds = max(0.0, interval_seconds - elapsed)
            if stop_event.wait(wait_seconds):
                break


def summarize_stats(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    cpu = [float(v["cpu_percent"]) for v in samples if v.get("cpu_percent") is not None]
    mem = [int(v["mem_usage_bytes"]) for v in samples if v.get("mem_usage_bytes") is not None]
    mem_pct = [float(v["mem_percent"]) for v in samples if v.get("mem_percent") is not None]
    pids = [int(v["pids"]) for v in samples if v.get("pids") is not None]

    def metric(values: List[float]) -> Dict[str, Any]:
        if not values:
            return {"count": 0, "mean": 0.0, "max": 0.0}
        return {
            "count": len(values),
            "mean": round(statistics.fmean(values), 2),
            "max": round(max(values), 2),
        }

    return {
        "samples": len(samples),
        "cpu_percent": metric(cpu),
        "mem_usage_bytes": metric([float(v) for v in mem]),
        "mem_percent": metric(mem_pct),
        "pids": metric([float(v) for v in pids]),
    }


def wait_for_health(base_url: str, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    url = f"{base_url.rstrip('/')}/health"
    last_error = "service not ready"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return
                last_error = f"http {resp.status}"
        except urllib.error.URLError as exc:
            last_error = str(exc)
        time.sleep(1.0)
    raise SystemExit(f"health check failed for {url}: {last_error}")


def existing_reports() -> set[str]:
    ensure_report_dir()
    return {str(path.resolve()) for path in REPORT_DIR.glob("*.json*")}


def find_new_reports(before: Iterable[str]) -> List[str]:
    before_set = set(before)
    after = sorted(str(path.resolve()) for path in REPORT_DIR.glob("*.json*"))
    return [path for path in after if path not in before_set]


def write_bundle_report(
    *,
    label: str,
    load_cmd: List[str],
    exit_code: int,
    stats_file: Path,
    stats_errors: List[str],
    new_reports: List[str],
) -> Path:
    samples: List[Dict[str, Any]] = []
    if stats_file.exists():
        for line in stats_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    bundle = {
        "started_at_utc": utc_now_iso(),
        "label": label,
        "container_name": CONTAINER_NAME,
        "resource_target": {"cpus": 2.0, "memory_limit": "2g"},
        "load_command": load_cmd,
        "load_command_shell": shlex.join(load_cmd),
        "stress_exit_code": exit_code,
        "docker_stats_file": str(stats_file.resolve()),
        "docker_stats_summary": summarize_stats(samples),
        "docker_stats_errors": stats_errors,
        "new_report_files": new_reports,
    }
    bundle_path = REPORT_DIR / f"vps_bundle_{label}_{utc_now_compact()}.json"
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return bundle_path


def cmd_up(args: argparse.Namespace) -> int:
    require_file(PROJECT_ROOT / ".env", "missing .env in project root")
    require_file(COMPOSE_FILE, f"missing compose file: {COMPOSE_FILE}")
    command = compose_cmd("up", "-d")
    if args.build:
        command.append("--build")
    run_command(command)
    wait_for_health(args.base_url, args.health_timeout)
    print(f"service ready at {args.base_url}")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    run_command(compose_cmd("down"), check=False)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    run_command(compose_cmd("logs", "--tail", str(args.tail), "-f", "app"))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    sample = sample_stats_once()
    print(json.dumps(sample, ensure_ascii=False, indent=2))
    return 0


def ensure_service(args: argparse.Namespace) -> None:
    up_args = argparse.Namespace(
        build=args.build,
        base_url=args.base_url,
        health_timeout=args.health_timeout,
    )
    cmd_up(up_args)


def build_concurrent_command(args: argparse.Namespace) -> List[str]:
    cmd = [
        python_bin(),
        str(PROJECT_ROOT / "scripts" / "stress_concurrent.py"),
        "--base-url",
        args.base_url,
        "--concurrency",
        str(args.concurrency),
        "--timeout",
        str(args.timeout),
        "--model",
        args.model,
        "--aspect-ratio",
        args.aspect_ratio,
        "--resolution",
        args.resolution,
        "--output-dir",
        str(args.output_dir),
        "--label",
        args.label,
        "--stagger-ms",
        str(args.stagger_ms),
    ]
    if args.seed is not None:
        cmd.extend(["--seed", str(args.seed)])
    if args.auth_token:
        cmd.extend(["--auth-token", args.auth_token])
    return cmd


def build_real_command(args: argparse.Namespace) -> List[str]:
    cmd = [
        python_bin(),
        str(PROJECT_ROOT / "scripts" / "stress_real_users.py"),
        "--base-url",
        args.base_url,
        "--stages",
        args.stages,
        "--output-dir",
        str(args.output_dir),
        "--user-max-inflight",
        str(args.user_max_inflight),
        "--img2img-ratio",
        str(args.img2img_ratio),
        "--text2img-model",
        args.text2img_model,
        "--img2img-model",
        args.img2img_model,
        "--aspect-ratio",
        args.aspect_ratio,
        "--resolution",
        args.resolution,
        "--http-timeout",
        str(args.http_timeout),
        "--generate-timeout",
        str(args.generate_timeout),
        "--preflight-timeout",
        str(args.preflight_timeout),
        "--stop-success-rate-below",
        str(args.stop_success_rate_below),
        "--generate-stagger-ms",
        str(args.generate_stagger_ms),
        "--stage-pause-seconds",
        str(args.stage_pause_seconds),
    ]
    if args.seed is not None:
        cmd.extend(["--seed", str(args.seed)])
    if args.admin_username:
        cmd.extend(["--admin-username", args.admin_username])
    if args.admin_password:
        cmd.extend(["--admin-password", args.admin_password])
    if args.user_prefix:
        cmd.extend(["--user-prefix", args.user_prefix])
    if args.reuse_user_prefix:
        cmd.extend(["--reuse-user-prefix", args.reuse_user_prefix])
    if args.password_prefix:
        cmd.extend(["--password-prefix", args.password_prefix])
    if args.reference_image_path:
        cmd.extend(["--reference-image-path", args.reference_image_path])
    return cmd


def run_load(
    *,
    label: str,
    load_cmd: List[str],
    interval_seconds: float,
) -> int:
    ensure_report_dir()
    before_reports = existing_reports()
    stats_file = REPORT_DIR / f"docker_stats_{label}_{utc_now_compact()}.jsonl"
    stats_errors: List[str] = []
    stop_event = threading.Event()
    sampler = threading.Thread(
        target=collect_stats,
        kwargs={
            "output_path": stats_file,
            "stop_event": stop_event,
            "interval_seconds": interval_seconds,
            "errors": stats_errors,
        },
        daemon=True,
    )

    print(f"[load] {shlex.join(load_cmd)}")
    print(f"[stats] writing {stats_file}")
    sampler.start()
    try:
        completed = run_command(load_cmd, check=False)
        exit_code = int(completed.returncode)
    finally:
        stop_event.set()
        sampler.join(timeout=max(3.0, interval_seconds + 1.0))

    new_reports = find_new_reports(before_reports)
    bundle_path = write_bundle_report(
        label=label,
        load_cmd=load_cmd,
        exit_code=exit_code,
        stats_file=stats_file,
        stats_errors=stats_errors,
        new_reports=new_reports,
    )
    print(f"[bundle] {bundle_path}")
    if new_reports:
        for report in new_reports:
            print(f"[report] {report}")
    return exit_code


def cmd_run_concurrent(args: argparse.Namespace) -> int:
    if args.ensure_up:
        ensure_service(args)
    else:
        wait_for_health(args.base_url, args.health_timeout)
    return run_load(
        label=args.label or f"concurrent_c{args.concurrency:03d}",
        load_cmd=build_concurrent_command(args),
        interval_seconds=args.stats_interval,
    )


def cmd_run_real(args: argparse.Namespace) -> int:
    if args.ensure_up:
        ensure_service(args)
    else:
        wait_for_health(args.base_url, args.health_timeout)
    stages_label = args.stages.replace(",", "-")
    return run_load(
        label=args.label or f"real_{stages_label}",
        load_cmd=build_real_command(args),
        interval_seconds=args.stats_interval,
    )


def add_common_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--health-timeout", type=float, default=120.0)
    parser.add_argument("--output-dir", type=Path, default=REPORT_DIR)
    parser.add_argument("--stats-interval", type=float, default=DEFAULT_STATS_INTERVAL)
    parser.add_argument("--build", action="store_true", help="auto build image when ensuring service is up")
    parser.add_argument("--ensure-up", action="store_true", help="start the 2c2g container before load test")
    parser.add_argument("--label", default="", help="bundle/report label suffix")
    parser.add_argument("--seed", type=int, default=None)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="2c2g VPS-like stress runner for st-imagen.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    up = subparsers.add_parser("up", help="start the 2c2g-limited app container")
    up.add_argument("--build", action="store_true")
    up.add_argument("--base-url", default=DEFAULT_BASE_URL)
    up.add_argument("--health-timeout", type=float, default=120.0)
    up.set_defaults(func=cmd_up)

    down = subparsers.add_parser("down", help="stop the 2c2g-limited app container")
    down.set_defaults(func=cmd_down)

    logs = subparsers.add_parser("logs", help="tail app container logs")
    logs.add_argument("--tail", type=int, default=200)
    logs.set_defaults(func=cmd_logs)

    stats = subparsers.add_parser("stats", help="show one docker stats sample")
    stats.set_defaults(func=cmd_stats)

    run_concurrent = subparsers.add_parser("run-concurrent", help="run direct SSE concurrency stress")
    add_common_runtime_args(run_concurrent)
    run_concurrent.add_argument("--concurrency", type=int, default=20)
    run_concurrent.add_argument("--timeout", type=float, default=300.0)
    run_concurrent.add_argument("--model", default="Nano Banana Pro")
    run_concurrent.add_argument("--aspect-ratio", default="1:1")
    run_concurrent.add_argument("--resolution", default="2K")
    run_concurrent.add_argument("--stagger-ms", type=float, default=1200.0)
    run_concurrent.add_argument("--auth-token", default=os.getenv("ST_IMAGEN_AUTH_TOKEN", ""))
    run_concurrent.set_defaults(func=cmd_run_concurrent)

    run_real = subparsers.add_parser("run-real", help="run browser-like staged stress")
    add_common_runtime_args(run_real)
    run_real.add_argument("--stages", default="10,20,30")
    run_real.add_argument("--admin-username", default="")
    run_real.add_argument("--admin-password", default="")
    run_real.add_argument("--user-prefix", default="stressu")
    run_real.add_argument("--reuse-user-prefix", default="")
    run_real.add_argument("--password-prefix", default="StressPass")
    run_real.add_argument("--user-max-inflight", type=int, default=1)
    run_real.add_argument("--img2img-ratio", type=float, default=0.2)
    run_real.add_argument("--reference-image-path", default="")
    run_real.add_argument("--text2img-model", default="Nano Banana Pro")
    run_real.add_argument("--img2img-model", default="gemini-3-pro-image-preview")
    run_real.add_argument("--aspect-ratio", default="1:1")
    run_real.add_argument("--resolution", default="2K")
    run_real.add_argument("--http-timeout", type=float, default=30.0)
    run_real.add_argument("--generate-timeout", type=float, default=300.0)
    run_real.add_argument("--preflight-timeout", type=float, default=90.0)
    run_real.add_argument("--stop-success-rate-below", type=float, default=0.8)
    run_real.add_argument("--generate-stagger-ms", type=float, default=1500.0)
    run_real.add_argument("--stage-pause-seconds", type=float, default=10.0)
    run_real.set_defaults(func=cmd_run_real)

    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
