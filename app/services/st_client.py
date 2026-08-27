"""ST 上游调用客户端。

按用户提供的工作流模板，所有账号共用同一组输入字段：
- in-0: prompt
- in-1: Nano Banana Pro aspect ratio (e.g. 1:1)
- in-2: Nano Banana Pro resolution (e.g. 4K)
- in-3: model
- in-4: GPT Image 2 size
- in-5: GPT Image 2 quality
- in-6: image_url（图生图；文生图传空字符串）
"""
from __future__ import annotations

import asyncio
import email.utils
import logging
import os
import time
from typing import Any, AsyncGenerator, Dict, Optional

import httpx

from app.services.upstream_redaction import (
    redact_upstream_data,
    redact_upstream_event_text,
    redact_upstream_text,
)


logger = logging.getLogger(__name__)


class STError(Exception):
    """ST 调用错误。"""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        payload: Any = None,
        *,
        headers: Optional[Dict[str, str]] = None,
        retry_after: Optional[float] = None,
        request_id: Optional[str] = None,
        upstream_started: bool = False,
    ) -> None:
        # Error messages are public API data; remove arbitrary image/CDN URLs
        # as well as the upstream brand/domain before exposing them.
        self.message = redact_upstream_event_text(message)
        super().__init__(self.message)
        self.status_code = status_code
        self.payload = redact_upstream_data(payload)
        self.body = self.payload
        self.error_body = self.payload
        self.headers = dict(headers or {})
        self.response_headers = self.headers
        self.retry_after = retry_after
        self.retry_after_seconds = retry_after
        self.request_id = request_id
        self.upstream_request_id = request_id
        self.upstream_started = upstream_started


class STClient:
    """异步 ST 客户端（共享 AsyncClient + per-loop 复用）。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("ST_BASE_URL", "")).rstrip("/")
        # 传输层超时必须高于工作流的 230s 总预算，避免底层先断开；
        # 真正的工作流总时限由 generate.py 的 SSE 预算控制。
        self.timeout = max(
            270.0,
            float(timeout_seconds or os.getenv("ST_TIMEOUT_SECONDS", "270")),
        )
        self.stream_read_timeout = max(
            self.timeout,
            float(os.getenv("ST_STREAM_READ_TIMEOUT_SECONDS", "330")),
        )
        self.connect_timeout = float(os.getenv("ST_CONNECT_TIMEOUT_SECONDS", "10"))
        self.max_connections = max(1, int(os.getenv("HTTP_MAX_CONNECTIONS", "128")))
        self.max_keepalive_connections = max(
            1, int(os.getenv("HTTP_MAX_KEEPALIVE", str(self.max_connections)))
        )
        self.trust_env = os.getenv("ST_TRUST_ENV", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._client: Optional[httpx.AsyncClient] = None
        self._client_lock: Optional[asyncio.Lock] = None
        self._loop_id: Optional[int] = None

    def _endpoint(self, path: str) -> str:
        if not self.base_url:
            raise STError("上游服务未配置", status_code=503)
        return f"{self.base_url}{path}"

    @staticmethod
    def _is_img2img_payload(payload: Dict[str, Any]) -> bool:
        return bool(str((payload or {}).get("in-6") or "").strip())

    @staticmethod
    def _timeout_message(exc: httpx.TimeoutException) -> str:
        detail = str(exc).strip()
        if detail:
            return f"上游超时: {detail}"
        return f"上游超时 ({type(exc).__name__})"

    def _ensure_runtime_state(self) -> None:
        loop_id = id(asyncio.get_running_loop())
        if self._loop_id != loop_id:
            self._client = None
            self._client_lock = asyncio.Lock()
            self._loop_id = loop_id

    async def _get_client(self) -> httpx.AsyncClient:
        self._ensure_runtime_state()
        if self._client is None or self._client.is_closed:
            assert self._client_lock is not None
            async with self._client_lock:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(
                        timeout=httpx.Timeout(
                            connect=self.connect_timeout,
                            read=self.stream_read_timeout,
                            write=self.timeout,
                            pool=self.timeout,
                        ),
                        limits=httpx.Limits(
                            max_connections=self.max_connections,
                            max_keepalive_connections=min(
                                self.max_connections, self.max_keepalive_connections
                            ),
                        ),
                        trust_env=self.trust_env,
                    )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    @staticmethod
    def _retry_after(headers: Any) -> Optional[float]:
        """Parse Retry-After as seconds or an HTTP date."""
        raw = str((headers or {}).get("retry-after") or "").strip()
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                target = email.utils.parsedate_to_datetime(raw).timestamp()
                return max(0.0, target - time.time())
            except (TypeError, ValueError, OverflowError):
                return None

    @staticmethod
    def _request_id(headers: Any) -> Optional[str]:
        for key in ("x-request-id", "x-request-id", "request-id", "trace-id"):
            value = str((headers or {}).get(key) or "").strip()
            if value:
                return value[:256]
        return None

    @classmethod
    def _error_from_response(cls, resp: Any, body: bytes) -> "STError":
        headers = {str(k).lower(): str(v) for k, v in dict(getattr(resp, "headers", {}) or {}).items()}
        try:
            err_payload = httpx.Response(resp.status_code, content=body, headers=headers).json()
        except Exception:
            err_payload = {"raw": body.decode("utf-8", errors="replace")[:2000]}
        return STError(
            f"ST HTTP {resp.status_code}",
            status_code=resp.status_code,
            payload=err_payload,
            headers=headers,
            retry_after=cls._retry_after(headers),
            request_id=cls._request_id(headers),
        )

    async def run_inference(
        self,
        org_id: str,
        flow_id: str,
        api_key: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """调用 https://api.st.com/inference/v0/run/{org_id}/{flow_id}."""
        url = self._endpoint(f"/inference/v0/run/{org_id}/{flow_id}")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        client = await self._get_client()
        try:
            resp = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise STError(self._timeout_message(exc), status_code=504) from exc
        except httpx.HTTPError as exc:
            raise STError(f"上游连接错误: {exc}", status_code=502) from exc

        # 不强制 raise_for_status，需要把上游错误体回传给前端
        if resp.status_code >= 400:
            body = getattr(resp, "content", None)
            if body is None:
                body = str(getattr(resp, "text", "")).encode("utf-8", errors="replace")
            raise self._error_from_response(resp, body)

        try:
            return resp.json()
        except Exception as exc:
            raise STError(
                f"上游返回非 JSON: {exc}",
                status_code=502,
                payload={"raw": resp.text[:2000]},
                headers={str(k).lower(): str(v) for k, v in dict(resp.headers).items()},
                request_id=self._request_id(resp.headers),
            ) from exc

    async def stream_inference(
        self,
        org_id: str,
        flow_id: str,
        api_key: str,
        payload: Dict[str, Any],
    ) -> AsyncGenerator[str, None]:
        """流式调用 https://api.st.com/inference/v0/stream/{org_id}/{flow_id}.

        以行（按 `\\n` 分割）为单位 yield 上游下发的事件文本。
        失败时抛出 STError（不会再吐内容）。
        """
        url = self._endpoint(f"/inference/v0/stream/{org_id}/{flow_id}")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        try:
            # 文生图和图生图共用同一个有界连接池；生成并发已经由上层
            # semaphore 控制，避免每个图生图请求重新建立 TCP/TLS 池。
            client = await self._get_client()
            async for line in self._stream_with_client(client, url, headers, payload):
                yield line
        except httpx.TimeoutException as exc:
            raise STError(self._timeout_message(exc), status_code=504) from exc
        except httpx.HTTPError as exc:
            raise STError(f"上游连接错误: {exc}", status_code=502) from exc

    async def _stream_with_client(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
    ) -> AsyncGenerator[str, None]:
        async with client.stream(
            "POST",
            url,
            headers=headers,
            json=payload,
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise self._error_from_response(resp, body)

            buffer = ""

            def _yield_payload(raw_line: str) -> Optional[str]:
                """从 SSE 行里抽出 data 负载；非 data 行返回 None。"""
                line = raw_line.strip()
                if not line:
                    return None
                if line.startswith(":"):  # SSE 注释 / keepalive
                    return None
                if line.startswith("data:"):
                    payload = line[5:].strip()
                else:
                    # 兜底：上游可能未严格走 SSE 标准
                    payload = line
                if not payload or payload == "[DONE]":
                    return None
                return payload

            async for chunk in resp.aiter_text():
                buffer += chunk
                while "\n" in buffer:
                    raw, buffer = buffer.split("\n", 1)
                    event_payload = _yield_payload(raw)
                    if event_payload is not None:
                        yield event_payload
            if buffer.strip():
                event_payload = _yield_payload(buffer)
                if event_payload is not None:
                    yield event_payload

    async def fetch_run_detail(
        self,
        org_id: str,
        flow_id: str,
        api_key: str,
        run_id: str,
        max_pages: int = 3,
        page_size: int = 200,
    ) -> Optional[Dict[str, Any]]:
        """从 ST Analytics 接口拉取指定 run 的详情。

        endpoint: GET /analytics/org/{org_id}/flows/{flow_id}?page=&page_size=
        分页查找 run_id 匹配的记录。

        说明：ST 区分 Public / Private API Key，但相同 Bearer 通常都能访问；
        若 4xx 直接返回 None（让上层降级到诊断 dump 路径）。

        Returns:
            匹配的 run 字典；找不到 / 调用失败时返回 None。
        """
        if not run_id:
            return None
        target = str(run_id).strip()
        if not target:
            return None

        url = self._endpoint(f"/analytics/org/{org_id}/flows/{flow_id}")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        client = await self._get_client()
        page = 0
        try:
            while page < max_pages:
                resp = await client.get(
                    url,
                    headers=headers,
                    params={"page": page, "page_size": page_size},
                )
                if resp.status_code >= 400:
                    logger.warning(
                        "st analytics http %s on page %s",
                        resp.status_code,
                        page,
                    )
                    return None
                try:
                    runs = resp.json()
                except Exception as exc:
                    logger.warning("st analytics non-json: %s", redact_upstream_text(exc))
                    return None
                if not isinstance(runs, list) or not runs:
                    return None
                for run in runs:
                    if isinstance(run, dict):
                        rid = str(run.get("run_id") or "").strip()
                        if rid and rid == target:
                            return run
                if len(runs) < page_size:
                    break
                page += 1
        except httpx.HTTPError as exc:
            logger.warning("st analytics http error: %s", redact_upstream_text(exc))
            return None

        return None


_st_client: Optional[STClient] = None


def get_st_client() -> STClient:
    global _st_client
    if _st_client is None:
        _st_client = STClient()
    return _st_client


async def close_st_client() -> None:
    global _st_client
    if _st_client is not None:
        await _st_client.close()
        _st_client = None


# ---------- 输出解析 ----------

def extract_image_urls(response: Dict[str, Any]) -> list[str]:
    """尽量稳健地从 ST 返回里抽取生成图 URL。

    已知 ST 工作流返回的几种形态：
    - 文生图：{"outputs": {"out-0": '{"image_url": "https://..."}'}}
    - 图生图：{"outputs": {"out-0": '{"transformed_image_url": "![alt](https://...)"}'}}
    - 直接：{"outputs": {"out-0": "https://..."}} 或 {"out-0": {"image_url": "..."}}

    策略：
    1) 字符串里先尝试 JSON 解析（剥掉外层引号包装的 JSON）
    2) dict 的 value 走通用 walk；若 key 含 "url"/"image"，更激进抽取
    3) 字符串里用正则把 http(s) URL 都抓出来（兼容 markdown `![alt](url)` 和裸 URL）
    """
    import json as _json
    import re

    URL_PATTERN = re.compile(r"https?://[^\s)\"'<>]+", re.IGNORECASE)

    urls: list[str] = []

    def _looks_like_image_url(v: str) -> bool:
        low = v.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            return False
        return (
            low.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
            or "image" in low
            or "img" in low
            or "st" in low
            or "st" in low
            or "supabase" in low
            or "cloudfront" in low
            or "s3" in low
            or "/storage/" in low
        )

    def _is_image_field_key(key: str) -> bool:
        k = key.lower()
        return ("url" in k and ("image" in k or "img" in k)) or k in {
            "image_url",
            "img_url",
            "url",
            "image",
            "src",
            "transformed_image_url",
            "output_url",
        }

    def _emit_from_string(s: str) -> None:
        # 直接是 URL
        if _looks_like_image_url(s):
            urls.append(s)
            return
        # markdown / 混合文本里抽 URL
        for match in URL_PATTERN.findall(s):
            cleaned = match.rstrip(".,);]")
            if _looks_like_image_url(cleaned):
                urls.append(cleaned)

    def _walk(value: Any, *, parent_key_hints_image: bool = False) -> None:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    inner = _json.loads(stripped)
                    _walk(inner, parent_key_hints_image=parent_key_hints_image)
                    return
                except Exception:
                    pass
            _emit_from_string(stripped)
        elif isinstance(value, list):
            for item in value:
                _walk(item, parent_key_hints_image=parent_key_hints_image)
        elif isinstance(value, dict):
            for key, sub in value.items():
                _walk(sub, parent_key_hints_image=_is_image_field_key(str(key)))

    _walk(response)
    # 去重并保持顺序
    seen = set()
    deduped: list[str] = []
    for u in urls:
        if u not in seen:
            deduped.append(u)
            seen.add(u)
    return deduped
