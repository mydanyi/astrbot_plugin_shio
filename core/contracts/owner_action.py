"""Closed, persona-agnostic contracts for explicitly requested owner actions.

This module deliberately contains no runtime tool discovery, parameter mapping, or
execution bridge.  A later code-owned controller may issue canonical receipts through
the private issuer entry point after its adapter and executor checks have completed.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import weakref
from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass, field, replace
from enum import Enum
from typing import Any

from ..capability_policy import CapabilityClass
from ._validation import ContractViolation, normalize_reason_codes, require_hex
from .binding import DecisionBinding


MAX_ACTION_OUTPUT_TEXT_CHARS = 4096
MAX_ACTION_REQUEST_LIFETIME_SECONDS = 900.0
MAX_CONFIRMATION_TTL_SECONDS = 600.0

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,95}$")
_REASON = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")


class OwnerActionOperation(str, Enum):
    """Operations with a code-owned adapter shape; never arbitrary tool names."""

    ARTIFACT_READ_EXACT = "artifact_read_exact"
    ARTIFACT_GREP = "artifact_grep"
    MEMORY_WRITE_LITERAL = "memory_write_literal"
    SANDBOX_SHELL_ONCE = "sandbox_shell_once"


class ActionSideEffect(str, Enum):
    SCOPED_READ = "scoped_read"
    STATE_WRITE = "state_write"
    CODE_EXECUTION = "code_execution"


class ActionConfirmationPolicy(str, Enum):
    CURRENT_EXPLICIT_REQUEST = "current_explicit_request"
    SECOND_TURN_REQUIRED = "second_turn_required"


class ActionEffectState(str, Enum):
    NOT_STARTED = "not_started"
    NO_SIDE_EFFECT = "no_side_effect"
    NOT_COMMITTED = "not_committed"
    COMMITTED = "committed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class ActionReceiptStatus(str, Enum):
    CONFIRMATION_REQUIRED = "confirmation_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    STALE = "stale"
    TIMED_OUT = "timed_out"
    EFFECT_UNKNOWN = "effect_unknown"


class ActionOutputKind(str, Enum):
    SAFE_TEXT = "safe_text"
    ARTIFACT_REF = "artifact_ref"


class ActionOutputVisibility(str, Enum):
    OWNER_ONLY = "owner_only"
    # Kept as a closed negative case. P3-08A rejects it for every ActionOutput.
    PUBLIC = "public"


_OPERATION_CAPABILITY: dict[OwnerActionOperation, CapabilityClass] = {
    OwnerActionOperation.ARTIFACT_READ_EXACT: CapabilityClass.ARTIFACT_READ,
    OwnerActionOperation.ARTIFACT_GREP: CapabilityClass.ARTIFACT_READ,
    OwnerActionOperation.MEMORY_WRITE_LITERAL: CapabilityClass.MEMORY_WRITE,
    OwnerActionOperation.SANDBOX_SHELL_ONCE: CapabilityClass.SHELL_EXEC,
}

_OPERATION_SIDE_EFFECT: dict[OwnerActionOperation, ActionSideEffect] = {
    OwnerActionOperation.ARTIFACT_READ_EXACT: ActionSideEffect.SCOPED_READ,
    OwnerActionOperation.ARTIFACT_GREP: ActionSideEffect.SCOPED_READ,
    OwnerActionOperation.MEMORY_WRITE_LITERAL: ActionSideEffect.STATE_WRITE,
    OwnerActionOperation.SANDBOX_SHELL_ONCE: ActionSideEffect.CODE_EXECUTION,
}

_OPERATION_CONFIRMATION_POLICIES: dict[
    OwnerActionOperation, frozenset[ActionConfirmationPolicy]
] = {
    OwnerActionOperation.ARTIFACT_READ_EXACT: frozenset(
        {ActionConfirmationPolicy.CURRENT_EXPLICIT_REQUEST}
    ),
    OwnerActionOperation.ARTIFACT_GREP: frozenset(
        {ActionConfirmationPolicy.CURRENT_EXPLICIT_REQUEST}
    ),
    OwnerActionOperation.MEMORY_WRITE_LITERAL: frozenset(
        {
            ActionConfirmationPolicy.CURRENT_EXPLICIT_REQUEST,
            ActionConfirmationPolicy.SECOND_TURN_REQUIRED,
        }
    ),
    OwnerActionOperation.SANDBOX_SHELL_ONCE: frozenset(
        {ActionConfirmationPolicy.SECOND_TURN_REQUIRED}
    ),
}

_MODEL_PROPOSAL_FIELDS = frozenset(
    {"capability_intent", "operation_intent", "confidence", "reason_codes"}
)
_MODEL_AUTHORITY_FIELDS = frozenset(
    {
        "action_id",
        "adapter_id",
        "adapter_version",
        "args",
        "arguments",
        "binding",
        "budget",
        "call_budget",
        "command",
        "confirmation",
        "confirmation_policy",
        "confirmation_token",
        "deadline",
        "exact_tool",
        "idempotency",
        "idempotency_key",
        "is_owner",
        "key",
        "message_id",
        "owner",
        "parameter_digest",
        "parameters",
        "path",
        "plugin_source",
        "principal",
        "request_digest",
        "scope_key",
        "sender",
        "sender_id",
        "sender_key",
        "session_id",
        "source",
        "timeout",
        "token",
        "tool",
        "tool_name",
    }
)


def _require_string(value: Any, field_name: str) -> str:
    if type(value) is not str:
        raise ContractViolation(f"{field_name}_invalid")
    normalized = value.strip()
    if not normalized:
        raise ContractViolation(f"{field_name}_required")
    return normalized


def _require_safe_name(value: Any, field_name: str) -> str:
    normalized = _require_string(value, field_name).lower()
    if not _SAFE_NAME.fullmatch(normalized):
        raise ContractViolation(f"{field_name}_invalid")
    return normalized


def _require_digest(value: Any, field_name: str, *, optional: bool = False) -> str:
    if optional and value == "":
        return ""
    if type(value) is not str:
        raise ContractViolation(f"{field_name}_invalid")
    return require_hex(value, field_name, lengths=(64,))


def _require_time(value: Any, field_name: str, *, allow_zero: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractViolation(f"{field_name}_invalid")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ContractViolation(f"{field_name}_invalid")
    if parsed < 0.0 or (not allow_zero and parsed == 0.0):
        raise ContractViolation(f"{field_name}_invalid")
    return parsed


def _require_score(value: Any, field_name: str) -> float:
    parsed = _require_time(value, field_name)
    if parsed > 1.0:
        raise ContractViolation(f"{field_name}_out_of_range")
    return parsed


def _require_enum(value: Any, enum_type: type[Enum], field_name: str) -> Any:
    if not isinstance(value, enum_type):
        raise ContractViolation(f"{field_name}_invalid")
    return value


def _require_binding(value: Any, field_name: str = "binding") -> DecisionBinding:
    if not isinstance(value, DecisionBinding):
        raise ContractViolation(f"{field_name}_invalid")
    return value


def _normalize_reason_values(values: Any) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise ContractViolation("reason_codes_invalid")
    values = tuple(values)
    if any(type(value) is not str for value in values):
        raise ContractViolation("reason_codes_invalid")
    normalized = normalize_reason_codes(values)
    if any(not _REASON.fullmatch(value) for value in normalized):
        raise ContractViolation("reason_code_invalid")
    return normalized


def _assert_exact_binding(expected: DecisionBinding, current: DecisionBinding) -> None:
    _require_binding(current, "current_binding")
    if current != expected:
        raise ContractViolation("decision_binding_mismatch")


def _assert_confirmation_binding(
    origin: DecisionBinding,
    confirmation: DecisionBinding,
) -> None:
    _require_binding(confirmation, "confirmation_binding")
    if confirmation.scope_key != origin.scope_key:
        raise ContractViolation("confirmation_scope_mismatch")
    if confirmation.session_id != origin.session_id:
        raise ContractViolation("confirmation_session_mismatch")
    if confirmation.current_sender_key != origin.current_sender_key:
        raise ContractViolation("confirmation_sender_mismatch")
    if confirmation.current_message_id == origin.current_message_id:
        raise ContractViolation("confirmation_message_not_new")
    if confirmation.current_content_digest == origin.current_content_digest:
        raise ContractViolation("confirmation_content_not_new")
    if confirmation.conversation_revision <= origin.conversation_revision:
        raise ContractViolation("confirmation_revision_stale")
    if confirmation.generation_epoch <= origin.generation_epoch:
        raise ContractViolation("confirmation_epoch_stale")


def _require_operation_shape(
    *,
    capability: CapabilityClass,
    operation: OwnerActionOperation,
    side_effect: ActionSideEffect | None = None,
    confirmation_policy: ActionConfirmationPolicy | None = None,
) -> None:
    _require_enum(capability, CapabilityClass, "capability")
    _require_enum(operation, OwnerActionOperation, "operation")
    if _OPERATION_CAPABILITY[operation] is not capability:
        raise ContractViolation("operation_capability_mismatch")
    if side_effect is not None:
        _require_enum(side_effect, ActionSideEffect, "side_effect")
        if _OPERATION_SIDE_EFFECT[operation] is not side_effect:
            raise ContractViolation("operation_side_effect_mismatch")
    if confirmation_policy is not None:
        _require_enum(
            confirmation_policy,
            ActionConfirmationPolicy,
            "confirmation_policy",
        )
        if confirmation_policy not in _OPERATION_CONFIRMATION_POLICIES[operation]:
            raise ContractViolation("operation_confirmation_policy_mismatch")


@dataclass(frozen=True, slots=True, repr=False)
class OwnerActionProposal:
    """Untrusted model proposal: broad capability plus one closed operation only."""

    binding: DecisionBinding = field(repr=False)
    capability: CapabilityClass
    operation: OwnerActionOperation
    confidence: float
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "binding", _require_binding(self.binding))
        _require_operation_shape(
            capability=self.capability,
            operation=self.operation,
        )
        object.__setattr__(
            self,
            "confidence",
            _require_score(self.confidence, "confidence"),
        )
        object.__setattr__(
            self,
            "reason_codes",
            _normalize_reason_values(self.reason_codes),
        )

    def assert_current_binding(self, current: DecisionBinding) -> None:
        _assert_exact_binding(self.binding, current)

    def trace_metadata(self) -> dict[str, str | float | int]:
        return {
            "schema_version": 1,
            "capability": self.capability.value,
            "operation": self.operation.value,
            "confidence": round(self.confidence, 3),
            "reason_count": len(self.reason_codes),
        }

    def __repr__(self) -> str:
        return (
            "OwnerActionProposal("
            f"capability={self.capability.value!r}, "
            f"operation={self.operation.value!r}, "
            f"confidence={self.confidence:.3f}, "
            f"reason_count={len(self.reason_codes)}, binding_sealed=True)"
        )


def parse_owner_action_proposal(
    binding: DecisionBinding,
    payload: Mapping[str, Any],
) -> OwnerActionProposal:
    """Parse the deliberately tiny model-facing proposal surface."""

    _require_binding(binding)
    if not isinstance(payload, Mapping):
        raise ContractViolation("model_action_payload_invalid")
    normalized: dict[str, Any] = {}
    for raw_key, value in payload.items():
        if type(raw_key) is not str:
            raise ContractViolation("model_action_key_invalid")
        key = raw_key.strip().lower()
        if not key:
            raise ContractViolation("model_action_key_invalid")
        if key in normalized:
            raise ContractViolation("model_action_key_duplicate")
        normalized[key] = value
    forbidden = sorted(set(normalized).intersection(_MODEL_AUTHORITY_FIELDS))
    if forbidden:
        raise ContractViolation("model_authority_field_forbidden")
    if set(normalized).difference(_MODEL_PROPOSAL_FIELDS):
        raise ContractViolation("model_action_field_unknown")
    required = {"capability_intent", "operation_intent", "confidence"}
    if not required.issubset(normalized):
        raise ContractViolation("model_action_field_required")
    capability_raw = _require_string(
        normalized["capability_intent"],
        "capability_intent",
    ).lower()
    operation_raw = _require_string(
        normalized["operation_intent"],
        "operation_intent",
    ).lower()
    try:
        capability = CapabilityClass(capability_raw)
    except ValueError as exc:
        raise ContractViolation("capability_intent_invalid") from exc
    try:
        operation = OwnerActionOperation(operation_raw)
    except ValueError as exc:
        raise ContractViolation("operation_intent_invalid") from exc
    reasons = normalized.get("reason_codes", ())
    if not isinstance(reasons, (list, tuple)):
        raise ContractViolation("reason_codes_invalid")
    return OwnerActionProposal(
        binding=binding,
        capability=capability,
        operation=operation,
        confidence=_require_score(normalized["confidence"], "confidence"),
        reason_codes=tuple(reasons),
    )


@dataclass(frozen=True, slots=True, repr=False)
class OwnerActionRequest:
    """Adapter-selected request with only digests for its hidden parameters."""

    binding: DecisionBinding = field(repr=False)
    action_id: str = field(repr=False)
    adapter_id: str
    adapter_version: str
    request_digest: str = field(repr=False)
    parameter_digest: str = field(repr=False)
    capability: CapabilityClass
    operation: OwnerActionOperation
    side_effect: ActionSideEffect
    issued_at: float = field(repr=False)
    deadline: float = field(repr=False)
    confirmation_policy: ActionConfirmationPolicy
    idempotency_key: str = field(repr=False)
    call_budget: int = 1
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "binding", _require_binding(self.binding))
        object.__setattr__(
            self,
            "action_id",
            _require_digest(self.action_id, "action_id"),
        )
        object.__setattr__(
            self,
            "adapter_id",
            _require_safe_name(self.adapter_id, "adapter_id"),
        )
        object.__setattr__(
            self,
            "adapter_version",
            _require_safe_name(self.adapter_version, "adapter_version"),
        )
        object.__setattr__(
            self,
            "request_digest",
            _require_digest(self.request_digest, "request_digest"),
        )
        object.__setattr__(
            self,
            "parameter_digest",
            _require_digest(self.parameter_digest, "parameter_digest"),
        )
        _require_operation_shape(
            capability=self.capability,
            operation=self.operation,
            side_effect=self.side_effect,
            confirmation_policy=self.confirmation_policy,
        )
        issued_at = _require_time(self.issued_at, "issued_at")
        deadline = _require_time(self.deadline, "deadline", allow_zero=False)
        if deadline <= issued_at:
            raise ContractViolation("deadline_not_after_issue")
        if deadline - issued_at > MAX_ACTION_REQUEST_LIFETIME_SECONDS:
            raise ContractViolation("deadline_lifetime_exceeded")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "deadline", deadline)
        object.__setattr__(
            self,
            "idempotency_key",
            _require_digest(self.idempotency_key, "idempotency_key"),
        )
        if type(self.call_budget) is not int or self.call_budget != 1:
            raise ContractViolation("call_budget_invalid")
        object.__setattr__(
            self,
            "reason_codes",
            _normalize_reason_values(self.reason_codes),
        )

    def assert_current_binding(self, current: DecisionBinding) -> None:
        _assert_exact_binding(self.binding, current)

    def is_expired(self, now: float) -> bool:
        return _require_time(now, "now") >= self.deadline

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "capability": self.capability.value,
            "operation": self.operation.value,
            "side_effect": self.side_effect.value,
            "confirmation_required": (
                self.confirmation_policy
                is ActionConfirmationPolicy.SECOND_TURN_REQUIRED
            ),
            "has_parameter_digest": True,
            "reason_count": len(self.reason_codes),
        }

    def __repr__(self) -> str:
        return (
            "OwnerActionRequest("
            f"capability={self.capability.value!r}, "
            f"operation={self.operation.value!r}, "
            f"side_effect={self.side_effect.value!r}, "
            f"confirmation_policy={self.confirmation_policy.value!r}, "
            "binding_bound=True, has_parameter_digest=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class PendingConfirmation:
    """Immutable shape for a confirmation awaiting the same owner and scope."""

    request: OwnerActionRequest = field(repr=False)
    binding: DecisionBinding = field(repr=False)
    action_id: str = field(repr=False)
    sender_key: str = field(repr=False)
    scope_key: str = field(repr=False)
    request_digest: str = field(repr=False)
    parameter_digest: str = field(repr=False)
    pending_id: str = field(repr=False)
    issued_at: float = field(repr=False)
    expires_at: float = field(repr=False)
    confirmation_binding: DecisionBinding | None = field(default=None, repr=False)
    confirmation_proof_digest: str = field(default="", repr=False)
    consumed_at: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, OwnerActionRequest):
            raise ContractViolation("pending_request_invalid")
        if (
            self.request.confirmation_policy
            is not ActionConfirmationPolicy.SECOND_TURN_REQUIRED
        ):
            raise ContractViolation("pending_request_confirmation_not_required")
        binding = _require_binding(self.binding)
        if binding != self.request.binding:
            raise ContractViolation("pending_binding_mismatch")
        action_id = _require_digest(self.action_id, "action_id")
        if action_id != self.request.action_id:
            raise ContractViolation("pending_action_mismatch")
        sender_key = _require_string(self.sender_key, "sender_key")
        scope_key = _require_string(self.scope_key, "scope_key")
        if sender_key != binding.current_sender_key:
            raise ContractViolation("pending_sender_mismatch")
        if scope_key != binding.scope_key:
            raise ContractViolation("pending_scope_mismatch")
        request_digest = _require_digest(self.request_digest, "request_digest")
        if request_digest != self.request.request_digest:
            raise ContractViolation("pending_request_digest_mismatch")
        parameter_digest = _require_digest(self.parameter_digest, "parameter_digest")
        if parameter_digest != self.request.parameter_digest:
            raise ContractViolation("pending_parameter_digest_mismatch")
        pending_id = _require_digest(self.pending_id, "pending_id")
        issued_at = _require_time(self.issued_at, "issued_at")
        expires_at = _require_time(self.expires_at, "expires_at", allow_zero=False)
        if issued_at < self.request.issued_at:
            raise ContractViolation("pending_issued_before_request")
        if expires_at <= issued_at:
            raise ContractViolation("pending_expiry_invalid")
        if expires_at > self.request.deadline:
            raise ContractViolation("pending_expiry_after_request_deadline")
        if expires_at - issued_at > MAX_CONFIRMATION_TTL_SECONDS:
            raise ContractViolation("pending_ttl_exceeded")

        confirmation = self.confirmation_binding
        proof = _require_digest(
            self.confirmation_proof_digest,
            "confirmation_proof_digest",
            optional=True,
        )
        consumed_at = _require_time(self.consumed_at, "consumed_at")
        if confirmation is None:
            if proof or consumed_at != 0.0:
                raise ContractViolation("pending_unconsumed_shape_invalid")
        else:
            _assert_confirmation_binding(binding, confirmation)
            if not proof:
                raise ContractViolation("confirmation_proof_digest_required")
            if consumed_at < issued_at or consumed_at >= expires_at:
                raise ContractViolation("confirmation_consumed_time_invalid")

        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "sender_key", sender_key)
        object.__setattr__(self, "scope_key", scope_key)
        object.__setattr__(self, "request_digest", request_digest)
        object.__setattr__(self, "parameter_digest", parameter_digest)
        object.__setattr__(self, "pending_id", pending_id)
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "confirmation_proof_digest", proof)
        object.__setattr__(self, "consumed_at", consumed_at)

    @classmethod
    def from_request(
        cls,
        request: OwnerActionRequest,
        *,
        pending_id: str,
        issued_at: float,
        expires_at: float,
    ) -> "PendingConfirmation":
        if not isinstance(request, OwnerActionRequest):
            raise ContractViolation("pending_request_invalid")
        return cls(
            request=request,
            binding=request.binding,
            action_id=request.action_id,
            sender_key=request.binding.current_sender_key,
            scope_key=request.binding.scope_key,
            request_digest=request.request_digest,
            parameter_digest=request.parameter_digest,
            pending_id=pending_id,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    @property
    def consumed(self) -> bool:
        return self.confirmation_binding is not None

    def consume_confirmation(
        self,
        *,
        confirmation_binding: DecisionBinding,
        request_digest: str,
        parameter_digest: str,
        confirmation_proof_digest: str,
        now: float,
    ) -> "PendingConfirmation":
        if self.consumed:
            raise ContractViolation("confirmation_already_consumed")
        current_digest = _require_digest(request_digest, "request_digest")
        if current_digest != self.request_digest:
            raise ContractViolation("confirmation_request_digest_mismatch")
        current_parameter_digest = _require_digest(
            parameter_digest,
            "parameter_digest",
        )
        if current_parameter_digest != self.parameter_digest:
            raise ContractViolation("confirmation_parameter_digest_mismatch")
        proof = _require_digest(
            confirmation_proof_digest,
            "confirmation_proof_digest",
        )
        consumed_at = _require_time(now, "now")
        if consumed_at < self.issued_at or consumed_at >= self.expires_at:
            raise ContractViolation("confirmation_expired")
        _assert_confirmation_binding(self.binding, confirmation_binding)
        return replace(
            self,
            confirmation_binding=confirmation_binding,
            confirmation_proof_digest=proof,
            consumed_at=consumed_at,
        )

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "capability": self.request.capability.value,
            "operation": self.request.operation.value,
            "consumed": self.consumed,
            "has_request_digest": True,
            "has_parameter_digest": True,
        }

    def __repr__(self) -> str:
        return (
            "PendingConfirmation("
            f"capability={self.request.capability.value!r}, "
            f"operation={self.request.operation.value!r}, "
            f"consumed={self.consumed}, identity_bound=True)"
        )


_ACTION_OUTPUT_AUTHORITY_LOCK = threading.RLock()
_PENDING_ACTION_OUTPUT_SEALS: set[object] = set()
_CANONICAL_ACTION_OUTPUTS: weakref.WeakValueDictionary[int, "ActionOutput"] = (
    weakref.WeakValueDictionary()
)
_CANONICAL_ACTION_OUTPUT_SNAPSHOTS: weakref.WeakKeyDictionary[
    "ActionOutput",
    tuple[Any, ...],
] = weakref.WeakKeyDictionary()
_ACTION_OUTPUT_REQUESTS: weakref.WeakKeyDictionary[
    "ActionOutput",
    OwnerActionRequest,
] = weakref.WeakKeyDictionary()
_ACTION_OUTPUT_REQUEST_SNAPSHOTS: weakref.WeakKeyDictionary[
    "ActionOutput",
    tuple[Any, ...],
] = weakref.WeakKeyDictionary()
_CANONICAL_ACTION_OUTPUT_MARKER = object()
_PUBLIC_ACTION_OUTPUT_MARKER = object()


@dataclass(frozen=True, slots=True, repr=False, eq=False, weakref_slot=True)
class ActionOutput:
    """Bounded owner-only output shape.

    Public construction deliberately creates data only, never output authority.
    The private module issuer binds one exact request object and a complete immutable
    snapshot.  No production caller currently owns its issuance permit.
    """

    binding: DecisionBinding = field(repr=False)
    request_digest: str = field(repr=False)
    kind: ActionOutputKind
    visibility: ActionOutputVisibility
    safe_text: str = field(default="", repr=False)
    artifact_ref: str = field(default="", repr=False)
    truncated: bool = False
    output_digest: str = field(init=False, repr=False)
    _issuer_seal: InitVar[object | None] = None
    _canonical_marker: object = field(init=False, repr=False, compare=False)

    def __post_init__(self, _issuer_seal: object | None) -> None:
        canonical = False
        if _issuer_seal is not None:
            with _ACTION_OUTPUT_AUTHORITY_LOCK:
                if _issuer_seal not in _PENDING_ACTION_OUTPUT_SEALS:
                    raise ContractViolation("action_output_issuer_seal_invalid")
                _PENDING_ACTION_OUTPUT_SEALS.remove(_issuer_seal)
            canonical = True
        object.__setattr__(
            self,
            "_canonical_marker",
            (
                _CANONICAL_ACTION_OUTPUT_MARKER
                if canonical
                else _PUBLIC_ACTION_OUTPUT_MARKER
            ),
        )
        object.__setattr__(self, "binding", _require_binding(self.binding))
        object.__setattr__(
            self,
            "request_digest",
            _require_digest(self.request_digest, "request_digest"),
        )
        _require_enum(self.kind, ActionOutputKind, "output_kind")
        _require_enum(self.visibility, ActionOutputVisibility, "output_visibility")
        if self.visibility is not ActionOutputVisibility.OWNER_ONLY:
            raise ContractViolation("action_output_visibility_forbidden")
        if type(self.truncated) is not bool:
            raise ContractViolation("action_output_truncated_invalid")

        safe_text = self.safe_text
        artifact_ref = self.artifact_ref
        if type(safe_text) is not str or type(artifact_ref) is not str:
            raise ContractViolation("action_output_payload_invalid")
        if self.kind is ActionOutputKind.SAFE_TEXT:
            if not safe_text.strip():
                raise ContractViolation("action_output_text_required")
            if len(safe_text) > MAX_ACTION_OUTPUT_TEXT_CHARS:
                raise ContractViolation("action_output_text_too_long")
            if any(ord(char) < 32 and char not in "\t\r\n" for char in safe_text):
                raise ContractViolation("action_output_text_control_character")
            if artifact_ref:
                raise ContractViolation("action_output_shape_invalid")
            digest_material = b"safe_text\0" + safe_text.encode("utf-8")
        else:
            if safe_text:
                raise ContractViolation("action_output_shape_invalid")
            artifact_ref = _require_digest(artifact_ref, "artifact_ref")
            if self.truncated:
                raise ContractViolation("artifact_ref_truncated_invalid")
            digest_material = b"artifact_ref\0" + artifact_ref.encode("ascii")
        object.__setattr__(self, "safe_text", safe_text)
        object.__setattr__(self, "artifact_ref", artifact_ref)
        object.__setattr__(
            self,
            "output_digest",
            hashlib.sha256(digest_material).hexdigest(),
        )

    @property
    def is_canonical(self) -> bool:
        with _ACTION_OUTPUT_AUTHORITY_LOCK:
            try:
                snapshot = _CANONICAL_ACTION_OUTPUT_SNAPSHOTS.get(self)
                request = _ACTION_OUTPUT_REQUESTS.get(self)
                request_snapshot = _ACTION_OUTPUT_REQUEST_SNAPSHOTS.get(self)
                return bool(
                    self._canonical_marker is _CANONICAL_ACTION_OUTPUT_MARKER
                    and _CANONICAL_ACTION_OUTPUTS.get(id(self)) is self
                    and snapshot is not None
                    and request is not None
                    and request_snapshot is not None
                    and _action_output_integrity_snapshot(self) == snapshot
                    and _owner_action_request_output_snapshot(request)
                    == request_snapshot
                    and self.binding is request.binding
                    and self.request_digest == request.request_digest
                )
            except Exception:
                return False

    @classmethod
    def from_safe_text(
        cls,
        *,
        binding: DecisionBinding,
        request_digest: str,
        text: str,
        truncated: bool = False,
    ) -> "ActionOutput":
        return cls(
            binding=binding,
            request_digest=request_digest,
            kind=ActionOutputKind.SAFE_TEXT,
            visibility=ActionOutputVisibility.OWNER_ONLY,
            safe_text=text,
            truncated=truncated,
        )

    @classmethod
    def from_artifact_ref(
        cls,
        *,
        binding: DecisionBinding,
        request_digest: str,
        artifact_ref: str,
    ) -> "ActionOutput":
        return cls(
            binding=binding,
            request_digest=request_digest,
            kind=ActionOutputKind.ARTIFACT_REF,
            visibility=ActionOutputVisibility.OWNER_ONLY,
            artifact_ref=artifact_ref,
        )

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "kind": self.kind.value,
            "visibility": self.visibility.value,
            "character_count": len(self.safe_text),
            "has_artifact_ref": bool(self.artifact_ref),
            "truncated": self.truncated,
            "canonical_authority": self.is_canonical,
        }

    def __repr__(self) -> str:
        return (
            "ActionOutput("
            f"kind={self.kind.value!r}, visibility={self.visibility.value!r}, "
            f"character_count={len(self.safe_text)}, "
            f"has_artifact_ref={bool(self.artifact_ref)}, "
            f"truncated={self.truncated}, lineage_bound=True)"
        )


_RECEIPT_ISSUANCE_LOCK = threading.Lock()
_PENDING_RECEIPT_SEALS: set[object] = set()
_CANONICAL_RECEIPTS: weakref.WeakValueDictionary[int, "ActionReceipt"] = (
    weakref.WeakValueDictionary()
)
_CANONICAL_RECEIPT_SNAPSHOTS: weakref.WeakKeyDictionary[
    "ActionReceipt",
    tuple[Any, ...],
] = weakref.WeakKeyDictionary()
_CANONICAL_RECEIPT_MARKER = object()


def _integrity_scalar(value: Any) -> tuple[type[Any], Any]:
    """Snapshot a scalar without allowing ``str`` enums or bool/int aliases."""

    return type(value), value


def _binding_integrity_snapshot(binding: Any) -> tuple[Any, ...]:
    if type(binding) is not DecisionBinding:
        return ("invalid_binding", type(binding), id(binding))
    return (
        "decision_binding_v1",
        id(binding),
        _integrity_scalar(binding.scope_key),
        _integrity_scalar(binding.session_id),
        _integrity_scalar(binding.current_message_id),
        _integrity_scalar(binding.current_sender_key),
        _integrity_scalar(binding.current_content_digest),
        _integrity_scalar(binding.conversation_revision),
        _integrity_scalar(binding.generation_epoch),
        _integrity_scalar(binding.trace_id),
    )


def _action_output_integrity_snapshot(output: Any) -> tuple[Any, ...] | None:
    if output is None:
        return None
    if type(output) is not ActionOutput:
        return ("invalid_output", type(output), id(output))
    return (
        "action_output_v1",
        id(output),
        _binding_integrity_snapshot(output.binding),
        _integrity_scalar(output.request_digest),
        (type(output.kind), id(output.kind)),
        (type(output.visibility), id(output.visibility)),
        _integrity_scalar(output.safe_text),
        _integrity_scalar(output.artifact_ref),
        _integrity_scalar(output.truncated),
        _integrity_scalar(output.output_digest),
        (type(output._canonical_marker), id(output._canonical_marker)),
    )


def _owner_action_request_output_snapshot(request: Any) -> tuple[Any, ...]:
    """Complete immutable request snapshot for exact output authority binding."""

    if type(request) is not OwnerActionRequest:
        return ("invalid_request", type(request), id(request))
    reasons = request.reason_codes
    reason_snapshot = (
        type(reasons),
        tuple(_integrity_scalar(reason) for reason in reasons)
        if type(reasons) is tuple
        else (type(reasons), id(reasons)),
    )
    return (
        "action_output_request_v1",
        id(request),
        _binding_integrity_snapshot(request.binding),
        _integrity_scalar(request.action_id),
        _integrity_scalar(request.adapter_id),
        _integrity_scalar(request.adapter_version),
        _integrity_scalar(request.request_digest),
        _integrity_scalar(request.parameter_digest),
        (type(request.capability), id(request.capability)),
        (type(request.operation), id(request.operation)),
        (type(request.side_effect), id(request.side_effect)),
        _integrity_scalar(request.issued_at),
        _integrity_scalar(request.deadline),
        (type(request.confirmation_policy), id(request.confirmation_policy)),
        _integrity_scalar(request.idempotency_key),
        _integrity_scalar(request.call_budget),
        reason_snapshot,
    )


def _issue_action_output(
    *,
    request: OwnerActionRequest,
    kind: ActionOutputKind,
    safe_text: str = "",
    artifact_ref: str = "",
    truncated: bool = False,
    _test_only: bool = False,
) -> ActionOutput:
    """Private exact-request issuer; production has no live issuance permit yet.

    E3C1A intentionally leaves this hard-off.  The private test-only branch exists
    solely to exercise receipt/output lineage until a same-open-handle runtime proof
    can replace it.  It is omitted from ``__all__``.
    """

    if type(_test_only) is not bool or not _test_only:
        raise ContractViolation("action_output_live_authority_unavailable")
    if type(request) is not OwnerActionRequest:
        raise ContractViolation("action_output_request_invalid")
    request_snapshot = _owner_action_request_output_snapshot(request)
    seal = object()
    with _ACTION_OUTPUT_AUTHORITY_LOCK:
        _PENDING_ACTION_OUTPUT_SEALS.add(seal)
    try:
        output = ActionOutput(
            binding=request.binding,
            request_digest=request.request_digest,
            kind=kind,
            visibility=ActionOutputVisibility.OWNER_ONLY,
            safe_text=safe_text,
            artifact_ref=artifact_ref,
            truncated=truncated,
            _issuer_seal=seal,
        )
    finally:
        with _ACTION_OUTPUT_AUTHORITY_LOCK:
            _PENDING_ACTION_OUTPUT_SEALS.discard(seal)
    with _ACTION_OUTPUT_AUTHORITY_LOCK:
        _CANONICAL_ACTION_OUTPUTS[id(output)] = output
        _CANONICAL_ACTION_OUTPUT_SNAPSHOTS[output] = (
            _action_output_integrity_snapshot(output)
        )
        _ACTION_OUTPUT_REQUESTS[output] = request
        _ACTION_OUTPUT_REQUEST_SNAPSHOTS[output] = request_snapshot
    return output


def _issue_test_action_output(
    request: OwnerActionRequest,
    text: str,
    *,
    truncated: bool = False,
) -> ActionOutput:
    """Private deterministic output authority used only by local contract tests."""

    return _issue_action_output(
        request=request,
        kind=ActionOutputKind.SAFE_TEXT,
        safe_text=text,
        truncated=truncated,
        _test_only=True,
    )


def _open_action_output(
    output: ActionOutput,
    *,
    request: OwnerActionRequest,
) -> ActionOutput:
    """Validate exact output/request identity without exposing payload authority."""

    if type(output) is not ActionOutput or type(request) is not OwnerActionRequest:
        raise ContractViolation("action_output_not_canonical")
    with _ACTION_OUTPUT_AUTHORITY_LOCK:
        canonical_request = _ACTION_OUTPUT_REQUESTS.get(output)
        request_snapshot = _ACTION_OUTPUT_REQUEST_SNAPSHOTS.get(output)
        output_snapshot = _CANONICAL_ACTION_OUTPUT_SNAPSHOTS.get(output)
        if (
            canonical_request is not request
            or _CANONICAL_ACTION_OUTPUTS.get(id(output)) is not output
            or output_snapshot is None
            or request_snapshot is None
            or _action_output_integrity_snapshot(output) != output_snapshot
            or _owner_action_request_output_snapshot(request) != request_snapshot
            or output.binding is not request.binding
            or output.request_digest != request.request_digest
            or output._canonical_marker is not _CANONICAL_ACTION_OUTPUT_MARKER
        ):
            raise ContractViolation("action_output_not_canonical")
    return output


def _action_receipt_integrity_snapshot(receipt: Any) -> tuple[Any, ...]:
    """Return the complete exact-object snapshot used by both issuers and controllers."""

    if type(receipt) is not ActionReceipt:
        return ("invalid_receipt", type(receipt), id(receipt))
    reasons = receipt.reason_codes
    reason_snapshot = (
        type(reasons),
        tuple(_integrity_scalar(reason) for reason in reasons)
        if type(reasons) is tuple
        else (type(reasons), id(reasons)),
    )
    return (
        "action_receipt_v1",
        _binding_integrity_snapshot(receipt.binding),
        _binding_integrity_snapshot(receipt.origin_binding),
        _integrity_scalar(receipt.action_id),
        _integrity_scalar(receipt.request_digest),
        _integrity_scalar(receipt.adapter_id),
        _integrity_scalar(receipt.adapter_version),
        (type(receipt.capability), id(receipt.capability)),
        (type(receipt.operation), id(receipt.operation)),
        (type(receipt.side_effect), id(receipt.side_effect)),
        (type(receipt.confirmation_policy), id(receipt.confirmation_policy)),
        (type(receipt.status), id(receipt.status)),
        (type(receipt.effect_state), id(receipt.effect_state)),
        _integrity_scalar(receipt.idempotency_key),
        _integrity_scalar(receipt.source_interface_digest),
        _integrity_scalar(receipt.result_digest),
        _action_output_integrity_snapshot(receipt.output),
        _integrity_scalar(receipt.pending_confirmation_id),
        _integrity_scalar(receipt.confirmation_proof_digest),
        _integrity_scalar(receipt.started_at),
        _integrity_scalar(receipt.completed_at),
        _integrity_scalar(receipt.attempt_count),
        reason_snapshot,
        (type(receipt._canonical_marker), id(receipt._canonical_marker)),
    )


@dataclass(frozen=True, slots=True, repr=False, eq=False, weakref_slot=True)
class ActionReceipt:
    """Canonical terminal or confirmation-required result of one typed request."""

    binding: DecisionBinding = field(repr=False)
    action_id: str = field(repr=False)
    request_digest: str = field(repr=False)
    adapter_id: str
    adapter_version: str
    capability: CapabilityClass
    operation: OwnerActionOperation
    side_effect: ActionSideEffect
    confirmation_policy: ActionConfirmationPolicy
    status: ActionReceiptStatus
    effect_state: ActionEffectState
    idempotency_key: str = field(repr=False)
    source_interface_digest: str = field(repr=False)
    result_digest: str = field(default="", repr=False)
    output: ActionOutput | None = field(default=None, repr=False)
    pending_confirmation_id: str = field(default="", repr=False)
    confirmation_proof_digest: str = field(default="", repr=False)
    started_at: float = field(default=0.0, repr=False)
    completed_at: float = field(default=0.0, repr=False)
    attempt_count: int = 0
    reason_codes: tuple[str, ...] = ()
    origin_binding: DecisionBinding | None = field(default=None, repr=False)
    _issuer_seal: InitVar[object | None] = None
    _canonical_marker: object = field(init=False, repr=False, compare=False)

    def __post_init__(self, _issuer_seal: object | None) -> None:
        with _RECEIPT_ISSUANCE_LOCK:
            if _issuer_seal is None or _issuer_seal not in _PENDING_RECEIPT_SEALS:
                raise ContractViolation("issuer_seal_invalid")
            _PENDING_RECEIPT_SEALS.remove(_issuer_seal)
        object.__setattr__(self, "_canonical_marker", _CANONICAL_RECEIPT_MARKER)

        binding = _require_binding(self.binding)
        origin = binding if self.origin_binding is None else _require_binding(
            self.origin_binding,
            "origin_binding",
        )
        action_id = _require_digest(self.action_id, "action_id")
        request_digest = _require_digest(self.request_digest, "request_digest")
        adapter_id = _require_safe_name(self.adapter_id, "adapter_id")
        adapter_version = _require_safe_name(
            self.adapter_version,
            "adapter_version",
        )
        _require_operation_shape(
            capability=self.capability,
            operation=self.operation,
            side_effect=self.side_effect,
            confirmation_policy=self.confirmation_policy,
        )
        _require_enum(self.status, ActionReceiptStatus, "receipt_status")
        _require_enum(self.effect_state, ActionEffectState, "effect_state")
        idempotency_key = _require_digest(self.idempotency_key, "idempotency_key")
        source_interface_digest = _require_digest(
            self.source_interface_digest,
            "source_interface_digest",
            optional=True,
        )
        result_digest = _require_digest(
            self.result_digest,
            "result_digest",
            optional=True,
        )
        pending_confirmation_id = _require_digest(
            self.pending_confirmation_id,
            "pending_confirmation_id",
            optional=True,
        )
        proof_digest = _require_digest(
            self.confirmation_proof_digest,
            "confirmation_proof_digest",
            optional=True,
        )
        started_at = _require_time(self.started_at, "started_at")
        completed_at = _require_time(self.completed_at, "completed_at")
        if type(self.attempt_count) is not int or self.attempt_count not in (0, 1):
            raise ContractViolation("attempt_count_invalid")
        reasons = _normalize_reason_values(self.reason_codes)
        if self.output is not None and type(self.output) is not ActionOutput:
            raise ContractViolation("action_output_invalid")
        if self.output is not None:
            if not self.output.is_canonical:
                raise ContractViolation("action_output_not_canonical")
            if self.output.binding is not binding:
                raise ContractViolation("action_output_binding_mismatch")
            if self.output.request_digest != request_digest:
                raise ContractViolation("action_output_request_digest_mismatch")

        self._validate_binding_lineage(
            binding=binding,
            origin=origin,
            pending_confirmation_id=pending_confirmation_id,
            confirmation_proof_digest=proof_digest,
        )
        self._validate_status_shape(
            result_digest=result_digest,
            source_interface_digest=source_interface_digest,
            confirmation_proof_digest=proof_digest,
            started_at=started_at,
            completed_at=completed_at,
            reason_codes=reasons,
        )

        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "origin_binding", origin)
        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "request_digest", request_digest)
        object.__setattr__(self, "adapter_id", adapter_id)
        object.__setattr__(self, "adapter_version", adapter_version)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(
            self,
            "source_interface_digest",
            source_interface_digest,
        )
        object.__setattr__(self, "result_digest", result_digest)
        object.__setattr__(
            self,
            "pending_confirmation_id",
            pending_confirmation_id,
        )
        object.__setattr__(self, "confirmation_proof_digest", proof_digest)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "reason_codes", reasons)

    def _validate_binding_lineage(
        self,
        *,
        binding: DecisionBinding,
        origin: DecisionBinding,
        pending_confirmation_id: str,
        confirmation_proof_digest: str,
    ) -> None:
        before_confirmation = self.status in {
            ActionReceiptStatus.CONFIRMATION_REQUIRED,
            ActionReceiptStatus.CANCELLED,
        } or (
            self.status is ActionReceiptStatus.STALE
            and self.effect_state is ActionEffectState.NOT_STARTED
        ) or (
            self.status is ActionReceiptStatus.DENIED
            and not confirmation_proof_digest
        )
        if self.confirmation_policy is ActionConfirmationPolicy.CURRENT_EXPLICIT_REQUEST:
            if binding != origin:
                raise ContractViolation("receipt_binding_mismatch")
            if confirmation_proof_digest:
                raise ContractViolation("unexpected_confirmation_proof")
            if pending_confirmation_id:
                raise ContractViolation("unexpected_pending_confirmation")
            return
        if not pending_confirmation_id:
            raise ContractViolation("pending_confirmation_id_required")
        if before_confirmation:
            if self.status is ActionReceiptStatus.CONFIRMATION_REQUIRED:
                if binding != origin:
                    raise ContractViolation("pending_receipt_binding_mismatch")
            elif binding != origin:
                _assert_confirmation_binding(origin, binding)
            if confirmation_proof_digest:
                raise ContractViolation("pending_receipt_confirmation_proof_forbidden")
            return
        _assert_confirmation_binding(origin, binding)
        if not confirmation_proof_digest:
            raise ContractViolation("confirmation_proof_digest_required")

    def _validate_status_shape(
        self,
        *,
        result_digest: str,
        source_interface_digest: str,
        confirmation_proof_digest: str,
        started_at: float,
        completed_at: float,
        reason_codes: tuple[str, ...],
    ) -> None:
        status = self.status
        effect = self.effect_state
        output = self.output
        executed = status in {
            ActionReceiptStatus.SUCCEEDED,
            ActionReceiptStatus.FAILED,
            ActionReceiptStatus.TIMED_OUT,
            ActionReceiptStatus.EFFECT_UNKNOWN,
        } or (status is ActionReceiptStatus.STALE and effect is not ActionEffectState.NOT_STARTED)

        if output is not None and status is not ActionReceiptStatus.SUCCEEDED:
            raise ContractViolation("action_output_status_invalid")
        if status is not ActionReceiptStatus.SUCCEEDED and not reason_codes:
            raise ContractViolation("receipt_reason_required")
        if executed:
            if (
                self.attempt_count != 1
                or started_at <= 0.0
                or completed_at < started_at
                or not source_interface_digest
                or not result_digest
            ):
                raise ContractViolation("receipt_execution_time_invalid")
        elif (
            self.attempt_count != 0
            or started_at != 0.0
            or completed_at != 0.0
            or source_interface_digest
            or result_digest
        ):
            raise ContractViolation("receipt_unstarted_time_invalid")

        if status is ActionReceiptStatus.CONFIRMATION_REQUIRED:
            if (
                self.confirmation_policy
                is not ActionConfirmationPolicy.SECOND_TURN_REQUIRED
                or effect is not ActionEffectState.NOT_STARTED
                or output is not None
            ):
                raise ContractViolation("confirmation_required_shape_invalid")
            return

        if status is ActionReceiptStatus.SUCCEEDED:
            if self.side_effect is ActionSideEffect.SCOPED_READ:
                if effect is not ActionEffectState.NO_SIDE_EFFECT or output is None:
                    raise ContractViolation("read_success_shape_invalid")
            elif effect is not ActionEffectState.COMMITTED:
                raise ContractViolation("mutation_success_shape_invalid")
            return

        if status is ActionReceiptStatus.FAILED:
            allowed = (
                {ActionEffectState.NO_SIDE_EFFECT}
                if self.side_effect is ActionSideEffect.SCOPED_READ
                else {ActionEffectState.NOT_COMMITTED, ActionEffectState.PARTIAL}
            )
            if effect not in allowed:
                raise ContractViolation("failed_effect_state_invalid")
            return

        if status in {
            ActionReceiptStatus.DENIED,
            ActionReceiptStatus.CANCELLED,
        }:
            if effect is not ActionEffectState.NOT_STARTED:
                raise ContractViolation("unstarted_receipt_shape_invalid")
            return

        if status is ActionReceiptStatus.STALE:
            if effect is ActionEffectState.NOT_STARTED:
                return
            allowed = (
                {ActionEffectState.NO_SIDE_EFFECT}
                if self.side_effect is ActionSideEffect.SCOPED_READ
                else {
                    ActionEffectState.NOT_COMMITTED,
                    ActionEffectState.COMMITTED,
                    ActionEffectState.PARTIAL,
                    ActionEffectState.UNKNOWN,
                }
            )
            if effect not in allowed:
                raise ContractViolation("stale_effect_state_invalid")
            return

        if status is ActionReceiptStatus.TIMED_OUT:
            allowed = (
                {ActionEffectState.NO_SIDE_EFFECT}
                if self.side_effect is ActionSideEffect.SCOPED_READ
                else {
                    ActionEffectState.NOT_COMMITTED,
                    ActionEffectState.PARTIAL,
                    ActionEffectState.UNKNOWN,
                }
            )
            if effect not in allowed:
                raise ContractViolation("timed_out_effect_state_invalid")
            return

        if status is ActionReceiptStatus.EFFECT_UNKNOWN:
            if self.side_effect is ActionSideEffect.SCOPED_READ:
                raise ContractViolation("read_effect_unknown_invalid")
            if effect is not ActionEffectState.UNKNOWN:
                raise ContractViolation("effect_unknown_state_invalid")
            return

        raise ContractViolation("receipt_status_unhandled")

    @property
    def succeeded(self) -> bool:
        return self.status is ActionReceiptStatus.SUCCEEDED

    @property
    def is_canonical(self) -> bool:
        with _RECEIPT_ISSUANCE_LOCK:
            snapshot = _CANONICAL_RECEIPT_SNAPSHOTS.get(self)
            return (
                self._canonical_marker is _CANONICAL_RECEIPT_MARKER
                and _CANONICAL_RECEIPTS.get(id(self)) is self
                and snapshot is not None
                and _action_receipt_integrity_snapshot(self) == snapshot
            )

    def assert_current_binding(self, current: DecisionBinding) -> None:
        _assert_exact_binding(self.binding, current)

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "capability": self.capability.value,
            "operation": self.operation.value,
            "side_effect": self.side_effect.value,
            "confirmation_required": (
                self.confirmation_policy
                is ActionConfirmationPolicy.SECOND_TURN_REQUIRED
            ),
            "has_confirmation_proof": bool(self.confirmation_proof_digest),
            "status": self.status.value,
            "effect_state": self.effect_state.value,
            "has_output": self.output is not None,
            "attempt_count": self.attempt_count,
            "reason_count": len(self.reason_codes),
            "has_source_attestation": bool(self.source_interface_digest),
        }

    def __repr__(self) -> str:
        return (
            "ActionReceipt("
            f"capability={self.capability.value!r}, "
            f"operation={self.operation.value!r}, "
            f"side_effect={self.side_effect.value!r}, "
            f"status={self.status.value!r}, "
            f"effect_state={self.effect_state.value!r}, "
            f"has_output={self.output is not None}, "
            f"attempt_count={self.attempt_count}, "
            f"issuer_constructed={self.is_canonical}, lineage_bound=True)"
        )


def _issue_action_receipt(
    *,
    request: OwnerActionRequest,
    completion_binding: DecisionBinding,
    pending_confirmation: PendingConfirmation | None,
    status: ActionReceiptStatus,
    effect_state: ActionEffectState,
    source_interface_digest: str,
    result_digest: str = "",
    output: ActionOutput | None = None,
    confirmation_proof_digest: str = "",
    started_at: float = 0.0,
    completed_at: float = 0.0,
    attempt_count: int = 0,
    reason_codes: tuple[str, ...] = (),
) -> ActionReceipt:
    """Private issuer entry for a future controller; not a public execution API."""

    if not isinstance(request, OwnerActionRequest):
        raise ContractViolation("receipt_request_invalid")
    _require_binding(completion_binding, "completion_binding")
    if output is not None:
        _open_action_output(output, request=request)
    pending_confirmation_id = ""
    if request.confirmation_policy is ActionConfirmationPolicy.SECOND_TURN_REQUIRED:
        if not isinstance(pending_confirmation, PendingConfirmation):
            raise ContractViolation("receipt_pending_confirmation_required")
        if pending_confirmation.request is not request:
            raise ContractViolation("receipt_pending_request_identity_mismatch")
        pending_confirmation_id = pending_confirmation.pending_id
        before_confirmation = status in {
            ActionReceiptStatus.CONFIRMATION_REQUIRED,
            ActionReceiptStatus.CANCELLED,
        } or (
            status is ActionReceiptStatus.STALE
            and effect_state is ActionEffectState.NOT_STARTED
        ) or (
            status is ActionReceiptStatus.DENIED
            and not confirmation_proof_digest
        )
        if before_confirmation:
            if pending_confirmation.consumed:
                raise ContractViolation("receipt_pending_already_consumed")
            if status is ActionReceiptStatus.CONFIRMATION_REQUIRED:
                if completion_binding != request.binding:
                    raise ContractViolation("receipt_pending_shape_mismatch")
            elif completion_binding != request.binding:
                _assert_confirmation_binding(request.binding, completion_binding)
            if confirmation_proof_digest:
                raise ContractViolation("receipt_pending_shape_mismatch")
        else:
            if not pending_confirmation.consumed:
                raise ContractViolation("receipt_confirmation_not_consumed")
            if completion_binding != pending_confirmation.confirmation_binding:
                raise ContractViolation("receipt_confirmation_binding_mismatch")
            if (
                confirmation_proof_digest
                != pending_confirmation.confirmation_proof_digest
            ):
                raise ContractViolation("receipt_confirmation_proof_mismatch")
    elif pending_confirmation is not None:
        raise ContractViolation("receipt_pending_confirmation_forbidden")
    issuer_seal = object()
    with _RECEIPT_ISSUANCE_LOCK:
        _PENDING_RECEIPT_SEALS.add(issuer_seal)
    try:
        receipt = ActionReceipt(
            binding=completion_binding,
            origin_binding=request.binding,
            action_id=request.action_id,
            request_digest=request.request_digest,
            adapter_id=request.adapter_id,
            adapter_version=request.adapter_version,
            capability=request.capability,
            operation=request.operation,
            side_effect=request.side_effect,
            confirmation_policy=request.confirmation_policy,
            status=status,
            effect_state=effect_state,
            idempotency_key=request.idempotency_key,
            source_interface_digest=source_interface_digest,
            result_digest=result_digest,
            output=output,
            pending_confirmation_id=pending_confirmation_id,
            confirmation_proof_digest=confirmation_proof_digest,
            started_at=started_at,
            completed_at=completed_at,
            attempt_count=attempt_count,
            reason_codes=reason_codes,
            _issuer_seal=issuer_seal,
        )
    finally:
        with _RECEIPT_ISSUANCE_LOCK:
            _PENDING_RECEIPT_SEALS.discard(issuer_seal)
    with _RECEIPT_ISSUANCE_LOCK:
        _CANONICAL_RECEIPTS[id(receipt)] = receipt
        _CANONICAL_RECEIPT_SNAPSHOTS[receipt] = (
            _action_receipt_integrity_snapshot(receipt)
        )
    return receipt


__all__ = [
    "ActionConfirmationPolicy",
    "ActionEffectState",
    "ActionOutput",
    "ActionOutputKind",
    "ActionOutputVisibility",
    "ActionReceipt",
    "ActionReceiptStatus",
    "ActionSideEffect",
    "OwnerActionOperation",
    "OwnerActionProposal",
    "OwnerActionRequest",
    "PendingConfirmation",
    "parse_owner_action_proposal",
]
