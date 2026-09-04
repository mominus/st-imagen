"""流量守卫单元测试：滑动窗口限流、熔断、全局闸门、登录防爆破。"""
from __future__ import annotations

import asyncio
import unittest

from app.services.guard import (
    CircuitBreaker,
    GenerationAdmission,
    LoginThrottle,
    SlidingWindowRateLimiter,
)


class SlidingWindowRateLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_allows_within_limit_and_blocks_over_limit(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=3, window_seconds=60.0)
        base = 1000.0
        for i in range(3):
            allowed, _ = await limiter.check("u1", now=base + i)
            self.assertTrue(allowed)
        allowed, retry_after = await limiter.check("u1", now=base + 3)
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)

    async def test_window_slides_and_old_hits_expire(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60.0)
        base = 2000.0
        self.assertTrue((await limiter.check("u1", now=base))[0])
        self.assertTrue((await limiter.check("u1", now=base + 1))[0])
        self.assertFalse((await limiter.check("u1", now=base + 2))[0])
        # 窗口滑过最早的命中后应重新放行
        self.assertTrue((await limiter.check("u1", now=base + 60.1))[0])

    async def test_keys_are_isolated(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0)
        self.assertTrue((await limiter.check("a", now=1.0))[0])
        self.assertFalse((await limiter.check("a", now=1.1))[0])
        self.assertTrue((await limiter.check("b", now=1.1))[0])

    async def test_disabled_when_limit_zero(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=0, window_seconds=60.0)
        for _ in range(100):
            self.assertTrue((await limiter.check("u1", now=1.0))[0])


class CircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    def test_stays_closed_below_threshold(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3, open_seconds=30.0)
        breaker.record_failure(now=1.0)
        breaker.record_failure(now=2.0)
        self.assertEqual(breaker.allow(now=3.0), (True, 0.0))

    def test_opens_at_threshold_and_recovers(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3, open_seconds=30.0)
        breaker.record_failure(now=1.0)
        breaker.record_failure(now=2.0)
        breaker.record_failure(now=3.0)
        allowed, remaining = breaker.allow(now=4.0)
        self.assertFalse(allowed)
        self.assertGreater(remaining, 0)
        # 断路到期后放行（半开）
        self.assertEqual(breaker.allow(now=34.1), (True, 0.0))
        # 半开期间再失败，立即重新断路
        breaker.record_failure(now=34.2)
        self.assertFalse(breaker.allow(now=34.3)[0])

    def test_success_resets_counter(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3, open_seconds=30.0)
        breaker.record_failure(now=1.0)
        breaker.record_failure(now=2.0)
        breaker.record_success()
        breaker.record_failure(now=3.0)
        breaker.record_failure(now=4.0)
        # 成功复位后只累计了 2 次，未到阈值
        self.assertEqual(breaker.allow(now=5.0), (True, 0.0))

    def test_disabled_when_threshold_zero(self) -> None:
        breaker = CircuitBreaker(failure_threshold=0, open_seconds=30.0)
        for _ in range(10):
            breaker.record_failure(now=1.0)
        self.assertEqual(breaker.allow(now=2.0), (True, 0.0))


class LoginThrottleTests(unittest.TestCase):
    def test_locks_after_consecutive_failures(self) -> None:
        throttle = LoginThrottle(max_failures=3, lockout_seconds=60.0)
        self.assertEqual(throttle.check_locked("admin", now=1.0), 0.0)
        throttle.record_failure("admin", now=1.0)
        throttle.record_failure("admin", now=2.0)
        self.assertEqual(throttle.check_locked("admin", now=3.0), 0.0)
        throttle.record_failure("admin", now=3.0)
        remaining = throttle.check_locked("admin", now=4.0)
        self.assertGreater(remaining, 0)
        # 锁定窗口过后恢复
        self.assertEqual(throttle.check_locked("admin", now=61.5), 0.0)

    def test_success_clears_failures(self) -> None:
        throttle = LoginThrottle(max_failures=2, lockout_seconds=60.0)
        throttle.record_failure("admin", now=1.0)
        throttle.record_success("admin")
        throttle.record_failure("admin", now=2.0)
        self.assertEqual(throttle.check_locked("admin", now=3.0), 0.0)

    def test_random_identity_tracking_is_bounded(self) -> None:
        throttle = LoginThrottle(max_failures=2, lockout_seconds=60.0)
        throttle.MAX_TRACKED_KEYS = 3

        for index in range(10):
            throttle.record_failure(f"user-{index}", now=1.0)

        self.assertEqual(len(throttle._failures), 3)


class ImmediateAdmissionTests(unittest.IsolatedAsyncioTestCase):
  async def test_gate_limits_true_concurrency(self) -> None:
      gate = GenerationAdmission(total_slots=2)
      active = 0
      peak = 0

      async def worker() -> None:
          nonlocal active, peak
          if not await gate.try_acquire():
              return
          active += 1
          peak = max(peak, active)
          await asyncio.sleep(0.02)
          active -= 1
          gate.release()

      await asyncio.gather(*[worker() for _ in range(6)])
      self.assertLessEqual(peak, 2)
      self.assertEqual(gate.rejected, 4)


class GuardObservabilityTests(unittest.IsolatedAsyncioTestCase):
  async def test_gate_tracks_in_flight_and_rejections(self) -> None:
      gate = GenerationAdmission(total_slots=1)
      self.assertEqual(gate.in_flight, 0)
      self.assertTrue(await gate.try_acquire())
      self.assertEqual(gate.in_flight, 1)
      # 满载时第三个请求等待超时 → rejected 计数
      self.assertFalse(await gate.try_acquire())
      self.assertEqual(gate.rejected, 1)
      gate.release()
      self.assertEqual(gate.in_flight, 0)
      self.assertTrue(await gate.try_acquire())
      gate.release()

  async def test_guard_snapshot_shape_and_rpm_counter(self) -> None:
      from app.services.guard import GenerationGuard

      guard = GenerationGuard()
      snapshot = guard.status_snapshot()
      self.assertIn("generation_admission", snapshot)
      self.assertNotIn("admission", snapshot)
      self.assertIn("circuit_breaker", snapshot)
      self.assertIn("user_rpm", snapshot)
      for key in ("enabled", "total_slots", "max_concurrent", "in_flight", "rejected", "models"):
          self.assertIn(key, snapshot["generation_admission"])
      for key in ("enabled", "is_open", "consecutive_failures", "remaining_seconds"):
          self.assertIn(key, snapshot["circuit_breaker"])

      # RPM 拒绝计数：limit=1 连打两次
      guard.user_rpm = SlidingWindowRateLimiter(limit=1, window_seconds=60.0)
      self.assertEqual(await guard.check_user_rate("u1"), 0.0)
      self.assertGreater(await guard.check_user_rate("u1"), 0.0)
      self.assertEqual(guard.rpm_rejected, 1)
      self.assertEqual(guard.status_snapshot()["user_rpm"]["rejected"], 1)

  async def test_breaker_snapshot_reports_open_state(self) -> None:
      breaker = CircuitBreaker(failure_threshold=2, open_seconds=30.0)
      breaker.record_failure()
      breaker.record_failure()
      snap = breaker.snapshot()
      self.assertTrue(snap["is_open"])
      self.assertGreater(snap["remaining_seconds"], 0)
      breaker.reset()
      snap = breaker.snapshot()
      self.assertFalse(snap["is_open"])
      self.assertEqual(snap["consecutive_failures"], 0)


class IsolationSnapshotTests(unittest.TestCase):
  def test_snapshot_lists_active_isolations_only(self) -> None:
      import time as time_mod

      from app.services.account_pool import AccountPoolService

      pool = AccountPoolService()
      now = time_mod.monotonic()
      pool._account_isolations["acc-1"] = (now + 50.0, "invalid API key")
      pool._account_isolations["acc-expired"] = (now - 1.0, "old reason")

      snapshot = pool.isolation_snapshot()

      self.assertIn("acc-1", snapshot)
      self.assertNotIn("acc-expired", snapshot)
      self.assertEqual(snapshot["acc-1"]["reason"], "invalid API key")
      self.assertLessEqual(snapshot["acc-1"]["remaining_seconds"], 50.0)


if __name__ == "__main__":
    unittest.main()
