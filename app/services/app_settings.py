"""运行时设置：DB 持久化 + 进程内缓存（单 worker 前提下无一致性问题）。

设计：
- 管理后台通过 set_setting 写 DB 并同步刷新缓存；
- 业务侧用 get_effective_* 读取：DB 值优先，未设置时回退 env 默认；
- 值以字符串存储，由各取用方按需解析（float/int），解析失败回退默认值。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

from sqlalchemy import delete, select

from app.models.database import AppSetting

logger = logging.getLogger(__name__)

# 已知的设置键（集中在这一处登记）
SETTING_GENERATED_IMAGE_RETENTION_DAYS = "generated_image_retention_days"
SETTING_REFERENCE_UPLOAD_RETENTION_DAYS = "reference_upload_retention_days"
SETTING_LINUXDO_OAUTH_ENABLED = "linuxdo_oauth_enabled"

KNOWN_SETTING_KEYS = {
    SETTING_GENERATED_IMAGE_RETENTION_DAYS,
    SETTING_REFERENCE_UPLOAD_RETENTION_DAYS,
    SETTING_LINUXDO_OAUTH_ENABLED,
}

# None 表示「确认无 DB 覆盖」，与「键不存在」区分开后可直接命中缓存
_cache: Dict[str, Optional[str]] = {}
_cache_lock = asyncio.Lock()


def _get_session_factory():
    from app.models.database import get_session_factory  # 延迟导入避免循环

    return get_session_factory()


async def get_setting(key: str) -> Optional[str]:
    """读取 DB 覆盖值；未设置返回 None。进程缓存命中时零 DB 开销。"""
    async with _cache_lock:
        if key in _cache:
            return _cache[key]

    factory = _get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(select(AppSetting).where(AppSetting.key == key))
        ).scalars().first()
        value = row.value if row is not None else None

    async with _cache_lock:
        _cache[key] = value
    return value


async def set_setting(key: str, value: Optional[str]) -> None:
    """写入/清除一个设置：value 为 None 时删除该键（回退 env 默认）。"""
    factory = _get_session_factory()
    async with factory() as session:
        if value is None:
            await session.execute(delete(AppSetting).where(AppSetting.key == key))
        else:
            row = (
                await session.execute(select(AppSetting).where(AppSetting.key == key))
            ).scalars().first()
            if row is None:
                session.add(AppSetting(key=key, value=str(value)))
            else:
                row.value = str(value)
        await session.commit()

    async with _cache_lock:
        _cache[key] = value
    logger.info("app setting updated: %s = %s", key, value)


async def get_effective_float(key: str, default: float, *, minimum: float = 0.0) -> float:
    """DB 覆盖值解析为 float；未设置或解析失败回退 default。"""
    raw = await get_setting(key)
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("app setting %s=%r is not a valid float; fallback to %s", key, raw, default)
        return default
    return max(minimum, value)


async def get_effective_bool(key: str, default: bool) -> bool:
    """DB 覆盖值解析为 bool；未设置或解析失败回退 default。"""
    raw = await get_setting(key)
    if raw is None:
        return default
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    logger.warning("app setting %s=%r is not a valid bool; fallback to %s", key, raw, default)
    return default


def clear_cache() -> None:
    """清空进程缓存（测试用）。"""
    _cache.clear()
