from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from .observability import sanitize_trace_metadata


PIPELINE_TRACE_EXTRA = "_shio_pipeline_trace"
PIPELINE_TRACE_CONTEXT_EXTRA = "_shio_pipeline_trace_context_v2"


@dataclass(frozen=True, slots=True)
class TraceStage:
    stage: str
    elapsed_ms: float
    metadata: dict[str, str | int | float | bool | None]

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "elapsed_ms": self.elapsed_ms,
            **self.metadata,
        }


@dataclass(slots=True)
class TraceContext:
    trace_id: str
    started_at: float
    stages: list[TraceStage] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "stages": [stage.as_dict() for stage in self.stages],
        }


def content_fingerprint(value: str) -> str:
    """Return a short irreversible fingerprint without retaining chat text."""

    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def identity_fingerprint(value: str) -> str:
    """Fingerprint a runtime identity before exposing it to diagnostic tests."""

    normalized = str(value or "").strip()
    return content_fingerprint(normalized) if normalized else ""


def _get_trace(event: Any) -> dict[str, Any] | None:
    trace = event.get_extra(PIPELINE_TRACE_EXTRA, None)
    return trace if isinstance(trace, dict) else None


def get_trace_context(event: Any) -> TraceContext | None:
    context = event.get_extra(PIPELINE_TRACE_CONTEXT_EXTRA, None)
    return context if isinstance(context, TraceContext) else None


def get_trace_id(event: Any) -> str:
    context = get_trace_context(event)
    return context.trace_id if context is not None else ""


def start_pipeline_trace(
    event: Any,
    *,
    current_message: str,
    sender_id: str,
    group_id: str,
    chat_type: str,
    is_owner: bool,
    envelope_metadata: dict[str, Any] | None = None,
    trace_id: str = "",
) -> None:
    """Create an event-local, content-free trace used by regression tests."""

    bound_trace_id = str(trace_id or "").strip().lower()
    if bound_trace_id and not re.fullmatch(r"[0-9a-f]{32}", bound_trace_id):
        raise ValueError("pipeline_trace_id_invalid")
    context = TraceContext(
        trace_id=bound_trace_id or secrets.token_hex(16),
        started_at=time.perf_counter(),
    )
    event.set_extra(PIPELINE_TRACE_CONTEXT_EXTRA, context)
    event.set_extra(PIPELINE_TRACE_EXTRA, context.snapshot())
    input_metadata: dict[str, Any] = {
        "message_fingerprint": content_fingerprint(current_message),
        "message_chars": len(str(current_message or "")),
        "sender_fingerprint": identity_fingerprint(sender_id),
        "group_fingerprint": identity_fingerprint(group_id),
        "chat_type": str(chat_type or "unknown")[:24],
        "is_owner": bool(is_owner),
    }
    input_metadata.update(dict(envelope_metadata or {}))
    record_pipeline_stage(
        event,
        "input",
        **input_metadata,
    )


def record_pipeline_stage(event: Any, stage: str, **metadata: Any) -> None:
    """Append one sanitized stage snapshot to the current event trace."""

    context = get_trace_context(event)
    if context is None:
        return
    safe_metadata = sanitize_trace_metadata(metadata)
    elapsed_ms = round((time.perf_counter() - context.started_at) * 1000, 3)
    context.stages.append(
        TraceStage(
            stage=str(stage or "unknown")[:48],
            elapsed_ms=elapsed_ms,
            metadata=safe_metadata,
        )
    )
    event.set_extra(PIPELINE_TRACE_EXTRA, context.snapshot())


def get_pipeline_trace(event: Any) -> dict[str, Any]:
    """Return a shallow diagnostic snapshot for tests and local inspection."""

    context = get_trace_context(event)
    if context is not None:
        return context.snapshot()
    trace = _get_trace(event)
    if trace is None:
        return {}
    return {
        "trace_id": str(trace.get("trace_id", "")),
        "stages": [dict(item) for item in trace.get("stages", []) if isinstance(item, dict)],
    }
