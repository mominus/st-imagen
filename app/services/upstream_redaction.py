"""对外展示前，清除上游服务品牌与域名信息。"""
from __future__ import annotations

import re
from typing import Any


# 先替换完整域名，避免仅替换品牌词后留下 sb.st.com 等变体。
_UPSTREAM_DOMAIN_RE = re.compile(
    r"(?i)\b(?:[a-z0-9-]+\.)*stack-?ai\.com\b"
)
_UPSTREAM_NAME_RE = re.compile(r"(?i)\bstack-?ai\b")


def redact_upstream_text(value: Any) -> str:
    """将上游服务的名称和域名统一替换为 ``st``。"""
    text = str(value or "")
    text = _UPSTREAM_DOMAIN_RE.sub("st", text)
    return _UPSTREAM_NAME_RE.sub("st", text)


def redact_upstream_data(value: Any) -> Any:
    """递归脱敏上游 JSON 响应，且不修改传入对象。"""
    if isinstance(value, str):
        return redact_upstream_text(value)
    if isinstance(value, dict):
        return {key: redact_upstream_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_upstream_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_upstream_data(item) for item in value)
    return value
