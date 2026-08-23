#!/usr/bin/env python3
"""Real multi-user staged stress for st-imagen.

The flow matches the browser path more closely than the basic concurrent
generator:

1. Admin logs in and creates invite codes
2. Synthetic end users activate accounts and keep their own cookie sessions
3. Each stage replays a lightweight page flow:
   /api/auth/status -> /api/auth/me -> /api/options -> /api/recent-images
4. All prepared users start /api/generate/stream at roughly the same time
5. Optional img2img users first upload a local reference image

Reports are saved under data/stress_reports by default.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import random
import secrets
import sqlite3
import statistics
import sys
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from dotenv import dotenv_values


TEXT2IMG_PROMPTS = [
    "a compact neon ramen bar at midnight, cinematic rain, realistic",
    "a red fox reading a map under pine trees, soft morning light",
    "minimalist product photo of a silver watch on slate stone",
    "a retro green scooter parked beside a seaside cafe, travel editorial",
    "a glass greenhouse in winter with warm lights inside, photo realistic",
    "an origami whale floating above city rooftops at dusk, dreamy",
    "a tiny bakery counter with fresh croissants, high detail food photography",
    "a brass telescope on an old wooden desk, moody museum lighting",
]

IMG2IMG_PROMPTS = [
    "keep the composition, convert into polished product photography, crisp reflections",
    "preserve the main shapes, turn it into a cinematic night scene with richer contrast",
    "retain the subject layout, restyle as premium magazine editorial photography",
    "keep the object structure, transform into a clean minimalist studio shot",
]


def utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int((len(ordered) - 1) * p)
    return float(ordered[idx])


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


def safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def extract_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        if isinstance(payload.get("message"), str) and payload["message"].strip():
            return payload["message"].strip()
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if isinstance(detail, dict):
            if isinstance(detail.get("message"), str) and detail["message"].strip():
                return detail["message"].strip()
    return fallback


def pick_reference_image(explicit_path: str) -> Optional[Path]:
    if explicit_path:
        path = Path(explicit_path).expanduser().resolve()
        return path if path.exists() else None

    generated_dir = Path(__file__).resolve().parents[1] / "data" / "uploads" / "generated"
    if not generated_dir.exists():
        return None
    candidates = [p for p in generated_dir.iterdir() if p.is_file()]
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: (p.stat().st_size, p.name))[0]


@dataclass
class UserRuntime:
    username: str
    password: str
    client: httpx.AsyncClient
    user_id: Optional[str] = None
    session_token: Optional[str] = None


@dataclass
class FlowResult:
    stage: str
    username: str
    mode: str
    prompt: str
    status: str = "pending"
    error_message: Optional[str] = None
    error_status_code: Optional[int] = None
    authenticated_before: Optional[bool] = None
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    auth_status_ms: Optional[float] = None
    login_ms: Optional[float] = None
    me_ms: Optional[float] = None
    options_ms: Optional[float] = None
    recent_images_ms: Optional[float] = None
    reference_upload_ms: Optional[float] = None
    preflight_ms: Optional[float] = None
    wait_for_gate_ms: Optional[float] = None
    generate_launch_delay_ms: Optional[float] = None
    first_event_ms: Optional[float] = None
    first_image_ms: Optional[float] = None
    generate_total_ms: Optional[float] = None
    total_ms: Optional[float] = None
    post_me_ms: Optional[float] = None
    upstream_response_time_ms: Optional[int] = None
    images_count: int = 0
    sse_event_count: int = 0


class StressError(Exception):
    pass


class RealUserStressRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.project_root = Path(__file__).resolve().parents[1]
        self.output_dir = Path(args.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.stamp = utc_now_compact()
        self.stage_values = self._parse_stages(args.stages)
        self.args.img2img_ratio = max(0.0, min(1.0, float(self.args.img2img_ratio)))
        self.reference_image_path = pick_reference_image(args.reference_image_path)
        reuse_prefix = str(args.reuse_user_prefix or "").strip().lower()
        self.reuse_user_prefix = reuse_prefix or None
        self.user_prefix = self.reuse_user_prefix or f"{args.user_prefix}_{self.stamp.lower()}"
        self.env_values = dotenv_values(self.project_root / ".env")
        self.admin_username, self.admin_password = self._load_admin_credentials()
        self.session_cookie_name = str(self.env_values.get("USER_SESSION_COOKIE_NAME") or "st_imagen_session")
        self.admin_client = httpx.AsyncClient(
            base_url=args.base_url,
            timeout=httpx.Timeout(args.http_timeout, connect=10.0),
            follow_redirects=True,
            trust_env=False,
            headers={"User-Agent": f"st-imagen-real-stress/{self.stamp} admin"},
        )
        self.admin_token: Optional[str] = None
        self.users: List[UserRuntime] = []

    @staticmethod
    def _parse_stages(raw: str) -> List[int]:
        values: List[int] = []
        for chunk in str(raw or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            values.append(max(1, int(chunk)))
        if not values:
            raise ValueError("stages cannot be empty")
        return values

    def _load_admin_credentials(self) -> tuple[str, str]:
        username = (
            self.args.admin_username
            or os.getenv("ST_IMAGEN_ADMIN_USERNAME")
            or self.env_values.get("ADMIN_USERNAME")
            or "admin"
        )
        password = (
            self.args.admin_password
            or os.getenv("ST_IMAGEN_ADMIN_PASSWORD")
            or self.env_values.get("ADMIN_PASSWORD")
            or ""
        )
        if not password:
            raise StressError("admin password is missing; pass --admin-password or set ST_IMAGEN_ADMIN_PASSWORD")
        return str(username), str(password)

    async def close(self) -> None:
        for user in self.users:
            try:
                await user.client.aclose()
            except Exception:
                pass
        await self.admin_client.aclose()

    async def admin_login(self) -> None:
        resp = await self.admin_client.post(
            "/api/admin/login",
            json={"username": self.admin_username, "password": self.admin_password},
        )
        payload = safe_json(resp)
        if resp.status_code >= 400 or not isinstance(payload, dict) or not payload.get("success") or not payload.get("token"):
            raise StressError(
                f"admin login failed: http {resp.status_code} "
                f"{extract_message(payload, resp.text[:300] or 'unknown error')}"
            )
        self.admin_token = str(payload["token"])

    def _admin_headers(self) -> Dict[str, str]:
        if not self.admin_token:
            raise StressError("admin token not initialized")
        return {"Authorization": f"Bearer {self.admin_token}"}

    def _merge_headers(self, base: Optional[Dict[str, str]], extra: Optional[Dict[str, str]]) -> Dict[str, str]:
        merged: Dict[str, str] = {}
        if base:
            merged.update(base)
        if extra:
            merged.update(extra)
        return merged

    def _refresh_session_token(self, user: UserRuntime, resp: Optional[httpx.Response] = None) -> None:
        token = user.client.cookies.get(self.session_cookie_name)
        if not token and resp is not None:
            token = resp.cookies.get(self.session_cookie_name)
        if token:
            user.session_token = str(token)

    def _user_headers(self, user: Optional[UserRuntime]) -> Dict[str, str]:
        if user is None or not user.session_token:
            return {}
        return {"Authorization": f"Bearer {user.session_token}"}

    def _resolve_database_path(self) -> Path:
        db_url = str(
            os.getenv("DATABASE_URL")
            or self.env_values.get("DATABASE_URL")
            or "sqlite+aiosqlite:///./data/image_gen.db"
        ).strip()
        prefix = "sqlite+aiosqlite:///"
        if not db_url.startswith(prefix):
            raise StressError(f"unsupported DATABASE_URL for reuse mode: {db_url}")
        raw_path = db_url[len(prefix) :]
        path = Path(raw_path)
        if not path.is_absolute():
            path = (self.project_root / raw_path).resolve()
        return path

    def _load_existing_users(self, count: int) -> List[UserRuntime]:
        if not self.reuse_user_prefix:
            raise StressError("reuse_user_prefix is required")

        if str(self.project_root) not in sys.path:
            sys.path.insert(0, str(self.project_root))
        from app.services.crypto import CryptoService

        db_path = self._resolve_database_path()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expires = now + timedelta(days=max(1, int(os.getenv("USER_SESSION_DAYS", "30"))))
        pattern = f"{self.reuse_user_prefix}_*"

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, username
                FROM users
                WHERE status = 'active' AND username GLOB ?
                ORDER BY username ASC
                LIMIT ?
                """,
                (pattern, count),
            ).fetchall()
            if len(rows) < count:
                raise StressError(
                    f"reuse prefix {self.reuse_user_prefix} has only {len(rows)} active users, need {count}"
                )

            runtimes: List[UserRuntime] = []
            for row in rows:
                username = str(row["username"])
                raw_token = secrets.token_urlsafe(32)
                conn.execute(
                    """
                    INSERT INTO user_sessions (
                        id, user_id, token_hash, ip_address, user_agent,
                        created_at, expires_at, last_seen_at, revoked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        str(uuid.uuid4()),
                        str(row["id"]),
                        CryptoService.hash_api_key(raw_token),
                        "127.0.0.1",
                        f"st-imagen-real-stress/{self.stamp} reuse {username}",
                        now.isoformat(sep=" ", timespec="microseconds"),
                        expires.isoformat(sep=" ", timespec="microseconds"),
                        now.isoformat(sep=" ", timespec="microseconds"),
                    ),
                )

                client = httpx.AsyncClient(
                    base_url=self.args.base_url,
                    timeout=httpx.Timeout(self.args.http_timeout, connect=10.0),
                    follow_redirects=True,
                    trust_env=False,
                    headers={"User-Agent": f"st-imagen-real-stress/{self.stamp} {username}"},
                )
                client.cookies.set(self.session_cookie_name, raw_token)
                runtimes.append(
                    UserRuntime(
                        username=username,
                        password="",
                        client=client,
                        user_id=str(row["id"]),
                        session_token=raw_token,
                    )
                )

            conn.commit()
            return runtimes
        finally:
            conn.close()

    async def create_invite_codes(self, count: int) -> List[str]:
        resp = await self.admin_client.post(
            "/api/admin/invite-codes",
            headers=self._admin_headers(),
            json={
                "count": count,
                "max_uses": 1,
                "expires_in_days": 7,
                "note": f"stress:{self.user_prefix}",
                "daily_quota": 0,
                "max_inflight": self.args.user_max_inflight,
            },
        )
        payload = safe_json(resp)
        if resp.status_code >= 400 or not isinstance(payload, dict):
            raise StressError(
                f"create invite codes failed: http {resp.status_code} "
                f"{extract_message(payload, resp.text[:300] or 'unknown error')}"
            )
        items = payload.get("items") or []
        codes = [str(item.get("raw_code") or "").strip() for item in items if str(item.get("raw_code") or "").strip()]
        if len(codes) != count:
            raise StressError(f"expected {count} invite codes, got {len(codes)}")
        return codes

    async def activate_user(self, idx: int, invite_code: str) -> UserRuntime:
        username = f"{self.user_prefix}_{idx:03d}"
        password = f"{self.args.password_prefix}-{uuid.uuid4().hex[:12]}"
        client = httpx.AsyncClient(
            base_url=self.args.base_url,
            timeout=httpx.Timeout(self.args.http_timeout, connect=10.0),
            follow_redirects=True,
            trust_env=False,
            headers={"User-Agent": f"st-imagen-real-stress/{self.stamp} {username}"},
        )
        resp = await client.post(
            "/api/auth/activate",
            json={
                "invite_code": invite_code,
                "username": username,
                "password": password,
            },
        )
        payload = safe_json(resp)
        if resp.status_code >= 400 or not isinstance(payload, dict) or not payload.get("success"):
            await client.aclose()
            raise StressError(
                f"activate user {username} failed: http {resp.status_code} "
                f"{extract_message(payload, resp.text[:300] or 'unknown error')}"
            )
        user_payload = payload.get("user") or {}
        runtime = UserRuntime(
            username=username,
            password=password,
            client=client,
            user_id=str(user_payload.get("id") or "") or None,
        )
        self._refresh_session_token(runtime, resp)
        return runtime

    async def bootstrap_users(self) -> None:
        needed = max(self.stage_values)
        if self.reuse_user_prefix:
            self.users = self._load_existing_users(needed)
            return
        invite_codes = await self.create_invite_codes(needed)
        tasks = [asyncio.create_task(self.activate_user(i + 1, code)) for i, code in enumerate(invite_codes)]
        self.users = list(await asyncio.gather(*tasks))

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        result: FlowResult,
        metric_name: str,
        user: Optional[UserRuntime] = None,
        expected_statuses: tuple[int, ...] = (200,),
        return_response: bool = False,
        **kwargs: Any,
    ) -> Any:
        started = time.monotonic()
        resp = await client.request(method, path, **kwargs)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        setattr(result, metric_name, elapsed_ms)
        payload = safe_json(resp)
        if resp.status_code not in expected_statuses:
            result.status = "error"
            result.error_status_code = resp.status_code
            result.error_message = f"{path}: {extract_message(payload, resp.text[:300] or 'request failed')}"
            raise StressError(result.error_message)
        if return_response:
            return payload, resp
        return payload

    async def _ensure_logged_in(self, user: UserRuntime, result: FlowResult) -> None:
        status_payload = await self._request_json(
            user.client,
            "GET",
            "/api/auth/status",
            result=result,
            metric_name="auth_status_ms",
            user=user,
        )
        authenticated = bool((status_payload or {}).get("authenticated"))
        result.authenticated_before = authenticated
        if authenticated:
            return
        login_payload, login_resp = await self._request_json(
            user.client,
            "POST",
            "/api/auth/login",
            result=result,
            metric_name="login_ms",
            user=user,
            return_response=True,
            json={"username": user.username, "password": user.password},
        )
        self._refresh_session_token(user, login_resp)
        if not isinstance(login_payload, dict) or not login_payload.get("success"):
            raise StressError("login response missing success=true")

    async def _upload_reference_image(self, user: UserRuntime, result: FlowResult) -> str:
        if not self.reference_image_path:
            raise StressError("img2img requested but no local reference image is available")
        content_type = mimetypes.guess_type(self.reference_image_path.name)[0] or "application/octet-stream"
        data = await asyncio.to_thread(self.reference_image_path.read_bytes)
        payload = await self._request_json(
            user.client,
            "POST",
            "/api/reference-image",
            result=result,
            metric_name="reference_upload_ms",
            user=user,
            files={"file": (self.reference_image_path.name, data, content_type)},
        )
        url = str((payload or {}).get("url") or "").strip()
        if not url:
            raise StressError("reference-image response missing url")
        return url

    async def _get_me_with_retry(self, user: UserRuntime, result: FlowResult, metric_name: str) -> Any:
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                return await self._request_json(
                    user.client,
                    "GET",
                    "/api/auth/me",
                    result=result,
                    metric_name=metric_name,
                    user=user,
                )
            except StressError as exc:
                last_exc = exc
                if result.error_status_code != 401 or attempt >= 2:
                    raise
                result.status = "pending"
                result.error_status_code = None
                result.error_message = None
                await asyncio.sleep(0.05 * (attempt + 1))
        if last_exc:
            raise last_exc
        raise StressError("failed to fetch /api/auth/me")

    async def run_one(
        self,
        user: UserRuntime,
        stage_name: str,
        generate_gate: asyncio.Event,
        ready_event: asyncio.Event,
    ) -> FlowResult:
        mode = "img2img" if self.reference_image_path and random.random() < self.args.img2img_ratio else "text2img"
        prompt = random.choice(IMG2IMG_PROMPTS if mode == "img2img" else TEXT2IMG_PROMPTS)
        prompt = f"{prompt} [stress {stage_name} {uuid.uuid4().hex[:8]}]"
        result = FlowResult(stage=stage_name, username=user.username, mode=mode, prompt=prompt)
        started_at = time.monotonic()
        gate_wait_started_at = 0.0
        generate_started_at: Optional[float] = None

        try:
            await self._ensure_logged_in(user, result)
            me_payload = await self._get_me_with_retry(user, result, "me_ms")
            if isinstance(me_payload, dict):
                user.user_id = str(me_payload.get("id") or "") or user.user_id
            await self._request_json(
                user.client,
                "GET",
                "/api/options",
                result=result,
                metric_name="options_ms",
                user=user,
            )
            await self._request_json(
                user.client,
                "GET",
                f"/api/recent-images?limit={random.choice((8, 12, 24))}",
                result=result,
                metric_name="recent_images_ms",
                user=user,
            )

            payload = {
                "mode": mode,
                "prompt": prompt,
            }
            if mode == "text2img":
                payload.update(
                    {
                        "model": self.args.text2img_model,
                        "aspect_ratio": self.args.aspect_ratio,
                        "resolution": self.args.resolution,
                    }
                )
            else:
                reference_url = await self._upload_reference_image(user, result)
                payload.update(
                    {
                        "model": self.args.img2img_model,
                        "aspect_ratio": self.args.aspect_ratio,
                        "resolution": self.args.resolution,
                        "image_url": reference_url,
                    }
                )

            result.preflight_ms = round((time.monotonic() - started_at) * 1000, 1)
            gate_wait_started_at = time.monotonic()
            ready_event.set()
            await generate_gate.wait()
            result.wait_for_gate_ms = round((time.monotonic() - gate_wait_started_at) * 1000, 1)
            if self.args.generate_stagger_ms > 0:
                delay_seconds = random.uniform(0.0, self.args.generate_stagger_ms / 1000.0)
                result.generate_launch_delay_ms = round(delay_seconds * 1000.0, 1)
                await asyncio.sleep(delay_seconds)
            generate_started_at = time.monotonic()
            async with user.client.stream(
                "POST",
                "/api/generate/stream",
                json=payload,
                timeout=httpx.Timeout(self.args.generate_timeout, connect=10.0),
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "ignore")[:500]
                    result.status = "error"
                    result.error_status_code = resp.status_code
                    result.error_message = f"http {resp.status_code}: {body}"
                    return result

                saw_first_event = False
                async for raw_line in resp.aiter_lines():
                    if not raw_line or not raw_line.startswith("data:"):
                        continue
                    data_str = raw_line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        evt = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    result.sse_event_count += 1
                    now_ms = (time.monotonic() - generate_started_at) * 1000
                    if not saw_first_event:
                        result.first_event_ms = round(now_ms, 1)
                        saw_first_event = True
                    etype = evt.get("type")
                    if etype == "start":
                        result.account_id = evt.get("account_id")
                        result.account_name = evt.get("account_name") or evt.get("account_short")
                    elif etype == "complete":
                        images = evt.get("images") or []
                        if isinstance(images, list):
                            result.images_count = len(images)
                        if result.images_count > 0 and result.first_image_ms is None:
                            result.first_image_ms = round(now_ms, 1)
                        upstream_ms = evt.get("response_time_ms")
                        if isinstance(upstream_ms, (int, float)):
                            result.upstream_response_time_ms = int(upstream_ms)
                        result.status = "success" if result.images_count > 0 else "error"
                        if result.status != "success":
                            result.error_message = "complete with no images"
                        break
                    elif etype == "error":
                        result.status = "error"
                        result.error_message = str(evt.get("message") or "unknown error")
                        status_code = evt.get("status_code")
                        if isinstance(status_code, int):
                            result.error_status_code = status_code
                        break

            post_me_payload = await self._request_json(
                user.client,
                "GET",
                "/api/auth/me",
                result=result,
                metric_name="post_me_ms",
                user=user,
                expected_statuses=(200, 401),
            )
            if result.status == "pending":
                result.status = "error"
                result.error_message = "stream ended without complete/error"
            if result.status == "success" and isinstance(post_me_payload, dict):
                user.user_id = str(post_me_payload.get("id") or "") or user.user_id
        except httpx.TimeoutException as exc:
            result.status = "timeout"
            result.error_message = f"{type(exc).__name__}: {exc}"
        except StressError as exc:
            if result.status == "pending":
                result.status = "error"
                result.error_message = str(exc)
        except Exception as exc:
            result.status = "error"
            result.error_message = f"{type(exc).__name__}: {exc}"
        finally:
            if not ready_event.is_set():
                ready_event.set()
            if generate_started_at is not None:
                result.generate_total_ms = round((time.monotonic() - generate_started_at) * 1000, 1)
            result.total_ms = round((time.monotonic() - started_at) * 1000, 1)
            if result.status == "pending":
                result.status = "error"
                result.error_message = result.error_message or "unknown error"
        return result

    @staticmethod
    def summarize_stage(
        stage_name: str,
        concurrency: int,
        started_iso: str,
        wall_ms: float,
        results: List[FlowResult],
    ) -> Dict[str, Any]:
        success = [r for r in results if r.status == "success"]
        failures = [r for r in results if r.status != "success"]
        errors = Counter()
        accounts = Counter()
        modes = Counter(r.mode for r in results)
        for item in results:
            if item.account_name:
                accounts[item.account_name] += 1
            if item.status == "success":
                continue
            if item.error_status_code:
                errors[f"http_{item.error_status_code}"] += 1
            elif item.error_message:
                errors[f"{item.status}:{item.error_message.split(':', 1)[0][:60]}"] += 1
            else:
                errors[item.status] += 1
        summary = {
            "stage": stage_name,
            "started_at": started_iso,
            "concurrency": concurrency,
            "wall_ms": round(wall_ms, 1),
            "throughput_per_sec": round(len(success) / max(wall_ms / 1000, 0.001), 2),
            "success": len(success),
            "error": len(failures),
            "success_rate": round(len(success) / len(results), 4) if results else 0.0,
            "mode_distribution": dict(modes),
            "auth_status_ms": stats_dict([r.auth_status_ms for r in results if r.auth_status_ms is not None]),
            "login_ms": stats_dict([r.login_ms for r in results if r.login_ms is not None]),
            "me_ms": stats_dict([r.me_ms for r in results if r.me_ms is not None]),
            "options_ms": stats_dict([r.options_ms for r in results if r.options_ms is not None]),
            "recent_images_ms": stats_dict([r.recent_images_ms for r in results if r.recent_images_ms is not None]),
            "reference_upload_ms": stats_dict([r.reference_upload_ms for r in results if r.reference_upload_ms is not None]),
            "preflight_ms": stats_dict([r.preflight_ms for r in results if r.preflight_ms is not None]),
            "wait_for_gate_ms": stats_dict([r.wait_for_gate_ms for r in results if r.wait_for_gate_ms is not None]),
            "generate_launch_delay_ms": stats_dict(
                [r.generate_launch_delay_ms for r in results if r.generate_launch_delay_ms is not None]
            ),
            "first_event_ms": stats_dict([r.first_event_ms for r in results if r.first_event_ms is not None]),
            "first_image_ms": stats_dict([r.first_image_ms for r in success if r.first_image_ms is not None]),
            "generate_total_ms": stats_dict([r.generate_total_ms for r in results if r.generate_total_ms is not None]),
            "total_ms": stats_dict([r.total_ms for r in results if r.total_ms is not None]),
            "post_me_ms": stats_dict([r.post_me_ms for r in results if r.post_me_ms is not None]),
            "upstream_response_time_ms": stats_dict(
                [float(r.upstream_response_time_ms) for r in success if r.upstream_response_time_ms is not None]
            ),
            "account_distribution": dict(accounts.most_common()),
            "error_buckets": dict(errors.most_common()),
        }
        return {"summary": summary, "tasks": [asdict(r) for r in results]}

    @staticmethod
    def print_stage_summary(summary: Dict[str, Any]) -> None:
        bar = "-" * 72
        print(f"\n{bar}")
        print(f"stage={summary['stage']} concurrency={summary['concurrency']} success={summary['success']}/{summary['concurrency']}")
        print(bar)
        print(
            f"wall={summary['wall_ms']/1000:.1f}s rate={summary['success_rate']*100:.1f}% "
            f"throughput={summary['throughput_per_sec']}/s modes={summary['mode_distribution']}"
        )
        gen_total = summary["generate_total_ms"]
        print(f"generate_ms     p50={gen_total['p50']} p95={gen_total['p95']} max={gen_total['max']}")
        total = summary["total_ms"]
        print(f"flow_total_ms   p50={total['p50']} p95={total['p95']} max={total['max']}")
        pre = summary["preflight_ms"]
        print(f"preflight_ms    p50={pre['p50']} p95={pre['p95']} max={pre['max']}")
        first = summary["first_event_ms"]
        print(f"first_event_ms  p50={first['p50']} p95={first['p95']} max={first['max']}")
        stagger = summary["generate_launch_delay_ms"]
        if stagger["count"]:
            print(f"launch_jitter_ms p50={stagger['p50']} p95={stagger['p95']} max={stagger['max']}")
        first_img = summary["first_image_ms"]
        print(f"first_image_ms  p50={first_img['p50']} p95={first_img['p95']} max={first_img['max']}")
        up = summary["upstream_response_time_ms"]
        if up["count"]:
            print(f"upstream_ms     p50={up['p50']} p95={up['p95']} max={up['max']}")
        if summary["account_distribution"]:
            sample = "  ".join(f"{k}={v}" for k, v in list(summary["account_distribution"].items())[:10])
            print(f"accounts        {sample}")
        if summary["error_buckets"]:
            print(f"errors          {summary['error_buckets']}")

    async def run_stage(self, concurrency: int) -> Dict[str, Any]:
        stage_name = f"c{concurrency:03d}"
        started_iso = datetime.now(timezone.utc).isoformat()
        users = self.users[:concurrency]
        generate_gate = asyncio.Event()
        ready_events = [asyncio.Event() for _ in users]
        started_at = time.monotonic()
        tasks = [
            asyncio.create_task(self.run_one(user, stage_name, generate_gate, ready_event))
            for user, ready_event in zip(users, ready_events)
        ]
        try:
            await asyncio.wait_for(
                asyncio.gather(*(evt.wait() for evt in ready_events)),
                timeout=self.args.preflight_timeout,
            )
        except asyncio.TimeoutError:
            print(f"[warn] stage {stage_name} preflight wait exceeded {self.args.preflight_timeout}s; releasing gate anyway")
        generate_gate.set()
        results = list(await asyncio.gather(*tasks))
        wall_ms = (time.monotonic() - started_at) * 1000
        return self.summarize_stage(stage_name, concurrency, started_iso, wall_ms, results)

    async def run(self) -> Dict[str, Any]:
        print(
            f"[bootstrap] base_url={self.args.base_url} stages={self.stage_values} "
            f"img2img_ratio={self.args.img2img_ratio} reference_image="
            f"{self.reference_image_path if self.reference_image_path else '<disabled>'}"
        )
        await self.admin_login()
        await self.bootstrap_users()
        action = "reused_users" if self.reuse_user_prefix else "activated_users"
        print(f"[bootstrap] {action}={len(self.users)} prefix={self.user_prefix}")

        stage_reports: List[Dict[str, Any]] = []
        for index, concurrency in enumerate(self.stage_values):
            report = await self.run_stage(concurrency)
            stage_reports.append(report)
            self.print_stage_summary(report["summary"])
            success_rate = float(report["summary"]["success_rate"])
            if success_rate < self.args.stop_success_rate_below:
                print(
                    f"[stop] stage c{concurrency:03d} success_rate={success_rate:.3f} "
                    f"< threshold={self.args.stop_success_rate_below:.3f}"
                )
                break
            if self.args.stage_pause_seconds > 0 and index < len(self.stage_values) - 1:
                await asyncio.sleep(self.args.stage_pause_seconds)

        return {
            "started_at_utc": self.stamp,
            "config": {
                "base_url": self.args.base_url,
                "stages": self.stage_values,
                "user_prefix": self.user_prefix,
                "user_max_inflight": self.args.user_max_inflight,
                "img2img_ratio": self.args.img2img_ratio,
                "text2img_model": self.args.text2img_model,
                "img2img_model": self.args.img2img_model,
                "aspect_ratio": self.args.aspect_ratio,
                "resolution": self.args.resolution,
                "http_timeout": self.args.http_timeout,
                "generate_timeout": self.args.generate_timeout,
                "preflight_timeout": self.args.preflight_timeout,
                "generate_stagger_ms": self.args.generate_stagger_ms,
                "stage_pause_seconds": self.args.stage_pause_seconds,
                "reference_image_path": str(self.reference_image_path) if self.reference_image_path else None,
                "output_dir": str(self.output_dir),
            },
            "bootstrap": {
                "activated_users": len(self.users),
            },
            "stages": stage_reports,
        }


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Real multi-user staged stress for st-imagen.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--stages", default="30,40,50")
    parser.add_argument("--output-dir", default="data/stress_reports")
    parser.add_argument("--admin-username", default="")
    parser.add_argument("--admin-password", default="")
    parser.add_argument("--user-prefix", default="stressu")
    parser.add_argument("--reuse-user-prefix", default="")
    parser.add_argument("--password-prefix", default="StressPass")
    parser.add_argument("--user-max-inflight", type=int, default=1)
    parser.add_argument("--img2img-ratio", type=float, default=0.2)
    parser.add_argument("--reference-image-path", default="")
    parser.add_argument("--text2img-model", default="Nano Banana Pro")
    parser.add_argument("--img2img-model", default="gemini-3-pro-image-preview")
    parser.add_argument("--aspect-ratio", default="1:1")
    parser.add_argument("--resolution", default="2K")
    parser.add_argument("--http-timeout", type=float, default=30.0)
    parser.add_argument("--generate-timeout", type=float, default=240.0)
    parser.add_argument("--preflight-timeout", type=float, default=90.0)
    parser.add_argument("--generate-stagger-ms", type=float, default=0.0)
    parser.add_argument("--stage-pause-seconds", type=float, default=0.0)
    parser.add_argument("--stop-success-rate-below", type=float, default=0.8)
    parser.add_argument("--seed", type=int, default=None)
    return parser.parse_args(argv)


async def async_main(args: argparse.Namespace) -> int:
    if args.seed is not None:
        random.seed(args.seed)
    runner = RealUserStressRunner(args)
    try:
        report = await runner.run()
    finally:
        await runner.close()

    stamp = report["started_at_utc"]
    report_path = Path(args.output_dir) / f"real_user_stress_{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport_file={report_path}")
    return 0


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
