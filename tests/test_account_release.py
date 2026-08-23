"""release_account 合并统计（mark_used 参数）的行为测试：真实临时 SQLite 库验证。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import database as database_mod
from app.models.database import Account, Base
from app.services.account_pool import AccountPoolService


class ReleaseAccountMergeStatsTests(unittest.IsolatedAsyncioTestCase):
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
        now = datetime.utcnow()
        fields = dict(
            id="acc-1",
            name="t@test.local",
            org_id="org",
            flow_id="flow",
            api_key_encrypted="enc",
            status="active",
            in_flight=1,
            total_requests=10,
            daily_used=2,
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

    async def test_release_with_mark_used_success_accumulates_stats(self) -> None:
        account_id = await self._create_account()
        pool = AccountPoolService()

        await pool.release_account(None, account_id, mark_used=True)

        acc = await self._get_account(account_id)
        self.assertEqual(acc.in_flight, 0)
        self.assertEqual(acc.total_requests, 11)
        self.assertEqual(acc.daily_used, 3)  # 同日累加
        self.assertIsNotNone(acc.last_used_at)

    async def test_release_with_mark_used_failure_counts_request_not_daily(self) -> None:
        account_id = await self._create_account()
        pool = AccountPoolService()

        await pool.release_account(None, account_id, mark_used=False)

        acc = await self._get_account(account_id)
        self.assertEqual(acc.in_flight, 0)
        self.assertEqual(acc.total_requests, 11)
        self.assertEqual(acc.daily_used, 2)  # 失败不占日额度

    async def test_release_without_mark_used_only_decrements_in_flight(self) -> None:
        account_id = await self._create_account()
        pool = AccountPoolService()

        await pool.release_account(None, account_id)

        acc = await self._get_account(account_id)
        self.assertEqual(acc.in_flight, 0)
        self.assertEqual(acc.total_requests, 10)
        self.assertEqual(acc.daily_used, 2)

    async def test_release_resets_daily_used_on_new_day(self) -> None:
        yesterday = datetime.utcnow() - timedelta(days=1)
        account_id = await self._create_account(last_used_at=yesterday, daily_used=7)
        pool = AccountPoolService()

        await pool.release_account(None, account_id, mark_used=True)

        acc = await self._get_account(account_id)
        self.assertEqual(acc.daily_used, 1)  # 跨日重置

    async def test_release_never_goes_negative(self) -> None:
        account_id = await self._create_account(in_flight=0)
        pool = AccountPoolService()

        await pool.release_account(None, account_id, mark_used=True)

        acc = await self._get_account(account_id)
        self.assertEqual(acc.in_flight, 0)


if __name__ == "__main__":
    unittest.main()
