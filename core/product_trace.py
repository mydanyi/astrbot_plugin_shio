from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_UNSAFE_REASON_MARKERS = ("_id", "base64", "cookie", "query", "secret", "token", "url")


class ProductStage(str, Enum):
    INGRESS = "ingress"
    SENDER_SOURCE = "sender_source"
    MEMORY = "memory"
    MEDIA = "media"
    ADDRESS = "address"
    ATTENTION = "attention"
    PARTICIPATION = "participation"
    ACTION = "action"
    EVIDENCE = "evidence"
    CONTENT_INTENT = "content_intent"
    AFFECT = "affect"
    EXPRESSION = "expression"
    PRESENTATION_INTENT = "presentation_intent"
    VALIDATION = "validation"
    PRESENTATION_RECEIPT = "presentation_receipt"
    SEND = "send"
    TERMINAL = "terminal"


class ProductOutcome(str, Enum):
    ACCEPTED = "accepted"
    DROPPED = "dropped"
    NO_ACTION = "no_action"
    REACTED = "reacted"
    SENT = "sent"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    DEGRADED = "degraded"
    FAILED = "failed"


class ProductTraceStatus(str, Enum):
    ACCEPTED = "accepted"
    DROPPED = "dropped"
    NOT_REQUESTED = "not_requested"
    EMPTY = "empty"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    SELECTED = "selected"
    SKIPPED = "skipped"
    ATTEMPTED = "attempted"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


def _validate_reason_code(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return ""
    if not _REASON_CODE_RE.fullmatch(normalized):
        raise ValueError("product_trace_reason_code_invalid")
    if any(marker in normalized for marker in _UNSAFE_REASON_MARKERS):
        raise ValueError("product_trace_reason_code_unsafe")
    return normalized


def _validate_count(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"product_trace_{field_name}_invalid")
    return value


@dataclass(frozen=True, slots=True)
class ProductTracePayload:
    status: ProductTraceStatus | None = None
    reason_code: str = ""
    item_count: int = 0
    call_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    target_bound: bool = False
    current_subject_only: bool = False
    source_verified: bool = False
    degraded: bool = False

    def __post_init__(self) -> None:
        if self.status is not None and not isinstance(self.status, ProductTraceStatus):
            raise TypeError("product_trace_status_invalid")
        object.__setattr__(self, "reason_code", _validate_reason_code(self.reason_code))
        for field_name in ("item_count", "call_count", "success_count", "failure_count"):
            object.__setattr__(
                self,
                field_name,
                _validate_count(getattr(self, field_name), field_name),
            )

    def as_dict(self) -> dict[str, str | int | bool]:
        result: dict[str, str | int | bool] = {
            "item_count": self.item_count,
            "call_count": self.call_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "target_bound": self.target_bound,
            "current_subject_only": self.current_subject_only,
            "source_verified": self.source_verified,
            "degraded": self.degraded,
        }
        if self.status is not None:
            result["status"] = self.status.value
        if self.reason_code:
            result["reason_code"] = self.reason_code
        return result


@dataclass(frozen=True, slots=True)
class ProductTraceEvent:
    sequence: int
    stage: ProductStage
    elapsed_ms: float
    conversation_revision: int
    generation_epoch: int
    payload: ProductTracePayload
    outcome: ProductOutcome | None = None

    def __post_init__(self) -> None:
        _validate_count(self.sequence, "sequence")
        _validate_count(self.conversation_revision, "conversation_revision")
        _validate_count(self.generation_epoch, "generation_epoch")
        if isinstance(self.elapsed_ms, bool) or not isinstance(self.elapsed_ms, (int, float)):
            raise ValueError("product_trace_elapsed_invalid")
        if self.elapsed_ms < 0:
            raise ValueError("product_trace_elapsed_invalid")
        if not isinstance(self.stage, ProductStage):
            raise TypeError("product_trace_stage_invalid")
        if not isinstance(self.payload, ProductTracePayload):
            raise TypeError("product_trace_payload_invalid")
        if self.stage is ProductStage.TERMINAL:
            if self.outcome is None:
                raise ValueError("product_trace_terminal_outcome_required")
        elif self.outcome is not None:
            raise ValueError("product_trace_nonterminal_outcome_forbidden")

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "sequence": self.sequence,
            "stage": self.stage.value,
            "elapsed_ms": round(float(self.elapsed_ms), 3),
            "conversation_revision": self.conversation_revision,
            "generation_epoch": self.generation_epoch,
            **self.payload.as_dict(),
        }
        if self.outcome is not None:
            result["outcome"] = self.outcome.value
        return result


_STAGE_ORDER = {stage: index for index, stage in enumerate(ProductStage)}


@dataclass(frozen=True, slots=True)
class ProductTrace:
    trace_id: str
    conversation_revision: int
    generation_epoch: int
    _events: list[ProductTraceEvent] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        normalized_trace = str(self.trace_id or "").strip().lower()
        if not _TRACE_ID_RE.fullmatch(normalized_trace):
            raise ValueError("product_trace_id_invalid")
        object.__setattr__(self, "trace_id", normalized_trace)
        _validate_count(self.conversation_revision, "conversation_revision")
        _validate_count(self.generation_epoch, "generation_epoch")

    @property
    def events(self) -> tuple[ProductTraceEvent, ...]:
        return tuple(self._events)

    def append(
        self,
        stage: ProductStage,
        *,
        elapsed_ms: float,
        payload: ProductTracePayload,
    ) -> ProductTraceEvent:
        if not isinstance(stage, ProductStage) or stage is ProductStage.TERMINAL:
            raise ValueError("product_trace_stage_append_invalid")
        if self._events and self._events[-1].stage is ProductStage.TERMINAL:
            raise ValueError("product_trace_already_terminal")
        if any(event.stage is stage for event in self._events):
            raise ValueError("product_trace_stage_duplicate")
        if self._events and _STAGE_ORDER[stage] <= _STAGE_ORDER[self._events[-1].stage]:
            raise ValueError("product_trace_stage_out_of_order")
        if self._events and float(elapsed_ms) < self._events[-1].elapsed_ms:
            raise ValueError("product_trace_elapsed_out_of_order")
        event = ProductTraceEvent(
            sequence=len(self._events),
            stage=stage,
            elapsed_ms=elapsed_ms,
            conversation_revision=self.conversation_revision,
            generation_epoch=self.generation_epoch,
            payload=payload,
        )
        self._events.append(event)
        return event

    def terminate(
        self,
        outcome: ProductOutcome,
        *,
        elapsed_ms: float,
        reason_code: str,
    ) -> ProductTraceEvent:
        if not isinstance(outcome, ProductOutcome):
            raise TypeError("product_trace_outcome_invalid")
        if self._events and self._events[-1].stage is ProductStage.TERMINAL:
            raise ValueError("product_trace_already_terminal")
        stages = {event.stage for event in self._events}
        required = {
            ProductOutcome.DROPPED: {ProductStage.INGRESS},
            ProductOutcome.NO_ACTION: {ProductStage.ACTION},
            ProductOutcome.REACTED: {ProductStage.PRESENTATION_RECEIPT},
            ProductOutcome.BLOCKED: {ProductStage.VALIDATION},
            ProductOutcome.SENT: {
                ProductStage.PRESENTATION_INTENT,
                ProductStage.PRESENTATION_RECEIPT,
                ProductStage.SEND,
            },
        }.get(outcome, set())
        if not required.issubset(stages):
            raise ValueError("product_trace_terminal_evidence_missing")
        if self._events and float(elapsed_ms) < self._events[-1].elapsed_ms:
            raise ValueError("product_trace_elapsed_out_of_order")
        payload = ProductTracePayload(
            status=(
                ProductTraceStatus.SUCCEEDED
                if outcome in {ProductOutcome.ACCEPTED, ProductOutcome.REACTED, ProductOutcome.SENT}
                else ProductTraceStatus.FAILED
            ),
            reason_code=reason_code,
        )
        event = ProductTraceEvent(
            sequence=len(self._events),
            stage=ProductStage.TERMINAL,
            elapsed_ms=elapsed_ms,
            conversation_revision=self.conversation_revision,
            generation_epoch=self.generation_epoch,
            payload=payload,
            outcome=outcome,
        )
        self._events.append(event)
        return event

    def snapshot(self) -> dict[str, Any]:
        terminal_count = sum(event.stage is ProductStage.TERMINAL for event in self._events)
        if terminal_count != 1 or self._events[-1].stage is not ProductStage.TERMINAL:
            raise ValueError("product_trace_terminal_required")
        return {
            "schema_version": 1,
            "trace_id": self.trace_id,
            "conversation_revision": self.conversation_revision,
            "generation_epoch": self.generation_epoch,
            "events": [event.as_dict() for event in self._events],
        }
