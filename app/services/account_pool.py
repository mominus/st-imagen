"""账号池：CRUD + 简单轮询选号。

借鉴 st-api 的 AccountPoolService，但极度简化：
- 共用一套工作流模板：每个账号只需 org_id / flow_id / api_key。
- 选号策略：active 状态、按 last_used_at 升序（最久未使用优先）。
- 失败切换由调用方决定（看 /api/generate 的错误处理）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import asc, case, delete, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Account
from app.services.crypto import get_crypto_service


logger = logging.getLogger(__name__)


class NoAvailableAccountError(Exception):
    """账号池中没有 active 账号（配置问题）。"""
    pass


class NoCapacityError(Exception):
    """所有 active 账号都已达 in_flight 上限（瞬时过载）。"""


class AccountPoolService:
    # 串行选号，避免 N 路并发同时读到同一快照后都选同一号。
    # 锁只包括 SELECT、UPDATE in_flight+1、COMMIT，持锁时间预计 < 5ms，
    # 对吞吐影响微乎其微（上游单请求 ~20s，选号锁完全不是瓶颈）。
    _select_lock: asyncio.Lock = asyncio.Lock()
    # SQLite 同时只允许单写者。高并发收尾时，账号释放会集中写 accounts.in_flight，
    # 这里在应用层串行释放，避免少量请求因为瞬时写锁冲突留下残留计数。
    _release_lock: asyncio.Lock = asyncio.Lock()
    RELEASE_RETRY_ATTEMPTS = max(1, int(os.getenv("ACCOUNT_RELEASE_RETRY_ATTEMPTS", "8")))
    RELEASE_RETRY_BASE_DELAY_SECONDS = max(
        0.01,
        float(os.getenv("ACCOUNT_RELEASE_RETRY_BASE_DELAY_SECONDS", "0.05")),
    )
    _account_cooldowns: Dict[str, Tuple[float, str]] = {}

    # ---------- CRUD ----------
    @staticmethod
    def _normalize_api_key(api_key: str) -> str:
        """剥掉常见误粘内容：首尾空白、Bearer 前缀、引号。"""
        cleaned = (api_key or "").strip().strip('"').strip("'")
        if cleaned.lower().startswith("bearer "):
            cleaned = cleaned[7:].strip()
        return cleaned

    DEFAULT_DAILY_QUOTA = 1_000_000  # 全部账号统一额度（每日）

    async def create_account(
        self,
        session: AsyncSession,
        name: str,
        org_id: str,
        flow_id: str,
        api_key: str,
        daily_quota: Optional[int] = None,
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
        # 当前需求：所有账号固定 1M/day；外部即便传了别的值也忽略掉
        daily_quota = self.DEFAULT_DAILY_QUOTA
        account = Account(
            id=str(uuid.uuid4()),
            name=name.strip() or f"acct-{datetime.utcnow().strftime('%H%M%S')}",
            org_id=org_id.strip(),
            flow_id=flow_id.strip(),
            api_key_encrypted=encrypted,
            private_api_key_encrypted=encrypted_private,
            status="active",
            daily_quota=max(0, int(daily_quota or 0)),
            daily_used=0,
            total_requests=0,
            max_inflight=max(1, int(max_inflight)) if max_inflight is not None else 10,
            in_flight=0,
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
        daily_quota: Optional[int] = None,
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
        if daily_quota is not None:
            account.daily_quota = max(0, int(daily_quota))
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
    def _prune_expired_cooldowns(self, now_monotonic: Optional[float] = None) -> None:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        expired = [
            account_id
            for account_id, (cooldown_until, _) in self._account_cooldowns.items()
            if cooldown_until <= now
        ]
        for account_id in expired:
            self._account_cooldowns.pop(account_id, None)

    def mark_account_cooldown(
        self,
        account_id: str,
        *,
        seconds: float,
        reason: str,
    ) -> None:
        cooldown_seconds = max(0.0, float(seconds))
        if not account_id or cooldown_seconds <= 0:
            return
        cooldown_until = time.monotonic() + cooldown_seconds
        previous = self._account_cooldowns.get(account_id)
        if previous is not None and previous[0] > cooldown_until:
            return
        self._account_cooldowns[account_id] = (cooldown_until, reason)
        logger.warning(
            "account cooldown set: account=%s seconds=%.1f reason=%s",
            account_id,
            cooldown_seconds,
            reason,
        )

    def cooldown_snapshot(self) -> Dict[str, Dict[str, object]]:
        """当前冷却中的账号快照：{account_id: {"remaining_seconds": x, "reason": str}}。"""
        self._prune_expired_cooldowns()
        now = time.monotonic()
        snapshot: Dict[str, Dict[str, object]] = {}
        for account_id, (cooldown_until, reason) in self._account_cooldowns.items():
            remaining = max(0.0, cooldown_until - now)
            if remaining <= 0:
                continue
            snapshot[account_id] = {
                "remaining_seconds": round(remaining, 1),
                "reason": reason,
            }
        return snapshot

    async def select_account(
        self,
        session: AsyncSession,
        exclude_ids: Optional[List[str]] = None,
    ) -> Account:
        """原子选号：选负载最低且未超限的账号，同时把 in_flight +1。

        策略：
        - status='active'
        - in_flight < max_inflight（超限的账号不参与选号）
        - 排除已尝试过的 ID（用于失败切换）
        - 排序键：in_flight ASC（负载低优先）→ last_used_at ASC（轮转公平）→ created_at ASC

        返回的 Account 在调用方 try/finally 中必须调 release_account 释放，
        否则 in_flight 会遗留。进程重启时会重置（見 init_database）。
        """
        async with self._select_lock:
            now_monotonic = time.monotonic()
            self._prune_expired_cooldowns(now_monotonic)
            active_cooldowns = self._account_cooldowns
            stmt = (
                select(Account)
                .where(Account.status == "active")
                .where(Account.in_flight < Account.max_inflight)
                .order_by(
                    asc(Account.in_flight),
                    asc(Account.last_used_at),
                    asc(Account.created_at),
                )
            )
            if exclude_ids:
                stmt = stmt.where(~Account.id.in_(exclude_ids))

            result = await session.execute(stmt)
            candidates = list(result.scalars().all())

            chosen: Optional[Account] = None
            cooled_candidates = 0
            for acc in candidates:
                cooldown = active_cooldowns.get(acc.id)
                if cooldown is not None and cooldown[0] > now_monotonic:
                    cooled_candidates += 1
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
                raise NoCapacityError(
                    "所有账号均达 in_flight 上限或处于临时冷却中，请稍后重试"
                )

            # 原子 +1：用 SQL 表达式（避免 ORM 的 read-modify-write）。
            # 立即 commit，让后续并发请求能看到负载变化。
            await session.execute(
                update(Account)
                .where(Account.id == chosen.id)
                .values(in_flight=Account.in_flight + 1)
            )
            await session.commit()
            await session.refresh(chosen)
            return chosen

    async def release_account(
        self,
        session: AsyncSession,
        account_id: str,
        *,
        mark_used: Optional[bool] = None,
    ) -> None:
        """释放一个在途名额：in_flight -1（不会为负）。调用方应在 try/finally 中调。

        mark_used 不为 None 时，同一条 UPDATE 顺带完成使用统计
        （total_requests+1、跨日重置的 daily_used、last_used_at），
        替代原先独立的 mark_used()，减少热点路径上的写事务。

        关键点：本函数会被 SSE generator 的 finally 调用。当客户端断开导致 ASGI task
        被 cancel 时，原本在 finally 里的 await 会立即 raise CancelledError，
        导致 in_flight 泄漏。解决方案：
        1. 用独立 session（不依赖请求级 session 的生命周期）
        2. 用 asyncio.shield 包裹，即便外层被 cancel，内部 task 也会跑完
        """
        del session
        from app.models.database import get_session_factory  # 延迟导入避免循环

        now = datetime.utcnow()
        values: Dict[str, object] = {"in_flight": Account.in_flight - 1}
        if mark_used is not None:
            today_iso = now.date().isoformat()
            inc_daily = 1 if mark_used else 0
            # 跨日（含首次：last_used_at 为 NULL）→ daily_used 重置为 inc_daily
            daily_expr = case(
                (
                    func.coalesce(func.date(Account.last_used_at), "1970-01-01") != today_iso,
                    inc_daily,
                ),
                else_=Account.daily_used + inc_daily,
            )
            values.update(
                last_used_at=now,
                total_requests=Account.total_requests + 1,
                daily_used=daily_expr,
            )

        async def _do_release() -> None:
            factory = get_session_factory()
            async with self._release_lock:
                for attempt in range(1, self.RELEASE_RETRY_ATTEMPTS + 1):
                    async with factory() as inner:
                        try:
                            result = await inner.execute(
                                update(Account)
                                .where(Account.id == account_id)
                                .where(Account.in_flight > 0)
                                .values(**values)
                            )
                            await inner.commit()
                            updated = max(0, int(result.rowcount or 0))
                            if updated == 0:
                                logger.warning(
                                    "release_account(%s) updated 0 rows on attempt %s",
                                    account_id,
                                    attempt,
                                )
                            elif attempt > 1:
                                logger.info(
                                    "release_account(%s) succeeded after retry %s",
                                    account_id,
                                    attempt,
                                )
                            return
                        except OperationalError as exc:
                            try:
                                await inner.rollback()
                            except Exception:
                                logger.warning(
                                    "release_account(%s) rollback failed after OperationalError",
                                    account_id,
                                )
                            if attempt >= self.RELEASE_RETRY_ATTEMPTS:
                                logger.warning(
                                    "release_account(%s) failed after %s attempts: %s",
                                    account_id,
                                    attempt,
                                    exc,
                                )
                                return
                            await asyncio.sleep(
                                self.RELEASE_RETRY_BASE_DELAY_SECONDS * attempt
                            )
                        except Exception as exc:
                            try:
                                await inner.rollback()
                            except Exception:
                                logger.warning(
                                    "release_account(%s) rollback failed after error",
                                    account_id,
                                )
                            logger.warning("release_account(%s) commit failed: %s", account_id, exc)
                            return

        try:
            await asyncio.shield(_do_release())
        except asyncio.CancelledError:
            # 当前 task 被 cancel，但内部 _do_release 受 shield 保护仍会跑完。
            # 不 re-raise（generator finally 内 swallow 即可，避免遮蔽 GeneratorExit）。
            pass
        except Exception as exc:
            logger.warning("release_account(%s) outer failed: %s", account_id, exc)

    # 第二层流控：短排队。配合第一层的 in_flight 限制，
    # 让瞬时过载请求短暂等待而不是立刻 429。
    # 默认等待 30s：上游单图 ~20s，给一个完整释放周期 + buffer。
    # 太短（如 5s）几乎等不到 release；太长前端体验差。30s 是 latency / success 折中点。
    DEFAULT_WAIT_TIMEOUT = 30.0
    DEFAULT_POLL_INTERVAL = 0.25

    async def select_account_or_wait(
        self,
        session: AsyncSession,
        exclude_ids: Optional[List[str]] = None,
        *,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> Account:
        """选号，若所有账号 in_flight 打满则短暂轮询等待至有容量。

        - NoCapacityError：在 wait_timeout 秒内反复尝试，每次失败后 sleep poll_interval
        - NoAvailableAccountError（账号池配置问题）：不等待，直接抛
        - 仍超时未拿到 → 抛 NoCapacityError（让上层返 429）

        polling 设计选择：
        - 实现简单（无需 Condition / Event 广播）
        - 0.25s 间隔，5s 内最多 20 次 SELECT，SQLite 单进程毫秒级查询无压力
        - select_account 内部已有 _select_lock 串行，避免雷鸣群效应
        """
        deadline = time.monotonic() + wait_timeout
        attempts = 0
        while True:
            try:
                acc = await self.select_account(session, exclude_ids=exclude_ids)
                if attempts > 0:
                    logger.info(
                        "select_account succeeded after %d wait attempt(s)", attempts
                    )
                return acc
            except NoCapacityError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                attempts += 1
                # 让其他 task 有机会释放 in_flight
                await asyncio.sleep(min(poll_interval, remaining))

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
