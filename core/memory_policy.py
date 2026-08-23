from __future__ import annotations

import asyncio
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from enum import Enum

from .context_assembler import ProvenancedFact
from .contracts import (
    ContractViolation,
    EvidenceConsumer,
    ExternalPluginEvidence,
    HistoryVisibility,
    IngressDecision,
    IngressDisposition,
    MemoryDecision,
    MemoryMode,
    PluginEvidenceKind,
    PluginEvidenceStatus,
    SenderKind,
)
from .conversation_event import ConversationEvent
from .plugin_adapters.livingmemory import (
    MAX_RECENT_LIMIT,
    LivingMemoryAdapter,
    LivingMemoryAdapterResult,
    LivingMemoryCandidate,
)


class MemoryPolicyIneligible(ContractViolation):
    """Raised before any read when the turn is not ACCEPT_HUMAN."""


class MemoryScope(str, Enum):
    """Closed visibility classes understood by Shio's memory consumer."""

    GROUP = "group"
    GROUP_BACKGROUND = "group_background"
    GLOBAL = "global"
    PUBLIC = "public"
    PERSONAL = "personal"
    OWNER_PRIVATE = "owner_private"


_GROUP_PUBLIC_SCOPES = {
    MemoryScope.GROUP.value,
    MemoryScope.GROUP_BACKGROUND.value,
    MemoryScope.GLOBAL.value,
    MemoryScope.PUBLIC.value,
}


def _normalized_exclusions(
    counts: Counter[str] | tuple[tuple[str, int], ...],
) -> tuple[tuple[str, int], ...]:
    source = counts.items() if isinstance(counts, Counter) else counts
    normalized: Counter[str] = Counter()
    for reason, count in source:
        key = str(reason or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_]+", key):
            raise ValueError("memory_exclusion_reason_invalid")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("memory_exclusion_count_invalid")
        normalized[key] += count
    return tuple(sorted(normalized.items()))


@dataclass(frozen=True, slots=True, repr=False)
class MemoryPolicyResult:
    """One safe wrapper around exactly one decision and one plugin evidence."""

    decision: MemoryDecision = field(repr=False)
    plugin_evidence: ExternalPluginEvidence = field(repr=False)
    current_subject_facts: tuple[ProvenancedFact, ...] = field(
        default=(),
        repr=False,
    )
    public_background_facts: tuple[ProvenancedFact, ...] = field(
        default=(),
        repr=False,
    )
    exclusion_counts: tuple[tuple[str, int], ...] = ()
    read_count: int = 0
    recent_record_count: int = 0
    provided_record_count: int = 0
    chat_type: str = ""
    relationship_role: str = ""
    current_message_precedence: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.decision, MemoryDecision):
            raise TypeError("memory_decision_required")
        if not isinstance(self.plugin_evidence, ExternalPluginEvidence):
            raise TypeError("memory_plugin_evidence_required")
        if self.plugin_evidence.binding != self.decision.binding:
            raise ContractViolation("memory_evidence_binding_mismatch")
        current = tuple(self.current_subject_facts)
        public = tuple(self.public_background_facts)
        object.__setattr__(self, "current_subject_facts", current)
        object.__setattr__(self, "public_background_facts", public)
        if self.decision.selected_facts != current + public:
            raise ContractViolation("memory_result_selection_mismatch")
        sender_key = self.decision.binding.current_sender_key
        if any(fact.subject_key != sender_key for fact in current):
            raise ContractViolation("memory_current_subject_mismatch")
        if any(
            fact.scope not in {
                MemoryScope.PERSONAL.value,
                MemoryScope.OWNER_PRIVATE.value,
            }
            for fact in current
        ):
            raise ContractViolation("memory_current_subject_scope_invalid")
        if any(fact.subject_key for fact in public):
            raise ContractViolation("memory_public_subject_forbidden")
        if any(fact.scope not in _GROUP_PUBLIC_SCOPES for fact in public):
            raise ContractViolation("memory_public_scope_invalid")
        if self.owner_private_facts and not (
            self.chat_type == "private" and self.relationship_role == "owner"
        ):
            raise ContractViolation("memory_owner_private_visibility_invalid")
        object.__setattr__(
            self,
            "exclusion_counts",
            _normalized_exclusions(self.exclusion_counts),
        )
        for name in (
            "read_count",
            "recent_record_count",
            "provided_record_count",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError(f"memory_{name}_invalid")
        if self.read_count > 1:
            raise ValueError("memory_read_budget_exceeded")
        if self.chat_type not in {"group", "private"}:
            raise ValueError("memory_chat_type_invalid")
        if self.relationship_role not in {"owner", "group_peer", "private_peer"}:
            raise ValueError("memory_relationship_role_invalid")
        if self.current_message_precedence is not True:
            raise ContractViolation("current_message_precedence_required")

    @property
    def context_order(self) -> tuple[str, ...]:
        order = ["current_message"]
        if self.personal_facts:
            order.append("memory_personal")
        if self.owner_private_facts:
            order.append("memory_owner_private")
        if self.group_public_facts:
            order.append("memory_group_public")
        return tuple(order)

    @property
    def personal_facts(self) -> tuple[ProvenancedFact, ...]:
        return tuple(
            fact
            for fact in self.current_subject_facts
            if fact.scope == MemoryScope.PERSONAL.value
        )

    @property
    def owner_private_facts(self) -> tuple[ProvenancedFact, ...]:
        return tuple(
            fact
            for fact in self.current_subject_facts
            if fact.scope == MemoryScope.OWNER_PRIVATE.value
        )

    @property
    def group_public_facts(self) -> tuple[ProvenancedFact, ...]:
        return self.public_background_facts

    def trace_metadata(self) -> dict[str, str | int | bool]:
        metadata: dict[str, str | int | bool] = {}
        metadata.update(self.decision.trace_metadata())
        metadata.update(self.plugin_evidence.trace_metadata())
        metadata.update(
            {
                "memory_read_count": self.read_count,
                "memory_recent_record_count": self.recent_record_count,
                "memory_provided_record_count": self.provided_record_count,
                "memory_current_subject_count": len(self.current_subject_facts),
                "memory_personal_count": len(self.personal_facts),
                "memory_owner_private_count": len(self.owner_private_facts),
                "memory_public_background_count": len(
                    self.public_background_facts
                ),
                "memory_exclusion_count": sum(
                    count for _, count in self.exclusion_counts
                ),
                "memory_chat_type": self.chat_type,
                "memory_relationship_role": self.relationship_role,
                "current_message_precedence": True,
            }
        )
        return metadata

    def __repr__(self) -> str:
        return (
            "MemoryPolicyResult("
            f"mode={self.decision.mode.value!r}, "
            f"plugin_status={self.plugin_evidence.status.value!r}, "
            f"facts={len(self.decision.selected_facts)}, "
            f"read_count={self.read_count}, "
            f"excluded={sum(count for _, count in self.exclusion_counts)})"
        )


def _plugin_evidence(
    decision: MemoryDecision,
    adapter_result: LivingMemoryAdapterResult,
) -> ExternalPluginEvidence:
    verified = adapter_result.status is PluginEvidenceStatus.VERIFIED
    return ExternalPluginEvidence(
        binding=decision.binding,
        plugin_id="livingmemory",
        evidence_kind=PluginEvidenceKind.MEMORY_REFERENCE,
        status=adapter_result.status,
        history_visibility=(
            HistoryVisibility.CURRENT_TURN_REFERENCE
            if verified
            else HistoryVisibility.DROP
        ),
        trusted=verified,
        allowed_consumers=(
            (EvidenceConsumer.CURRENT_TURN,) if verified else ()
        ),
        reason_codes=adapter_result.reason_codes,
    )


def _candidate_key(candidate: LivingMemoryCandidate) -> tuple[str, str, str]:
    fact = candidate.fact
    normalized_content = " ".join(fact.content.split()).casefold()
    return (fact.subject_key, fact.scope, normalized_content)


def _candidate_rank(candidate: LivingMemoryCandidate) -> tuple[float, float, float, str]:
    fact = candidate.fact
    return (
        candidate.relevance,
        fact.confidence,
        fact.observed_at,
        fact.source_id,
    )


def _dedupe_candidates(
    candidates: list[LivingMemoryCandidate],
    exclusions: Counter[str],
) -> list[LivingMemoryCandidate]:
    best: OrderedDict[tuple[str, str, str], LivingMemoryCandidate] = OrderedDict()
    for candidate in candidates:
        key = _candidate_key(candidate)
        previous = best.get(key)
        if previous is None:
            best[key] = candidate
            continue
        exclusions["duplicate"] += 1
        if _candidate_rank(candidate) > _candidate_rank(previous):
            best[key] = candidate
    return list(best.values())


class MemoryPolicy:
    """Per-turn single-flight memory selector with a hard one-read budget."""

    __slots__ = (
        "_cache",
        "_lock",
        "_max_cached_turns",
        "_minimum_personal_confidence",
        "_minimum_personal_relevance",
        "_minimum_public_confidence",
        "_minimum_public_relevance",
    )

    def __init__(
        self,
        *,
        max_cached_turns: int = 512,
        minimum_personal_confidence: float = 0.5,
        minimum_personal_relevance: float = 0.5,
        minimum_public_confidence: float = 0.5,
        minimum_public_relevance: float = 0.6,
    ) -> None:
        if (
            isinstance(max_cached_turns, bool)
            or not isinstance(max_cached_turns, int)
            or not 1 <= max_cached_turns <= 10_000
        ):
            raise ValueError("memory_cache_size_invalid")
        thresholds = (
            minimum_personal_confidence,
            minimum_personal_relevance,
            minimum_public_confidence,
            minimum_public_relevance,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0.0 <= float(value) <= 1.0
            for value in thresholds
        ):
            raise ValueError("memory_threshold_invalid")
        self._cache: OrderedDict[object, MemoryPolicyResult] = OrderedDict()
        self._lock = asyncio.Lock()
        self._max_cached_turns = max_cached_turns
        self._minimum_personal_confidence = float(
            minimum_personal_confidence
        )
        self._minimum_personal_relevance = float(minimum_personal_relevance)
        self._minimum_public_confidence = float(minimum_public_confidence)
        self._minimum_public_relevance = float(minimum_public_relevance)

    @staticmethod
    def _validate_turn(
        ingress_decision: IngressDecision,
        event: ConversationEvent,
    ) -> None:
        if not isinstance(ingress_decision, IngressDecision):
            raise TypeError("ingress_decision_required")
        if not isinstance(event, ConversationEvent):
            raise TypeError("conversation_event_required")
        if (
            ingress_decision.disposition
            is not IngressDisposition.ACCEPT_HUMAN
            or ingress_decision.sender_kind is not SenderKind.HUMAN
        ):
            raise MemoryPolicyIneligible("memory_requires_accept_human")
        if ingress_decision.binding != event.binding:
            raise ContractViolation("memory_ingress_binding_mismatch")
        if event.principal.sender_key != event.binding.current_sender_key:
            raise ContractViolation("memory_principal_binding_mismatch")
        if event.envelope.chat_type not in {"group", "private"}:
            raise ContractViolation("memory_chat_type_unsupported")

    def _select(
        self,
        adapter_result: LivingMemoryAdapterResult,
        *,
        event: ConversationEvent,
        max_results: int,
        exclusions: Counter[str],
    ) -> tuple[tuple[ProvenancedFact, ...], tuple[ProvenancedFact, ...]]:
        current: list[LivingMemoryCandidate] = []
        public: list[LivingMemoryCandidate] = []
        sender_key = event.binding.current_sender_key
        chat_type = event.envelope.chat_type
        for candidate in adapter_result.candidates:
            fact = candidate.fact
            if fact.subject_key:
                if fact.subject_key != sender_key:
                    exclusions["other_subject"] += 1
                    continue
                if fact.scope == MemoryScope.OWNER_PRIVATE.value:
                    if not event.principal.is_owner:
                        exclusions["owner_private_nonowner"] += 1
                        continue
                    if chat_type != "private":
                        exclusions["owner_private_group_forbidden"] += 1
                        continue
                elif fact.scope != MemoryScope.PERSONAL.value:
                    exclusions["subject_scope_invalid"] += 1
                    continue
                if fact.confidence < self._minimum_personal_confidence:
                    exclusions["low_personal_confidence"] += 1
                    continue
                if candidate.relevance < self._minimum_personal_relevance:
                    exclusions["low_personal_relevance"] += 1
                    continue
                current.append(candidate)
                continue

            if fact.scope not in _GROUP_PUBLIC_SCOPES:
                exclusions["unscoped_background"] += 1
                continue
            if chat_type == "private" and fact.scope in {"group", "group_background"}:
                exclusions["scope_mismatch"] += 1
                continue
            if fact.confidence < self._minimum_public_confidence:
                exclusions["low_public_confidence"] += 1
                continue
            if candidate.relevance < self._minimum_public_relevance:
                exclusions["low_public_relevance"] += 1
                continue
            public.append(candidate)

        current = _dedupe_candidates(current, exclusions)
        public = _dedupe_candidates(public, exclusions)
        current.sort(key=_candidate_rank, reverse=True)
        public.sort(key=_candidate_rank, reverse=True)
        ordered = [*current, *public]
        selected = ordered[:max_results]
        if len(ordered) > len(selected):
            exclusions["budget_exceeded"] += len(ordered) - len(selected)
        current_selected = tuple(
            candidate.fact
            for candidate in selected
            if candidate.fact.subject_key
        )
        public_selected = tuple(
            candidate.fact
            for candidate in selected
            if not candidate.fact.subject_key
        )
        return current_selected, public_selected

    async def decide(
        self,
        ingress_decision: IngressDecision,
        event: ConversationEvent,
        *,
        adapter: LivingMemoryAdapter,
        provided_recall: object = None,
        include_recent: bool = True,
        semantic_required: bool = False,
        max_results: int = 5,
        recent_limit: int | None = None,
    ) -> MemoryPolicyResult:
        self._validate_turn(ingress_decision, event)
        if not isinstance(adapter, LivingMemoryAdapter):
            raise TypeError("livingmemory_adapter_required")
        if type(include_recent) is not bool or type(semantic_required) is not bool:
            raise TypeError("memory_mode_flag_invalid")
        if (
            isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or not 1 <= max_results <= 20
        ):
            raise ValueError("memory_max_results_invalid")
        effective_recent_limit = (
            min(MAX_RECENT_LIMIT, max(2, max_results * 2))
            if recent_limit is None
            else recent_limit
        )
        if (
            isinstance(effective_recent_limit, bool)
            or not isinstance(effective_recent_limit, int)
            or not 1 <= effective_recent_limit <= MAX_RECENT_LIMIT
        ):
            raise ValueError("memory_recent_limit_invalid")

        binding = event.binding
        async with self._lock:
            cached = self._cache.get(binding)
            if cached is not None:
                self._cache.move_to_end(binding)
                return cached

            adapter_result = await adapter.collect(
                binding=binding,
                provided_recall=provided_recall,
                include_recent=include_recent,
                include_semantic=semantic_required,
                recent_limit=effective_recent_limit,
            )
            exclusions = Counter(dict(adapter_result.exclusion_counts))
            if adapter_result.status is not PluginEvidenceStatus.VERIFIED:
                mode = MemoryMode.DEGRADED
                current_facts: tuple[ProvenancedFact, ...] = ()
                public_facts: tuple[ProvenancedFact, ...] = ()
                decision_max_results = 0
                decision_reasons = (
                    f"livingmemory_{adapter_result.status.value}",
                    "current_message_precedence",
                )
            elif not include_recent and not semantic_required:
                mode = MemoryMode.SKIP
                current_facts = ()
                public_facts = ()
                decision_max_results = 0
                decision_reasons = (
                    "memory_not_requested",
                    "current_message_precedence",
                )
            else:
                mode = (
                    MemoryMode.SEMANTIC_RECALL
                    if semantic_required
                    else MemoryMode.RECENT_ONLY
                )
                current_facts, public_facts = self._select(
                    adapter_result,
                    event=event,
                    max_results=max_results,
                    exclusions=exclusions,
                )
                decision_max_results = max_results
                decision_reasons = (
                    f"memory_{mode.value}",
                    "current_message_precedence",
                )

            decision = MemoryDecision(
                binding=binding,
                mode=mode,
                selected_facts=current_facts + public_facts,
                max_results=decision_max_results,
                reason_codes=decision_reasons,
            )
            evidence = _plugin_evidence(decision, adapter_result)
            relationship_role = (
                "owner"
                if event.principal.is_owner
                else f"{event.envelope.chat_type}_peer"
            )
            result = MemoryPolicyResult(
                decision=decision,
                plugin_evidence=evidence,
                current_subject_facts=current_facts,
                public_background_facts=public_facts,
                exclusion_counts=tuple(exclusions.items()),
                read_count=adapter_result.read_count,
                recent_record_count=adapter_result.recent_record_count,
                provided_record_count=adapter_result.provided_record_count,
                chat_type=event.envelope.chat_type,
                relationship_role=relationship_role,
            )
            self._cache[binding] = result
            self._cache.move_to_end(binding)
            while len(self._cache) > self._max_cached_turns:
                self._cache.popitem(last=False)
            return result

    def __repr__(self) -> str:
        return (
            "MemoryPolicy("
            f"cached_turns={len(self._cache)}, "
            f"max_cached_turns={self._max_cached_turns})"
        )


def proactive_group_public_facts(
    result: MemoryPolicyResult,
) -> tuple[ProvenancedFact, ...]:
    """Return the only memory class eligible for a group proactive turn.

    The current P7 proactive runtime remains zero-memory.  This projection is
    the P8 boundary for any later integration: private chat, personal and
    owner-private facts cannot be projected.
    """

    if type(result) is not MemoryPolicyResult:
        raise TypeError("memory_policy_result_required")
    if result.chat_type != "group":
        return ()
    facts = result.group_public_facts
    if any(fact.subject_key or fact.scope not in _GROUP_PUBLIC_SCOPES for fact in facts):
        raise ContractViolation("proactive_public_memory_invalid")
    return facts


__all__ = [
    "MemoryPolicy",
    "MemoryPolicyIneligible",
    "MemoryPolicyResult",
    "MemoryScope",
    "proactive_group_public_facts",
]
