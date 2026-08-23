"""Canonical authority and idempotency controller for explicit owner actions.

The public request, pending-confirmation, lease, and receipt objects are deliberately
parameter-free shapes.  The controller keeps the exact typed parameter record and the
exact D-owned adapter draft material private, consumes only its canonical accepted-turn
consumer ticket, and issues receipts only through the contracts module's private issuer.

This module does not discover tools and does not execute anything.  The sealed
executor claims a one-shot :class:`ExecutionLease` and returns an opaque blueprint;
``complete_execution`` accepts no caller-authored status, effect, result, or output.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import threading
import weakref
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from .accepted_turn_authority import (
    AcceptedTurnAuthority,
    AcceptedTurnConsumer,
    AcceptedTurnContext,
    AcceptedTurnTicket,
)
from .action_planner import PlannedAction, PlannedActionAuthority, StructuralOutcome
from .capability_policy import CapabilityClass
from .contracts import (
    ActionKind,
    ActionConfirmationPolicy,
    ActionEffectState,
    ActionReceipt,
    ActionReceiptStatus,
    ActionSideEffect,
    ContractViolation,
    DecisionBinding,
    OwnerActionOperation,
    OwnerActionRequest,
    PendingConfirmation,
    SenderKind,
)
from .contracts._validation import normalize_reason_codes, require_hex
from .contracts.owner_action import (
    _action_receipt_integrity_snapshot,
    _issue_action_receipt,
)
from .conversation_event import PluginSource
from .identity import build_scope_key, build_sender_key
from .owner_action_adapters import (
    AdapterDraft,
    AdapterDraftRejected,
    OWNER_ACTION_ADAPTER_REGISTRY,
    _open_adapter_draft,
)
from .owner_action_router import (
    OwnerActionRouteDecision,
    OwnerActionRouteStatus,
    OwnerActionRouter,
)


_TRUSTED_OWNER_VERIFICATION_SOURCE = (
    "astrbot_event_sender_id:configured_owner_id"
)
_DEFAULT_MAX_LEDGER_ENTRIES = 256
_MAX_PENDING_RESOLUTION_MESSAGE_BYTES = 64


class OwnerActionLedgerState(str, Enum):
    PREPARED = "prepared"
    CONFIRMATION_REQUIRED = "confirmation_required"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    TERMINAL = "terminal"


class PendingDecision(str, Enum):
    CONFIRM = "confirm"
    DENY = "deny"
    CANCEL = "cancel"


class PendingMatchStatus(str, Enum):
    NONE = "none"
    UNIQUE = "unique"
    AMBIGUOUS = "ambiguous"


class PendingResolutionStatus(str, Enum):
    CONFIRM = "confirm"
    DENY = "deny"
    CANCEL = "cancel"
    UNCLEAR = "unclear"


class ActionClaimStatus(str, Enum):
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    TERMINAL = "terminal"


class OwnerActionLifecycleKind(str, Enum):
    PREPARED_ABORTED = "prepared_aborted"
    TERMINAL_RELEASED = "terminal_released"


_INTERRUPTED_RECOVERY_OUTCOME = MappingProxyType(
    {
        OwnerActionOperation.ARTIFACT_READ_EXACT: (
            ActionReceiptStatus.FAILED,
            ActionEffectState.NO_SIDE_EFFECT,
            "interrupted_read_failed_no_effect",
        ),
        OwnerActionOperation.ARTIFACT_GREP: (
            ActionReceiptStatus.FAILED,
            ActionEffectState.NO_SIDE_EFFECT,
            "interrupted_read_failed_no_effect",
        ),
        OwnerActionOperation.MEMORY_WRITE_LITERAL: (
            ActionReceiptStatus.EFFECT_UNKNOWN,
            ActionEffectState.UNKNOWN,
            "interrupted_mutation_effect_unknown",
        ),
        OwnerActionOperation.SANDBOX_SHELL_ONCE: (
            ActionReceiptStatus.EFFECT_UNKNOWN,
            ActionEffectState.UNKNOWN,
            "interrupted_mutation_effect_unknown",
        ),
    }
)
if frozenset(_INTERRUPTED_RECOVERY_OUTCOME) != frozenset(OwnerActionOperation):
    raise RuntimeError("owner_action_recovery_projection_incomplete")


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class OwnerActionLifecycleTombstone:
    """Minimal irreversible seam retained after sensitive lineage reclamation."""

    kind: OwnerActionLifecycleKind
    operation: OwnerActionOperation
    status: ActionReceiptStatus
    effect_state: ActionEffectState
    attempted: bool
    retry_blocked: bool
    reclaimed_at: float = field(repr=False)
    lifecycle_fingerprint: str = field(repr=False)

    def __init__(self) -> None:
        raise ContractViolation("lifecycle_tombstone_constructor_forbidden")

    def trace_metadata(self) -> dict[str, str | bool | int]:
        (
            kind,
            operation,
            status,
            effect_state,
            attempted,
            retry_blocked,
        ) = _inspect_lifecycle_tombstone_presentation(self)
        return {
            "schema_version": 1,
            "kind": kind,
            "operation": operation,
            "status": status,
            "effect_state": effect_state,
            "attempted": attempted,
            "retry_blocked": retry_blocked,
            "has_reclaimed_at": True,
            "has_lifecycle_fingerprint": True,
            "content_visible": False,
            "identifier_visible": False,
        }

    def __repr__(self) -> str:
        (
            kind,
            operation,
            status,
            effect_state,
            attempted,
            retry_blocked,
        ) = _inspect_lifecycle_tombstone_presentation(self)
        return (
            "OwnerActionLifecycleTombstone("
            f"kind={kind!r}, operation={operation!r}, "
            f"status={status!r}, "
            f"effect_state={effect_state!r}, "
            f"attempted={attempted}, retry_blocked={retry_blocked}, "
            "content_visible=False, identifier_visible=False)"
        )


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class PlannedOwnerActionRoute:
    """Controller-sealed link from one exact owner plan to one closed operation."""

    binding: DecisionBinding = field(repr=False)
    action_id: str = field(repr=False)
    route_digest: str = field(repr=False)
    capability: CapabilityClass
    operation: OwnerActionOperation

    def __post_init__(self) -> None:
        if not isinstance(self.binding, DecisionBinding):
            raise ContractViolation("action_route_binding_invalid")
        object.__setattr__(
            self,
            "action_id",
            require_hex(self.action_id, "action_id", lengths=(64,)),
        )
        object.__setattr__(
            self,
            "route_digest",
            require_hex(self.route_digest, "route_digest", lengths=(64,)),
        )
        if type(self.capability) is not CapabilityClass:
            raise ContractViolation("action_route_capability_invalid")
        if type(self.operation) is not OwnerActionOperation:
            raise ContractViolation("action_route_operation_invalid")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "capability": self.capability.value,
            "operation": self.operation.value,
            "origin_planned_action_bound": True,
            "binding_bound": True,
        }

    def __repr__(self) -> str:
        return (
            "PlannedOwnerActionRoute("
            f"capability={self.capability.value!r}, "
            f"operation={self.operation.value!r}, "
            "origin_planned_action_bound=True, binding_bound=True)"
        )


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class OwnerActionDenial:
    """Controller-issued pre-execution denial with no fabricated adapter data."""

    binding: DecisionBinding = field(repr=False)
    action_id: str = field(repr=False)
    denial_digest: str = field(repr=False)
    status: ActionReceiptStatus
    effect_state: ActionEffectState
    denied_at: float = field(repr=False)
    reason_codes: tuple[str, ...]

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "status": self.status.value,
            "effect_state": self.effect_state.value,
            "reason_count": len(self.reason_codes),
            "has_adapter": False,
            "has_parameters": False,
        }

    def __repr__(self) -> str:
        return (
            "OwnerActionDenial("
            f"status={self.status.value!r}, "
            f"effect_state={self.effect_state.value!r}, "
            f"reason_count={len(self.reason_codes)}, "
            "adapter_present=False, parameters_visible=False)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class PendingMatch:
    """Content-free result of code-owned current-principal pending lookup."""

    status: PendingMatchStatus
    pending: PendingConfirmation | None = field(default=None, repr=False)
    candidate_count: int = 0

    def __post_init__(self) -> None:
        if type(self.status) is not PendingMatchStatus:
            raise ContractViolation("pending_match_status_invalid")
        if self.pending is not None and type(self.pending) is not PendingConfirmation:
            raise ContractViolation("pending_match_pending_invalid")
        if (
            isinstance(self.candidate_count, bool)
            or not isinstance(self.candidate_count, int)
            or self.candidate_count < 0
        ):
            raise ContractViolation("pending_match_count_invalid")
        expected = {
            PendingMatchStatus.NONE: (False, 0),
            PendingMatchStatus.UNIQUE: (True, 1),
            PendingMatchStatus.AMBIGUOUS: (False, None),
        }[self.status]
        if (self.pending is not None) is not expected[0]:
            raise ContractViolation("pending_match_shape_invalid")
        if expected[1] is None:
            if self.candidate_count < 2:
                raise ContractViolation("pending_match_shape_invalid")
        elif self.candidate_count != expected[1]:
            raise ContractViolation("pending_match_shape_invalid")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "pending_match_status": self.status.value,
            "candidate_count": self.candidate_count,
            "has_exact_pending": self.pending is not None,
            "content_selected": False,
        }

    def __repr__(self) -> str:
        return (
            "PendingMatch("
            f"status={self.status.value!r}, "
            f"candidate_count={self.candidate_count}, "
            f"has_exact_pending={self.pending is not None}, "
            "content_selected=False)"
        )


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class PendingResolutionRoute:
    """Controller-canonical current-message resolution with no visible body."""

    binding: DecisionBinding = field(repr=False)
    route_digest: str = field(repr=False)
    request_digest: str = field(repr=False)
    parameter_digest: str = field(repr=False)
    decision: PendingDecision
    capability: CapabilityClass
    operation: OwnerActionOperation

    def __post_init__(self) -> None:
        if not isinstance(self.binding, DecisionBinding):
            raise ContractViolation("pending_resolution_binding_invalid")
        object.__setattr__(
            self,
            "route_digest",
            require_hex(
                self.route_digest,
                "pending_resolution_route_digest",
                lengths=(64,),
            ),
        )
        object.__setattr__(
            self,
            "request_digest",
            require_hex(self.request_digest, "request_digest", lengths=(64,)),
        )
        object.__setattr__(
            self,
            "parameter_digest",
            require_hex(self.parameter_digest, "parameter_digest", lengths=(64,)),
        )
        if type(self.decision) is not PendingDecision:
            raise ContractViolation("pending_resolution_decision_invalid")
        if type(self.capability) is not CapabilityClass:
            raise ContractViolation("pending_resolution_capability_invalid")
        if type(self.operation) is not OwnerActionOperation:
            raise ContractViolation("pending_resolution_operation_invalid")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "decision": self.decision.value,
            "capability": self.capability.value,
            "operation": self.operation.value,
            "current_binding_bound": True,
            "exact_pending_bound": True,
            "message_hidden": True,
        }

    def __repr__(self) -> str:
        return (
            "PendingResolutionRoute("
            f"decision={self.decision.value!r}, "
            f"capability={self.capability.value!r}, "
            f"operation={self.operation.value!r}, "
            "current_binding_bound=True, exact_pending_bound=True, "
            "message_hidden=True)"
        )


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class OwnerActionContinuationLineage:
    """Opaque controller-issued link from a confirmation plan to its origin."""

    binding: DecisionBinding = field(repr=False)
    current_action_id: str = field(repr=False)
    origin_action_id: str = field(repr=False)
    lineage_digest: str = field(repr=False)
    decision: PendingDecision
    capability: CapabilityClass
    operation: OwnerActionOperation

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "decision": self.decision.value,
            "capability": self.capability.value,
            "operation": self.operation.value,
            "current_plan_bound": True,
            "origin_request_bound": True,
            "resolution_route_bound": True,
            "outcome_text_present": False,
        }

    def __repr__(self) -> str:
        return (
            "OwnerActionContinuationLineage("
            f"decision={self.decision.value!r}, "
            f"capability={self.capability.value!r}, "
            f"operation={self.operation.value!r}, "
            "current_plan_bound=True, origin_request_bound=True, "
            "receipt_identity_private=True, outcome_text_present=False)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class OwnerActionContinuationInspection:
    """Parameter-free exact objects bound by one canonical continuation lineage."""

    lineage: OwnerActionContinuationLineage = field(repr=False)
    current_planned_action: PlannedAction = field(repr=False)
    origin_request: OwnerActionRequest = field(repr=False)
    resolution_route: PendingResolutionRoute = field(repr=False)
    receipt: ActionReceipt | None = field(default=None, repr=False)

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "decision": self.lineage.decision.value,
            "capability": self.lineage.capability.value,
            "operation": self.lineage.operation.value,
            "current_plan_bound": True,
            "origin_request_bound": True,
            "resolution_route_bound": True,
            "receipt_bound": self.receipt is not None,
            "outcome_text_present": False,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "OwnerActionContinuationInspection("
            f"decision={metadata['decision']!r}, "
            f"receipt_bound={metadata['receipt_bound']}, "
            "parameter_free=True, outcome_text_present=False)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class PendingResolutionMatch:
    """Body-free result of the controller's closed resolution phrase parser."""

    status: PendingResolutionStatus
    route: PendingResolutionRoute | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.status) is not PendingResolutionStatus:
            raise ContractViolation("pending_resolution_status_invalid")
        if self.route is not None and type(self.route) is not PendingResolutionRoute:
            raise ContractViolation("pending_resolution_route_invalid")
        if self.status is PendingResolutionStatus.UNCLEAR:
            if self.route is not None:
                raise ContractViolation("pending_resolution_match_shape_invalid")
            return
        if self.route is None or self.route.decision.value != self.status.value:
            raise ContractViolation("pending_resolution_match_shape_invalid")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "resolution_status": self.status.value,
            "has_exact_route": self.route is not None,
            "model_decision_used": False,
            "message_hidden": True,
        }

    def __repr__(self) -> str:
        return (
            "PendingResolutionMatch("
            f"status={self.status.value!r}, "
            f"has_exact_route={self.route is not None}, "
            "model_decision_used=False, message_hidden=True)"
        )


def _require_time(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractViolation(f"{field_name}_invalid")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0.0:
        raise ContractViolation(f"{field_name}_invalid")
    return parsed


def _digest_parts(*parts: object) -> str:
    encoded = json.dumps(
        parts,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _binding_parts(binding: DecisionBinding) -> tuple[object, ...]:
    return (
        binding.scope_key,
        binding.session_id,
        binding.current_message_id,
        binding.current_sender_key,
        binding.current_content_digest,
        binding.conversation_revision,
        binding.generation_epoch,
        binding.trace_id,
    )


def _snapshot_scalar(value: Any) -> tuple[type[Any], Any]:
    return type(value), value


def _snapshot_enum(value: Any) -> tuple[type[Any], int]:
    return type(value), id(value)


def _snapshot_binding(binding: Any) -> tuple[Any, ...]:
    if type(binding) is not DecisionBinding:
        return ("invalid_binding", type(binding), id(binding))
    return (
        "decision_binding_v1",
        id(binding),
        *(_snapshot_scalar(part) for part in _binding_parts(binding)),
    )


def _snapshot_reasons(reasons: Any) -> tuple[Any, ...]:
    if type(reasons) is not tuple:
        return ("invalid_reasons", type(reasons), id(reasons))
    return (
        "reason_codes_v1",
        tuple(_snapshot_scalar(reason) for reason in reasons),
    )


def _snapshot_action_route(
    route: Any,
    planned_action: PlannedAction,
    route_decision: OwnerActionRouteDecision,
) -> tuple[Any, ...]:
    if type(route) is not PlannedOwnerActionRoute:
        return ("invalid_action_route", type(route), id(route))
    return (
        "planned_owner_action_route_v1",
        _snapshot_binding(route.binding),
        _snapshot_scalar(route.action_id),
        _snapshot_scalar(route.route_digest),
        _snapshot_enum(route.capability),
        _snapshot_enum(route.operation),
        id(planned_action),
        id(route_decision),
    )


def _snapshot_request(request: Any) -> tuple[Any, ...]:
    if type(request) is not OwnerActionRequest:
        return ("invalid_request", type(request), id(request))
    return (
        "owner_action_request_v1",
        _snapshot_binding(request.binding),
        _snapshot_scalar(request.action_id),
        _snapshot_scalar(request.adapter_id),
        _snapshot_scalar(request.adapter_version),
        _snapshot_scalar(request.request_digest),
        _snapshot_scalar(request.parameter_digest),
        _snapshot_enum(request.capability),
        _snapshot_enum(request.operation),
        _snapshot_enum(request.side_effect),
        _snapshot_scalar(request.issued_at),
        _snapshot_scalar(request.deadline),
        _snapshot_enum(request.confirmation_policy),
        _snapshot_scalar(request.idempotency_key),
        _snapshot_scalar(request.call_budget),
        _snapshot_reasons(request.reason_codes),
    )


def _snapshot_pending(pending: Any) -> tuple[Any, ...]:
    if type(pending) is not PendingConfirmation:
        return ("invalid_pending", type(pending), id(pending))
    return (
        "pending_confirmation_v1",
        id(pending.request),
        _snapshot_binding(pending.binding),
        _snapshot_scalar(pending.action_id),
        _snapshot_scalar(pending.sender_key),
        _snapshot_scalar(pending.scope_key),
        _snapshot_scalar(pending.request_digest),
        _snapshot_scalar(pending.parameter_digest),
        _snapshot_scalar(pending.pending_id),
        _snapshot_scalar(pending.issued_at),
        _snapshot_scalar(pending.expires_at),
        (
            None
            if pending.confirmation_binding is None
            else _snapshot_binding(pending.confirmation_binding)
        ),
        _snapshot_scalar(pending.confirmation_proof_digest),
        _snapshot_scalar(pending.consumed_at),
    )


def _snapshot_denial(denial: Any) -> tuple[Any, ...]:
    if type(denial) is not OwnerActionDenial:
        return ("invalid_denial", type(denial), id(denial))
    return (
        "owner_action_denial_v1",
        _snapshot_binding(denial.binding),
        _snapshot_scalar(denial.action_id),
        _snapshot_scalar(denial.denial_digest),
        _snapshot_enum(denial.status),
        _snapshot_enum(denial.effect_state),
        _snapshot_scalar(denial.denied_at),
        _snapshot_reasons(denial.reason_codes),
    )


def _snapshot_resolution_route(
    route: Any,
    pending: PendingConfirmation,
    request: OwnerActionRequest,
    ticket: AcceptedTurnTicket,
    context: AcceptedTurnContext,
) -> tuple[Any, ...]:
    if type(route) is not PendingResolutionRoute:
        return ("invalid_resolution_route", type(route), id(route))
    return (
        "pending_resolution_route_v1",
        _snapshot_binding(route.binding),
        _snapshot_scalar(route.route_digest),
        _snapshot_scalar(route.request_digest),
        _snapshot_scalar(route.parameter_digest),
        _snapshot_enum(route.decision),
        _snapshot_enum(route.capability),
        _snapshot_enum(route.operation),
        id(pending),
        id(request),
        id(ticket),
        id(context),
    )


def _snapshot_lineage(lineage: Any) -> tuple[Any, ...]:
    if type(lineage) is not OwnerActionContinuationLineage:
        return ("invalid_lineage", type(lineage), id(lineage))
    return (
        "owner_action_continuation_lineage_v1",
        _snapshot_binding(lineage.binding),
        _snapshot_scalar(lineage.current_action_id),
        _snapshot_scalar(lineage.origin_action_id),
        _snapshot_scalar(lineage.lineage_digest),
        _snapshot_enum(lineage.decision),
        _snapshot_enum(lineage.capability),
        _snapshot_enum(lineage.operation),
    )


def _snapshot_lease(lease: Any) -> tuple[Any, ...]:
    if type(lease) is not ExecutionLease:
        return ("invalid_lease", type(lease), id(lease))
    return (
        "execution_lease_v1",
        _snapshot_binding(lease.binding),
        _snapshot_scalar(lease.request_digest),
        _snapshot_scalar(lease.lease_digest),
        _snapshot_enum(lease.capability),
        _snapshot_enum(lease.operation),
        _snapshot_scalar(lease.issued_at),
        _snapshot_scalar(lease.deadline),
    )


def _snapshot_lifecycle_tombstone(tombstone: Any) -> tuple[Any, ...]:
    if type(tombstone) is not OwnerActionLifecycleTombstone:
        return ("invalid_lifecycle_tombstone", type(tombstone), id(tombstone))
    try:
        return (
            "owner_action_lifecycle_tombstone_v1",
            _snapshot_enum(object.__getattribute__(tombstone, "kind")),
            _snapshot_enum(object.__getattribute__(tombstone, "operation")),
            _snapshot_enum(object.__getattribute__(tombstone, "status")),
            _snapshot_enum(object.__getattribute__(tombstone, "effect_state")),
            _snapshot_scalar(object.__getattribute__(tombstone, "attempted")),
            _snapshot_scalar(object.__getattribute__(tombstone, "retry_blocked")),
            _snapshot_scalar(object.__getattribute__(tombstone, "reclaimed_at")),
            _snapshot_scalar(
                object.__getattribute__(tombstone, "lifecycle_fingerprint")
            ),
        )
    except AttributeError:
        return ("invalid_lifecycle_tombstone_fields", type(tombstone), id(tombstone))


def _build_lifecycle_tombstone_presentation_registry():
    registry_lock = threading.RLock()
    records = weakref.WeakKeyDictionary()

    def register(
        controller: object,
        tombstone: OwnerActionLifecycleTombstone,
        snapshot: tuple[Any, ...],
    ) -> None:
        if type(controller) is not OwnerActionController:
            raise ContractViolation("lifecycle_tombstone_controller_invalid")
        if (
            type(tombstone) is not OwnerActionLifecycleTombstone
            or type(snapshot) is not tuple
            or _snapshot_lifecycle_tombstone(tombstone) != snapshot
        ):
            raise ContractViolation("lifecycle_tombstone_corrupt")
        try:
            kind = object.__getattribute__(tombstone, "kind")
            operation = object.__getattribute__(tombstone, "operation")
            status = object.__getattribute__(tombstone, "status")
            effect_state = object.__getattribute__(tombstone, "effect_state")
            attempted = object.__getattribute__(tombstone, "attempted")
            retry_blocked = object.__getattribute__(tombstone, "retry_blocked")
        except AttributeError:
            raise ContractViolation("lifecycle_tombstone_corrupt") from None
        if (
            type(kind) is not OwnerActionLifecycleKind
            or type(operation) is not OwnerActionOperation
            or type(status) is not ActionReceiptStatus
            or type(effect_state) is not ActionEffectState
            or type(attempted) is not bool
            or type(retry_blocked) is not bool
            or retry_blocked is not True
        ):
            raise ContractViolation("lifecycle_tombstone_corrupt")
        record = (
            weakref.ref(controller),
            snapshot,
            (
                kind.value,
                operation.value,
                status.value,
                effect_state.value,
                attempted,
                retry_blocked,
            ),
        )
        with registry_lock:
            if tombstone in records:
                raise ContractViolation("lifecycle_tombstone_corrupt")
            records[tombstone] = record

    def inspect(
        tombstone: OwnerActionLifecycleTombstone,
    ) -> tuple[str, str, str, str, bool, bool]:
        if type(tombstone) is not OwnerActionLifecycleTombstone:
            raise ContractViolation("lifecycle_tombstone_corrupt")
        with registry_lock:
            record = records.get(tombstone)
        if type(record) is not tuple or len(record) != 3:
            raise ContractViolation("lifecycle_tombstone_corrupt")
        controller_ref, snapshot, presentation = record
        controller = controller_ref() if type(controller_ref) is weakref.ReferenceType else None
        if (
            type(controller) is not OwnerActionController
            or type(snapshot) is not tuple
            or type(presentation) is not tuple
            or len(presentation) != 6
        ):
            raise ContractViolation("lifecycle_tombstone_corrupt")
        controller.inspect_lifecycle_tombstone(tombstone)
        with registry_lock:
            if records.get(tombstone) is not record:
                raise ContractViolation("lifecycle_tombstone_corrupt")
        if _snapshot_lifecycle_tombstone(tombstone) != snapshot:
            raise ContractViolation("lifecycle_tombstone_corrupt")
        if (
            any(type(value) is not str for value in presentation[:4])
            or type(presentation[4]) is not bool
            or type(presentation[5]) is not bool
            or presentation[5] is not True
        ):
            raise ContractViolation("lifecycle_tombstone_corrupt")
        return presentation

    return register, inspect


(
    _register_lifecycle_tombstone_presentation,
    _inspect_lifecycle_tombstone_presentation,
) = _build_lifecycle_tombstone_presentation_registry()
del _build_lifecycle_tombstone_presentation_registry


def _assert_followup_binding(
    origin: DecisionBinding,
    current: DecisionBinding,
) -> None:
    if not isinstance(current, DecisionBinding):
        raise ContractViolation("confirmation_binding_invalid")
    if current.scope_key != origin.scope_key:
        raise ContractViolation("confirmation_scope_mismatch")
    if current.session_id != origin.session_id:
        raise ContractViolation("confirmation_session_mismatch")
    if current.current_sender_key != origin.current_sender_key:
        raise ContractViolation("confirmation_sender_mismatch")
    if current.current_message_id == origin.current_message_id:
        raise ContractViolation("confirmation_message_not_new")
    if current.current_content_digest == origin.current_content_digest:
        raise ContractViolation("confirmation_content_not_new")
    if current.conversation_revision <= origin.conversation_revision:
        raise ContractViolation("confirmation_revision_stale")
    if current.generation_epoch <= origin.generation_epoch:
        raise ContractViolation("confirmation_epoch_stale")


def _parse_pending_resolution(
    operation: OwnerActionOperation,
    current_message: str,
    *,
    has_reference: bool,
) -> PendingDecision | None:
    if type(current_message) is not str or has_reference:
        return None
    if len(current_message.encode("utf-8")) > _MAX_PENDING_RESOLUTION_MESSAGE_BYTES:
        return None
    normalized = current_message.strip()
    phrases = {
        OwnerActionOperation.SANDBOX_SHELL_ONCE: {
            "确认执行": PendingDecision.CONFIRM,
            "拒绝执行": PendingDecision.DENY,
            "取消执行": PendingDecision.CANCEL,
        },
        OwnerActionOperation.MEMORY_WRITE_LITERAL: {
            "确认保存": PendingDecision.CONFIRM,
            "拒绝保存": PendingDecision.DENY,
            "取消保存": PendingDecision.CANCEL,
        },
    }.get(operation)
    if phrases is None:
        return None
    return phrases.get(normalized)


@dataclass(frozen=True, slots=True, repr=False)
class _AdapterShape:
    adapter_id: str
    adapter_version: str
    capability: CapabilityClass
    operation: OwnerActionOperation
    side_effect: ActionSideEffect
    confirmation_policy: ActionConfirmationPolicy
    source_interface_digest: str = field(repr=False)


def _draft_shape(value: AdapterDraft) -> _AdapterShape:
    return _AdapterShape(
        adapter_id=value.adapter_id,
        adapter_version=value.adapter_version,
        capability=value.capability,
        operation=value.operation,
        side_effect=value.side_effect,
        confirmation_policy=value.confirmation_policy,
        source_interface_digest=require_hex(
            value.source_interface_digest,
            "source_interface_digest",
            lengths=(64,),
        ),
    )


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ExecutionLease:
    """One controller-canonical permission to make exactly one attempt."""

    binding: DecisionBinding = field(repr=False)
    request_digest: str = field(repr=False)
    lease_digest: str = field(repr=False)
    capability: CapabilityClass
    operation: OwnerActionOperation
    issued_at: float = field(repr=False)
    deadline: float = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.binding, DecisionBinding):
            raise ContractViolation("execution_lease_binding_invalid")
        object.__setattr__(
            self,
            "request_digest",
            require_hex(self.request_digest, "request_digest", lengths=(64,)),
        )
        object.__setattr__(
            self,
            "lease_digest",
            require_hex(self.lease_digest, "lease_digest", lengths=(64,)),
        )
        if type(self.capability) is not CapabilityClass:
            raise ContractViolation("execution_lease_capability_invalid")
        if type(self.operation) is not OwnerActionOperation:
            raise ContractViolation("execution_lease_operation_invalid")
        issued_at = _require_time(self.issued_at, "execution_lease_issued_at")
        deadline = _require_time(self.deadline, "execution_lease_deadline")
        if deadline <= issued_at:
            raise ContractViolation("execution_lease_expired")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "deadline", deadline)

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "capability": self.capability.value,
            "operation": self.operation.value,
            "single_attempt": True,
            "binding_bound": True,
            "parameter_free": True,
        }

    def __repr__(self) -> str:
        return (
            "ExecutionLease("
            f"capability={self.capability.value!r}, "
            f"operation={self.operation.value!r}, "
            "single_attempt=True, binding_bound=True, parameter_free=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class OwnerActionInspection:
    request: OwnerActionRequest = field(repr=False)
    state: OwnerActionLedgerState
    has_pending: bool
    has_lease: bool
    has_receipt: bool

    def __post_init__(self) -> None:
        if type(self.request) is not OwnerActionRequest:
            raise ContractViolation("owner_action_inspection_request_invalid")
        if type(self.state) is not OwnerActionLedgerState:
            raise ContractViolation("owner_action_inspection_state_invalid")
        if any(
            type(value) is not bool
            for value in (self.has_pending, self.has_lease, self.has_receipt)
        ):
            raise ContractViolation("owner_action_inspection_shape_invalid")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "capability": self.request.capability.value,
            "operation": self.request.operation.value,
            "state": self.state.value,
            "has_pending": self.has_pending,
            "has_lease": self.has_lease,
            "has_receipt": self.has_receipt,
            "parameter_free": True,
        }

    def __repr__(self) -> str:
        return (
            "OwnerActionInspection("
            f"capability={self.request.capability.value!r}, "
            f"operation={self.request.operation.value!r}, "
            f"state={self.state.value!r}, "
            f"has_pending={self.has_pending}, has_lease={self.has_lease}, "
            f"has_receipt={self.has_receipt}, parameter_free=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class OwnerActionRequestLineageInspection:
    """Exact canonical origin plan/route for one controller-owned request."""

    request: OwnerActionRequest = field(repr=False)
    planned_action: PlannedAction = field(repr=False)
    action_route: PlannedOwnerActionRoute = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.request) is not OwnerActionRequest:
            raise ContractViolation("request_lineage_request_invalid")
        if type(self.planned_action) is not PlannedAction:
            raise ContractViolation("request_lineage_plan_invalid")
        if type(self.action_route) is not PlannedOwnerActionRoute:
            raise ContractViolation("request_lineage_route_invalid")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "capability": self.request.capability.value,
            "operation": self.request.operation.value,
            "origin_plan_bound": True,
            "origin_route_bound": True,
            "parameter_free": True,
        }

    def __repr__(self) -> str:
        return (
            "OwnerActionRequestLineageInspection("
            f"capability={self.request.capability.value!r}, "
            f"operation={self.request.operation.value!r}, "
            "origin_plan_bound=True, origin_route_bound=True, parameter_free=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class OwnerActionDenialInspection:
    """Exact canonical origin plan/route for one pre-execution denial."""

    denial: OwnerActionDenial = field(repr=False)
    planned_action: PlannedAction = field(repr=False)
    action_route: PlannedOwnerActionRoute = field(repr=False)
    capability: CapabilityClass
    operation: OwnerActionOperation

    def __post_init__(self) -> None:
        if type(self.denial) is not OwnerActionDenial:
            raise ContractViolation("denial_lineage_denial_invalid")
        if type(self.planned_action) is not PlannedAction:
            raise ContractViolation("denial_lineage_plan_invalid")
        if type(self.action_route) is not PlannedOwnerActionRoute:
            raise ContractViolation("denial_lineage_route_invalid")
        if type(self.capability) is not CapabilityClass:
            raise ContractViolation("denial_lineage_capability_invalid")
        if type(self.operation) is not OwnerActionOperation:
            raise ContractViolation("denial_lineage_operation_invalid")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "capability": self.capability.value,
            "operation": self.operation.value,
            "origin_plan_bound": True,
            "origin_route_bound": True,
            "parameter_free": True,
        }

    def __repr__(self) -> str:
        return (
            "OwnerActionDenialInspection("
            f"capability={self.capability.value!r}, "
            f"operation={self.operation.value!r}, "
            "origin_plan_bound=True, origin_route_bound=True, parameter_free=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class OwnerActionReceiptInspection:
    """Exact canonical request and origin plan/route for one receipt."""

    receipt: ActionReceipt = field(repr=False)
    request: OwnerActionRequest = field(repr=False)
    planned_action: PlannedAction = field(repr=False)
    action_route: PlannedOwnerActionRoute = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.receipt) is not ActionReceipt:
            raise ContractViolation("receipt_lineage_receipt_invalid")
        if type(self.request) is not OwnerActionRequest:
            raise ContractViolation("receipt_lineage_request_invalid")
        if type(self.planned_action) is not PlannedAction:
            raise ContractViolation("receipt_lineage_plan_invalid")
        if type(self.action_route) is not PlannedOwnerActionRoute:
            raise ContractViolation("receipt_lineage_route_invalid")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "capability": self.receipt.capability.value,
            "operation": self.receipt.operation.value,
            "origin_request_bound": True,
            "origin_plan_bound": True,
            "origin_route_bound": True,
            "parameter_free": True,
        }

    def __repr__(self) -> str:
        return (
            "OwnerActionReceiptInspection("
            f"capability={self.receipt.capability.value!r}, "
            f"operation={self.receipt.operation.value!r}, "
            "origin_request_bound=True, origin_plan_bound=True, "
            "origin_route_bound=True, parameter_free=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ActionClaim:
    status: ActionClaimStatus
    lease: ExecutionLease | None = field(default=None, repr=False)
    receipt: ActionReceipt | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.status) is not ActionClaimStatus:
            raise ContractViolation("action_claim_status_invalid")
        if self.lease is not None and type(self.lease) is not ExecutionLease:
            raise ContractViolation("action_claim_lease_invalid")
        if self.receipt is not None and type(self.receipt) is not ActionReceipt:
            raise ContractViolation("action_claim_receipt_invalid")
        expected = {
            ActionClaimStatus.CLAIMED: (True, False),
            ActionClaimStatus.IN_PROGRESS: (False, False),
            ActionClaimStatus.TERMINAL: (False, True),
        }[self.status]
        if (self.lease is not None, self.receipt is not None) != expected:
            raise ContractViolation("action_claim_shape_invalid")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "claim_status": self.status.value,
            "has_lease": self.lease is not None,
            "has_receipt": self.receipt is not None,
        }

    def __repr__(self) -> str:
        return (
            "ActionClaim("
            f"status={self.status.value!r}, "
            f"has_lease={self.lease is not None}, "
            f"has_receipt={self.receipt is not None})"
        )


@dataclass(slots=True, repr=False)
class _ActionRecord:
    request: OwnerActionRequest
    action_route: PlannedOwnerActionRoute
    planned_action: PlannedAction
    parameter_record: object
    adapter_draft: AdapterDraft
    adapter_material: object
    adapter_shape: _AdapterShape
    request_snapshot: tuple[Any, ...]
    state: OwnerActionLedgerState = OwnerActionLedgerState.PREPARED
    base_pending: PendingConfirmation | None = None
    base_pending_snapshot: tuple[Any, ...] | None = None
    pending: PendingConfirmation | None = None
    pending_snapshot: tuple[Any, ...] | None = None
    authorized_binding: DecisionBinding | None = None
    lease: ExecutionLease | None = None
    lease_consumed: bool = False
    latest_receipt: ActionReceipt | None = None
    continuation: _ContinuationLineageRecord | None = None


@dataclass(slots=True, repr=False)
class _RouteRecord:
    route: PlannedOwnerActionRoute
    ticket: AcceptedTurnTicket
    context: AcceptedTurnContext
    planned_action: PlannedAction
    route_decision: OwnerActionRouteDecision
    route_snapshot: tuple[Any, ...]
    denial: OwnerActionDenial | None = None
    consumed: bool = False


@dataclass(slots=True, repr=False)
class _PendingResolutionRouteRecord:
    route: PendingResolutionRoute
    pending: PendingConfirmation
    request: OwnerActionRequest
    ticket: AcceptedTurnTicket
    context: AcceptedTurnContext
    decision: PendingDecision
    route_snapshot: tuple[Any, ...]
    pending_snapshot: tuple[Any, ...]
    request_snapshot: tuple[Any, ...]
    current_planned_action: PlannedAction | None = None
    lineage: OwnerActionContinuationLineage | None = None
    consumed: bool = False


@dataclass(slots=True, repr=False)
class _ContinuationLineageRecord:
    lineage: OwnerActionContinuationLineage
    lineage_snapshot: tuple[Any, ...]
    current_planned_action: PlannedAction
    origin_request: OwnerActionRequest
    origin_request_snapshot: tuple[Any, ...]
    resolution_route: PendingResolutionRoute
    resolution_route_snapshot: tuple[Any, ...]
    decision: PendingDecision
    receipt: ActionReceipt | None = None
    receipt_snapshot: tuple[Any, ...] | None = None


@dataclass(slots=True, repr=False)
class _DenialRecord:
    denial: OwnerActionDenial
    route_record: _RouteRecord
    denial_snapshot: tuple[Any, ...]


@dataclass(slots=True, repr=False)
class _ReceiptRecord:
    receipt: ActionReceipt
    action_record: _ActionRecord
    receipt_snapshot: tuple[Any, ...]


@dataclass(slots=True, repr=False)
class _LeaseRecord:
    lease: ExecutionLease
    action_record: _ActionRecord
    lease_snapshot: tuple[Any, ...]


@dataclass(frozen=True, slots=True, repr=False)
class _LifecycleTombstoneRecord:
    tombstone: OwnerActionLifecycleTombstone
    tombstone_snapshot: tuple[Any, ...]


def _require_exact_record(
    value: object,
    record_type: type[Any],
    reason_code: str,
) -> None:
    """Reject registry impostors and partially deleted exact slot records."""

    if type(value) is not record_type:
        raise ContractViolation(reason_code)
    slots = object.__getattribute__(record_type, "__slots__")
    if type(slots) is not tuple or any(type(name) is not str for name in slots):
        raise ContractViolation(reason_code)
    try:
        for name in slots:
            object.__getattribute__(value, name)
    except AttributeError:
        raise ContractViolation(reason_code) from None


def _require_exact_registry_pair(value: object, reason_code: str) -> None:
    if type(value) is not tuple or len(value) != 2:
        raise ContractViolation(reason_code)


def _require_exact_snapshot(value: object, reason_code: str) -> None:
    if type(value) is not tuple:
        raise ContractViolation(reason_code)


_CONTROLLER_MUTABLE_MAPPING_FIELDS = (
    "_continuations",
    "_denials",
    "_idempotency",
    "_leases",
    "_ledger",
    "_receipts",
    "_resolution_route_by_ticket",
    "_resolution_routes",
    "_route_by_ticket",
    "_routes",
    "_tombstones",
)
_MISSING_TRANSACTION_FIELD = object()


class _ControllerMutationTransaction:
    """Rollback Controller graph mutations even when a dict hook raises BaseException.

    Lifecycle commit paths deliberately touch several correlated indexes.  Tests may
    replace one internal ``dict`` with a fault-injecting subclass whose mutation first
    succeeds and then raises.  Snapshot through the unbound built-in ``dict`` methods,
    and restore the original mapping objects the same way so overridden hooks cannot
    prevent rollback.
    """

    __slots__ = ("_controller", "_mapping_snapshots", "_record_snapshots")

    def __init__(
        self,
        controller: object,
        *,
        record_fields: tuple[tuple[object, tuple[str, ...]], ...] = (),
    ) -> None:
        mapping_snapshots: list[tuple[str, dict[Any, Any], dict[Any, Any]]] = []
        for field_name in _CONTROLLER_MUTABLE_MAPPING_FIELDS:
            try:
                mapping = object.__getattribute__(controller, field_name)
            except AttributeError:
                raise ContractViolation("owner_action_controller_state_corrupt") from None
            if not isinstance(mapping, dict):
                raise ContractViolation("owner_action_controller_state_corrupt")
            mapping_snapshots.append(
                (field_name, mapping, dict.copy(mapping))
            )

        snapshots: list[tuple[object, tuple[tuple[str, object], ...]]] = []
        for target, field_names in record_fields:
            if type(field_names) is not tuple or any(
                type(field_name) is not str for field_name in field_names
            ):
                raise ContractViolation("owner_action_transaction_fields_invalid")
            values: list[tuple[str, object]] = []
            for field_name in field_names:
                try:
                    value = object.__getattribute__(target, field_name)
                except AttributeError:
                    value = _MISSING_TRANSACTION_FIELD
                values.append((field_name, value))
            snapshots.append((target, tuple(values)))

        self._controller = controller
        self._mapping_snapshots = tuple(mapping_snapshots)
        self._record_snapshots = tuple(snapshots)

    def __enter__(self) -> "_ControllerMutationTransaction":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        del exc, traceback
        if exc_type is None:
            return False

        for field_name, original_mapping, snapshot in self._mapping_snapshots:
            dict.clear(original_mapping)
            dict.update(original_mapping, snapshot)
            object.__setattr__(self._controller, field_name, original_mapping)
        for target, fields in self._record_snapshots:
            for field_name, value in fields:
                if value is _MISSING_TRANSACTION_FIELD:
                    try:
                        object.__delattr__(target, field_name)
                    except AttributeError:
                        pass
                else:
                    object.__setattr__(target, field_name, value)
        return False


class OwnerActionController:
    """Single authority for owner request, confirmation, lease, and receipt lineage."""

    __slots__ = (
        "__weakref__",
        "_accepted_turn_authority",
        "_action_outcome_authority",
        "_action_outcome_authority_snapshot",
        "_action_outcome_plan_authority",
        "_continuations",
        "_controller_nonce",
        "_denials",
        "_idempotency",
        "_leases",
        "_ledger",
        "_lock",
        "_max_ledger_entries",
        "_owner_action_router",
        "_planned_action_authority",
        "_receipts",
        "_resolution_route_by_ticket",
        "_resolution_routes",
        "_route_by_ticket",
        "_routes",
        "_tombstones",
    )

    def __init__(
        self,
        accepted_turn_authority: AcceptedTurnAuthority,
        owner_action_router: OwnerActionRouter,
        planned_action_authority: PlannedActionAuthority,
        *,
        max_ledger_entries: int = _DEFAULT_MAX_LEDGER_ENTRIES,
    ) -> None:
        if type(accepted_turn_authority) is not AcceptedTurnAuthority:
            raise ContractViolation("accepted_turn_authority_required")
        if type(owner_action_router) is not OwnerActionRouter:
            raise ContractViolation("owner_action_router_required")
        if type(planned_action_authority) is not PlannedActionAuthority:
            raise ContractViolation("planned_action_authority_required")
        if (
            isinstance(max_ledger_entries, bool)
            or not isinstance(max_ledger_entries, int)
            or max_ledger_entries < 1
        ):
            raise ContractViolation("owner_action_ledger_limit_invalid")
        self._accepted_turn_authority = accepted_turn_authority
        self._owner_action_router = owner_action_router
        self._planned_action_authority = planned_action_authority
        self._action_outcome_authority: object | None = None
        self._action_outcome_plan_authority: PlannedActionAuthority | None = None
        self._action_outcome_authority_snapshot: tuple[Any, ...] | None = None
        self._max_ledger_entries = max_ledger_entries
        self._controller_nonce = secrets.token_hex(32)
        self._denials: dict[int, _DenialRecord] = {}
        self._continuations: dict[
            int,
            tuple[OwnerActionContinuationLineage, _ContinuationLineageRecord],
        ] = {}
        self._ledger: dict[int, _ActionRecord] = {}
        self._idempotency: dict[
            str,
            _ActionRecord | _LifecycleTombstoneRecord,
        ] = {}
        self._leases: dict[int, _LeaseRecord] = {}
        self._receipts: dict[int, _ReceiptRecord] = {}
        self._resolution_routes: dict[int, _PendingResolutionRouteRecord] = {}
        self._resolution_route_by_ticket: dict[
            int,
            tuple[AcceptedTurnTicket, _PendingResolutionRouteRecord],
        ] = {}
        self._routes: dict[int, _RouteRecord] = {}
        self._tombstones: dict[int, _LifecycleTombstoneRecord] = {}
        self._route_by_ticket: dict[
            int,
            tuple[AcceptedTurnTicket, _RouteRecord],
        ] = {}
        self._lock = threading.RLock()

    @property
    def ledger_size(self) -> int:
        with self._lock:
            return len(self._ledger)

    def _inspect_action_outcome_authority(
        self,
        authority: object,
        planned_action_authority: PlannedActionAuthority,
    ) -> None:
        """Revalidate the exact Controller-scoped outcome authority."""

        from .action_outcome import (
            ActionOutcomeAuthority,
            _inspect_runtime_authority,
        )

        if type(authority) is not ActionOutcomeAuthority:
            raise ContractViolation("action_outcome_authority_not_canonical")
        if type(planned_action_authority) is not PlannedActionAuthority:
            raise ContractViolation("action_outcome_plan_authority_required")
        with self._lock:
            _inspect_runtime_authority(
                authority,
                controller=self,
                planned_action_authority=planned_action_authority,
            )
            if (
                self._action_outcome_authority is not authority
                or self._action_outcome_plan_authority is not planned_action_authority
                or planned_action_authority is not self._planned_action_authority
                or type(self._action_outcome_authority_snapshot) is not tuple
            ):
                raise ContractViolation("action_outcome_authority_not_canonical")

    def _validate_ticket(
        self,
        ticket: AcceptedTurnTicket,
    ) -> tuple[DecisionBinding, AcceptedTurnContext]:
        if type(ticket) is not AcceptedTurnTicket:
            raise ContractViolation("accepted_turn_ticket_required")
        binding = ticket.binding
        context = self._accepted_turn_authority.context_for(
            ticket,
            consumer=AcceptedTurnConsumer.OWNER_ACTION,
            binding=binding,
        )
        if type(context) is not AcceptedTurnContext:
            raise ContractViolation("accepted_turn_context_required")
        envelope = context.envelope
        principal = context.principal
        expected_scope = build_scope_key(
            platform_id=envelope.platform_id,
            bot_id=envelope.bot_id,
            chat_type="private",
            session_id=envelope.session_id,
        )
        expected_sender = build_sender_key(expected_scope, envelope.sender_id)
        structurally_trusted_owner = bool(
            type(principal.is_owner) is bool
            and principal.is_owner
            and principal.relationship_role == "owner"
            and principal.verification_source
            == _TRUSTED_OWNER_VERIFICATION_SOURCE
            and principal.sender_id == envelope.sender_id
            and principal.sender_key == envelope.sender_key
            and envelope.chat_type == "private"
            and not envelope.group_id
            and expected_scope
            and envelope.scope_key == expected_scope
            and expected_sender
            and envelope.sender_key == expected_sender
            and binding.scope_key == expected_scope
            and binding.session_id == envelope.session_id
            and binding.current_sender_key == expected_sender
            and binding.current_content_digest == context.content_digest
            and context.sender_kind is SenderKind.HUMAN
            and context.plugin_source is PluginSource.NONE
        )
        if not structurally_trusted_owner:
            raise ContractViolation("configured_private_owner_required")
        return binding, context

    def _canonical_record(self, request: OwnerActionRequest) -> _ActionRecord:
        if type(request) is not OwnerActionRequest:
            raise ContractViolation("owner_action_request_required")
        record = self._ledger.get(id(request))
        if record is None:
            raise ContractViolation("request_not_canonical")
        _require_exact_record(
            record,
            _ActionRecord,
            "owner_action_ledger_corrupt",
        )
        if record.request is not request:
            raise ContractViolation("request_not_canonical")
        self._validate_action_record_integrity(record)
        return record

    def _mint_lifecycle_tombstone(
        self,
        *,
        kind: OwnerActionLifecycleKind,
        operation: OwnerActionOperation,
        status: ActionReceiptStatus,
        effect_state: ActionEffectState,
        attempted: bool,
        reclaimed_at: float,
        source_fingerprint: str,
    ) -> tuple[OwnerActionLifecycleTombstone, _LifecycleTombstoneRecord]:
        if type(kind) is not OwnerActionLifecycleKind:
            raise ContractViolation("lifecycle_tombstone_kind_invalid")
        if type(operation) is not OwnerActionOperation:
            raise ContractViolation("lifecycle_tombstone_operation_invalid")
        if type(status) is not ActionReceiptStatus:
            raise ContractViolation("lifecycle_tombstone_status_invalid")
        if type(effect_state) is not ActionEffectState:
            raise ContractViolation("lifecycle_tombstone_effect_invalid")
        if type(attempted) is not bool:
            raise ContractViolation("lifecycle_tombstone_attempted_invalid")
        timestamp = _require_time(reclaimed_at, "reclaimed_at")
        source_digest = require_hex(
            source_fingerprint,
            "lifecycle_source_fingerprint",
            lengths=(64,),
        )
        tombstone = object.__new__(OwnerActionLifecycleTombstone)
        object.__setattr__(tombstone, "kind", kind)
        object.__setattr__(tombstone, "operation", operation)
        object.__setattr__(tombstone, "status", status)
        object.__setattr__(tombstone, "effect_state", effect_state)
        object.__setattr__(tombstone, "attempted", attempted)
        object.__setattr__(tombstone, "retry_blocked", True)
        object.__setattr__(tombstone, "reclaimed_at", timestamp)
        object.__setattr__(
            tombstone,
            "lifecycle_fingerprint",
            _digest_parts(
                "owner-action-lifecycle-tombstone-v1",
                self._controller_nonce,
                kind.value,
                operation.value,
                status.value,
                effect_state.value,
                attempted,
                timestamp,
                source_digest,
            ),
        )
        snapshot = _snapshot_lifecycle_tombstone(tombstone)
        return tombstone, _LifecycleTombstoneRecord(
            tombstone=tombstone,
            tombstone_snapshot=snapshot,
        )

    def _register_lifecycle_tombstone(
        self,
        record: _LifecycleTombstoneRecord,
    ) -> OwnerActionLifecycleTombstone:
        _require_exact_record(
            record,
            _LifecycleTombstoneRecord,
            "lifecycle_tombstone_corrupt",
        )
        _require_exact_snapshot(
            record.tombstone_snapshot,
            "lifecycle_tombstone_corrupt",
        )
        tombstone = record.tombstone
        if (
            type(tombstone) is not OwnerActionLifecycleTombstone
            or _snapshot_lifecycle_tombstone(tombstone)
            != record.tombstone_snapshot
            or id(tombstone) in self._tombstones
        ):
            raise ContractViolation("lifecycle_tombstone_corrupt")
        self._tombstones[id(tombstone)] = record
        _register_lifecycle_tombstone_presentation(
            self,
            tombstone,
            record.tombstone_snapshot,
        )
        return tombstone

    def inspect_lifecycle_tombstone(
        self,
        tombstone: OwnerActionLifecycleTombstone,
    ) -> OwnerActionLifecycleTombstone:
        if type(tombstone) is not OwnerActionLifecycleTombstone:
            raise ContractViolation("lifecycle_tombstone_required")
        with self._lock:
            record = self._tombstones.get(id(tombstone))
            if record is None:
                raise ContractViolation("lifecycle_tombstone_not_canonical")
            _require_exact_record(
                record,
                _LifecycleTombstoneRecord,
                "lifecycle_tombstone_corrupt",
            )
            _require_exact_snapshot(
                record.tombstone_snapshot,
                "lifecycle_tombstone_corrupt",
            )
            if record.tombstone is not tombstone:
                raise ContractViolation("lifecycle_tombstone_not_canonical")
            if (
                _snapshot_lifecycle_tombstone(tombstone)
                != record.tombstone_snapshot
                or type(tombstone.kind) is not OwnerActionLifecycleKind
                or type(tombstone.operation) is not OwnerActionOperation
                or type(tombstone.status) is not ActionReceiptStatus
                or type(tombstone.effect_state) is not ActionEffectState
                or type(tombstone.attempted) is not bool
                or tombstone.retry_blocked is not True
                or type(tombstone.reclaimed_at) is not float
                or type(tombstone.lifecycle_fingerprint) is not str
                or len(tombstone.lifecycle_fingerprint) != 64
            ):
                raise ContractViolation("lifecycle_tombstone_corrupt")
            return tombstone

    def _validate_route_record_integrity(self, record: _RouteRecord) -> None:
        _require_exact_record(record, _RouteRecord, "action_route_corrupt")
        _require_exact_snapshot(record.route_snapshot, "action_route_corrupt")
        route = record.route
        plan = self._planned_action_authority.inspect_plan(record.planned_action)
        if (
            _snapshot_action_route(route, plan, record.route_decision)
            != record.route_snapshot
            or route.binding is not plan.binding
            or route.binding is not record.context.binding
            or route.action_id != plan.action_id
            or plan.kind is not ActionKind.EXECUTE_ACTION
            or plan.action.capability_intent != route.capability.value
            or plan.action.operation_intent is not route.operation
            or route.route_digest
            != _digest_parts(
                "planned-owner-action-route-v1",
                self._controller_nonce,
                route.action_id,
                *_binding_parts(route.binding),
                route.capability.value,
                route.operation.value,
            )
        ):
            raise ContractViolation("action_route_corrupt")

    def _validate_action_record_integrity(self, record: _ActionRecord) -> None:
        _require_exact_record(
            record,
            _ActionRecord,
            "owner_action_ledger_corrupt",
        )
        _require_exact_snapshot(
            record.request_snapshot,
            "request_corrupt",
        )
        request = record.request
        if _snapshot_request(request) != record.request_snapshot:
            raise ContractViolation("request_corrupt")
        route_record = self._routes.get(id(record.action_route))
        if route_record is None:
            raise ContractViolation("request_action_route_not_canonical")
        self._validate_route_record_integrity(route_record)
        if route_record.route is not record.action_route:
            raise ContractViolation("request_action_route_not_canonical")
        if (
            record.planned_action is not route_record.planned_action
            or request.binding is not record.action_route.binding
            or request.action_id != record.action_route.action_id
            or request.adapter_id != record.adapter_shape.adapter_id
            or request.adapter_version != record.adapter_shape.adapter_version
            or request.capability is not record.adapter_shape.capability
            or request.operation is not record.adapter_shape.operation
            or request.side_effect is not record.adapter_shape.side_effect
            or request.confirmation_policy
            is not record.adapter_shape.confirmation_policy
            or request.call_budget != 1
            or request.request_digest
            != _digest_parts(
                "owner-action-request-v1",
                self._controller_nonce,
                *_binding_parts(request.binding),
                request.adapter_id,
                request.adapter_version,
                request.capability.value,
                request.operation.value,
                request.side_effect.value,
                request.confirmation_policy.value,
                request.parameter_digest,
                request.idempotency_key,
                request.issued_at,
                request.deadline,
                record.action_route.route_digest,
            )
        ):
            raise ContractViolation("request_corrupt")
        for pending, snapshot in (
            (record.base_pending, record.base_pending_snapshot),
            (record.pending, record.pending_snapshot),
        ):
            if pending is None:
                if snapshot is not None:
                    raise ContractViolation("pending_corrupt")
                continue
            _require_exact_snapshot(snapshot, "pending_corrupt")
            if (
                _snapshot_pending(pending) != snapshot
                or pending.request is not request
                or pending.binding is not request.binding
                or pending.action_id != request.action_id
                or pending.request_digest != request.request_digest
                or pending.parameter_digest != request.parameter_digest
            ):
                raise ContractViolation("pending_corrupt")

    def _validate_pending_integrity(
        self,
        record: _ActionRecord,
        pending: PendingConfirmation,
        snapshot: tuple[Any, ...] | None,
    ) -> None:
        self._validate_action_record_integrity(record)
        if snapshot is None or _snapshot_pending(pending) != snapshot:
            raise ContractViolation("pending_corrupt")
        if (
            pending.request is not record.request
            or pending.binding is not record.request.binding
            or pending.action_id != record.request.action_id
            or pending.sender_key != record.request.binding.current_sender_key
            or pending.scope_key != record.request.binding.scope_key
            or pending.request_digest != record.request.request_digest
            or pending.parameter_digest != record.request.parameter_digest
            or pending.issued_at < record.request.issued_at
            or pending.expires_at > record.request.deadline
        ):
            raise ContractViolation("pending_corrupt")

    def _validate_receipt_record_integrity(self, entry: _ReceiptRecord) -> None:
        _require_exact_record(entry, _ReceiptRecord, "receipt_not_canonical")
        _require_exact_snapshot(entry.receipt_snapshot, "receipt_not_canonical")
        receipt = entry.receipt
        record = entry.action_record
        self._validate_action_record_integrity(record)
        if (
            not receipt.is_canonical
            or _action_receipt_integrity_snapshot(receipt)
            != entry.receipt_snapshot
            or receipt.action_id != record.request.action_id
            or receipt.request_digest != record.request.request_digest
            or receipt.adapter_id != record.request.adapter_id
            or receipt.adapter_version != record.request.adapter_version
            or receipt.capability is not record.request.capability
            or receipt.operation is not record.request.operation
            or receipt.side_effect is not record.request.side_effect
            or receipt.confirmation_policy
            is not record.request.confirmation_policy
            or receipt.idempotency_key != record.request.idempotency_key
            or receipt.origin_binding is not record.request.binding
        ):
            raise ContractViolation("receipt_not_canonical")

    def _validate_lease_record_integrity(self, entry: _LeaseRecord) -> None:
        _require_exact_record(entry, _LeaseRecord, "execution_lease_corrupt")
        _require_exact_snapshot(entry.lease_snapshot, "execution_lease_corrupt")
        lease = entry.lease
        record = entry.action_record
        self._validate_action_record_integrity(record)
        if (
            _snapshot_lease(lease) != entry.lease_snapshot
            or record.lease is not lease
            or lease.binding is not record.authorized_binding
            or lease.request_digest != record.request.request_digest
            or lease.capability is not record.request.capability
            or lease.operation is not record.request.operation
            or lease.deadline != record.request.deadline
            or lease.lease_digest
            != _digest_parts(
                "owner-action-lease-v1",
                self._controller_nonce,
                record.request.request_digest,
                *_binding_parts(lease.binding),
                lease.issued_at,
            )
        ):
            raise ContractViolation("execution_lease_corrupt")

    def _inspect_exact_execution_attempt(
        self,
        request: OwnerActionRequest,
        lease: ExecutionLease,
        adapter_draft: AdapterDraft,
        adapter_material: object,
    ) -> None:
        """Friend inspector for E2C; returns no ledger or authority material.

        The inspector deliberately reuses the E3C0R request and lease integrity
        validators on every call.  Exact request/draft/material/parameter identities
        are checked while the Controller lock is held, but no callback or tool code
        runs under that lock.
        """

        if type(request) is not OwnerActionRequest:
            raise ContractViolation("owner_action_request_required")
        if type(lease) is not ExecutionLease:
            raise ContractViolation("execution_lease_required")
        if type(adapter_draft) is not AdapterDraft:
            raise ContractViolation("adapter_draft_not_canonical")
        with self._lock:
            self._inspect_exact_execution_lease(request, lease)
            record = self._canonical_record(request)
            lease_entry = self._leases[id(lease)]
            if (
                lease_entry.action_record is not record
                or record.adapter_draft is not adapter_draft
                or record.adapter_material is not adapter_material
                or record.parameter_record
                is not getattr(adapter_material, "parameter_record", None)
                or record.lease is not lease
                or record.lease_consumed
                or record.state is not OwnerActionLedgerState.IN_PROGRESS
            ):
                raise ContractViolation("execution_attempt_not_canonical")

    def _inspect_exact_execution_lease(
        self,
        request: OwnerActionRequest,
        lease: ExecutionLease,
    ) -> None:
        """Private snapshot inspector used by deterministic sealed test issuance."""

        if type(request) is not OwnerActionRequest:
            raise ContractViolation("owner_action_request_required")
        if type(lease) is not ExecutionLease:
            raise ContractViolation("execution_lease_required")
        with self._lock:
            record = self._canonical_record(request)
            lease_entry = self._leases.get(id(lease))
            if lease_entry is None:
                raise ContractViolation("execution_lease_not_canonical")
            self._validate_lease_record_integrity(lease_entry)
            if lease_entry.lease is not lease:
                raise ContractViolation("execution_lease_not_canonical")
            if (
                lease_entry.action_record is not record
                or record.lease is not lease
                or record.lease_consumed
                or record.state is not OwnerActionLedgerState.IN_PROGRESS
            ):
                raise ContractViolation("execution_attempt_not_canonical")
            return None

    def _canonical_pending(
        self,
        pending: PendingConfirmation,
    ) -> _ActionRecord:
        if type(pending) is not PendingConfirmation:
            raise ContractViolation("pending_confirmation_required")
        record = self._ledger.get(id(pending.request))
        if record is None:
            raise ContractViolation("pending_not_canonical")
        _require_exact_record(
            record,
            _ActionRecord,
            "owner_action_ledger_corrupt",
        )
        if record.request is not pending.request:
            raise ContractViolation("pending_not_canonical")
        if record.pending is not pending:
            raise ContractViolation("pending_not_active")
        self._validate_pending_integrity(record, pending, record.pending_snapshot)
        return record

    def _register_receipt(
        self,
        record: _ActionRecord,
        receipt: ActionReceipt,
    ) -> ActionReceipt:
        self._validate_action_record_integrity(record)
        if not receipt.is_canonical:
            raise ContractViolation("receipt_not_canonical")
        receipt_snapshot = _action_receipt_integrity_snapshot(receipt)
        self._receipts[id(receipt)] = _ReceiptRecord(
            receipt=receipt,
            action_record=record,
            receipt_snapshot=receipt_snapshot,
        )
        record.latest_receipt = receipt
        continuation = record.continuation
        if (
            continuation is not None
            and receipt.status is not ActionReceiptStatus.CONFIRMATION_REQUIRED
        ):
            if continuation.receipt is not None and continuation.receipt is not receipt:
                raise ContractViolation("continuation_receipt_already_bound")
            continuation.receipt = receipt
            continuation.receipt_snapshot = receipt_snapshot
        return receipt

    def _canonical_route(
        self,
        route: PlannedOwnerActionRoute,
        ticket: AcceptedTurnTicket,
        context: AcceptedTurnContext,
    ) -> _RouteRecord:
        if type(route) is not PlannedOwnerActionRoute:
            raise ContractViolation("planned_owner_action_route_required")
        record = self._routes.get(id(route))
        if record is None:
            raise ContractViolation("action_route_not_canonical")
        self._validate_route_record_integrity(record)
        if record.route is not route:
            raise ContractViolation("action_route_not_canonical")
        if record.ticket is not ticket or record.context is not context:
            raise ContractViolation("action_route_ticket_mismatch")
        if record.consumed:
            raise ContractViolation("action_route_replayed")
        return record

    def seal_action_route(
        self,
        ticket: AcceptedTurnTicket,
        planned_action: PlannedAction,
        route_decision: OwnerActionRouteDecision,
    ) -> PlannedOwnerActionRoute:
        """Seal one exact existing owner plan; never invent a new action identity."""

        if type(route_decision) is not OwnerActionRouteDecision:
            raise ContractViolation("owner_action_route_required")
        with self._lock:
            binding, context = self._validate_ticket(ticket)
            inspected_plan = self._planned_action_authority.inspect_plan(planned_action)
            inspected_route = self._owner_action_router.inspect_route(
                route_decision,
                ticket=ticket,
            )
            if (
                inspected_route.status is not OwnerActionRouteStatus.MATCHED
                or inspected_route.proposal is None
            ):
                raise ContractViolation("owner_action_route_not_matched")
            proposal = inspected_route.proposal
            proposal_capability = proposal.capability
            proposal_operation = proposal.operation
            existing = self._route_by_ticket.get(id(ticket))
            if existing is not None:
                _require_exact_registry_pair(existing, "action_route_corrupt")
            if existing is not None and existing[0] is ticket:
                record = existing[1]
                _require_exact_record(record, _RouteRecord, "action_route_corrupt")
                if (
                    record.planned_action is inspected_plan
                    and record.route_decision is inspected_route
                    and not record.consumed
                ):
                    self._validate_route_record_integrity(record)
                    return record.route
                raise ContractViolation("action_route_already_issued")
            outstanding = 0
            for record in self._routes.values():
                _require_exact_record(record, _RouteRecord, "action_route_corrupt")
                if not record.consumed:
                    outstanding += 1
            if outstanding >= self._max_ledger_entries:
                raise ContractViolation("action_route_ledger_full")
            if proposal.binding != binding:
                raise ContractViolation("proposal_binding_mismatch")
            if inspected_plan.binding is not binding:
                raise ContractViolation("planned_action_binding_mismatch")
            if (
                inspected_plan.kind is not ActionKind.EXECUTE_ACTION
                or inspected_plan.structural_outcome is not StructuralOutcome.CONTINUE
            ):
                raise ContractViolation("planned_owner_action_route_invalid")
            if inspected_plan.action.capability_intent != proposal_capability.value:
                raise ContractViolation("planned_action_capability_mismatch")
            if inspected_plan.action.operation_intent is not proposal_operation:
                raise ContractViolation("planned_action_operation_mismatch")
            action_id = require_hex(
                inspected_plan.action_id,
                "planned_action_id",
                lengths=(64,),
            )
            route = PlannedOwnerActionRoute(
                binding=binding,
                action_id=action_id,
                route_digest=_digest_parts(
                    "planned-owner-action-route-v1",
                    self._controller_nonce,
                    action_id,
                    *_binding_parts(binding),
                    proposal_capability.value,
                    proposal_operation.value,
                ),
                capability=proposal_capability,
                operation=proposal_operation,
            )
            record = _RouteRecord(
                route=route,
                ticket=ticket,
                context=context,
                planned_action=inspected_plan,
                route_decision=inspected_route,
                route_snapshot=_snapshot_action_route(
                    route,
                    inspected_plan,
                    inspected_route,
                ),
            )
            self._routes[id(route)] = record
            self._route_by_ticket[id(ticket)] = (ticket, record)
            return route

    def issue_request(
        self,
        ticket: AcceptedTurnTicket,
        action_route: PlannedOwnerActionRoute,
        *,
        adapter_draft: AdapterDraft,
        issued_at: float,
        deadline: float,
        idempotency_key: str,
        reason_codes: tuple[str, ...] = (),
    ) -> OwnerActionRequest:
        """Consume one exact owner-action ticket and sign one exact request."""

        try:
            material = _open_adapter_draft(adapter_draft)
        except AdapterDraftRejected as exc:
            raise ContractViolation("adapter_draft_not_canonical") from exc
        shape = _draft_shape(adapter_draft)
        normalized_parameter_digest = require_hex(
            adapter_draft.parameter_digest,
            "parameter_digest",
            lengths=(64,),
        )
        normalized_idempotency = require_hex(
            idempotency_key,
            "idempotency_key",
            lengths=(64,),
        )
        issued = _require_time(issued_at, "issued_at")
        expires = _require_time(deadline, "deadline")

        with self._lock:
            binding, context = self._validate_ticket(ticket)
            route_record = self._canonical_route(action_route, ticket, context)
            if action_route.binding != binding:
                raise ContractViolation("action_route_binding_mismatch")
            if (
                action_route.capability is not shape.capability
                or action_route.operation is not shape.operation
            ):
                raise ContractViolation("proposal_adapter_mismatch")
            if adapter_draft.binding != binding:
                raise ContractViolation("adapter_draft_binding_mismatch")
            if (
                adapter_draft.call_budget != 1
                or not adapter_draft.private_owner_only
                or material.source_interface_digest
                != adapter_draft.source_interface_digest
                or material.descriptor
                is not OWNER_ACTION_ADAPTER_REGISTRY.get(adapter_draft.operation)
            ):
                raise ContractViolation("adapter_draft_authority_mismatch")
            if normalized_idempotency in self._idempotency:
                raise ContractViolation("idempotency_key_replayed")
            if len(self._ledger) >= self._max_ledger_entries:
                raise ContractViolation("owner_action_ledger_full")
            if route_record.denial is not None:
                raise ContractViolation("action_route_denied")

            request_digest = _digest_parts(
                "owner-action-request-v1",
                self._controller_nonce,
                *_binding_parts(binding),
                shape.adapter_id,
                shape.adapter_version,
                shape.capability.value,
                shape.operation.value,
                shape.side_effect.value,
                shape.confirmation_policy.value,
                normalized_parameter_digest,
                normalized_idempotency,
                issued,
                expires,
                action_route.route_digest,
            )
            request = OwnerActionRequest(
                binding=binding,
                action_id=action_route.action_id,
                adapter_id=shape.adapter_id,
                adapter_version=shape.adapter_version,
                request_digest=request_digest,
                parameter_digest=normalized_parameter_digest,
                capability=shape.capability,
                operation=shape.operation,
                side_effect=shape.side_effect,
                issued_at=issued,
                deadline=expires,
                confirmation_policy=shape.confirmation_policy,
                idempotency_key=normalized_idempotency,
                call_budget=1,
                reason_codes=reason_codes,
            )

            record = _ActionRecord(
                request=request,
                action_route=action_route,
                planned_action=route_record.planned_action,
                parameter_record=material.parameter_record,
                adapter_draft=adapter_draft,
                adapter_material=material,
                adapter_shape=shape,
                request_snapshot=_snapshot_request(request),
                authorized_binding=binding,
            )
            self._owner_action_router.claim_route_and_ticket(
                route_record.route_decision,
                ticket=ticket,
            )
            self._ledger[id(request)] = record
            self._idempotency[normalized_idempotency] = record
            route_record.consumed = True
            return request

    def deny_action_route(
        self,
        ticket: AcceptedTurnTicket,
        action_route: PlannedOwnerActionRoute,
        *,
        denied_at: float,
        reason_codes: tuple[str, ...],
    ) -> OwnerActionDenial:
        """Terminate a prepared route without inventing request/adapter material.

        The two upstream claims are serialized by this controller but are not a
        cross-module transaction.  Production wiring remains blocked until E3C
        provides one public composite commit API for both authorities.
        """

        denied = _require_time(denied_at, "denied_at")
        reasons = normalize_reason_codes(reason_codes)
        if not reasons:
            raise ContractViolation("action_denial_reason_required")
        with self._lock:
            binding, context = self._validate_ticket(ticket)
            route_record = self._canonical_route(action_route, ticket, context)
            if route_record.denial is not None:
                raise ContractViolation("action_route_denial_replayed")
            self._owner_action_router.inspect_route(
                route_record.route_decision,
                ticket=ticket,
            )
            denial = object.__new__(OwnerActionDenial)
            object.__setattr__(denial, "binding", binding)
            object.__setattr__(denial, "action_id", action_route.action_id)
            object.__setattr__(
                denial,
                "denial_digest",
                _digest_parts(
                    "owner-action-denial-v1",
                    self._controller_nonce,
                    action_route.route_digest,
                    *_binding_parts(binding),
                    denied,
                    *reasons,
                ),
            )
            object.__setattr__(denial, "status", ActionReceiptStatus.DENIED)
            object.__setattr__(denial, "effect_state", ActionEffectState.NOT_STARTED)
            object.__setattr__(denial, "denied_at", denied)
            object.__setattr__(denial, "reason_codes", reasons)

            self._owner_action_router.claim_route_and_ticket(
                route_record.route_decision,
                ticket=ticket,
            )
            route_record.denial = denial
            route_record.consumed = True
            self._denials[id(denial)] = _DenialRecord(
                denial=denial,
                route_record=route_record,
                denial_snapshot=_snapshot_denial(denial),
            )
            return denial

    def inspect_denial(self, denial: OwnerActionDenial) -> OwnerActionDenial:
        if type(denial) is not OwnerActionDenial:
            raise ContractViolation("action_denial_required")
        with self._lock:
            entry = self._denials.get(id(denial))
            if entry is None:
                raise ContractViolation("action_denial_not_canonical")
            _require_exact_record(
                entry,
                _DenialRecord,
                "action_denial_corrupt",
            )
            _require_exact_snapshot(
                entry.denial_snapshot,
                "action_denial_corrupt",
            )
            if entry.denial is not denial:
                raise ContractViolation("action_denial_not_canonical")
            self._validate_route_record_integrity(entry.route_record)
            if (
                entry.route_record.denial is not denial
                or not entry.route_record.consumed
                or _snapshot_denial(denial) != entry.denial_snapshot
                or denial.binding is not entry.route_record.route.binding
                or denial.action_id != entry.route_record.route.action_id
                or denial.status is not ActionReceiptStatus.DENIED
                or denial.effect_state is not ActionEffectState.NOT_STARTED
                or denial.denial_digest
                != _digest_parts(
                    "owner-action-denial-v1",
                    self._controller_nonce,
                    entry.route_record.route.route_digest,
                    *_binding_parts(denial.binding),
                    denial.denied_at,
                    *denial.reason_codes,
                )
            ):
                raise ContractViolation("action_denial_corrupt")
            if entry.route_record.denial is not denial or not entry.route_record.consumed:
                raise ContractViolation("action_denial_not_active")
            return denial

    def inspect_denial_lineage(
        self,
        denial: OwnerActionDenial,
    ) -> OwnerActionDenialInspection:
        """Return the exact canonical origin plan/route of one exact denial."""

        with self._lock:
            self.inspect_denial(denial)
            entry = self._denials[id(denial)]
            route_record = entry.route_record
            plan = self._planned_action_authority.inspect_plan(
                route_record.planned_action
            )
            self._validate_route_record_integrity(route_record)
            return OwnerActionDenialInspection(
                denial=denial,
                planned_action=plan,
                action_route=route_record.route,
                capability=route_record.route.capability,
                operation=route_record.route.operation,
            )

    def inspect_request(self, request: OwnerActionRequest) -> OwnerActionInspection:
        with self._lock:
            record = self._canonical_record(request)
            return OwnerActionInspection(
                request=record.request,
                state=record.state,
                has_pending=record.pending is not None,
                has_lease=record.lease is not None,
                has_receipt=record.latest_receipt is not None,
            )

    def inspect_request_lineage(
        self,
        request: OwnerActionRequest,
    ) -> OwnerActionRequestLineageInspection:
        """Return the exact canonical origin plan/route of one exact request."""

        with self._lock:
            record = self._canonical_record(request)
            plan = self._planned_action_authority.inspect_plan(record.planned_action)
            route_record = self._routes.get(id(record.action_route))
            if route_record is None:
                raise ContractViolation("request_action_route_not_canonical")
            self._validate_route_record_integrity(route_record)
            if route_record.route is not record.action_route:
                raise ContractViolation("request_action_route_not_canonical")
            return OwnerActionRequestLineageInspection(
                request=request,
                planned_action=plan,
                action_route=record.action_route,
            )

    def lookup_idempotent_request(
        self,
        idempotency_key: str,
    ) -> OwnerActionRequest | None:
        normalized = require_hex(
            idempotency_key,
            "idempotency_key",
            lengths=(64,),
        )
        with self._lock:
            record = self._idempotency.get(normalized)
            if record is None:
                return None
            if type(record) is _LifecycleTombstoneRecord:
                _require_exact_record(
                    record,
                    _LifecycleTombstoneRecord,
                    "lifecycle_tombstone_corrupt",
                )
                self.inspect_lifecycle_tombstone(record.tombstone)
                raise ContractViolation("idempotency_key_reclaimed")
            _require_exact_record(
                record,
                _ActionRecord,
                "owner_action_ledger_corrupt",
            )
            self._validate_action_record_integrity(record)
            return record.request

    def canonical_receipt_for(
        self,
        request: OwnerActionRequest,
    ) -> ActionReceipt | None:
        with self._lock:
            receipt = self._canonical_record(request).latest_receipt
            if receipt is not None:
                self.inspect_receipt(receipt)
            return receipt

    def inspect_receipt(self, receipt: ActionReceipt) -> ActionReceipt:
        if type(receipt) is not ActionReceipt:
            raise ContractViolation("action_receipt_required")
        with self._lock:
            entry = self._receipts.get(id(receipt))
            if entry is None:
                raise ContractViolation("receipt_not_canonical")
            self._validate_receipt_record_integrity(entry)
            if entry.receipt is not receipt:
                raise ContractViolation("receipt_not_canonical")
            return receipt

    def inspect_receipt_lineage(
        self,
        receipt: ActionReceipt,
    ) -> OwnerActionReceiptInspection:
        """Return exact request and origin canonical plan/route for one receipt."""

        with self._lock:
            self.inspect_receipt(receipt)
            entry = self._receipts[id(receipt)]
            record = entry.action_record
            plan = self._planned_action_authority.inspect_plan(record.planned_action)
            route_record = self._routes.get(id(record.action_route))
            if route_record is None:
                raise ContractViolation("receipt_action_route_not_canonical")
            self._validate_route_record_integrity(route_record)
            if route_record.route is not record.action_route:
                raise ContractViolation("receipt_action_route_not_canonical")
            return OwnerActionReceiptInspection(
                receipt=receipt,
                request=record.request,
                planned_action=plan,
                action_route=record.action_route,
            )

    def abort_prepared(
        self,
        request: OwnerActionRequest,
        *,
        ticket: object,
        durable_authority: object,
        aborted_at: float,
    ) -> OwnerActionLifecycleTombstone:
        """Abort one never-started request and release all sensitive material."""

        from .owner_action_durable_finalize import (
            DurableFinalizeTicket,
            OwnerActionDurableFinalizeAuthority,
            _claim_prepared_abort_ticket,
            _inspect_prepared_abort_ticket,
        )

        if type(ticket) is not DurableFinalizeTicket:
            raise ContractViolation("durable_abort_ticket_required")
        if type(durable_authority) is not OwnerActionDurableFinalizeAuthority:
            raise ContractViolation("durable_finalize_authority_not_canonical")
        _inspect_prepared_abort_ticket(
            durable_authority,
            ticket,
            controller=self,
            request=request,
            require_cleaned=False,
        )

        timestamp = _require_time(aborted_at, "aborted_at")
        with self._lock:
            record = self._canonical_record(request)
            if record.state is not OwnerActionLedgerState.PREPARED:
                raise ContractViolation("request_not_prepared")
            if (
                record.base_pending is not None
                or record.base_pending_snapshot is not None
                or record.pending is not None
                or record.pending_snapshot is not None
                or record.lease is not None
                or record.lease_consumed
                or record.latest_receipt is not None
                or record.continuation is not None
            ):
                raise ContractViolation("prepared_abort_state_corrupt")
            if self._ledger.get(id(request)) is not record:
                raise ContractViolation("request_not_canonical")
            if self._idempotency.get(request.idempotency_key) is not record:
                raise ContractViolation("idempotency_ledger_corrupt")
            route_record = self._routes.get(id(record.action_route))
            if route_record is None:
                raise ContractViolation("prepared_abort_route_corrupt")
            self._validate_route_record_integrity(route_record)
            if (
                route_record.route is not record.action_route
                or not route_record.consumed
                or route_record.denial is not None
            ):
                raise ContractViolation("prepared_abort_route_corrupt")
            ticket_entry = self._route_by_ticket.get(id(route_record.ticket))
            if ticket_entry is None:
                raise ContractViolation("prepared_abort_route_corrupt")
            _require_exact_registry_pair(
                ticket_entry,
                "prepared_abort_route_corrupt",
            )
            if (
                ticket_entry[0] is not route_record.ticket
                or ticket_entry[1] is not route_record
            ):
                raise ContractViolation("prepared_abort_route_corrupt")
            for entry in self._leases.values():
                _require_exact_record(
                    entry,
                    _LeaseRecord,
                    "prepared_abort_state_corrupt",
                )
                if entry.action_record is record:
                    raise ContractViolation("prepared_abort_state_corrupt")
            for entry in self._receipts.values():
                _require_exact_record(
                    entry,
                    _ReceiptRecord,
                    "prepared_abort_state_corrupt",
                )
                if entry.action_record is record:
                    raise ContractViolation("prepared_abort_state_corrupt")
            for entry in self._resolution_routes.values():
                _require_exact_record(
                    entry,
                    _PendingResolutionRouteRecord,
                    "prepared_abort_state_corrupt",
                )
                if entry.request is request:
                    raise ContractViolation("prepared_abort_state_corrupt")
            for entry in self._continuations.values():
                _require_exact_registry_pair(
                    entry,
                    "prepared_abort_state_corrupt",
                )
                continuation_record = entry[1]
                _require_exact_record(
                    continuation_record,
                    _ContinuationLineageRecord,
                    "prepared_abort_state_corrupt",
                )
                if continuation_record.origin_request is request:
                    raise ContractViolation("prepared_abort_state_corrupt")

            tombstone, tombstone_record = self._mint_lifecycle_tombstone(
                kind=OwnerActionLifecycleKind.PREPARED_ABORTED,
                operation=request.operation,
                status=ActionReceiptStatus.CANCELLED,
                effect_state=ActionEffectState.NOT_STARTED,
                attempted=False,
                reclaimed_at=timestamp,
                source_fingerprint=_digest_parts(
                    "owner-action-prepared-abort-source-v1",
                    request.action_id,
                    request.request_digest,
                    request.idempotency_key,
                    record.action_route.route_digest,
                ),
            )

            with _ControllerMutationTransaction(self):
                self._register_lifecycle_tombstone(tombstone_record)
                self._idempotency[request.idempotency_key] = tombstone_record
                del self._ledger[id(request)]
                del self._routes[id(record.action_route)]
                del self._route_by_ticket[id(route_record.ticket)]
                _claim_prepared_abort_ticket(
                    durable_authority,
                    ticket,
                    controller=self,
                    request=request,
                    tombstone=tombstone,
                )
            return tombstone

    def purge_prepared_abort_tombstone(
        self,
        tombstone: OwnerActionLifecycleTombstone,
        *,
        ticket: object,
        durable_authority: object,
    ) -> None:
        """Remove a prepared-abort replay fence after durable acknowledgement."""

        from .owner_action_durable_finalize import (
            DurableFinalizeTicket,
            OwnerActionDurableFinalizeAuthority,
            _inspect_completed_prepared_abort,
            _remove_completed_prepared_abort,
        )

        if type(tombstone) is not OwnerActionLifecycleTombstone:
            raise ContractViolation("lifecycle_tombstone_required")
        if type(ticket) is not DurableFinalizeTicket:
            raise ContractViolation("durable_abort_ticket_required")
        if type(durable_authority) is not OwnerActionDurableFinalizeAuthority:
            raise ContractViolation("durable_finalize_authority_not_canonical")
        record = _inspect_completed_prepared_abort(durable_authority, ticket)
        if record.controller_ref() is not self or record.tombstone is not tombstone:
            raise ContractViolation("durable_abort_tombstone_mismatch")
        with self._lock:
            tombstone_record = self._tombstones.get(id(tombstone))
            if tombstone_record is None:
                raise ContractViolation("lifecycle_tombstone_not_canonical")
            _require_exact_record(
                tombstone_record,
                _LifecycleTombstoneRecord,
                "lifecycle_tombstone_corrupt",
            )
            self.inspect_lifecycle_tombstone(tombstone)
            idempotency_keys = tuple(
                key
                for key, value in self._idempotency.items()
                if value is tombstone_record
            )
            with _ControllerMutationTransaction(self):
                for key in idempotency_keys:
                    del self._idempotency[key]
                del self._tombstones[id(tombstone)]
                _remove_completed_prepared_abort(
                    durable_authority,
                    ticket,
                    controller=self,
                    tombstone=tombstone,
                )

    def start_confirmation(
        self,
        request: OwnerActionRequest,
        *,
        now: float,
        ttl: float,
    ) -> PendingConfirmation:
        issued = _require_time(now, "now")
        lifetime = _require_time(ttl, "confirmation_ttl")
        if lifetime <= 0.0:
            raise ContractViolation("confirmation_ttl_invalid")
        with self._lock:
            record = self._canonical_record(request)
            if (
                request.confirmation_policy
                is not ActionConfirmationPolicy.SECOND_TURN_REQUIRED
            ):
                raise ContractViolation("request_confirmation_not_required")
            if record.state is OwnerActionLedgerState.CONFIRMATION_REQUIRED:
                assert record.pending is not None
                self._validate_pending_integrity(
                    record,
                    record.pending,
                    record.pending_snapshot,
                )
                return record.pending
            if record.state is not OwnerActionLedgerState.PREPARED:
                raise ContractViolation("request_not_prepared")
            expires = issued + lifetime
            pending = PendingConfirmation.from_request(
                request,
                pending_id=_digest_parts(
                    "owner-action-pending-v1",
                    self._controller_nonce,
                    request.request_digest,
                    issued,
                    expires,
                ),
                issued_at=issued,
                expires_at=expires,
            )
            receipt = _issue_action_receipt(
                request=request,
                completion_binding=request.binding,
                pending_confirmation=pending,
                status=ActionReceiptStatus.CONFIRMATION_REQUIRED,
                effect_state=ActionEffectState.NOT_STARTED,
                source_interface_digest="",
                reason_codes=("owner_confirmation_required",),
            )
            record.base_pending = pending
            record.base_pending_snapshot = _snapshot_pending(pending)
            record.pending = pending
            record.pending_snapshot = record.base_pending_snapshot
            record.state = OwnerActionLedgerState.CONFIRMATION_REQUIRED
            self._register_receipt(record, receipt)
            return pending

    def _validate_pending_followup(
        self,
        record: _ActionRecord,
        pending: PendingConfirmation,
        ticket: AcceptedTurnTicket,
    ) -> tuple[DecisionBinding, AcceptedTurnContext]:
        binding, context = self._validate_ticket(ticket)
        _assert_followup_binding(pending.binding, binding)
        if pending.request is not record.request:
            raise ContractViolation("pending_request_identity_mismatch")
        return binding, context

    def _pending_candidates(
        self,
        binding: DecisionBinding,
        current_time: float,
    ) -> list[PendingConfirmation]:
        candidates: list[PendingConfirmation] = []
        for record in self._ledger.values():
            _require_exact_record(
                record,
                _ActionRecord,
                "owner_action_ledger_corrupt",
            )
            pending = record.pending
            if (
                record.state
                is not OwnerActionLedgerState.CONFIRMATION_REQUIRED
                or pending is None
            ):
                continue
            self._validate_pending_integrity(
                record,
                pending,
                record.pending_snapshot,
            )
            if pending.consumed or current_time >= pending.expires_at:
                continue
            try:
                _assert_followup_binding(pending.binding, binding)
            except ContractViolation:
                continue
            candidates.append(pending)
        return candidates

    def match_pending(
        self,
        ticket: AcceptedTurnTicket,
        *,
        now: float,
    ) -> PendingMatch:
        """Select 0/1/many solely by the current trusted structural principal.

        The lookup intentionally does not consume the owner-action ticket.  The exact
        same ticket and returned exact pending handle must still pass
        ``route_pending_resolution`` before ``resolve_pending``.  Visible text, model
        output, pending IDs, and quoted content are not lookup inputs.
        """

        current_time = _require_time(now, "now")
        with self._lock:
            binding, _context = self._validate_ticket(ticket)
            candidates = self._pending_candidates(binding, current_time)

            if not candidates:
                return PendingMatch(
                    status=PendingMatchStatus.NONE,
                    candidate_count=0,
                )
            if len(candidates) == 1:
                return PendingMatch(
                    status=PendingMatchStatus.UNIQUE,
                    pending=candidates[0],
                    candidate_count=1,
                )
            return PendingMatch(
                status=PendingMatchStatus.AMBIGUOUS,
                candidate_count=len(candidates),
            )

    def route_pending_resolution(
        self,
        pending: PendingConfirmation,
        ticket: AcceptedTurnTicket,
        *,
        current_message: str,
        now: float,
    ) -> PendingResolutionMatch:
        """Parse one exact current body into a sealed route or return UNCLEAR.

        The authority context intentionally contains only a content digest, so the
        caller supplies the current body.  It is accepted only when its UTF-8 digest
        equals the exact canonical accepted-turn binding.  The body is parsed and
        discarded; it is never stored in the route ledger.
        """

        current_time = _require_time(now, "now")
        if type(current_message) is not str:
            raise ContractViolation("current_message_required")
        with self._lock:
            record = self._canonical_pending(pending)
            if record.state is not OwnerActionLedgerState.CONFIRMATION_REQUIRED:
                raise ContractViolation("pending_not_active")
            binding, context = self._validate_pending_followup(
                record,
                pending,
                ticket,
            )
            if hashlib.sha256(current_message.encode("utf-8")).hexdigest() != (
                binding.current_content_digest
            ):
                raise ContractViolation("current_message_binding_mismatch")
            candidates = self._pending_candidates(binding, current_time)
            if len(candidates) != 1 or candidates[0] is not pending:
                raise ContractViolation("pending_resolution_not_unique")

            decision = _parse_pending_resolution(
                record.request.operation,
                current_message,
                has_reference=bool(
                    context.envelope.reply_to_message_id
                ),
            )
            if decision is None:
                return PendingResolutionMatch(
                    status=PendingResolutionStatus.UNCLEAR,
                )

            existing = self._resolution_route_by_ticket.get(id(ticket))
            if existing is not None:
                _require_exact_registry_pair(
                    existing,
                    "pending_resolution_route_corrupt",
                )
            if existing is not None and existing[0] is ticket:
                route_record = existing[1]
                _require_exact_record(
                    route_record,
                    _PendingResolutionRouteRecord,
                    "pending_resolution_route_corrupt",
                )
                if (
                    route_record.pending is pending
                    and route_record.decision is decision
                    and not route_record.consumed
                ):
                    self._validate_resolution_route_record_integrity(route_record)
                    return PendingResolutionMatch(
                        status=PendingResolutionStatus(decision.value),
                        route=route_record.route,
                    )
                raise ContractViolation("pending_resolution_route_already_issued")
            if len(self._resolution_routes) >= self._max_ledger_entries:
                raise ContractViolation("pending_resolution_route_ledger_full")

            route = PendingResolutionRoute(
                binding=binding,
                route_digest=_digest_parts(
                    "pending-resolution-route-v1",
                    self._controller_nonce,
                    pending.pending_id,
                    record.request.request_digest,
                    record.request.parameter_digest,
                    decision.value,
                    *_binding_parts(binding),
                ),
                request_digest=record.request.request_digest,
                parameter_digest=record.request.parameter_digest,
                decision=decision,
                capability=record.request.capability,
                operation=record.request.operation,
            )
            route_record = _PendingResolutionRouteRecord(
                route=route,
                pending=pending,
                request=record.request,
                ticket=ticket,
                context=context,
                decision=decision,
                route_snapshot=_snapshot_resolution_route(
                    route,
                    pending,
                    record.request,
                    ticket,
                    context,
                ),
                pending_snapshot=_snapshot_pending(pending),
                request_snapshot=record.request_snapshot,
            )
            self._resolution_routes[id(route)] = route_record
            self._resolution_route_by_ticket[id(ticket)] = (
                ticket,
                route_record,
            )
            return PendingResolutionMatch(
                status=PendingResolutionStatus(decision.value),
                route=route,
            )

    def _validate_resolution_route_record_integrity(
        self,
        record: _PendingResolutionRouteRecord,
    ) -> None:
        _require_exact_record(
            record,
            _PendingResolutionRouteRecord,
            "pending_resolution_route_corrupt",
        )
        for snapshot in (
            record.route_snapshot,
            record.pending_snapshot,
            record.request_snapshot,
        ):
            _require_exact_snapshot(
                snapshot,
                "pending_resolution_route_corrupt",
            )
        route = record.route
        action_record = self._ledger.get(id(record.request))
        if action_record is None:
            raise ContractViolation("pending_resolution_route_corrupt")
        self._validate_action_record_integrity(action_record)
        if action_record.request is not record.request:
            raise ContractViolation("pending_resolution_route_corrupt")
        self._validate_pending_integrity(
            action_record,
            record.pending,
            record.pending_snapshot,
        )
        expected_snapshot = _snapshot_resolution_route(
            route,
            record.pending,
            record.request,
            record.ticket,
            record.context,
        )
        if (
            expected_snapshot != record.route_snapshot
            or record.request_snapshot != action_record.request_snapshot
            or route.binding is not record.context.binding
            or route.request_digest != record.request.request_digest
            or route.parameter_digest != record.request.parameter_digest
            or route.decision is not record.decision
            or route.capability is not record.request.capability
            or route.operation is not record.request.operation
            or record.pending.request is not record.request
            or route.route_digest
            != _digest_parts(
                "pending-resolution-route-v1",
                self._controller_nonce,
                record.pending.pending_id,
                record.request.request_digest,
                record.request.parameter_digest,
                record.decision.value,
                *_binding_parts(route.binding),
            )
        ):
            raise ContractViolation("pending_resolution_route_corrupt")

    def _canonical_resolution_route(
        self,
        route: PendingResolutionRoute,
        *,
        allow_consumed: bool = False,
    ) -> _PendingResolutionRouteRecord:
        if type(route) is not PendingResolutionRoute:
            raise ContractViolation("pending_resolution_route_required")
        record = self._resolution_routes.get(id(route))
        if record is None:
            raise ContractViolation("pending_resolution_route_not_canonical")
        self._validate_resolution_route_record_integrity(record)
        if record.route is not route:
            raise ContractViolation("pending_resolution_route_not_canonical")
        if record.consumed and not allow_consumed:
            raise ContractViolation("pending_resolution_route_replayed")
        return record

    def inspect_pending_resolution_route(
        self,
        route: PendingResolutionRoute,
    ) -> PendingResolutionRoute:
        """Non-consuming exact inspection used by the current-turn planner."""

        with self._lock:
            self._canonical_resolution_route(route)
            return route

    def _build_continuation_lineage(
        self,
        *,
        route_record: _PendingResolutionRouteRecord,
        current_planned_action: PlannedAction,
    ) -> tuple[OwnerActionContinuationLineage, _ContinuationLineageRecord]:
        request = route_record.request
        route = route_record.route
        lineage = object.__new__(OwnerActionContinuationLineage)
        object.__setattr__(lineage, "binding", current_planned_action.binding)
        object.__setattr__(lineage, "current_action_id", current_planned_action.action_id)
        object.__setattr__(lineage, "origin_action_id", request.action_id)
        object.__setattr__(
            lineage,
            "lineage_digest",
            _digest_parts(
                "owner-action-continuation-lineage-v1",
                self._controller_nonce,
                current_planned_action.action_id,
                request.action_id,
                request.request_digest,
                route.route_digest,
                route_record.decision.value,
                *_binding_parts(current_planned_action.binding),
            ),
        )
        object.__setattr__(lineage, "decision", route_record.decision)
        object.__setattr__(lineage, "capability", route.capability)
        object.__setattr__(lineage, "operation", route.operation)
        return lineage, _ContinuationLineageRecord(
            lineage=lineage,
            lineage_snapshot=_snapshot_lineage(lineage),
            current_planned_action=current_planned_action,
            origin_request=request,
            origin_request_snapshot=route_record.request_snapshot,
            resolution_route=route,
            resolution_route_snapshot=route_record.route_snapshot,
            decision=route_record.decision,
        )

    def continuation_lineage_for(
        self,
        resolution_route: PendingResolutionRoute,
    ) -> OwnerActionContinuationLineage:
        """Return the lineage issued by one already-consumed exact resolution route."""

        if type(resolution_route) is not PendingResolutionRoute:
            raise ContractViolation("pending_resolution_route_required")
        with self._lock:
            route_record = self._canonical_resolution_route(
                resolution_route,
                allow_consumed=True,
            )
            if not route_record.consumed or route_record.lineage is None:
                raise ContractViolation("continuation_lineage_not_ready")
            return route_record.lineage

    def inspect_continuation_lineage(
        self,
        lineage: OwnerActionContinuationLineage,
    ) -> OwnerActionContinuationInspection:
        """Inspect one controller-canonical lineage without consuming it."""

        if type(lineage) is not OwnerActionContinuationLineage:
            raise ContractViolation("continuation_lineage_required")
        with self._lock:
            entry = self._continuations.get(id(lineage))
            if entry is None:
                raise ContractViolation("continuation_lineage_not_canonical")
            _require_exact_registry_pair(
                entry,
                "continuation_lineage_corrupt",
            )
            if entry[0] is not lineage:
                raise ContractViolation("continuation_lineage_not_canonical")
            record = entry[1]
            _require_exact_record(
                record,
                _ContinuationLineageRecord,
                "continuation_lineage_corrupt",
            )
            for snapshot in (
                record.lineage_snapshot,
                record.origin_request_snapshot,
                record.resolution_route_snapshot,
            ):
                _require_exact_snapshot(
                    snapshot,
                    "continuation_lineage_corrupt",
                )
            if record.receipt_snapshot is not None:
                _require_exact_snapshot(
                    record.receipt_snapshot,
                    "continuation_lineage_corrupt",
                )
            plan = self._planned_action_authority.inspect_plan(
                record.current_planned_action
            )
            route = record.resolution_route
            request = record.origin_request
            route_record = self._canonical_resolution_route(
                route,
                allow_consumed=True,
            )
            action_record = self._canonical_record(request)
            expected_digest = _digest_parts(
                "owner-action-continuation-lineage-v1",
                self._controller_nonce,
                plan.action_id,
                request.action_id,
                request.request_digest,
                route.route_digest,
                record.decision.value,
                *_binding_parts(plan.binding),
            )
            if (
                _snapshot_lineage(lineage) != record.lineage_snapshot
                or record.origin_request_snapshot != action_record.request_snapshot
                or record.resolution_route_snapshot != route_record.route_snapshot
                or route_record.lineage is not lineage
                or route_record.current_planned_action is not plan
                or lineage.binding is not plan.binding
                or lineage.current_action_id != plan.action_id
                or lineage.origin_action_id != request.action_id
                or lineage.lineage_digest != expected_digest
                or lineage.decision is not record.decision
                or lineage.capability is not route.capability
                or lineage.operation is not route.operation
            ):
                raise ContractViolation("continuation_lineage_corrupt")
            receipt = record.receipt
            if receipt is not None:
                self.inspect_receipt(receipt)
                receipt_entry = self._receipts.get(id(receipt))
                if (
                    receipt_entry is None
                    or record.receipt_snapshot is None
                    or receipt_entry.receipt_snapshot != record.receipt_snapshot
                ):
                    raise ContractViolation("continuation_receipt_lineage_mismatch")
                if (
                    receipt.request_digest != request.request_digest
                    or receipt.idempotency_key != request.idempotency_key
                    or receipt.origin_binding != request.binding
                    or receipt.binding != plan.binding
                    or receipt.action_id != request.action_id
                ):
                    raise ContractViolation("continuation_receipt_lineage_mismatch")
            return OwnerActionContinuationInspection(
                lineage=lineage,
                current_planned_action=plan,
                origin_request=request,
                resolution_route=route,
                receipt=receipt,
            )

    def resolve_pending(
        self,
        resolution_route: PendingResolutionRoute,
        ticket: AcceptedTurnTicket,
        *,
        current_planned_action: PlannedAction,
        now: float,
    ) -> PendingConfirmation | ActionReceipt:
        """Consume one exact code-owned current-message resolution route."""

        resolved_at = _require_time(now, "now")
        with self._lock:
            route_record = self._canonical_resolution_route(resolution_route)
            pending = route_record.pending
            record = self._canonical_pending(pending)
            if record.state is not OwnerActionLedgerState.CONFIRMATION_REQUIRED:
                raise ContractViolation("pending_not_active")
            binding, context = self._validate_pending_followup(
                record,
                pending,
                ticket,
            )
            inspected_plan = self._planned_action_authority.inspect_plan(
                current_planned_action
            )
            if (
                route_record.ticket is not ticket
                or route_record.context is not context
                or resolution_route.binding != binding
                or resolution_route.request_digest != record.request.request_digest
                or resolution_route.parameter_digest
                != record.request.parameter_digest
                or resolution_route.decision is not route_record.decision
                or resolution_route.capability is not record.request.capability
                or resolution_route.operation is not record.request.operation
                or route_record.request is not record.request
            ):
                raise ContractViolation("pending_resolution_route_binding_mismatch")
            if (
                inspected_plan.binding is not binding
                or inspected_plan.structural_outcome is not StructuralOutcome.CONTINUE
                or inspected_plan.action.reply_target is None
                or inspected_plan.action.reply_target.message_id
                != binding.current_message_id
                or inspected_plan.action.reply_target.sender_key
                != binding.current_sender_key
                or inspected_plan.action.reply_target.session_id != binding.session_id
                or inspected_plan.action.reply_target.scope_key != binding.scope_key
                or inspected_plan.action.reply_target.content_digest
                != binding.current_content_digest
                or inspected_plan.action_id == record.request.action_id
            ):
                raise ContractViolation("pending_resolution_plan_mismatch")
            if route_record.decision is PendingDecision.CONFIRM:
                if (
                    inspected_plan.kind is not ActionKind.EXECUTE_ACTION
                    or inspected_plan.action.capability_intent
                    != resolution_route.capability.value
                    or inspected_plan.action.operation_intent
                    is not resolution_route.operation
                ):
                    raise ContractViolation("pending_resolution_plan_mismatch")
            elif (
                inspected_plan.kind is not ActionKind.REPLY
                or inspected_plan.action.capability_intent
                or inspected_plan.action.operation_intent is not None
            ):
                raise ContractViolation("pending_resolution_plan_mismatch")
            proof_digest = _digest_parts(
                "owner-action-confirmation-v1",
                self._controller_nonce,
                pending.pending_id,
                *_binding_parts(binding),
                record.request.request_digest,
                record.request.parameter_digest,
                resolution_route.route_digest,
            )
            consumed = pending.consume_confirmation(
                confirmation_binding=binding,
                request_digest=record.request.request_digest,
                parameter_digest=record.request.parameter_digest,
                confirmation_proof_digest=proof_digest,
                now=resolved_at,
            )
            receipt: ActionReceipt | None = None
            if route_record.decision is not PendingDecision.CONFIRM:
                status = (
                    ActionReceiptStatus.DENIED
                    if route_record.decision is PendingDecision.DENY
                    else ActionReceiptStatus.CANCELLED
                )
                reason = (
                    "owner_action_denied"
                    if route_record.decision is PendingDecision.DENY
                    else "owner_action_cancelled"
                )
                receipt = _issue_action_receipt(
                    request=record.request,
                    completion_binding=binding,
                    pending_confirmation=pending,
                    status=status,
                    effect_state=ActionEffectState.NOT_STARTED,
                    source_interface_digest="",
                    reason_codes=(reason,),
                )

            lineage, lineage_record = self._build_continuation_lineage(
                route_record=route_record,
                current_planned_action=inspected_plan,
            )

            self._accepted_turn_authority.claim_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.OWNER_ACTION,
                binding=binding,
                context=context,
            )
            route_record.current_planned_action = inspected_plan
            route_record.lineage = lineage
            route_record.consumed = True
            record.continuation = lineage_record
            self._continuations[id(lineage)] = (lineage, lineage_record)
            if route_record.decision is PendingDecision.CONFIRM:
                record.pending = consumed
                record.pending_snapshot = _snapshot_pending(consumed)
                record.authorized_binding = binding
                record.state = OwnerActionLedgerState.READY
                return consumed
            assert receipt is not None
            record.authorized_binding = binding
            record.state = OwnerActionLedgerState.TERMINAL
            return self._register_receipt(record, receipt)

    def sweep_expired(self, *, now: float) -> tuple[ActionReceipt, ...]:
        """Terminate expired pending actions without borrowing a new turn ticket."""

        expired_at = _require_time(now, "now")
        receipts: list[ActionReceipt] = []
        with self._lock:
            for record in self._ledger.values():
                _require_exact_record(
                    record,
                    _ActionRecord,
                    "owner_action_ledger_corrupt",
                )
                pending = record.pending
                if (
                    record.state
                    is not OwnerActionLedgerState.CONFIRMATION_REQUIRED
                    or pending is None
                ):
                    continue
                self._validate_pending_integrity(
                    record,
                    pending,
                    record.pending_snapshot,
                )
                if pending.consumed or expired_at < pending.expires_at:
                    continue
                receipt = _issue_action_receipt(
                    request=record.request,
                    completion_binding=record.request.binding,
                    pending_confirmation=pending,
                    status=ActionReceiptStatus.STALE,
                    effect_state=ActionEffectState.NOT_STARTED,
                    source_interface_digest="",
                    reason_codes=("pending_confirmation_expired",),
                )
                record.authorized_binding = record.request.binding
                record.state = OwnerActionLedgerState.TERMINAL
                receipts.append(self._register_receipt(record, receipt))
        return tuple(receipts)

    def _terminal_claim(self, record: _ActionRecord) -> ActionClaim:
        receipt = record.latest_receipt
        if receipt is None or receipt.status is ActionReceiptStatus.CONFIRMATION_REQUIRED:
            raise ContractViolation("terminal_receipt_missing")
        self.inspect_receipt(receipt)
        return ActionClaim(
            status=ActionClaimStatus.TERMINAL,
            receipt=receipt,
        )

    def claim_execution(
        self,
        request: OwnerActionRequest,
        *,
        current_binding: DecisionBinding,
        now: float,
        pending_confirmation: PendingConfirmation | None = None,
        stale: bool = False,
    ) -> ActionClaim:
        """Atomically claim the only lease or return the existing terminal receipt."""

        if not isinstance(current_binding, DecisionBinding):
            raise ContractViolation("current_binding_invalid")
        if type(stale) is not bool:
            raise ContractViolation("stale_flag_invalid")
        claimed_at = _require_time(now, "now")
        with self._lock:
            record = self._canonical_record(request)
            expected = record.authorized_binding
            if expected is None or current_binding is not expected:
                raise ContractViolation("execution_binding_mismatch")
            if record.state is OwnerActionLedgerState.TERMINAL:
                return self._terminal_claim(record)
            if record.state is OwnerActionLedgerState.IN_PROGRESS:
                if record.lease is None:
                    raise ContractViolation("execution_lease_not_active")
                lease_entry = self._leases.get(id(record.lease))
                if lease_entry is None:
                    raise ContractViolation("execution_lease_not_canonical")
                self._validate_lease_record_integrity(lease_entry)
                if lease_entry.lease is not record.lease:
                    raise ContractViolation("execution_lease_not_canonical")
                return ActionClaim(status=ActionClaimStatus.IN_PROGRESS)

            if (
                request.confirmation_policy
                is ActionConfirmationPolicy.SECOND_TURN_REQUIRED
            ):
                if record.state is not OwnerActionLedgerState.READY:
                    raise ContractViolation("confirmation_required_before_execution")
                if (
                    pending_confirmation is None
                    or record.pending is not pending_confirmation
                    or not pending_confirmation.consumed
                ):
                    raise ContractViolation("confirmed_pending_not_canonical")
                self._validate_pending_integrity(
                    record,
                    pending_confirmation,
                    record.pending_snapshot,
                )
            else:
                if record.state is not OwnerActionLedgerState.PREPARED:
                    raise ContractViolation("request_not_prepared")
                if pending_confirmation is not None:
                    raise ContractViolation("pending_confirmation_forbidden")

            if claimed_at < request.issued_at:
                raise ContractViolation("execution_before_request_issue")
            if stale or claimed_at >= request.deadline:
                receipt = _issue_action_receipt(
                    request=request,
                    completion_binding=current_binding,
                    pending_confirmation=(
                        record.base_pending
                        if request.confirmation_policy
                        is ActionConfirmationPolicy.SECOND_TURN_REQUIRED
                        else None
                    ),
                    status=ActionReceiptStatus.STALE,
                    effect_state=ActionEffectState.NOT_STARTED,
                    source_interface_digest="",
                    reason_codes=("stale_before_start",),
                )
                record.state = OwnerActionLedgerState.TERMINAL
                self._register_receipt(record, receipt)
                return self._terminal_claim(record)

            lease = ExecutionLease(
                binding=current_binding,
                request_digest=request.request_digest,
                lease_digest=_digest_parts(
                    "owner-action-lease-v1",
                    self._controller_nonce,
                    request.request_digest,
                    *_binding_parts(current_binding),
                    claimed_at,
                ),
                capability=request.capability,
                operation=request.operation,
                issued_at=claimed_at,
                deadline=request.deadline,
            )
            record.lease = lease
            record.state = OwnerActionLedgerState.IN_PROGRESS
            self._leases[id(lease)] = _LeaseRecord(
                lease=lease,
                action_record=record,
                lease_snapshot=_snapshot_lease(lease),
            )
            return ActionClaim(
                status=ActionClaimStatus.CLAIMED,
                lease=lease,
            )

    def complete_execution(
        self,
        lease: ExecutionLease,
        blueprint: object,
    ) -> ActionReceipt:
        """Consume one exact sealed blueprint and issue the only terminal receipt."""

        if type(lease) is not ExecutionLease:
            raise ContractViolation("execution_lease_required")

        # Lock order is fixed: Controller -> E2C blueprint registry -> receipt issuer.
        # No await, callback, or tool invocation occurs while this lock is held.
        with self._lock:
            entry = self._leases.get(id(lease))
            if entry is None:
                raise ContractViolation("execution_lease_not_canonical")
            self._validate_lease_record_integrity(entry)
            if entry.lease is not lease:
                raise ContractViolation("execution_lease_not_canonical")
            record = entry.action_record
            if record.lease_consumed or record.state is OwnerActionLedgerState.TERMINAL:
                raise ContractViolation("execution_lease_consumed")
            if record.lease is not lease or record.state is not OwnerActionLedgerState.IN_PROGRESS:
                raise ContractViolation("execution_lease_not_active")

            pending = (
                record.pending
                if record.request.confirmation_policy
                is ActionConfirmationPolicy.SECOND_TURN_REQUIRED
                else None
            )
            if pending is not None:
                self._validate_pending_integrity(
                    record,
                    pending,
                    record.pending_snapshot,
                )

            # Local import avoids reversing the executor -> Controller module edge.
            from .owner_action_executor import (
                OwnerActionExecutionRejected,
                OwnerExecutionBlueprint,
                _CompletionKind,
                _open_execution_blueprint,
            )

            if type(blueprint) is not OwnerExecutionBlueprint:
                raise ContractViolation("execution_blueprint_required")
            try:
                completion = _open_execution_blueprint(
                    blueprint,
                    controller=self,
                    request=record.request,
                    lease=lease,
                )
            except OwnerActionExecutionRejected as exc:
                raise ContractViolation(exc.reason_code) from None

            confirmation_proof = (
                "" if pending is None else pending.confirmation_proof_digest
            )
            if completion.kind is _CompletionKind.PRE_CALL_TERMINATED:
                source_interface_digest = ""
                result_digest = ""
                started_at = 0.0
                completed_at = 0.0
                attempt_count = 0
            else:
                source_interface_digest = record.adapter_shape.source_interface_digest
                result_digest = completion.result_digest
                started_at = lease.issued_at
                completed_at = completion.completed_at
                attempt_count = 1

            receipt = _issue_action_receipt(
                request=record.request,
                completion_binding=lease.binding,
                pending_confirmation=pending,
                status=completion.status,
                effect_state=completion.effect_state,
                source_interface_digest=source_interface_digest,
                result_digest=result_digest,
                output=completion.output,
                confirmation_proof_digest=confirmation_proof,
                started_at=started_at,
                completed_at=completed_at,
                attempt_count=attempt_count,
                reason_codes=completion.reason_codes,
            )
            record.lease_consumed = True
            record.state = OwnerActionLedgerState.TERMINAL
            return self._register_receipt(record, receipt)

    def recover_interrupted_execution(
        self,
        lease: ExecutionLease,
        *,
        recovered_at: float,
    ) -> ActionReceipt:
        """Terminalize an interrupted exact lease without executing or retrying it."""

        if type(lease) is not ExecutionLease:
            raise ContractViolation("execution_lease_required")
        completed = _require_time(recovered_at, "recovered_at")
        with self._lock:
            entry = self._leases.get(id(lease))
            if entry is None:
                raise ContractViolation("execution_lease_not_canonical")
            self._validate_lease_record_integrity(entry)
            if entry.lease is not lease:
                raise ContractViolation("execution_lease_not_canonical")
            record = entry.action_record
            if record.lease_consumed or record.state is OwnerActionLedgerState.TERMINAL:
                raise ContractViolation("execution_lease_consumed")
            if record.lease is not lease or record.state is not OwnerActionLedgerState.IN_PROGRESS:
                raise ContractViolation("execution_lease_not_active")
            if completed < lease.issued_at:
                raise ContractViolation("recovered_before_execution_start")
            if frozenset(_INTERRUPTED_RECOVERY_OUTCOME) != frozenset(
                OwnerActionOperation
            ):
                raise ContractViolation("owner_action_recovery_projection_incomplete")
            try:
                status, effect_state, reason_code = (
                    _INTERRUPTED_RECOVERY_OUTCOME[record.request.operation]
                )
            except KeyError:
                raise ContractViolation(
                    "owner_action_recovery_operation_unmapped"
                ) from None

            pending = (
                record.pending
                if record.request.confirmation_policy
                is ActionConfirmationPolicy.SECOND_TURN_REQUIRED
                else None
            )
            confirmation_proof = ""
            if pending is not None:
                self._validate_pending_integrity(
                    record,
                    pending,
                    record.pending_snapshot,
                )
                if not pending.consumed:
                    raise ContractViolation("confirmed_pending_not_canonical")
                confirmation_proof = pending.confirmation_proof_digest

            receipt = _issue_action_receipt(
                request=record.request,
                completion_binding=lease.binding,
                pending_confirmation=pending,
                status=status,
                effect_state=effect_state,
                source_interface_digest=record.adapter_shape.source_interface_digest,
                result_digest=_digest_parts(
                    "owner-action-interrupted-recovery-result-v1",
                    self._controller_nonce,
                    record.request.request_digest,
                    lease.lease_digest,
                    status.value,
                    effect_state.value,
                    lease.issued_at,
                    completed,
                ),
                confirmation_proof_digest=confirmation_proof,
                started_at=lease.issued_at,
                completed_at=completed,
                attempt_count=1,
                reason_codes=(reason_code,),
            )
            transaction_fields: list[tuple[object, tuple[str, ...]]] = [
                (
                    record,
                    ("lease_consumed", "state", "latest_receipt"),
                )
            ]
            if record.continuation is not None:
                transaction_fields.append(
                    (
                        record.continuation,
                        ("receipt", "receipt_snapshot"),
                    )
                )
            with _ControllerMutationTransaction(
                self,
                record_fields=tuple(transaction_fields),
            ):
                record.lease_consumed = True
                record.state = OwnerActionLedgerState.TERMINAL
                return self._register_receipt(record, receipt)

    def release_terminal_lineage(
        self,
        source: ActionReceipt | OwnerActionDenial | OwnerActionContinuationLineage,
        *,
        outcome: object,
        outcome_authority: object,
        ticket: object,
        durable_authority: object,
        reclaimed_at: float,
    ) -> OwnerActionLifecycleTombstone:
        """Release exact terminal lineage after an outcome lifecycle terminal."""

        from .action_outcome import ActionOutcomeAuthority, ActionOutcomeIntent
        from .owner_action_durable_finalize import (
            DurableFinalizeTicket,
            OwnerActionDurableFinalizeAuthority,
            _claim_controller_finalize_ticket,
            _inspect_controller_finalize_ticket,
        )

        timestamp = _require_time(reclaimed_at, "reclaimed_at")
        if type(outcome) is not ActionOutcomeIntent:
            raise ContractViolation("action_outcome_required")
        if type(outcome_authority) is not ActionOutcomeAuthority:
            raise ContractViolation("action_outcome_authority_not_canonical")
        if type(ticket) is not DurableFinalizeTicket:
            raise ContractViolation("durable_finalize_ticket_required")
        if type(durable_authority) is not OwnerActionDurableFinalizeAuthority:
            raise ContractViolation("durable_finalize_authority_not_canonical")
        _inspect_controller_finalize_ticket(
            durable_authority,
            ticket,
            controller=self,
            source=source,
        )

        with self._lock:
            self._inspect_action_outcome_authority(
                outcome_authority,
                self._planned_action_authority,
            )
            action_record: _ActionRecord | None = None
            denial_record: _DenialRecord | None = None
            if type(source) is ActionReceipt:
                self.inspect_receipt(source)
                receipt_entry = self._receipts[id(source)]
                action_record = receipt_entry.action_record
                if (
                    action_record.state is not OwnerActionLedgerState.TERMINAL
                    or action_record.latest_receipt is not source
                    or source.status is ActionReceiptStatus.CONFIRMATION_REQUIRED
                    or action_record.continuation is not None
                ):
                    raise ContractViolation("action_lineage_not_terminal")
                operation = source.operation
                status = source.status
                effect_state = source.effect_state
                attempted = source.attempt_count == 1
                source_fingerprint = _digest_parts(
                    "owner-action-terminal-receipt-source-v1",
                    source.action_id,
                    source.request_digest,
                    source.idempotency_key,
                    source.status.value,
                    source.effect_state.value,
                    source.result_digest,
                    source.completed_at,
                )
            elif type(source) is OwnerActionDenial:
                self.inspect_denial(source)
                denial_record = self._denials[id(source)]
                operation = denial_record.route_record.route.operation
                status = source.status
                effect_state = source.effect_state
                attempted = False
                source_fingerprint = _digest_parts(
                    "owner-action-terminal-denial-source-v1",
                    source.action_id,
                    source.denial_digest,
                    source.status.value,
                    source.effect_state.value,
                    source.denied_at,
                )
            elif type(source) is OwnerActionContinuationLineage:
                inspection = self.inspect_continuation_lineage(source)
                receipt = inspection.receipt
                if receipt is None:
                    raise ContractViolation("action_lineage_not_terminal")
                receipt_entry = self._receipts.get(id(receipt))
                if receipt_entry is None:
                    raise ContractViolation("receipt_not_canonical")
                self._validate_receipt_record_integrity(receipt_entry)
                if receipt_entry.receipt is not receipt:
                    raise ContractViolation("receipt_not_canonical")
                action_record = receipt_entry.action_record
                if (
                    action_record.state is not OwnerActionLedgerState.TERMINAL
                    or action_record.latest_receipt is not receipt
                    or receipt.status is ActionReceiptStatus.CONFIRMATION_REQUIRED
                    or action_record.continuation is None
                    or action_record.continuation.lineage is not source
                ):
                    raise ContractViolation("action_lineage_not_terminal")
                operation = source.operation
                status = receipt.status
                effect_state = receipt.effect_state
                attempted = receipt.attempt_count == 1
                source_fingerprint = _digest_parts(
                    "owner-action-terminal-continuation-source-v1",
                    source.current_action_id,
                    source.origin_action_id,
                    source.lineage_digest,
                    receipt.request_digest,
                    receipt.status.value,
                    receipt.effect_state.value,
                    receipt.result_digest,
                    receipt.completed_at,
                )
            else:
                raise ContractViolation("action_terminal_source_required")

            reclaim_mode = outcome_authority._inspect_reclaimed_source(
                outcome,
                source,
            )
            if reclaim_mode not in {
                "abandoned_before_composer",
                "delivery_terminal",
            }:
                raise ContractViolation("action_outcome_reclaim_state_corrupt")

            tombstone, tombstone_record = self._mint_lifecycle_tombstone(
                kind=OwnerActionLifecycleKind.TERMINAL_RELEASED,
                operation=operation,
                status=status,
                effect_state=effect_state,
                attempted=attempted,
                reclaimed_at=timestamp,
                source_fingerprint=source_fingerprint,
            )

            if denial_record is not None:
                _require_exact_record(
                    denial_record,
                    _DenialRecord,
                    "action_denial_release_state_corrupt",
                )
                route_record = denial_record.route_record
                self._validate_route_record_integrity(route_record)
                ticket_entry = self._route_by_ticket.get(id(route_record.ticket))
                if ticket_entry is None:
                    raise ContractViolation("action_denial_release_state_corrupt")
                _require_exact_registry_pair(
                    ticket_entry,
                    "action_denial_release_state_corrupt",
                )
                if (
                    self._denials.get(id(source)) is not denial_record
                    or self._routes.get(id(route_record.route)) is not route_record
                    or ticket_entry[0] is not route_record.ticket
                    or ticket_entry[1] is not route_record
                    or route_record.denial is not source
                    or not route_record.consumed
                ):
                    raise ContractViolation("action_denial_release_state_corrupt")
                with _ControllerMutationTransaction(self):
                    self._register_lifecycle_tombstone(tombstone_record)
                    del self._denials[id(source)]
                    del self._routes[id(route_record.route)]
                    del self._route_by_ticket[id(route_record.ticket)]
                    _claim_controller_finalize_ticket(
                        durable_authority,
                        ticket,
                        controller=self,
                        source=source,
                        tombstone=tombstone,
                    )
                return tombstone

            assert action_record is not None
            self._validate_action_record_integrity(action_record)
            request = action_record.request
            if (
                self._ledger.get(id(request)) is not action_record
                or self._idempotency.get(request.idempotency_key) is not action_record
                or action_record.state is not OwnerActionLedgerState.TERMINAL
            ):
                raise ContractViolation("terminal_release_state_corrupt")
            route_record = self._routes.get(id(action_record.action_route))
            if route_record is None:
                raise ContractViolation("terminal_release_route_corrupt")
            self._validate_route_record_integrity(route_record)
            if (
                route_record.route is not action_record.action_route
                or not route_record.consumed
            ):
                raise ContractViolation("terminal_release_route_corrupt")
            ticket_entry = self._route_by_ticket.get(id(route_record.ticket))
            if ticket_entry is None:
                raise ContractViolation("terminal_release_route_corrupt")
            _require_exact_registry_pair(
                ticket_entry,
                "terminal_release_route_corrupt",
            )
            if (
                ticket_entry[0] is not route_record.ticket
                or ticket_entry[1] is not route_record
            ):
                raise ContractViolation("terminal_release_route_corrupt")

            receipt_entries_list: list[tuple[int, _ReceiptRecord]] = []
            for key, entry in self._receipts.items():
                _require_exact_record(
                    entry,
                    _ReceiptRecord,
                    "terminal_release_receipt_corrupt",
                )
                if entry.action_record is action_record:
                    receipt_entries_list.append((key, entry))
            receipt_entries = tuple(receipt_entries_list)
            if not receipt_entries or all(
                entry.receipt is not action_record.latest_receipt
                for _key, entry in receipt_entries
            ):
                raise ContractViolation("terminal_release_receipt_corrupt")
            for key, entry in receipt_entries:
                if key != id(entry.receipt):
                    raise ContractViolation("terminal_release_receipt_corrupt")
                self._validate_receipt_record_integrity(entry)

            lease_entries_list: list[tuple[int, _LeaseRecord]] = []
            for key, entry in self._leases.items():
                _require_exact_record(
                    entry,
                    _LeaseRecord,
                    "terminal_release_lease_corrupt",
                )
                if entry.action_record is action_record:
                    lease_entries_list.append((key, entry))
            lease_entries = tuple(lease_entries_list)
            for key, entry in lease_entries:
                if key != id(entry.lease):
                    raise ContractViolation("terminal_release_lease_corrupt")
                self._validate_lease_record_integrity(entry)
            if action_record.lease is None:
                if lease_entries:
                    raise ContractViolation("terminal_release_lease_corrupt")
            elif (
                len(lease_entries) != 1
                or lease_entries[0][1].lease is not action_record.lease
                or not action_record.lease_consumed
            ):
                raise ContractViolation("terminal_release_lease_corrupt")

            resolution_entries_list: list[
                tuple[int, _PendingResolutionRouteRecord]
            ] = []
            for key, entry in self._resolution_routes.items():
                _require_exact_record(
                    entry,
                    _PendingResolutionRouteRecord,
                    "terminal_release_resolution_corrupt",
                )
                if entry.request is request:
                    resolution_entries_list.append((key, entry))
            resolution_entries = tuple(resolution_entries_list)
            for key, entry in resolution_entries:
                if key != id(entry.route):
                    raise ContractViolation("terminal_release_resolution_corrupt")
                self._validate_resolution_route_record_integrity(entry)
                mirror = self._resolution_route_by_ticket.get(id(entry.ticket))
                if mirror is None:
                    raise ContractViolation("terminal_release_resolution_corrupt")
                _require_exact_registry_pair(
                    mirror,
                    "terminal_release_resolution_corrupt",
                )
                if (
                    mirror[0] is not entry.ticket
                    or mirror[1] is not entry
                ):
                    raise ContractViolation("terminal_release_resolution_corrupt")
            mirrored_resolution_records_list: list[
                _PendingResolutionRouteRecord
            ] = []
            for mirror in self._resolution_route_by_ticket.values():
                _require_exact_registry_pair(
                    mirror,
                    "terminal_release_resolution_corrupt",
                )
                mirrored_record = mirror[1]
                _require_exact_record(
                    mirrored_record,
                    _PendingResolutionRouteRecord,
                    "terminal_release_resolution_corrupt",
                )
                if mirrored_record.request is request:
                    mirrored_resolution_records_list.append(mirrored_record)
            mirrored_resolution_records = tuple(
                mirrored_resolution_records_list
            )
            if {id(entry) for entry in mirrored_resolution_records} != {
                id(entry) for _key, entry in resolution_entries
            }:
                raise ContractViolation("terminal_release_resolution_corrupt")

            continuation_entries_list: list[
                tuple[int, OwnerActionContinuationLineage, _ContinuationLineageRecord]
            ] = []
            for key, continuation_pair in self._continuations.items():
                _require_exact_registry_pair(
                    continuation_pair,
                    "terminal_release_continuation_corrupt",
                )
                lineage, entry = continuation_pair
                if type(lineage) is not OwnerActionContinuationLineage:
                    raise ContractViolation("terminal_release_continuation_corrupt")
                _require_exact_record(
                    entry,
                    _ContinuationLineageRecord,
                    "terminal_release_continuation_corrupt",
                )
                if entry.origin_request is request:
                    continuation_entries_list.append((key, lineage, entry))
            continuation_entries = tuple(continuation_entries_list)
            for key, lineage, entry in continuation_entries:
                if key != id(lineage) or entry.lineage is not lineage:
                    raise ContractViolation("terminal_release_continuation_corrupt")
                self.inspect_continuation_lineage(lineage)
            if action_record.continuation is None:
                if continuation_entries:
                    raise ContractViolation("terminal_release_continuation_corrupt")
            elif (
                len(continuation_entries) != 1
                or continuation_entries[0][2] is not action_record.continuation
            ):
                raise ContractViolation("terminal_release_continuation_corrupt")

            with _ControllerMutationTransaction(self):
                self._register_lifecycle_tombstone(tombstone_record)
                self._idempotency[request.idempotency_key] = tombstone_record
                for key, _entry in receipt_entries:
                    del self._receipts[key]
                for key, _entry in lease_entries:
                    del self._leases[key]
                for key, entry in resolution_entries:
                    del self._resolution_routes[key]
                    del self._resolution_route_by_ticket[id(entry.ticket)]
                for key, _lineage, _entry in continuation_entries:
                    del self._continuations[key]
                del self._ledger[id(request)]
                del self._routes[id(action_record.action_route)]
                del self._route_by_ticket[id(route_record.ticket)]
                _claim_controller_finalize_ticket(
                    durable_authority,
                    ticket,
                    controller=self,
                    source=source,
                    tombstone=tombstone,
                )
            return tombstone

    def purge_durable_lifecycle_tombstone(
        self,
        tombstone: OwnerActionLifecycleTombstone,
        *,
        ticket: object,
        durable_authority: object,
    ) -> None:
        """Remove the last in-memory replay fence after durable dual cleanup."""

        from .owner_action_durable_finalize import (
            DurableFinalizeTicket,
            OwnerActionDurableFinalizeAuthority,
            _claim_completed_controller_finalize,
            _inspect_completed_controller_finalize,
        )

        if type(tombstone) is not OwnerActionLifecycleTombstone:
            raise ContractViolation("lifecycle_tombstone_required")
        if type(ticket) is not DurableFinalizeTicket:
            raise ContractViolation("durable_finalize_ticket_required")
        if type(durable_authority) is not OwnerActionDurableFinalizeAuthority:
            raise ContractViolation("durable_finalize_authority_not_canonical")
        _inspect_completed_controller_finalize(
            durable_authority,
            ticket,
            controller=self,
            tombstone=tombstone,
        )
        with self._lock:
            record = self._tombstones.get(id(tombstone))
            if record is None:
                raise ContractViolation("lifecycle_tombstone_not_canonical")
            _require_exact_record(
                record,
                _LifecycleTombstoneRecord,
                "lifecycle_tombstone_corrupt",
            )
            self.inspect_lifecycle_tombstone(tombstone)
            idempotency_keys = tuple(
                key for key, value in self._idempotency.items() if value is record
            )
            with _ControllerMutationTransaction(self):
                for key in idempotency_keys:
                    del self._idempotency[key]
                del self._tombstones[id(tombstone)]
                _claim_completed_controller_finalize(
                    durable_authority,
                    ticket,
                    controller=self,
                    tombstone=tombstone,
                )

    def trace_metadata(self) -> dict[str, int | bool]:
        with self._lock:
            states_list: list[OwnerActionLedgerState] = []
            for record in self._ledger.values():
                _require_exact_record(
                    record,
                    _ActionRecord,
                    "owner_action_ledger_corrupt",
                )
                states_list.append(record.state)
            states = tuple(states_list)
            return {
                "schema_version": 1,
                "adapter_count": len(OWNER_ACTION_ADAPTER_REGISTRY),
                "ledger_count": len(self._ledger),
                "pending_count": sum(
                    state is OwnerActionLedgerState.CONFIRMATION_REQUIRED
                    for state in states
                ),
                "in_progress_count": sum(
                    state is OwnerActionLedgerState.IN_PROGRESS
                    for state in states
                ),
                "terminal_count": sum(
                    state is OwnerActionLedgerState.TERMINAL
                    for state in states
                ),
                "pending_resolution_route_count": len(self._resolution_routes),
                "continuation_lineage_count": len(self._continuations),
                "denial_count": len(self._denials),
                "lifecycle_tombstone_count": len(self._tombstones),
                "parameters_visible": False,
            }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "OwnerActionController("
            f"adapter_count={metadata['adapter_count']}, "
            f"ledger_count={metadata['ledger_count']}, "
            "canonical_owner_ticket_required=True, parameters_visible=False)"
        )


__all__ = [
    "ActionClaim",
    "ActionClaimStatus",
    "ExecutionLease",
    "OwnerActionController",
    "OwnerActionContinuationInspection",
    "OwnerActionContinuationLineage",
    "OwnerActionDenial",
    "OwnerActionDenialInspection",
    "OwnerActionInspection",
    "OwnerActionLifecycleKind",
    "OwnerActionLifecycleTombstone",
    "OwnerActionLedgerState",
    "OwnerActionReceiptInspection",
    "OwnerActionRequestLineageInspection",
    "PendingDecision",
    "PendingMatch",
    "PendingMatchStatus",
    "PendingResolutionMatch",
    "PendingResolutionRoute",
    "PendingResolutionStatus",
    "PlannedOwnerActionRoute",
]
