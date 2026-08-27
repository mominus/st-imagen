"""release_account 运行时槽位与异步统计写入测试。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import database as database_mod
from app.models.database import Account, Base
from app.services.account_pool import AccountPoolService


class AccountUsagePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "test.db"
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self._factory = async_sessionmaker(self._engine, expire_on_commit=False)
        # release_account 内部延迟导入 get_session_factory，替换模块级工厂指向测试库
        self._orig_factory = database_mod._session_factory
        database_mod._session_factory = self._factory

    async def asyncTearDown(self) -> None:
        database_mod._session_factory = self._orig_factory
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def _create_account(self, **overrides) -> str:
        now = datetime.now(UTC).replace(tzinfo=None)
        fields = dict(
            id="acc-1",
            name="t@test.local",
            org_id="org",
            flow_id="flow",
            api_key_encrypted="enc",
            status="active",
            total_requests=10,
            daily_used=7,
            max_inflight=10,
            last_used_at=now,
            created_at=now,
        )
        fields.update(overrides)
        async with self._factory() as session:
            session.add(Account(**fields))
            await session.commit()
        return fields["id"]

    async def _get_account(self, account_id: str) -> Account:
        from sqlalchemy import select

        async with self._factory() as session:
            obj = (await session.execute(select(Account).where(Account.id == account_id))).scalars().first()
            return obj

    async def _release(self, pool: AccountPoolService, account_id: str, count_request=None) -> None:
        token = f"token-{account_id}"
        pool._runtime_in_flight[account_id] = 1
        pool._runtime_tokens[token] = account_id
        await pool.release_account(None, account_id, count_request=count_request, slot_token=token)
        await pool.drain_usage_persistence()

    async def test_release_with_request_record_accumulates_stats(self) -> None:
        account_id = await self._create_account()
        pool = AccountPoolService()

        await self._release(pool, account_id, count_request=True)

        acc = await self._get_account(account_id)
        self.assertEqual(acc.total_requests, 11)
        self.assertEqual(acc.daily_used, 7)  # legacy quota column is untouched
        self.assertIsNotNone(acc.last_used_at)

    async def test_release_with_failed_request_still_accumulates_stats(self) -> None:
        account_id = await self._create_account()
        pool = AccountPoolService()

        await self._release(pool, account_id, count_request=False)

        acc = await self._get_account(account_id)
        self.assertEqual(acc.total_requests, 11)
        self.assertEqual(acc.daily_used, 7)

    async def test_release_without_request_record_only_decrements_in_flight(self) -> None:
        account_id = await self._create_account()
        pool = AccountPoolService()

        await self._release(pool, account_id)

        acc = await self._get_account(account_id)
        self.assertEqual(acc.total_requests, 10)
        self.assertEqual(acc.daily_used, 7)

if __name__ == "__main__":
    unittest.main()
