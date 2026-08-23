from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Mapping

from astrbot_plugin_shio.core.contracts.ingress import (
    EvidenceConsumer,
    HistoryVisibility,
    PluginEvidenceKind,
)


class PluginId(str, Enum):
    LIVING_MEMORY = "livingmemory"
    RENEBAN = "reneban"
    ANYSEARCH = "anysearch"
    MEME_MANAGER = "meme_manager"
    PARSER = "parser"


class PluginState(str, Enum):
    PRESENT = "present"
    MISSING = "missing"
    DISABLED = "disabled"
    TIMEOUT = "timeout"
    ERROR = "error"
    INTERFACE_CHANGED = "interface_changed"


class ConformanceLevel(str, Enum):
    CONFORMANT = "conformant"
    DEGRADED = "degraded"
    NONCONFORMANT = "nonconformant"


class HookPhase(IntEnum):
    """Logical AstrBot order used by the offline compatibility harness."""

    WAKING_CHECK = 10
    STAR_REQUEST = 20
    INGRESS_ADMISSION = 30
    SHIO_STATE = 40
    LLM_REQUEST = 50
    TOOL_EXECUTION = 60
    LLM_RESPONSE = 70
    PRESENTATION = 80
    SEND = 90
    AFTER_SEND = 100
    HISTORY_ADAPTATION = 110


class HookRole(str, Enum):
    PASSIVE_CAPTURE = "passive_capture"
    BAN_GATE = "ban_gate"
    SHIO_ADMISSION = "shio_admission"
    MEMORY_REFERENCE = "memory_reference"
    GROUNDING_TOOL = "grounding_tool"
    EXTERNAL_OUTPUT = "external_output"
    PRESENTATION_EFFECT = "presentation_effect"
    SEND_EFFECT = "send_effect"
    SEND_RECEIPT = "send_receipt"
    HISTORY_FILTER = "history_filter"


class PluginEffectKind(str, Enum):
    GATE_READ = "gate_read"
    MEMORY_REFERENCE_READ = "memory_reference_read"
    EXTERNAL_PASSIVE_CAPTURE = "external_passive_capture"
    PUBLIC_WEB_READ = "public_web_read"
    PRESENTATION_SEND = "presentation_send"
    EXTERNAL_MEDIA_SEND = "external_media_send"


@dataclass(frozen=True, slots=True)
class HookPoint:
    phase: HookPhase
    priority: int
    role: HookRole

    def __post_init__(self) -> None:
        if not isinstance(self.phase, HookPhase):
            raise TypeError("hook_phase_invalid")
        if not isinstance(self.role, HookRole):
            raise TypeError("hook_role_invalid")
        if not isinstance(self.priority, int) or not -10_000 <= self.priority <= 10_000:
            raise ValueError("hook_priority_invalid")


@dataclass(frozen=True, slots=True)
class PluginContract:
    plugin_id: PluginId
    interface_token: str
    evidence_kind: PluginEvidenceKind
    required_hooks: tuple[HookPoint, ...]
    allowed_history: frozenset[HistoryVisibility]
    allowed_consumers: frozenset[EvidenceConsumer]
    max_effect_counts: Mapping[PluginEffectKind, int]

    def __post_init__(self) -> None:
        token = str(self.interface_token or "").strip().lower()
        if not token.endswith(".v1") or any(char.isspace() for char in token):
            raise ValueError("plugin_interface_token_invalid")
        object.__setattr__(self, "interface_token", token)
        hooks = tuple(self.required_hooks)
        if not hooks or len(set(hooks)) != len(hooks):
            raise ValueError("plugin_hook_contract_invalid")
        if hooks != tuple(sorted(hooks, key=_hook_sort_key)):
            raise ValueError("plugin_hook_contract_unsorted")
        if not self.allowed_history:
            raise ValueError("plugin_history_contract_empty")
        if EvidenceConsumer.PERSONA in self.allowed_consumers:
            raise ValueError("plugin_persona_consumer_forbidden")
        if EvidenceConsumer.LEARNING in self.allowed_consumers:
            raise ValueError("plugin_learning_consumer_forbidden")
        effect_counts = dict(self.max_effect_counts)
        if not effect_counts or any(
            not isinstance(effect, PluginEffectKind)
            or not isinstance(max_count, int)
            or max_count < 1
            for effect, max_count in effect_counts.items()
        ):
            raise ValueError("plugin_effect_contract_invalid")
        object.__setattr__(self, "max_effect_counts", MappingProxyType(effect_counts))


@dataclass(frozen=True, slots=True)
class PluginObservation:
    plugin_id: PluginId
    state: PluginState
    interface_token: str
    registered_hooks: tuple[HookPoint, ...]
    evidence_kind: PluginEvidenceKind
    history_visibility: HistoryVisibility
    consumers: tuple[EvidenceConsumer, ...]
    effects: tuple[PluginEffectKind, ...]
    safe_degradation: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "interface_token", str(self.interface_token or "").strip().lower())
        object.__setattr__(self, "registered_hooks", tuple(self.registered_hooks))
        object.__setattr__(self, "consumers", tuple(dict.fromkeys(self.consumers)))
        object.__setattr__(self, "effects", tuple(self.effects))


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    plugin_id: PluginId
    state: PluginState
    level: ConformanceLevel
    reason_codes: tuple[str, ...]


def _hook_sort_key(point: HookPoint) -> tuple[int, int, str]:
    return (int(point.phase), -point.priority, point.role.value)


def evaluate_plugin(
    contract: PluginContract,
    observation: PluginObservation,
) -> ConformanceReport:
    """Evaluate one synthetic adapter observation without importing a plugin."""

    reasons: list[str] = []
    if observation.plugin_id is not contract.plugin_id:
        reasons.append("plugin_id_mismatch")

    if observation.state is not PluginState.PRESENT:
        if observation.effects:
            reasons.append("degraded_effect_leak")
        if observation.history_visibility is not HistoryVisibility.DROP:
            reasons.append("degraded_history_leak")
        if observation.consumers:
            reasons.append("degraded_consumer_leak")
        if not observation.safe_degradation:
            reasons.append("unsafe_degradation")
        if reasons:
            return ConformanceReport(
                plugin_id=contract.plugin_id,
                state=observation.state,
                level=ConformanceLevel.NONCONFORMANT,
                reason_codes=tuple(dict.fromkeys(reasons)),
            )
        return ConformanceReport(
            plugin_id=contract.plugin_id,
            state=observation.state,
            level=ConformanceLevel.DEGRADED,
            reason_codes=(f"plugin_{observation.state.value}",),
        )

    if observation.interface_token != contract.interface_token:
        reasons.append("interface_token_mismatch")
    if observation.registered_hooks != contract.required_hooks:
        reasons.append("hook_contract_mismatch")
    if observation.evidence_kind is not contract.evidence_kind:
        reasons.append("evidence_kind_mismatch")
    if observation.history_visibility not in contract.allowed_history:
        reasons.append("history_visibility_forbidden")
    if not set(observation.consumers).issubset(contract.allowed_consumers):
        reasons.append("consumer_forbidden")

    effect_counts = Counter(observation.effects)
    if any(effect not in contract.max_effect_counts for effect in effect_counts):
        reasons.append("effect_forbidden")
    if any(
        count > contract.max_effect_counts.get(effect, 0)
        for effect, count in effect_counts.items()
    ):
        reasons.append("effect_budget_exceeded")

    reason_codes = tuple(dict.fromkeys(reasons))
    return ConformanceReport(
        plugin_id=contract.plugin_id,
        state=observation.state,
        level=(
            ConformanceLevel.NONCONFORMANT
            if reason_codes
            else ConformanceLevel.CONFORMANT
        ),
        reason_codes=reason_codes,
    )


def production_plugin_contracts() -> Mapping[PluginId, PluginContract]:
    """Return the closed P1 production-plugin compatibility catalog."""

    return _PRODUCTION_PLUGIN_CONTRACTS


_PRODUCTION_PLUGIN_CONTRACTS: Mapping[PluginId, PluginContract] = MappingProxyType(
    {
        PluginId.LIVING_MEMORY: PluginContract(
            plugin_id=PluginId.LIVING_MEMORY,
            interface_token="memory_reference.v1",
            evidence_kind=PluginEvidenceKind.MEMORY_REFERENCE,
            required_hooks=(
                HookPoint(HookPhase.WAKING_CHECK, 0, HookRole.PASSIVE_CAPTURE),
                HookPoint(HookPhase.LLM_REQUEST, 0, HookRole.MEMORY_REFERENCE),
            ),
            allowed_history=frozenset({HistoryVisibility.CURRENT_TURN_REFERENCE}),
            allowed_consumers=frozenset({EvidenceConsumer.CURRENT_TURN}),
            max_effect_counts={
                PluginEffectKind.MEMORY_REFERENCE_READ: 1,
                # External behavior documented as X-01; Shio never consumes it.
                PluginEffectKind.EXTERNAL_PASSIVE_CAPTURE: 1,
            },
        ),
        PluginId.RENEBAN: PluginContract(
            plugin_id=PluginId.RENEBAN,
            interface_token="ban_gate.v1",
            evidence_kind=PluginEvidenceKind.GATE_DECISION,
            required_hooks=(
                HookPoint(HookPhase.STAR_REQUEST, 114, HookRole.BAN_GATE),
            ),
            allowed_history=frozenset({HistoryVisibility.DROP}),
            allowed_consumers=frozenset({EvidenceConsumer.INGRESS}),
            max_effect_counts={PluginEffectKind.GATE_READ: 1},
        ),
        PluginId.ANYSEARCH: PluginContract(
            plugin_id=PluginId.ANYSEARCH,
            interface_token="grounding_search.v1",
            evidence_kind=PluginEvidenceKind.GROUNDING_FACT,
            required_hooks=(
                HookPoint(HookPhase.TOOL_EXECUTION, 0, HookRole.GROUNDING_TOOL),
            ),
            allowed_history=frozenset({HistoryVisibility.CURRENT_TURN_REFERENCE}),
            allowed_consumers=frozenset({EvidenceConsumer.CURRENT_TURN}),
            max_effect_counts={PluginEffectKind.PUBLIC_WEB_READ: 1},
        ),
        PluginId.MEME_MANAGER: PluginContract(
            plugin_id=PluginId.MEME_MANAGER,
            interface_token="presentation_effect.v1",
            evidence_kind=PluginEvidenceKind.PRESENTATION_EFFECT,
            required_hooks=(
                HookPoint(HookPhase.PRESENTATION, 0, HookRole.PRESENTATION_EFFECT),
            ),
            allowed_history=frozenset({HistoryVisibility.DROP}),
            allowed_consumers=frozenset({EvidenceConsumer.PRESENTATION}),
            max_effect_counts={PluginEffectKind.PRESENTATION_SEND: 1},
        ),
        PluginId.PARSER: PluginContract(
            plugin_id=PluginId.PARSER,
            interface_token="external_output.v1",
            evidence_kind=PluginEvidenceKind.EXTERNAL_OUTPUT,
            required_hooks=(
                HookPoint(HookPhase.STAR_REQUEST, 0, HookRole.EXTERNAL_OUTPUT),
            ),
            allowed_history=frozenset({HistoryVisibility.DROP}),
            allowed_consumers=frozenset(),
            max_effect_counts={PluginEffectKind.EXTERNAL_MEDIA_SEND: 1},
        ),
    }
)


__all__ = [
    "ConformanceLevel",
    "ConformanceReport",
    "HookPhase",
    "HookPoint",
    "HookRole",
    "PluginContract",
    "PluginEffectKind",
    "PluginId",
    "PluginObservation",
    "PluginState",
    "evaluate_plugin",
    "production_plugin_contracts",
]
