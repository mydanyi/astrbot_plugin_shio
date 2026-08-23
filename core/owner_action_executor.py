"""Sealed, fail-closed executor for the closed trusted-owner action surface.

P3-08E3C1A deliberately does not make a production capability executable.  The live
runtime allowlist is empty and the artifact layer has no handle-bound pre/post identity
proof issuer yet.  Every canonical claimed lease nevertheless reaches one sealed
blueprint, including pre-call hard blocks, so the Controller can terminalize it.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import threading
import time
import weakref
from collections.abc import Callable, Mapping
from dataclasses import InitVar, dataclass, field
from enum import Enum
from typing import Any

from . import owner_action_adapters as adapters
from . import owner_action_runtime_collector as runtime_collector
from .contracts import (
    ActionEffectState,
    ActionOutput,
    ActionReceiptStatus,
    ActionSideEffect,
    DecisionBinding,
    OwnerActionOperation,
    OwnerActionRequest,
)
from .contracts.owner_action import _open_action_output
from .owner_action_controller import ExecutionLease, OwnerActionController


class OwnerActionExecutionRejected(ValueError):
    """A closed executor rejection containing only a stable safe reason code."""

    def __init__(self, reason_code: str) -> None:
        normalized = str(reason_code or "").strip().lower()
        if not normalized or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789_.-"
            for char in normalized
        ):
            normalized = "owner_execution_rejected"
        self.reason_code = normalized
        super().__init__(normalized)


class _CompletionKind(str, Enum):
    PRE_CALL_TERMINATED = "pre_call_terminated"
    EXECUTED = "executed"


@dataclass(frozen=True, slots=True, repr=False)
class _ExecutionCompletion:
    kind: _CompletionKind
    status: ActionReceiptStatus
    effect_state: ActionEffectState
    attempt_count: int
    result_digest: str = field(repr=False)
    completed_at: float = field(repr=False)
    output: ActionOutput | None = field(default=None, repr=False)
    reason_codes: tuple[str, ...] = ()
    receipt_eligible: bool = True

    def __post_init__(self) -> None:
        if type(self.kind) is not _CompletionKind:
            raise OwnerActionExecutionRejected("completion_kind_invalid")
        if type(self.status) is not ActionReceiptStatus:
            raise OwnerActionExecutionRejected("completion_status_invalid")
        if type(self.effect_state) is not ActionEffectState:
            raise OwnerActionExecutionRejected("completion_effect_invalid")
        if type(self.attempt_count) is not int or self.attempt_count not in {0, 1}:
            raise OwnerActionExecutionRejected("completion_attempt_invalid")
        if type(self.result_digest) is not str:
            raise OwnerActionExecutionRejected("completion_digest_invalid")
        if (
            isinstance(self.completed_at, bool)
            or not isinstance(self.completed_at, (int, float))
            or not math.isfinite(float(self.completed_at))
            or self.completed_at < 0.0
        ):
            raise OwnerActionExecutionRejected("completion_time_invalid")
        if self.output is not None and type(self.output) is not ActionOutput:
            raise OwnerActionExecutionRejected("completion_output_invalid")
        if (
            type(self.reason_codes) is not tuple
            or any(type(value) is not str or not value for value in self.reason_codes)
            or self.receipt_eligible is not True
        ):
            raise OwnerActionExecutionRejected("completion_shape_invalid")
        if self.kind is _CompletionKind.PRE_CALL_TERMINATED:
            if (
                self.status is not ActionReceiptStatus.DENIED
                or self.effect_state is not ActionEffectState.NOT_STARTED
                or self.attempt_count != 0
                or self.result_digest != ""
                or self.completed_at != 0.0
                or self.output is not None
                or not self.reason_codes
            ):
                raise OwnerActionExecutionRejected("pre_call_completion_invalid")
        elif (
            self.attempt_count != 1
            or len(self.result_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.result_digest)
            or self.status
            not in {
                ActionReceiptStatus.SUCCEEDED,
                ActionReceiptStatus.FAILED,
                ActionReceiptStatus.STALE,
                ActionReceiptStatus.TIMED_OUT,
                ActionReceiptStatus.EFFECT_UNKNOWN,
            }
            or (self.output is not None and self.status is not ActionReceiptStatus.SUCCEEDED)
            or (
                self.status is ActionReceiptStatus.STALE
                and self.effect_state is ActionEffectState.NOT_STARTED
            )
        ):
            raise OwnerActionExecutionRejected("executed_completion_invalid")

    def __repr__(self) -> str:
        return (
            "_ExecutionCompletion(authority_hidden=True, output_hidden=True, "
            "result_hidden=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _BlueprintMaterial:
    executor: "OwnerActionExecutor" = field(repr=False)
    controller: OwnerActionController = field(repr=False)
    request: OwnerActionRequest = field(repr=False)
    lease: ExecutionLease = field(repr=False)
    draft: adapters.AdapterDraft = field(repr=False)
    collector: runtime_collector.RuntimeConformanceCollector = field(repr=False)
    completion: _ExecutionCompletion = field(repr=False)


_BLUEPRINT_LOCK = threading.RLock()
_PENDING_BLUEPRINT_SEALS: set[object] = set()
_CANONICAL_BLUEPRINTS: weakref.WeakValueDictionary[int, "OwnerExecutionBlueprint"] = (
    weakref.WeakValueDictionary()
)
_BLUEPRINT_MATERIALS: weakref.WeakKeyDictionary[
    "OwnerExecutionBlueprint", _BlueprintMaterial
] = weakref.WeakKeyDictionary()
_BLUEPRINT_SNAPSHOTS: weakref.WeakKeyDictionary[
    "OwnerExecutionBlueprint", tuple[object, ...]
] = weakref.WeakKeyDictionary()
_OPENED_BLUEPRINTS: weakref.WeakSet["OwnerExecutionBlueprint"] = weakref.WeakSet()
_BLUEPRINT_MARKER = object()


@dataclass(frozen=True, slots=True, repr=False, eq=False, weakref_slot=True)
class OwnerExecutionBlueprint:
    """Opaque one-shot completion authority for a future Controller-only opener.

    Receipt status, effect state, result digest, output, and reason codes never live
    in public fields.  They remain in module-private material and are available only
    through the identity-bound, one-shot private opener below.
    """

    operation: OwnerActionOperation
    attempt_started: bool
    tool_invoked: bool
    has_safe_output: bool
    _issuer_seal: InitVar[object | None] = None
    _canonical_marker: object = field(init=False, repr=False, compare=False)

    def __post_init__(self, _issuer_seal: object | None) -> None:
        with _BLUEPRINT_LOCK:
            if _issuer_seal is None or _issuer_seal not in _PENDING_BLUEPRINT_SEALS:
                raise OwnerActionExecutionRejected("blueprint_issuer_seal_invalid")
            _PENDING_BLUEPRINT_SEALS.remove(_issuer_seal)
        object.__setattr__(self, "_canonical_marker", _BLUEPRINT_MARKER)
        if type(self.operation) is not OwnerActionOperation:
            raise OwnerActionExecutionRejected("blueprint_operation_invalid")
        if any(
            type(value) is not bool
            for value in (
                self.attempt_started,
                self.tool_invoked,
                self.has_safe_output,
            )
        ):
            raise OwnerActionExecutionRejected("blueprint_shape_invalid")

    @property
    def is_canonical(self) -> bool:
        with _BLUEPRINT_LOCK:
            try:
                marker = self._canonical_marker
                expected = _BLUEPRINT_SNAPSHOTS.get(self)
                material = _BLUEPRINT_MATERIALS.get(self)
                return (
                    marker is _BLUEPRINT_MARKER
                    and _CANONICAL_BLUEPRINTS.get(id(self)) is self
                    and material is not None
                    and expected is not None
                    and expected == _blueprint_state(self, material)
                )
            except Exception:
                return False

    def trace_metadata(self) -> dict[str, str | int | bool]:
        if not self.is_canonical:
            raise OwnerActionExecutionRejected("blueprint_not_canonical")
        return {
            "schema_version": 1,
            "operation": self.operation.value,
            "attempt_started": self.attempt_started,
            "tool_invoked": self.tool_invoked,
            "has_safe_output": self.has_safe_output,
            "completion_authority_hidden": True,
            "canonical": True,
        }

    def __copy__(self) -> "OwnerExecutionBlueprint":
        raise OwnerActionExecutionRejected("blueprint_copy_forbidden")

    def __deepcopy__(self, memo: dict[int, object]) -> "OwnerExecutionBlueprint":
        del memo
        raise OwnerActionExecutionRejected("blueprint_copy_forbidden")

    def __repr__(self) -> str:
        if not self.is_canonical:
            return (
                "OwnerExecutionBlueprint(canonical=False, authority_hidden=True, "
                "output_hidden=True)"
            )
        return (
            "OwnerExecutionBlueprint("
            f"operation={self.operation.value!r}, "
            f"attempt_started={self.attempt_started}, tool_invoked={self.tool_invoked}, "
            f"has_safe_output={self.has_safe_output}, canonical=True, "
            "authority_hidden=True, output_hidden=True)"
        )


def _snapshot_scalar(value: object) -> tuple[type[object], object]:
    return type(value), value


def _snapshot_binding(binding: object) -> tuple[object, ...]:
    if type(binding) is not DecisionBinding:
        return ("invalid_binding", type(binding), id(binding))
    return (
        id(binding),
        _snapshot_scalar(binding.scope_key),
        _snapshot_scalar(binding.session_id),
        _snapshot_scalar(binding.current_message_id),
        _snapshot_scalar(binding.current_sender_key),
        _snapshot_scalar(binding.current_content_digest),
        _snapshot_scalar(binding.conversation_revision),
        _snapshot_scalar(binding.generation_epoch),
        _snapshot_scalar(binding.trace_id),
    )


def _snapshot_request(request: object) -> tuple[object, ...]:
    if type(request) is not OwnerActionRequest:
        return ("invalid_request", type(request), id(request))
    reasons = request.reason_codes
    return (
        id(request),
        _snapshot_binding(request.binding),
        _snapshot_scalar(request.action_id),
        _snapshot_scalar(request.adapter_id),
        _snapshot_scalar(request.adapter_version),
        _snapshot_scalar(request.request_digest),
        _snapshot_scalar(request.parameter_digest),
        (type(request.capability), id(request.capability)),
        (type(request.operation), id(request.operation)),
        (type(request.side_effect), id(request.side_effect)),
        _snapshot_scalar(request.issued_at),
        _snapshot_scalar(request.deadline),
        (type(request.confirmation_policy), id(request.confirmation_policy)),
        _snapshot_scalar(request.idempotency_key),
        _snapshot_scalar(request.call_budget),
        (
            type(reasons),
            tuple(_snapshot_scalar(reason) for reason in reasons)
            if type(reasons) is tuple
            else (type(reasons), id(reasons)),
        ),
    )


def _snapshot_lease(lease: object) -> tuple[object, ...]:
    if type(lease) is not ExecutionLease:
        return ("invalid_lease", type(lease), id(lease))
    return (
        id(lease),
        _snapshot_binding(lease.binding),
        _snapshot_scalar(lease.request_digest),
        _snapshot_scalar(lease.lease_digest),
        (type(lease.capability), id(lease.capability)),
        (type(lease.operation), id(lease.operation)),
        _snapshot_scalar(lease.issued_at),
        _snapshot_scalar(lease.deadline),
    )


def _snapshot_completion(completion: object) -> tuple[object, ...]:
    if type(completion) is not _ExecutionCompletion:
        return ("invalid_completion", type(completion), id(completion))
    output = completion.output
    output_snapshot: tuple[object, ...] | None
    if output is None:
        output_snapshot = None
    elif type(output) is ActionOutput:
        output_snapshot = (
            id(output),
            _snapshot_binding(output.binding),
            _snapshot_scalar(output.request_digest),
            (type(output.kind), id(output.kind)),
            (type(output.visibility), id(output.visibility)),
            _snapshot_scalar(output.safe_text),
            _snapshot_scalar(output.artifact_ref),
            _snapshot_scalar(output.truncated),
            _snapshot_scalar(output.output_digest),
            _snapshot_scalar(output.is_canonical),
        )
    else:
        output_snapshot = ("invalid_output", type(output), id(output))
    reasons = completion.reason_codes
    return (
        (type(completion.kind), id(completion.kind)),
        (type(completion.status), id(completion.status)),
        (type(completion.effect_state), id(completion.effect_state)),
        _snapshot_scalar(completion.attempt_count),
        _snapshot_scalar(completion.result_digest),
        _snapshot_scalar(completion.completed_at),
        output_snapshot,
        (
            type(reasons),
            tuple(_snapshot_scalar(reason) for reason in reasons)
            if type(reasons) is tuple
            else (type(reasons), id(reasons)),
        ),
        _snapshot_scalar(completion.receipt_eligible),
    )


def _snapshot_draft(draft: object) -> tuple[object, ...]:
    if type(draft) is not adapters.AdapterDraft:
        return ("invalid_draft", type(draft), id(draft))
    return (
        id(draft),
        _snapshot_binding(draft.binding),
        *(
            _snapshot_scalar(getattr(draft, name))
            for name in (
                "adapter_id",
                "adapter_version",
                "capability",
                "operation",
                "side_effect",
                "confirmation_policy",
                "exact_tool_name",
                "plugin_id",
                "source_qualname",
                "interface_version",
                "schema_digest",
                "parameter_digest",
                "source_interface_digest",
                "call_budget",
                "private_owner_only",
            )
        ),
        _snapshot_scalar(draft.is_canonical),
    )


def _blueprint_state(
    blueprint: OwnerExecutionBlueprint,
    material: _BlueprintMaterial | None = None,
) -> tuple[object, ...]:
    try:
        public_state: tuple[object, ...] = (
            blueprint.operation,
            blueprint.attempt_started,
            blueprint.tool_invoked,
            blueprint.has_safe_output,
            id(blueprint._canonical_marker),
        )
        if material is None:
            return public_state
        return (
            public_state,
            id(material.executor),
            id(material.controller),
            _snapshot_request(material.request),
            _snapshot_lease(material.lease),
            _snapshot_draft(material.draft),
            id(material.collector),
            _snapshot_completion(material.completion),
        )
    except Exception:
        return ("invalid",)


def _issue_blueprint(
    *,
    executor: "OwnerActionExecutor",
    request: OwnerActionRequest,
    lease: ExecutionLease,
    draft: adapters.AdapterDraft,
    collector: runtime_collector.RuntimeConformanceCollector,
    completion: _ExecutionCompletion,
    tool_invoked: bool,
) -> OwnerExecutionBlueprint:
    if type(completion) is not _ExecutionCompletion:
        raise OwnerActionExecutionRejected("completion_material_invalid")
    if type(executor) is not OwnerActionExecutor:
        raise OwnerActionExecutionRejected("owner_executor_not_canonical")
    profile = executor._profile()
    if type(request) is not OwnerActionRequest or type(lease) is not ExecutionLease:
        raise OwnerActionExecutionRejected("blueprint_lineage_invalid")
    if completion.output is not None:
        try:
            _open_action_output(completion.output, request=request)
        except Exception:
            raise OwnerActionExecutionRejected(
                "artifact_safe_output_authority_unavailable"
            ) from None
    _validate_completion_for_request(
        request,
        lease,
        completion,
        test_runtime_enabled=profile.test_runtime_enabled,
    )
    seal = object()
    with _BLUEPRINT_LOCK:
        _PENDING_BLUEPRINT_SEALS.add(seal)
    try:
        blueprint = OwnerExecutionBlueprint(
            operation=request.operation,
            attempt_started=True,
            tool_invoked=tool_invoked,
            has_safe_output=completion.output is not None,
            _issuer_seal=seal,
        )
    finally:
        with _BLUEPRINT_LOCK:
            _PENDING_BLUEPRINT_SEALS.discard(seal)
    material = _BlueprintMaterial(
        executor=executor,
        controller=profile.controller,
        request=request,
        lease=lease,
        draft=draft,
        collector=collector,
        completion=completion,
    )
    with _BLUEPRINT_LOCK:
        _CANONICAL_BLUEPRINTS[id(blueprint)] = blueprint
        _BLUEPRINT_MATERIALS[blueprint] = material
        _BLUEPRINT_SNAPSHOTS[blueprint] = _blueprint_state(blueprint, material)
    return blueprint


def _open_execution_blueprint(
    blueprint: OwnerExecutionBlueprint,
    *,
    controller: OwnerActionController,
    request: OwnerActionRequest,
    lease: ExecutionLease,
) -> _ExecutionCompletion:
    """One-shot Controller friend opener; omitted from ``__all__``."""

    if type(blueprint) is not OwnerExecutionBlueprint or not blueprint.is_canonical:
        raise OwnerActionExecutionRejected("blueprint_not_canonical")
    if (
        type(controller) is not OwnerActionController
        or type(request) is not OwnerActionRequest
        or type(lease) is not ExecutionLease
    ):
        raise OwnerActionExecutionRejected("blueprint_lineage_invalid")
    with _BLUEPRINT_LOCK:
        material = _BLUEPRINT_MATERIALS.get(blueprint)
        if material is None:
            raise OwnerActionExecutionRejected("blueprint_material_missing")
        if (
            material.controller is not controller
            or material.request is not request
            or material.lease is not lease
        ):
            raise OwnerActionExecutionRejected("blueprint_lineage_mismatch")
        if _BLUEPRINT_SNAPSHOTS.get(blueprint) != _blueprint_state(
            blueprint,
            material,
        ):
            raise OwnerActionExecutionRejected("blueprint_not_canonical")
        if blueprint in _OPENED_BLUEPRINTS:
            raise OwnerActionExecutionRejected("blueprint_replayed")
        _OPENED_BLUEPRINTS.add(blueprint)
        return material.completion


def _validate_completion_for_request(
    request: OwnerActionRequest,
    lease: ExecutionLease,
    completion: _ExecutionCompletion,
    *,
    test_runtime_enabled: bool,
) -> None:
    """Close status/effect/output shapes before a blueprint becomes authority."""

    if completion.kind is _CompletionKind.PRE_CALL_TERMINATED:
        return
    if completion.completed_at < lease.issued_at:
        raise OwnerActionExecutionRejected("completion_before_lease")
    if completion.status is not ActionReceiptStatus.SUCCEEDED and not completion.reason_codes:
        raise OwnerActionExecutionRejected("completion_reason_required")
    if completion.status is ActionReceiptStatus.STALE:
        # Kept only for the existing deterministic ActionOutcome contract fixture.
        # The live/test E2C execute path itself never maps an invocation to STALE.
        if not test_runtime_enabled:
            raise OwnerActionExecutionRejected("executed_stale_forbidden")

    status = completion.status
    effect = completion.effect_state
    output = completion.output
    if output is not None and status is not ActionReceiptStatus.SUCCEEDED:
        raise OwnerActionExecutionRejected("completion_output_status_invalid")
    if request.side_effect is ActionSideEffect.SCOPED_READ:
        allowed = {
            ActionReceiptStatus.SUCCEEDED: {ActionEffectState.NO_SIDE_EFFECT},
            ActionReceiptStatus.FAILED: {ActionEffectState.NO_SIDE_EFFECT},
            ActionReceiptStatus.TIMED_OUT: {ActionEffectState.NO_SIDE_EFFECT},
            ActionReceiptStatus.STALE: {ActionEffectState.NO_SIDE_EFFECT},
        }
        if status not in allowed or effect not in allowed[status]:
            raise OwnerActionExecutionRejected("read_completion_shape_invalid")
        if status is ActionReceiptStatus.SUCCEEDED and output is None:
            raise OwnerActionExecutionRejected("read_completion_output_required")
    else:
        allowed = {
            ActionReceiptStatus.SUCCEEDED: {ActionEffectState.COMMITTED},
            ActionReceiptStatus.FAILED: {
                ActionEffectState.NOT_COMMITTED,
                ActionEffectState.PARTIAL,
            },
            ActionReceiptStatus.TIMED_OUT: {
                ActionEffectState.NOT_COMMITTED,
                ActionEffectState.PARTIAL,
                ActionEffectState.UNKNOWN,
            },
            ActionReceiptStatus.EFFECT_UNKNOWN: {ActionEffectState.UNKNOWN},
            ActionReceiptStatus.STALE: {
                ActionEffectState.NOT_COMMITTED,
                ActionEffectState.COMMITTED,
                ActionEffectState.PARTIAL,
                ActionEffectState.UNKNOWN,
            },
        }
        if status not in allowed or effect not in allowed[status]:
            raise OwnerActionExecutionRejected("mutation_completion_shape_invalid")

    if status is ActionReceiptStatus.SUCCEEDED and not test_runtime_enabled:
        # No production live same-handle output/effect authority exists in E3C1A.
        raise OwnerActionExecutionRejected("production_success_authority_unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class _ExecutorProfile:
    controller: OwnerActionController = field(repr=False)
    clock: Callable[[], float] = field(repr=False)
    test_runtime_enabled: bool


_EXECUTOR_PROFILE_LOCK = threading.RLock()
_EXECUTOR_PROFILES: weakref.WeakKeyDictionary[
    "OwnerActionExecutor", _ExecutorProfile
] = weakref.WeakKeyDictionary()
_ATTEMPT_LOCK = threading.RLock()
_ATTEMPTED_LEASES: dict[int, ExecutionLease] = {}


class _BlockedToolAuthority(RuntimeError):
    pass


class _NarrowToolContext:
    """No event, plugin context, send, handoff, hook, or nested tool authority."""

    __slots__ = ("_authority_attempted", "_binding_digest")

    _FORBIDDEN = frozenset(
        {
            "event",
            "send",
            "send_message",
            "call_tool",
            "tools",
            "handoff",
            "background",
            "create_task",
            "plugin_context",
            "context",
        }
    )

    def __init__(self, binding: DecisionBinding) -> None:
        self._authority_attempted = False
        encoded = "\0".join(
            (
                binding.scope_key,
                binding.session_id,
                binding.current_message_id,
                binding.current_sender_key,
                binding.current_content_digest,
                str(binding.conversation_revision),
                str(binding.generation_epoch),
            )
        ).encode("utf-8")
        self._binding_digest = hashlib.sha256(encoded).hexdigest()

    @property
    def binding_digest(self) -> str:
        return self._binding_digest

    @property
    def authority_attempted(self) -> bool:
        return self._authority_attempted

    def __getattr__(self, name: str) -> Any:
        if name in self._FORBIDDEN:
            self._authority_attempted = True
            raise _BlockedToolAuthority("tool_authority_forbidden")
        raise AttributeError(name)

    def __repr__(self) -> str:
        return "_NarrowToolContext(binding_sealed=True, authority_absent=True)"


def _safe_now(clock: Callable[[], float]) -> float:
    try:
        value = float(clock())
    except Exception:
        raise OwnerActionExecutionRejected("executor_clock_invalid") from None
    if not math.isfinite(value) or value < 0.0:
        raise OwnerActionExecutionRejected("executor_clock_invalid")
    return value


def _safe_completion_now(
    clock: Callable[[], float],
    *,
    lower_bound: float,
) -> tuple[float, bool]:
    try:
        value = _safe_now(clock)
    except OwnerActionExecutionRejected:
        return lower_bound, False
    return max(value, lower_bound), True


def _digest_json(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except Exception:
        raise OwnerActionExecutionRejected("tool_schema_invalid") from None
    return hashlib.sha256(payload).hexdigest()


def _result_digest(
    request: OwnerActionRequest,
    *,
    reason_code: str,
    raw_digest: str = "",
) -> str:
    payload = "\0".join(
        (
            "sealed-owner-execution-v1",
            request.request_digest,
            request.operation.value,
            reason_code,
            raw_digest,
        )
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _failure_completion(
    request: OwnerActionRequest,
    *,
    reason_code: str,
    completed_at: float,
    timed_out: bool = False,
    uncertain_mutation: bool = False,
    raw_digest: str = "",
) -> _ExecutionCompletion:
    if uncertain_mutation:
        status = ActionReceiptStatus.EFFECT_UNKNOWN
        effect = ActionEffectState.UNKNOWN
    elif timed_out:
        status = ActionReceiptStatus.TIMED_OUT
        effect = (
            ActionEffectState.NO_SIDE_EFFECT
            if request.side_effect is ActionSideEffect.SCOPED_READ
            else ActionEffectState.UNKNOWN
        )
        if effect is ActionEffectState.UNKNOWN:
            status = ActionReceiptStatus.EFFECT_UNKNOWN
    else:
        status = ActionReceiptStatus.FAILED
        effect = (
            ActionEffectState.NO_SIDE_EFFECT
            if request.side_effect is ActionSideEffect.SCOPED_READ
            else ActionEffectState.NOT_COMMITTED
        )
    return _ExecutionCompletion(
        kind=_CompletionKind.EXECUTED,
        status=status,
        effect_state=effect,
        attempt_count=1,
        result_digest=_result_digest(
            request,
            reason_code=reason_code,
            raw_digest=raw_digest,
        ),
        completed_at=completed_at,
        output=None,
        reason_codes=(reason_code,),
    )


def _cancelled_completion(
    request: OwnerActionRequest,
    *,
    clock: Callable[[], float],
    lower_bound: float,
) -> _ExecutionCompletion:
    """Map external task cancellation without losing mutation uncertainty."""

    completed_at, clock_valid = _safe_completion_now(
        clock,
        lower_bound=lower_bound,
    )
    return _failure_completion(
        request,
        reason_code=(
            "execution_cancelled"
            if clock_valid
            else "executor_clock_invalid_after_call"
        ),
        completed_at=completed_at,
        uncertain_mutation=request.side_effect is not ActionSideEffect.SCOPED_READ,
    )


def _blocked_completion(
    request: OwnerActionRequest,
    *,
    reason_code: str,
) -> _ExecutionCompletion:
    """Represent a claimed lease whose tool attempt never started.

    The Controller consumes this closed kind only through its sealed blueprint
    opener and records ``DENIED/NOT_STARTED/attempt_count=0`` without inventing an
    execution attempt.
    """

    return _ExecutionCompletion(
        kind=_CompletionKind.PRE_CALL_TERMINATED,
        status=ActionReceiptStatus.DENIED,
        effect_state=ActionEffectState.NOT_STARTED,
        attempt_count=0,
        result_digest="",
        completed_at=0.0,
        output=None,
        reason_codes=(reason_code,),
    )


def _issue_pre_call_blueprint(
    *,
    executor: "OwnerActionExecutor",
    request: OwnerActionRequest,
    lease: ExecutionLease,
    draft: adapters.AdapterDraft,
    collector: runtime_collector.RuntimeConformanceCollector,
    reason_code: str,
) -> OwnerExecutionBlueprint:
    safe_reason = OwnerActionExecutionRejected(reason_code).reason_code
    return _issue_blueprint(
        executor=executor,
        request=request,
        lease=lease,
        draft=draft,
        collector=collector,
        completion=_blocked_completion(request, reason_code=safe_reason),
        tool_invoked=False,
    )


def _assert_shape_lineage(
    request: OwnerActionRequest,
    lease: ExecutionLease,
    draft: adapters.AdapterDraft,
) -> None:
    if type(request) is not OwnerActionRequest:
        raise OwnerActionExecutionRejected("owner_action_request_required")
    if type(lease) is not ExecutionLease:
        raise OwnerActionExecutionRejected("execution_lease_required")
    if type(draft) is not adapters.AdapterDraft or not draft.is_canonical:
        raise OwnerActionExecutionRejected("adapter_draft_not_canonical")
    if not (
        request.binding is lease.binding
        and request.binding is draft.binding
        and request.request_digest == lease.request_digest
        and request.parameter_digest == draft.parameter_digest
        and request.capability is lease.capability is draft.capability
        and request.operation is lease.operation is draft.operation
        and request.side_effect is draft.side_effect
        and request.confirmation_policy is draft.confirmation_policy
        and request.adapter_id == draft.adapter_id
        and request.adapter_version == draft.adapter_version
        and request.deadline == lease.deadline
        and request.call_budget == 1
        and draft.call_budget == 1
        and draft.private_owner_only is True
    ):
        raise OwnerActionExecutionRejected("execution_lineage_mismatch")


def _open_exact_controller_attempt(
    controller: OwnerActionController,
    request: OwnerActionRequest,
    lease: ExecutionLease,
    draft: adapters.AdapterDraft,
    material: object,
) -> None:
    """Use the Controller-owned E3C0R snapshot inspector without ledger access."""

    try:
        controller._inspect_exact_execution_attempt(
            request,
            lease,
            draft,
            material,
        )
    except Exception:
        raise OwnerActionExecutionRejected("controller_attempt_not_canonical") from None


def _validate_material(
    request: OwnerActionRequest,
    draft: adapters.AdapterDraft,
    material: object,
) -> None:
    descriptor = adapters.OWNER_ACTION_ADAPTER_REGISTRY.get(request.operation)
    if descriptor is None or material.descriptor is not descriptor:
        raise OwnerActionExecutionRejected("adapter_descriptor_mismatch")
    actual_descriptor = (
        draft.adapter_id,
        draft.adapter_version,
        draft.capability,
        draft.operation,
        draft.side_effect,
        draft.confirmation_policy,
        draft.exact_tool_name,
        draft.plugin_id,
        draft.source_qualname,
        draft.interface_version,
        draft.schema_digest,
        draft.call_budget,
        draft.private_owner_only,
    )
    expected_descriptor = (
        descriptor.adapter_id,
        descriptor.adapter_version,
        descriptor.capability,
        descriptor.operation,
        descriptor.side_effect,
        descriptor.confirmation_policy,
        descriptor.exact_tool_name,
        descriptor.plugin_id,
        descriptor.source_qualname,
        descriptor.interface_version,
        descriptor.schema_digest,
        descriptor.call_budget,
        descriptor.private_owner_only,
    )
    if actual_descriptor != expected_descriptor:
        raise OwnerActionExecutionRejected("adapter_draft_drift")
    record_types = {
        OwnerActionOperation.ARTIFACT_READ_EXACT: adapters._ArtifactReadExactParameters,
        OwnerActionOperation.ARTIFACT_GREP: adapters._ArtifactGrepParameters,
        OwnerActionOperation.MEMORY_WRITE_LITERAL: adapters._MemoryWriteLiteralParameters,
        OwnerActionOperation.SANDBOX_SHELL_ONCE: adapters._SandboxShellOnceParameters,
    }
    if type(material.parameter_record) is not record_types[request.operation]:
        raise OwnerActionExecutionRejected("parameter_record_type_mismatch")
    try:
        parameter_digest = adapters._digest_payload(
            adapters._parameter_payload(material.parameter_record)
        )
    except Exception:
        raise OwnerActionExecutionRejected("parameter_record_invalid") from None
    if (
        parameter_digest != draft.parameter_digest
        or parameter_digest != request.parameter_digest
        or material.source_interface_digest != draft.source_interface_digest
    ):
        raise OwnerActionExecutionRejected("parameter_record_drift")


def _compile_arguments(
    operation: OwnerActionOperation,
    record: object,
) -> dict[str, Any]:
    if operation is OwnerActionOperation.ARTIFACT_READ_EXACT:
        if type(record) is not adapters._ArtifactReadExactParameters:
            raise OwnerActionExecutionRejected("parameter_record_type_mismatch")
        arguments = {"path": record.path, "offset": record.offset, "limit": record.limit}
        valid = (
            type(record.path) is str
            and bool(record.path)
            and type(record.offset) is int
            and record.offset >= 0
            and type(record.limit) is int
            and record.limit >= 1
        )
    elif operation is OwnerActionOperation.ARTIFACT_GREP:
        if type(record) is not adapters._ArtifactGrepParameters:
            raise OwnerActionExecutionRejected("parameter_record_type_mismatch")
        arguments = {
            "pattern": record.literal_pattern,
            "path": record.path,
            "glob": record.glob,
            "-A": record.context_after,
            "-B": record.context_before,
            "result_limit": record.result_limit,
        }
        valid = (
            all(type(arguments[name]) is str for name in ("pattern", "path", "glob"))
            and bool(record.literal_pattern)
            and all(
                type(arguments[name]) is int and arguments[name] >= 0
                for name in ("-A", "-B")
            )
            and type(record.result_limit) is int
            and record.result_limit >= 1
        )
    elif operation is OwnerActionOperation.MEMORY_WRITE_LITERAL:
        if type(record) is not adapters._MemoryWriteLiteralParameters:
            raise OwnerActionExecutionRejected("parameter_record_type_mismatch")
        arguments = {
            "memory": record.memory,
            "topics": list(record.topics),
            "key_facts": list(record.key_facts),
            "sentiment": record.sentiment,
            "importance": record.importance,
            "reason": record.reason,
        }
        valid = (
            type(record.memory) is str
            and bool(record.memory)
            and type(record.topics) is tuple
            and type(record.key_facts) is tuple
            and type(record.sentiment) is str
            and type(record.importance) is float
            and 0.0 <= record.importance <= 1.0
            and type(record.reason) is str
        )
    else:
        raise OwnerActionExecutionRejected("shell_runtime_hard_disabled")
    if not valid:
        raise OwnerActionExecutionRejected("typed_arguments_invalid")
    return arguments


def _validate_exact_schema(
    draft: adapters.AdapterDraft,
    tool: object,
    arguments: Mapping[str, Any],
) -> None:
    schema = getattr(tool, "parameters", None)
    if type(schema) is not dict or _digest_json(schema) != draft.schema_digest:
        raise OwnerActionExecutionRejected("tool_schema_drift")
    if schema.get("type") != "object" or type(schema.get("properties")) is not dict:
        raise OwnerActionExecutionRejected("tool_schema_invalid")
    properties = schema["properties"]
    required = schema.get("required", [])
    if (
        type(required) is not list
        or any(type(item) is not str for item in required)
        or not set(required).issubset(arguments)
        or set(arguments).difference(properties)
    ):
        raise OwnerActionExecutionRejected("tool_arguments_schema_mismatch")
    for name, value in arguments.items():
        node = properties.get(name)
        if type(node) is not dict or not _validate_schema_node(node, value):
            raise OwnerActionExecutionRejected("tool_arguments_schema_mismatch")


def _validate_schema_node(schema: Mapping[str, Any], value: Any) -> bool:
    """Closed equivalent validator for the exact audited schema subset.

    The audited tool schemas use Draft 2020-12 primitive/object/array constraints.
    Unknown validation keywords fail closed instead of being silently ignored.
    """

    allowed = {
        "type",
        "description",
        "default",
        "minimum",
        "maximum",
        "enum",
        "pattern",
        "minLength",
        "maxLength",
        "items",
        "properties",
        "required",
        "additionalProperties",
    }
    if any(type(key) is not str or key not in allowed for key in schema):
        return False
    node_type = schema.get("type")
    type_ok = {
        "string": type(value) is str,
        "integer": type(value) is int,
        "number": type(value) in {int, float} and not isinstance(value, bool),
        "array": type(value) is list,
        "object": type(value) is dict,
        "boolean": type(value) is bool,
    }.get(node_type, False)
    if not type_ok:
        return False
    enum = schema.get("enum")
    if enum is not None and (type(enum) is not list or value not in enum):
        return False
    if node_type in {"integer", "number"}:
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and (
            type(minimum) not in {int, float} or value < minimum
        ):
            return False
        if maximum is not None and (
            type(maximum) not in {int, float} or value > maximum
        ):
            return False
    if node_type == "string":
        minimum_length = schema.get("minLength")
        maximum_length = schema.get("maxLength")
        if minimum_length is not None and (
            type(minimum_length) is not int or len(value) < minimum_length
        ):
            return False
        if maximum_length is not None and (
            type(maximum_length) is not int or len(value) > maximum_length
        ):
            return False
        pattern = schema.get("pattern")
        if pattern is not None:
            if type(pattern) is not str:
                return False
            try:
                if re.search(pattern, value) is None:
                    return False
            except re.error:
                return False
    if node_type == "array":
        items = schema.get("items")
        if type(items) is not dict or any(
            not _validate_schema_node(items, item) for item in value
        ):
            return False
    if node_type == "object":
        properties = schema.get("properties")
        required = schema.get("required", [])
        if type(properties) is not dict or type(required) is not list:
            return False
        if any(type(name) is not str or name not in value for name in required):
            return False
        if schema.get("additionalProperties") is False and set(value).difference(properties):
            return False
        if any(
            name in properties
            and (
                type(properties[name]) is not dict
                or not _validate_schema_node(properties[name], item)
            )
            for name, item in value.items()
        ):
            return False
    return True


def _assert_local_tool(tool: object) -> Callable[..., Any]:
    try:
        names = frozenset(cls.__name__.replace("_", "").casefold() for cls in type(tool).mro())
    except Exception:
        raise OwnerActionExecutionRejected("runtime_tool_type_invalid") from None
    if "handofftool" in names:
        raise OwnerActionExecutionRejected("handoff_forbidden")
    if "mcptool" in names:
        raise OwnerActionExecutionRejected("mcp_forbidden")
    if bool(getattr(tool, "is_background_task", False)):
        raise OwnerActionExecutionRejected("background_forbidden")
    if getattr(tool, "handler", None) is not None:
        raise OwnerActionExecutionRejected("plugin_hook_forbidden")
    call = getattr(tool, "call", None)
    if not callable(call):
        raise OwnerActionExecutionRejected("runtime_call_missing")
    if not (inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call)):
        raise OwnerActionExecutionRejected("runtime_async_call_required")
    return call


async def _collect_one_result(
    call: Callable[..., Any],
    context: _NarrowToolContext,
    arguments: dict[str, Any],
) -> tuple[object, ...]:
    produced = call(context, **arguments)
    if inspect.isawaitable(produced):
        produced = await produced
    if hasattr(produced, "__aiter__"):
        values: list[object] = []
        async for value in produced:
            values.append(value)
            if len(values) > 1:
                break
        return tuple(values)
    if produced is None:
        return ()
    if type(produced) in {list, tuple}:
        return tuple(produced[:2])
    return (produced,)


class OwnerActionExecutor:
    """One-attempt executor; public instances remain production hard-blocked."""

    __slots__ = ("__weakref__",)

    def __init__(self, controller: OwnerActionController) -> None:
        if type(controller) is not OwnerActionController:
            raise OwnerActionExecutionRejected("owner_action_controller_required")
        profile = _ExecutorProfile(
            controller=controller,
            clock=time.time,
            test_runtime_enabled=False,
        )
        with _EXECUTOR_PROFILE_LOCK:
            _EXECUTOR_PROFILES[self] = profile

    def _profile(self) -> _ExecutorProfile:
        with _EXECUTOR_PROFILE_LOCK:
            profile = _EXECUTOR_PROFILES.get(self)
        if profile is None:
            raise OwnerActionExecutionRejected("owner_executor_not_canonical")
        return profile

    async def execute(
        self,
        request: OwnerActionRequest,
        lease: ExecutionLease,
        draft: adapters.AdapterDraft,
        collector: runtime_collector.RuntimeConformanceCollector,
    ) -> OwnerExecutionBlueprint:
        profile = self._profile()
        _assert_shape_lineage(request, lease, draft)
        if type(collector) is not runtime_collector.RuntimeConformanceCollector:
            raise OwnerActionExecutionRejected("runtime_collector_required")
        try:
            material = adapters._open_adapter_draft(draft)
        except Exception:
            raise OwnerActionExecutionRejected("adapter_draft_not_canonical") from None
        _open_exact_controller_attempt(
            profile.controller,
            request,
            lease,
            draft,
            material,
        )
        runtime_material = None
        runtime_rejection = ""
        try:
            runtime_material = runtime_collector._open_runtime_evidence(
                material.runtime_evidence,
                collector=collector,
                binding=request.binding,
                operation=request.operation,
            )
        except Exception as exc:
            code = getattr(exc, "reason_code", "runtime_evidence_rejected")
            if code in {
                "runtime_collector_not_canonical",
                "runtime_evidence_collector_mismatch",
                "runtime_open_binding_invalid",
                "runtime_evidence_open_mismatch",
            }:
                # A caller-supplied cross-lineage tuple must not consume the attempt.
                raise OwnerActionExecutionRejected(code) from None
            runtime_rejection = OwnerActionExecutionRejected(code).reason_code
        if runtime_material is not None and (
            runtime_material.binding is not request.binding
            or runtime_material.operation is not request.operation
            or runtime_material.local_function_tool is not True
            or runtime_material.background_disabled is not True
            or runtime_material.direct_send_blocked is not True
            or runtime_material.narrow_context_verified is not True
            or runtime_material.return_contract_verified is not True
        ):
            runtime_rejection = "runtime_evidence_lineage_mismatch"
        with _ATTEMPT_LOCK:
            existing = _ATTEMPTED_LEASES.get(id(lease))
            if existing is not None:
                raise OwnerActionExecutionRejected("execution_attempt_replayed")
            _ATTEMPTED_LEASES[id(lease)] = lease

        try:
            _validate_material(request, draft, material)
        except Exception as exc:
            return _issue_pre_call_blueprint(
                executor=self,
                request=request,
                lease=lease,
                draft=draft,
                collector=collector,
                reason_code=getattr(exc, "reason_code", "adapter_material_invalid"),
            )

        try:
            started_check = _safe_now(profile.clock)
        except OwnerActionExecutionRejected as exc:
            return _issue_pre_call_blueprint(
                executor=self,
                request=request,
                lease=lease,
                draft=draft,
                collector=collector,
                reason_code=exc.reason_code,
            )
        if started_check < lease.issued_at:
            return _issue_pre_call_blueprint(
                executor=self,
                request=request,
                lease=lease,
                draft=draft,
                collector=collector,
                reason_code="execution_clock_before_lease",
            )
        if runtime_rejection:
            return _issue_pre_call_blueprint(
                executor=self,
                request=request,
                lease=lease,
                draft=draft,
                collector=collector,
                reason_code=runtime_rejection,
            )
        if not profile.test_runtime_enabled:
            return _issue_pre_call_blueprint(
                executor=self,
                request=request,
                lease=lease,
                draft=draft,
                collector=collector,
                reason_code="production_execution_hard_blocked",
            )

        remaining = min(request.deadline, lease.deadline) - started_check
        if remaining <= 0.0:
            return _issue_pre_call_blueprint(
                executor=self,
                request=request,
                lease=lease,
                draft=draft,
                collector=collector,
                reason_code="execution_deadline_expired",
            )

        try:
            arguments = _compile_arguments(request.operation, material.parameter_record)
            assert runtime_material is not None
            _validate_exact_schema(draft, runtime_material.tool, arguments)
        except Exception as exc:
            return _issue_pre_call_blueprint(
                executor=self,
                request=request,
                lease=lease,
                draft=draft,
                collector=collector,
                reason_code=getattr(exc, "reason_code", "pre_call_validation_failed"),
            )

        # LivingMemory remains non-executable until its live admin/private scope and
        # mutation result contract are independently audited. Shell has no candidate.
        if request.operation is OwnerActionOperation.MEMORY_WRITE_LITERAL:
            return _issue_pre_call_blueprint(
                executor=self,
                request=request,
                lease=lease,
                draft=draft,
                collector=collector,
                reason_code="memory_execution_not_audited",
            )

        try:
            call = _assert_local_tool(runtime_material.tool)
        except Exception as exc:
            return _issue_pre_call_blueprint(
                executor=self,
                request=request,
                lease=lease,
                draft=draft,
                collector=collector,
                reason_code=getattr(exc, "reason_code", "runtime_tool_invalid"),
            )
        context = _NarrowToolContext(request.binding)
        try:
            async with asyncio.timeout(remaining):
                results = await _collect_one_result(call, context, arguments)
        except asyncio.CancelledError:
            # Cancellation is a normal async control path and derives directly from
            # BaseException on supported Python versions.  Once the canonical
            # attempt is registered it must still produce terminalizable authority.
            completion = _cancelled_completion(
                request,
                clock=profile.clock,
                lower_bound=lease.issued_at,
            )
        except TimeoutError:
            completed_at, clock_valid = _safe_completion_now(
                profile.clock,
                lower_bound=lease.issued_at,
            )
            completion = _failure_completion(
                request,
                reason_code=(
                    "execution_deadline_exceeded"
                    if clock_valid
                    else "executor_clock_invalid_after_call"
                ),
                completed_at=completed_at,
                timed_out=True,
            )
        except Exception:
            completed_at, clock_valid = _safe_completion_now(
                profile.clock,
                lower_bound=lease.issued_at,
            )
            completion = _failure_completion(
                request,
                reason_code=(
                    "direct_send_forbidden"
                    if context.authority_attempted
                    else (
                        "runtime_call_failed"
                        if clock_valid
                        else "executor_clock_invalid_after_call"
                    )
                ),
                completed_at=completed_at,
                uncertain_mutation=request.side_effect is not ActionSideEffect.SCOPED_READ,
            )
        else:
            completed_at, clock_valid = _safe_completion_now(
                profile.clock,
                lower_bound=lease.issued_at,
            )
            if not clock_valid:
                completion = _failure_completion(
                    request,
                    reason_code="executor_clock_invalid_after_call",
                    completed_at=completed_at,
                    uncertain_mutation=(
                        request.side_effect is not ActionSideEffect.SCOPED_READ
                    ),
                )
            elif context.authority_attempted:
                completion = _failure_completion(
                    request,
                    reason_code="direct_send_forbidden",
                    completed_at=completed_at,
                )
            elif len(results) != 1:
                completion = _failure_completion(
                    request,
                    reason_code="result_cardinality_invalid",
                    completed_at=completed_at,
                )
            elif type(results[0]) is not bytes:
                completion = _failure_completion(
                    request,
                    reason_code="artifact_raw_bytes_required",
                    completed_at=completed_at,
                )
            else:
                # E2A's public ArtifactIdentityProof is a typed data shape, not live
                # handle authority.  Until the same opened runtime object can issue
                # canonical pre/post fstat proof, raw bytes cannot be converted to
                # SafeArtifactOutput or ActionOutput.
                raw_digest = hashlib.sha256(results[0]).hexdigest()
                completion = _failure_completion(
                    request,
                    reason_code="artifact_live_identity_proof_unavailable",
                    completed_at=completed_at,
                    raw_digest=raw_digest,
                )
        return _issue_blueprint(
            executor=self,
            request=request,
            lease=lease,
            draft=draft,
            collector=collector,
            completion=completion,
            tool_invoked=True,
        )

    def trace_metadata(self) -> dict[str, int | bool]:
        profile = self._profile()
        return {
            "schema_version": 1,
            "production_execution_enabled": False,
            "test_runtime_enabled": profile.test_runtime_enabled,
            "call_budget": 1,
            "direct_send_available": False,
            "plugin_hook_available": False,
            "live_event_available": False,
        }

    def __repr__(self) -> str:
        profile = self._profile()
        return (
            "OwnerActionExecutor("
            "production_execution_enabled=False, call_budget=1, "
            f"test_runtime_enabled={profile.test_runtime_enabled}, "
            "direct_send_available=False, plugin_hook_available=False)"
        )


def _build_test_owner_action_executor(
    controller: OwnerActionController,
    *,
    clock: Callable[[], float],
) -> OwnerActionExecutor:
    """Private deterministic profile for pure-return executor conformance tests."""

    if type(controller) is not OwnerActionController or not callable(clock):
        raise OwnerActionExecutionRejected("test_executor_profile_invalid")
    executor = OwnerActionExecutor(controller)
    with _EXECUTOR_PROFILE_LOCK:
        _EXECUTOR_PROFILES[executor] = _ExecutorProfile(
            controller=controller,
            clock=clock,
            test_runtime_enabled=True,
        )
    return executor


def _issue_test_execution_blueprint(
    controller: OwnerActionController,
    request: OwnerActionRequest,
    lease: ExecutionLease,
    *,
    status: ActionReceiptStatus,
    effect_state: ActionEffectState,
    result_digest: str = "",
    completed_at: float | None = None,
    output: ActionOutput | None = None,
    reason_codes: tuple[str, ...] = (),
) -> OwnerExecutionBlueprint:
    """Private deterministic sealed authority for Controller/Outcome contract tests.

    This helper never executes a tool and is omitted from ``__all__``.  Production
    execution cannot reach it through ``OwnerActionExecutor.execute``.
    """

    if type(controller) is not OwnerActionController:
        raise OwnerActionExecutionRejected("owner_action_controller_required")
    try:
        controller._inspect_exact_execution_lease(request, lease)
    except Exception:
        raise OwnerActionExecutionRejected("controller_attempt_not_canonical") from None
    pre_call = (
        status is ActionReceiptStatus.DENIED
        and effect_state is ActionEffectState.NOT_STARTED
    )
    if pre_call:
        completion = _blocked_completion(
            request,
            reason_code=(reason_codes[0] if reason_codes else "test_pre_call_terminated"),
        )
    else:
        completed = lease.issued_at + 1.0 if completed_at is None else completed_at
        digest = result_digest or _result_digest(
            request,
            reason_code="test_execution_result",
        )
        completion = _ExecutionCompletion(
            kind=_CompletionKind.EXECUTED,
            status=status,
            effect_state=effect_state,
            attempt_count=1,
            result_digest=digest,
            completed_at=completed,
            output=output,
            reason_codes=(
                reason_codes
                if reason_codes or status is ActionReceiptStatus.SUCCEEDED
                else ("test_execution_failure",)
            ),
        )
    executor = _build_test_owner_action_executor(
        controller,
        clock=lambda: lease.issued_at,
    )
    # The deterministic helper has no runtime draft/collector authority.  Exact
    # request/lease snapshots and the test-only executor profile remain sealed in the
    # blueprint; real execute() blueprints always carry the canonical live objects.
    return _issue_blueprint(
        executor=executor,
        request=request,
        lease=lease,
        draft=object(),  # type: ignore[arg-type]
        collector=object(),  # type: ignore[arg-type]
        completion=completion,
        tool_invoked=not pre_call,
    )


__all__ = [
    "OwnerActionExecutionRejected",
    "OwnerActionExecutor",
    "OwnerExecutionBlueprint",
]
