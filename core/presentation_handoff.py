from __future__ import annotations

import hashlib
import threading
import weakref
from dataclasses import dataclass, field
from typing import NamedTuple

from .action_outcome import (
    ActionOutcomeAuthority,
    ActionOutcomeIntent,
    ActionOutcomeKind,
)
from .action_planner import PlannedAction
from .affect import AffectAppraisal
from .capability_policy import CapabilityPolicy
from .contracts import ActionKind, ExpressionIntent, ExpressionModality
from .output_validator_v2 import (
    OutputValidationReport,
    inspect_output_validation_report,
)
from .reply_composer import (
    ReplyComposerRequest,
    ReplyComposerResult,
    _inspect_canonical_reply_composer_request,
)
from .semantic_guard import (
    SemanticGuardContract,
    SemanticGuardPhase,
    inspect_semantic_guard_contract,
)


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class PresentationHandoff:
    """Opaque, module-minted proof of the exact validated visible bytes."""

    composer_request: ReplyComposerRequest = field(repr=False)
    semantic_contract: SemanticGuardContract = field(repr=False)
    composer_result: ReplyComposerResult = field(repr=False)
    validation_report: OutputValidationReport = field(repr=False)
    action_outcome: ActionOutcomeIntent | None = field(repr=False)
    action_outcome_authority: ActionOutcomeAuthority | None = field(repr=False)
    eligible: bool
    target_message_id: str
    final_visible_text: str = field(repr=False)
    final_segments: tuple[str, ...] = field(repr=False)
    final_text_digest: str
    final_segments_digest: str
    semantic_tags: tuple[str, ...]
    emotion_tags: tuple[str, ...]
    candidate_call_budget: int
    reason_codes: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            "PresentationHandoff("
            f"eligible={self.eligible!r}, "
            f"visible_chars={len(self.final_visible_text)!r}, "
            f"segment_count={len(self.final_segments)!r}, "
            f"has_digest={bool(self.final_text_digest)!r}, "
            f"semantic_tag_count={len(self.semantic_tags)!r}, "
            f"emotion_tag_count={len(self.emotion_tags)!r}, "
            f"candidate_call_budget={self.candidate_call_budget!r}, "
            f"reason_count={len(self.reason_codes)!r})"
        )

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "eligible": self.eligible,
            "has_final_text": bool(self.final_visible_text),
            "final_text_digest": self.final_text_digest,
            "final_segments_digest": self.final_segments_digest,
            "final_segment_count": len(self.final_segments),
            "semantic_tag_count": len(self.semantic_tags),
            "emotion_tag_count": len(self.emotion_tags),
            "candidate_call_budget": self.candidate_call_budget,
            "reason_count": len(self.reason_codes),
        }


def _digest(text: str) -> str:
    return hashlib.sha256(
        str(text or "").encode("utf-8", errors="replace")
    ).hexdigest()


def _segments_digest(segments: tuple[str, ...]) -> str:
    if type(segments) is not tuple or any(type(item) is not str for item in segments):
        raise ValueError("presentation_segments_invalid")
    digest = hashlib.sha256()
    for segment in segments:
        encoded = segment.encode("utf-8", errors="replace")
        digest.update(len(encoded).to_bytes(8, byteorder="big", signed=False))
        digest.update(encoded)
    return digest.hexdigest()


def _snapshot(handoff: PresentationHandoff) -> tuple[object, ...]:
    if type(handoff) is not PresentationHandoff:
        raise ValueError("presentation_handoff_invalid")
    return (
        handoff.eligible,
        handoff.target_message_id,
        handoff.final_visible_text,
        handoff.final_segments,
        handoff.final_text_digest,
        handoff.final_segments_digest,
        handoff.semantic_tags,
        handoff.emotion_tags,
        handoff.candidate_call_budget,
        handoff.reason_codes,
    )


class _PresentationRecord(NamedTuple):
    handoff_ref: weakref.ReferenceType
    request_ref: weakref.ReferenceType
    contract_ref: weakref.ReferenceType
    result_ref: weakref.ReferenceType
    validation_ref: weakref.ReferenceType
    outcome_ref: weakref.ReferenceType | None
    authority_ref: weakref.ReferenceType | None
    snapshot: tuple[object, ...]


def _build_presentation_vault():
    lock = threading.RLock()
    records: tuple[_PresentationRecord, ...] = ()
    capacity = 1024

    def prune_locked() -> None:
        nonlocal records
        if type(records) is not tuple:
            raise ValueError("presentation_vault_corrupt")
        live: list[_PresentationRecord] = []
        for record in records:
            if type(record) is not _PresentationRecord:
                raise ValueError("presentation_vault_corrupt")
            if record.handoff_ref() is None:
                continue
            live.append(record)
        records = tuple(live)

    def mint(
        *,
        composer_request: ReplyComposerRequest,
        semantic_contract: SemanticGuardContract,
        action_outcome: ActionOutcomeIntent | None,
        action_outcome_authority: ActionOutcomeAuthority | None,
        planned_action: PlannedAction,
        expression_intent: ExpressionIntent,
        affect_appraisal: AffectAppraisal,
        result: ReplyComposerResult,
        validation: OutputValidationReport,
        capability_policy: CapabilityPolicy,
    ) -> PresentationHandoff:
        values = _validated_presentation_values(
            composer_request=composer_request,
            semantic_contract=semantic_contract,
            action_outcome=action_outcome,
            action_outcome_authority=action_outcome_authority,
            planned_action=planned_action,
            expression_intent=expression_intent,
            affect_appraisal=affect_appraisal,
            result=result,
            validation=validation,
            capability_policy=capability_policy,
        )
        with lock:
            nonlocal records
            prune_locked()
            if len(records) >= capacity:
                raise ValueError("presentation_vault_full")
            handoff = object.__new__(PresentationHandoff)
            exact_values = {
                "composer_request": composer_request,
                "semantic_contract": semantic_contract,
                "composer_result": result,
                "validation_report": validation,
                "action_outcome": action_outcome,
                "action_outcome_authority": action_outcome_authority,
                **values,
            }
            for name, value in exact_values.items():
                object.__setattr__(handoff, name, value)
            records = (
                *records,
                _PresentationRecord(
                    weakref.ref(handoff),
                    weakref.ref(composer_request),
                    weakref.ref(semantic_contract),
                    weakref.ref(result),
                    weakref.ref(validation),
                    weakref.ref(action_outcome) if action_outcome is not None else None,
                    (
                        weakref.ref(action_outcome_authority)
                        if action_outcome_authority is not None
                        else None
                    ),
                    _snapshot(handoff),
                ),
            )
            return handoff

    def inspect(
        value: object,
        *,
        composer_request: ReplyComposerRequest,
        semantic_contract: SemanticGuardContract,
        action_outcome: ActionOutcomeIntent | None,
        action_outcome_authority: ActionOutcomeAuthority | None,
    ) -> PresentationHandoff:
        if type(value) is not PresentationHandoff:
            raise ValueError("presentation_handoff_not_canonical")
        with lock:
            prune_locked()
            matches = tuple(
                record for record in records if record.handoff_ref() is value
            )
            if len(matches) != 1:
                raise ValueError("presentation_handoff_not_canonical")
            record = matches[0]
        result = record.result_ref()
        validation = record.validation_ref()
        outcome = record.outcome_ref() if record.outcome_ref is not None else None
        authority = (
            record.authority_ref() if record.authority_ref is not None else None
        )
        if (
            record.request_ref() is not composer_request
            or record.contract_ref() is not semantic_contract
            or type(result) is not ReplyComposerResult
            or type(validation) is not OutputValidationReport
            or outcome is not action_outcome
            or authority is not action_outcome_authority
            or value.composer_request is not composer_request
            or value.semantic_contract is not semantic_contract
            or value.composer_result is not result
            or value.validation_report is not validation
            or value.action_outcome is not outcome
            or value.action_outcome_authority is not authority
            or _snapshot(value) != record.snapshot
            or value.final_text_digest != _digest(value.final_visible_text)
            or value.final_segments_digest != _segments_digest(value.final_segments)
            or "\n".join(value.final_segments) != value.final_visible_text
        ):
            raise ValueError("presentation_handoff_corrupt")
        _inspect_canonical_reply_composer_request(composer_request)
        inspect_semantic_guard_contract(
            semantic_contract,
            composer_request=composer_request,
        )
        semantic_report = validation.semantic_guard_report
        if semantic_report is None:
            raise ValueError("presentation_semantic_report_missing")
        inspect_output_validation_report(
            validation,
            composer_request=composer_request,
            require_passed=True if value.eligible else None,
            phase=semantic_report.phase,
            result=result,
            visible_text=result.visible_text,
        )
        return value

    def metrics() -> tuple[int, int, bool]:
        with lock:
            prune_locked()
            return len(records), capacity, len(records) <= capacity

    return mint, inspect, metrics


(
    _mint_presentation_handoff,
    _inspect_presentation_handoff,
    _presentation_vault_metrics,
) = _build_presentation_vault()
del _build_presentation_vault


def _inspect_canonical_presentation_handoff(
    value: object,
    *,
    composer_request: ReplyComposerRequest,
    semantic_contract: SemanticGuardContract,
    action_outcome: ActionOutcomeIntent | None,
    action_outcome_authority: ActionOutcomeAuthority | None,
) -> PresentationHandoff:
    """Narrow internal inspector; no raw/copy/rebuild registration exists."""

    return _inspect_presentation_handoff(
        value,
        composer_request=composer_request,
        semantic_contract=semantic_contract,
        action_outcome=action_outcome,
        action_outcome_authority=action_outcome_authority,
    )


def _validated_presentation_values(
    *,
    composer_request: ReplyComposerRequest,
    semantic_contract: SemanticGuardContract,
    action_outcome: ActionOutcomeIntent | None,
    action_outcome_authority: ActionOutcomeAuthority | None,
    planned_action: PlannedAction,
    expression_intent: ExpressionIntent,
    affect_appraisal: AffectAppraisal,
    result: ReplyComposerResult,
    validation: OutputValidationReport,
    capability_policy: CapabilityPolicy,
) -> dict[str, object]:
    """Compute fields after complete typed validation; mints no authority."""

    _inspect_canonical_reply_composer_request(composer_request)
    inspect_semantic_guard_contract(
        semantic_contract,
        composer_request=composer_request,
    )
    if (
        semantic_contract.action_outcome is not action_outcome
        or semantic_contract.action_outcome_authority is not action_outcome_authority
        or composer_request.action_outcome is not action_outcome
        or composer_request.action_outcome_authority is not action_outcome_authority
    ):
        raise ValueError("presentation_action_outcome_mismatch")
    semantic_report = validation.semantic_guard_report
    if semantic_report is None or semantic_report.phase not in {
        SemanticGuardPhase.INITIAL,
        SemanticGuardPhase.REPAIR,
    }:
        raise ValueError("presentation_validation_phase_invalid")
    inspect_output_validation_report(
        validation,
        composer_request=composer_request,
        require_passed=None,
        phase=semantic_report.phase,
        result=result,
        visible_text=result.visible_text,
    )

    reasons: list[str] = []
    visible = str(result.visible_text or "")
    segments = result.bubbles
    if type(planned_action) is not PlannedAction:
        raise TypeError("presentation_action_missing")
    binding = planned_action.binding
    target = planned_action.action.reply_target
    if planned_action is not composer_request.planned_action:
        reasons.append("presentation_action_identity_mismatch")
    if action_outcome is None:
        if planned_action.kind not in {ActionKind.REPLY, ActionKind.USE_TOOL}:
            reasons.append("presentation_action_not_textual")
    elif planned_action.kind is ActionKind.EXECUTE_ACTION:
        pass
    elif (
        planned_action.kind is ActionKind.REPLY
        and action_outcome.kind in {ActionOutcomeKind.DENIED, ActionOutcomeKind.CANCELLED}
    ):
        pass
    else:
        reasons.append("presentation_action_outcome_shape_invalid")
    if target is None:
        reasons.append("presentation_target_missing")
    if not validation.is_valid:
        reasons.append("final_output_not_validated")
    if not visible.strip():
        reasons.append("empty_final_output")
    if (
        type(segments) is not tuple
        or any(type(segment) is not str or not segment.strip() for segment in segments)
        or "\n".join(segments) != visible
    ):
        reasons.append("presentation_segment_boundary_mismatch")
    if result.target_message_id != binding.current_message_id:
        reasons.append("presentation_target_mismatch")
    if type(expression_intent) is not ExpressionIntent:
        reasons.append("presentation_expression_missing")
    else:
        if (
            expression_intent is not composer_request.expression_intent
            or expression_intent.binding != binding
            or expression_intent.reply_target != target
        ):
            reasons.append("presentation_expression_binding_mismatch")
        if expression_intent.modality not in {
            ExpressionModality.TEXT,
            ExpressionModality.TEXT_AND_MEME,
        }:
            reasons.append("presentation_non_text_deferred_to_p6")
    if type(affect_appraisal) is not AffectAppraisal:
        reasons.append("presentation_affect_missing")
    elif (
        affect_appraisal is not composer_request.affect_appraisal
        or affect_appraisal.target_message_id != binding.current_message_id
        or affect_appraisal.target_content_digest != binding.current_content_digest
        or affect_appraisal.focus_sender_key != binding.current_sender_key
        or not affect_appraisal.is_actionable
    ):
        reasons.append("presentation_affect_binding_mismatch")
    if type(capability_policy) is not CapabilityPolicy:
        reasons.append("presentation_capability_missing")
    elif (
        capability_policy is not composer_request.capability_policy
        or capability_policy.principal_key != binding.current_sender_key
        or capability_policy.conversation_mode != "direct_reply"
        or capability_policy.is_degraded
    ):
        reasons.append("presentation_principal_mismatch")
    reason_codes = tuple(dict.fromkeys(reasons))

    if reason_codes:
        values: dict[str, object] = {
            "eligible": False,
            "target_message_id": "",
            "final_visible_text": "",
            "final_segments": (),
            "final_text_digest": _digest(""),
            "final_segments_digest": _segments_digest(()),
            "semantic_tags": (),
            "emotion_tags": (),
            "candidate_call_budget": 0,
            "reason_codes": reason_codes,
        }
    else:
        emotion_tags = tuple(expression_intent.emotion_tags)
        semantic_tags = tuple(
            dict.fromkeys(
                (
                    expression_intent.social_act,
                    affect_appraisal.topic_return.value,
                )
            )
        )
        values = {
            "eligible": True,
            "target_message_id": binding.current_message_id,
            "final_visible_text": visible,
            "final_segments": segments,
            "final_text_digest": _digest(visible),
            "final_segments_digest": _segments_digest(segments),
            "semantic_tags": semantic_tags,
            "emotion_tags": emotion_tags,
            "candidate_call_budget": 0,
            "reason_codes": (),
        }
    return values


def build_presentation_handoff(
    *,
    composer_request: ReplyComposerRequest,
    semantic_contract: SemanticGuardContract,
    action_outcome: ActionOutcomeIntent | None,
    action_outcome_authority: ActionOutcomeAuthority | None,
    planned_action: PlannedAction,
    expression_intent: ExpressionIntent,
    affect_appraisal: AffectAppraisal,
    result: ReplyComposerResult,
    validation: OutputValidationReport,
    capability_policy: CapabilityPolicy,
) -> PresentationHandoff:
    """Mint an exact presentation snapshot from a canonical full validation."""

    return _mint_presentation_handoff(
        composer_request=composer_request,
        semantic_contract=semantic_contract,
        action_outcome=action_outcome,
        action_outcome_authority=action_outcome_authority,
        planned_action=planned_action,
        expression_intent=expression_intent,
        affect_appraisal=affect_appraisal,
        result=result,
        validation=validation,
        capability_policy=capability_policy,
    )


__all__ = ["PresentationHandoff", "build_presentation_handoff"]
