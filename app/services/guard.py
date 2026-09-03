"""进程内流量守卫：全局限流、每用户 RPM、上游熔断、管理员登录防爆破。

单 worker 部署（UVICORN_WORKERS=1）是这些进程内状态成立的前提；
所有阈值都可用环境变量调整，设为 0 表示关闭对应守卫。
"""
from __future__ import annotations

from app.time_utils import utcnow_naive

import asyncio
import logging
import os
import random
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


class SlidingWindowRateLimiter:
    """按 key 的滑动窗口限流（进程内存）。

    key 通常是 user_id。窗口内超出 limit 的请求抛 RateLimitedError。
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = max(0, limit)
        self.window_seconds = window_seconds
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    async def check(self, key: str, *, now: Optional[float] = None) -> Tuple[bool, float]:
        """返回 (允许, 建议等待秒数)。允许时同时记录一次命中。"""
        if not self.enabled:
            return True, 0.0
        moment = now if now is not None else time.monotonic()
        async with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                bucket = deque()
                self._hits[key] = bucket
            cutoff = moment - self.window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry_after = max(0.0, bucket[0] + self.window_seconds - moment)
                # 惰性清理：key 过多时丢弃空桶，防止长期运行内存增长
                if len(self._hits) > 10000:
                    self._hits = {k: v for k, v in self._hits.items() if v}
                return False, retry_after
            bucket.append(moment)
            return True, 0.0


class CircuitBreaker:
    """上游熔断：连续 N 次 failover 级失败后断路 M 秒，期间快速失败。

    任何一次成功复位计数。半开由「断路到期后放行首个请求」天然实现——
    到期后 allow() 恢复 True，若该请求再失败会立刻重新计数并再次断路。
    """

    def __init__(self, failure_threshold: int, open_seconds: float) -> None:
        self.failure_threshold = max(0, failure_threshold)
        self.open_seconds = open_seconds
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None

    @property
    def enabled(self) -> bool:
        return self.failure_threshold > 0 and self.open_seconds > 0

    def allow(self, *, now: Optional[float] = None) -> Tuple[bool, float]:
        """返回 (放行, 剩余断路秒数)。"""
        if not self.enabled or self._opened_at is None:
            return True, 0.0
        moment = now if now is not None else time.monotonic()
        elapsed = moment - self._opened_at
        if elapsed >= self.open_seconds:
            self._opened_at = None
            return True, 0.0
        return False, self.open_seconds - elapsed

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self, *, now: Optional[float] = None) -> None:
        if not self.enabled:
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._opened_at = now if now is not None else time.monotonic()
            logger.warning(
                "upstream circuit opened after %s consecutive failures for %ss",
                self._consecutive_failures,
                self.open_seconds,
            )

    def reset(self) -> None:
        """手动复位（管理后台用）：清空失败计数并闭合断路器。"""
        self._consecutive_failures = 0
        self._opened_at = None

    def snapshot(self) -> Dict[str, object]:
        """当前状态快照（纯读取，不改变断路状态），供管理接口展示。"""
        is_open = False
        remaining = 0.0
        if self.enabled and self._opened_at is not None:
            elapsed = time.monotonic() - self._opened_at
            if elapsed < self.open_seconds:
                is_open = True
                remaining = self.open_seconds - elapsed
        return {
            "enabled": self.enabled,
            "failure_threshold": self.failure_threshold,
            "open_seconds": self.open_seconds,
            "consecutive_failures": self._consecutive_failures,
            "is_open": is_open,
            "remaining_seconds": round(remaining, 1),
        }

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures


class RouteCircuitBreaker:
    """Sliding-window breaker for one model/flow route.

    A half-open route admits exactly one probe. Account authentication failures
    are filtered by the caller and therefore never reach this object.
    """

    def __init__(
        self,
        *,
        window_seconds: float = 30.0,
        min_samples: int = 5,
        failure_rate: float = 0.6,
        open_seconds: float = 30.0,
    ) -> None:
        self.window_seconds = max(1.0, window_seconds)
        self.min_samples = max(1, min_samples)
        self.failure_rate = min(1.0, max(0.01, failure_rate))
        self.open_seconds = max(0.1, open_seconds)
        self._events: Deque[Tuple[float, bool]] = deque()
        self._state = "closed"
        self._opened_at: Optional[float] = None
        self._current_open_seconds = self.open_seconds
        self._backoff_level = 0
        self._max_open_seconds = max(
            self.open_seconds,
            _env_float("ROUTE_BREAKER_MAX_OPEN_SECONDS", 300.0),
        )
        self._probe_in_flight = False

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._events and self._events[0][0] <= cutoff:
            self._events.popleft()

    def allow(self, *, now: Optional[float] = None) -> Tuple[bool, float]:
        moment = time.monotonic() if now is None else now
        if self._state == "open":
            remaining = self._current_open_seconds - (moment - (self._opened_at or moment))
            if remaining > 0:
                return False, remaining
            self._state = "half-open"
            self._probe_in_flight = False
        if self._state == "half-open":
            if self._probe_in_flight:
                return False, self._current_open_seconds
            self._probe_in_flight = True
        return True, 0.0

    def record(
        self,
        success: bool,
        *,
        now: Optional[float] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        moment = time.monotonic() if now is None else now

        def _next_open_seconds() -> float:
            self._backoff_level += 1
            base = min(
                self._max_open_seconds,
                self.open_seconds * (2 ** max(0, self._backoff_level - 1)),
            )
            # Keep the first open deterministic; subsequent opens use jitter to
            # avoid synchronized workers probing a recovering route together.
            jitter = 1.0 if self._backoff_level == 1 else random.uniform(0.8, 1.2)
            return min(self._max_open_seconds, max(self.open_seconds, base * jitter, float(retry_after or 0.0)))

        if self._state == "half-open":
            self._probe_in_flight = False
            if success:
                self._state = "closed"
                self._opened_at = None
                self._current_open_seconds = self.open_seconds
                self._backoff_level = 0
                self._events.clear()
            else:
                self._state = "open"
                self._opened_at = moment
                self._current_open_seconds = _next_open_seconds()
            return
        if self._state == "open":
            return
        self._events.append((moment, success))
        self._prune(moment)
        if len(self._events) < self.min_samples:
            return
        failures = sum(1 for _, ok in self._events if not ok)
        if failures / len(self._events) >= self.failure_rate:
            self._state = "open"
            self._opened_at = moment
            self._current_open_seconds = _next_open_seconds()
            logger.warning(
                "route circuit opened: failures=%s samples=%s open_seconds=%s",
                failures,
                len(self._events),
                self.open_seconds,
            )

    def reset(self) -> None:
        self._events.clear()
        self._state = "closed"
        self._opened_at = None
        self._current_open_seconds = self.open_seconds
        self._backoff_level = 0
        self._probe_in_flight = False

    def snapshot(self) -> Dict[str, object]:
        now = time.monotonic()
        self._prune(now)
        failures = sum(1 for _, ok in self._events if not ok)
        remaining = 0.0
        if self._state == "open" and self._opened_at is not None:
            remaining = max(0.0, self._current_open_seconds - (now - self._opened_at))
        return {
            "state": self._state,
            "is_open": self._state == "open",
            "is_half_open": self._state == "half-open",
            "samples": len(self._events),
            "failures": failures,
            "failure_rate": round(failures / len(self._events), 3) if self._events else 0.0,
            "remaining_seconds": round(remaining, 1),
        }


class RouteCircuitRegistry:
    def __init__(self) -> None:
        self._breakers: Dict[str, RouteCircuitBreaker] = {}
        self.window_seconds = _env_float("ROUTE_BREAKER_WINDOW_SECONDS", 30.0)
        self.min_samples = _env_int("ROUTE_BREAKER_MIN_SAMPLES", 5)
        self.failure_rate = _env_float("ROUTE_BREAKER_FAILURE_RATE", 0.6)
        self.open_seconds = _env_float("ROUTE_BREAKER_OPEN_SECONDS", 30.0)
        self._loaded = False

    def _get(self, key: str) -> RouteCircuitBreaker:
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = RouteCircuitBreaker(
                window_seconds=self.window_seconds,
                min_samples=self.min_samples,
                failure_rate=self.failure_rate,
                open_seconds=self.open_seconds,
            )
            self._breakers[key] = breaker
        return breaker

    async def load_persisted(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            from sqlalchemy import select
            from app.models.database import RouteHealth, get_session_factory

            factory = get_session_factory()
            async with factory() as session:
                result = await session.execute(
                    select(RouteHealth).where(RouteHealth.state == "open")
                )
                now = utcnow_naive()
                for health in result.scalars().all():
                    if health.retry_after_at is None:
                        continue
                    remaining = (health.retry_after_at - now).total_seconds()
                    if remaining <= 0:
                        continue
                    breaker = self._get(health.route_key)
                    breaker._state = "open"
                    breaker._opened_at = time.monotonic()
                    breaker._current_open_seconds = remaining
        except Exception as exc:
            logger.warning("load persisted route health failed: %s", exc)

    def allow(self, key: str) -> Tuple[bool, float]:
        return self._get(key).allow()

    def record(self, key: str, success: bool, retry_after: Optional[float] = None) -> None:
        breaker = self._get(key)
        before = breaker.snapshot()["state"]
        breaker.record(success, retry_after=retry_after)
        after_snapshot = breaker.snapshot()
        if before != after_snapshot["state"]:
            self._persist_state_in_background(key, after_snapshot)

    def reset(self, key: Optional[str] = None) -> None:
        if key is None:
            for route_key, breaker in self._breakers.items():
                breaker.reset()
                self._persist_state_in_background(route_key, breaker.snapshot())
        elif key in self._breakers:
            self._breakers[key].reset()
            self._persist_state_in_background(key, self._breakers[key].snapshot())

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        return {key: breaker.snapshot() for key, breaker in self._breakers.items()}

    def _persist_state_in_background(self, key: str, snapshot: Dict[str, object]) -> None:
        if not key:
            return
        try:
            from app.models.database import get_session_factory

            get_session_factory()
            loop = asyncio.get_running_loop()
        except (RuntimeError, AttributeError):
            return
        loop.create_task(self._persist_state(key, snapshot))

    async def _persist_state(self, key: str, snapshot: Dict[str, object]) -> None:
        try:
            from datetime import timedelta
            from app.models.database import RouteHealth, get_session_factory

            factory = get_session_factory()
            async with factory() as session:
                health = await session.get(RouteHealth, key)
                if health is None:
                    health = RouteHealth(route_key=key)
                    session.add(health)
                health.state = str(snapshot.get("state") or "closed")
                health.failure_count = int(snapshot.get("failures") or 0)
                remaining = float(snapshot.get("remaining_seconds") or 0.0)
                health.opened_at = utcnow_naive() if health.state == "open" else None
                health.retry_after_at = (
                    utcnow_naive() + timedelta(seconds=remaining) if remaining > 0 else None
                )
                await session.commit()
        except Exception as exc:
            logger.warning("persist route health failed: route=%s error=%s", key[:80], exc)


class GenerationAdmission:
    """进程内即时准入闸门；满载直接拒绝，不创建服务端等待队列。"""

    def __init__(self, total_slots: int) -> None:
        self.total_slots = max(0, int(total_slots))
        self._lock = asyncio.Lock()
        self._model_in_flight: Dict[str, int] = {}
        self._in_flight = 0
        self._rejected = 0

    @property
    def max_concurrent(self) -> int:
        return self.total_slots

    @property
    def enabled(self) -> bool:
        return self.total_slots > 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def rejected(self) -> int:
        return self._rejected

    @staticmethod
    def _model_key(model: str) -> str:
        return str(model or "unknown")

    async def try_acquire(self, model: str = "unknown") -> bool:
        """在极短锁区内更新计数；容量不足立即返回 False。"""
        key = self._model_key(model)
        async with self._lock:
            if self.enabled and self._in_flight >= self.total_slots:
                self._rejected += 1
                return False
            self._in_flight += 1
            self._model_in_flight[key] = self._model_in_flight.get(key, 0) + 1
            return True

    def release(self, model: str = "unknown") -> None:
        key = self._model_key(model)
        self._in_flight = max(0, self._in_flight - 1)
        if key in self._model_in_flight:
            self._model_in_flight[key] = max(0, self._model_in_flight[key] - 1)
            if self._model_in_flight[key] == 0:
                self._model_in_flight.pop(key, None)

    def snapshot(self) -> Dict[str, object]:
        return {
            "enabled": self.enabled,
            "total_slots": self.total_slots,
            "max_concurrent": self.max_concurrent,
            "in_flight": self._in_flight,
            "rejected": self._rejected,
            "models": {
                key: {
                    "in_flight": self._model_in_flight.get(key, 0),
                }
                for key in sorted(self._model_in_flight)
            },
        }


class LoginThrottle:
    """管理员登录防爆破：同一用户名连续失败 N 次后锁定 M 秒。"""

    MAX_TRACKED_KEYS = 10_000

    def __init__(self, max_failures: int, lockout_seconds: float) -> None:
        self.max_failures = max(0, max_failures)
        self.lockout_seconds = lockout_seconds
        self._failures: Dict[str, Tuple[int, float]] = {}  # username -> (次数, 首次失败时刻)

    @property
    def enabled(self) -> bool:
        return self.max_failures > 0

    def check_locked(self, username: str, *, now: Optional[float] = None) -> float:
        """返回剩余锁定秒数；0 表示未锁定。"""
        if not self.enabled:
            return 0.0
        entry = self._failures.get(username)
        if entry is None:
            return 0.0
        count, first_at = entry
        moment = now if now is not None else time.monotonic()
        elapsed = moment - first_at
        # 锁定窗口：达到失败次数后，从首次失败起 lockout_seconds 内拒绝登录
        if count >= self.max_failures and elapsed < self.lockout_seconds:
            return self.lockout_seconds - elapsed
        if elapsed >= self.lockout_seconds:
            self._failures.pop(username, None)
        return 0.0

    def record_failure(self, username: str, *, now: Optional[float] = None) -> None:
        if not self.enabled:
            return
        moment = now if now is not None else time.monotonic()
        entry = self._failures.get(username)
        if entry is None and len(self._failures) >= self.MAX_TRACKED_KEYS:
            expired = [
                key
                for key, (_count, first_at) in self._failures.items()
                if moment - first_at >= self.lockout_seconds
            ]
            for key in expired:
                self._failures.pop(key, None)
            # Random usernames must not turn the throttle itself into an
            # unbounded-memory denial of service.
            while len(self._failures) >= self.MAX_TRACKED_KEYS:
                self._failures.pop(next(iter(self._failures)))
        if entry is None or moment - entry[1] >= self.lockout_seconds:
            self._failures[username] = (1, moment)
        else:
            self._failures[username] = (entry[0] + 1, entry[1])

    def record_success(self, username: str) -> None:
        self._failures.pop(username, None)


class GenerationGuard:
    """生图入口守卫：全局并发 + 每用户 RPM + 上游熔断的统一门面。"""

    def __init__(self) -> None:
        self._rpm_rejected = 0
        self.generation_admission = GenerationAdmission(
            _env_int("GENERATION_GLOBAL_MAX_CONCURRENT", 90),
        )
        self.user_rpm = SlidingWindowRateLimiter(
            _env_int("USER_RPM_LIMIT", 8),
            60.0,
        )
        self.upstream_breaker = CircuitBreaker(
            _env_int("CIRCUIT_BREAKER_FAILURES", 6),
            _env_float("CIRCUIT_BREAKER_OPEN_SECONDS", 30.0),
        )
        self.route_breakers = RouteCircuitRegistry()

    async def check_user_rate(self, user_key: str) -> float:
        """返回 0 表示放行；>0 为建议的重试等待秒数。"""
        allowed, retry_after = await self.user_rpm.check(user_key)
        if allowed:
            return 0.0
        self._rpm_rejected += 1
        return max(retry_after, 1.0)

    async def load_persisted_route_health(self) -> None:
        await self.route_breakers.load_persisted()

    @property
    def rpm_rejected(self) -> int:
        return self._rpm_rejected

    def status_snapshot(self) -> Dict[str, object]:
        """管理接口用的整体运行状态快照。"""
        admission = self.generation_admission.snapshot()
        return {
            "generation_admission": admission,
            "circuit_breaker": self.upstream_breaker.snapshot(),
            "route_breakers": self.route_breakers.snapshot(),
            "user_rpm": {
                "enabled": self.user_rpm.enabled,
                "limit": self.user_rpm.limit,
                "rejected": self._rpm_rejected,
            },
        }

    def check_upstream(self, route_key: Optional[str] = None) -> float:
        """返回 0 表示放行；>0 为断路剩余秒数。"""
        if route_key:
            allowed, remaining = self.route_breakers.allow(route_key)
            return 0.0 if allowed else remaining
        allowed, remaining = self.upstream_breaker.allow()
        return 0.0 if allowed else remaining

    def record_upstream_success(self, route_key: Optional[str] = None) -> None:
        if route_key:
            self.route_breakers.record(route_key, True)
            return
        self.upstream_breaker.record_success()

    def record_upstream_failure(
        self,
        route_key: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        if route_key:
            self.route_breakers.record(route_key, False, retry_after=retry_after)
            return
        self.upstream_breaker.record_failure()


_guard: Optional[GenerationGuard] = None


def get_generation_guard() -> GenerationGuard:
    global _guard
    if _guard is None:
        _guard = GenerationGuard()
    return _guard


_login_throttle: Optional[LoginThrottle] = None


def get_login_throttle() -> LoginThrottle:
    global _login_throttle
    if _login_throttle is None:
        _login_throttle = LoginThrottle(
            _env_int("ADMIN_LOGIN_MAX_FAILURES", 5),
            _env_float("ADMIN_LOGIN_LOCKOUT_SECONDS", 60.0),
        )
    return _login_throttle


_user_login_throttle: Optional[LoginThrottle] = None


def get_user_login_throttle() -> LoginThrottle:
    """Failure throttle shared by ordinary-user login identities and IPs."""
    global _user_login_throttle
    if _user_login_throttle is None:
        _user_login_throttle = LoginThrottle(
            _env_int("USER_LOGIN_MAX_FAILURES", 5),
            _env_float("USER_LOGIN_LOCKOUT_SECONDS", 60.0),
        )
    return _user_login_throttle
