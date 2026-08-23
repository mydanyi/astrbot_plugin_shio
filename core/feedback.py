from __future__ import annotations

import threading
import weakref
from dataclasses import dataclass
from enum import Enum
from typing import Collection


class FeedbackSignal(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class FeedbackConfidence(str, Enum):
    HIGH = "high"
    LOW = "low"


class FeedbackEvidenceSource(str, Enum):
    TARGET_FOLLOWUP = "target_followup"
    EXPLICIT_REPLY_REFERENCE = "explicit_reply_reference"
    PLATFORM_REACTION = "platform_reaction"
    ADJACENT_GROUP_MESSAGE = "adjacent_group_message"


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    eq=False,
    repr=False,
    weakref_slot=True,
)
class FeedbackEvidence:
    reviewer_key: str
    reply_id: str
    signal: FeedbackSignal
    source: FeedbackEvidenceSource
    confidence: FeedbackConfidence
    is_target_user: bool
    affects_target_profile: bool
    reply_trace_id: str = ""

    def trace_metadata(self) -> dict[str, str | bool]:
        try:
            inspect_feedback_evidence(self)
        except Exception:
            return {"feedback_evidence_canonical": False}
        return {
            "feedback_evidence_canonical": True,
            "signal": self.signal.value,
            "source": self.source.value,
            "confidence": self.confidence.value,
            "is_target_user": self.is_target_user,
            "affects_target_profile": self.affects_target_profile,
            "has_reviewer_key": bool(self.reviewer_key),
            "has_reply_id": bool(self.reply_id),
            "has_reply_trace_id": bool(self.reply_trace_id),
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        if not metadata.get("feedback_evidence_canonical", False):
            return "FeedbackEvidence(canonical=False)"
        return (
            "FeedbackEvidence(canonical=True, "
            f"signal={metadata['signal']!r}, "
            f"source={metadata['source']!r}, "
            f"confidence={metadata['confidence']!r}, identifiers_visible=False)"
        )


def _feedback_snapshot(evidence: FeedbackEvidence) -> tuple[object, ...]:
    if type(evidence) is not FeedbackEvidence:
        raise ValueError("feedback_evidence_not_canonical")
    try:
        values = (
            evidence.reviewer_key,
            evidence.reply_id,
            evidence.signal,
            evidence.source,
            evidence.confidence,
            evidence.is_target_user,
            evidence.affects_target_profile,
            evidence.reply_trace_id,
        )
    except (AttributeError, TypeError) as exc:
        raise ValueError("feedback_evidence_corrupt") from exc
    if (
        type(values[0]) is not str
        or not values[0]
        or len(values[0]) > 512
        or type(values[1]) is not str
        or len(values[1]) > 256
        or type(values[2]) is not FeedbackSignal
        or type(values[3]) is not FeedbackEvidenceSource
        or type(values[4]) is not FeedbackConfidence
        or type(values[5]) is not bool
        or type(values[6]) is not bool
        or type(values[7]) is not str
        or len(values[7]) > 256
    ):
        raise ValueError("feedback_evidence_corrupt")
    return values


def _build_feedback_vault():
    lock = threading.RLock()
    records: tuple[
        tuple[weakref.ReferenceType[FeedbackEvidence], tuple[object, ...]],
        ...,
    ] = ()

    def prune_locked() -> None:
        nonlocal records
        records = tuple(record for record in records if record[0]() is not None)

    def issue(**values: object) -> FeedbackEvidence:
        nonlocal records
        with lock:
            prune_locked()
            if len(records) >= 8192:
                raise ValueError("feedback_evidence_ledger_full")
            evidence = object.__new__(FeedbackEvidence)
            for name, value in values.items():
                object.__setattr__(evidence, name, value)
            snapshot = _feedback_snapshot(evidence)
            records = (*records, (weakref.ref(evidence), snapshot))
            return evidence

    def inspect(evidence: FeedbackEvidence) -> FeedbackEvidence:
        with lock:
            prune_locked()
            snapshot = _feedback_snapshot(evidence)
            matches = tuple(record for record in records if record[0]() is evidence)
            if len(matches) != 1:
                raise ValueError("feedback_evidence_not_canonical")
            if matches[0][1] != snapshot:
                raise ValueError("feedback_evidence_corrupt")
            return evidence

    return issue, inspect


_issue_feedback_evidence, inspect_feedback_evidence = _build_feedback_vault()
del _build_feedback_vault


def classify_feedback_evidence(
    *,
    reviewer_key: str,
    target_identity_key: str,
    reply_id: str,
    signal: FeedbackSignal,
    reply_to_message_id: str = "",
    reaction_to_message_id: str = "",
    sent_platform_message_ids: Collection[str] = (),
    reply_trace_id: str = "",
) -> FeedbackEvidence:
    reviewer = str(reviewer_key or "").strip()
    target = str(target_identity_key or "").strip()
    sent_ids = {
        str(value or "").strip()
        for value in sent_platform_message_ids
        if str(value or "").strip()
    }
    reply_to = str(reply_to_message_id or "").strip()
    reaction_to = str(reaction_to_message_id or "").strip()
    has_reply_id = bool(str(reply_id or "").strip())
    is_target = bool(reviewer and target and reviewer == target)

    if reaction_to:
        if has_reply_id and reaction_to in sent_ids:
            source = FeedbackEvidenceSource.PLATFORM_REACTION
            confidence = FeedbackConfidence.HIGH
        else:
            source = FeedbackEvidenceSource.ADJACENT_GROUP_MESSAGE
            confidence = FeedbackConfidence.LOW
    elif reply_to:
        if has_reply_id and reply_to in sent_ids:
            source = FeedbackEvidenceSource.EXPLICIT_REPLY_REFERENCE
            confidence = FeedbackConfidence.HIGH
        else:
            source = FeedbackEvidenceSource.ADJACENT_GROUP_MESSAGE
            confidence = FeedbackConfidence.LOW
    elif has_reply_id and is_target:
        source = FeedbackEvidenceSource.TARGET_FOLLOWUP
        confidence = FeedbackConfidence.HIGH
    else:
        source = FeedbackEvidenceSource.ADJACENT_GROUP_MESSAGE
        confidence = FeedbackConfidence.LOW

    return _issue_feedback_evidence(
        reviewer_key=reviewer,
        reply_id=str(reply_id or "").strip(),
        signal=signal,
        source=source,
        confidence=confidence,
        is_target_user=is_target,
        affects_target_profile=is_target and confidence == FeedbackConfidence.HIGH,
        reply_trace_id=str(reply_trace_id or "").strip(),
    )
