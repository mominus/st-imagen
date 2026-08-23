"""进程内流量守卫：全局限流、每用户 RPM、上游熔断、管理员登录防爆破。

单 worker 部署（UVICORN_WORKERS=1）是这些进程内状态成立的前提；
所有阈值都可用环境变量调整，设为 0 表示关闭对应守卫。
"""
from __future__ import annotations

import asyncio
import logging
import os
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


class GlobalConcurrencyGate:
    """全局并发闸门：限制同时在途的生图请求数（信号量 + 快速等待）。"""

    def __init__(self, max_concurrent: int, wait_seconds: float) -> None:
        self.max_concurrent = max(0, max_concurrent)
        self.wait_seconds = wait_seconds
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._lock = asyncio.Lock()
        self._in_flight = 0  # 当前在途数（acquire 成功 +1 / release -1）
        self._rejected = 0  # 等待超时被拒的累计次数

    @property
    def enabled(self) -> bool:
        return self.max_concurrent > 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def rejected(self) -> int:
        return self._rejected

    async def _get_semaphore(self) -> asyncio.Semaphore:
        async with self._lock:
            if self._semaphore is None:
                self._semaphore = asyncio.Semaphore(self.max_concurrent)
            return self._semaphore

    async def acquire(self) -> bool:
        """尝试在 wait_seconds 内拿到全局槽；超时返回 False。"""
        if not self.enabled:
            return True
        sem = await self._get_semaphore()
        try:
            await asyncio.wait_for(sem.acquire(), timeout=self.wait_seconds)
        except asyncio.TimeoutError:
            self._rejected += 1
            return False
        self._in_flight += 1
        return True

    def release(self) -> None:
        if self.enabled and self._semaphore is not None:
            self._semaphore.release()
        self._in_flight = max(0, self._in_flight - 1)


class LoginThrottle:
    """管理员登录防爆破：同一用户名连续失败 N 次后锁定 M 秒。"""

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
        self.global_gate = GlobalConcurrencyGate(
            _env_int("GENERATION_GLOBAL_MAX_CONCURRENT", 64),
            _env_float("GENERATION_GLOBAL_WAIT_SECONDS", 5.0),
        )
        self.user_rpm = SlidingWindowRateLimiter(
            _env_int("USER_RPM_LIMIT", 12),
            60.0,
        )
        self.upstream_breaker = CircuitBreaker(
            _env_int("CIRCUIT_BREAKER_FAILURES", 6),
            _env_float("CIRCUIT_BREAKER_OPEN_SECONDS", 30.0),
        )

    async def check_user_rate(self, user_key: str) -> float:
        """返回 0 表示放行；>0 为建议的重试等待秒数。"""
        allowed, retry_after = await self.user_rpm.check(user_key)
        if allowed:
            return 0.0
        self._rpm_rejected += 1
        return max(retry_after, 1.0)

    @property
    def rpm_rejected(self) -> int:
        return self._rpm_rejected

    def status_snapshot(self) -> Dict[str, object]:
        """管理接口用的整体运行状态快照。"""
        return {
            "global_gate": {
                "enabled": self.global_gate.enabled,
                "max_concurrent": self.global_gate.max_concurrent,
                "in_flight": self.global_gate.in_flight,
                "rejected": self.global_gate.rejected,
            },
            "circuit_breaker": self.upstream_breaker.snapshot(),
            "user_rpm": {
                "enabled": self.user_rpm.enabled,
                "limit": self.user_rpm.limit,
                "rejected": self._rpm_rejected,
            },
        }

    def check_upstream(self) -> float:
        """返回 0 表示放行；>0 为断路剩余秒数。"""
        allowed, remaining = self.upstream_breaker.allow()
        return 0.0 if allowed else remaining

    def record_upstream_success(self) -> None:
        self.upstream_breaker.record_success()

    def record_upstream_failure(self) -> None:
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
