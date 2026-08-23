from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .accepted_turn_authority import AcceptedTurnTicket
from .capability_policy import CapabilityClass, CapabilityPolicy
from .context_assembler import ReplyTarget
from .contracts import (
    ActionDecision,
    ActionKind,
    AddressDecision,
    AddressKind,
    AttentionDecision,
    AttentionLevel,
    ContractViolation,
    DecisionBinding,
    IngressDecision,
    KnowledgeGapDecision,
    KnowledgeNeed,
    ModelActionSuggestion,
    OwnerActionOperation,
    ParticipationDecision,
    ParticipationLevel,
    parse_model_action_suggestion,
)
from .contracts._validation import (
    normalize_reason_codes,
    require_safe_name,
)
from .owner_action_router import (
    OwnerActionRouteDecision,
    OwnerActionRouteStatus,
    OwnerActionRouter,
)


class StructuralOutcome(str, Enum):
    NO_ACTION = "no_action"
    WAIT = "wait"
    REACT = "react"
    CONTINUE = "continue"
    INITIATE = "initiate"


_FORBIDDEN_MODEL_FIELDS = frozenset(
    {
        "owner",
        "is_owner",
        "sender_id",
        "sender_key",
        "target",
        "reply_target",
        "target_message_id",
        "target_sender_id",
        "binding",
        "scope_key",
        "session_id",
        "message_id",
        "trace_id",
        "generation_epoch",
        "conversation_revision",
        "tool",
        "tool_name",
        "exact_tool",
        "arguments",
        "tool_arguments",
        "args",
        "kwargs",
        "source",
        "source_kind",
        "plugin_source",
        "max_tool_calls",
        "tool_call_budget",
        "call_budget",
        "budget",
        "deadline",
        "capability_policy",
        "authority",
        "operation",
        "operation_intent",
    }
)
_CLOSED_REACTION_INTENTS = frozenset(
    {
        "acknowledge",
        "light_reaction",
        "warm_acknowledge",
        "empathetic_reaction",
        "playful_reaction",
    }
)
_PLANNER_PUBLISH_SEAL = object()
_DEFAULT_MAX_CANONICAL_PLANS = 512


def _target_matches(binding: DecisionBinding, target: ReplyTarget) -> bool:
    return bool(
        isinstance(target, ReplyTarget)
        and target.message_id == binding.current_message_id
        and target.sender_key == binding.current_sender_key
        and target.session_id == binding.session_id
        and target.scope_key == binding.scope_key
        and target.content_digest == binding.current_content_digest
        and target.is_actionable
    )


def _require_structural_bindings(
    *,
    ingress: IngressDecision,
    address: AddressDecision,
    attention: AttentionDecision,
    participation: ParticipationDecision,
    reply_target: ReplyTarget,
) -> DecisionBinding:
    if not isinstance(ingress, IngressDecision):
        raise ContractViolation("planner_ingress_required")
    binding = ingress.binding
    for value, reason in (
        (address, "planner_address_required"),
        (attention, "planner_attention_required"),
        (participation, "planner_participation_required"),
    ):
        if not hasattr(value, "binding"):
            raise ContractViolation(reason)
        if value.binding != binding:
            raise ContractViolation("planner_decision_binding_mismatch")
    if not isinstance(address, AddressDecision):
        raise ContractViolation("planner_address_required")
    if not isinstance(attention, AttentionDecision):
        raise ContractViolation("planner_attention_required")
    if not isinstance(participation, ParticipationDecision):
        raise ContractViolation("planner_participation_required")
    if not _target_matches(binding, reply_target):
        raise ContractViolation("planner_reply_target_binding_mismatch")
    return binding


def _require_reconcile_bindings(
    *,
    ingress: IngressDecision,
    address: AddressDecision,
    attention: AttentionDecision,
    participation: ParticipationDecision,
    reply_target: ReplyTarget,
    knowledge_gap: KnowledgeGapDecision,
    capability_policy: CapabilityPolicy,
) -> DecisionBinding:
    binding = _require_structural_bindings(
        ingress=ingress,
        address=address,
        attention=attention,
        participation=participation,
        reply_target=reply_target,
    )
    if not isinstance(knowledge_gap, KnowledgeGapDecision):
        raise ContractViolation("planner_knowledge_gap_required")
    if knowledge_gap.binding != binding:
        raise ContractViolation("planner_knowledge_binding_mismatch")
    if not isinstance(capability_policy, CapabilityPolicy):
        raise ContractViolation("planner_capability_policy_required")
    if capability_policy.principal_key != binding.current_sender_key:
        raise ContractViolation("planner_capability_principal_mismatch")
    if capability_policy.conversation_mode != "direct_reply":
        raise ContractViolation("planner_capability_mode_mismatch")
    return binding


def _coerce_model_suggestion(
    value: Mapping[str, Any] | ModelActionSuggestion | None,
) -> ModelActionSuggestion | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        keys = {str(key or "").strip() for key in value}
        if keys.intersection(_FORBIDDEN_MODEL_FIELDS):
            raise ContractViolation("planner_model_authority_field_forbidden")
        suggestion = parse_model_action_suggestion(value)
    elif isinstance(value, ModelActionSuggestion):
        suggestion = value
    else:
        raise ContractViolation("planner_model_suggestion_invalid")

    capability = str(suggestion.capability_intent or "").strip()
    if capability:
        try:
            CapabilityClass(capability)
        except ValueError as exc:
            raise ContractViolation("planner_model_exact_tool_forbidden") from exc
    if suggestion.action_hint is ActionKind.EXECUTE_ACTION:
        raise ContractViolation("planner_model_execute_action_forbidden")
    return suggestion


def _reaction_intent(
    suggestion: ModelActionSuggestion | None,
) -> tuple[str, bool]:
    if (
        suggestion is not None
        and suggestion.action_hint is ActionKind.REACT
        and suggestion.confidence >= 0.5
        and suggestion.expression_intent in _CLOSED_REACTION_INTENTS
    ):
        return suggestion.expression_intent, True
    return "light_reaction", False


def _verified_owner_action_policy(policy: CapabilityPolicy) -> bool:
    return bool(
        policy.policy_kind == "owner"
        and policy.is_owner
        and policy.relationship_role == "owner"
        and policy.verification_source
        == "astrbot_event_sender_id:configured_owner_id"
        and policy.agent_full
        and not policy.is_degraded
    )


@dataclass(frozen=True, slots=True)
class StructuralGateDecision:
    binding: DecisionBinding = field(repr=False)
    outcome: StructuralOutcome
    reply_target: ReplyTarget = field(repr=False)
    wait_until: float = 0.0
    expression_intent: str = ""
    model_hint_applied: bool = False
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.binding, DecisionBinding):
            raise ContractViolation("planner_binding_required")
        if not isinstance(self.outcome, StructuralOutcome):
            raise ContractViolation("planner_structural_outcome_invalid")
        if not _target_matches(self.binding, self.reply_target):
            raise ContractViolation("planner_reply_target_binding_mismatch")
        try:
            wait_until = float(self.wait_until)
        except (TypeError, ValueError) as exc:
            raise ContractViolation("planner_wait_until_invalid") from exc
        if not math.isfinite(wait_until) or wait_until < 0:
            raise ContractViolation("planner_wait_until_invalid")
        object.__setattr__(self, "wait_until", wait_until)
        object.__setattr__(self, "reason_codes", normalize_reason_codes(self.reason_codes))
        if type(self.model_hint_applied) is not bool:
            raise ContractViolation("planner_model_hint_flag_invalid")
        if self.outcome is StructuralOutcome.WAIT:
            if wait_until <= 0 or self.expression_intent:
                raise ContractViolation("planner_wait_shape_invalid")
        elif self.outcome is StructuralOutcome.REACT:
            if wait_until or self.expression_intent not in _CLOSED_REACTION_INTENTS:
                raise ContractViolation("planner_react_shape_invalid")
        elif wait_until or self.expression_intent:
            raise ContractViolation("planner_structural_payload_forbidden")

    def trace_metadata(self) -> dict[str, str | int | float | bool]:
        return {
            "structural_outcome": self.outcome.value,
            "structural_waiting": self.outcome is StructuralOutcome.WAIT,
            "structural_reaction": self.outcome is StructuralOutcome.REACT,
            "structural_model_hint_applied": self.model_hint_applied,
            "structural_reason_count": len(self.reason_codes),
        }


def _action_digest(
    action: ActionDecision,
    structural_outcome: StructuralOutcome,
    model_hint_applied: bool,
    planner_reason_codes: tuple[str, ...],
) -> str:
    binding = action.binding
    payload = {
        "binding": {
            "scope_key": binding.scope_key,
            "session_id": binding.session_id,
            "message_id": binding.current_message_id,
            "sender_key": binding.current_sender_key,
            "content_digest": binding.current_content_digest,
            "conversation_revision": binding.conversation_revision,
            "generation_epoch": binding.generation_epoch,
            "trace_id": binding.trace_id,
        },
        "decision": {
            "kind": action.kind.value,
            "has_target": action.reply_target is not None,
            "has_public_scope": bool(action.public_scope_key),
            "capability_intent": action.capability_intent,
            "operation_intent": (
                action.operation_intent.value
                if action.operation_intent is not None
                else ""
            ),
            "expression_intent": action.expression_intent,
            "wait_until": action.wait_until,
            "reason_codes": action.reason_codes,
        },
        "structural_outcome": structural_outcome.value,
        "model_hint_applied": model_hint_applied,
        "planner_reason_codes": planner_reason_codes,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PlannedAction:
    action: ActionDecision = field(repr=False)
    structural_outcome: StructuralOutcome
    model_hint_applied: bool = False
    planner_reason_codes: tuple[str, ...] = ()
    action_id: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.action) is not ActionDecision:
            raise ContractViolation("planned_action_decision_required")
        if type(self.structural_outcome) is not StructuralOutcome:
            raise ContractViolation("planned_action_structural_outcome_invalid")
        if type(self.model_hint_applied) is not bool:
            raise ContractViolation("planned_action_model_hint_invalid")
        reasons = normalize_reason_codes(self.planner_reason_codes)
        object.__setattr__(self, "planner_reason_codes", reasons)
        allowed = {
            StructuralOutcome.NO_ACTION: {ActionKind.NO_ACTION},
            StructuralOutcome.WAIT: {ActionKind.WAIT},
            StructuralOutcome.REACT: {ActionKind.REACT},
            StructuralOutcome.CONTINUE: {
                ActionKind.REPLY,
                ActionKind.USE_TOOL,
                ActionKind.EXECUTE_ACTION,
            },
            StructuralOutcome.INITIATE: {ActionKind.INITIATE},
        }
        if self.action.kind not in allowed[self.structural_outcome]:
            raise ContractViolation("planned_action_structural_mismatch")
        object.__setattr__(
            self,
            "action_id",
            _action_digest(
                self.action,
                self.structural_outcome,
                self.model_hint_applied,
                reasons,
            ),
        )

    @property
    def binding(self) -> DecisionBinding:
        return self.action.binding

    @property
    def kind(self) -> ActionKind:
        return self.action.kind

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "action_id": self.action_id,
            "action_kind": self.action.kind.value,
            "structural_outcome": self.structural_outcome.value,
            "action_has_target": self.action.reply_target is not None,
            "action_has_public_scope": bool(self.action.public_scope_key),
            "action_has_capability_intent": bool(self.action.capability_intent),
            "action_has_operation_intent": bool(self.action.operation_intent),
            "action_waiting": self.action.kind is ActionKind.WAIT,
            "action_model_hint_applied": self.model_hint_applied,
            "action_reason_count": len(self.planner_reason_codes),
        }


@dataclass(slots=True, repr=False)
class _CanonicalPlanRecord:
    plan: PlannedAction
    action: ActionDecision
    binding: DecisionBinding
    reply_target: ReplyTarget | None
    snapshot: tuple[object, ...]


def _binding_snapshot(binding: DecisionBinding) -> tuple[object, ...]:
    if type(binding) is not DecisionBinding:
        raise ContractViolation("planned_action_binding_invalid")
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


def _target_snapshot(target: ReplyTarget | None) -> tuple[object, ...]:
    if target is None:
        return ()
    if type(target) is not ReplyTarget:
        raise ContractViolation("planned_action_target_invalid")
    return (
        target.message_id,
        target.sender_key,
        target.session_id,
        target.scope_key,
        target.content_digest,
        target.source_kind,
        target.referenced_message_id,
        target.degradation_reasons,
    )


def _canonical_plan_snapshot(plan: PlannedAction) -> tuple[object, ...]:
    if type(plan) is not PlannedAction:
        raise ContractViolation("planned_action_required")
    action = plan.action
    if type(action) is not ActionDecision:
        raise ContractViolation("planned_action_decision_required")
    if type(action.kind) is not ActionKind:
        raise ContractViolation("planned_action_kind_invalid")
    if type(plan.structural_outcome) is not StructuralOutcome:
        raise ContractViolation("planned_action_structural_outcome_invalid")
    if type(plan.model_hint_applied) is not bool:
        raise ContractViolation("planned_action_model_hint_invalid")
    if type(plan.planner_reason_codes) is not tuple or any(
        type(reason) is not str for reason in plan.planner_reason_codes
    ):
        raise ContractViolation("planned_action_reason_codes_invalid")
    if type(action.reason_codes) is not tuple or any(
        type(reason) is not str for reason in action.reason_codes
    ):
        raise ContractViolation("planned_action_decision_reason_codes_invalid")
    if action.operation_intent is not None and (
        type(action.operation_intent) is not OwnerActionOperation
    ):
        raise ContractViolation("planned_action_operation_invalid")
    binding_snapshot = _binding_snapshot(action.binding)
    target_snapshot = _target_snapshot(action.reply_target)
    # Reconstructing the public decision re-runs all closed-shape invariants.  The
    # exact identities are checked separately by PlannedActionAuthority.
    ActionDecision(
        binding=action.binding,
        kind=action.kind,
        reply_target=action.reply_target,
        public_scope_key=action.public_scope_key,
        capability_intent=action.capability_intent,
        operation_intent=action.operation_intent,
        expression_intent=action.expression_intent,
        wait_until=action.wait_until,
        reason_codes=action.reason_codes,
    )
    expected_action_id = _action_digest(
        action,
        plan.structural_outcome,
        plan.model_hint_applied,
        plan.planner_reason_codes,
    )
    if plan.action_id != expected_action_id:
        raise ContractViolation("planned_action_digest_mismatch")
    return (
        *binding_snapshot,
        action.kind,
        target_snapshot,
        action.public_scope_key,
        action.capability_intent,
        action.operation_intent,
        action.expression_intent,
        action.wait_until,
        action.reason_codes,
        plan.structural_outcome,
        plan.model_hint_applied,
        plan.planner_reason_codes,
        plan.action_id,
    )


class PlannedActionAuthority:
    """Bounded exact-object registry for plans emitted by this planner instance.

    Public ``PlannedAction`` remains a serializable shape, never authority.  Only a
    plan published by ``reconcile_action`` through this exact long-lived authority
    can pass ``inspect_plan``; copies, cross-authority plans, and post-publication
    mutation of the plan or nested decision/target/binding fail closed.
    """

    __slots__ = ("_lock", "_max_plans", "_records")

    def __init__(self, *, max_plans: int = _DEFAULT_MAX_CANONICAL_PLANS) -> None:
        if isinstance(max_plans, bool) or not isinstance(max_plans, int) or max_plans < 1:
            raise ContractViolation("planned_action_authority_limit_invalid")
        self._max_plans = max_plans
        self._records: dict[int, _CanonicalPlanRecord] = {}
        self._lock = threading.RLock()

    def _publish(self, plan: PlannedAction, seal: object) -> PlannedAction:
        if seal is not _PLANNER_PUBLISH_SEAL:
            raise ContractViolation("planned_action_publish_forbidden")
        snapshot = _canonical_plan_snapshot(plan)
        with self._lock:
            existing = self._records.get(id(plan))
            if existing is not None:
                if existing.plan is plan and existing.snapshot == snapshot:
                    return plan
                raise ContractViolation("planned_action_registry_collision")
            if len(self._records) >= self._max_plans:
                # Eviction is fail-closed: an old exact object stops being canonical.
                self._records.pop(next(iter(self._records)))
            self._records[id(plan)] = _CanonicalPlanRecord(
                plan=plan,
                action=plan.action,
                binding=plan.binding,
                reply_target=plan.action.reply_target,
                snapshot=snapshot,
            )
            return plan

    def inspect_plan(self, plan: PlannedAction) -> PlannedAction:
        with self._lock:
            if type(plan) is not PlannedAction:
                raise ContractViolation("planned_action_required")
            record = self._records.get(id(plan))
            if record is None or record.plan is not plan:
                raise ContractViolation("planned_action_not_canonical")
            try:
                snapshot = _canonical_plan_snapshot(plan)
            except ContractViolation as exc:
                raise ContractViolation("planned_action_corrupt") from exc
            if (
                plan.action is not record.action
                or plan.binding is not record.binding
                or plan.action.reply_target is not record.reply_target
                or snapshot != record.snapshot
            ):
                raise ContractViolation("planned_action_corrupt")
            return plan

    def trace_metadata(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "schema_version": 1,
                "max_plans": self._max_plans,
                "canonical_plan_count": len(self._records),
                "ledger_bounded": len(self._records) <= self._max_plans,
            }


def _publish_canonical_plan(
    authority: PlannedActionAuthority,
    plan: PlannedAction,
) -> PlannedAction:
    if type(authority) is not PlannedActionAuthority:
        raise ContractViolation("planned_action_authority_required")
    return authority._publish(plan, _PLANNER_PUBLISH_SEAL)


def structural_action_gate(
    *,
    ingress: IngressDecision,
    address: AddressDecision,
    attention: AttentionDecision,
    participation: ParticipationDecision,
    reply_target: ReplyTarget,
    model_suggestion: Mapping[str, Any] | ModelActionSuggestion | None = None,
    now: float = 0.0,
) -> StructuralGateDecision:
    """Resolve only structural termination/participation, never knowledge or tools."""

    binding = _require_structural_bindings(
        ingress=ingress,
        address=address,
        attention=attention,
        participation=participation,
        reply_target=reply_target,
    )
    suggestion = _coerce_model_suggestion(model_suggestion)
    try:
        current_time = float(now)
    except (TypeError, ValueError) as exc:
        raise ContractViolation("planner_clock_invalid") from exc
    if not math.isfinite(current_time) or current_time < 0:
        raise ContractViolation("planner_clock_invalid")

    if not ingress.allows_state_mutation:
        return StructuralGateDecision(
            binding=binding,
            outcome=StructuralOutcome.NO_ACTION,
            reply_target=reply_target,
            reason_codes=("ingress_denied",),
        )
    if attention.level is AttentionLevel.IGNORE:
        return StructuralGateDecision(
            binding=binding,
            outcome=StructuralOutcome.NO_ACTION,
            reply_target=reply_target,
            reason_codes=("attention_ignored",),
        )
    if participation.level is ParticipationLevel.NO_ACTION:
        return StructuralGateDecision(
            binding=binding,
            outcome=StructuralOutcome.NO_ACTION,
            reply_target=reply_target,
            reason_codes=("participation_no_action",),
        )
    if participation.level is ParticipationLevel.WAIT:
        delay = max(1.0, float(participation.cooldown_remaining_s))
        return StructuralGateDecision(
            binding=binding,
            outcome=StructuralOutcome.WAIT,
            reply_target=reply_target,
            wait_until=current_time + delay,
            reason_codes=("participation_wait",),
        )
    if participation.level is ParticipationLevel.REACT_ONLY:
        expression_intent, applied = _reaction_intent(suggestion)
        return StructuralGateDecision(
            binding=binding,
            outcome=StructuralOutcome.REACT,
            reply_target=reply_target,
            expression_intent=expression_intent,
            model_hint_applied=applied,
            reason_codes=("participation_react_only",),
        )
    if participation.level not in {
        ParticipationLevel.MUST_REPLY,
        ParticipationLevel.MAY_JOIN,
    }:
        raise ContractViolation("planner_participation_invalid")
    if (
        address.kind is AddressKind.DIRECT_SELF
        and attention.level is not AttentionLevel.FORCE
    ):
        raise ContractViolation("planner_direct_attention_mismatch")
    return StructuralGateDecision(
        binding=binding,
        outcome=StructuralOutcome.CONTINUE,
        reply_target=reply_target,
        reason_codes=(
            "direct_force_must_reply"
            if (
                address.kind is AddressKind.DIRECT_SELF
                and attention.level is AttentionLevel.FORCE
                and participation.level is ParticipationLevel.MUST_REPLY
            )
            else "participation_continue",
        ),
    )


def _terminal_action(gate: StructuralGateDecision) -> PlannedAction:
    if gate.outcome is StructuralOutcome.NO_ACTION:
        action = ActionDecision(
            binding=gate.binding,
            kind=ActionKind.NO_ACTION,
            reason_codes=gate.reason_codes,
        )
    elif gate.outcome is StructuralOutcome.WAIT:
        action = ActionDecision(
            binding=gate.binding,
            kind=ActionKind.WAIT,
            wait_until=gate.wait_until,
            reason_codes=gate.reason_codes,
        )
    elif gate.outcome is StructuralOutcome.REACT:
        action = ActionDecision(
            binding=gate.binding,
            kind=ActionKind.REACT,
            reply_target=gate.reply_target,
            expression_intent=gate.expression_intent,
            reason_codes=gate.reason_codes,
        )
    else:
        raise ContractViolation("planner_terminal_gate_required")
    return PlannedAction(
        action=action,
        structural_outcome=gate.outcome,
        model_hint_applied=gate.model_hint_applied,
        planner_reason_codes=gate.reason_codes,
    )


def reconcile_action(
    *,
    gate: StructuralGateDecision,
    ingress: IngressDecision,
    address: AddressDecision,
    attention: AttentionDecision,
    participation: ParticipationDecision,
    reply_target: ReplyTarget,
    knowledge_gap: KnowledgeGapDecision,
    capability_policy: CapabilityPolicy,
    owner_action_router: OwnerActionRouter | None = None,
    owner_action_ticket: AcceptedTurnTicket | None = None,
    owner_action_route: OwnerActionRouteDecision | None = None,
    pending_resolution_controller: object | None = None,
    pending_resolution_route: object | None = None,
    planned_action_authority: PlannedActionAuthority | None = None,
) -> PlannedAction:
    """Reconcile one structural gate with code-owned knowledge and capability policy."""

    binding = _require_reconcile_bindings(
        ingress=ingress,
        address=address,
        attention=attention,
        participation=participation,
        reply_target=reply_target,
        knowledge_gap=knowledge_gap,
        capability_policy=capability_policy,
    )
    if not isinstance(gate, StructuralGateDecision):
        raise ContractViolation("planner_structural_gate_required")
    if gate.binding != binding or gate.reply_target != reply_target:
        raise ContractViolation("planner_structural_gate_binding_mismatch")
    if gate.outcome is not StructuralOutcome.CONTINUE:
        return _terminal_action(gate)

    if not capability_policy.chat_read:
        action = ActionDecision(
            binding=binding,
            kind=ActionKind.NO_ACTION,
            reason_codes=("chat_capability_denied",),
        )
        return PlannedAction(
            action=action,
            structural_outcome=StructuralOutcome.NO_ACTION,
            planner_reason_codes=("chat_capability_denied",),
        )

    owner_route_values = (
        owner_action_router,
        owner_action_ticket,
        owner_action_route,
    )
    continuation_values = (
        pending_resolution_controller,
        pending_resolution_route,
    )
    if any(value is not None for value in owner_route_values) and any(
        value is not None for value in continuation_values
    ):
        raise ContractViolation("planner_owner_action_routes_conflict")
    if any(value is not None for value in owner_route_values):
        if not all(value is not None for value in owner_route_values):
            raise ContractViolation("planner_owner_action_route_inputs_incomplete")
        if type(owner_action_router) is not OwnerActionRouter:
            raise ContractViolation("planner_owner_action_router_required")
        if type(owner_action_ticket) is not AcceptedTurnTicket:
            raise ContractViolation("planner_owner_action_ticket_required")
        if type(owner_action_route) is not OwnerActionRouteDecision:
            raise ContractViolation("planner_owner_action_route_required")
        if owner_action_ticket.binding is not binding:
            raise ContractViolation("planner_owner_action_ticket_binding_mismatch")
        inspected_route = owner_action_router.inspect_route(
            owner_action_route,
            ticket=owner_action_ticket,
        )
        if inspected_route.status is OwnerActionRouteStatus.MATCHED:
            proposal = inspected_route.proposal
            if proposal is None or proposal.binding is not binding:
                raise ContractViolation("planner_owner_action_proposal_binding_mismatch")
            budget = capability_policy.max_external_tool_calls
            if (
                not _verified_owner_action_policy(capability_policy)
                or not capability_policy.allows_capability(proposal.capability)
                or (budget != -1 and budget < 1)
            ):
                reasons = ("owner_action_policy_denied_reply",)
                action = ActionDecision(
                    binding=binding,
                    kind=ActionKind.REPLY,
                    reply_target=reply_target,
                    reason_codes=reasons,
                )
                return PlannedAction(
                    action=action,
                    structural_outcome=StructuralOutcome.CONTINUE,
                    planner_reason_codes=(*gate.reason_codes, *reasons),
                )
            reasons = ("owner_action_route_verified",)
            action = ActionDecision(
                binding=binding,
                kind=ActionKind.EXECUTE_ACTION,
                reply_target=reply_target,
                capability_intent=proposal.capability.value,
                operation_intent=proposal.operation,
                reason_codes=reasons,
            )
            return _publish_canonical_plan(
                planned_action_authority,
                PlannedAction(
                    action=action,
                    structural_outcome=StructuralOutcome.CONTINUE,
                    model_hint_applied=False,
                    planner_reason_codes=(*gate.reason_codes, *reasons),
                ),
            )

    if any(value is not None for value in continuation_values):
        if not all(value is not None for value in continuation_values):
            raise ContractViolation("planner_pending_resolution_inputs_incomplete")
        # Runtime-local import is intentional: the controller depends on the
        # PlannedAction shape, while this branch only needs its exact inspector.
        from .owner_action_controller import (
            OwnerActionController,
            PendingDecision,
            PendingResolutionRoute,
        )

        if type(pending_resolution_controller) is not OwnerActionController:
            raise ContractViolation("planner_pending_resolution_controller_required")
        if type(pending_resolution_route) is not PendingResolutionRoute:
            raise ContractViolation("planner_pending_resolution_route_required")
        inspected_resolution = (
            pending_resolution_controller.inspect_pending_resolution_route(
                pending_resolution_route
            )
        )
        if inspected_resolution.binding is not binding:
            raise ContractViolation("planner_pending_resolution_binding_mismatch")
        budget = capability_policy.max_external_tool_calls
        if (
            not _verified_owner_action_policy(capability_policy)
            or not capability_policy.allows_capability(
                inspected_resolution.capability
            )
            or (budget != -1 and budget < 1)
        ):
            reasons = ("owner_action_confirmation_policy_denied_reply",)
            action = ActionDecision(
                binding=binding,
                kind=ActionKind.REPLY,
                reply_target=reply_target,
                reason_codes=reasons,
            )
            return PlannedAction(
                action=action,
                structural_outcome=StructuralOutcome.CONTINUE,
                planner_reason_codes=(*gate.reason_codes, *reasons),
            )
        reasons = (
            "owner_action_confirmation_route_verified",
            f"pending_decision_{inspected_resolution.decision.value}",
        )
        if inspected_resolution.decision is PendingDecision.CONFIRM:
            action = ActionDecision(
                binding=binding,
                kind=ActionKind.EXECUTE_ACTION,
                reply_target=reply_target,
                capability_intent=inspected_resolution.capability.value,
                operation_intent=inspected_resolution.operation,
                reason_codes=reasons,
            )
        else:
            # Denial/cancellation is visible conversation, not an execution plan.
            action = ActionDecision(
                binding=binding,
                kind=ActionKind.REPLY,
                reply_target=reply_target,
                reason_codes=reasons,
            )
        return _publish_canonical_plan(
            planned_action_authority,
            PlannedAction(
                action=action,
                structural_outcome=StructuralOutcome.CONTINUE,
                model_hint_applied=False,
                planner_reason_codes=(*gate.reason_codes, *reasons),
            ),
        )

    capability = knowledge_gap.requested_capability
    if knowledge_gap.need is not KnowledgeNeed.NONE and capability is not None:
        policy_budget = capability_policy.max_external_tool_calls
        budget_allowed = policy_budget == -1 or (
            policy_budget >= knowledge_gap.max_tool_calls > 0
        )
        configured_route_available = (
            not capability_policy.requires_explicit_tool_name
            or bool(capability_policy.explicitly_configured_tools)
        )
        if (
            not capability_policy.is_degraded
            and capability_policy.allows_capability(capability)
            and budget_allowed
            and configured_route_available
        ):
            reasons = ("knowledge_tool_required",)
            action = ActionDecision(
                binding=binding,
                kind=ActionKind.USE_TOOL,
                reply_target=reply_target,
                capability_intent=capability.value,
                reason_codes=reasons,
            )
            return PlannedAction(
                action=action,
                structural_outcome=StructuralOutcome.CONTINUE,
                model_hint_applied=gate.model_hint_applied,
                planner_reason_codes=(*gate.reason_codes, *reasons),
            )
        reasons = ("knowledge_capability_denied_reply",)
    else:
        reasons = ("direct_reply_ready",)

    action = ActionDecision(
        binding=binding,
        kind=ActionKind.REPLY,
        reply_target=reply_target,
        reason_codes=reasons,
    )
    return PlannedAction(
        action=action,
        structural_outcome=StructuralOutcome.CONTINUE,
        model_hint_applied=gate.model_hint_applied,
        planner_reason_codes=(*gate.reason_codes, *reasons),
    )


def plan_action(
    *,
    ingress: IngressDecision,
    address: AddressDecision,
    attention: AttentionDecision,
    participation: ParticipationDecision,
    reply_target: ReplyTarget,
    knowledge_gap: KnowledgeGapDecision,
    capability_policy: CapabilityPolicy,
    model_suggestion: Mapping[str, Any] | ModelActionSuggestion | None = None,
    owner_action_router: OwnerActionRouter | None = None,
    owner_action_ticket: AcceptedTurnTicket | None = None,
    owner_action_route: OwnerActionRouteDecision | None = None,
    pending_resolution_controller: object | None = None,
    pending_resolution_route: object | None = None,
    planned_action_authority: PlannedActionAuthority | None = None,
    now: float = 0.0,
) -> PlannedAction:
    """Run the structural gate first, then the knowledge/capability reconcile."""

    gate = structural_action_gate(
        ingress=ingress,
        address=address,
        attention=attention,
        participation=participation,
        reply_target=reply_target,
        model_suggestion=model_suggestion,
        now=now,
    )
    planned = reconcile_action(
        gate=gate,
        ingress=ingress,
        address=address,
        attention=attention,
        participation=participation,
        reply_target=reply_target,
        knowledge_gap=knowledge_gap,
        capability_policy=capability_policy,
        owner_action_router=owner_action_router,
        owner_action_ticket=owner_action_ticket,
        owner_action_route=owner_action_route,
        pending_resolution_controller=pending_resolution_controller,
        pending_resolution_route=pending_resolution_route,
        planned_action_authority=planned_action_authority,
    )
    if planned_action_authority is not None:
        return _publish_canonical_plan(planned_action_authority, planned)
    return planned


def plan_initiative(
    *,
    binding: DecisionBinding,
    public_scope_key: str,
    expression_intent: str,
) -> PlannedAction:
    """Build the typed INITIATE shape only; production selection belongs to P7."""

    if not isinstance(binding, DecisionBinding):
        raise ContractViolation("planner_binding_required")
    intent = require_safe_name(expression_intent, "initiative_expression_intent")
    action = ActionDecision(
        binding=binding,
        kind=ActionKind.INITIATE,
        public_scope_key=str(public_scope_key or "").strip(),
        expression_intent=intent,
        reason_codes=("initiative_contract_only",),
    )
    return PlannedAction(
        action=action,
        structural_outcome=StructuralOutcome.INITIATE,
        planner_reason_codes=("initiative_contract_only",),
    )


__all__ = [
    "PlannedActionAuthority",
    "PlannedAction",
    "StructuralGateDecision",
    "StructuralOutcome",
    "plan_action",
    "plan_initiative",
    "reconcile_action",
    "structural_action_gate",
]
