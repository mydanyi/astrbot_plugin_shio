from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..context_assembler import ReplyTarget
from ._validation import (
    ContractViolation,
    normalize_reason_codes,
    require_nonnegative,
    require_score,
    require_text,
)
from .binding import DecisionBinding, require_same_binding
from .ingress import IngressDecision
from .owner_action import OwnerActionOperation


class AddressKind(str, Enum):
    DIRECT_SELF = "direct_self"
    ABOUT_SELF = "about_self"
    OTHER_PERSON = "other_person"
    OPEN_GROUP = "open_group"
    UNCERTAIN = "uncertain"


class AddressEvidence(str, Enum):
    PRIVATE_CHANNEL = "private_channel"
    NATIVE_WAKE = "native_wake"
    STRUCTURED_MENTION = "structured_mention"
    REPLY_TO_SELF = "reply_to_self"
    VOCATIVE_ALIAS = "vocative_alias"
    SELF_REFERENCE = "self_reference"
    THIRD_PERSON_REFERENCE = "third_person_reference"
    OTHER_PERSON_TARGET = "other_person_target"
    OPEN_GROUP_MARKER = "open_group_marker"
    NONE = "none"


class AttentionLevel(str, Enum):
    FORCE = "force"
    CONSIDER = "consider"
    IGNORE = "ignore"


class ParticipationLevel(str, Enum):
    MUST_REPLY = "must_reply"
    MAY_JOIN = "may_join"
    REACT_ONLY = "react_only"
    WAIT = "wait"
    NO_ACTION = "no_action"


class ActionKind(str, Enum):
    REPLY = "reply"
    REACT = "react"
    WAIT = "wait"
    USE_TOOL = "use_tool"
    EXECUTE_ACTION = "execute_action"
    NO_ACTION = "no_action"
    INITIATE = "initiate"


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class AddressDecision:
    binding: DecisionBinding = field(repr=False)
    kind: AddressKind
    evidence: tuple[AddressEvidence, ...]
    confidence: float
    is_meta_discussion: bool = False
    referenced_message_id: str = field(default="", repr=False)
    referenced_sender_key: str = field(default="", repr=False)
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.kind) is not AddressKind:
            raise ContractViolation("address_kind_invalid")
        if any(type(value) is not AddressEvidence for value in self.evidence):
            raise ContractViolation("address_evidence_invalid")
        object.__setattr__(self, "confidence", require_score(self.confidence, "address_confidence"))
        object.__setattr__(self, "evidence", tuple(dict.fromkeys(self.evidence)))
        object.__setattr__(self, "reason_codes", normalize_reason_codes(self.reason_codes))
        meaningful = {value for value in self.evidence if value is not AddressEvidence.NONE}
        if self.kind is not AddressKind.UNCERTAIN and not meaningful:
            raise ContractViolation("address_evidence_required")
        if self.referenced_sender_key and not self.referenced_message_id:
            raise ContractViolation("address_reference_message_required")

    def trace_metadata(self) -> dict[str, str | int | float | bool]:
        return {
            "address_kind": self.kind.value,
            "address_confidence": self.confidence,
            "address_evidence_count": len(self.evidence),
            "address_meta_discussion": self.is_meta_discussion,
            "address_has_reference": bool(self.referenced_message_id),
        }


@dataclass(frozen=True, slots=True)
class AttentionDecision:
    binding: DecisionBinding
    level: AttentionLevel
    self_relevance: float = 0.0
    response_value: float = 0.0
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.level) is not AttentionLevel:
            raise ContractViolation("attention_level_invalid")
        object.__setattr__(self, "self_relevance", require_score(self.self_relevance, "self_relevance"))
        object.__setattr__(self, "response_value", require_score(self.response_value, "response_value"))
        object.__setattr__(self, "reason_codes", normalize_reason_codes(self.reason_codes))

    def trace_metadata(self) -> dict[str, str | int | float]:
        return {
            "attention_level": self.level.value,
            "attention_self_relevance": self.self_relevance,
            "attention_response_value": self.response_value,
            "attention_reason_count": len(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class ParticipationDecision:
    binding: DecisionBinding
    level: ParticipationLevel
    interruption_cost: float = 0.0
    recent_presence: float = 0.0
    cooldown_remaining_s: float = 0.0
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.level) is not ParticipationLevel:
            raise ContractViolation("participation_level_invalid")
        object.__setattr__(self, "interruption_cost", require_score(self.interruption_cost, "interruption_cost"))
        object.__setattr__(self, "recent_presence", require_score(self.recent_presence, "recent_presence"))
        object.__setattr__(
            self,
            "cooldown_remaining_s",
            require_nonnegative(self.cooldown_remaining_s, "cooldown_remaining_s"),
        )
        object.__setattr__(self, "reason_codes", normalize_reason_codes(self.reason_codes))

    def trace_metadata(self) -> dict[str, str | int | float]:
        return {
            "participation_level": self.level.value,
            "participation_interruption_cost": self.interruption_cost,
            "participation_recent_presence": self.recent_presence,
            "participation_cooldown_remaining_s": self.cooldown_remaining_s,
            "participation_reason_count": len(self.reason_codes),
        }


def _validate_target(binding: DecisionBinding, value: ReplyTarget) -> None:
    if type(value) is not ReplyTarget or (
        value.message_id != binding.current_message_id
        or value.sender_key != binding.current_sender_key
        or value.session_id != binding.session_id
        or value.scope_key != binding.scope_key
        or value.content_digest != binding.current_content_digest
        or not value.is_actionable
    ):
        raise ContractViolation("reply_target_binding_mismatch")


@dataclass(frozen=True, slots=True)
class ActionDecision:
    binding: DecisionBinding
    kind: ActionKind
    reply_target: ReplyTarget | None = None
    public_scope_key: str = ""
    capability_intent: str = ""
    operation_intent: OwnerActionOperation | None = None
    expression_intent: str = ""
    wait_until: float = 0.0
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.kind) is not ActionKind:
            raise ContractViolation("action_kind_invalid")
        object.__setattr__(self, "reason_codes", normalize_reason_codes(self.reason_codes))
        object.__setattr__(self, "wait_until", require_nonnegative(self.wait_until, "wait_until"))
        targeted = {
            ActionKind.REPLY,
            ActionKind.REACT,
            ActionKind.USE_TOOL,
            ActionKind.EXECUTE_ACTION,
        }
        if self.kind in targeted:
            if self.reply_target is None:
                raise ContractViolation("visible_action_target_required")
            _validate_target(self.binding, self.reply_target)
            if self.public_scope_key:
                raise ContractViolation("targeted_action_public_scope_forbidden")
        elif self.reply_target is not None:
            raise ContractViolation("non_targeted_action_target_forbidden")
        if self.kind is ActionKind.INITIATE:
            if self.public_scope_key != self.binding.scope_key:
                raise ContractViolation("initiative_public_scope_required")
            if not self.expression_intent:
                raise ContractViolation("initiative_expression_required")
        elif self.public_scope_key:
            raise ContractViolation("public_scope_only_for_initiative")
        if self.kind is ActionKind.USE_TOOL and not self.capability_intent:
            raise ContractViolation("tool_capability_intent_required")
        if self.kind is ActionKind.EXECUTE_ACTION:
            if not self.capability_intent:
                raise ContractViolation("action_capability_intent_required")
            if self.operation_intent is None:
                raise ContractViolation("action_operation_intent_required")
            if type(self.operation_intent) is not OwnerActionOperation:
                raise ContractViolation("action_operation_intent_invalid")
        elif self.operation_intent is not None:
            raise ContractViolation("operation_intent_only_for_execute_action")
        if (
            self.kind not in {ActionKind.USE_TOOL, ActionKind.EXECUTE_ACTION}
            and self.capability_intent
        ):
            raise ContractViolation("capability_intent_only_for_tool_or_action")
        if self.kind is ActionKind.REACT and not self.expression_intent:
            raise ContractViolation("reaction_expression_required")
        if self.kind is ActionKind.WAIT and self.wait_until <= 0:
            raise ContractViolation("wait_deadline_required")
        if self.kind is not ActionKind.WAIT and self.wait_until:
            raise ContractViolation("wait_deadline_only_for_wait")

    @property
    def is_visible(self) -> bool:
        return self.kind in {ActionKind.REPLY, ActionKind.REACT, ActionKind.INITIATE}


_MODEL_ALLOWED_KEYS = {
    "action_hint",
    "confidence",
    "capability_intent",
    "expression_intent",
    "reason_codes",
}
_MODEL_PROTECTED_KEYS = {
    "sender_key",
    "sender_id",
    "is_owner",
    "owner",
    "target",
    "target_message_id",
    "target_sender_id",
    "capability_policy",
    "authority",
    "plugin_source",
    "source_kind",
    "media_source",
    "media_origin",
    "scope_key",
    "session_id",
    "operation",
    "operation_intent",
}


@dataclass(frozen=True, slots=True)
class ModelActionSuggestion:
    action_hint: ActionKind
    confidence: float
    capability_intent: str = ""
    expression_intent: str = ""
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.action_hint) is not ActionKind:
            raise ContractViolation("model_action_hint_invalid")
        object.__setattr__(self, "confidence", require_score(self.confidence, "model_confidence"))
        object.__setattr__(self, "reason_codes", normalize_reason_codes(self.reason_codes))


def parse_model_action_suggestion(payload: Mapping[str, Any]) -> ModelActionSuggestion:
    keys = {str(key or "").strip() for key in payload}
    if keys.intersection(_MODEL_PROTECTED_KEYS):
        raise ContractViolation("model_authority_field_forbidden")
    if keys.difference(_MODEL_ALLOWED_KEYS):
        raise ContractViolation("model_suggestion_field_unknown")
    try:
        action = ActionKind(str(payload.get("action_hint", "")).strip().lower())
    except ValueError as exc:
        raise ContractViolation("model_action_hint_invalid") from exc
    if action is ActionKind.EXECUTE_ACTION:
        raise ContractViolation("model_execute_action_forbidden")
    reason_values = payload.get("reason_codes", ())
    if isinstance(reason_values, str):
        reason_values = (reason_values,)
    return ModelActionSuggestion(
        action_hint=action,
        confidence=float(payload.get("confidence", 0.0)),
        capability_intent=str(payload.get("capability_intent", "") or "").strip(),
        expression_intent=str(payload.get("expression_intent", "") or "").strip(),
        reason_codes=tuple(reason_values or ()),
    )


@dataclass(frozen=True, slots=True)
class BehaviorDecisionFrame:
    ingress: IngressDecision
    address: AddressDecision
    attention: AttentionDecision
    participation: ParticipationDecision
    action: ActionDecision

    def __post_init__(self) -> None:
        require_same_binding(
            self.ingress,
            self.address,
            self.attention,
            self.participation,
            self.action,
        )
        if not self.ingress.allows_state_mutation:
            if self.attention.level is not AttentionLevel.IGNORE:
                raise ContractViolation("denied_ingress_attention_forbidden")
            if self.participation.level is not ParticipationLevel.NO_ACTION:
                raise ContractViolation("denied_ingress_participation_forbidden")
            if self.action.kind is not ActionKind.NO_ACTION:
                raise ContractViolation("denied_ingress_action_forbidden")
            return
        if self.address.kind is AddressKind.DIRECT_SELF and self.attention.level is not AttentionLevel.FORCE:
            raise ContractViolation("direct_address_attention_must_force")
        if self.participation.level is ParticipationLevel.NO_ACTION and self.action.kind is not ActionKind.NO_ACTION:
            raise ContractViolation("no_participation_action_forbidden")
        if self.participation.level is ParticipationLevel.REACT_ONLY and self.action.kind not in {ActionKind.REACT, ActionKind.NO_ACTION}:
            raise ContractViolation("react_only_action_invalid")
