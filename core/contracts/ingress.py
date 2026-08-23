from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ._validation import (
    ContractViolation,
    normalize_reason_codes,
    require_hex,
    require_safe_name,
)
from .binding import DecisionBinding


class SenderKind(str, Enum):
    HUMAN = "human"
    SELF = "self"
    KNOWN_BOT = "known_bot"
    UNKNOWN_AUTOMATION = "unknown_automation"
    PLUGIN_ECHO = "plugin_echo"
    UNKNOWN = "unknown"


class IngressDisposition(str, Enum):
    ACCEPT_HUMAN = "accept_human"
    DROP_BANNED = "drop_banned"
    DROP_SELF = "drop_self"
    DROP_KNOWN_BOT = "drop_known_bot"
    DROP_PLUGIN_ECHO = "drop_plugin_echo"
    DEGRADED_EXTERNAL_GATE = "degraded_external_gate"


class PluginEvidenceKind(str, Enum):
    GATE_DECISION = "gate_decision"
    MEMORY_REFERENCE = "memory_reference"
    GROUNDING_FACT = "grounding_fact"
    PRESENTATION_EFFECT = "presentation_effect"
    EXTERNAL_OUTPUT = "external_output"
    TOOL_RESULT = "tool_result"


class PluginEvidenceStatus(str, Enum):
    VERIFIED = "verified"
    MISSING = "missing"
    DISABLED = "disabled"
    TIMEOUT = "timeout"
    ERROR = "error"
    INTERFACE_CHANGED = "interface_changed"
    HOOK_ORDER_INVALID = "hook_order_invalid"


class HistoryVisibility(str, Enum):
    DROP = "drop"
    CURRENT_TURN_REFERENCE = "current_turn_reference"
    PUBLIC_SCENE_REFERENCE = "public_scene_reference"
    PERSONAL_REFERENCE = "personal_reference"


class EvidenceConsumer(str, Enum):
    INGRESS = "ingress"
    CURRENT_TURN = "current_turn"
    PUBLIC_SCENE = "public_scene"
    PERSONAL_MEMORY = "personal_memory"
    PERSONA = "persona"
    LEARNING = "learning"
    PRESENTATION = "presentation"


_NON_HISTORY_KINDS = {
    PluginEvidenceKind.GATE_DECISION,
    PluginEvidenceKind.PRESENTATION_EFFECT,
    PluginEvidenceKind.EXTERNAL_OUTPUT,
}


@dataclass(frozen=True, slots=True)
class ExternalPluginEvidence:
    binding: DecisionBinding
    plugin_id: str
    evidence_kind: PluginEvidenceKind
    status: PluginEvidenceStatus
    history_visibility: HistoryVisibility = HistoryVisibility.DROP
    trusted: bool = False
    allowed_consumers: tuple[EvidenceConsumer, ...] = ()
    source_event_digest: str = ""
    target_message_id: str = ""
    subject_key: str = ""
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugin_id", require_safe_name(self.plugin_id, "plugin_id"))
        object.__setattr__(self, "reason_codes", normalize_reason_codes(self.reason_codes))
        if self.source_event_digest:
            object.__setattr__(
                self,
                "source_event_digest",
                require_hex(self.source_event_digest, "source_event_digest", lengths=(64,)),
            )
        if self.target_message_id and self.target_message_id != self.binding.current_message_id:
            raise ContractViolation("plugin_target_binding_mismatch")
        consumers = tuple(dict.fromkeys(self.allowed_consumers))
        object.__setattr__(self, "allowed_consumers", consumers)
        if self.status is not PluginEvidenceStatus.VERIFIED or not self.trusted:
            if self.history_visibility is not HistoryVisibility.DROP or consumers:
                raise ContractViolation("unverified_plugin_evidence_visible")
        if self.evidence_kind in _NON_HISTORY_KINDS:
            if self.history_visibility is not HistoryVisibility.DROP:
                raise ContractViolation("plugin_effect_cannot_enter_history")
            forbidden = {
                EvidenceConsumer.CURRENT_TURN,
                EvidenceConsumer.PUBLIC_SCENE,
                EvidenceConsumer.PERSONAL_MEMORY,
                EvidenceConsumer.PERSONA,
                EvidenceConsumer.LEARNING,
            }
            if forbidden.intersection(consumers):
                raise ContractViolation("plugin_effect_consumer_invalid")
        if EvidenceConsumer.PERSONA in consumers or EvidenceConsumer.LEARNING in consumers:
            raise ContractViolation("plugin_evidence_cannot_shape_persona_or_learning")

    def can_feed(self, consumer: EvidenceConsumer) -> bool:
        return (
            self.trusted
            and self.status is PluginEvidenceStatus.VERIFIED
            and consumer in self.allowed_consumers
        )

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "plugin_source": self.plugin_id,
            "plugin_status": self.status.value,
            "plugin_evidence_kind": self.evidence_kind.value,
            "history_visibility": self.history_visibility.value,
            "plugin_trusted": self.trusted,
            "plugin_consumer_count": len(self.allowed_consumers),
            "plugin_reason_count": len(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class IngressDecision:
    binding: DecisionBinding
    sender_kind: SenderKind
    disposition: IngressDisposition
    gate_evidence: tuple[ExternalPluginEvidence, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", normalize_reason_codes(self.reason_codes))
        if any(evidence.binding != self.binding for evidence in self.gate_evidence):
            raise ContractViolation("ingress_evidence_binding_mismatch")
        expected = {
            IngressDisposition.ACCEPT_HUMAN: {SenderKind.HUMAN},
            IngressDisposition.DROP_BANNED: {SenderKind.HUMAN},
            IngressDisposition.DROP_SELF: {SenderKind.SELF},
            IngressDisposition.DROP_KNOWN_BOT: {SenderKind.KNOWN_BOT},
            IngressDisposition.DROP_PLUGIN_ECHO: {SenderKind.PLUGIN_ECHO},
            IngressDisposition.DEGRADED_EXTERNAL_GATE: {
                SenderKind.HUMAN,
                SenderKind.UNKNOWN,
                SenderKind.UNKNOWN_AUTOMATION,
            },
        }[self.disposition]
        if self.sender_kind not in expected:
            raise ContractViolation("ingress_sender_disposition_mismatch")
        if self.disposition is not IngressDisposition.ACCEPT_HUMAN and not self.reason_codes:
            raise ContractViolation("ingress_drop_reason_required")

    @property
    def allows_state_mutation(self) -> bool:
        return (
            self.disposition is IngressDisposition.ACCEPT_HUMAN
            and self.sender_kind is SenderKind.HUMAN
        )

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "sender_kind": self.sender_kind.value,
            "ingress_status": self.disposition.value,
            "ingress_allowed": self.allows_state_mutation,
            "gate_evidence_count": len(self.gate_evidence),
            "ingress_reason_count": len(self.reason_codes),
        }

