"""运行时设置（app_settings）与管理后台 settings 接口的校验逻辑测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import delete

from app.models import database as database_mod
from app.models.database import AppSetting, Base, GenerationCounter, GenerationLog
from app.services import app_settings
from app.services.generation_stats import (
    get_total_generated_images,
    increment_total_generated_images,
)
from app.routers.admin import (
    AppSettingsUpdateRequest,
    StorageCleanupRequest,
    _dir_stats,
    _purge_dir_files,
    _runtime_config_payload,
    _setting_item,
)


class AppSettingsServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "test.db"
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self._factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._orig_factory = database_mod._session_factory
        database_mod._session_factory = self._factory
        app_settings.clear_cache()

    async def asyncTearDown(self) -> None:
        database_mod._session_factory = self._orig_factory
        app_settings.clear_cache()
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def test_unset_key_returns_none_and_falls_back_to_default(self) -> None:
        self.assertIsNone(await app_settings.get_setting(app_settings.SETTING_GENERATED_IMAGE_RETENTION_DAYS))
        self.assertEqual(
            await app_settings.get_effective_float(
                app_settings.SETTING_GENERATED_IMAGE_RETENTION_DAYS, 7.0
            ),
            7.0,
        )

    async def test_set_and_clear_override(self) -> None:
        key = app_settings.SETTING_GENERATED_IMAGE_RETENTION_DAYS
        await app_settings.set_setting(key, "1")
        self.assertEqual(await app_settings.get_setting(key), "1")
        self.assertEqual(await app_settings.get_effective_float(key, 7.0), 1.0)

        # 清除覆盖后回退默认
        await app_settings.set_setting(key, None)
        self.assertIsNone(await app_settings.get_setting(key))
        self.assertEqual(await app_settings.get_effective_float(key, 7.0), 7.0)

        # DB 行也应被删除
        from sqlalchemy import select

        async with self._factory() as session:
            rows = (await session.execute(select(AppSetting))).scalars().all()
        self.assertEqual(rows, [])

    async def test_update_overwrites_existing_value(self) -> None:
        key = app_settings.SETTING_REFERENCE_UPLOAD_RETENTION_DAYS
        await app_settings.set_setting(key, "3")
        await app_settings.set_setting(key, "0.5")
        self.assertEqual(await app_settings.get_setting(key), "0.5")

    async def test_invalid_value_falls_back_to_default(self) -> None:
        key = app_settings.SETTING_GENERATED_IMAGE_RETENTION_DAYS
        await app_settings.set_setting(key, "not-a-number")
        self.assertEqual(await app_settings.get_effective_float(key, 7.0), 7.0)

    async def test_effective_float_clamps_to_minimum(self) -> None:
        key = app_settings.SETTING_GENERATED_IMAGE_RETENTION_DAYS
        await app_settings.set_setting(key, "-5")
        self.assertEqual(await app_settings.get_effective_float(key, 7.0), 0.0)


class DirStatsTests(unittest.TestCase):
    def test_counts_files_sizes_and_skips_tmp_and_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.png").write_bytes(b"x" * 100)
            (root / "b.jpg").write_bytes(b"x" * 50)
            (root / "c.png.tmp").write_bytes(b"x" * 999)  # 临时文件不计
            (root / "generated").mkdir()  # 子目录不计
            (root / "generated" / "d.png").write_bytes(b"x" * 10)

            stats = _dir_stats(root)

        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["size_bytes"], 150)

    def test_missing_directory_returns_zero(self) -> None:
        stats = _dir_stats(Path("/nonexistent-dir-xyz"))
        self.assertEqual(stats, {"count": 0, "size_bytes": 0})


class PurgeDirFilesTests(unittest.TestCase):
    def test_purges_files_keeps_subdirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.png").write_bytes(b"x")
            (root / "b.jpg").write_bytes(b"x")
            (root / "c.tmp").write_bytes(b"x")  # 临时文件也一并清掉
            (root / "generated").mkdir()
            (root / "generated" / "d.png").write_bytes(b"x")  # 子目录内容不动

            removed = _purge_dir_files(root)

            self.assertEqual(removed, 3)
            self.assertEqual(list(root.iterdir()), [root / "generated"])
            self.assertTrue((root / "generated" / "d.png").exists())

    def test_missing_directory_returns_zero(self) -> None:
        self.assertEqual(_purge_dir_files(Path("/nonexistent-dir-xyz")), 0)


class CleanupRequestValidationTests(unittest.TestCase):
    def test_requires_at_least_one_target(self) -> None:
        with self.assertRaises(ValidationError):
            StorageCleanupRequest(targets=[])

    def test_accepts_targets(self) -> None:
        req = StorageCleanupRequest(targets=["logs", "logs", "generated_images"])
        self.assertEqual(req.targets, ["logs", "logs", "generated_images"])


class SettingsValidationTests(unittest.TestCase):
    def test_setting_item_shapes(self) -> None:
        self.assertEqual(
            _setting_item(None, 7.0),
            {"value": 7.0, "default": 7.0, "overridden": False},
        )
        self.assertEqual(
            _setting_item("1", 7.0),
            {"value": 1.0, "default": 7.0, "overridden": True},
        )
        self.assertEqual(
            _setting_item("oops", 7.0),
            {"value": 7.0, "default": 7.0, "overridden": False},
        )

    def test_update_request_bounds(self) -> None:
        with self.assertRaises(ValidationError):
            AppSettingsUpdateRequest(generated_image_retention_days=-1)
        with self.assertRaises(ValidationError):
            AppSettingsUpdateRequest(generated_image_retention_days=99999)
        req = AppSettingsUpdateRequest(generated_image_retention_days=1, reference_upload_retention_days=None)
        provided = req.model_dump(exclude_unset=True)
        self.assertEqual(provided["generated_image_retention_days"], 1.0)
        self.assertIsNone(provided["reference_upload_retention_days"])

    def test_absent_fields_are_excluded(self) -> None:
        req = AppSettingsUpdateRequest(generated_image_retention_days=1)
        provided = req.model_dump(exclude_unset=True)
        self.assertNotIn("reference_upload_retention_days", provided)


class GenerationCounterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "test.db"
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self._factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def test_counter_is_incremented_and_survives_log_cleanup(self) -> None:
        async with self._factory() as session:
            await increment_total_generated_images(session, 3)
            await session.commit()
            session.add(
                GenerationLog(
                    id="log-1",
                    mode="text2img",
                    status="success",
                    output_preview="/uploads/generated/a.png",
                )
            )
            await session.commit()

            await session.execute(delete(GenerationLog))
            await session.commit()
            self.assertEqual(await get_total_generated_images(session), 3)

            counter = (
                await session.get(GenerationCounter, "total_generated_images")
            )
            self.assertIsNotNone(counter)
            self.assertEqual(counter.value, 3)


class RuntimeConfigPayloadTests(unittest.TestCase):
    def test_reports_live_capacity_policy_as_read_only_direct_rejection(self) -> None:
        guard = SimpleNamespace(
            generation_admission=SimpleNamespace(max_concurrent=32),
            user_rpm=SimpleNamespace(limit=12),
        )
        pool = SimpleNamespace(DEFAULT_MAX_INFLIGHT=10)
        client = SimpleNamespace(max_connections=40, max_keepalive_connections=40)

        with patch("app.routers.admin.get_stackai_client", return_value=client):
            payload = _runtime_config_payload(guard, pool)

        self.assertEqual(payload["generation"]["admission_mode"], "reject_when_full")
        self.assertEqual(payload["generation"]["busy_status_code"], 429)
        self.assertEqual(payload["generation"]["global_max_concurrent"], 32)
        self.assertEqual(payload["generation"]["account_default_max_inflight"], 10)
        self.assertEqual(payload["network"]["http_max_connections"], 40)


if __name__ == "__main__":
    unittest.main()
