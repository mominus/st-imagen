"""LINUX DO Connect OAuth2：协议客户端 + 回调 state 保护。

官方文档：https://wiki.linux.do/Community/LinuxDoConnect
- 授权端点: https://connect.linux.do/oauth2/authorize （response_type=code, scope=user）
- 令牌端点: https://connect.linux.do/oauth2/token （form-urlencoded，支持 PKCE S256）
- 用户信息: https://connect.linux.do/api/user （Bearer access_token）

开关采用与图片保留期一致的「DB 运行时设置覆盖 env」模式：管理后台可以在线
启停，而 client_id/client_secret 属于敏感凭据，只从 .env 读取。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
from fastapi import Request, Response

from app.services import app_settings

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://connect.linux.do/oauth2/authorize"
TOKEN_URL = "https://connect.linux.do/oauth2/token"
USER_INFO_URL = "https://connect.linux.do/api/user"

SETTING_LINUXDO_OAUTH_ENABLED = app_settings.SETTING_LINUXDO_OAUTH_ENABLED

STATE_COOKIE_NAME = "imagen_oauth_state"
# 用户必须在 10 分钟内完成 connect.linux.do 的授权跳转，超时重新发起
STATE_COOKIE_MAX_AGE = 600
_UPSTREAM_TIMEOUT = httpx.Timeout(15.0, connect=10.0)

# 测试注入 httpx.MockTransport 用；生产路径保持为空
_http_client_kwargs: Dict[str, Any] = {}


class LinuxDOOAuthError(Exception):
    """OAuth 流程失败；message 可直接展示给用户，不含敏感信息。"""


def set_http_client_kwargs(**kwargs: Any) -> None:
    """仅测试使用：替换 httpx.AsyncClient 的构造参数（如 transport）。"""
    _http_client_kwargs.clear()
    _http_client_kwargs.update(kwargs)


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT, **_http_client_kwargs)


def _env_flag(name: str, default: str) -> bool:
    return (os.getenv(name) or default).strip().lower() in {"1", "true", "yes", "on"}


def env_enabled_default() -> bool:
    return _env_flag("LINUXDO_OAUTH_ENABLED", "false")


async def is_enabled() -> bool:
    """运行时设置（DB）优先于 env 默认，管理后台可在线开关。"""
    return await app_settings.get_effective_bool(
        SETTING_LINUXDO_OAUTH_ENABLED, env_enabled_default()
    )


def is_configured() -> bool:
    return bool((os.getenv("LINUXDO_CLIENT_ID") or "").strip()) and bool(
        (os.getenv("LINUXDO_CLIENT_SECRET") or "").strip()
    )


def _client_credentials() -> tuple[str, str]:
    client_id = (os.getenv("LINUXDO_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("LINUXDO_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        raise LinuxDOOAuthError("LINUX DO 登录未配置客户端凭据，请联系管理员")
    return client_id, client_secret


def oauth_scope() -> str:
    return (os.getenv("LINUXDO_OAUTH_SCOPE") or "user").strip() or "user"


def min_trust_level() -> int:
    try:
        return max(0, int((os.getenv("LINUXDO_MIN_TRUST_LEVEL") or "0").strip() or 0))
    except ValueError:
        return 0


def resolve_redirect_uri(request: Optional[Request]) -> str:
    """回调地址：env 覆盖优先，否则按公网根地址推导（与图片外链同一来源）。"""
    override = (os.getenv("LINUXDO_REDIRECT_URI") or "").strip()
    if override:
        return override.rstrip("/")
    from app.public_url import public_base_url

    return public_base_url(request).rstrip("/") + "/api/auth/linuxdo/callback"


def make_pkce_pair() -> tuple[str, str]:
    """返回 (code_verifier, code_challenge)；S256 方式，符合 RFC 7636 长度要求。"""
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def encode_state_payload(*, state: str, verifier: str, invite_code: str) -> str:
    """state + PKCE verifier + 邀请码打包进一次性的 HttpOnly cookie。"""
    raw = json.dumps(
        {"s": state, "v": verifier, "i": invite_code}, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_state_payload(raw: Optional[str]) -> Optional[Dict[str, str]]:
    if not raw:
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    state = str(data.get("s") or "")
    verifier = str(data.get("v") or "")
    if not state or not verifier:
        return None
    return {
        "state": state,
        "verifier": verifier,
        "invite_code": str(data.get("i") or ""),
    }


def _cookie_common_kwargs() -> Dict[str, Any]:
    same_site = (os.getenv("USER_SESSION_SAMESITE") or "lax").strip().lower() or "lax"
    if same_site not in {"lax", "strict"}:
        raise RuntimeError("USER_SESSION_SAMESITE 仅允许设置为 lax 或 strict")
    secure_value = (os.getenv("USER_SESSION_SECURE") or "true").strip().lower()
    if secure_value not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise RuntimeError("USER_SESSION_SECURE 必须是明确的布尔值")
    return {
        "httponly": True,
        "secure": secure_value in {"1", "true", "yes", "on"},
        "samesite": same_site,
        "domain": (os.getenv("USER_SESSION_DOMAIN") or "").strip() or None,
    }


def set_state_cookie(response: Response, payload: str) -> None:
    response.set_cookie(
        key=STATE_COOKIE_NAME,
        value=payload,
        max_age=STATE_COOKIE_MAX_AGE,
        **_cookie_common_kwargs(),
    )


def clear_state_cookie(response: Response) -> None:
    common = _cookie_common_kwargs()
    response.delete_cookie(key=STATE_COOKIE_NAME, path="/", domain=common["domain"])


async def build_authorize_url(*, state: str, code_challenge: str, redirect_uri: str) -> str:
    client_id, _ = _client_credentials()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": oauth_scope(),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(*, code: str, redirect_uri: str, code_verifier: str) -> str:
    client_id, client_secret = _client_credentials()
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": code_verifier,
    }
    try:
        async with _new_client() as client:
            resp = await client.post(
                TOKEN_URL, data=form, headers={"Accept": "application/json"}
            )
    except httpx.HTTPError as exc:
        logger.warning("linuxdo token exchange network error: %s", exc)
        raise LinuxDOOAuthError("LINUX DO 登录服务连接失败，请稍后重试") from exc

    if resp.status_code != 200:
        logger.warning(
            "linuxdo token exchange failed: status=%s body=%s",
            resp.status_code,
            resp.text[:200],
        )
        raise LinuxDOOAuthError("LINUX DO 授权失败，请重新发起登录")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise LinuxDOOAuthError("LINUX DO 授权响应异常，请重新发起登录") from exc
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise LinuxDOOAuthError("LINUX DO 授权未返回访问令牌，请重新发起登录")
    return access_token


async def fetch_profile(access_token: str) -> Dict[str, Any]:
    try:
        async with _new_client() as client:
            resp = await client.get(
                USER_INFO_URL,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        logger.warning("linuxdo userinfo network error: %s", exc)
        raise LinuxDOOAuthError("获取 LINUX DO 用户信息失败，请稍后重试") from exc

    if resp.status_code != 200:
        logger.warning(
            "linuxdo userinfo failed: status=%s body=%s", resp.status_code, resp.text[:200]
        )
        raise LinuxDOOAuthError("获取 LINUX DO 用户信息失败，请重新发起登录")
    try:
        data = resp.json()
    except ValueError as exc:
        raise LinuxDOOAuthError("LINUX DO 用户信息响应异常，请重新发起登录") from exc
    return parse_profile(data)


def parse_profile(data: Any) -> Dict[str, Any]:
    """只强制要求 id（唯一不可变）；其余字段宽松解析。"""
    if not isinstance(data, dict):
        raise LinuxDOOAuthError("LINUX DO 用户信息格式异常")
    linuxdo_id = str(data.get("id") or "").strip()
    if not linuxdo_id:
        raise LinuxDOOAuthError("LINUX DO 用户信息缺少 id，无法登录")
    trust_level: Optional[int]
    try:
        trust_level = int(data.get("trust_level"))
    except (TypeError, ValueError):
        trust_level = None
    return {
        "id": linuxdo_id,
        "username": str(data.get("username") or "").strip(),
        "name": str(data.get("name") or "").strip(),
        "trust_level": trust_level,
    }
