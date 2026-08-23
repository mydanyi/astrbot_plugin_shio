"""Sealed semantic outcomes for Controller-owned owner actions.

This module is intentionally a narrow bridge between the owner-action authority and
the later content/semantic-delivery layers.  It never turns an action result into a
``GroundingFact`` and it never exposes action output, paths, identifiers, digests, or
reason text.  Only exact objects accepted by the Controller's public canonical
inspectors can issue an outcome.

P3-08E3C1B deliberately keeps ``ActionReceipt.output`` hard-disabled.  A later safe
output authority must prove the same-handle bytes and their final presentation seal
before output-bearing receipts may enter this layer.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import weakref
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import NamedTuple
from weakref import WeakKeyDictionary

from .action_planner import PlannedAction, PlannedActionAuthority, StructuralOutcome
from .capability_policy import CapabilityClass
from .contracts import (
    ActionEffectState,
    ActionKind,
    ActionReceipt,
    ActionReceiptStatus,
    ActionSideEffect,
    ContractViolation,
    OwnerActionOperation,
    OwnerActionRequest,
)
from .owner_action_controller import (
    OwnerActionContinuationInspection,
    OwnerActionContinuationLineage,
    OwnerActionController,
    OwnerActionDenial,
    OwnerActionDenialInspection,
    OwnerActionReceiptInspection,
    OwnerActionRequestLineageInspection,
    PendingDecision,
)


_DEFAULT_MAX_OUTCOMES = 256
_OUTCOME_ISSUER_SEAL = object()
_ACTION_OUTCOME_AUTHORITY_INSTANCE_SEAL = object()
_ACTION_OUTCOME_REGISTRATION_SEAL = object()


class ActionOutcomeKind(str, Enum):
    """Closed semantic result set; no free-form status or reason channel exists."""

    CONFIRMATION_REQUIRED = "confirmation_required"
    DENIED = "denied"
    CANCELLED = "cancelled"
    STALE_NOT_STARTED = "stale_not_started"
    SUCCEEDED_READ = "succeeded_read"
    SUCCEEDED_COMMITTED = "succeeded_committed"
    FAILED_NO_EFFECT = "failed_no_effect"
    FAILED_PARTIAL = "failed_partial"
    TIMED_OUT = "timed_out"
    EFFECT_UNKNOWN = "effect_unknown"

    # Canonical receipts may become stale after an attempt.  Preserve that
    # uncertainty rather than folding it into success/failure wording.
    STALE_NO_EFFECT = "stale_no_effect"
    STALE_NOT_COMMITTED = "stale_not_committed"
    STALE_COMMITTED = "stale_committed"
    STALE_PARTIAL = "stale_partial"
    STALE_EFFECT_UNKNOWN = "stale_effect_unknown"


class ActionOutcomeOperation(str, Enum):
    """Safe public projection of the code-owned owner operation set."""

    READ_ARTIFACT = "read_artifact"
    SEARCH_ARTIFACT = "search_artifact"
    SAVE_MEMORY = "save_memory"
    RUN_SANDBOX_COMMAND = "run_sandbox_command"


class ActionOutcomeProposition(str, Enum):
    """Closed, language-independent facts used by Composer and Guard alike."""

    WAIT_CONFIRM = "wait_confirm"
    DENIED = "denied"
    CANCELLED = "cancelled"
    STALE = "stale"
    ATTEMPTED = "attempted"
    NOT_STARTED = "not_started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    NO_EFFECT = "no_effect"
    PARTIAL = "partial"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"
    OUTPUT_BODY = "output_body"


@dataclass(frozen=True, slots=True)
class ActionOutcomeSemantics:
    """Single code-owned status matrix shared by prompting and validation."""

    operation_hint: str
    status_hint: str
    required_propositions: frozenset[ActionOutcomeProposition]
    forbidden_propositions: frozenset[ActionOutcomeProposition]


_ACTION_OPERATION_HINTS = MappingProxyType(
    {
        ActionOutcomeOperation.READ_ARTIFACT: "读取指定资料",
        ActionOutcomeOperation.SEARCH_ARTIFACT: "在指定资料中查找",
        ActionOutcomeOperation.SAVE_MEMORY: "保存一条明确记忆",
        ActionOutcomeOperation.RUN_SANDBOX_COMMAND: "运行一次受限指令",
    }
)


def _status_semantics(
    status_hint: str,
    *,
    required: tuple[ActionOutcomeProposition, ...],
    forbidden: tuple[ActionOutcomeProposition, ...],
) -> tuple[str, frozenset[ActionOutcomeProposition], frozenset[ActionOutcomeProposition]]:
    return status_hint, frozenset(required), frozenset(forbidden)


_ACTION_STATUS_SEMANTICS = MappingProxyType(
    {
        ActionOutcomeKind.CONFIRMATION_REQUIRED: _status_semantics(
            "正在等待主人明确确认，尚未执行；自然请求确认，不能声称已经完成",
            required=(
                ActionOutcomeProposition.WAIT_CONFIRM,
                ActionOutcomeProposition.NOT_STARTED,
            ),
            forbidden=(
                ActionOutcomeProposition.ATTEMPTED,
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.FAILED,
                ActionOutcomeProposition.COMMITTED,
                ActionOutcomeProposition.PARTIAL,
            ),
        ),
        ActionOutcomeKind.DENIED: _status_semantics(
            "请求已被代码权限边界拒绝且没有执行；自然说明不能执行，不编造具体理由",
            required=(
                ActionOutcomeProposition.DENIED,
                ActionOutcomeProposition.NOT_STARTED,
            ),
            forbidden=(
                ActionOutcomeProposition.ATTEMPTED,
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.FAILED,
                ActionOutcomeProposition.COMMITTED,
            ),
        ),
        ActionOutcomeKind.CANCELLED: _status_semantics(
            "请求已取消且没有执行；自然确认取消，不得暗示已经执行",
            required=(
                ActionOutcomeProposition.CANCELLED,
                ActionOutcomeProposition.NOT_STARTED,
            ),
            forbidden=(
                ActionOutcomeProposition.ATTEMPTED,
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.COMMITTED,
            ),
        ),
        ActionOutcomeKind.STALE_NOT_STARTED: _status_semantics(
            "请求已过时且没有开始；自然说明没有执行",
            required=(
                ActionOutcomeProposition.STALE,
                ActionOutcomeProposition.NOT_STARTED,
            ),
            forbidden=(
                ActionOutcomeProposition.ATTEMPTED,
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.COMMITTED,
            ),
        ),
        ActionOutcomeKind.SUCCEEDED_READ: _status_semantics(
            "只读动作已成功完成，但没有经过安全授权的结果正文可展示；只能说明完成，不能编造内容",
            required=(ActionOutcomeProposition.SUCCEEDED,),
            forbidden=(
                ActionOutcomeProposition.NOT_STARTED,
                ActionOutcomeProposition.FAILED,
                ActionOutcomeProposition.PARTIAL,
                ActionOutcomeProposition.UNKNOWN,
                ActionOutcomeProposition.OUTPUT_BODY,
            ),
        ),
        ActionOutcomeKind.SUCCEEDED_COMMITTED: _status_semantics(
            "写入动作已成功提交；只能说明已完成，不能虚构额外效果",
            required=(
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.COMMITTED,
            ),
            forbidden=(
                ActionOutcomeProposition.NOT_STARTED,
                ActionOutcomeProposition.FAILED,
                ActionOutcomeProposition.NOT_COMMITTED,
                ActionOutcomeProposition.PARTIAL,
                ActionOutcomeProposition.UNKNOWN,
            ),
        ),
        ActionOutcomeKind.FAILED_NO_EFFECT: _status_semantics(
            "动作已经尝试但失败，代码确认没有产生副作用；不得说成成功或根本未执行",
            required=(
                ActionOutcomeProposition.ATTEMPTED,
                ActionOutcomeProposition.FAILED,
                ActionOutcomeProposition.NO_EFFECT,
            ),
            forbidden=(
                ActionOutcomeProposition.NOT_STARTED,
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.COMMITTED,
                ActionOutcomeProposition.PARTIAL,
                ActionOutcomeProposition.UNKNOWN,
            ),
        ),
        ActionOutcomeKind.FAILED_PARTIAL: _status_semantics(
            "动作失败但可能已经产生部分效果；必须保留不确定性，不能说成完全成功或完全未执行",
            required=(
                ActionOutcomeProposition.ATTEMPTED,
                ActionOutcomeProposition.FAILED,
                ActionOutcomeProposition.PARTIAL,
            ),
            forbidden=(
                ActionOutcomeProposition.NOT_STARTED,
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.NO_EFFECT,
            ),
        ),
        ActionOutcomeKind.TIMED_OUT: _status_semantics(
            "动作已经发起但超时，最终效果不能由对话层确定；必须保留不确定性",
            required=(
                ActionOutcomeProposition.ATTEMPTED,
                ActionOutcomeProposition.TIMED_OUT,
                ActionOutcomeProposition.UNKNOWN,
            ),
            forbidden=(
                ActionOutcomeProposition.NOT_STARTED,
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.FAILED,
                ActionOutcomeProposition.COMMITTED,
                ActionOutcomeProposition.NOT_COMMITTED,
                ActionOutcomeProposition.NO_EFFECT,
            ),
        ),
        ActionOutcomeKind.EFFECT_UNKNOWN: _status_semantics(
            "动作已经发起但效果未知；必须坦率说明无法确定，不能猜测成功或失败",
            required=(
                ActionOutcomeProposition.ATTEMPTED,
                ActionOutcomeProposition.UNKNOWN,
            ),
            forbidden=(
                ActionOutcomeProposition.NOT_STARTED,
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.FAILED,
                ActionOutcomeProposition.COMMITTED,
                ActionOutcomeProposition.NOT_COMMITTED,
                ActionOutcomeProposition.NO_EFFECT,
            ),
        ),
        ActionOutcomeKind.STALE_NO_EFFECT: _status_semantics(
            "动作已经尝试，结果已过时，但代码确认没有副作用；不得说成成功或根本没尝试",
            required=(
                ActionOutcomeProposition.STALE,
                ActionOutcomeProposition.ATTEMPTED,
                ActionOutcomeProposition.NO_EFFECT,
            ),
            forbidden=(
                ActionOutcomeProposition.NOT_STARTED,
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.COMMITTED,
            ),
        ),
        ActionOutcomeKind.STALE_NOT_COMMITTED: _status_semantics(
            "动作已经尝试，结果已过时且没有提交；不得说成已保存或根本没尝试",
            required=(
                ActionOutcomeProposition.STALE,
                ActionOutcomeProposition.ATTEMPTED,
                ActionOutcomeProposition.NOT_COMMITTED,
            ),
            forbidden=(
                ActionOutcomeProposition.NOT_STARTED,
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.COMMITTED,
            ),
        ),
        ActionOutcomeKind.STALE_COMMITTED: _status_semantics(
            "动作已经尝试；结果虽然已过时，但提交确实发生，不能否认已经发生的提交",
            required=(
                ActionOutcomeProposition.STALE,
                ActionOutcomeProposition.ATTEMPTED,
                ActionOutcomeProposition.COMMITTED,
            ),
            forbidden=(
                ActionOutcomeProposition.NOT_STARTED,
                ActionOutcomeProposition.FAILED,
                ActionOutcomeProposition.NOT_COMMITTED,
                ActionOutcomeProposition.NO_EFFECT,
            ),
        ),
        ActionOutcomeKind.STALE_PARTIAL: _status_semantics(
            "动作已经尝试，结果已过时且可能只有部分效果；必须保留不确定性",
            required=(
                ActionOutcomeProposition.STALE,
                ActionOutcomeProposition.ATTEMPTED,
                ActionOutcomeProposition.PARTIAL,
            ),
            forbidden=(
                ActionOutcomeProposition.NOT_STARTED,
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.NO_EFFECT,
            ),
        ),
        ActionOutcomeKind.STALE_EFFECT_UNKNOWN: _status_semantics(
            "动作已经尝试，结果已过时且效果未知；必须保留不确定性",
            required=(
                ActionOutcomeProposition.STALE,
                ActionOutcomeProposition.ATTEMPTED,
                ActionOutcomeProposition.UNKNOWN,
            ),
            forbidden=(
                ActionOutcomeProposition.NOT_STARTED,
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.FAILED,
                ActionOutcomeProposition.COMMITTED,
                ActionOutcomeProposition.NOT_COMMITTED,
                ActionOutcomeProposition.NO_EFFECT,
            ),
        ),
    }
)

if frozenset(_ACTION_OPERATION_HINTS) != frozenset(ActionOutcomeOperation):
    raise RuntimeError("action_outcome_operation_hint_incomplete")
if frozenset(_ACTION_STATUS_SEMANTICS) != frozenset(ActionOutcomeKind):
    raise RuntimeError("action_outcome_status_semantics_incomplete")


_ACTION_OUTCOME_TRUTH_PROFILE = MappingProxyType(
    {
        ActionOutcomeKind.CONFIRMATION_REQUIRED: ("not_started", None, None),
        ActionOutcomeKind.DENIED: ("not_started", None, None),
        ActionOutcomeKind.CANCELLED: ("not_started", None, None),
        ActionOutcomeKind.STALE_NOT_STARTED: ("not_started", None, None),
        ActionOutcomeKind.SUCCEEDED_READ: ("attempted", "succeeded", "read_only"),
        ActionOutcomeKind.SUCCEEDED_COMMITTED: ("attempted", "succeeded", "committed"),
        ActionOutcomeKind.FAILED_NO_EFFECT: ("attempted", "failed", "no_effect"),
        ActionOutcomeKind.FAILED_PARTIAL: ("attempted", "failed", "partial"),
        ActionOutcomeKind.TIMED_OUT: ("attempted", "timed_out", "unknown"),
        ActionOutcomeKind.EFFECT_UNKNOWN: ("attempted", "unknown", "unknown"),
        ActionOutcomeKind.STALE_NO_EFFECT: ("attempted", None, "no_effect"),
        ActionOutcomeKind.STALE_NOT_COMMITTED: ("attempted", None, "not_committed"),
        ActionOutcomeKind.STALE_COMMITTED: ("attempted", None, "committed"),
        ActionOutcomeKind.STALE_PARTIAL: ("attempted", None, "partial"),
        ActionOutcomeKind.STALE_EFFECT_UNKNOWN: ("attempted", "unknown", "unknown"),
    }
)
if frozenset(_ACTION_OUTCOME_TRUTH_PROFILE) != frozenset(ActionOutcomeKind):
    raise RuntimeError("action_outcome_truth_profile_incomplete")


def _closed_lattice_forbidden(
    kind: ActionOutcomeKind,
    operation: ActionOutcomeOperation,
) -> frozenset[ActionOutcomeProposition]:
    execution, result, effect = _ACTION_OUTCOME_TRUTH_PROFILE[kind]
    forbidden = {ActionOutcomeProposition.OUTPUT_BODY}
    if execution == "not_started":
        forbidden.update(
            {
                ActionOutcomeProposition.ATTEMPTED,
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.FAILED,
                ActionOutcomeProposition.COMMITTED,
                ActionOutcomeProposition.PARTIAL,
                ActionOutcomeProposition.TIMED_OUT,
                ActionOutcomeProposition.UNKNOWN,
            }
        )
    else:
        forbidden.add(ActionOutcomeProposition.NOT_STARTED)

    if result == "succeeded":
        forbidden.update(
            {
                ActionOutcomeProposition.FAILED,
                ActionOutcomeProposition.TIMED_OUT,
                ActionOutcomeProposition.UNKNOWN,
            }
        )
    elif result == "failed":
        forbidden.update(
            {
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.TIMED_OUT,
                ActionOutcomeProposition.UNKNOWN,
            }
        )
    elif result == "timed_out":
        forbidden.update(
            {
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.FAILED,
            }
        )
    elif result == "unknown":
        forbidden.update(
            {
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.FAILED,
                ActionOutcomeProposition.TIMED_OUT,
            }
        )

    effect_conflicts = {
        "committed": {
            ActionOutcomeProposition.NOT_COMMITTED,
            ActionOutcomeProposition.NO_EFFECT,
            ActionOutcomeProposition.PARTIAL,
            ActionOutcomeProposition.UNKNOWN,
        },
        "not_committed": {
            ActionOutcomeProposition.COMMITTED,
            ActionOutcomeProposition.NO_EFFECT,
            ActionOutcomeProposition.PARTIAL,
            ActionOutcomeProposition.UNKNOWN,
        },
        "no_effect": {
            ActionOutcomeProposition.COMMITTED,
            ActionOutcomeProposition.PARTIAL,
            ActionOutcomeProposition.UNKNOWN,
        },
        "partial": {
            ActionOutcomeProposition.COMMITTED,
            ActionOutcomeProposition.NOT_COMMITTED,
            ActionOutcomeProposition.NO_EFFECT,
            ActionOutcomeProposition.UNKNOWN,
        },
        "unknown": {
            ActionOutcomeProposition.COMMITTED,
            ActionOutcomeProposition.NOT_COMMITTED,
            ActionOutcomeProposition.NO_EFFECT,
            ActionOutcomeProposition.PARTIAL,
        },
        "read_only": {
            ActionOutcomeProposition.COMMITTED,
            ActionOutcomeProposition.PARTIAL,
            ActionOutcomeProposition.UNKNOWN,
        },
    }
    if effect in effect_conflicts:
        forbidden.update(effect_conflicts[effect])
    if operation in {
        ActionOutcomeOperation.READ_ARTIFACT,
        ActionOutcomeOperation.SEARCH_ARTIFACT,
    }:
        forbidden.add(ActionOutcomeProposition.COMMITTED)
    return frozenset(forbidden)


def action_outcome_semantics(
    outcome: "ActionOutcomeIntent",
) -> ActionOutcomeSemantics:
    """Project one exact-shaped outcome into the sole delivery semantics table."""

    kind, operation, _attempted, _has_output = _outcome_snapshot(outcome)
    return action_outcome_semantics_for(kind, operation)


def action_outcome_semantics_for(
    kind: ActionOutcomeKind,
    operation: ActionOutcomeOperation,
) -> ActionOutcomeSemantics:
    """Return the shared closed semantics without minting outcome authority."""

    if type(kind) is not ActionOutcomeKind:
        raise ContractViolation("action_outcome_kind_invalid")
    if type(operation) is not ActionOutcomeOperation:
        raise ContractViolation("action_outcome_operation_invalid")
    status_hint, required, forbidden = _ACTION_STATUS_SEMANTICS[kind]
    return ActionOutcomeSemantics(
        operation_hint=_ACTION_OPERATION_HINTS[operation],
        status_hint=status_hint,
        required_propositions=required,
        forbidden_propositions=(
            forbidden | _closed_lattice_forbidden(kind, operation)
        ),
    )


def expand_action_outcome_propositions(
    outcome: "ActionOutcomeIntent",
    asserted: frozenset[ActionOutcomeProposition],
) -> frozenset[ActionOutcomeProposition]:
    """Apply code-level entailments for the exact closed outcome kind.

    These are semantic implications, not lexical shortcuts: cancellation and
    denial entail that execution did not start; failure, timeout, partial
    effect and effect-unknown outcomes entail an attempt; stale terminal
    variants entail an earlier attempt.  Read/search failure is intrinsically
    side-effect free, so a natural ``读取失败`` need not repeat that fact.
    """

    kind, operation, _attempted, _has_output = _outcome_snapshot(outcome)
    return expand_action_outcome_propositions_for(kind, operation, asserted)


def expand_action_outcome_propositions_for(
    kind: ActionOutcomeKind,
    operation: ActionOutcomeOperation,
    asserted: frozenset[ActionOutcomeProposition],
) -> frozenset[ActionOutcomeProposition]:
    """Apply the same entailments for closed kind/operation matrix tests."""

    if type(kind) is not ActionOutcomeKind:
        raise ContractViolation("action_outcome_kind_invalid")
    if type(operation) is not ActionOutcomeOperation:
        raise ContractViolation("action_outcome_operation_invalid")
    if type(asserted) is not frozenset or any(
        type(value) is not ActionOutcomeProposition for value in asserted
    ):
        raise ContractViolation("action_outcome_propositions_invalid")
    expanded = set(asserted)
    if (
        ActionOutcomeProposition.DENIED in expanded
        and kind is ActionOutcomeKind.DENIED
    ) or (
        ActionOutcomeProposition.CANCELLED in expanded
        and kind is ActionOutcomeKind.CANCELLED
    ):
        expanded.add(ActionOutcomeProposition.NOT_STARTED)
    if ActionOutcomeProposition.SUCCEEDED in expanded:
        expanded.add(ActionOutcomeProposition.ATTEMPTED)
    if ActionOutcomeProposition.COMMITTED in expanded:
        expanded.add(ActionOutcomeProposition.ATTEMPTED)
    if ActionOutcomeProposition.FAILED in expanded:
        expanded.add(ActionOutcomeProposition.ATTEMPTED)
    if ActionOutcomeProposition.TIMED_OUT in expanded:
        expanded.add(ActionOutcomeProposition.ATTEMPTED)
    if ActionOutcomeProposition.PARTIAL in expanded:
        expanded.add(ActionOutcomeProposition.ATTEMPTED)
        if kind is ActionOutcomeKind.FAILED_PARTIAL:
            expanded.add(ActionOutcomeProposition.FAILED)
    if (
        ActionOutcomeProposition.UNKNOWN in expanded
        and kind is ActionOutcomeKind.EFFECT_UNKNOWN
    ):
        expanded.add(ActionOutcomeProposition.ATTEMPTED)
    if (
        ActionOutcomeProposition.STALE in expanded
        and kind is not ActionOutcomeKind.STALE_NOT_STARTED
    ):
        expanded.add(ActionOutcomeProposition.ATTEMPTED)
    if (
        kind is ActionOutcomeKind.FAILED_NO_EFFECT
        and operation
        in {
            ActionOutcomeOperation.READ_ARTIFACT,
            ActionOutcomeOperation.SEARCH_ARTIFACT,
        }
        and ActionOutcomeProposition.FAILED in expanded
    ):
        expanded.add(ActionOutcomeProposition.NO_EFFECT)
    return frozenset(expanded)


_OWNER_OPERATION_PROJECTION = MappingProxyType(
    {
        OwnerActionOperation.ARTIFACT_READ_EXACT:
            ActionOutcomeOperation.READ_ARTIFACT,
        OwnerActionOperation.ARTIFACT_GREP:
            ActionOutcomeOperation.SEARCH_ARTIFACT,
        OwnerActionOperation.MEMORY_WRITE_LITERAL:
            ActionOutcomeOperation.SAVE_MEMORY,
        OwnerActionOperation.SANDBOX_SHELL_ONCE:
            ActionOutcomeOperation.RUN_SANDBOX_COMMAND,
    }
)
if frozenset(_OWNER_OPERATION_PROJECTION) != frozenset(OwnerActionOperation):
    raise RuntimeError("action_outcome_operation_projection_incomplete")


def _project_operation(operation: OwnerActionOperation) -> ActionOutcomeOperation:
    if type(operation) is not OwnerActionOperation:
        raise ContractViolation("action_outcome_operation_invalid")
    if frozenset(_OWNER_OPERATION_PROJECTION) != frozenset(OwnerActionOperation):
        raise ContractViolation("action_outcome_operation_projection_incomplete")
    try:
        return _OWNER_OPERATION_PROJECTION[operation]
    except KeyError:
        raise ContractViolation("action_outcome_operation_unmapped") from None


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class ActionOutcomeIntent:
    """Minimal public view of one exact Controller-derived outcome.

    Construction is disabled.  Exact authority lives in ``ActionOutcomeAuthority``;
    this public object carries no Controller, plan, request, receipt, identifier,
    digest, reason, path, or output payload.
    """

    kind: ActionOutcomeKind
    operation: ActionOutcomeOperation
    attempted: bool
    has_output: bool

    def trace_metadata(self) -> dict[str, str | bool]:
        kind, operation, attempted, has_output = (
            _inspect_action_outcome_presentation(self)
        )
        return {
            "outcome": kind,
            "operation": operation,
            "attempted": attempted,
            "has_output": has_output,
        }

    def __repr__(self) -> str:
        kind, operation, attempted, has_output = (
            _inspect_action_outcome_presentation(self)
        )
        return (
            "ActionOutcomeIntent("
            f"outcome={kind!r}, "
            f"operation={operation!r}, "
            f"attempted={attempted}, "
            f"has_output={has_output})"
        )


class _OutcomeSource(str, Enum):
    RECEIPT = "receipt"
    DENIAL = "denial"
    CONTINUATION = "continuation"


class _OutcomeConsumer(str, Enum):
    NONE = "none"
    GENERIC = "generic"
    REPLY_COMPOSER = "reply_composer"


class _OutcomeReclaimMode(str, Enum):
    ABANDONED_BEFORE_COMPOSER = "abandoned_before_composer"
    DELIVERY_TERMINAL = "delivery_terminal"


class _OutcomeRecord(NamedTuple):
    outcome: ActionOutcomeIntent
    source: _OutcomeSource
    source_object: object
    current_planned_action: PlannedAction
    capability: CapabilityClass
    operation: OwnerActionOperation
    origin_request: OwnerActionRequest | None
    receipt: ActionReceipt | None
    denial: OwnerActionDenial | None
    lineage: OwnerActionContinuationLineage | None
    outcome_snapshot: tuple[
        ActionOutcomeKind,
        ActionOutcomeOperation,
        bool,
        bool,
    ]
    consumed: bool
    consumer_kind: _OutcomeConsumer
    composer_consumer: object | None
    composer_snapshot: tuple[object, ...] | None


class _OutcomeReclaimRecord(NamedTuple):
    authority_ref: weakref.ReferenceType["ActionOutcomeAuthority"]
    source: _OutcomeSource
    source_fingerprint: str
    outcome_snapshot: tuple[
        ActionOutcomeKind,
        ActionOutcomeOperation,
        bool,
        bool,
    ]
    mode: _OutcomeReclaimMode


def _new_outcome(
    *,
    kind: ActionOutcomeKind,
    operation: ActionOutcomeOperation,
    attempted: bool,
    has_output: bool,
    issuer_seal: object,
) -> ActionOutcomeIntent:
    if issuer_seal is not _OUTCOME_ISSUER_SEAL:
        raise ContractViolation("action_outcome_issuer_forbidden")
    if type(kind) is not ActionOutcomeKind:
        raise ContractViolation("action_outcome_kind_invalid")
    if type(operation) is not ActionOutcomeOperation:
        raise ContractViolation("action_outcome_operation_invalid")
    if type(attempted) is not bool or type(has_output) is not bool:
        raise ContractViolation("action_outcome_shape_invalid")
    outcome = object.__new__(ActionOutcomeIntent)
    object.__setattr__(outcome, "kind", kind)
    object.__setattr__(outcome, "operation", operation)
    object.__setattr__(outcome, "attempted", attempted)
    object.__setattr__(outcome, "has_output", has_output)
    return outcome


def _outcome_snapshot(
    outcome: ActionOutcomeIntent,
) -> tuple[ActionOutcomeKind, ActionOutcomeOperation, bool, bool]:
    if type(outcome) is not ActionOutcomeIntent:
        raise ContractViolation("action_outcome_required")
    try:
        kind = object.__getattribute__(outcome, "kind")
        operation = object.__getattribute__(outcome, "operation")
        attempted = object.__getattribute__(outcome, "attempted")
        has_output = object.__getattribute__(outcome, "has_output")
    except AttributeError:
        raise ContractViolation("action_outcome_corrupt") from None
    if type(kind) is not ActionOutcomeKind:
        raise ContractViolation("action_outcome_corrupt")
    if type(operation) is not ActionOutcomeOperation:
        raise ContractViolation("action_outcome_corrupt")
    if type(attempted) is not bool or type(has_output) is not bool:
        raise ContractViolation("action_outcome_corrupt")
    return (
        kind,
        operation,
        attempted,
        has_output,
    )


def _require_execute_plan(
    planned_action_authority: PlannedActionAuthority,
    value: PlannedAction,
) -> PlannedAction:
    plan = planned_action_authority.inspect_plan(value)
    if (
        plan.kind is not ActionKind.EXECUTE_ACTION
        or plan.structural_outcome is not StructuralOutcome.CONTINUE
        or plan.action.reply_target is None
        or type(plan.action.operation_intent) is not OwnerActionOperation
        or not plan.action.capability_intent
    ):
        raise ContractViolation("action_outcome_execute_plan_required")
    return plan


def _require_reply_plan(
    planned_action_authority: PlannedActionAuthority,
    value: PlannedAction,
) -> PlannedAction:
    plan = planned_action_authority.inspect_plan(value)
    if (
        plan.kind is not ActionKind.REPLY
        or plan.structural_outcome is not StructuralOutcome.CONTINUE
        or plan.action.reply_target is None
        or plan.action.capability_intent
        or plan.action.operation_intent is not None
    ):
        raise ContractViolation("action_outcome_reply_plan_required")
    return plan


def _map_receipt(receipt: ActionReceipt) -> tuple[ActionOutcomeKind, bool, bool]:
    if type(receipt) is not ActionReceipt:
        raise ContractViolation("action_outcome_receipt_required")
    if receipt.output is not None:
        raise ContractViolation("action_outcome_output_authority_unavailable")
    status = receipt.status
    effect = receipt.effect_state
    if type(status) is not ActionReceiptStatus or type(effect) is not ActionEffectState:
        raise ContractViolation("action_outcome_receipt_corrupt")
    if type(receipt.attempt_count) is not int or receipt.attempt_count not in (0, 1):
        raise ContractViolation("action_outcome_receipt_corrupt")

    if status is ActionReceiptStatus.CONFIRMATION_REQUIRED:
        kind = ActionOutcomeKind.CONFIRMATION_REQUIRED
    elif status is ActionReceiptStatus.DENIED:
        kind = ActionOutcomeKind.DENIED
    elif status is ActionReceiptStatus.CANCELLED:
        kind = ActionOutcomeKind.CANCELLED
    elif status is ActionReceiptStatus.SUCCEEDED:
        kind = (
            ActionOutcomeKind.SUCCEEDED_READ
            if receipt.side_effect is ActionSideEffect.SCOPED_READ
            else ActionOutcomeKind.SUCCEEDED_COMMITTED
        )
    elif status is ActionReceiptStatus.FAILED:
        kind = (
            ActionOutcomeKind.FAILED_PARTIAL
            if effect is ActionEffectState.PARTIAL
            else ActionOutcomeKind.FAILED_NO_EFFECT
        )
    elif status is ActionReceiptStatus.TIMED_OUT:
        kind = ActionOutcomeKind.TIMED_OUT
    elif status is ActionReceiptStatus.EFFECT_UNKNOWN:
        kind = ActionOutcomeKind.EFFECT_UNKNOWN
    elif status is ActionReceiptStatus.STALE:
        kind = {
            ActionEffectState.NOT_STARTED: ActionOutcomeKind.STALE_NOT_STARTED,
            ActionEffectState.NO_SIDE_EFFECT: ActionOutcomeKind.STALE_NO_EFFECT,
            ActionEffectState.NOT_COMMITTED: ActionOutcomeKind.STALE_NOT_COMMITTED,
            ActionEffectState.COMMITTED: ActionOutcomeKind.STALE_COMMITTED,
            ActionEffectState.PARTIAL: ActionOutcomeKind.STALE_PARTIAL,
            ActionEffectState.UNKNOWN: ActionOutcomeKind.STALE_EFFECT_UNKNOWN,
        }[effect]
    else:  # pragma: no cover - the exact Enum is closed above
        raise ContractViolation("action_outcome_receipt_status_unhandled")

    attempted = receipt.attempt_count == 1
    unattempted_kinds = {
        ActionOutcomeKind.CONFIRMATION_REQUIRED,
        ActionOutcomeKind.DENIED,
        ActionOutcomeKind.CANCELLED,
        ActionOutcomeKind.STALE_NOT_STARTED,
    }
    if attempted is not (kind not in unattempted_kinds):
        raise ContractViolation("action_outcome_attempt_shape_invalid")
    return kind, attempted, False


def _validate_persistent_state(
    state: tuple[_OutcomeRecord, ...],
) -> None:
    if type(state) is not tuple:
        raise ContractViolation("action_outcome_authority_state_corrupt")
    for index, record in enumerate(state):
        if (
            type(record) is not _OutcomeRecord
            or type(record.source) is not _OutcomeSource
            or type(record.current_planned_action) is not PlannedAction
            or type(record.capability) is not CapabilityClass
            or type(record.operation) is not OwnerActionOperation
            or type(record.outcome_snapshot) is not tuple
            or type(record.consumed) is not bool
            or type(record.consumer_kind) is not _OutcomeConsumer
            or _outcome_snapshot(record.outcome) != record.outcome_snapshot
        ):
            raise ContractViolation("action_outcome_authority_state_corrupt")
        if not record.consumed:
            consumer_valid = bool(
                record.consumer_kind is _OutcomeConsumer.NONE
                and record.composer_consumer is None
                and record.composer_snapshot is None
            )
        elif record.consumer_kind is _OutcomeConsumer.GENERIC:
            consumer_valid = bool(
                record.composer_consumer is None
                and record.composer_snapshot is None
            )
        elif record.consumer_kind is _OutcomeConsumer.REPLY_COMPOSER:
            try:
                from .reply_composer import (
                    ReplyComposerRequest,
                    _inspect_canonical_reply_composer_request,
                )

                consumer_valid = bool(
                    type(record.composer_consumer) is ReplyComposerRequest
                    and type(record.composer_snapshot) is tuple
                    and _inspect_canonical_reply_composer_request(
                        record.composer_consumer
                    )
                    == record.composer_snapshot
                )
            except Exception as exc:
                raise ContractViolation(
                    "action_outcome_composer_consumer_corrupt"
                ) from exc
        else:  # pragma: no cover - exact internal Enum is closed
            consumer_valid = False
        if not consumer_valid:
            raise ContractViolation("action_outcome_authority_state_corrupt")
        if record.source is _OutcomeSource.RECEIPT:
            source_valid = bool(
                record.source_object is record.receipt
                and record.origin_request is not None
                and record.receipt is not None
                and record.denial is None
                and record.lineage is None
            )
        elif record.source is _OutcomeSource.DENIAL:
            source_valid = bool(
                record.source_object is record.denial
                and record.origin_request is None
                and record.receipt is None
                and record.denial is not None
                and record.lineage is None
            )
        else:
            source_valid = bool(
                record.source_object is record.lineage
                and record.origin_request is not None
                and record.receipt is not None
                and record.denial is None
                and record.lineage is not None
            )
        if not source_valid:
            raise ContractViolation("action_outcome_authority_state_corrupt")
        for other in state[index + 1 :]:
            if record.outcome is other.outcome or (
                record.source is other.source
                and record.source_object is other.source_object
            ):
                raise ContractViolation("action_outcome_authority_state_corrupt")


def _find_persistent_record(
    state: tuple[_OutcomeRecord, ...],
    outcome: ActionOutcomeIntent,
) -> _OutcomeRecord:
    if type(outcome) is not ActionOutcomeIntent:
        raise ContractViolation("action_outcome_required")
    _validate_persistent_state(state)
    matches = tuple(record for record in state if record.outcome is outcome)
    if not matches:
        raise ContractViolation("action_outcome_not_canonical")
    if len(matches) != 1:
        raise ContractViolation("action_outcome_authority_state_corrupt")
    record = matches[0]
    if _outcome_snapshot(outcome) != record.outcome_snapshot:
        raise ContractViolation("action_outcome_corrupt")
    return record


def _build_runtime_registry():
    """Build a closure-owned canonical runtime and outcome vault.

    The returned callables expose complete operations only.  No callable accepts a
    candidate authority, caller-built outcome/record, caller-selected ledger limit,
    or returns a registration/state record.  The weak mappings and their lock remain
    closure-local so object fields and module globals cannot replace authoritative
    state.
    """

    registry_lock = threading.RLock()
    runtime_registrations = WeakKeyDictionary()
    authority_states = WeakKeyDictionary()
    authority_retired_sources = WeakKeyDictionary()
    outcome_reclaims = WeakKeyDictionary()
    outcome_presentations = WeakKeyDictionary()
    source_fingerprint_key = secrets.token_bytes(32)

    def source_fingerprint(
        source: _OutcomeSource,
        source_object: object,
    ) -> str:
        """Project one inspected canonical source to a non-reversible replay key."""

        if source is _OutcomeSource.RECEIPT:
            if type(source_object) is not ActionReceipt:
                raise ContractViolation("action_outcome_source_invalid")
            parts = (
                source.value,
                source_object.action_id,
                source_object.request_digest,
                source_object.idempotency_key,
                source_object.status.value,
                source_object.effect_state.value,
                source_object.result_digest,
                source_object.pending_confirmation_id,
                source_object.confirmation_proof_digest,
                str(source_object.started_at),
                str(source_object.completed_at),
                str(source_object.attempt_count),
            )
        elif source is _OutcomeSource.DENIAL:
            if type(source_object) is not OwnerActionDenial:
                raise ContractViolation("action_outcome_source_invalid")
            parts = (
                source.value,
                source_object.action_id,
                source_object.denial_digest,
                source_object.status.value,
                source_object.effect_state.value,
                str(source_object.denied_at),
            )
        elif source is _OutcomeSource.CONTINUATION:
            if type(source_object) is not OwnerActionContinuationLineage:
                raise ContractViolation("action_outcome_source_invalid")
            parts = (
                source.value,
                source_object.current_action_id,
                source_object.origin_action_id,
                source_object.lineage_digest,
                source_object.decision.value,
                source_object.capability.value,
                source_object.operation.value,
            )
        else:  # pragma: no cover - exact internal Enum is closed
            raise ContractViolation("action_outcome_source_invalid")
        return hmac.new(
            source_fingerprint_key,
            "\x1f".join(parts).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def retired_sources_for_locked(authority: object) -> frozenset[str]:
        retired = authority_retired_sources.get(authority)
        if type(retired) is not frozenset or any(
            type(item) is not str or len(item) != 64 for item in retired
        ):
            raise ContractViolation("action_outcome_authority_state_corrupt")
        return retired

    def register_presentation_locked(
        authority: object,
        outcome: ActionOutcomeIntent,
        snapshot: tuple[
            ActionOutcomeKind,
            ActionOutcomeOperation,
            bool,
            bool,
        ],
    ) -> None:
        if (
            type(authority) is not ActionOutcomeAuthority
            or type(outcome) is not ActionOutcomeIntent
            or type(snapshot) is not tuple
            or len(snapshot) != 4
            or _outcome_snapshot(outcome) != snapshot
            or outcome in outcome_presentations
        ):
            raise ContractViolation("action_outcome_corrupt")
        kind, operation, attempted, has_output = snapshot
        if (
            type(kind) is not ActionOutcomeKind
            or type(operation) is not ActionOutcomeOperation
            or type(attempted) is not bool
            or type(has_output) is not bool
        ):
            raise ContractViolation("action_outcome_corrupt")
        outcome_presentations[outcome] = (
            weakref.ref(authority),
            snapshot,
            (
                kind.value,
                operation.value,
                attempted,
                has_output,
            ),
        )

    def inspect_presentation(
        outcome: ActionOutcomeIntent,
    ) -> tuple[str, str, bool, bool]:
        if type(outcome) is not ActionOutcomeIntent:
            raise ContractViolation("action_outcome_corrupt")
        with registry_lock:
            presentation_record = outcome_presentations.get(outcome)
            if type(presentation_record) is not tuple or len(presentation_record) != 3:
                raise ContractViolation("action_outcome_corrupt")
            authority_ref, snapshot, presentation = presentation_record
            authority = (
                authority_ref()
                if type(authority_ref) is weakref.ReferenceType
                else None
            )
            if (
                type(authority) is not ActionOutcomeAuthority
                or type(snapshot) is not tuple
                or len(snapshot) != 4
                or type(presentation) is not tuple
                or len(presentation) != 4
            ):
                raise ContractViolation("action_outcome_corrupt")
            if _outcome_snapshot(outcome) != snapshot:
                raise ContractViolation("action_outcome_corrupt")
            try:
                _controller, _plan_authority, _limit, _registration, state = (
                    registration_for_locked(authority)
                )
            except ContractViolation:
                raise ContractViolation("action_outcome_corrupt") from None

            active = tuple(
                record
                for record in state
                if type(record) is _OutcomeRecord and record.outcome is outcome
            )
            reclaimed = outcome_reclaims.get(outcome)
            if len(active) == 1 and reclaimed is None:
                if active[0].outcome_snapshot != snapshot:
                    raise ContractViolation("action_outcome_corrupt")
            elif not active and type(reclaimed) is _OutcomeReclaimRecord:
                if (
                    reclaimed.authority_ref() is not authority
                    or type(reclaimed.outcome_snapshot) is not tuple
                    or reclaimed.outcome_snapshot != snapshot
                ):
                    raise ContractViolation("action_outcome_corrupt")
            else:
                raise ContractViolation("action_outcome_corrupt")
            if (
                type(presentation[0]) is not str
                or type(presentation[1]) is not str
                or type(presentation[2]) is not bool
                or type(presentation[3]) is not bool
            ):
                raise ContractViolation("action_outcome_corrupt")
            return presentation

    def find_active_record_locked(
        authority: object,
        state: tuple[_OutcomeRecord, ...],
        outcome: ActionOutcomeIntent,
    ) -> _OutcomeRecord:
        if type(outcome) is not ActionOutcomeIntent:
            raise ContractViolation("action_outcome_required")
        reclaimed = outcome_reclaims.get(outcome)
        if reclaimed is not None:
            if (
                type(reclaimed) is not _OutcomeReclaimRecord
                or reclaimed.authority_ref() is not authority
                or type(reclaimed.outcome_snapshot) is not tuple
                or reclaimed.outcome_snapshot != _outcome_snapshot(outcome)
                or type(reclaimed.source) is not _OutcomeSource
                or type(reclaimed.source_fingerprint) is not str
                or len(reclaimed.source_fingerprint) != 64
                or type(reclaimed.mode) is not _OutcomeReclaimMode
            ):
                raise ContractViolation("action_outcome_reclaim_state_corrupt")
            raise ContractViolation("action_outcome_reclaimed")
        return _find_persistent_record(state, outcome)

    def registration_for_locked(
        authority: object,
        *,
        exact_controller: OwnerActionController | None = None,
        exact_plan_authority: PlannedActionAuthority | None = None,
    ) -> tuple[
        OwnerActionController,
        PlannedActionAuthority,
        int,
        tuple[object, ...],
        tuple[_OutcomeRecord, ...],
    ]:
        if type(authority) is not ActionOutcomeAuthority:
            raise ContractViolation("action_outcome_authority_not_canonical")
        controller = getattr(authority, "_controller", None)
        planned_action_authority = getattr(
            authority,
            "_planned_action_authority",
            None,
        )
        if (
            type(controller) is not OwnerActionController
            or type(planned_action_authority) is not PlannedActionAuthority
            or (
                exact_controller is not None
                and controller is not exact_controller
            )
            or (
                exact_plan_authority is not None
                and planned_action_authority is not exact_plan_authority
            )
        ):
            raise ContractViolation("action_outcome_authority_not_canonical")
        registration = runtime_registrations.get(controller)
        if type(registration) is not tuple or len(registration) != 4:
            raise ContractViolation("action_outcome_authority_not_canonical")
        seal, authority_ref, registered_plan_authority, limit = registration
        state = authority_states.get(authority)
        if (
            seal is not _ACTION_OUTCOME_REGISTRATION_SEAL
            or type(authority_ref) is not weakref.ReferenceType
            or authority_ref() is not authority
            or registered_plan_authority is not planned_action_authority
            or type(limit) is not int
            or limit < 1
            or getattr(authority, "_max_outcomes", None) != limit
            or getattr(authority, "_authority_seal", None)
            is not _ACTION_OUTCOME_AUTHORITY_INSTANCE_SEAL
            or authority not in authority_states
            or authority not in authority_retired_sources
        ):
            raise ContractViolation("action_outcome_authority_not_canonical")
        _validate_persistent_state(state)
        retired_sources_for_locked(authority)
        if len(state) > limit:
            raise ContractViolation("action_outcome_authority_state_corrupt")
        return (
            controller,
            planned_action_authority,
            limit,
            registration,
            state,
        )

    def issue_runtime(
        controller: OwnerActionController,
        planned_action_authority: PlannedActionAuthority,
        *,
        max_outcomes: int,
    ) -> "ActionOutcomeAuthority":
        if type(controller) is not OwnerActionController:
            raise ContractViolation("action_outcome_controller_required")
        if type(planned_action_authority) is not PlannedActionAuthority:
            raise ContractViolation("action_outcome_plan_authority_required")
        if type(max_outcomes) is not int or max_outcomes < 1:
            raise ContractViolation("action_outcome_limit_invalid")

        # Lock order is Controller -> vault everywhere.  The Controller mirrors
        # are diagnostic/tamper-detection copies only; the closure registration
        # remains authoritative.
        with controller._lock:
            if planned_action_authority is not controller._planned_action_authority:
                raise ContractViolation("action_outcome_plan_authority_mismatch")
            with registry_lock:
                if (
                    controller in runtime_registrations
                    or controller._action_outcome_authority is not None
                    or controller._action_outcome_plan_authority is not None
                    or controller._action_outcome_authority_snapshot is not None
                ):
                    raise ContractViolation("action_outcome_authority_already_issued")
                authority = object.__new__(ActionOutcomeAuthority)
                object.__setattr__(
                    authority,
                    "_authority_seal",
                    _ACTION_OUTCOME_AUTHORITY_INSTANCE_SEAL,
                )
                object.__setattr__(authority, "_controller", controller)
                object.__setattr__(
                    authority,
                    "_planned_action_authority",
                    planned_action_authority,
                )
                object.__setattr__(authority, "_max_outcomes", max_outcomes)
                registration = (
                    _ACTION_OUTCOME_REGISTRATION_SEAL,
                    weakref.ref(authority),
                    planned_action_authority,
                    max_outcomes,
                )
                runtime_registrations[controller] = registration
                authority_states[authority] = ()
                authority_retired_sources[authority] = frozenset()
                object.__setattr__(
                    controller,
                    "_action_outcome_authority",
                    authority,
                )
                object.__setattr__(
                    controller,
                    "_action_outcome_plan_authority",
                    planned_action_authority,
                )
                object.__setattr__(
                    controller,
                    "_action_outcome_authority_snapshot",
                    registration,
                )
                return authority

    def inspect(
        authority: object,
        *,
        controller: OwnerActionController,
        planned_action_authority: PlannedActionAuthority,
    ) -> None:
        with registry_lock:
            (
                registered_controller,
                registered_plan_authority,
                _limit,
                registration,
                _state,
            ) = registration_for_locked(
                authority,
                exact_controller=controller,
                exact_plan_authority=planned_action_authority,
            )
            if (
                registered_controller is not controller
                or registered_plan_authority is not planned_action_authority
                or controller._planned_action_authority
                is not planned_action_authority
                or controller._action_outcome_authority is not authority
                or controller._action_outcome_plan_authority
                is not planned_action_authority
                or controller._action_outcome_authority_snapshot is not registration
            ):
                raise ContractViolation("action_outcome_authority_not_canonical")

    def issue_outcome(
        authority: object,
        *,
        source: _OutcomeSource,
        current_planned_action: PlannedAction,
        origin_request: OwnerActionRequest | None = None,
        receipt: ActionReceipt | None = None,
        denial: OwnerActionDenial | None = None,
        lineage: OwnerActionContinuationLineage | None = None,
    ) -> ActionOutcomeIntent:
        if type(authority) is not ActionOutcomeAuthority:
            raise ContractViolation("action_outcome_authority_not_canonical")
        authority._require_runtime_registration()
        if source is _OutcomeSource.RECEIPT:
            if (
                origin_request is None
                or receipt is None
                or denial is not None
                or lineage is not None
            ):
                raise ContractViolation("action_outcome_source_invalid")
            plan, capability, operation, kind, attempted = (
                authority._inspect_direct_receipt(
                    current_planned_action=current_planned_action,
                    origin_request=origin_request,
                    receipt=receipt,
                )
            )
            source_object = receipt
        elif source is _OutcomeSource.DENIAL:
            if (
                origin_request is not None
                or receipt is not None
                or denial is None
                or lineage is not None
            ):
                raise ContractViolation("action_outcome_source_invalid")
            plan, capability, operation = authority._inspect_denial_source(
                current_planned_action=current_planned_action,
                denial=denial,
            )
            kind = ActionOutcomeKind.DENIED
            attempted = False
            source_object = denial
        elif source is _OutcomeSource.CONTINUATION:
            if (
                origin_request is not None
                or receipt is not None
                or denial is not None
                or lineage is None
            ):
                raise ContractViolation("action_outcome_source_invalid")
            inspection, plan, inspected_receipt, kind, attempted = (
                authority._inspect_continuation_source(
                    current_planned_action=current_planned_action,
                    lineage=lineage,
                )
            )
            origin_request = inspection.origin_request
            receipt = inspected_receipt
            capability = origin_request.capability
            operation = origin_request.operation
            source_object = lineage
        else:
            raise ContractViolation("action_outcome_source_invalid")

        outcome = _new_outcome(
            kind=kind,
            operation=_project_operation(operation),
            attempted=attempted,
            has_output=False,
            issuer_seal=_OUTCOME_ISSUER_SEAL,
        )
        record = _OutcomeRecord(
            outcome=outcome,
            source=source,
            source_object=source_object,
            current_planned_action=plan,
            capability=capability,
            operation=operation,
            origin_request=origin_request,
            receipt=receipt,
            denial=denial,
            lineage=lineage,
            outcome_snapshot=_outcome_snapshot(outcome),
            consumed=False,
            consumer_kind=_OutcomeConsumer.NONE,
            composer_consumer=None,
            composer_snapshot=None,
        )
        with registry_lock:
            _controller, _plan_authority, limit, _registration, state = (
                registration_for_locked(authority)
            )
            fingerprint = source_fingerprint(record.source, record.source_object)
            if fingerprint in retired_sources_for_locked(authority):
                raise ContractViolation("action_outcome_source_replayed")
            if any(
                item.source is record.source
                and item.source_object is record.source_object
                for item in state
            ):
                raise ContractViolation("action_outcome_source_replayed")
            if any(item.outcome is record.outcome for item in state):
                raise ContractViolation("action_outcome_authority_state_corrupt")
            if len(state) >= limit:
                raise ContractViolation("action_outcome_ledger_full")
            new_state = state + (record,)
            _validate_persistent_state(new_state)
            if len(new_state) > limit:
                raise ContractViolation("action_outcome_authority_state_corrupt")
            register_presentation_locked(
                authority,
                outcome,
                record.outcome_snapshot,
            )
            authority_states[authority] = new_state
        return outcome

    def inspect_outcome(
        authority: object,
        outcome: ActionOutcomeIntent,
        *,
        current_planned_action: PlannedAction,
    ) -> ActionOutcomeIntent:
        if type(authority) is not ActionOutcomeAuthority:
            raise ContractViolation("action_outcome_authority_not_canonical")
        authority._require_runtime_registration()
        with registry_lock:
            _controller, _plan_authority, _limit, _registration, state = (
                registration_for_locked(authority)
            )
            record = find_active_record_locked(authority, state, outcome)
        authority._reinspect_record(record, current_planned_action)
        with registry_lock:
            _controller, _plan_authority, _limit, _registration, state = (
                registration_for_locked(authority)
            )
            current = find_active_record_locked(authority, state, outcome)
            if (
                current.outcome is not record.outcome
                or current.source is not record.source
                or current.source_object is not record.source_object
                or current.current_planned_action
                is not record.current_planned_action
                or current.capability is not record.capability
                or current.operation is not record.operation
                or current.origin_request is not record.origin_request
                or current.receipt is not record.receipt
                or current.denial is not record.denial
                or current.lineage is not record.lineage
                or current.outcome_snapshot != record.outcome_snapshot
                or current.consumed is not record.consumed
                or current.consumer_kind is not record.consumer_kind
                or current.composer_consumer is not record.composer_consumer
                or current.composer_snapshot != record.composer_snapshot
            ):
                raise ContractViolation("action_outcome_authority_state_corrupt")
            return current.outcome

    def consume_outcome(
        authority: object,
        outcome: ActionOutcomeIntent,
        *,
        current_planned_action: PlannedAction,
    ) -> ActionOutcomeIntent:
        if type(authority) is not ActionOutcomeAuthority:
            raise ContractViolation("action_outcome_authority_not_canonical")
        authority._require_runtime_registration()
        with registry_lock:
            _controller, _plan_authority, _limit, _registration, state = (
                registration_for_locked(authority)
            )
            record = find_active_record_locked(authority, state, outcome)
            if record.consumed:
                raise ContractViolation("action_outcome_replayed")
        authority._reinspect_record(record, current_planned_action)
        with registry_lock:
            _controller, _plan_authority, limit, _registration, state = (
                registration_for_locked(authority)
            )
            current = find_active_record_locked(authority, state, outcome)
            if current.consumed:
                raise ContractViolation("action_outcome_replayed")
            if current is not record:
                raise ContractViolation("action_outcome_authority_state_corrupt")
            consumed_record = _OutcomeRecord(
                outcome=current.outcome,
                source=current.source,
                source_object=current.source_object,
                current_planned_action=current.current_planned_action,
                capability=current.capability,
                operation=current.operation,
                origin_request=current.origin_request,
                receipt=current.receipt,
                denial=current.denial,
                lineage=current.lineage,
                outcome_snapshot=current.outcome_snapshot,
                consumed=True,
                consumer_kind=_OutcomeConsumer.GENERIC,
                composer_consumer=None,
                composer_snapshot=None,
            )
            new_state = tuple(
                consumed_record if item is current else item for item in state
            )
            _validate_persistent_state(new_state)
            if len(new_state) > limit:
                raise ContractViolation("action_outcome_authority_state_corrupt")
            authority_states[authority] = new_state
            return current.outcome

    def claim_for_composer(
        authority: object,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
        *,
        consumer: object,
    ) -> ActionOutcomeIntent:
        """Atomically consume one outcome into one deterministic exact request."""

        if type(authority) is not ActionOutcomeAuthority:
            raise ContractViolation("action_outcome_authority_not_canonical")
        from .reply_composer import (
            ReplyComposerRequest,
            _inspect_action_outcome_reply_composer_request,
        )

        if type(consumer) is not ReplyComposerRequest:
            raise ContractViolation("action_outcome_composer_consumer_required")
        authority._require_runtime_registration()
        with registry_lock:
            _controller, _plan_authority, _limit, _registration, state = (
                registration_for_locked(authority)
            )
            record = find_active_record_locked(authority, state, outcome)
            if record.consumed:
                raise ContractViolation("action_outcome_replayed")
        authority._reinspect_record(record, current_planned_action)
        try:
            snapshot = _inspect_action_outcome_reply_composer_request(
                consumer,
                outcome=outcome,
                authority=authority,
                current_planned_action=current_planned_action,
            )
        except Exception as exc:
            raise ContractViolation(
                "action_outcome_composer_consumer_invalid"
            ) from exc
        if type(snapshot) is not tuple:
            raise ContractViolation("action_outcome_composer_consumer_invalid")

        with registry_lock:
            _controller, _plan_authority, limit, _registration, state = (
                registration_for_locked(authority)
            )
            current = find_active_record_locked(authority, state, outcome)
            if current.consumed:
                raise ContractViolation("action_outcome_replayed")
            if current is not record:
                raise ContractViolation("action_outcome_authority_state_corrupt")
            try:
                if _inspect_action_outcome_reply_composer_request(
                    consumer,
                    outcome=outcome,
                    authority=authority,
                    current_planned_action=current_planned_action,
                ) != snapshot:
                    raise ContractViolation(
                        "action_outcome_composer_consumer_corrupt"
                    )
            except ContractViolation:
                raise
            except Exception as exc:
                raise ContractViolation(
                    "action_outcome_composer_consumer_corrupt"
                ) from exc
            consumed_record = _OutcomeRecord(
                outcome=current.outcome,
                source=current.source,
                source_object=current.source_object,
                current_planned_action=current.current_planned_action,
                capability=current.capability,
                operation=current.operation,
                origin_request=current.origin_request,
                receipt=current.receipt,
                denial=current.denial,
                lineage=current.lineage,
                outcome_snapshot=current.outcome_snapshot,
                consumed=True,
                consumer_kind=_OutcomeConsumer.REPLY_COMPOSER,
                composer_consumer=consumer,
                composer_snapshot=snapshot,
            )
            new_state = tuple(
                consumed_record if item is current else item for item in state
            )
            _validate_persistent_state(new_state)
            if len(new_state) > limit:
                raise ContractViolation("action_outcome_authority_state_corrupt")
            authority_states[authority] = new_state
            return current.outcome

    def inspect_for_composer(
        authority: object,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
        *,
        consumer: object,
    ) -> ActionOutcomeIntent:
        """Inspect the one claimed request without consuming or changing state."""

        if type(authority) is not ActionOutcomeAuthority:
            raise ContractViolation("action_outcome_authority_not_canonical")
        from .reply_composer import (
            ReplyComposerRequest,
            _inspect_action_outcome_reply_composer_request,
        )

        if type(consumer) is not ReplyComposerRequest:
            raise ContractViolation("action_outcome_composer_consumer_required")
        authority._require_runtime_registration()
        with registry_lock:
            _controller, _plan_authority, _limit, _registration, state = (
                registration_for_locked(authority)
            )
            record = find_active_record_locked(authority, state, outcome)
            if (
                not record.consumed
                or record.consumer_kind is not _OutcomeConsumer.REPLY_COMPOSER
            ):
                raise ContractViolation("action_outcome_composer_not_claimed")
            if record.composer_consumer is not consumer:
                raise ContractViolation("action_outcome_composer_consumer_not_exact")
            snapshot = record.composer_snapshot
        authority._reinspect_record(record, current_planned_action)
        try:
            inspected_snapshot = _inspect_action_outcome_reply_composer_request(
                consumer,
                outcome=outcome,
                authority=authority,
                current_planned_action=current_planned_action,
            )
        except Exception as exc:
            raise ContractViolation(
                "action_outcome_composer_consumer_corrupt"
            ) from exc
        if inspected_snapshot != snapshot:
            raise ContractViolation("action_outcome_composer_consumer_corrupt")
        with registry_lock:
            _controller, _plan_authority, _limit, _registration, state = (
                registration_for_locked(authority)
            )
            current = find_active_record_locked(authority, state, outcome)
            if (
                current is not record
                or current.composer_consumer is not consumer
                or current.composer_snapshot != snapshot
                or current.consumer_kind is not _OutcomeConsumer.REPLY_COMPOSER
                or not current.consumed
            ):
                raise ContractViolation("action_outcome_authority_state_corrupt")
            return current.outcome

    def reclaim_outcome(
        authority: object,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
        *,
        mode: _OutcomeReclaimMode,
        consumer: object | None,
    ) -> ActionOutcomeIntent:
        """Atomically retire one exact outcome after a closed lifecycle event."""

        if type(authority) is not ActionOutcomeAuthority:
            raise ContractViolation("action_outcome_authority_not_canonical")
        if type(mode) is not _OutcomeReclaimMode:
            raise ContractViolation("action_outcome_reclaim_mode_invalid")
        authority._require_runtime_registration()

        composer_snapshot: tuple[object, ...] | None = None
        composer_inspector = None
        if mode is _OutcomeReclaimMode.DELIVERY_TERMINAL:
            from .reply_composer import (
                ReplyComposerRequest,
                _inspect_action_outcome_reply_composer_request,
            )

            if type(consumer) is not ReplyComposerRequest:
                raise ContractViolation("action_outcome_composer_consumer_required")
            composer_inspector = _inspect_action_outcome_reply_composer_request
        elif consumer is not None:
            raise ContractViolation("action_outcome_composer_consumer_forbidden")

        with registry_lock:
            _controller, _plan_authority, _limit, _registration, state = (
                registration_for_locked(authority)
            )
            record = find_active_record_locked(authority, state, outcome)
            if mode is _OutcomeReclaimMode.ABANDONED_BEFORE_COMPOSER:
                if (
                    record.consumed
                    or record.consumer_kind is not _OutcomeConsumer.NONE
                    or record.composer_consumer is not None
                    or record.composer_snapshot is not None
                ):
                    raise ContractViolation("action_outcome_already_claimed")
            else:
                if (
                    not record.consumed
                    or record.consumer_kind is not _OutcomeConsumer.REPLY_COMPOSER
                ):
                    raise ContractViolation("action_outcome_composer_not_claimed")
                if record.composer_consumer is not consumer:
                    raise ContractViolation(
                        "action_outcome_composer_consumer_not_exact"
                    )
                composer_snapshot = record.composer_snapshot

        authority._reinspect_record(record, current_planned_action)
        if composer_inspector is not None:
            try:
                inspected_snapshot = composer_inspector(
                    consumer,
                    outcome=outcome,
                    authority=authority,
                    current_planned_action=current_planned_action,
                )
            except Exception as exc:
                raise ContractViolation(
                    "action_outcome_composer_consumer_corrupt"
                ) from exc
            if inspected_snapshot != composer_snapshot:
                raise ContractViolation("action_outcome_composer_consumer_corrupt")

        with registry_lock:
            _controller, _plan_authority, limit, _registration, state = (
                registration_for_locked(authority)
            )
            current = find_active_record_locked(authority, state, outcome)
            if current is not record:
                raise ContractViolation("action_outcome_authority_state_corrupt")
            if mode is _OutcomeReclaimMode.ABANDONED_BEFORE_COMPOSER:
                if (
                    current.consumed
                    or current.consumer_kind is not _OutcomeConsumer.NONE
                    or current.composer_consumer is not None
                    or current.composer_snapshot is not None
                ):
                    raise ContractViolation("action_outcome_already_claimed")
            else:
                if (
                    not current.consumed
                    or current.consumer_kind is not _OutcomeConsumer.REPLY_COMPOSER
                    or current.composer_consumer is not consumer
                    or current.composer_snapshot != composer_snapshot
                ):
                    raise ContractViolation(
                        "action_outcome_composer_consumer_not_exact"
                    )
                try:
                    if composer_inspector(
                        consumer,
                        outcome=outcome,
                        authority=authority,
                        current_planned_action=current_planned_action,
                    ) != composer_snapshot:
                        raise ContractViolation(
                            "action_outcome_composer_consumer_corrupt"
                        )
                except ContractViolation:
                    raise
                except Exception as exc:
                    raise ContractViolation(
                        "action_outcome_composer_consumer_corrupt"
                    ) from exc

            fingerprint = source_fingerprint(
                current.source,
                current.source_object,
            )
            retired = retired_sources_for_locked(authority)
            if fingerprint in retired or outcome in outcome_reclaims:
                raise ContractViolation("action_outcome_reclaim_state_corrupt")
            new_state = tuple(item for item in state if item is not current)
            _validate_persistent_state(new_state)
            if len(new_state) >= len(state) or len(new_state) > limit:
                raise ContractViolation("action_outcome_authority_state_corrupt")
            reclaim_record = _OutcomeReclaimRecord(
                authority_ref=weakref.ref(authority),
                source=current.source,
                source_fingerprint=fingerprint,
                outcome_snapshot=current.outcome_snapshot,
                mode=mode,
            )
            authority_states[authority] = new_state
            authority_retired_sources[authority] = retired | {fingerprint}
            outcome_reclaims[outcome] = reclaim_record
            return outcome

    def abandon_before_composer(
        authority: object,
        outcome: ActionOutcomeIntent,
        *,
        current_planned_action: PlannedAction,
    ) -> ActionOutcomeIntent:
        return reclaim_outcome(
            authority,
            outcome,
            current_planned_action,
            mode=_OutcomeReclaimMode.ABANDONED_BEFORE_COMPOSER,
            consumer=None,
        )

    def acknowledge_delivery_terminal(
        authority: object,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
        *,
        consumer: object,
    ) -> ActionOutcomeIntent:
        return reclaim_outcome(
            authority,
            outcome,
            current_planned_action,
            mode=_OutcomeReclaimMode.DELIVERY_TERMINAL,
            consumer=consumer,
        )

    def inspect_reclaimed_source(
        authority: object,
        outcome: ActionOutcomeIntent,
        source_object: object,
    ) -> str:
        """Return only the closed reclaim mode for Controller release gating."""

        if type(authority) is not ActionOutcomeAuthority:
            raise ContractViolation("action_outcome_authority_not_canonical")
        if type(outcome) is not ActionOutcomeIntent:
            raise ContractViolation("action_outcome_required")
        authority._require_runtime_registration()
        with registry_lock:
            registration_for_locked(authority)
            reclaimed = outcome_reclaims.get(outcome)
            if reclaimed is None or reclaimed.authority_ref() is not authority:
                raise ContractViolation("action_outcome_not_reclaimed")
            if (
                type(reclaimed) is not _OutcomeReclaimRecord
                or type(reclaimed.outcome_snapshot) is not tuple
                or reclaimed.outcome_snapshot != _outcome_snapshot(outcome)
                or type(reclaimed.mode) is not _OutcomeReclaimMode
                or source_fingerprint(reclaimed.source, source_object)
                != reclaimed.source_fingerprint
            ):
                raise ContractViolation("action_outcome_reclaim_state_corrupt")
            return reclaimed.mode.value

    def finalize_reclaimed_outcome(
        authority: object,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
        *,
        ticket: object,
        durable_authority: object,
    ) -> None:
        """Delete minimal reclaim state only under the exact durable ticket."""

        from .owner_action_durable_finalize import (
            DurableFinalizeTicket,
            OwnerActionDurableFinalizeAuthority,
            _claim_outcome_finalize_ticket,
            _inspect_outcome_finalize_ticket,
        )

        if type(authority) is not ActionOutcomeAuthority:
            raise ContractViolation("action_outcome_authority_not_canonical")
        if type(outcome) is not ActionOutcomeIntent:
            raise ContractViolation("action_outcome_required")
        if type(current_planned_action) is not PlannedAction:
            raise ContractViolation("action_outcome_current_plan_required")
        if type(ticket) is not DurableFinalizeTicket:
            raise ContractViolation("durable_finalize_ticket_required")
        if type(durable_authority) is not OwnerActionDurableFinalizeAuthority:
            raise ContractViolation("durable_finalize_authority_not_canonical")
        _inspect_outcome_finalize_ticket(
            durable_authority,
            ticket,
            outcome_authority=authority,
            outcome=outcome,
            current_planned_action=current_planned_action,
        )
        authority._require_runtime_registration()
        with registry_lock:
            registration_for_locked(authority)
            reclaimed = outcome_reclaims.get(outcome)
            if (
                type(reclaimed) is not _OutcomeReclaimRecord
                or reclaimed.authority_ref() is not authority
                or reclaimed.outcome_snapshot != _outcome_snapshot(outcome)
            ):
                raise ContractViolation("action_outcome_reclaim_state_corrupt")
            retired = retired_sources_for_locked(authority)
            if reclaimed.source_fingerprint not in retired:
                raise ContractViolation("action_outcome_reclaim_state_corrupt")
            try:
                del outcome_reclaims[outcome]
                authority_retired_sources[authority] = retired - {
                    reclaimed.source_fingerprint
                }
                _claim_outcome_finalize_ticket(
                    durable_authority,
                    ticket,
                    outcome_authority=authority,
                    outcome=outcome,
                    current_planned_action=current_planned_action,
                )
            except BaseException:
                outcome_reclaims[outcome] = reclaimed
                authority_retired_sources[authority] = retired
                raise
        durable_authority.complete(ticket)

    def metrics(authority: object) -> tuple[int, int, int, bool]:
        if type(authority) is not ActionOutcomeAuthority:
            raise ContractViolation("action_outcome_authority_not_canonical")
        authority._require_runtime_registration()
        with registry_lock:
            _controller, _plan_authority, limit, _registration, state = (
                registration_for_locked(authority)
            )
            return (
                len(state),
                sum(record.consumed for record in state),
                len(retired_sources_for_locked(authority)),
                len(state) <= limit,
            )

    def counts() -> tuple[int, int]:
        with registry_lock:
            return (len(runtime_registrations), len(authority_states))

    return (
        issue_runtime,
        inspect,
        issue_outcome,
        inspect_outcome,
        consume_outcome,
        claim_for_composer,
        inspect_for_composer,
        abandon_before_composer,
        acknowledge_delivery_terminal,
        inspect_reclaimed_source,
        finalize_reclaimed_outcome,
        inspect_presentation,
        metrics,
        counts,
    )


(
    _issue_runtime_authority,
    _inspect_runtime_authority,
    _issue_persistent_outcome,
    _inspect_persistent_outcome,
    _consume_persistent_outcome,
    _claim_persistent_outcome_for_composer,
    _inspect_persistent_outcome_for_composer,
    _abandon_persistent_outcome_before_composer,
    _acknowledge_persistent_outcome_delivery_terminal,
    _inspect_reclaimed_outcome_source,
    _finalize_persistent_reclaimed_outcome,
    _inspect_action_outcome_presentation,
    _persistent_state_metrics,
    _runtime_registry_counts,
) = _build_runtime_registry()
del _build_runtime_registry


class ActionOutcomeAuthority:
    """Controller-scoped exact issuer and one-shot outcome consumer."""

    __slots__ = (
        "__weakref__",
        "_authority_seal",
        "_controller",
        "_max_outcomes",
        "_planned_action_authority",
    )

    def __init__(
        self,
        controller: OwnerActionController,
        planned_action_authority: PlannedActionAuthority,
        *,
        max_outcomes: int = _DEFAULT_MAX_OUTCOMES,
    ) -> None:
        del controller, planned_action_authority, max_outcomes
        raise ContractViolation("action_outcome_authority_constructor_forbidden")

    @classmethod
    def issue_for_runtime(
        cls,
        controller: OwnerActionController,
        planned_action_authority: PlannedActionAuthority,
        *,
        max_outcomes: int = _DEFAULT_MAX_OUTCOMES,
    ) -> "ActionOutcomeAuthority":
        """Issue the one canonical authority for one exact Controller graph."""

        if cls is not ActionOutcomeAuthority:
            raise ContractViolation("action_outcome_authority_subclass_forbidden")
        return _issue_runtime_authority(
            controller,
            planned_action_authority,
            max_outcomes=max_outcomes,
        )

    def _require_runtime_registration(self) -> None:
        controller = getattr(self, "_controller", None)
        planned_action_authority = getattr(
            self,
            "_planned_action_authority",
            None,
        )
        if (
            type(controller) is not OwnerActionController
            or type(planned_action_authority) is not PlannedActionAuthority
            or getattr(self, "_authority_seal", None)
            is not _ACTION_OUTCOME_AUTHORITY_INSTANCE_SEAL
        ):
            raise ContractViolation("action_outcome_authority_not_canonical")
        controller._inspect_action_outcome_authority(
            self,
            planned_action_authority,
        )

    def __copy__(self):
        raise ContractViolation("action_outcome_authority_copy_forbidden")

    def __deepcopy__(self, memo):
        del memo
        raise ContractViolation("action_outcome_authority_copy_forbidden")

    def __reduce_ex__(self, protocol):
        del protocol
        raise ContractViolation("action_outcome_authority_pickle_forbidden")

    def _inspect_direct_receipt(
        self,
        *,
        current_planned_action: PlannedAction,
        origin_request: OwnerActionRequest,
        receipt: ActionReceipt,
    ) -> tuple[PlannedAction, CapabilityClass, OwnerActionOperation, ActionOutcomeKind, bool]:
        plan = _require_execute_plan(
            self._planned_action_authority,
            current_planned_action,
        )
        request_lineage = self._controller.inspect_request_lineage(origin_request)
        receipt_lineage = self._controller.inspect_receipt_lineage(receipt)
        if type(request_lineage) is not OwnerActionRequestLineageInspection:
            raise ContractViolation("action_outcome_request_inspection_invalid")
        if type(receipt_lineage) is not OwnerActionReceiptInspection:
            raise ContractViolation("action_outcome_receipt_inspection_invalid")
        inspected_receipt = receipt_lineage.receipt
        if (
            request_lineage.request is not origin_request
            or receipt_lineage.request is not origin_request
        ):
            raise ContractViolation("action_outcome_request_not_exact")
        if self._controller.canonical_receipt_for(origin_request) is not inspected_receipt:
            raise ContractViolation("action_outcome_receipt_not_current")
        # A second-turn terminal receipt is bound to the follow-up plan, not the
        # origin plan.  It must enter through the Controller's continuation
        # inspection so neither plan can be substituted.
        if inspected_receipt.binding is not origin_request.binding:
            raise ContractViolation("action_outcome_continuation_required")
        if (
            request_lineage.planned_action is not plan
            or receipt_lineage.planned_action is not plan
            or request_lineage.action_route is not receipt_lineage.action_route
            or origin_request.binding is not plan.binding
            or origin_request.action_id != plan.action_id
            or origin_request.capability.value != plan.action.capability_intent
            or origin_request.operation is not plan.action.operation_intent
        ):
            raise ContractViolation("action_outcome_origin_plan_mismatch")
        if (
            inspected_receipt.request_digest != origin_request.request_digest
            or inspected_receipt.action_id != origin_request.action_id
            or inspected_receipt.adapter_id != origin_request.adapter_id
            or inspected_receipt.adapter_version != origin_request.adapter_version
            or inspected_receipt.capability is not origin_request.capability
            or inspected_receipt.operation is not origin_request.operation
            or inspected_receipt.side_effect is not origin_request.side_effect
            or inspected_receipt.confirmation_policy
            is not origin_request.confirmation_policy
            or inspected_receipt.idempotency_key != origin_request.idempotency_key
            or inspected_receipt.origin_binding is not origin_request.binding
        ):
            raise ContractViolation("action_outcome_receipt_lineage_mismatch")
        kind, attempted, _ = _map_receipt(inspected_receipt)
        return (
            plan,
            origin_request.capability,
            origin_request.operation,
            kind,
            attempted,
        )

    def issue_from_receipt(
        self,
        *,
        current_planned_action: PlannedAction,
        origin_request: OwnerActionRequest,
        receipt: ActionReceipt,
    ) -> ActionOutcomeIntent:
        """Issue from a direct origin-turn receipt; continuations are forbidden."""

        self._require_runtime_registration()
        return _issue_persistent_outcome(
            self,
            source=_OutcomeSource.RECEIPT,
            current_planned_action=current_planned_action,
            origin_request=origin_request,
            receipt=receipt,
        )

    def _inspect_denial_source(
        self,
        *,
        current_planned_action: PlannedAction,
        denial: OwnerActionDenial,
    ) -> tuple[PlannedAction, CapabilityClass, OwnerActionOperation]:
        plan = _require_execute_plan(
            self._planned_action_authority,
            current_planned_action,
        )
        inspected = self._controller.inspect_denial_lineage(denial)
        if type(inspected) is not OwnerActionDenialInspection:
            raise ContractViolation("action_outcome_denial_inspection_invalid")
        if inspected.planned_action is not plan:
            raise ContractViolation("action_outcome_origin_plan_mismatch")
        operation = inspected.operation
        capability = inspected.capability
        if (
            inspected.denial is not denial
            or inspected.action_route.binding is not plan.binding
            or inspected.action_route.action_id != plan.action_id
            or inspected.action_route.capability is not capability
            or inspected.action_route.operation is not operation
            or denial.binding is not plan.binding
            or denial.action_id != plan.action_id
            or denial.status is not ActionReceiptStatus.DENIED
            or denial.effect_state is not ActionEffectState.NOT_STARTED
            or plan.action.capability_intent != capability.value
            or plan.action.operation_intent is not operation
        ):
            raise ContractViolation("action_outcome_denial_lineage_mismatch")
        return plan, capability, operation

    def issue_from_denial(
        self,
        *,
        current_planned_action: PlannedAction,
        denial: OwnerActionDenial,
    ) -> ActionOutcomeIntent:
        """Issue from one exact Controller denial before a request exists."""

        self._require_runtime_registration()
        return _issue_persistent_outcome(
            self,
            source=_OutcomeSource.DENIAL,
            current_planned_action=current_planned_action,
            denial=denial,
        )

    def _inspect_continuation_source(
        self,
        *,
        current_planned_action: PlannedAction,
        lineage: OwnerActionContinuationLineage,
    ) -> tuple[
        OwnerActionContinuationInspection,
        PlannedAction,
        ActionReceipt,
        ActionOutcomeKind,
        bool,
    ]:
        inspection = self._controller.inspect_continuation_lineage(lineage)
        if type(inspection) is not OwnerActionContinuationInspection:
            raise ContractViolation("action_outcome_continuation_inspection_invalid")
        plan = self._planned_action_authority.inspect_plan(current_planned_action)
        if inspection.current_planned_action is not plan:
            raise ContractViolation("action_outcome_plan_mismatch")
        request_inspection = self._controller.inspect_request_lineage(
            inspection.origin_request
        )
        if (
            type(request_inspection) is not OwnerActionRequestLineageInspection
            or request_inspection.request is not inspection.origin_request
        ):
            raise ContractViolation("action_outcome_request_not_exact")
        receipt = inspection.receipt
        if receipt is None:
            raise ContractViolation("action_outcome_continuation_receipt_required")
        receipt_inspection = self._controller.inspect_receipt_lineage(receipt)
        if (
            type(receipt_inspection) is not OwnerActionReceiptInspection
            or receipt_inspection.receipt is not receipt
            or receipt_inspection.request is not inspection.origin_request
            or receipt_inspection.planned_action
            is not request_inspection.planned_action
            or receipt_inspection.action_route is not request_inspection.action_route
        ):
            raise ContractViolation("action_outcome_receipt_not_exact")
        if self._controller.canonical_receipt_for(inspection.origin_request) is not receipt:
            raise ContractViolation("action_outcome_receipt_not_current")

        request = inspection.origin_request
        if (
            receipt.request_digest != request.request_digest
            or receipt.action_id != request.action_id
            or receipt.origin_binding is not request.binding
            or receipt.binding is not plan.binding
            or receipt.capability is not request.capability
            or receipt.operation is not request.operation
            or receipt.side_effect is not request.side_effect
            or lineage.capability is not request.capability
            or lineage.operation is not request.operation
            or inspection.resolution_route.capability is not request.capability
            or inspection.resolution_route.operation is not request.operation
            or inspection.resolution_route.decision is not lineage.decision
        ):
            raise ContractViolation("action_outcome_continuation_lineage_mismatch")

        if lineage.decision is PendingDecision.CONFIRM:
            _require_execute_plan(self._planned_action_authority, plan)
            if (
                plan.action.capability_intent != request.capability.value
                or plan.action.operation_intent is not request.operation
                or receipt.status
                in {
                    ActionReceiptStatus.CONFIRMATION_REQUIRED,
                    ActionReceiptStatus.DENIED,
                    ActionReceiptStatus.CANCELLED,
                }
            ):
                raise ContractViolation("action_outcome_confirm_lineage_invalid")
        elif lineage.decision in {PendingDecision.DENY, PendingDecision.CANCEL}:
            _require_reply_plan(self._planned_action_authority, plan)
            expected_status = (
                ActionReceiptStatus.DENIED
                if lineage.decision is PendingDecision.DENY
                else ActionReceiptStatus.CANCELLED
            )
            if receipt.status is not expected_status:
                raise ContractViolation("action_outcome_resolution_status_mismatch")
        else:  # pragma: no cover - exact PendingDecision is closed
            raise ContractViolation("action_outcome_resolution_decision_invalid")
        kind, attempted, _ = _map_receipt(receipt)
        return inspection, plan, receipt, kind, attempted

    def issue_from_continuation(
        self,
        *,
        current_planned_action: PlannedAction,
        lineage: OwnerActionContinuationLineage,
    ) -> ActionOutcomeIntent:
        """Issue from one exact current-plan/origin-receipt continuation."""

        self._require_runtime_registration()
        return _issue_persistent_outcome(
            self,
            source=_OutcomeSource.CONTINUATION,
            current_planned_action=current_planned_action,
            lineage=lineage,
        )

    def _reinspect_record(
        self,
        record: _OutcomeRecord,
        current_planned_action: PlannedAction,
    ) -> None:
        plan = self._planned_action_authority.inspect_plan(current_planned_action)
        if record.current_planned_action is not plan:
            raise ContractViolation("action_outcome_plan_mismatch")
        if record.source is _OutcomeSource.RECEIPT:
            assert record.origin_request is not None and record.receipt is not None
            checked = self._inspect_direct_receipt(
                current_planned_action=plan,
                origin_request=record.origin_request,
                receipt=record.receipt,
            )
            _, capability, operation, kind, attempted = checked
        elif record.source is _OutcomeSource.DENIAL:
            assert record.denial is not None
            _, capability, operation = self._inspect_denial_source(
                current_planned_action=plan,
                denial=record.denial,
            )
            kind = ActionOutcomeKind.DENIED
            attempted = False
        elif record.source is _OutcomeSource.CONTINUATION:
            assert record.lineage is not None
            inspection, _, receipt, kind, attempted = (
                self._inspect_continuation_source(
                    current_planned_action=plan,
                    lineage=record.lineage,
                )
            )
            capability = inspection.origin_request.capability
            operation = inspection.origin_request.operation
            if record.receipt is not receipt:
                raise ContractViolation("action_outcome_receipt_identity_mismatch")
        else:  # pragma: no cover - internal source Enum is closed
            raise ContractViolation("action_outcome_source_invalid")
        if (
            capability is not record.capability
            or operation is not record.operation
            or kind is not record.outcome.kind
            or _project_operation(operation) is not record.outcome.operation
            or attempted is not record.outcome.attempted
            or record.outcome.has_output
        ):
            raise ContractViolation("action_outcome_lineage_corrupt")

    def inspect_outcome(
        self,
        outcome: ActionOutcomeIntent,
        *,
        current_planned_action: PlannedAction,
    ) -> ActionOutcomeIntent:
        """Non-consuming exact inspection for later typed builders/guards."""

        self._require_runtime_registration()
        return _inspect_persistent_outcome(
            self,
            outcome,
            current_planned_action=current_planned_action,
        )

    def consume_outcome(
        self,
        outcome: ActionOutcomeIntent,
        *,
        current_planned_action: PlannedAction,
    ) -> ActionOutcomeIntent:
        """Consume the exact outcome once; copies and replay fail closed."""

        self._require_runtime_registration()
        return _consume_persistent_outcome(
            self,
            outcome,
            current_planned_action=current_planned_action,
        )

    def claim_for_composer(
        self,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
        *,
        consumer: object,
    ) -> ActionOutcomeIntent:
        """Consume into one exact, fully built ReplyComposerRequest."""

        self._require_runtime_registration()
        return _claim_persistent_outcome_for_composer(
            self,
            outcome,
            current_planned_action,
            consumer=consumer,
        )

    def inspect_for_composer(
        self,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
        *,
        consumer: object,
    ) -> ActionOutcomeIntent:
        """Validate the exact claimed request without consuming it again."""

        self._require_runtime_registration()
        return _inspect_persistent_outcome_for_composer(
            self,
            outcome,
            current_planned_action,
            consumer=consumer,
        )

    def abandon_before_composer(
        self,
        outcome: ActionOutcomeIntent,
        *,
        current_planned_action: PlannedAction,
    ) -> ActionOutcomeIntent:
        """Retire an exact outcome only when Composer never claimed it."""

        self._require_runtime_registration()
        return _abandon_persistent_outcome_before_composer(
            self,
            outcome,
            current_planned_action=current_planned_action,
        )

    def acknowledge_delivery_terminal(
        self,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
        *,
        consumer: object,
    ) -> ActionOutcomeIntent:
        """Retire one exact Composer claim after an explicit delivery terminal."""

        self._require_runtime_registration()
        return _acknowledge_persistent_outcome_delivery_terminal(
            self,
            outcome,
            current_planned_action,
            consumer=consumer,
        )

    def _inspect_reclaimed_source(
        self,
        outcome: ActionOutcomeIntent,
        source_object: object,
    ) -> str:
        """Controller friend seam; returns no source, request, or ledger record."""

        self._require_runtime_registration()
        return _inspect_reclaimed_outcome_source(
            self,
            outcome,
            source_object,
        )

    def finalize_reclaimed_outcome(
        self,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
        *,
        ticket: object,
        durable_authority: object,
    ) -> None:
        """Consume the exact OUTCOME_RETIRE ticket and delete reclaim state."""

        self._require_runtime_registration()
        _finalize_persistent_reclaimed_outcome(
            self,
            outcome,
            current_planned_action,
            ticket=ticket,
            durable_authority=durable_authority,
        )

    def trace_metadata(self) -> dict[str, int | bool]:
        self._require_runtime_registration()
        outcome_count, consumed_count, reclaimed_count, ledger_bounded = (
            _persistent_state_metrics(self)
        )
        return {
            "schema_version": 1,
            "outcome_count": outcome_count,
            "consumed_count": consumed_count,
            "reclaimed_count": reclaimed_count,
            "ledger_bounded": ledger_bounded,
            "output_authority_connected": False,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "ActionOutcomeAuthority("
            f"outcome_count={metadata['outcome_count']}, "
            f"consumed_count={metadata['consumed_count']}, "
            f"reclaimed_count={metadata['reclaimed_count']}, "
            "output_authority_connected=False)"
        )


__all__ = [
    "ActionOutcomeAuthority",
    "ActionOutcomeIntent",
    "ActionOutcomeKind",
    "ActionOutcomeOperation",
    "ActionOutcomeProposition",
    "ActionOutcomeSemantics",
    "action_outcome_semantics",
    "action_outcome_semantics_for",
    "expand_action_outcome_propositions",
    "expand_action_outcome_propositions_for",
]
