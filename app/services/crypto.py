"""加密 / 哈希服务（Fernet + bcrypt）。

借鉴 st-api app/services/crypto.py 的精简版。
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional

import bcrypt
from cryptography.fernet import Fernet, InvalidToken


class CryptoService:
    def __init__(self, encryption_key: Optional[str] = None) -> None:
        self._encryption_key = encryption_key or os.getenv("ENCRYPTION_KEY")
        self._fernet: Optional[Fernet] = None
        if self._encryption_key:
            self._init_fernet()

    def _init_fernet(self) -> None:
        try:
            key = self._encryption_key.encode() if isinstance(self._encryption_key, str) else self._encryption_key
            self._fernet = Fernet(key)
        except Exception:
            # 兼容用户填的不是标准 Fernet key，做一次 padding/截断。
            key_bytes = self._encryption_key.encode() if isinstance(self._encryption_key, str) else self._encryption_key
            if len(key_bytes) < 32:
                key_bytes = key_bytes.ljust(32, b"\0")
            elif len(key_bytes) > 32:
                key_bytes = key_bytes[:32]
            self._fernet = Fernet(base64.urlsafe_b64encode(key_bytes))

    @staticmethod
    def generate_encryption_key() -> str:
        return Fernet.generate_key().decode()

    def encrypt(self, plaintext: str) -> str:
        if self._fernet is None:
            raise RuntimeError("ENCRYPTION_KEY 未设置")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        if self._fernet is None:
            raise RuntimeError("ENCRYPTION_KEY 未设置")
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("解密失败：密钥错误或数据损坏") from exc

    @staticmethod
    def hash_password(password: str) -> str:
        pwd_bytes = password.encode("utf-8")[:72]
        return bcrypt.hashpw(pwd_bytes, bcrypt.gensalt(rounds=12)).decode("utf-8")

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        try:
            return bcrypt.checkpw(
                password.encode("utf-8")[:72],
                password_hash.encode("utf-8"),
            )
        except Exception:
            return False

    @staticmethod
    def mask_api_key(api_key: str) -> str:
        if len(api_key) <= 11:
            return api_key[:3] + "***"
        return api_key[:7] + "..." + api_key[-4:]

    @staticmethod
    def hash_api_key(api_key: str) -> str:
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


_crypto_service: Optional[CryptoService] = None


def get_crypto_service() -> CryptoService:
    global _crypto_service
    if _crypto_service is None:
        _crypto_service = CryptoService()
    return _crypto_service
