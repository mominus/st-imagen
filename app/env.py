"""项目级环境变量加载。"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import List, Tuple

from cryptography.fernet import Fernet
from dotenv import dotenv_values, load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
PLACEHOLDER_ENCRYPTION_KEY = "replace-with-fernet-key"
PLACEHOLDER_JWT_SECRET_KEY = "replace-with-random-jwt-secret"
PLACEHOLDER_ADMIN_PASSWORDS = frozenset(
    {
        "admin123",
        "replace-with-strong-admin-password",
    }
)
ENV_FILE_PRIORITY_KEYS = (
    "ENCRYPTION_KEY",
    "JWT_SECRET_KEY",
)


def load_project_env() -> Tuple[Path, List[str]]:
    """固定从项目根目录 .env 读取，并强制关键安全配置以 .env 为准。"""
    load_dotenv(ENV_PATH, override=False)

    forced_keys: List[str] = []
    values = dotenv_values(ENV_PATH)
    for key in ENV_FILE_PRIORITY_KEYS:
        file_value = values.get(key)
        if file_value is None:
            continue
        current = os.environ.get(key)
        if current != file_value:
            forced_keys.append(key)
        os.environ[key] = file_value
    return ENV_PATH, forced_keys


def env_fingerprint(name: str) -> str:
    value = os.getenv(name) or ""
    if not value:
        return "unset"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def is_standard_fernet_key(value: str) -> bool:
    raw = (value or "").strip()
    if not raw:
        return False
    try:
        Fernet(raw.encode("utf-8"))
        return True
    except Exception:
        return False
