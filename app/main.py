"""FastAPI 主入口：注册路由 + 静态文件 + 生命周期。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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

from app.models.database import close_database, init_database
from app.routers import admin_router, generate_router, user_auth_router
from app.routers.generate import close_downloads_client
from app.services.auth import get_auth_service
from app.services.account_pool import get_account_pool_service
from app.services.stackai_client import close_stackai_client
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
    "runtime config: DEBUG=%s STACKAI_TIMEOUT_SECONDS=%s STACKAI_CONNECT_TIMEOUT_SECONDS=%s STACKAI_STREAM_READ_TIMEOUT_SECONDS=%s",
    (os.getenv("DEBUG") or "false").strip().lower(),
    os.getenv("STACKAI_TIMEOUT_SECONDS", "270"),
    os.getenv("STACKAI_CONNECT_TIMEOUT_SECONDS", "10"),
    os.getenv("STACKAI_STREAM_READ_TIMEOUT_SECONDS", "330"),
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

    encryption_key = (os.getenv("ENCRYPTION_KEY") or "").strip()
    if not encryption_key or encryption_key == PLACEHOLDER_ENCRYPTION_KEY:
        issues.append("ENCRYPTION_KEY 未设置为真实值")

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
    await get_auth_service().ensure_default_admin()

    print("=" * 60)
    print(f" StackAI Image Gen v{APP_VERSION}")
    print(f" Frontend  : /")
    print(f" Admin page: /{ADMIN_PATH}")
    print("=" * 60)
    yield
    try:
        await close_stackai_client()
    except Exception:
        logger.exception("Failed to close stackai client")
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
    title="StackAI Image Gen",
    description="多账号 StackAI 图像生成代理",
    version=APP_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
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
app.include_router(generate_router)


# 缓存头：uploads 文件名唯一不可变 → immutable 长缓存；static 与页面 HTML 无版本指纹
# → no-cache（每次带 ETag 重新校验，304 便宜且部署后必定拿到新文件）；options 进程内
# 静态 → 短缓存。SSE/动态接口不受影响。
_CACHE_CONTROL_RULES = (
    ("/uploads/", "public, max-age=31536000, immutable"),
    ("/static/", "no-cache"),
    ("/api/options", "public, max-age=300"),
)


@app.middleware("http")
async def cache_control_middleware(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    for prefix, value in _CACHE_CONTROL_RULES:
        if path.startswith(prefix):
            response.headers.setdefault("Cache-Control", value)
            break
    return response


static_path = Path(__file__).parent / "static"
upload_path = Path(
    os.getenv("UPLOADS_DIR") or (Path(__file__).resolve().parents[1] / "data" / "uploads")
).resolve()
upload_path.mkdir(parents=True, exist_ok=True)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": APP_VERSION}


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
