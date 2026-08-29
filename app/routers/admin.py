"""管理后台 API：登录 / 改密 / 账号 CRUD / 简单统计。"""
from __future__ import annotations

from app.time_utils import utcnow_naive

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import database as database_mod
from app.models.database import (
    Account,
    AdminAuditLog,
    GenerationLog,
    InviteCode,
    User,
    get_session,
    get_session_factory,
)
from app.routers import generate as generate_mod
from app.services.account_pool import get_account_pool_service
from app.services.auth import (
    InvalidCredentialsError,
    get_auth_service,
)
from app.services.deps import require_admin
from app.services import app_settings
from app.services.generation_stats import get_total_generated_images
from app.services.dashboard_analytics import get_dashboard_analytics, normalize_failure_category
from app.services.failure_classifier import dashboard_failure_code
from app.services.guard import get_generation_guard, get_login_throttle
from app.services.st_client import STError, get_st_client
from app.services.upstream_redaction import redact_upstream_text
from app.services.user_auth import (
    InvalidPasswordError,
    InvalidUsernameError,
    UsernameTakenError,
    build_user_usage_snapshot,
    get_effective_user_status,
    get_user_auth_service,
    is_user_expired,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])


async def _record_admin_audit(
    payload: Optional[dict],
    *,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    detail: Optional[dict] = None,
    success: bool = True,
) -> None:
    """Persist a safe summary of an admin operation without blocking it.

    Audit writes use a short-lived independent session so runtime-only admin
    actions can be audited too. Callers must pass already-sanitized details;
    this helper deliberately has no access to request credentials or secrets.
    """
    try:
        factory = get_session_factory()
        async with factory() as session:
            session.add(
                AdminAuditLog(
                    id=str(uuid4()),
                    admin_id=str((payload or {}).get("sub") or "") or None,
                    admin_username=str((payload or {}).get("username") or "") or None,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    detail_json=(
                        json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
                        if detail is not None
                        else None
                    ),
                    success=bool(success),
                )
            )
            await session.commit()
    except Exception as exc:
        # Operations remain available if the audit store is temporarily
        # unavailable; the failure is still visible to server operators.
        logger.warning("admin audit write failed: action=%s error=%s", action, exc)


# ---------------- Schemas ----------------
class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    success: bool
    token: Optional[str] = None
    admin: Optional[dict] = None
    message: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=128)


class AppSettingsUpdateRequest(BaseModel):
    # 传数字 = 设置覆盖值；显式传 null = 清除覆盖（回退 env 默认）；不传 = 不改动
    generated_image_retention_days: Optional[float] = Field(default=None, ge=0, le=3650)
    reference_upload_retention_days: Optional[float] = Field(default=None, ge=0, le=3650)


class StorageCleanupRequest(BaseModel):
    targets: List[str] = Field(min_length=1)


class AccountCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    org_id: str = Field(min_length=1, max_length=255)
    flow_id: str = Field(min_length=1, max_length=255)
    api_key: str = Field(min_length=1)
    # 可选：ST Private API Key，仅用于失败时拉取运行详情里的 Errors 字段
    private_api_key: Optional[str] = None
    # 单账号并发上限，不传使用 ACCOUNT_MAX_INFLIGHT（生产默认 10）
    max_inflight: Optional[int] = Field(default=None, ge=1, le=200)


class AccountUpdateRequest(BaseModel):
    name: Optional[str] = None
    org_id: Optional[str] = None
    flow_id: Optional[str] = None
    api_key: Optional[str] = None  # 留空表示不更新
    status: Optional[str] = None  # active / disabled
    # None=不变；空串=清空；非空=覆写
    private_api_key: Optional[str] = None
    max_inflight: Optional[int] = Field(default=None, ge=1, le=200)


class AccountImportRequest(BaseModel):
    raw_json: str = Field(min_length=1, max_length=5_000_000)
    # 批量导入使用稳定的产品默认值，不受旧部署环境中遗留的 2 并发覆盖影响。
    max_inflight: int = Field(default=10, ge=1, le=200)


class AccountBulkStatusRequest(BaseModel):
    status: str = Field(pattern="^(active|disabled)$")


class InviteCodeCreateRequest(BaseModel):
    count: int = Field(default=10, ge=1, le=200)
    max_uses: int = Field(default=1, ge=1, le=1000)
    expires_in_days: Optional[int] = Field(default=30, ge=1, le=3650)
    note: Optional[str] = Field(default=None, max_length=255)
    daily_quota: Optional[int] = Field(default=10, ge=0, le=1_000_000)
    max_inflight: Optional[int] = Field(default=None, ge=1, le=100)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    status: str = Field(default="active", pattern="^(active|disabled)$")
    daily_quota: int = Field(default=10, ge=0, le=1_000_000)
    max_inflight: int = Field(default=2, ge=1, le=100)
    expires_at: Optional[datetime] = None


class UserBatchCreateRequest(BaseModel):
    count: int = Field(default=10, ge=1, le=200)
    status: str = Field(default="active", pattern="^(active|disabled)$")
    daily_quota: int = Field(default=10, ge=0, le=1_000_000)
    max_inflight: int = Field(default=2, ge=1, le=100)
    expires_at: Optional[datetime] = None


class UserUpdateRequest(BaseModel):
    status: Optional[str] = Field(default=None, pattern="^(active|disabled)$")
    daily_quota: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    max_inflight: Optional[int] = Field(default=None, ge=1, le=100)
    expires_at: Optional[datetime] = None
    new_password: Optional[str] = Field(default=None, min_length=8, max_length=128)


def _account_to_dict(acc: Account) -> dict:
    # 不返回密文，只返回脱敏占位符
    pool = get_account_pool_service()
    runtime_in_flight = pool.runtime_in_flight(acc.id)
    return {
        "id": acc.id,
        "name": acc.name,
        "org_id": acc.org_id,
        "flow_id": acc.flow_id,
        "api_key_masked": "sk-...****",
        # 不返回 Private API Key 明文，只告知是否已配置
        "private_api_key_set": bool(getattr(acc, "private_api_key_encrypted", None)),
        "status": acc.status,
        "total_requests": acc.total_requests,
        "in_flight": runtime_in_flight,
        "max_inflight": getattr(acc, "max_inflight", None) or pool.DEFAULT_MAX_INFLIGHT,
        "last_used_at": acc.last_used_at.isoformat() if acc.last_used_at else None,
        "created_at": acc.created_at.isoformat() if acc.created_at else None,
        "updated_at": acc.updated_at.isoformat() if acc.updated_at else None,
    }


def _strip_json_code_fence(text: str) -> str:
    raw = str(text or "").strip().lstrip("\ufeff").strip()
    if raw.startswith("```") and raw.endswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return raw


def _normalize_import_api_key(api_key: str) -> str:
    cleaned = (api_key or "").strip().strip('"').strip("'")
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()
    return cleaned


def _extract_import_rows(payload: Any) -> List[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "accounts", "data", "rows", "list", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        if any(
            key in payload
            for key in ("email", "public_api_key", "private_api_key", "org_id", "flow_id", "api_key", "name")
        ):
            return [payload]
    return []


def _get_first_text_value(item: Dict[str, Any], keys: List[str]) -> str:
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _invite_status(invite: InviteCode) -> str:
    now = utcnow_naive()
    if invite.revoked_at is not None:
        return "revoked"
    if invite.expires_at and invite.expires_at < now:
        return "expired"
    if invite.used_count >= invite.max_uses:
        return "exhausted"
    return "active"


def _invite_to_dict(invite: InviteCode, *, raw_code: Optional[str] = None) -> dict:
    return {
        "id": invite.id,
        "code_prefix": invite.code_prefix,
        "code_suffix": invite.code_suffix,
        "raw_code": raw_code,
        "note": invite.note,
        "max_uses": invite.max_uses,
        "used_count": invite.used_count,
        "remaining_uses": max(0, invite.max_uses - invite.used_count),
        "daily_quota": invite.daily_quota,
        "max_inflight": invite.max_inflight,
        "status": _invite_status(invite),
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
        "revoked_at": invite.revoked_at.isoformat() if invite.revoked_at else None,
        "created_at": invite.created_at.isoformat() if invite.created_at else None,
        "updated_at": invite.updated_at.isoformat() if invite.updated_at else None,
    }


def _user_to_dict(user: User) -> dict:
    current = utcnow_naive()
    usage = build_user_usage_snapshot(user)
    effective_status = get_effective_user_status(user, now=current)
    return {
        "id": user.id,
        "username": user.username,
        "status": user.status,
        "effective_status": effective_status,
        "is_expired": is_user_expired(user, now=current),
        "invite_code_id": user.invite_code_id,
        "daily_quota": user.daily_quota,
        "daily_used": usage["daily_used"],
        "total_requests": user.total_requests,
        "in_flight": usage["in_flight"],
        "quota_remaining": usage["quota_remaining"],
        "max_inflight": user.max_inflight,
        "last_used_at": user.last_used_at.isoformat() if user.last_used_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "expires_at": user.expires_at.isoformat() if user.expires_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
    }


# ---------------- Auth ----------------
@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    throttle = get_login_throttle()
    locked_remaining = throttle.check_locked(req.username)
    if locked_remaining > 0:
        raise HTTPException(
            status_code=429,
            detail={
                "message": f"失败次数过多，请 {int(locked_remaining) + 1} 秒后再试",
                "retry_after": int(locked_remaining) + 1,
            },
        )
    auth = get_auth_service()
    try:
        token, admin = await auth.authenticate(req.username, req.password)
    except InvalidCredentialsError:
        throttle.record_failure(req.username)
        return LoginResponse(success=False, message="用户名或密码错误")
    throttle.record_success(req.username)
    return LoginResponse(success=True, token=token, admin=admin)


@router.post("/change-password")
async def change_password(req: ChangePasswordRequest, payload=Depends(require_admin)):
    auth = get_auth_service()
    try:
        ok = await auth.change_password(payload["sub"], req.old_password, req.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=400, detail="旧密码不正确")
    return {"success": True}


@router.get("/me")
async def me(payload=Depends(require_admin)):
    return {"id": payload.get("sub"), "username": payload.get("username")}


# ---------------- App Settings ----------------
def _setting_item(raw: Optional[str], default: float) -> dict:
    overridden = raw is not None
    try:
        value = max(0.0, float(str(raw))) if overridden else default
    except (TypeError, ValueError):
        value, overridden = default, False
    return {"value": value, "default": default, "overridden": overridden}


def _dir_stats(directory: Path, *, skip_suffixes: tuple = (".tmp",)) -> dict:
    """目录内普通文件的数量与总大小（跳过子目录与临时文件）。"""
    count = 0
    total = 0
    if directory.exists():
        for path in directory.iterdir():
            if not path.is_file() or path.suffix.lower() in skip_suffixes:
                continue
            try:
                total += path.stat().st_size
                count += 1
            except OSError:
                continue
    return {"count": count, "size_bytes": total}


def _sqlite_storage_size() -> Optional[int]:
    """数据库主文件 + WAL 的磁盘占用；拿不到路径时返回 None。"""
    engine = getattr(database_mod, "_engine", None)
    db_path = getattr(engine.url, "database", None) if engine is not None else None
    if not db_path:
        return None
    total = 0
    for candidate in (Path(db_path), Path(f"{db_path}-wal")):
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


async def _storage_stats(session: AsyncSession) -> dict:
    log_count = (
        await session.execute(select(func.count(GenerationLog.id)))
    ).scalar() or 0
    disk = generate_mod._generated_disk_snapshot()
    return {
        "logs": {
            "count": int(log_count),
            "size_bytes": _sqlite_storage_size(),
        },
        "generated_images": await asyncio.to_thread(
            _dir_stats, generate_mod.GENERATED_IMAGE_DIR
        ),
        "reference_images": await asyncio.to_thread(
            _dir_stats, generate_mod.REFERENCE_UPLOAD_DIR
        ),
        "disk": {
            "free_bytes": disk["free_bytes"],
            "min_free_bytes": disk["min_free_bytes"],
            "writable_bytes": disk["writable_bytes"],
            "available": disk["can_write"],
        },
    }


async def _settings_payload(session: AsyncSession) -> dict:
    gen_raw = await app_settings.get_setting(app_settings.SETTING_GENERATED_IMAGE_RETENTION_DAYS)
    ref_raw = await app_settings.get_setting(app_settings.SETTING_REFERENCE_UPLOAD_RETENTION_DAYS)
    guard = get_generation_guard()
    pool = get_account_pool_service()
    return {
        "items": {
            "generated_image_retention_days": _setting_item(
                gen_raw, generate_mod.GENERATED_IMAGE_RETENTION_DAYS
            ),
            "reference_upload_retention_days": _setting_item(
                ref_raw, generate_mod.REFERENCE_UPLOAD_RETENTION_DAYS
            ),
        },
        "storage": await _storage_stats(session),
        # Concurrency is intentionally read-only here.  The process-local
        # admission counters are created at startup and changing them from the
        # console would make live capacity accounting inconsistent.
        "runtime_config": _runtime_config_payload(guard, pool),
    }


@router.get("/settings")
async def get_app_settings(
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    return await _settings_payload(session)


@router.put("/settings")
async def update_app_settings(
    req: AppSettingsUpdateRequest,
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    provided = req.model_dump(exclude_unset=True)
    if not provided:
        raise HTTPException(status_code=400, detail="没有可更新的设置项")

    field_to_key = {
        "generated_image_retention_days": app_settings.SETTING_GENERATED_IMAGE_RETENTION_DAYS,
        "reference_upload_retention_days": app_settings.SETTING_REFERENCE_UPLOAD_RETENTION_DAYS,
    }
    for field, value in provided.items():
        key = field_to_key.get(field)
        if key is None:
            raise HTTPException(status_code=400, detail=f"未知的设置项: {field}")
        # None = 清除覆盖回退默认；数字 = 写入覆盖
        await app_settings.set_setting(
            key, None if value is None else str(float(value))
        )

    # 立即按新保留期跑一次清理，跳过节流窗口
    try:
        await generate_mod._maybe_cleanup_uploads(force=True)
    except Exception as exc:
        logger.warning("post-settings cleanup failed: %s", exc)

    return await _settings_payload(session)


# ---------------- Storage Cleanup ----------------
CLEANUP_TARGETS = {"logs", "generated_images", "reference_images"}


@router.post("/settings/cleanup")
async def cleanup_storage(
    req: StorageCleanupRequest,
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    targets = list(dict.fromkeys(t.strip() for t in req.targets if t and t.strip()))
    unknown = [t for t in targets if t not in CLEANUP_TARGETS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知的清理目标: {', '.join(unknown)}")
    if not targets:
        raise HTTPException(status_code=400, detail="没有可清理的目标")

    removed: Dict[str, int] = {}
    if "logs" in targets:
        result = await session.execute(delete(GenerationLog))
        await session.commit()
        removed["logs"] = max(0, int(result.rowcount or 0))
    image_targets = {
        "generated": "generated_images" in targets,
        "reference": "reference_images" in targets,
    }
    if any(image_targets.values()):
        removed.update(
            await generate_mod._cleanup_uploads_by_retention(**image_targets)
        )

    logger.info("storage cleanup by admin: targets=%s removed=%s", targets, removed)
    result_payload = await _settings_payload(session)
    return {"removed": removed, **result_payload}


# ---------------- Runtime Status ----------------
def _safe_env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _runtime_config_payload(guard, pool) -> dict:
    """Return the effective, read-only process configuration for the console.

    Generation admission is intentionally process-local.  Showing these values
    from the live services (instead of duplicating .env defaults in the UI)
    prevents the admin console from drifting away from the running process.
    """
    client = get_st_client()
    worker_count = max(1, _safe_env_int("UVICORN_WORKERS", 1))
    admission = guard.generation_admission
    image_persistence = generate_mod.image_persistence_snapshot()
    return {
        "process": {
            "worker_count": worker_count,
            "single_worker_required": True,
            "single_worker_ok": worker_count == 1,
        },
        "generation": {
            "global_max_concurrent": admission.max_concurrent,
            "account_default_max_inflight": max(1, int(pool.DEFAULT_MAX_INFLIGHT)),
            "user_rpm_limit": guard.user_rpm.limit,
            "busy_status_code": 429,
            "admission_mode": "reject_when_full",
            "workflow_idle_timeout_seconds": generate_mod.GENERATE_STREAM_IDLE_TIMEOUT_SECONDS,
            "workflow_total_timeout_seconds": generate_mod.GENERATE_STREAM_TOTAL_TIMEOUT_SECONDS,
            "sse_keepalive_interval_seconds": generate_mod.GENERATE_STREAM_KEEPALIVE_INTERVAL_SECONDS,
        },
        "image_delivery": {
            "response_requires_local_persist": True,
            "upstream_image_url_exposed": False,
        },
        "network": {
            "http_max_connections": client.max_connections,
            "http_max_keepalive": client.max_keepalive_connections,
            "upstream_timeout_seconds": client.timeout,
            "upstream_connect_timeout_seconds": client.connect_timeout,
            "upstream_read_timeout_seconds": client.stream_read_timeout,
            "db_pool_size": database_mod.DB_POOL_SIZE,
            "db_max_overflow": database_mod.DB_MAX_OVERFLOW,
            "db_pool_timeout_seconds": database_mod.DB_POOL_TIMEOUT_SECONDS,
        },
        "persistence": {
            "image_download_concurrency": generate_mod.GENERATED_IMAGE_DOWNLOAD_CONCURRENCY,
            "image_download_timeout_seconds": generate_mod.GENERATED_IMAGE_TIMEOUT_SECONDS,
            "image_save_total_timeout_seconds": generate_mod.GENERATED_IMAGE_SAVE_TOTAL_TIMEOUT_SECONDS,
            "image_download_attempts": generate_mod.GENERATED_IMAGE_DOWNLOAD_ATTEMPTS,
            "image_retry_backoff_seconds": generate_mod.GENERATED_IMAGE_DOWNLOAD_RETRY_BACKOFF_SECONDS,
            "upload_cleanup_interval_seconds": generate_mod.UPLOAD_CLEANUP_INTERVAL_SECONDS,
            "history_log_async": True,
            "history_log_active_tasks": image_persistence["active_tasks"],
            "history_log_started": image_persistence["started"],
            "response_waits_for_local_file": True,
        },
    }

# ---------------- Runtime Metrics / Status ----------------
def _runtime_metrics_payload() -> dict:
    """Return only volatile in-process concurrency counters."""
    guard_snapshot = get_generation_guard().generation_admission.snapshot()
    pool = get_account_pool_service()
    return {
        "generation": {
            "in_flight": int(guard_snapshot["in_flight"]),
            "models": guard_snapshot["models"],
        },
        "account": {
            "in_flight": pool.runtime_total_in_flight(),
        },
    }


@router.get("/runtime-metrics")
async def runtime_metrics(payload=Depends(require_admin)):
    del payload
    return _runtime_metrics_payload()


async def _runtime_status_payload(session: Optional[AsyncSession] = None) -> dict:
    guard = get_generation_guard()
    pool = get_account_pool_service()

    isolation_map = pool.isolation_snapshot()
    isolation_items: List[Dict[str, object]] = []
    if isolation_map:
        # The normal 30-second diagnostic poll is in-memory. Only open a
        # short-lived DB session when there are isolated accounts and the UI
        # needs their display names.
        if session is not None:
            rows = (
                await session.execute(
                    select(Account.id, Account.name).where(Account.id.in_(list(isolation_map.keys())))
                )
            ).all()
        else:
            factory = get_session_factory()
            async with factory() as status_session:
                rows = (
                    await status_session.execute(
                        select(Account.id, Account.name).where(Account.id.in_(list(isolation_map.keys())))
                    )
                ).all()
        name_by_id = {row.id: row.name for row in rows}
        for account_id, info in isolation_map.items():
            isolation_items.append(
                {
                    "account_id": account_id,
                    "name": name_by_id.get(account_id, account_id[:8]),
                    "remaining_seconds": info["remaining_seconds"],
                    "reason": info["reason"],
                }
            )
    isolation_items.sort(key=lambda item: float(item["remaining_seconds"]), reverse=True)

    guard_snapshot = guard.status_snapshot()
    generation_snapshot = guard_snapshot.get("generation_admission", {})

    return {
        "guard": {
            "generation_admission": {
                "rejected": generation_snapshot.get("rejected", 0),
            },
            "circuit_breaker": guard_snapshot.get("circuit_breaker", {}),
            "route_breakers": guard_snapshot.get("route_breakers", {}),
            "user_rpm": guard_snapshot.get("user_rpm", {}),
        },
        "image_persistence": generate_mod.image_persistence_snapshot(),
        "account_isolations": isolation_items,
    }


@router.get("/runtime-status")
async def runtime_status(payload=Depends(require_admin)):
    del payload
    return await _runtime_status_payload()


@router.post("/circuit-breaker/reset")
async def reset_circuit_breaker(payload=Depends(require_admin)):
    breaker = get_generation_guard().upstream_breaker
    was_open = breaker.snapshot()["is_open"]
    try:
        breaker.reset()
    except Exception as exc:
        await _record_admin_audit(
            payload,
            action="reset_circuit_breaker",
            target_type="circuit_breaker",
            detail={"was_open": bool(was_open), "error": str(exc)[:240]},
            success=False,
        )
        raise
    logger.info("circuit breaker reset by admin (was_open=%s)", was_open)
    await _record_admin_audit(
        payload,
        action="reset_circuit_breaker",
        target_type="circuit_breaker",
        detail={"was_open": bool(was_open)},
    )
    return {"success": True, "was_open": bool(was_open), "circuit_breaker": breaker.snapshot()}


@router.post("/circuit-breaker/routes/reset")
async def reset_route_circuit_breakers(payload=Depends(require_admin)):
    guard = get_generation_guard()
    before = guard.route_breakers.snapshot()
    try:
        guard.route_breakers.reset()
    except Exception as exc:
        await _record_admin_audit(
            payload,
            action="reset_route_circuit_breakers",
            target_type="route_circuit_breakers",
            detail={"route_count": len(before), "error": str(exc)[:240]},
            success=False,
        )
        raise
    logger.info("route circuit breakers reset by admin: count=%s", len(before))
    await _record_admin_audit(
        payload,
        action="reset_route_circuit_breakers",
        target_type="route_circuit_breakers",
        detail={"route_count": len(before)},
    )
    return {"success": True, "reset_count": len(before), "route_breakers": guard.route_breakers.snapshot()}


@router.post("/accounts/isolation/clear")
async def clear_account_isolations(payload=Depends(require_admin)):
    pool = get_account_pool_service()
    try:
        cleared = pool.clear_all_account_isolations()
    except Exception as exc:
        await _record_admin_audit(
            payload,
            action="clear_all_account_isolations",
            target_type="account_isolations",
            detail={"error": str(exc)[:240]},
            success=False,
        )
        raise
    logger.info("account failure isolations cleared by admin: count=%s", cleared)
    await _record_admin_audit(
        payload,
        action="clear_all_account_isolations",
        target_type="account_isolations",
        detail={"cleared": int(cleared)},
    )
    return {"success": True, "cleared": cleared}


@router.post("/accounts/{account_id}/isolation/clear")
async def clear_account_isolation(account_id: str, payload=Depends(require_admin)):
    pool = get_account_pool_service()
    try:
        cleared = pool.clear_account_isolation(account_id)
    except Exception as exc:
        await _record_admin_audit(
            payload,
            action="clear_account_isolation",
            target_type="account",
            target_id=account_id,
            detail={"error": str(exc)[:240]},
            success=False,
        )
        raise
    if not cleared:
        await _record_admin_audit(
            payload,
            action="clear_account_isolation",
            target_type="account",
            target_id=account_id,
            detail={"cleared": False},
        )
        return {"success": True, "cleared": False}
    logger.info("account failure isolation cleared by admin: account=%s", account_id[:8])
    await _record_admin_audit(
        payload,
        action="clear_account_isolation",
        target_type="account",
        target_id=account_id,
        detail={"cleared": True},
    )
    return {"success": True, "cleared": True}


# ---------------- Account CRUD ----------------
@router.get("/accounts")
async def list_accounts(
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    pool = get_account_pool_service()
    rows = await pool.list_accounts(session)
    isolations = pool.isolation_snapshot()
    items = []
    for account in rows:
        item = _account_to_dict(account)
        isolation = isolations.get(account.id)
        if isolation:
            item["isolation_seconds"] = isolation["remaining_seconds"]
            item["isolation_reason"] = isolation["reason"]
        items.append(item)
    return {"items": items, "total": len(items)}


@router.post("/accounts")
async def create_account(
    req: AccountCreateRequest,
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    pool = get_account_pool_service()
    acc = await pool.create_account(
        session,
        name=req.name,
        org_id=req.org_id,
        flow_id=req.flow_id,
        api_key=req.api_key,
        private_api_key=req.private_api_key,
        max_inflight=req.max_inflight,
    )
    await session.commit()
    return _account_to_dict(acc)


@router.post("/accounts/import")
async def import_accounts(
    req: AccountImportRequest,
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    raw_text = _strip_json_code_fence(req.raw_json)
    if not raw_text:
        raise HTTPException(status_code=400, detail="JSON 内容不能为空")

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"JSON 解析失败：第 {exc.lineno} 行第 {exc.colno} 列")

    rows = _extract_import_rows(parsed)
    if not rows:
        raise HTTPException(
            status_code=400,
            detail="未识别到可导入账号。请提供对象数组，或包含 items/accounts/data 的 JSON。",
        )

    pool = get_account_pool_service()
    existing_accounts = await pool.list_accounts(session)
    existing_emails = {
        str(acc.name or "").strip().lower()
        for acc in existing_accounts
        if str(acc.name or "").strip()
    }
    existing_public_keys = set()
    for acc in existing_accounts:
        try:
            api_key = pool.decrypt_api_key(acc)
        except Exception as exc:
            logger.warning("skip existing account public key dedupe for account=%s: %s", acc.id[:8], exc)
            continue
        normalized_key = _normalize_import_api_key(api_key)
        if normalized_key:
            existing_public_keys.add(normalized_key)

    seen_emails = set(existing_emails)
    seen_public_keys = set(existing_public_keys)
    created: List[dict] = []
    skipped: List[dict] = []
    invalid: List[dict] = []

    for idx, raw_item in enumerate(rows, start=1):
        if not isinstance(raw_item, dict):
            invalid.append({"index": idx, "reason": "该条不是 JSON 对象"})
            continue

        email = _get_first_text_value(raw_item, ["email", "name", "account_name", "account"])
        public_api_key = _get_first_text_value(raw_item, ["public_api_key", "api_key", "publicKey"])
        private_api_key = _get_first_text_value(raw_item, ["private_api_key", "privateKey"])
        org_id = _get_first_text_value(raw_item, ["org_id", "orgId"])
        flow_id = _get_first_text_value(raw_item, ["flow_id", "flowId"])

        normalized_email = email.lower()
        normalized_public_key = _normalize_import_api_key(public_api_key)

        missing_fields = []
        if not normalized_email:
            missing_fields.append("email")
        if not normalized_public_key:
            missing_fields.append("public_api_key")
        if not org_id:
            missing_fields.append("org_id")
        if not flow_id:
            missing_fields.append("flow_id")

        if missing_fields:
            invalid.append(
                {
                    "index": idx,
                    "email": normalized_email or None,
                    "reason": f"缺少必填字段：{', '.join(missing_fields)}",
                }
            )
            continue

        if normalized_email in seen_emails:
            skipped.append(
                {
                    "index": idx,
                    "email": normalized_email,
                    "reason": "重复 email，已自动跳过",
                }
            )
            continue

        if normalized_public_key in seen_public_keys:
            skipped.append(
                {
                    "index": idx,
                    "email": normalized_email,
                    "reason": "重复 public_api_key，已自动跳过",
                }
            )
            continue

        try:
            acc = await pool.create_account(
                session,
                name=normalized_email,
                org_id=org_id,
                flow_id=flow_id,
                api_key=normalized_public_key,
                private_api_key=private_api_key or None,
                max_inflight=req.max_inflight,
            )
        except Exception as exc:
            invalid.append(
                {
                    "index": idx,
                    "email": normalized_email,
                    "reason": f"导入失败：{exc}",
                }
            )
            continue

        created.append(_account_to_dict(acc))
        seen_emails.add(normalized_email)
        seen_public_keys.add(normalized_public_key)

    await session.commit()
    return {
        "total_input": len(rows),
        "created_count": len(created),
        "skipped_count": len(skipped),
        "invalid_count": len(invalid),
        "created": created,
        "skipped": skipped,
        "invalid": invalid,
    }


@router.put("/accounts/{account_id}")
async def update_account(
    account_id: str,
    req: AccountUpdateRequest,
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    pool = get_account_pool_service()
    acc = await pool.update_account(
        session,
        account_id,
        name=req.name,
        org_id=req.org_id,
        flow_id=req.flow_id,
        api_key=req.api_key,
        status=req.status,
        private_api_key=req.private_api_key,
        max_inflight=req.max_inflight,
    )
    if acc is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    await session.commit()
    return _account_to_dict(acc)


@router.post("/accounts/bulk/status")
async def bulk_update_accounts_status(
    req: AccountBulkStatusRequest,
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    pool = get_account_pool_service()
    affected = await pool.update_all_accounts_status(session, status=req.status)
    await session.commit()
    return {"success": True, "status": req.status, "affected": affected}


@router.post("/accounts/{account_id}/test")
async def test_account(
    account_id: str,
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """用最小输入触发一次 ST 调用，仅用于检查账号可达性 / API Key 是否有效。"""
    pool = get_account_pool_service()
    account = await pool.get_account(session, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    try:
        api_key = pool.decrypt_api_key(account)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"解密失败：{exc}")

    client = get_st_client()
    test_payload = {
        "in-0": "a small red apple on a white table",
        "in-1": "1:1",
        "in-2": "2K",
        "in-3": "Nano Banana Pro",
        "in-4": "",
        "in-5": "",
        "in-6": "",
    }
    try:
        raw = await client.run_inference(
            org_id=account.org_id,
            flow_id=account.flow_id,
            api_key=api_key,
            payload=test_payload,
        )
        return {
            "ok": True,
            "message": "上游已应答（HTTP 200）",
            "raw_keys": list(raw.keys()) if isinstance(raw, dict) else None,
        }
    except STError as exc:
        return {
            "ok": False,
            "status_code": exc.status_code,
            "message": redact_upstream_text(exc.message),
        }


@router.delete("/accounts/{account_id}")
async def delete_account(
    account_id: str,
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    pool = get_account_pool_service()
    ok = await pool.delete_account(session, account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="账号不存在")
    await session.commit()
    return {"success": True}


@router.delete("/accounts")
async def delete_all_accounts(
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    pool = get_account_pool_service()
    affected = await pool.delete_all_accounts(session)
    await session.commit()
    return {"success": True, "affected": affected}


@router.get("/invite-codes")
async def list_invite_codes(
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    service = get_user_auth_service()
    rows = await service.list_invite_codes(session)
    return {"items": [_invite_to_dict(r) for r in rows], "total": len(rows)}


@router.post("/invite-codes")
async def create_invite_codes(
    req: InviteCodeCreateRequest,
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    service = get_user_auth_service()
    expires_at = (
        utcnow_naive() + timedelta(days=req.expires_in_days)
        if req.expires_in_days is not None
        else None
    )
    items = await service.create_invite_codes(
        session,
        count=req.count,
        max_uses=req.max_uses,
        note=req.note,
        expires_at=expires_at,
        daily_quota=req.daily_quota,
        max_inflight=req.max_inflight,
    )
    await session.commit()
    return {"items": [_invite_to_dict(invite, raw_code=raw_code) for invite, raw_code in items]}


@router.post("/invite-codes/{invite_id}/revoke")
async def revoke_invite_code(
    invite_id: str,
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    service = get_user_auth_service()
    invite = await service.revoke_invite_code(session, invite_id)
    if invite is None:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    await session.commit()
    return _invite_to_dict(invite)


@router.delete("/invite-codes/{invite_id}")
async def delete_invite_code(
    invite_id: str,
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    service = get_user_auth_service()
    ok = await service.delete_invite_code(session, invite_id)
    if not ok:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    await session.commit()
    return {"success": True}


@router.delete("/invite-codes")
async def delete_all_invite_codes(
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    service = get_user_auth_service()
    affected = await service.delete_all_invite_codes(session)
    await session.commit()
    return {"success": True, "affected": affected}


@router.get("/users")
async def list_users(
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    service = get_user_auth_service()
    rows = await service.list_users(session)
    return {"items": [_user_to_dict(r) for r in rows], "total": len(rows)}


@router.post("/users")
async def create_user(
    req: UserCreateRequest,
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    service = get_user_auth_service()
    try:
        user = await service.create_user(
            session,
            username=req.username,
            password=req.password,
            status=req.status,
            daily_quota=req.daily_quota,
            max_inflight=req.max_inflight,
            expires_at=req.expires_at,
        )
    except (InvalidUsernameError, InvalidPasswordError, UsernameTakenError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except IntegrityError as exc:
        if "users.username" in str(exc).lower() or "unique constraint failed: users.username" in str(exc).lower():
            raise HTTPException(status_code=400, detail="用户名已存在")
        raise HTTPException(status_code=500, detail=f"创建用户失败：{exc}")
    except OperationalError as exc:
        detail = str(exc)
        if "expires_at" in detail and ("no such column" in detail.lower() or "has no column named" in detail.lower()):
            raise HTTPException(status_code=500, detail="创建用户失败：数据库结构未更新，请重启 st-imagen 服务后重试")
        raise HTTPException(status_code=500, detail=f"创建用户失败：{detail}")

    await session.commit()
    return _user_to_dict(user)


@router.post("/users/batch")
async def create_users_batch(
    req: UserBatchCreateRequest,
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    service = get_user_auth_service()
    try:
        created = await service.create_users_batch(
            session,
            count=req.count,
            status=req.status,
            daily_quota=req.daily_quota,
            max_inflight=req.max_inflight,
            expires_at=req.expires_at,
        )
    except (InvalidPasswordError, UsernameTakenError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except IntegrityError as exc:
        if "users.username" in str(exc).lower() or "unique constraint failed: users.username" in str(exc).lower():
            raise HTTPException(status_code=400, detail="批量创建失败：用户名重复，请重试")
        raise HTTPException(status_code=500, detail=f"批量创建用户失败：{exc}")
    except OperationalError as exc:
        detail = str(exc)
        if "expires_at" in detail and ("no such column" in detail.lower() or "has no column named" in detail.lower()):
            raise HTTPException(status_code=500, detail="批量创建失败：数据库结构未更新，请重启 st-imagen 服务后重试")
        raise HTTPException(status_code=500, detail=f"批量创建用户失败：{detail}")
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    await session.commit()
    return {
        "items": [
            {
                "id": user.id,
                "username": user.username,
                "password": password,
                "status": user.status,
            }
            for user, password in created
        ],
        "total": len(created),
    }


@router.put("/users/{user_id}")
async def update_user(
    user_id: str,
    req: UserUpdateRequest,
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    service = get_user_auth_service()
    fields_set = getattr(req, "model_fields_set", getattr(req, "__fields_set__", set()))
    user = await service.update_user(
        session,
        user_id,
        status=req.status,
        daily_quota=req.daily_quota,
        max_inflight=req.max_inflight,
        expires_at=req.expires_at,
        expires_at_provided="expires_at" in fields_set,
        new_password=req.new_password,
    )
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    await session.commit()
    return _user_to_dict(user)


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    service = get_user_auth_service()
    ok = await service.delete_user(session, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="用户不存在")
    await session.commit()
    return {"success": True}


@router.delete("/users")
async def delete_all_users(
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    service = get_user_auth_service()
    affected = await service.delete_all_users(session)
    await session.commit()
    return {"success": True, "affected": affected}


# ---------------- 简单统计 ----------------
async def _stats_overview_payload(session: AsyncSession) -> dict:
    await get_user_auth_service().ensure_user_schema(session)
    # 账号统计
    total_acc = (await session.execute(select(func.count(Account.id)))).scalar_one()
    active_acc = (
        await session.execute(
            select(func.count(Account.id)).where(Account.status == "active")
        )
    ).scalar_one()

    # 调用统计
    total_calls = (await session.execute(select(func.count(GenerationLog.id)))).scalar_one()
    success_calls = (
        await session.execute(
            select(func.count(GenerationLog.id)).where(GenerationLog.status == "success")
        )
    ).scalar_one()
    error_calls = total_calls - success_calls
    total_generated_images = await get_total_generated_images(session)
    total_users = (await session.execute(select(func.count(User.id)))).scalar_one()
    active_users = (
        await session.execute(
            select(func.count(User.id))
            .where(User.status == "active")
            .where(or_(User.expires_at.is_(None), User.expires_at > utcnow_naive()))
        )
    ).scalar_one()
    total_invites = (await session.execute(select(func.count(InviteCode.id)))).scalar_one()
    active_invites = (
        await session.execute(
            select(func.count(InviteCode.id)).where(InviteCode.revoked_at.is_(None))
        )
    ).scalar_one()

    return {
        "accounts": {"total": total_acc, "active": active_acc},
        "users": {"total": total_users, "active": active_users},
        "invites": {"total": total_invites, "active": active_invites},
        "generations": {
            "total": total_calls,
            "success": success_calls,
            "error": error_calls,
            "images_total": total_generated_images,
        },
    }


@router.get("/stats/overview")
async def stats_overview(
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    del payload
    return await _stats_overview_payload(session)


@router.get("/stats/dashboard")
async def stats_dashboard(
    period: str = Query(default="24h"),
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Return complete-period dashboard aggregates, independent of log pagination."""
    del payload
    try:
        return await get_dashboard_analytics(session, period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/dashboard/snapshot")
async def dashboard_snapshot(
    period: str = Query(default="24h"),
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Return the data needed to paint the dashboard's first viewport.

    Management tables remain separate endpoints so a slow or broken account
    list cannot prevent operators from seeing service health and recent work.
    """
    try:
        # The snapshot shares one AsyncSession for the database-backed pieces;
        # keep those reads sequential because AsyncSession is not concurrent.
        overview = await _stats_overview_payload(session)
        analytics = await get_dashboard_analytics(session, period)
        runtime_status_data = await _runtime_status_payload(session)
        # Volatile counters are intentionally sampled as part of the same
        # response, but do not require another database round trip.
        recent = await _recent_logs_payload(session, limit=20)
        return {
            "admin": {
                "id": payload.get("sub"),
                "username": payload.get("username"),
            },
            "overview": overview,
            "analytics": analytics,
            "runtime_metrics": _runtime_metrics_payload(),
            "runtime_status": runtime_status_data,
            "runtime_config": _runtime_config_payload(
                get_generation_guard(), get_account_pool_service()
            ),
            "recent_logs": recent["items"],
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _recent_logs_payload(session: AsyncSession, limit: int = 50) -> dict:
    limit = max(1, min(200, int(limit)))
    # 左连接 Account 表以拿到账号名
    rows = await session.execute(
        select(GenerationLog, Account.name, User.username)
        .outerjoin(Account, Account.id == GenerationLog.account_id)
        .outerjoin(User, User.id == GenerationLog.user_id)
        .order_by(GenerationLog.timestamp.desc())
        .limit(limit)
    )
    items = []
    for r, account_name, username in rows.all():
        failure_category = (
            getattr(r, "failure_category", None)
            or (normalize_failure_category(r.error_message) if r.status != "success" else None)
        )
        items.append(
            {
                "id": r.id,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "user_id": r.user_id,
                "username": username,
                "account_id": r.account_id,
                "account_name": account_name,
                "mode": r.mode,
                "model": r.model,
                "aspect_ratio": r.aspect_ratio,
                "resolution": r.resolution,
                "prompt_preview": r.prompt_preview,
                "image_url": r.image_url,
                "output_preview": r.output_preview,
                "output_images": getattr(r, "output_images", None),
                "response_time_ms": r.response_time_ms,
                "status": r.status,
                "error_message": redact_upstream_text(r.error_message),
                "failure_category": failure_category,
                "error_code": getattr(r, "error_code", None) or (dashboard_failure_code(failure_category) if failure_category else None),
                "is_stream": getattr(r, "is_stream", False),
            }
        )
    return {"items": items, "total": len(items)}


@router.get("/logs")
async def recent_logs(
    payload=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    limit: int = 50,
):
    del payload
    return await _recent_logs_payload(session, limit)
