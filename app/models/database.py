"""SQLAlchemy 模型 + SQLite 异步连接管理。

项目使用 SQLAlchemy 管理账号、用户、健康状态、会话、设置和生成日志等持久化表。
"""
from __future__ import annotations

from app.time_utils import utcnow_naive

import os
import logging
import json
from datetime import datetime, timedelta
from typing import AsyncGenerator, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    Index,
    String,
    Text,
    case,
    event,
    text,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.services.failure_classifier import classify_dashboard_failure, dashboard_failure_code


logger = logging.getLogger(__name__)
Base = declarative_base()


def _utcnow() -> datetime:
    return utcnow_naive()


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///./data/image_gen.db",
)
DB_POOL_SIZE = max(1, int(os.getenv("DB_POOL_SIZE", "4")))
DB_MAX_OVERFLOW = max(0, int(os.getenv("DB_MAX_OVERFLOW", "0")))
DB_POOL_TIMEOUT_SECONDS = max(1.0, float(os.getenv("DB_POOL_TIMEOUT_SECONDS", "3")))


class Account(Base):
    """ST 账号；同一套工作流模板下所有账号共享 in-* 输入约定。"""

    __tablename__ = "accounts"

    id = Column(String(36), primary_key=True)
    name = Column(String(255), nullable=False)
    org_id = Column(String(255), nullable=False)
    flow_id = Column(String(255), nullable=False)
    api_key_encrypted = Column(Text, nullable=False)
    # Private API Key（ST 控制台 → API Keys 里的 Private 类型），
    # 仅用于调用 /analytics 接口拉取运行详情中的 Errors 字段。
    # 与 inference 用的 Public Key 权限隔离，缺省可空。
    private_api_key_encrypted = Column(Text, nullable=True)
    status = Column(String(20), default="active", nullable=False)  # active / disabled
    # Historical columns retained for existing SQLite databases. Account-level
    # daily quotas are no longer a runtime feature; capacity is controlled by
    # max_inflight and the process-local admission gate.
    daily_quota = Column(Integer, default=0, nullable=False)
    daily_used = Column(Integer, default=0, nullable=False)
    total_requests = Column(Integer, default=0, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    # 实时在途数只保存在单 worker 进程内；数据库仅保存账号并发上限。
    max_inflight = Column(Integer, default=10, nullable=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class AccountHealth(Base):
    """Persisted account isolation state; transient failure counters stay in memory."""

    __tablename__ = "account_health"

    account_id = Column(String(36), primary_key=True)
    state = Column(String(20), nullable=False, default="healthy")
    reason = Column(Text, nullable=True)
    last_failure_scope = Column(String(40), nullable=True)
    retry_after_at = Column(DateTime, nullable=True, index=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class RouteHealth(Base):
    """Persisted model/flow route breaker state."""

    __tablename__ = "route_health"

    route_key = Column(String(512), primary_key=True)
    state = Column(String(20), nullable=False, default="closed")
    failure_count = Column(Integer, nullable=False, default=0)
    window_started_at = Column(DateTime, nullable=True)
    opened_at = Column(DateTime, nullable=True, index=True)
    retry_after_at = Column(DateTime, nullable=True, index=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class Admin(Base):
    """管理员表"""

    __tablename__ = "admins"

    id = Column(String(36), primary_key=True)
    username = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    token_version = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)


class AdminAuditLog(Base):
    """管理员危险操作审计记录；detail_json 只保存脱敏后的摘要。"""

    __tablename__ = "admin_audit_logs"

    id = Column(String(36), primary_key=True)
    timestamp = Column(DateTime, default=_utcnow, nullable=False, index=True)
    admin_id = Column(String(36), nullable=True, index=True)
    admin_username = Column(String(255), nullable=True)
    action = Column(String(64), nullable=False, index=True)
    target_type = Column(String(64), nullable=True)
    target_id = Column(String(255), nullable=True)
    detail_json = Column(Text, nullable=True)
    success = Column(Boolean, nullable=False, default=True, index=True)


class InviteCode(Base):
    """邀请码：只存哈希，不存明文。"""

    __tablename__ = "invite_codes"

    id = Column(String(36), primary_key=True)
    code_hash = Column(String(64), unique=True, nullable=False, index=True)
    code_prefix = Column(String(32), nullable=False)
    code_suffix = Column(String(16), nullable=True)
    note = Column(String(255), nullable=True)
    max_uses = Column(Integer, default=1, nullable=False)
    used_count = Column(Integer, default=0, nullable=False)
    daily_quota = Column(Integer, default=10, nullable=False)
    max_inflight = Column(Integer, default=2, nullable=False)
    expires_at = Column(DateTime, nullable=True, index=True)
    revoked_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class User(Base):
    """普通用户：通过邀请码激活。"""

    __tablename__ = "users"

    id = Column(String(36), primary_key=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    status = Column(String(20), default="active", nullable=False)  # active / disabled
    auth_kind = Column(String(24), default="password", nullable=False)  # password / invite_guest
    invite_code_id = Column(String(36), nullable=True, index=True)
    daily_quota = Column(Integer, default=10, nullable=False)
    daily_used = Column(Integer, default=0, nullable=False)
    total_requests = Column(Integer, default=0, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True, index=True)
    in_flight = Column(Integer, default=0, nullable=False)
    max_inflight = Column(Integer, default=2, nullable=False)
    abnormal_failure_count = Column(Integer, default=0, nullable=False)
    disabled_until = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class UserSession(Base):
    """普通用户登录会话：数据库里只保存 token 哈希。"""

    __tablename__ = "user_sessions"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), nullable=False, index=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    ip_address = Column(String(255), nullable=True)
    user_agent = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    last_seen_at = Column(DateTime, default=_utcnow, nullable=False)
    revoked_at = Column(DateTime, nullable=True, index=True)


class AppSetting(Base):
    """运行时可变设置（管理后台维护，DB 覆盖 env 默认值）。"""

    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class GenerationLog(Base):
    """生成日志（轻量记录，便于排查与统计）。"""

    __tablename__ = "generation_logs"
    __table_args__ = (
        Index("ix_generation_logs_timestamp_status", "timestamp", "status"),
        Index("ix_generation_logs_timestamp_model", "timestamp", "model"),
    )

    id = Column(String(36), primary_key=True)
    timestamp = Column(DateTime, default=_utcnow, nullable=False, index=True)
    user_id = Column(String(36), nullable=True, index=True)
    account_id = Column(String(36), nullable=True, index=True)
    mode = Column(String(20), nullable=False)  # text2img / img2img
    model = Column(String(255), nullable=True)
    aspect_ratio = Column(String(50), nullable=True)
    resolution = Column(String(50), nullable=True)
    prompt_preview = Column(Text, nullable=True)
    image_url = Column(Text, nullable=True)
    output_preview = Column(Text, nullable=True)
    output_images = Column(Text, nullable=True)
    response_time_ms = Column(Integer, nullable=True)
    status = Column(String(20), nullable=False)  # success / error
    error_message = Column(Text, nullable=True)
    error_code = Column(String(64), nullable=True, index=True)
    failure_category = Column(String(32), nullable=True, index=True)
    is_stream = Column(Boolean, default=False, nullable=False)


@event.listens_for(GenerationLog, "before_insert")
def _populate_generation_failure_fields(_mapper, _connection, target: GenerationLog) -> None:
    """Persist stable failure metadata while retaining the raw redacted message."""
    if target.status == "success":
        target.error_code = None
        target.failure_category = None
        return
    category = target.failure_category or classify_dashboard_failure(target.error_message)
    target.failure_category = category
    target.error_code = target.error_code or dashboard_failure_code(category)


def is_abnormal_generation_failure(message: object) -> bool:
    """Failures caused by rejected/invalid generation requests, not infrastructure."""
    value = str(message or "").lower()
    if not value:
        return False
    infrastructure = ("timeout", "timed out", "connection", "network", "502", "503", "504", "gateway")
    rejected = (
        "error in node", "safety system", "safety", "色情", "违规", "not allowed",
        "invalid configuration", "no image data found", "failed to generate image", "error code: 400",
    )
    return not any(item in value for item in infrastructure) and any(item in value for item in rejected)


@event.listens_for(GenerationLog, "after_insert")
def _track_abnormal_user_failure(_mapper, connection, target: GenerationLog) -> None:
    """Atomically count abusive failures and impose a three-hour temporary ban."""
    if not target.user_id or target.status == "success" or not is_abnormal_generation_failure(target.error_message):
        return
    now = _utcnow()
    connection.execute(
        User.__table__.update()
        .where(User.id == target.user_id)
        .values(
            abnormal_failure_count=User.abnormal_failure_count + 1,
            disabled_until=case(
                (User.abnormal_failure_count + 1 > 3, now + timedelta(hours=3)),
                else_=User.disabled_until,
            ),
        )
    )


class GenerationCounter(Base):
    """永久累计的生成图片计数，不随生成日志清理而删除。"""

    __tablename__ = "generation_counters"

    key = Column(String(64), primary_key=True)
    value = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


# ---------- 引擎/会话工厂 ----------
_engine = None
_session_factory: Optional[async_sessionmaker] = None


def get_database_url() -> str:
    return os.getenv("DATABASE_URL", DATABASE_URL)


async def init_database() -> None:
    """初始化数据库引擎、会话工厂，并创建所有表。"""
    global _engine, _session_factory

    db_url = get_database_url()
    if "sqlite" not in db_url.lower():
        raise RuntimeError(
            "MVP 仅支持 SQLite，DATABASE_URL 形如 sqlite+aiosqlite:///./data/image_gen.db"
        )

    _engine = create_async_engine(
        db_url,
        echo=os.getenv("DEBUG", "false").lower() == "true",
        future=True,
        pool_pre_ping=True,
        # 某些 SQLAlchemy + aiosqlite 组合默认会退回 NullPool；
        # 显式指定异步 QueuePool，才能稳定接受 pool_size/max_overflow 参数。
        poolclass=AsyncAdaptedQueuePool,
        pool_size=DB_POOL_SIZE,
        max_overflow=DB_MAX_OVERFLOW,
        pool_timeout=DB_POOL_TIMEOUT_SECONDS,
        connect_args={"timeout": 60, "check_same_thread": False},
    )
    logger.info(
        "database engine initialized: pool_size=%s max_overflow=%s pool_timeout=%ss",
        DB_POOL_SIZE,
        DB_MAX_OVERFLOW,
        DB_POOL_TIMEOUT_SECONDS,
    )
    _session_factory = async_sessionmaker(
        _engine, class_=AsyncSession, expire_on_commit=False
    )

    # busy_timeout / synchronous 是连接级 PRAGMA，需对池里每个新连接生效
    @event.listens_for(_engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=10000")
            # WAL 下 synchronous=NORMAL：commit 不再逐次 fsync，写入吞吐显著提升；
            # 应用崩溃不丢已提交事务（仅掉电可能丢最后一批 WAL），对本项目可接受。
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 兼容已有库：补齐增量列（SQLite 加可空列是安全的）
        await _ensure_columns(conn)
        await _ensure_indexes(conn)
        await _ensure_generation_counter(conn)
        # journal_mode 是数据库级设置，设一次持久生效
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("UPDATE users SET in_flight = 0 WHERE in_flight <> 0"))


async def _ensure_columns(conn) -> None:
    """对老库做增量字段补齐。新增字段时在这里登记一次即可。"""
    pending = [
        ("accounts", "private_api_key_encrypted", "TEXT"),
        ("accounts", "max_inflight", "INTEGER NOT NULL DEFAULT 2"),
        ("admins", "token_version", "INTEGER NOT NULL DEFAULT 0"),
        ("invite_codes", "code_suffix", "VARCHAR(16)"),
        ("users", "expires_at", "DATETIME"),
        ("users", "auth_kind", "VARCHAR(24) NOT NULL DEFAULT 'password'"),
        ("users", "abnormal_failure_count", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "disabled_until", "DATETIME"),
        ("generation_logs", "user_id", "VARCHAR(36)"),
        ("generation_logs", "output_images", "TEXT"),
        ("generation_logs", "error_code", "VARCHAR(64)"),
        ("generation_logs", "failure_category", "VARCHAR(32)"),
    ]
    for table, column, coltype in pending:
        try:
            res = await conn.execute(text(f"PRAGMA table_info({table})"))
            cols = {row[1] for row in res.fetchall()}
            if column not in cols:
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                )
                logger.info("schema migrated: %s.%s added", table, column)
        except Exception as exc:
            logger.warning("schema migration skipped for %s.%s: %s", table, column, exc)


async def _ensure_indexes(conn) -> None:
    """Create performance indexes required by dashboard time-window queries."""
    statements = (
        "CREATE INDEX IF NOT EXISTS ix_generation_logs_timestamp_status "
        "ON generation_logs (timestamp, status)",
        "CREATE INDEX IF NOT EXISTS ix_generation_logs_timestamp_model "
        "ON generation_logs (timestamp, model)",
    )
    for statement in statements:
        await conn.execute(text(statement))


def _legacy_output_image_count(output_images: object, output_preview: object) -> int:
    """从旧日志中恢复成功输出图片数，供首次建立永久计数时回填。"""
    if output_images:
        raw = str(output_images).strip()
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, list):
            return len([item for item in parsed if str(item or "").strip()])
        if isinstance(parsed, dict):
            images = parsed.get("images")
            if isinstance(images, list):
                return len([item for item in images if str(item or "").strip()])
            if parsed.get("url"):
                return 1
        return 1
    return 1 if output_preview else 0


async def _ensure_generation_counter(conn) -> None:
    """为已有数据库初始化一次永久累计图片数。"""
    key = "total_generated_images"
    result = await conn.execute(
        text("SELECT value FROM generation_counters WHERE key = :key"),
        {"key": key},
    )
    if result.first() is not None:
        return

    rows = await conn.execute(
        text(
            "SELECT output_images, output_preview "
            "FROM generation_logs WHERE status = 'success'"
        )
    )
    total = sum(_legacy_output_image_count(row[0], row[1]) for row in rows.fetchall())
    await conn.execute(
        text(
            "INSERT OR IGNORE INTO generation_counters (key, value, updated_at) "
            "VALUES (:key, :value, :updated_at)"
        ),
        {"key": key, "value": total, "updated_at": _utcnow()},
    )
    logger.info("generation counter initialized: total_generated_images=%s", total)


async def close_database() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
    _session_factory = None


def get_session_factory() -> async_sessionmaker:
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入用的会话提供者。"""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except BaseException:
            try:
                if session.in_transaction():
                    await session.rollback()
            except Exception as exc:
                logger.warning("session rollback on dependency error failed: %s", exc)
            raise
        else:
            try:
                if session.in_transaction():
                    await session.rollback()
            except Exception as exc:
                logger.warning("session rollback on dependency exit failed: %s", exc)
