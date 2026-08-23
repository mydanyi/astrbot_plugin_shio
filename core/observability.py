from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any


_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{16}$")
_SAFE_COMPONENT_RE = re.compile(r"^[a-z0-9_.-]{1,48}$")
_SAFE_FAILURE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_SAFE_TAXONOMY_RE = re.compile(r"^[a-z][a-z0-9_.:,|-]{0,79}$")

_SECRET_KEY_PARTS = (
    "token",
    "cookie",
    "authorization",
    "api_key",
    "apikey",
    "secret",
    "password",
    "base64",
    "request_body",
    "response_body",
    "full_request",
    "full_response",
    "prompt",
)
_RAW_ID_KEYS = {
    "sender_id",
    "user_id",
    "group_id",
    "message_id",
    "session_id",
    "scope_key",
    "sender_key",
    "target_message_id",
    "target_sender_id",
    "reply_to_message_id",
    "platform_message_id",
}
_CONTENT_KEYS = {
    "text",
    "content",
    "body",
    "current_message",
    "raw_reply",
    "final_reply",
    "completion",
    "request",
    "response",
}
_SAFE_STRING_KEYS = {
    "source_kind",
    "identity_status",
    "timestamp_source",
    "chat_type",
    "history_source",
    "memory_subject_model",
    "target_model",
    "fact_model",
    "reply_shape",
    "v2_mode",
    "v2_plan_status",
    "v2_plan_fact_model",
    "reason_code",
    "failure_kind",
    "typed_tool_result_capabilities",
    "action",
    "outcome",
    "trigger",
}
_SAFE_STRING_SUFFIXES = (
    "_status",
    "_kind",
    "_model",
    "_source",
    "_route",
    "_code",
    "_mode",
    "_capabilities",
)


def diagnostic_digest(value: Any) -> str:
    """Return an irreversible short digest for an identifier or opaque label."""

    normalized = str(value or "").strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]


def safe_exception_kind(exc: BaseException) -> str:
    """Expose only the exception class, never its potentially sensitive message."""

    name = type(exc).__name__
    return name if _SAFE_FAILURE_RE.fullmatch(name) else "Exception"


def _safe_string(key: str, value: Any) -> str | None:
    rendered = str(value or "").strip()
    if not rendered:
        return ""
    if key.endswith(("_digest", "_fingerprint")):
        return rendered if _DIGEST_RE.fullmatch(rendered) else None
    if key == "failure_kind":
        return rendered if _SAFE_FAILURE_RE.fullmatch(rendered) else None
    if key in _SAFE_STRING_KEYS or key.endswith(_SAFE_STRING_SUFFIXES):
        normalized = rendered.lower()
        return normalized if _SAFE_TAXONOMY_RE.fullmatch(normalized) else None
    return None


def sanitize_trace_metadata(
    metadata: Mapping[str, Any] | None,
) -> dict[str, str | int | float | bool | None]:
    """Apply a fail-closed metadata policy shared by traces and logs.

    Numeric counters and booleans are accepted unless their key names a raw
    principal/platform identifier. Strings need an explicit taxonomy-style key
    or a validated digest. Unknown strings are dropped instead of truncated.
    """

    safe: dict[str, str | int | float | bool | None] = {}
    for original_key, value in dict(metadata or {}).items():
        key = str(original_key or "").strip()[:64]
        lowered = key.lower()
        if not key:
            continue
        if any(part in lowered for part in _SECRET_KEY_PARTS):
            continue
        if lowered in _RAW_ID_KEYS or lowered in _CONTENT_KEYS:
            continue
        if isinstance(value, bool) or value is None:
            safe[key] = value
            continue
        if isinstance(value, (int, float)):
            safe[key] = value
            continue
        rendered = _safe_string(lowered, value)
        if rendered is not None:
            safe[key] = rendered
    return safe


def structured_log(
    logger: Any,
    level: str,
    component: str,
    *,
    trace_id: str = "",
    **metadata: Any,
) -> dict[str, str | int | float | bool | None]:
    """Write one content-free JSON diagnostic record and return its payload."""

    normalized_component = str(component or "unknown").strip().lower()
    if not _SAFE_COMPONENT_RE.fullmatch(normalized_component):
        normalized_component = "unknown"
    normalized_trace_id = str(trace_id or "").strip().lower()
    payload: dict[str, str | int | float | bool | None] = {
        "component": normalized_component,
        "trace_id": (
            normalized_trace_id if _TRACE_ID_RE.fullmatch(normalized_trace_id) else ""
        ),
        **sanitize_trace_metadata(metadata),
    }
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    method = getattr(logger, str(level or "info").lower(), None)
    if not callable(method):
        method = getattr(logger, "info")
    method("[Shio/trace] %s", rendered)
    return payload
