from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable

from .action_outcome import ActionOutcomeAuthority, ActionOutcomeIntent
from .action_planner import PlannedAction
from .context_assembler import ReplyTarget
from .contracts import (
    ContentIntent,
    ContentIntentKind,
    GroundingFact,
    MediaContext,
)
from .current_question_anchor import (
    CurrentQuestionAnchor,
    CurrentTurnKind,
)
from .memory_policy import MemoryPolicyResult


class ContentIntentBuildError(ValueError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class ContentIntentSeed:
    """Content decision fixed to one admitted turn before external grounding."""

    intent: ContentIntent = field(repr=False)
    anchor_atom_count: int
    memory_fact_count: int
    media_item_count: int
    action_outcome: ActionOutcomeIntent | None = field(repr=False, default=None)
    action_outcome_authority: ActionOutcomeAuthority | None = field(
        repr=False,
        default=None,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.intent, ContentIntent):
            raise TypeError("content_intent_required")
        if self.anchor_atom_count != len(self.intent.required_atoms):
            raise ContentIntentBuildError("content_anchor_count_mismatch")
        if self.media_item_count != len(self.intent.required_media_item_ids):
            raise ContentIntentBuildError("content_media_count_mismatch")
        if self.memory_fact_count < 0:
            raise ContentIntentBuildError("content_memory_count_invalid")
        if (self.action_outcome is None) is not (
            self.action_outcome_authority is None
        ):
            raise ContentIntentBuildError("content_action_outcome_pair_required")
        if self.action_outcome is not None:
            if type(self.action_outcome) is not ActionOutcomeIntent:
                raise TypeError("action_outcome_intent_invalid")
            if type(self.action_outcome_authority) is not ActionOutcomeAuthority:
                raise TypeError("action_outcome_authority_invalid")
            if self.intent.grounding_facts:
                raise ContentIntentBuildError(
                    "content_grounding_action_outcome_mutually_exclusive"
                )

    @property
    def binding(self):
        return self.intent.binding

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            **self.intent.trace_metadata(),
            "content_anchor_atom_count": self.anchor_atom_count,
            "content_memory_fact_count": self.memory_fact_count,
            "content_media_item_count": self.media_item_count,
            "content_current_anchor_precedence": True,
            "content_has_action_outcome": self.action_outcome is not None,
        }

    def __repr__(self) -> str:
        return (
            "ContentIntentSeed("
            f"kind={self.intent.kind.value!r}, "
            f"required_atoms={self.anchor_atom_count}, "
            f"memory_facts={self.memory_fact_count}, "
            f"media_items={self.media_item_count}, "
            f"grounding_facts={len(self.intent.grounding_facts)}, "
            f"has_action_outcome={self.action_outcome is not None})"
        )


def _kind_for_anchor(anchor: CurrentQuestionAnchor) -> ContentIntentKind:
    if anchor.turn_kind in {CurrentTurnKind.QUESTION, CurrentTurnKind.REQUEST}:
        return ContentIntentKind.ANSWER
    return ContentIntentKind.SOCIAL_RESPONSE


def _require_target_binding(
    anchor: CurrentQuestionAnchor,
    reply_target: ReplyTarget,
) -> None:
    if not isinstance(reply_target, ReplyTarget):
        raise TypeError("reply_target_required")
    binding = anchor.binding
    if (
        reply_target.message_id != binding.current_message_id
        or reply_target.sender_key != binding.current_sender_key
        or reply_target.session_id != binding.session_id
        or reply_target.scope_key != binding.scope_key
        or reply_target.content_digest != binding.current_content_digest
        or reply_target.source_kind not in {"inbound", "current_inbound"}
        or not reply_target.is_actionable
    ):
        raise ContentIntentBuildError("content_reply_target_binding_mismatch")


def build_content_intent_seed(
    *,
    anchor: CurrentQuestionAnchor,
    reply_target: ReplyTarget,
    memory_result: MemoryPolicyResult | None = None,
    media_context: MediaContext | None = None,
) -> ContentIntentSeed:
    """Freeze current-turn semantics; history and memory cannot add required atoms."""

    if not isinstance(anchor, CurrentQuestionAnchor):
        raise TypeError("current_question_anchor_required")
    _require_target_binding(anchor, reply_target)
    binding = anchor.binding

    memory_fact_count = 0
    if memory_result is not None:
        if not isinstance(memory_result, MemoryPolicyResult):
            raise TypeError("memory_policy_result_invalid")
        if memory_result.decision.binding != binding:
            raise ContentIntentBuildError("content_memory_binding_mismatch")
        if memory_result.current_message_precedence is not True:
            raise ContentIntentBuildError("content_memory_precedence_missing")
        memory_fact_count = len(memory_result.decision.selected_facts)

    if media_context is not None:
        if not isinstance(media_context, MediaContext):
            raise TypeError("media_context_invalid")
        if media_context.binding != binding:
            raise ContentIntentBuildError("content_media_binding_mismatch")
        media_item_ids = tuple(item.item_id for item in media_context.items)
        if media_item_ids != anchor.media_item_ids:
            raise ContentIntentBuildError("content_anchor_media_mismatch")
    elif anchor.media_item_ids:
        raise ContentIntentBuildError("content_media_context_required")

    reasons = [f"current_{anchor.turn_kind.value}"]
    reasons.append(
        "explicit_english" if anchor.answer_language == "en" else "default_chinese"
    )
    if memory_result is not None:
        reasons.append("screened_memory_available")
    if media_context is not None:
        reasons.append("typed_media_bound")

    intent = ContentIntent(
        binding=binding,
        reply_target=reply_target,
        kind=_kind_for_anchor(anchor),
        required_atoms=anchor.semantic_atoms,
        forbidden_atoms=(),
        required_media_item_ids=anchor.media_item_ids,
        grounding_facts=(),
        answer_language=anchor.answer_language,
        reason_codes=tuple(reasons),
    )
    return ContentIntentSeed(
        intent=intent,
        anchor_atom_count=len(anchor.semantic_atoms),
        memory_fact_count=memory_fact_count,
        media_item_count=len(anchor.media_item_ids),
    )


def attach_grounding_facts(
    seed: ContentIntentSeed,
    grounding_facts: Iterable[GroundingFact],
) -> ContentIntentSeed:
    """Attach verified same-turn evidence without changing the semantic seed."""

    if not isinstance(seed, ContentIntentSeed):
        raise TypeError("content_intent_seed_required")
    if seed.action_outcome is not None or seed.action_outcome_authority is not None:
        raise ContentIntentBuildError(
            "content_grounding_action_outcome_mutually_exclusive"
        )
    if seed.intent.grounding_facts:
        raise ContentIntentBuildError("content_intent_already_grounded")
    facts = tuple(grounding_facts)
    if any(not isinstance(fact, GroundingFact) for fact in facts):
        raise TypeError("grounding_fact_invalid")
    fact_ids = tuple(fact.fact_id for fact in facts)
    if len(set(fact_ids)) != len(fact_ids):
        raise ContentIntentBuildError("grounding_fact_duplicate")
    if any(fact.binding != seed.binding for fact in facts):
        raise ContentIntentBuildError("grounding_fact_binding_mismatch")
    if not facts:
        return seed

    intent = replace(
        seed.intent,
        grounding_facts=facts,
        reason_codes=tuple(
            dict.fromkeys((*seed.intent.reason_codes, "typed_grounding_attached"))
        ),
    )
    return ContentIntentSeed(
        intent=intent,
        anchor_atom_count=seed.anchor_atom_count,
        memory_fact_count=seed.memory_fact_count,
        media_item_count=seed.media_item_count,
    )


def attach_action_outcome(
    seed: ContentIntentSeed,
    outcome: ActionOutcomeIntent,
    authority: ActionOutcomeAuthority,
    *,
    current_planned_action: PlannedAction,
) -> ContentIntentSeed:
    """Attach one exact Controller outcome without consuming its authority.

    Action outcomes describe the status of an owner action.  They are never
    evidence facts and cannot share a ContentIntent with AnySearch grounding.
    The authority performs the canonical plan/source/continuation inspection;
    this builder only freezes the exact pair for the later Composer claim.
    """

    if type(seed) is not ContentIntentSeed:
        raise TypeError("content_intent_seed_required")
    if type(outcome) is not ActionOutcomeIntent:
        raise TypeError("action_outcome_intent_invalid")
    if type(authority) is not ActionOutcomeAuthority:
        raise TypeError("action_outcome_authority_invalid")
    if type(current_planned_action) is not PlannedAction:
        raise TypeError("planned_action_invalid")
    if seed.intent.grounding_facts:
        raise ContentIntentBuildError(
            "content_grounding_action_outcome_mutually_exclusive"
        )
    if seed.action_outcome is not None or seed.action_outcome_authority is not None:
        raise ContentIntentBuildError("content_action_outcome_already_attached")
    if (
        current_planned_action.binding != seed.binding
        or current_planned_action.action.reply_target != seed.intent.reply_target
    ):
        raise ContentIntentBuildError("content_action_outcome_plan_mismatch")
    inspected = authority.inspect_outcome(
        outcome,
        current_planned_action=current_planned_action,
    )
    if inspected is not outcome:
        raise ContentIntentBuildError("content_action_outcome_not_exact")
    return ContentIntentSeed(
        intent=seed.intent,
        anchor_atom_count=seed.anchor_atom_count,
        memory_fact_count=seed.memory_fact_count,
        media_item_count=seed.media_item_count,
        action_outcome=outcome,
        action_outcome_authority=authority,
    )


__all__ = [
    "ContentIntentBuildError",
    "ContentIntentSeed",
    "attach_action_outcome",
    "attach_grounding_facts",
    "build_content_intent_seed",
]
