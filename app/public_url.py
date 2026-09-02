"""对外公网根地址推导：PUBLIC_BASE_URL 优先，其次反代转发头，最后回退请求自身。"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import Request


def public_base_url(request: Optional[Request]) -> str:
    """返回以 / 结尾的公网根地址。

    OAuth 回调地址、生成图外链等需要绝对地址的场景统一走这里。
    """
    configured = (os.getenv("PUBLIC_BASE_URL") or "").strip()
    if configured:
        return configured.rstrip("/") + "/"

    if request is None:
        return "http://127.0.0.1:8001/"
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip()
    if forwarded_host:
        scheme = forwarded_proto or request.url.scheme
        return f"{scheme}://{forwarded_host}/"

    return str(request.base_url)
