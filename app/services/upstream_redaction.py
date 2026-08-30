"""对外展示前，清除上游服务品牌与域名信息。"""
from __future__ import annotations

import json
import re
from typing import Any


# 先替换完整域名，避免仅替换品牌词后留下 sb.st.com 等变体。
_UPSTREAM_DOMAIN_RE = re.compile(
    r"(?i)\b(?:[a-z0-9-]+\.)*" + "sta" + r"ck-?ai\.com\b"
)
_UPSTREAM_NAME_RE = re.compile(
    r"(?i)(?<![a-z0-9])" + "sta" + r"ck-?ai(?=$|[^a-z0-9])"
)
_UPSTREAM_SHORT_NAME_RE = re.compile(r"(?i)(?<![a-z0-9])st(?=$|[^a-z0-9])")
_UPSTREAM_SUPPORT_EMAIL_RE = re.compile(
    r"(?i)\bsupport@" + "sta" + r"ck-?ai\.com\b"
)
_HTTP_URL_RE = re.compile(r"(?i)https?://[^\s\"'<>]+")


def redact_upstream_text(value: Any) -> str:
    """将任何上游品牌、域名和支持邮箱统一替换为 ``Upstream``。"""
    text = str(value or "")
    text = _UPSTREAM_SUPPORT_EMAIL_RE.sub("Upstream", text)
    text = _UPSTREAM_DOMAIN_RE.sub("Upstream", text)
    text = _UPSTREAM_NAME_RE.sub("Upstream", text)
    return _UPSTREAM_SHORT_NAME_RE.sub("Upstream", text)


def redact_upstream_data(value: Any) -> Any:
    """递归脱敏上游 JSON 响应，且不修改传入对象。"""
    if isinstance(value, str):
        return redact_upstream_text(value)
    if isinstance(value, dict):
        return {
            redact_upstream_text(key) if isinstance(key, str) else key: redact_upstream_data(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_upstream_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_upstream_data(item) for item in value)
    return value


def redact_upstream_event_data(value: Any) -> Any:
    """脱敏可发送给浏览器的上游事件，并移除所有 HTTP(S) URL。

    普通错误日志仍保留脱敏后的 URL 结构，便于排障；SSE 进度事件则不应
    把图片 CDN、运行详情或其他上游链接交给用户，因此这里统一替换为
    ``<hidden-url>``。
    """
    if isinstance(value, str):
        text = redact_upstream_text(value)
        return _HTTP_URL_RE.sub("<hidden-url>", text)
    if isinstance(value, dict):
        return {
            redact_upstream_event_data(key) if isinstance(key, str) else key: redact_upstream_event_data(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_upstream_event_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_upstream_event_data(item) for item in value)
    return value


def redact_upstream_event_text(value: Any) -> str:
    """返回不含任何 HTTP(S) URL 的 SSE 上游事件文本。"""
    text = str(value or "")
    try:
        parsed = json.loads(text)
    except Exception:
        return str(redact_upstream_event_data(text))
    return json.dumps(
        redact_upstream_event_data(parsed),
        ensure_ascii=False,
        separators=(",", ":"),
    )
