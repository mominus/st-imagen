"""Decide whether a generation attempt consumed user quota.

Admission failures, transport failures and workflow timeouts are free. A request
is billable once the upstream workflow reports node execution, a node-level
error, or generated output. This policy is shared by synchronous and SSE paths.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


_NODE_ERROR_MARKERS = (
    "error in node",
    "error during node",
    "node execution failed",
)
_FREE_TIMEOUT_REASONS = frozenset({"idle_timeout", "total_timeout"})


def _walk_values(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_values(item)


def contains_node_error(value: Any) -> bool:
    """Return whether an upstream payload explicitly reports a node failure."""
    return any(
        any(marker in item.lower() for marker in _NODE_ERROR_MARKERS)
        for item in _walk_values(value)
        if isinstance(item, str)
    )


def is_free_workflow_timeout(value: Any) -> bool:
    """Recognize server-generated idle/total workflow timeout payloads."""
    return any(
        isinstance(item, str) and item.strip().lower() in _FREE_TIMEOUT_REASONS
        for item in _walk_values(value)
    )


def upstream_event_consumes_quota(event: Any) -> bool:
    """Detect evidence that an SSE/sync event entered executable workflow nodes."""
    if contains_node_error(event):
        return True
    if not isinstance(event, Mapping):
        return False
    if event.get("outputs") or event.get("images") or event.get("image_url"):
        return True
    progress = event.get("progress_data")
    if not isinstance(progress, Mapping):
        return False
    if str(progress.get("current_node") or "").strip():
        return True
    for key in ("started_nodes", "completed_nodes"):
        value = progress.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return True
    return False


def upstream_error_consumes_quota(message: Any, payload: Any = None) -> bool:
    """Classify an exception without charging connection/config/timeout failures."""
    if is_free_workflow_timeout(payload):
        return False
    return contains_node_error(message) or contains_node_error(payload)
