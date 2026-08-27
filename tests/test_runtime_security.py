from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from app.main import _validate_runtime_security_settings


class RuntimeSecuritySettingsTests(unittest.TestCase):
    def test_valid_security_settings_are_accepted(self):
        values = {
            "JWT_SECRET_KEY": "x" * 32,
            "ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "ADMIN_PASSWORD": "a-secure-admin-password",
            "UVICORN_WORKERS": "1",
            "ALLOW_LEGACY_ENCRYPTION_KEY": "false",
        }
        with patch.dict(os.environ, values, clear=False):
            _validate_runtime_security_settings()

    def test_short_jwt_secret_and_multiple_workers_are_rejected(self):
        values = {
            "JWT_SECRET_KEY": "short",
            "ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "ADMIN_PASSWORD": "a-secure-admin-password",
            "UVICORN_WORKERS": "2",
        }
        with patch.dict(os.environ, values, clear=False):
            with self.assertRaisesRegex(RuntimeError, "32.*UVICORN_WORKERS"):
                _validate_runtime_security_settings()

    def test_nonstandard_fernet_key_requires_explicit_migration_flag(self):
        values = {
            "JWT_SECRET_KEY": "x" * 32,
            "ENCRYPTION_KEY": "legacy-passphrase",
            "ADMIN_PASSWORD": "a-secure-admin-password",
            "UVICORN_WORKERS": "1",
            "ALLOW_LEGACY_ENCRYPTION_KEY": "false",
        }
        with patch.dict(os.environ, values, clear=False):
            with self.assertRaisesRegex(RuntimeError, "Fernet"):
                _validate_runtime_security_settings()
