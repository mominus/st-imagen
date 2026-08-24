"""账号池：CRUD + 进程内并发准入选号。

借鉴 st-api 的 AccountPoolService，但极度简化：
- 共用一套工作流模板：每个账号只需 org_id / flow_id / api_key。
- 选号策略：跳过故障隔离/已满账号，优先在途数低、最久未使用的账号。
- 失败切换由调用方决定（看 /api/generate 的错误处理）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Account, AccountHealth
from app.services.crypto import get_crypto_service


logger = logging.getLogger(__name__)


class NoAvailableAccountError(Exception):
    """账号池中没有 active 账号（配置问题）。"""
    failure_scope = "account_pool"
    retry_after = None


class NoCapacityError(Exception):
    """所有 active 账号都已达 in_flight 上限（瞬时过载）。"""

    def __init__(self, message: str = "所有账号均达 in_flight 上限，请稍后重试", *, failure_scope: str = "account_capacity", retry_after: Optional[float] = None):
        super().__init__(message)
        self.failure_scope = failure_scope
        self.retry_after = retry_after


class AccountPoolService:
    # 单 worker 部署下，容量计数只保存在内存。锁只保护一次很短的
    # SELECT + counter update，不会持有 SQLite 写事务，也不会等待上游。
    _select_lock: asyncio.Lock = asyncio.Lock()
    DEFAULT_MAX_INFLIGHT = max(1, int(os.getenv("ACCOUNT_MAX_INFLIGHT", "2")))
    _account_isolations: Dict[str, Tuple[float, str]] = {}

    def __init__(self) -> None:
        try:
            self.DEFAULT_MAX_INFLIGHT = max(
                1, int(os.getenv("ACCOUNT_MAX_INFLIGHT", str(self.DEFAULT_MAX_INFLIGHT)))
            )
        except (TypeError, ValueError):
            pass
        self._health_loaded = False
        self._runtime_in_flight: Dict[str, int] = {}
        # Slot tokens are in-process idempotency tokens. They prevent a request's
        # cleanup paths from releasing the same account slot twice.
        self._runtime_tokens: Dict[str, str] = {}
        self._usage_tasks: set[asyncio.Task] = set()
        self._usage_persist_lock = asyncio.Lock()

    # ---------- CRUD ----------
    @staticmethod
    def _normalize_api_key(api_key: str) -> str:
        """剥掉常见误粘内容：首尾空白、Bearer 前缀、引号。"""
        cleaned = (api_key or "").strip().strip('"').strip("'")
        if cleaned.lower().startswith("bearer "):
            cleaned = cleaned[7:].strip()
        return cleaned

    async def create_account(
        self,
        session: AsyncSession,
        name: str,
        org_id: str,
        flow_id: str,
        api_key: str,
        private_api_key: Optional[str] = None,
        max_inflight: Optional[int] = None,
    ) -> Account:
        crypto = get_crypto_service()
        cleaned_key = self._normalize_api_key(api_key)
        if not cleaned_key:
            raise ValueError("api_key 不能为空")
        encrypted = crypto.encrypt(cleaned_key)
        cleaned_private = self._normalize_api_key(private_api_key or "")
        encrypted_private = crypto.encrypt(cleaned_private) if cleaned_private else None
        account = Account(
            id=str(uuid.uuid4()),
            name=name.strip() or f"acct-{datetime.utcnow().strftime('%H%M%S')}",
            org_id=org_id.strip(),
            flow_id=flow_id.strip(),
            api_key_encrypted=encrypted,
            private_api_key_encrypted=encrypted_private,
            status="active",
            # Keep legacy non-null columns populated for old SQLite schemas.
            daily_quota=0,
            daily_used=0,
            total_requests=0,
            max_inflight=max(1, int(max_inflight)) if max_inflight is not None else self.DEFAULT_MAX_INFLIGHT,
        )
        session.add(account)
        await session.flush()
        logger.info(f"Account created id={account.id} name={account.name}")
        return account

    async def list_accounts(self, session: AsyncSession) -> List[Account]:
        result = await session.execute(select(Account).order_by(Account.created_at.asc()))
        return list(result.scalars().all())

    async def get_account(self, session: AsyncSession, account_id: str) -> Optional[Account]:
        result = await session.execute(select(Account).where(Account.id == account_id))
        return result.scalar_one_or_none()

    async def update_account(
        self,
        session: AsyncSession,
        account_id: str,
        *,
        name: Optional[str] = None,
        org_id: Optional[str] = None,
        flow_id: Optional[str] = None,
        api_key: Optional[str] = None,
        status: Optional[str] = None,
        private_api_key: Optional[str] = None,
        max_inflight: Optional[int] = None,
    ) -> Optional[Account]:
        account = await self.get_account(session, account_id)
        if account is None:
            return None
        if name is not None:
            account.name = name.strip() or account.name
        if org_id is not None:
            account.org_id = org_id.strip() or account.org_id
        if flow_id is not None:
            account.flow_id = flow_id.strip() or account.flow_id
        if api_key is not None and api_key.strip():
            cleaned_key = self._normalize_api_key(api_key)
            if cleaned_key:
                account.api_key_encrypted = get_crypto_service().encrypt(cleaned_key)
        if private_api_key is not None:
            # 显式传入：空串视为清空，非空写入
            cleaned_private = self._normalize_api_key(private_api_key)
            if cleaned_private:
                account.private_api_key_encrypted = get_crypto_service().encrypt(cleaned_private)
            else:
                account.private_api_key_encrypted = None
        if status is not None and status in {"active", "disabled"}:
            account.status = status
        if max_inflight is not None:
            account.max_inflight = max(1, int(max_inflight))
        account.updated_at = datetime.utcnow()
        await session.flush()
        return account

    async def delete_account(self, session: AsyncSession, account_id: str) -> bool:
        account = await self.get_account(session, account_id)
        if account is None:
            return False
        await session.delete(account)
        return True

    async def update_all_accounts_status(
        self,
        session: AsyncSession,
        *,
        status: str,
    ) -> int:
        if status not in {"active", "disabled"}:
            raise ValueError("status 必须为 active 或 disabled")

        result = await session.execute(
            update(Account)
            .where(Account.status != status)
            .values(status=status, updated_at=datetime.utcnow())
        )
        await session.flush()
        return max(0, int(result.rowcount or 0))

    async def delete_all_accounts(self, session: AsyncSession) -> int:
        result = await session.execute(delete(Account))
        await session.flush()
        return max(0, int(result.rowcount or 0))

    # ---------- 选号 / 释放 ----------
    def _prune_expired_isolations(self, now_monotonic: Optional[float] = None) -> None:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        expired = [
            account_id
            for account_id, (isolated_until, _) in self._account_isolations.items()
            if isolated_until <= now
        ]
        for account_id in expired:
            self._account_isolations.pop(account_id, None)

    def mark_account_isolated(
        self,
        account_id: str,
        *,
        seconds: float,
        reason: str,
    ) -> None:
        isolation_seconds = max(0.0, float(seconds))
        if not account_id or isolation_seconds <= 0:
            return
        isolated_until = time.monotonic() + isolation_seconds
        previous = self._account_isolations.get(account_id)
        if previous is not None and previous[0] > isolated_until:
            return
        self._account_isolations[account_id] = (isolated_until, reason)
        self._persist_health_in_background(account_id, reason, isolation_seconds)
        logger.warning(
            "account failure isolation set: account=%s seconds=%.1f reason=%s",
            account_id,
            isolation_seconds,
            reason,
        )

    def clear_account_isolation(self, account_id: str) -> bool:
        normalized = str(account_id or "").strip()
        removed = self._account_isolations.pop(normalized, None) is not None
        if removed:
            self._persist_health_in_background(normalized, "manual recovery", 0.0)
        return removed

    def clear_all_account_isolations(self) -> int:
        ids = list(self._account_isolations)
        self._account_isolations.clear()
        if ids:
            for account_id in ids:
                self._persist_health_in_background(account_id, "manual recovery", 0.0)
        return len(ids)

    def _persist_health_in_background(self, account_id: str, reason: str, seconds: float) -> None:
        """Write only state transitions; per-request transient failures stay in memory."""
        try:
            from app.models.database import get_session_factory

            get_session_factory()
            loop = asyncio.get_running_loop()
        except (RuntimeError, AttributeError):
            return
        loop.create_task(self._persist_health(account_id, reason, seconds))

    async def _persist_health(self, account_id: str, reason: str, seconds: float) -> None:
        try:
            from app.models.database import get_session_factory

            factory = get_session_factory()
            async with factory() as session:
                health = await session.get(AccountHealth, account_id)
                if health is None:
                    health = AccountHealth(account_id=account_id)
                    session.add(health)
                health.state = "isolated" if seconds > 0 else "healthy"
                health.reason = str(reason or "")[:1000]
                health.last_failure_scope = "account" if seconds > 0 else None
                health.retry_after_at = (
                    datetime.utcnow() + timedelta(seconds=seconds) if seconds > 0 else None
                )
                await session.commit()
        except Exception as exc:
            logger.warning("persist account health failed: account=%s error=%s", account_id[:8], exc)

    async def _load_persisted_health(self, session: AsyncSession) -> None:
        if self._health_loaded:
            return
        try:
            del session
            from app.models.database import get_session_factory

            factory = get_session_factory()
            async with factory() as health_session:
                result = await health_session.execute(
                    select(AccountHealth).where(AccountHealth.state == "isolated")
                )
            now = datetime.utcnow()
            for health in result.scalars().all():
                if health.retry_after_at is None:
                    continue
                remaining = (health.retry_after_at - now).total_seconds()
                if remaining > 0:
                    self._account_isolations[health.account_id] = (
                        time.monotonic() + remaining,
                        health.reason or "persisted account isolation",
                    )
        except Exception as exc:
            logger.warning("load persisted account health failed: %s", exc)
        self._health_loaded = True

    def isolation_snapshot(self) -> Dict[str, Dict[str, object]]:
        """当前故障隔离账号快照。"""
        self._prune_expired_isolations()
        now = time.monotonic()
        snapshot: Dict[str, Dict[str, object]] = {}
        for account_id, (isolated_until, reason) in self._account_isolations.items():
            remaining = max(0.0, isolated_until - now)
            if remaining <= 0:
                continue
            snapshot[account_id] = {
                "remaining_seconds": round(remaining, 1),
                "reason": reason,
            }
        return snapshot

    def runtime_in_flight(self, account_id: str) -> int:
        return max(0, int(self._runtime_in_flight.get(str(account_id), 0)))

    async def select_account(
        self,
        session: AsyncSession,
        exclude_ids: Optional[List[str]] = None,
        *,
        reserve: bool = True,
    ) -> Account:
        """原子选号：选负载最低且未超限的账号，同时把 in_flight +1。

        策略：
        - status='active'
        - in_flight < max_inflight（超限的账号不参与选号）
        - 排除已尝试过的 ID（用于失败切换）
        - 排序键：in_flight ASC（负载低优先）→ last_used_at ASC（轮转公平）→ created_at ASC

        ``reserve=False`` 只做同样的可用性检查并返回候选账号，不增加
        ``in_flight``。默认返回的 Account 带有进程内属性 ``_slot_token``；
        调用方应在 finally 中把它传给 release_account，重复释放同一个
        token 是幂等的。
        """
        async with self._select_lock:
            await self._load_persisted_health(session)
            now_monotonic = time.monotonic()
            self._prune_expired_isolations(now_monotonic)
            active_isolations = self._account_isolations
            stmt = select(Account).where(Account.status == "active")
            if exclude_ids:
                stmt = stmt.where(~Account.id.in_(exclude_ids))

            result = await session.execute(stmt)
            candidates = list(result.scalars().all())
            candidates.sort(
                key=lambda acc: (
                    self.runtime_in_flight(acc.id),
                    acc.last_used_at or datetime.min,
                    acc.created_at or datetime.min,
                )
            )

            chosen: Optional[Account] = None
            for acc in candidates:
                isolation = active_isolations.get(acc.id)
                if isolation is not None and isolation[0] > now_monotonic:
                    continue
                if self.runtime_in_flight(acc.id) >= max(1, int(acc.max_inflight or 1)):
                    continue
                chosen = acc
                break

            if chosen is None:
                # 区分两种失败：账号池空 vs 账号池满
                any_active = await session.scalar(
                    select(func.count()).select_from(Account).where(Account.status == "active")
                )
                if not any_active:
                    raise NoAvailableAccountError(
                        "没有可用账号；请在管理后台启用至少一个 active 账号"
                    )
                isolation_remaining = min(
                    (
                        max(0.0, until - now_monotonic)
                        for account_id, (until, _reason) in active_isolations.items()
                        if until > now_monotonic
                        and account_id not in (exclude_ids or [])
                    ),
                    default=None,
                )
                if isolation_remaining is not None:
                    raise NoCapacityError(
                        "所有账号均处于故障隔离中，请稍后重试",
                        failure_scope="account_isolation",
                        retry_after=isolation_remaining,
                    )
                raise NoCapacityError("所有账号均达 in_flight 上限，请稍后重试")

            if not reserve:
                return chosen

            slot_token = str(uuid.uuid4())
            self._runtime_in_flight[chosen.id] = self.runtime_in_flight(chosen.id) + 1
            self._runtime_tokens[slot_token] = chosen.id
            # Close the read transaction promptly.  This is not a capacity
            # write; it prevents the request session from holding a pooled
            # SQLite connection after selection.
            if reserve and hasattr(session, "commit"):
                await session.commit()
            if reserve and hasattr(session, "refreshed") and hasattr(session, "refresh"):
                await session.refresh(chosen)
            chosen._slot_token = slot_token
            return chosen

    async def release_account(
        self,
        session: AsyncSession,
        account_id: str,
        *,
        count_request: Optional[bool] = None,
        slot_token: Optional[str] = None,
    ) -> None:
        """释放进程内账号槽位；统计写入不再更新实时容量字段。"""
        del session
        normalized_account_id = str(account_id or "").strip()
        if not slot_token:
            return
        token_account_id = self._runtime_tokens.pop(str(slot_token), None)
        if token_account_id != normalized_account_id:
            return

        current = self.runtime_in_flight(normalized_account_id)
        if current > 0:
            self._runtime_in_flight[normalized_account_id] = current - 1
        if self._runtime_in_flight.get(normalized_account_id) == 0:
            self._runtime_in_flight.pop(normalized_account_id, None)
        if count_request is not None:
            # Persisting aggregate usage is deliberately deferred so a SQLite
            # write cannot extend the generation critical section.
            self._schedule_usage_persistence(normalized_account_id)

    async def _record_account_usage(self, account_id: str) -> None:
        """Record aggregate account usage outside the generation critical path."""
        from app.models.database import get_session_factory

        async with self._usage_persist_lock:
            now = datetime.utcnow()
            try:
                factory = get_session_factory()
                async with factory() as inner:
                    await inner.execute(
                        update(Account)
                        .where(Account.id == account_id)
                        .values(
                            last_used_at=now,
                            total_requests=Account.total_requests + 1,
                        )
                    )
                    await inner.commit()
            except Exception as exc:
                logger.warning("record account usage failed: account=%s error=%s", account_id[:8], exc)

    def _schedule_usage_persistence(self, account_id: str) -> None:
        try:
            task = asyncio.create_task(
                self._record_account_usage(account_id)
            )
        except RuntimeError:
            return
        self._usage_tasks.add(task)
        task.add_done_callback(self._usage_tasks.discard)

    async def drain_usage_persistence(self, timeout: float = 3.0) -> None:
        tasks = [task for task in self._usage_tasks if not task.done()]
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=max(0.0, float(timeout)),
            )
        except asyncio.TimeoutError:
            logger.warning("account usage persistence drain timed out; pending=%s", len(tasks))

    # ---------- 解密 ----------
    @staticmethod
    def decrypt_api_key(account: Account) -> str:
        return get_crypto_service().decrypt(account.api_key_encrypted)

    @staticmethod
    def decrypt_private_api_key(account: Account) -> Optional[str]:
        """返回 Private API Key 明文；未配置或解密失败时返回 None。"""
        enc = getattr(account, "private_api_key_encrypted", None)
        if not enc:
            return None
        try:
            value = get_crypto_service().decrypt(enc)
            return value or None
        except Exception as exc:
            logger.warning("decrypt private_api_key failed for account=%s: %s", account.id[:8], exc)
            return None


_account_pool_service: Optional[AccountPoolService] = None


def get_account_pool_service() -> AccountPoolService:
    global _account_pool_service
    if _account_pool_service is None:
        _account_pool_service = AccountPoolService()
    return _account_pool_service
