"""出站 URL 安全校验与受限重定向处理。"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass
from typing import Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import httpx


OUTBOUND_MAX_REDIRECTS = max(0, int(os.getenv("OUTBOUND_MAX_REDIRECTS", "4")))
ALLOW_PRIVATE_OUTBOUND_URLS = os.getenv("ALLOW_PRIVATE_OUTBOUND_URLS", "").lower() in {
    "1",
    "true",
    "yes",
}
BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata",
}
BLOCKED_HOSTNAME_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
)


class UnsafeOutboundURLError(ValueError):
    """出站 URL 不安全或不可解析。"""


@dataclass(frozen=True)
class OutboundTarget:
    url: str
    scheme: str
    hostname: str
    port: Optional[int]
    resolved_ips: Tuple[str, ...] = ()


def _normalize_hostname(raw: str) -> str:
    host = str(raw or "").strip().rstrip(".")
    if not host:
        raise UnsafeOutboundURLError("URL 缺少主机名")
    try:
        host = host.encode("idna").decode("ascii")
    except Exception as exc:
        raise UnsafeOutboundURLError("URL 主机名格式无效") from exc
    return host.lower()


def parse_outbound_target(url: str) -> OutboundTarget:
    raw = str(url or "").strip()
    if not raw:
        raise UnsafeOutboundURLError("URL 不能为空")
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeOutboundURLError("仅允许 http(s) URL")
    if not parsed.hostname:
        raise UnsafeOutboundURLError("URL 缺少主机名")
    if parsed.username or parsed.password:
        raise UnsafeOutboundURLError("URL 不允许携带用户信息")
    hostname = _normalize_hostname(parsed.hostname)
    return OutboundTarget(
        url=raw,
        scheme=parsed.scheme.lower(),
        hostname=hostname,
        port=parsed.port,
    )


def _is_ip_public(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text)
    return bool(ip.is_global)


def _is_hostname_blocked(hostname: str) -> bool:
    if hostname in BLOCKED_HOSTNAMES:
        return True
    return any(hostname.endswith(suffix) for suffix in BLOCKED_HOSTNAME_SUFFIXES)


async def _resolve_hostname_ips(hostname: str) -> Set[str]:
    try:
        ipaddress.ip_address(hostname)
        return {hostname}
    except ValueError:
        pass

    def _lookup() -> Set[str]:
        resolved: Set[str] = set()
        infos = socket.getaddrinfo(
            hostname,
            None,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        for family, _socktype, _proto, _canonname, sockaddr in infos:
            if family == socket.AF_INET:
                resolved.add(sockaddr[0])
            elif family == socket.AF_INET6:
                resolved.add(sockaddr[0])
        return resolved

    try:
        resolved = await asyncio.to_thread(_lookup)
    except socket.gaierror as exc:
        raise UnsafeOutboundURLError(f"域名无法解析: {hostname}") from exc
    if not resolved:
        raise UnsafeOutboundURLError(f"域名无法解析: {hostname}")
    return resolved


async def ensure_safe_outbound_url(url: str) -> OutboundTarget:
    target = parse_outbound_target(url)
    if ALLOW_PRIVATE_OUTBOUND_URLS:
        return target
    if _is_hostname_blocked(target.hostname):
        raise UnsafeOutboundURLError(f"不允许访问该主机: {target.hostname}")

    resolved_ips = await _resolve_hostname_ips(target.hostname)
    blocked = sorted(ip for ip in resolved_ips if not _is_ip_public(ip))
    if blocked:
        raise UnsafeOutboundURLError(
            f"不允许访问内网或保留地址: {target.hostname} -> {', '.join(blocked[:4])}"
        )
    return OutboundTarget(
        url=target.url,
        scheme=target.scheme,
        hostname=target.hostname,
        port=target.port,
        resolved_ips=tuple(sorted(resolved_ips)),
    )


def _pinned_request_url(target: OutboundTarget) -> str:
    """Build a URL whose connection endpoint is an already-validated IP.

    Keeping the hostname in the URL after validation would let the HTTP client
    resolve it again, opening a DNS-rebinding window between validation and
    connect.  Host and TLS SNI are supplied separately by ``open_safe_stream``.
    """
    if not target.resolved_ips:
        return target.url
    parsed = urlparse(target.url)
    ip = target.resolved_ips[0]
    host = f"[{ip}]" if ":" in ip else ip
    netloc = f"{host}:{target.port}" if target.port is not None else host
    return urlunparse(parsed._replace(netloc=netloc))


def _host_header(target: OutboundTarget) -> str:
    host = f"[{target.hostname}]" if ":" in target.hostname else target.hostname
    return f"{host}:{target.port}" if target.port is not None else host


async def open_safe_stream(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    max_redirects: int = OUTBOUND_MAX_REDIRECTS,
    timeout: Optional[httpx.Timeout] = None,
) -> httpx.Response:
    current_url = str(url or "").strip()
    redirects = 0

    while True:
        target = await ensure_safe_outbound_url(current_url)
        request_url = _pinned_request_url(target)
        extensions = {"timeout": timeout.as_dict()} if timeout is not None else None
        if target.scheme == "https" and target.resolved_ips:
            extensions = dict(extensions or {})
            extensions["sni_hostname"] = target.hostname
        request_headers = dict(headers or {})
        if target.resolved_ips:
            request_headers["Host"] = _host_header(target)
            # The pool sees the pinned IP as the origin.  Do not let a TLS
            # connection opened with one hostname's SNI be reused for another
            # hostname that happens to resolve to the same address.
            request_headers["Connection"] = "close"
        request = client.build_request(
            method, request_url, headers=request_headers, extensions=extensions
        )
        response = await client.send(request, stream=True)

        location = response.headers.get("location")
        if response.is_redirect and location:
            if redirects >= max_redirects:
                await response.aclose()
                raise UnsafeOutboundURLError("重定向次数过多")
            # Resolve relative redirects against the logical hostname, not the
            # IP-pinned transport URL.
            next_url = urljoin(current_url, location)
            await response.aclose()
            current_url = next_url
            redirects += 1
            continue

        return response
