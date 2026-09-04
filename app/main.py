"""FastAPI 主入口：注册路由 + 静态文件 + 生命周期。"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app import __version__ as APP_VERSION
from app.env import (
    ENV_PATH,
    PLACEHOLDER_ADMIN_PASSWORDS,
    PLACEHOLDER_ENCRYPTION_KEY,
    PLACEHOLDER_JWT_SECRET_KEY,
    env_fingerprint,
    is_standard_fernet_key,
    load_project_env,
)

# Load project configuration before importing modules that snapshot environment
# backed constants (SQLite pool, account defaults, stream limits, etc.).
ENV_PATH_LOADED, FORCED_ENV_KEYS = load_project_env()

from app.models.database import close_database, get_session_factory, init_database
from app.routers import admin_router, generate_router, linuxdo_auth_router, user_auth_router
from app.routers.generate import close_downloads_client
from app.services.account_pool import get_account_pool_service
from app.services.auth import get_auth_service
from app.services.database_maintenance import remove_expired_sessions, remove_old_generation_logs
from app.services.st_client import close_st_client
from app.services.upstream_redaction import redact_upstream_data
from app.services.user_auth import get_user_auth_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger.info("env loaded from %s", ENV_PATH_LOADED)
if FORCED_ENV_KEYS:
    logger.warning(
        "forced critical settings from %s for keys=%s",
        ENV_PATH_LOADED,
        ",".join(FORCED_ENV_KEYS),
    )
logger.info(
    "ENCRYPTION_KEY fingerprint=%s",
    env_fingerprint("ENCRYPTION_KEY"),
)
logger.info(
    "runtime config: DEBUG=%s ST_TIMEOUT_SECONDS=%s ST_CONNECT_TIMEOUT_SECONDS=%s ST_STREAM_READ_TIMEOUT_SECONDS=%s",
    (os.getenv("DEBUG") or "false").strip().lower(),
    os.getenv("ST_TIMEOUT_SECONDS", "270"),
    os.getenv("ST_CONNECT_TIMEOUT_SECONDS", "10"),
    os.getenv("ST_STREAM_READ_TIMEOUT_SECONDS", "330"),
)
raw_encryption_key = (os.getenv("ENCRYPTION_KEY") or "").strip()
if not raw_encryption_key:
    logger.warning("ENCRYPTION_KEY is unset; account secrets cannot be encrypted/decrypted")
elif raw_encryption_key == PLACEHOLDER_ENCRYPTION_KEY:
    logger.warning(
        "ENCRYPTION_KEY is still the placeholder value from %s; replace it before production use",
        ENV_PATH,
    )
elif not is_standard_fernet_key(raw_encryption_key):
    logger.warning(
        "ENCRYPTION_KEY is not a standard Fernet key; crypto service will use fallback key normalization"
    )


# 后台页面路径可通过 ADMIN_PATH 自定义；未设置时默认 /admin。
ADMIN_PATH = (os.getenv("ADMIN_PATH") or "").strip().strip("/") or "admin"


def _validate_runtime_security_settings() -> None:
    issues = []

    jwt_secret = (os.getenv("JWT_SECRET_KEY") or "").strip()
    if not jwt_secret or jwt_secret == PLACEHOLDER_JWT_SECRET_KEY:
        issues.append("JWT_SECRET_KEY 未设置为真实值")
    elif len(jwt_secret.encode("utf-8")) < 32:
        issues.append("JWT_SECRET_KEY 必须至少为 32 字节")

    encryption_key = (os.getenv("ENCRYPTION_KEY") or "").strip()
    if not encryption_key or encryption_key == PLACEHOLDER_ENCRYPTION_KEY:
        issues.append("ENCRYPTION_KEY 未设置为真实值")
    elif not is_standard_fernet_key(encryption_key) and os.getenv(
        "ALLOW_LEGACY_ENCRYPTION_KEY", "false"
    ).lower() not in {"1", "true", "yes"}:
        issues.append("ENCRYPTION_KEY 必须是标准 Fernet 密钥")

    if int(os.getenv("UVICORN_WORKERS", "1")) != 1:
        issues.append("当前进程内并发模型要求 UVICORN_WORKERS=1")

    if issues:
        raise RuntimeError("安全配置无效: " + "; ".join(issues))

    admin_password = os.getenv("ADMIN_PASSWORD") or ""
    if not admin_password or admin_password in PLACEHOLDER_ADMIN_PASSWORDS:
        logger.warning(
            "ADMIN_PASSWORD is blank or still a placeholder; existing admin accounts can still log in, "
            "but default-admin bootstrap will be blocked until you set a real password in %s",
            ENV_PATH,
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _validate_runtime_security_settings()
    await init_database()
    removed_sessions = await remove_expired_sessions()
    if removed_sessions:
        logger.info("expired/revoked sessions removed at startup: %s", removed_sessions)
    removed_logs = await remove_old_generation_logs(
        int(os.getenv("GENERATION_LOG_RETENTION_DAYS", "90"))
    )
    if removed_logs:
        logger.info("expired generation logs removed at startup: %s", removed_logs)
    await get_auth_service().ensure_default_admin()

    print("=" * 60)
    print(f" ST Image Gen v{APP_VERSION}")
    print(" Frontend  : /")
    print(f" Admin page: /{ADMIN_PATH}")
    print("=" * 60)
    yield
    try:
        await close_st_client()
    except Exception:
        logger.exception("Failed to close st client")
    try:
        await close_downloads_client()
    except Exception:
        logger.exception("Failed to close downloads client")
    try:
        await get_account_pool_service().drain_usage_persistence()
        await get_user_auth_service().drain_usage_persistence()
    except Exception:
        logger.exception("Failed to drain usage persistence workers")
    await close_database()


app = FastAPI(
    title="ST Image Gen",
    description="多账号 ST 图像生成代理",
    version=APP_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.exception_handler(HTTPException)
async def sanitized_http_exception(_request: Request, exc: HTTPException):
    """Guarantee that no upstream provider identifier crosses the API boundary."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": redact_upstream_data(exc.detail)},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def sanitized_validation_exception(_request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": redact_upstream_data(exc.errors())},
    )


# CORS：默认不开跨域；若显式配置 '*'，则不允许凭证。
cors_origins_env = os.getenv("CORS_ORIGINS", "").strip()
cors_allow_credentials_env = (os.getenv("CORS_ALLOW_CREDENTIALS") or "").strip().lower()
allow_origins = (
    ["*"]
    if cors_origins_env == "*"
    else [o.strip() for o in cors_origins_env.split(",") if o.strip()]
)
if allow_origins:
    allow_credentials = (
        cors_allow_credentials_env in {"1", "true", "yes"}
        if cors_allow_credentials_env
        else allow_origins != ["*"]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )


app.include_router(admin_router)
app.include_router(user_auth_router)
app.include_router(linuxdo_auth_router)
app.include_router(generate_router)


# 缓存头：uploads 文件名唯一不可变 → immutable 长缓存；static 与页面 HTML 无版本指纹
# → no-cache（每次带 ETag 重新校验，304 便宜且部署后必定拿到新文件）；options 进程内
# 静态 → 短缓存。SSE/动态接口不受影响。
_CACHE_CONTROL_RULES = (
    ("/uploads/", "public, max-age=31536000, immutable"),
    ("/static/", "no-cache"),
    ("/api/options", "public, max-age=300"),
)

_request_counts: Counter[tuple[str, str, int]] = Counter()
_request_duration_seconds: Counter[tuple[str, str]] = Counter()


def _metric_path(request: Request) -> str:
    """Return a bounded-cardinality route label for request metrics."""
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    return str(route_path) if route_path else "__unmatched__"


@app.middleware("http")
async def cache_control_middleware(request: Request, call_next):
    started = time.monotonic()
    request_id = request.headers.get("X-Request-ID", "").strip()[:128] or str(uuid.uuid4())
    response = await call_next(request)
    path = request.url.path
    for prefix, value in _CACHE_CONTROL_RULES:
        if path.startswith(prefix):
            response.headers.setdefault("Cache-Control", value)
            break
    if path.startswith("/api/") and "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Request-ID"] = request_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob: https:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; script-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'",
    )
    method = request.method
    path = _metric_path(request)
    _request_counts[(method, path, response.status_code)] += 1
    _request_duration_seconds[(method, path)] += time.monotonic() - started
    return response


static_path = Path(__file__).parent / "static"
upload_path = Path(
    os.getenv("UPLOADS_DIR") or (Path(__file__).resolve().parents[1] / "data" / "uploads")
).resolve()
upload_path.mkdir(parents=True, exist_ok=True)
# nginx reads this bind mount as UID 101 while app writes as UID 10001. Only
# public upload content is exposed here; the database remains under data/.
upload_path.chmod(0o755)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/health/live")
async def health_live() -> dict:
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready() -> JSONResponse:
    checks: dict[str, object] = {}
    status_code = 200
    try:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        logger.warning("readiness database check failed: %s", exc)
        checks["database"] = "error"
        status_code = 503
    try:
        free_bytes = shutil.disk_usage(upload_path).free
        minimum = max(0, int(os.getenv("GENERATED_IMAGE_MIN_FREE_BYTES", "0")))
        checks["disk"] = "ok" if free_bytes >= minimum else "insufficient"
        if free_bytes < minimum:
            status_code = 503
    except OSError as exc:
        logger.warning("readiness disk check failed: %s", exc)
        checks["disk"] = "error"
        status_code = 503
    return JSONResponse(
        {"status": "ok" if status_code == 200 else "not_ready", "checks": checks},
        status_code=status_code,
    )


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> PlainTextResponse:
    expected = (os.getenv("METRICS_TOKEN") or "").strip()
    supplied = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=404, detail="Not Found")
    lines = ["# TYPE st_imagen_http_requests_total counter"]
    for (method, path, status), value in sorted(_request_counts.items()):
        lines.append(
            f'st_imagen_http_requests_total{{method="{method}",path="{path}",status="{status}"}} {value}'
        )
    lines.append("# TYPE st_imagen_http_request_duration_seconds_total counter")
    for (method, path), value in sorted(_request_duration_seconds.items()):
        lines.append(
            f'st_imagen_http_request_duration_seconds_total{{method="{method}",path="{path}"}} {value:.6f}'
        )
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


_NO_CACHE_HEADERS = {"Cache-Control": "no-cache"}


@app.get("/")
async def index_page() -> FileResponse:
    index_file = static_path / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file), media_type="text/html", headers=_NO_CACHE_HEADERS)
    return JSONResponse({"status": "ok"}, status_code=200)


@app.get(f"/{ADMIN_PATH}")
async def admin_page() -> FileResponse:
    admin_file = static_path / "admin.html"
    if admin_file.exists():
        return FileResponse(str(admin_file), media_type="text/html", headers=_NO_CACHE_HEADERS)
    return JSONResponse({"status": "ok"}, status_code=200)


@app.get(f"/{ADMIN_PATH}" + "/{page_path:path}")
async def admin_subpage(page_path: str) -> FileResponse:
    admin_file = static_path / "admin.html"
    if admin_file.exists():
        return FileResponse(str(admin_file), media_type="text/html", headers=_NO_CACHE_HEADERS)
    return JSONResponse({"status": "ok"}, status_code=200)


if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

if upload_path.exists():
    app.mount("/uploads", StaticFiles(directory=str(upload_path)), name="uploads")
