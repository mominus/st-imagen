"""统一的 StackAI 故障范围分类。

分类的关键原则是：一次公共路由故障不能把所有账号变成“坏账号”。
只有错误体明确指向凭据或 org/flow 配置时，才隔离当前账号。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from app.services.stackai_client import StackAIError


ACCOUNT_SCOPE = "account"
UPSTREAM_ROUTE_SCOPE = "upstream_route"
ROUTE_CONFIG_SCOPE = "route_config"
REQUEST_SCOPE = "request"
UNKNOWN_SCOPE = "unknown"

_ACCOUNT_HINTS = re.compile(
    r"(?:api[ _-]?key|access[ _-]?token|credential|authentication|authorization|"
    r"bearer|token\s+(?:expired|revoked|invalid)|key\s+(?:expired|revoked|invalid)|"
    r"invalid\s+(?:api[ _-]?key|token)|expired|revoked|secret)",
    re.IGNORECASE,
)
_ROUTE_CONFIG_HINTS = re.compile(
    r"(?:org(?:anization)?\s*(?:id|not found|invalid)|flow\s*(?:id|not found|invalid)|"
    r"workflow\s*(?:not found|invalid)|deployment|configuration|model\s*(?:not found|invalid))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FailureDecision:
    scope: str
    failover: bool
    isolate_account: bool
    count_route_failure: bool
    retry_after: Optional[float] = None


def _error_text(exc: StackAIError) -> str:
    parts = [exc.message, str(exc.payload or "")]
    return " ".join(parts)[:12000]


def classify_stackai_error(exc: StackAIError) -> FailureDecision:
    """Return a conservative decision for retry, failover and isolation."""
    status = exc.status_code
    text = _error_text(exc)

    # Credentials are account-local. A bare 401 is not enough to quarantine an
    # account because gateways also use 401 for route-level auth failures.
    if _ACCOUNT_HINTS.search(text):
        return FailureDecision(
            ACCOUNT_SCOPE,
            failover=True,
            isolate_account=True,
            count_route_failure=False,
            retry_after=exc.retry_after,
        )

    # org/flow/model/deployment mistakes are also local to the selected account
    # (two accounts may point at different routes).
    if _ROUTE_CONFIG_HINTS.search(text) and status in {400, 403, 404, 422}:
        return FailureDecision(
            ROUTE_CONFIG_SCOPE,
            failover=True,
            isolate_account=True,
            count_route_failure=False,
            retry_after=exc.retry_after,
        )

    # Public throttling, gateway errors, timeouts and disconnects are shared
    # route failures. They must not cool down the selected account.
    if status in {408, 429, 502, 503, 504} or status is None or status >= 500:
        return FailureDecision(
            UPSTREAM_ROUTE_SCOPE,
            failover=False,
            isolate_account=False,
            count_route_failure=True,
            retry_after=exc.retry_after,
        )

    if status in {400, 422}:
        return FailureDecision(REQUEST_SCOPE, False, False, False, exc.retry_after)

    # Conservative default: an ambiguous 403/404 is a route problem, but not a
    # reason to quarantine an account. It is safe to expose the error once.
    return FailureDecision(UNKNOWN_SCOPE, False, False, False, exc.retry_after)


def route_key(*, base_url: str, org_id: str, flow_id: str, mode: str, model: str) -> str:
    return "|".join(
        (str(base_url).rstrip("/"), str(org_id), str(flow_id), str(mode), str(model))
    )
