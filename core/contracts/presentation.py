from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..context_assembler import ReplyTarget
from ._validation import (
    ContractViolation,
    normalize_reason_codes,
    require_hex,
    require_safe_name,
    require_text,
)
from .behavior import _validate_target
from .binding import DecisionBinding


class ExpressionModality(str, Enum):
    TEXT = "text"
    TEXT_AND_MEME = "text_and_meme"
    MEME_ONLY = "meme_only"
    REACTION = "reaction"


@dataclass(frozen=True, slots=True)
class ExpressionIntent:
    binding: DecisionBinding
    reply_target: ReplyTarget | None
    modality: ExpressionModality
    social_act: str
    emotion_tags: tuple[str, ...] = ()
    max_bubbles: int = 1
    public_scope_key: str = ""
    meme_executor: str = ""
    max_meme_calls: int = 0
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "social_act", require_safe_name(self.social_act, "social_act"))
        object.__setattr__(self, "reason_codes", normalize_reason_codes(self.reason_codes))
        object.__setattr__(
            self,
            "emotion_tags",
            tuple(dict.fromkeys(require_safe_name(tag, "emotion_tag") for tag in self.emotion_tags)),
        )
        if self.reply_target is not None:
            _validate_target(self.binding, self.reply_target)
            if self.public_scope_key:
                raise ContractViolation("expression_target_and_public_scope_conflict")
        elif self.public_scope_key != self.binding.scope_key:
            raise ContractViolation("expression_target_or_public_scope_required")
        text_modes = {ExpressionModality.TEXT, ExpressionModality.TEXT_AND_MEME}
        if self.modality in text_modes:
            if not isinstance(self.max_bubbles, int) or not 1 <= self.max_bubbles <= 3:
                raise ContractViolation("expression_bubble_budget_invalid")
        elif self.max_bubbles != 0:
            raise ContractViolation("non_text_expression_bubbles_forbidden")
        meme_modes = {ExpressionModality.TEXT_AND_MEME, ExpressionModality.MEME_ONLY}
        if self.modality in meme_modes:
            if self.meme_executor != "meme_manager" or self.max_meme_calls != 1:
                raise ContractViolation("meme_executor_budget_invalid")
        elif self.meme_executor or self.max_meme_calls:
            raise ContractViolation("meme_fields_without_meme_modality")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "expression_modality": self.modality.value,
            "expression_social_act": self.social_act,
            "expression_emotion_count": len(self.emotion_tags),
            "expression_max_bubbles": self.max_bubbles,
            "expression_meme_planned": bool(self.meme_executor),
        }


class PresentationEffectKind(str, Enum):
    TEXT = "text"
    MEME = "meme"
    REACTION = "reaction"


class EffectStatus(str, Enum):
    PLANNED = "planned"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True, slots=True)
class PresentationEffectReceipt:
    binding: DecisionBinding
    effect_kind: PresentationEffectKind
    executor: str
    status: EffectStatus
    target_message_id: str
    attempt_count: int = 0
    success_count: int = 0
    failure_kind: str = ""
    external_receipt_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "executor", require_safe_name(self.executor, "presentation_executor"))
        if self.target_message_id != self.binding.current_message_id:
            raise ContractViolation("presentation_target_binding_mismatch")
        if not isinstance(self.attempt_count, int) or self.attempt_count < 0:
            raise ContractViolation("presentation_attempt_count_invalid")
        if not isinstance(self.success_count, int) or not 0 <= self.success_count <= self.attempt_count:
            raise ContractViolation("presentation_success_count_invalid")
        if self.status is EffectStatus.SUCCEEDED and self.success_count < 1:
            raise ContractViolation("presentation_success_without_receipt")
        if self.status is EffectStatus.FAILED and not self.failure_kind:
            raise ContractViolation("presentation_failure_kind_required")
        if self.status is not EffectStatus.FAILED and self.failure_kind:
            raise ContractViolation("presentation_failure_kind_unexpected")
        if self.external_receipt_digest:
            object.__setattr__(
                self,
                "external_receipt_digest",
                require_hex(self.external_receipt_digest, "external_receipt_digest", lengths=(64,)),
            )

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "presentation_effect_kind": self.effect_kind.value,
            "presentation_executor": self.executor,
            "presentation_status": self.status.value,
            "presentation_attempt_count": self.attempt_count,
            "presentation_success_count": self.success_count,
            "presentation_has_external_receipt": bool(self.external_receipt_digest),
        }


@dataclass(frozen=True, slots=True)
class PresentationReceipt:
    binding: DecisionBinding
    effects: tuple[PresentationEffectReceipt, ...]
    terminal: bool
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", normalize_reason_codes(self.reason_codes))
        if any(effect.binding != self.binding for effect in self.effects):
            raise ContractViolation("presentation_effect_binding_mismatch")
        if sum(effect.effect_kind is PresentationEffectKind.MEME for effect in self.effects) > 1:
            raise ContractViolation("multiple_meme_executors_forbidden")
        if self.terminal and any(effect.status is EffectStatus.PLANNED for effect in self.effects):
            raise ContractViolation("terminal_presentation_has_planned_effect")

    @property
    def succeeded(self) -> bool:
        return bool(self.effects) and all(
            effect.status in {EffectStatus.SUCCEEDED, EffectStatus.SUPPRESSED}
            for effect in self.effects
        ) and any(effect.status is EffectStatus.SUCCEEDED for effect in self.effects)

    def trace_metadata(self) -> dict[str, int | bool]:
        return {
            "presentation_effect_count": len(self.effects),
            "presentation_success_count": sum(
                effect.status is EffectStatus.SUCCEEDED for effect in self.effects
            ),
            "presentation_failure_count": sum(
                effect.status is EffectStatus.FAILED for effect in self.effects
            ),
            "presentation_terminal": self.terminal,
            "presentation_succeeded": self.succeeded,
        }

