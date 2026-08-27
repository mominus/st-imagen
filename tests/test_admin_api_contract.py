from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.routing import APIRoute

from app.routers import admin_router
from app.routers.admin import _account_to_dict, _invite_to_dict, _user_to_dict


ROOT = Path(__file__).parents[1]
CONTRACT_PATH = ROOT / "tests" / "contracts" / "admin_api.json"
ADMIN_SCRIPT_ROOT = ROOT / "app" / "static" / "admin"


def load_contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def dependency_names(route: APIRoute) -> set[str]:
    names: set[str] = set()

    def collect(dependant) -> None:
        for dependency in dependant.dependencies:
            names.add(dependency.call.__name__)
            collect(dependency)

    collect(route.dependant)
    return names


def canonical_path(path: str) -> str:
    path = path.split("?", 1)[0]
    path = re.sub(r"\$\{[^}]+\}", "{value}", path)
    return re.sub(r"\{[^}]+\}", "{value}", path)


def test_admin_router_matches_versioned_contract_and_auth_policy():
    contract = load_contract()
    assert contract["version"] == 1
    expected = {
        (item["method"], item["path"]): item["auth"] for item in contract["routes"]
    }
    actual: dict[tuple[str, str], APIRoute] = {}
    for route in admin_router.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            actual[(method, route.path)] = route

    assert set(actual) == set(expected)
    for operation, auth_required in expected.items():
        dependencies = dependency_names(actual[operation])
        assert ("require_admin" in dependencies) is auth_required, operation


def test_every_frontend_admin_endpoint_exists_in_backend_contract():
    contract_paths = {
        canonical_path(item["path"]) for item in load_contract()["routes"]
    }
    frontend_paths: set[str] = set()
    api_call_pattern = re.compile(r"\bapi\(\s*([`\"'])(/api/admin/.*?)\1", re.DOTALL)
    for script in ADMIN_SCRIPT_ROOT.glob("*.js"):
        for match in api_call_pattern.finditer(script.read_text(encoding="utf-8")):
            frontend_paths.add(canonical_path(match.group(2)))

    assert frontend_paths
    assert frontend_paths <= contract_paths


def test_resource_serializers_keep_frontend_field_contracts():
    fields = load_contract()["resource_fields"]
    timestamps = {
        "last_used_at": None,
        "last_login_at": None,
        "expires_at": None,
        "revoked_at": None,
        "created_at": None,
        "updated_at": None,
    }

    account = SimpleNamespace(
        id="account-1",
        name="account",
        org_id="org",
        flow_id="flow",
        private_api_key_encrypted=None,
        status="active",
        total_requests=0,
        max_inflight=2,
        **{key: timestamps[key] for key in ("last_used_at", "created_at", "updated_at")},
    )
    pool = SimpleNamespace(
        DEFAULT_MAX_INFLIGHT=2,
        runtime_in_flight=lambda _account_id: 0,
    )
    with patch("app.routers.admin.get_account_pool_service", return_value=pool):
        account_payload = _account_to_dict(account)

    invite = SimpleNamespace(
        id="invite-1",
        code_prefix="ABCD",
        code_suffix="WXYZ",
        note="contract",
        max_uses=1,
        used_count=0,
        daily_quota=10,
        max_inflight=2,
        **{key: timestamps[key] for key in ("expires_at", "revoked_at", "created_at", "updated_at")},
    )
    invite_payload = _invite_to_dict(invite)

    user = SimpleNamespace(
        id="user-1",
        username="contract-user",
        status="active",
        invite_code_id=None,
        daily_quota=10,
        total_requests=0,
        max_inflight=2,
        **{key: timestamps[key] for key in ("last_used_at", "last_login_at", "expires_at", "created_at", "updated_at")},
    )
    usage = {"daily_used": 0, "in_flight": 0, "quota_remaining": 10}
    with (
        patch("app.routers.admin.build_user_usage_snapshot", return_value=usage),
        patch("app.routers.admin.get_effective_user_status", return_value="active"),
        patch("app.routers.admin.is_user_expired", return_value=False),
    ):
        user_payload = _user_to_dict(user)

    assert list(account_payload) == fields["account"]
    assert list(invite_payload) == fields["invite"]
    assert list(user_payload) == fields["user"]
    assert "api_key" not in account_payload
    assert "private_api_key" not in account_payload
