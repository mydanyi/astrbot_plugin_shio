from __future__ import annotations

import hashlib
import threading
import time
import uuid
import weakref
from collections import deque
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, NamedTuple, Sequence

from .context_assembler import ReplyTarget
from .learning_cluster import LearningContext


class SegmentSendStatus(str, Enum):
    PLANNED = "planned"
    ATTEMPTED = "attempted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PlatformReceiptSource(str, Enum):
    """A source that explicitly returned the platform's own message ID.

    AstrBot's generic ``event.send`` contract currently provides neither source.
    These values reserve a typed boundary for a future adapter or framework hook;
    callers must never substitute time adjacency, text matching, or log parsing.
    """

    ASTRBOT_EXPLICIT_RESULT = "astrbot_explicit_result"
    ADAPTER_EXPLICIT_RESULT = "adapter_explicit_result"


@dataclass(frozen=True, slots=True)
class GenericSendCapability:
    event_send_returns_platform_message_id: bool
    message_event_result_contains_platform_message_id: bool
    context_send_returns_platform_message_id: bool
    portable_platform_receipt_available: bool


CURRENT_GENERIC_SEND_CAPABILITY = GenericSendCapability(
    event_send_returns_platform_message_id=False,
    message_event_result_contains_platform_message_id=False,
    context_send_returns_platform_message_id=False,
    portable_platform_receipt_available=False,
)


@dataclass(frozen=True, slots=True)
class SendSegmentAttempt:
    internal_reply_id: str
    segment_id: str
    segment_index: int
    visible_text: str
    visible_text_digest: str
    visible_text_length: int
    status: SegmentSendStatus
    attempted_at: float = 0.0
    completed_at: float = 0.0
    platform_message_id: str = ""
    platform_receipt_source: PlatformReceiptSource | None = None
    failure_kind: str = ""

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "segment_index": self.segment_index,
            "visible_text_length": self.visible_text_length,
            "status": self.status.value,
            "has_platform_message_id": bool(self.platform_message_id),
            "has_failure_kind": bool(self.failure_kind),
        }


@dataclass(frozen=True, slots=True)
class InternalReplySend:
    internal_reply_id: str
    target_message_id: str
    target_sender_key: str
    session_id: str
    scope_key: str
    target_content_digest: str
    target_source_kind: str
    target_referenced_message_id: str
    expression_ids: tuple[str, ...]
    trace_id: str
    segments: tuple[SendSegmentAttempt, ...]
    created_at: float

    @property
    def all_succeeded(self) -> bool:
        return bool(self.segments) and all(
            segment.status == SegmentSendStatus.SUCCEEDED for segment in self.segments
        )

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "segment_count": len(self.segments),
            "succeeded_count": sum(
                segment.status == SegmentSendStatus.SUCCEEDED
                for segment in self.segments
            ),
            "failed_count": sum(
                segment.status == SegmentSendStatus.FAILED for segment in self.segments
            ),
            "all_succeeded": self.all_succeeded,
            "has_target_message_id": bool(self.target_message_id),
            "has_target_sender_key": bool(self.target_sender_key),
        }


@dataclass(frozen=True, slots=True)
class SentReplyRecord:
    """Immutable view of one reply's real platform send attempts."""

    reply_id: str
    target_message_id: str
    target_sender_key: str
    session_id: str
    scope_key: str
    target_content_digest: str
    target_source_kind: str
    target_referenced_message_id: str
    segments: tuple[SendSegmentAttempt, ...]
    expression_ids: tuple[str, ...]
    trace_id: str
    created_at: float
    sent_at: float

    @property
    def successful_segments(self) -> tuple[SendSegmentAttempt, ...]:
        return tuple(
            segment
            for segment in self.segments
            if segment.status == SegmentSendStatus.SUCCEEDED
        )

    @property
    def successful_visible_text(self) -> str:
        return "\n".join(segment.visible_text for segment in self.successful_segments)

    @property
    def has_failed_segments(self) -> bool:
        return any(
            segment.status == SegmentSendStatus.FAILED for segment in self.segments
        )

    @property
    def is_terminal(self) -> bool:
        return bool(self.segments) and all(
            segment.status in {SegmentSendStatus.SUCCEEDED, SegmentSendStatus.FAILED}
            for segment in self.segments
        )

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "segment_count": len(self.segments),
            "succeeded_count": len(self.successful_segments),
            "failed_count": sum(
                segment.status == SegmentSendStatus.FAILED for segment in self.segments
            ),
            "has_failed_segments": self.has_failed_segments,
            "is_terminal": self.is_terminal,
            "has_target_message_id": bool(self.target_message_id),
            "has_target_sender_key": bool(self.target_sender_key),
            "has_platform_message_id": any(
                bool(segment.platform_message_id) for segment in self.segments
            ),
            "expression_count": len(self.expression_ids),
            "successful_visible_chars": len(self.successful_visible_text),
            "has_trace_id": bool(self.trace_id),
        }


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class OwnerActionSendTerminalEvidence:
    """Opaque proof that one exact action presentation reached send success."""

    segment_count: int
    completed_at: float = field(repr=False)
    all_succeeded: bool

    def trace_metadata(self) -> dict[str, int | bool]:
        return _send_evidence_trace(self)

    def __repr__(self) -> str:
        try:
            metadata = self.trace_metadata()
        except Exception:
            return "OwnerActionSendTerminalEvidence(canonical=False)"
        return (
            "OwnerActionSendTerminalEvidence("
            f"canonical=True, segment_count={metadata['segment_count']!r}, "
            "all_succeeded=True, content_visible=False)"
        )


class _OwnerActionSendTerminalInspection(NamedTuple):
    presentation: object
    composer_request: object
    action_outcome: object
    action_outcome_authority: object
    segment_count: int


class _PresentationSendRecord(NamedTuple):
    ledger_ref: weakref.ReferenceType
    presentation_ref: weakref.ReferenceType
    internal_reply_id: str
    presentation_snapshot: tuple[object, ...]
    binding_snapshot: tuple[object, ...]
    evidence_ref: weakref.ReferenceType | None
    evidence_snapshot: tuple[object, ...] | None


def _send_evidence_snapshot(
    evidence: OwnerActionSendTerminalEvidence,
) -> tuple[object, ...]:
    if type(evidence) is not OwnerActionSendTerminalEvidence:
        raise ValueError("owner_action_send_evidence_not_canonical")
    try:
        segment_count = object.__getattribute__(evidence, "segment_count")
        completed_at = object.__getattribute__(evidence, "completed_at")
        all_succeeded = object.__getattribute__(evidence, "all_succeeded")
    except AttributeError as exc:
        raise ValueError("owner_action_send_evidence_corrupt") from exc
    if (
        type(segment_count) is not int
        or segment_count <= 0
        or type(completed_at) is not float
        or completed_at < 0.0
        or type(all_succeeded) is not bool
        or not all_succeeded
    ):
        raise ValueError("owner_action_send_evidence_corrupt")
    return (segment_count, completed_at, all_succeeded)


def _reply_binding_snapshot(record: InternalReplySend) -> tuple[object, ...]:
    if type(record) is not InternalReplySend:
        raise ValueError("owner_action_send_reply_corrupt")
    segments: list[tuple[object, ...]] = []
    for segment in record.segments:
        if type(segment) is not SendSegmentAttempt:
            raise ValueError("owner_action_send_reply_corrupt")
        segments.append(
            (
                segment.internal_reply_id,
                segment.segment_id,
                segment.segment_index,
                segment.visible_text,
                segment.visible_text_digest,
                segment.visible_text_length,
            )
        )
    return (
        record.internal_reply_id,
        record.target_message_id,
        record.target_sender_key,
        record.session_id,
        record.scope_key,
        record.target_content_digest,
        record.target_source_kind,
        record.target_referenced_message_id,
        record.expression_ids,
        record.trace_id,
        tuple(segments),
    )


def _presentation_send_snapshot(presentation: object) -> tuple[object, ...]:
    from .presentation_handoff import PresentationHandoff

    if type(presentation) is not PresentationHandoff:
        raise ValueError("owner_action_presentation_not_canonical")
    try:
        return (
            object.__getattribute__(presentation, "composer_request"),
            object.__getattribute__(presentation, "semantic_contract"),
            object.__getattribute__(presentation, "action_outcome"),
            object.__getattribute__(presentation, "action_outcome_authority"),
            object.__getattribute__(presentation, "eligible"),
            object.__getattribute__(presentation, "target_message_id"),
            object.__getattribute__(presentation, "final_visible_text"),
            object.__getattribute__(presentation, "final_segments"),
            object.__getattribute__(presentation, "final_text_digest"),
            object.__getattribute__(presentation, "final_segments_digest"),
            object.__getattribute__(presentation, "reason_codes"),
        )
    except AttributeError as exc:
        raise ValueError("owner_action_presentation_corrupt") from exc


def _build_owner_action_send_evidence_vault():
    lock = threading.RLock()
    records: tuple[_PresentationSendRecord, ...] = ()
    capacity = 1024

    def prune_locked() -> None:
        nonlocal records
        if type(records) is not tuple:
            raise ValueError("owner_action_send_evidence_vault_corrupt")
        live: list[_PresentationSendRecord] = []
        for record in records:
            if type(record) is not _PresentationSendRecord:
                raise ValueError("owner_action_send_evidence_vault_corrupt")
            if record.ledger_ref() is None or record.presentation_ref() is None:
                continue
            live.append(record)
        records = tuple(live)

    def register(
        ledger: object,
        presentation: object,
        reply: InternalReplySend,
    ) -> None:
        nonlocal records
        with lock:
            prune_locked()
            if len(records) >= capacity:
                raise ValueError("owner_action_send_evidence_vault_full")
            if any(
                item.ledger_ref() is ledger
                and item.internal_reply_id == reply.internal_reply_id
                for item in records
            ):
                raise ValueError("owner_action_send_reply_already_bound")
            records = (
                *records,
                _PresentationSendRecord(
                    ledger_ref=weakref.ref(ledger),
                    presentation_ref=weakref.ref(presentation),
                    internal_reply_id=reply.internal_reply_id,
                    presentation_snapshot=_presentation_send_snapshot(presentation),
                    binding_snapshot=_reply_binding_snapshot(reply),
                    evidence_ref=None,
                    evidence_snapshot=None,
                ),
            )

    def binding_for(
        ledger: object,
        presentation: object,
        internal_reply_id: str,
    ) -> _PresentationSendRecord:
        with lock:
            prune_locked()
            matches = tuple(
                record
                for record in records
                if record.ledger_ref() is ledger
                and record.presentation_ref() is presentation
                and record.internal_reply_id == internal_reply_id
            )
            if len(matches) != 1:
                raise ValueError("owner_action_send_reply_not_canonical")
            return matches[0]

    def held(ledger: object, internal_reply_id: str) -> bool:
        with lock:
            prune_locked()
            return any(
                record.ledger_ref() is ledger
                and record.internal_reply_id == internal_reply_id
                for record in records
            )

    def issue(
        ledger: object,
        presentation: object,
        reply: InternalReplySend,
    ) -> OwnerActionSendTerminalEvidence:
        nonlocal records
        with lock:
            prune_locked()
            current = binding_for(
                ledger,
                presentation,
                reply.internal_reply_id,
            )
            if _reply_binding_snapshot(reply) != current.binding_snapshot:
                raise ValueError("owner_action_send_reply_corrupt")
            if current.evidence_ref is not None:
                existing = current.evidence_ref()
                if (
                    type(existing) is not OwnerActionSendTerminalEvidence
                    or current.evidence_snapshot != _send_evidence_snapshot(existing)
                ):
                    raise ValueError("owner_action_send_evidence_vault_corrupt")
                return existing
            completed_at = max(segment.completed_at for segment in reply.segments)
            evidence = object.__new__(OwnerActionSendTerminalEvidence)
            object.__setattr__(evidence, "segment_count", len(reply.segments))
            object.__setattr__(evidence, "completed_at", float(completed_at))
            object.__setattr__(evidence, "all_succeeded", True)
            snapshot = _send_evidence_snapshot(evidence)
            replacement = current._replace(
                evidence_ref=weakref.ref(evidence),
                evidence_snapshot=snapshot,
            )
            records = tuple(
                replacement if item is current else item for item in records
            )
            return evidence

    def inspect_record(
        evidence: object,
        ledger: object | None = None,
        presentation: object | None = None,
    ) -> _PresentationSendRecord:
        if type(evidence) is not OwnerActionSendTerminalEvidence:
            raise ValueError("owner_action_send_evidence_not_canonical")
        with lock:
            prune_locked()
            matches = tuple(
                record
                for record in records
                if record.evidence_ref is not None
                and record.evidence_ref() is evidence
            )
            if len(matches) != 1:
                raise ValueError("owner_action_send_evidence_not_canonical")
            record = matches[0]
            if (
                record.evidence_snapshot != _send_evidence_snapshot(evidence)
                or (ledger is not None and record.ledger_ref() is not ledger)
                or (
                    presentation is not None
                    and record.presentation_ref() is not presentation
                )
            ):
                raise ValueError("owner_action_send_evidence_corrupt")
            return record

    def trace(evidence: OwnerActionSendTerminalEvidence) -> dict[str, int | bool]:
        record = inspect_record(evidence)
        snapshot = record.evidence_snapshot
        if type(snapshot) is not tuple or len(snapshot) != 3:
            raise ValueError("owner_action_send_evidence_vault_corrupt")
        return {
            "schema_version": 1,
            "segment_count": snapshot[0],
            "all_succeeded": True,
            "content_visible": False,
        }

    return register, binding_for, held, issue, inspect_record, trace


(
    _register_presentation_send,
    _presentation_send_binding,
    _presentation_send_held,
    _issue_send_evidence,
    _inspect_send_evidence_record,
    _send_evidence_trace,
) = _build_owner_action_send_evidence_vault()
del _build_owner_action_send_evidence_vault


def _inspect_presentation_for_send(value: object):
    from .presentation_handoff import (
        PresentationHandoff,
        _inspect_canonical_presentation_handoff,
    )

    if type(value) is not PresentationHandoff:
        raise TypeError("owner_action_presentation_required")
    try:
        request = object.__getattribute__(value, "composer_request")
        contract = object.__getattribute__(value, "semantic_contract")
        outcome = object.__getattribute__(value, "action_outcome")
        authority = object.__getattribute__(value, "action_outcome_authority")
    except AttributeError as exc:
        raise ValueError("owner_action_presentation_corrupt") from exc
    handoff = _inspect_canonical_presentation_handoff(
        value,
        composer_request=request,
        semantic_contract=contract,
        action_outcome=outcome,
        action_outcome_authority=authority,
    )
    if not handoff.eligible or not handoff.final_segments:
        raise ValueError("owner_action_presentation_not_sendable")
    return handoff


@dataclass(slots=True)
class ReplyObservationTracker:
    internal_reply_id: str
    target_sender_id: str
    expression_ids: tuple[str, ...]
    target_sequence: int
    feedback_window_seconds: float
    trace_id: str = ""
    learning_context: LearningContext | None = None
    successful_segments: list[str] = field(default_factory=list)
    pending_automatic_segment_id: str = ""
    pending_automatic_text: str = ""

    @property
    def successful_visible_text(self) -> str:
        return "\n".join(self.successful_segments)


def _text_digest(text: str) -> str:
    # This is a binding digest, not a display/log fingerprint.  Keep the full
    # SHA-256 so it can be compared exactly with ConversationLedger content.
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


class InternalSendReceiptLedger:
    """Map internal replies to explicit per-segment send outcomes.

    The ledger deliberately has no fuzzy lookup by text, time, or logs. Platform
    message IDs can only enter through :class:`PlatformReceiptSource`.
    """

    def __init__(
        self,
        *,
        id_factory: Callable[[], str] | None = None,
        now_fn: Callable[[], float] | None = None,
        max_replies: int = 512,
    ) -> None:
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._now_fn = now_fn or time.time
        self._max_replies = max(16, int(max_replies))
        self._replies: dict[str, InternalReplySend] = {}
        self._segment_to_reply: dict[str, str] = {}
        self._reply_order: deque[str] = deque()
        self._lock = threading.RLock()

    def begin_reply(
        self,
        *,
        target: ReplyTarget,
        visible_segments: Sequence[str],
        expression_ids: Sequence[str] = (),
        trace_id: str = "",
    ) -> InternalReplySend:
        with self._lock:
            return self._begin_reply_locked(
                target=target,
                visible_segments=visible_segments,
                expression_ids=expression_ids,
                trace_id=trace_id,
            )

    def _begin_reply_locked(
        self,
        *,
        target: ReplyTarget,
        visible_segments: Sequence[str],
        expression_ids: Sequence[str] = (),
        trace_id: str = "",
    ) -> InternalReplySend:
        if not target.message_id or not target.sender_key:
            raise ValueError("send receipt requires an exact target principal")
        segments = tuple(str(value or "").strip() for value in visible_segments)
        if not segments or any(not value for value in segments):
            raise ValueError("send receipt requires non-empty visible segments")
        internal_reply_id = f"shio-{str(self._id_factory()).strip()}"
        if internal_reply_id == "shio-" or internal_reply_id in self._replies:
            raise ValueError("internal reply ID must be non-empty and unique")
        created_at = float(self._now_fn())
        attempts = tuple(
            SendSegmentAttempt(
                internal_reply_id=internal_reply_id,
                segment_id=f"{internal_reply_id}:{index}",
                segment_index=index,
                visible_text=text,
                visible_text_digest=_text_digest(text),
                visible_text_length=len(text),
                status=SegmentSendStatus.PLANNED,
            )
            for index, text in enumerate(segments)
        )
        record = InternalReplySend(
            internal_reply_id=internal_reply_id,
            target_message_id=target.message_id,
            target_sender_key=target.sender_key,
            session_id=target.session_id,
            scope_key=target.scope_key,
            target_content_digest=target.content_digest,
            target_source_kind=target.source_kind,
            target_referenced_message_id=target.referenced_message_id,
            expression_ids=tuple(
                dict.fromkeys(str(value) for value in expression_ids if str(value))
            )[:3],
            trace_id=str(trace_id or "").strip(),
            segments=attempts,
            created_at=created_at,
        )
        self._evict_for_new_reply()
        self._replies[internal_reply_id] = record
        self._reply_order.append(internal_reply_id)
        for segment in attempts:
            self._segment_to_reply[segment.segment_id] = internal_reply_id
        return record

    def begin_presentation_reply(
        self,
        presentation: object,
        *,
        expression_ids: Sequence[str] = (),
        trace_id: str = "",
    ) -> InternalReplySend:
        """Bind a reply to one exact canonical final presentation."""

        handoff = _inspect_presentation_for_send(presentation)
        request = handoff.composer_request
        planned_action = request.planned_action
        target = planned_action.action.reply_target
        if type(target) is not ReplyTarget:
            raise ValueError("owner_action_send_target_invalid")
        with self._lock:
            record = self._begin_reply_locked(
                target=target,
                visible_segments=handoff.final_segments,
                expression_ids=expression_ids,
                trace_id=trace_id,
            )
            try:
                _register_presentation_send(self, handoff, record)
            except BaseException:
                self._remove_reply_locked(record.internal_reply_id)
                raise
            return record

    def begin_proactive_presentation_reply(
        self,
        presentation: object,
    ) -> InternalReplySend:
        """Bind one exact proactive group presentation without a fake user target."""

        from .proactive_runtime import (
            claim_proactive_presentation_for_send,
            inspect_proactive_presentation_for_send,
        )

        handoff = inspect_proactive_presentation_for_send(presentation)
        target = handoff.target
        try:
            platform_id = target.platform_id
            bot_id = target.bot_id
            group_id = target.group_id
            origin = target.unified_msg_origin
            scope_key = target.scope_key
            topic_digest = handoff.request.plan.topic_digest
        except AttributeError as exc:
            raise ValueError("proactive_send_target_corrupt") from exc
        if (
            any(type(value) is not str or not value for value in (
                platform_id,
                bot_id,
                group_id,
                origin,
                scope_key,
                topic_digest,
            ))
            or type(handoff.final_segments) is not tuple
            or not 1 <= len(handoff.final_segments) <= 3
        ):
            raise ValueError("proactive_send_target_invalid")
        with self._lock:
            internal_reply_id = f"shio-{str(self._id_factory()).strip()}"
            if internal_reply_id == "shio-" or internal_reply_id in self._replies:
                raise ValueError("internal reply ID must be non-empty and unique")
            created_at = float(self._now_fn())
            attempts = tuple(
                SendSegmentAttempt(
                    internal_reply_id=internal_reply_id,
                    segment_id=f"{internal_reply_id}:{index}",
                    segment_index=index,
                    visible_text=text,
                    visible_text_digest=_text_digest(text),
                    visible_text_length=len(text),
                    status=SegmentSendStatus.PLANNED,
                )
                for index, text in enumerate(handoff.final_segments)
            )
            record = InternalReplySend(
                internal_reply_id=internal_reply_id,
                target_message_id="",
                target_sender_key="",
                session_id=origin,
                scope_key=scope_key,
                target_content_digest=topic_digest,
                target_source_kind="proactive_group",
                target_referenced_message_id="",
                expression_ids=(),
                trace_id=f"proactive-{topic_digest[:32]}",
                segments=attempts,
                created_at=created_at,
            )
            self._evict_for_new_reply()
            self._replies[internal_reply_id] = record
            self._reply_order.append(internal_reply_id)
            for segment in attempts:
                self._segment_to_reply[segment.segment_id] = internal_reply_id
            try:
                claim_proactive_presentation_for_send(
                    handoff,
                    ledger=self,
                    reply=record,
                )
            except BaseException:
                self._remove_reply_locked(internal_reply_id)
                raise
            return record

    def get_reply(self, internal_reply_id: str) -> InternalReplySend | None:
        with self._lock:
            return self._replies.get(str(internal_reply_id or "").strip())

    def sent_reply_record(self, internal_reply_id: str) -> SentReplyRecord | None:
        with self._lock:
            record = self._replies.get(str(internal_reply_id or "").strip())
            if record is None:
                return None
            sent_at = max(
                (
                    segment.completed_at
                    for segment in record.segments
                    if segment.status == SegmentSendStatus.SUCCEEDED
                ),
                default=0.0,
            )
            return SentReplyRecord(
                reply_id=record.internal_reply_id,
                target_message_id=record.target_message_id,
                target_sender_key=record.target_sender_key,
                session_id=record.session_id,
                scope_key=record.scope_key,
                target_content_digest=record.target_content_digest,
                target_source_kind=record.target_source_kind,
                target_referenced_message_id=record.target_referenced_message_id,
                segments=record.segments,
                expression_ids=record.expression_ids,
                trace_id=record.trace_id,
                created_at=record.created_at,
                sent_at=sent_at,
            )

    def proactive_sent_reply_record(
        self,
        presentation: object,
        *,
        internal_reply_id: str,
    ) -> SentReplyRecord:
        """Return a successful exact proactive send only to its presentation."""

        from .proactive_runtime import inspect_proactive_presentation_for_send

        handoff = inspect_proactive_presentation_for_send(presentation)
        authority = handoff._authority_ref()
        authority.inspect_completed_send(
            handoff,
            ledger=self,
            internal_reply_id=internal_reply_id,
        )
        record = self.sent_reply_record(internal_reply_id)
        if (
            type(record) is not SentReplyRecord
            or record.target_source_kind != "proactive_group"
            or record.session_id != handoff.target.unified_msg_origin
            or record.scope_key != handoff.target.scope_key
            or record.target_content_digest != handoff.request.plan.topic_digest
            or not record.is_terminal
            or not record.successful_segments
            or any(
                segment.status is not SegmentSendStatus.SUCCEEDED
                or segment.visible_text != handoff.final_segments[segment.segment_index]
                for segment in record.segments
            )
        ):
            raise ValueError("proactive_send_receipt_invalid")
        return record

    def append_segment(
        self,
        internal_reply_id: str,
        *,
        visible_text: str,
    ) -> SendSegmentAttempt:
        with self._lock:
            return self._append_segment_locked(
                internal_reply_id,
                visible_text=visible_text,
            )

    def _append_segment_locked(
        self,
        internal_reply_id: str,
        *,
        visible_text: str,
    ) -> SendSegmentAttempt:
        key = str(internal_reply_id or "").strip()
        record = self._replies.get(key)
        if record is None:
            raise KeyError("unknown internal reply")
        text = str(visible_text or "").strip()
        if not text:
            raise ValueError("send receipt requires a non-empty visible segment")
        index = len(record.segments)
        segment = SendSegmentAttempt(
            internal_reply_id=key,
            segment_id=f"{key}:{index}",
            segment_index=index,
            visible_text=text,
            visible_text_digest=_text_digest(text),
            visible_text_length=len(text),
            status=SegmentSendStatus.PLANNED,
        )
        self._replies[key] = replace(record, segments=(*record.segments, segment))
        self._segment_to_reply[segment.segment_id] = key
        return segment

    def _evict_for_new_reply(self) -> None:
        while len(self._replies) >= self._max_replies and self._reply_order:
            terminal_key = next(
                (
                    key
                    for key in self._reply_order
                    if (record := self._replies.get(key)) is not None
                    and not _presentation_send_held(self, key)
                    and record.segments
                    and all(
                        segment.status
                        in {SegmentSendStatus.SUCCEEDED, SegmentSendStatus.FAILED}
                        for segment in record.segments
                    )
                ),
                None,
            )
            if terminal_key is None:
                raise ValueError("send_receipt_ledger_full")
            self._remove_reply_locked(terminal_key)

    def _remove_reply_locked(self, internal_reply_id: str) -> None:
        if internal_reply_id in self._reply_order:
            self._reply_order.remove(internal_reply_id)
        record = self._replies.pop(internal_reply_id, None)
        if record is not None:
            for segment in record.segments:
                self._segment_to_reply.pop(segment.segment_id, None)

    def mark_attempted(self, segment_id: str) -> InternalReplySend:
        with self._lock:
            return self._replace_segment(
                segment_id,
                expected=SegmentSendStatus.PLANNED,
                update=lambda segment: replace(
                    segment,
                    status=SegmentSendStatus.ATTEMPTED,
                    attempted_at=float(self._now_fn()),
                ),
            )

    def mark_succeeded(
        self,
        segment_id: str,
        *,
        platform_message_id: str = "",
        receipt_source: PlatformReceiptSource | None = None,
    ) -> InternalReplySend:
        platform_id = str(platform_message_id or "").strip()
        if bool(platform_id) != bool(receipt_source):
            raise ValueError(
                "platform message ID and explicit receipt source must be supplied together"
            )
        with self._lock:
            return self._replace_segment(
                segment_id,
                expected=SegmentSendStatus.ATTEMPTED,
                update=lambda segment: replace(
                    segment,
                    status=SegmentSendStatus.SUCCEEDED,
                    completed_at=float(self._now_fn()),
                    platform_message_id=platform_id,
                    platform_receipt_source=receipt_source,
                    failure_kind="",
                ),
            )

    def mark_failed(self, segment_id: str, *, failure_kind: str) -> InternalReplySend:
        kind = str(failure_kind or "").strip()
        if not kind:
            raise ValueError("failed sends require a non-content failure kind")
        with self._lock:
            return self._replace_segment(
                segment_id,
                expected=SegmentSendStatus.ATTEMPTED,
                update=lambda segment: replace(
                    segment,
                    status=SegmentSendStatus.FAILED,
                    completed_at=float(self._now_fn()),
                    failure_kind=kind[:80],
                ),
            )

    def issue_owner_action_send_terminal_evidence(
        self,
        presentation: object,
        *,
        internal_reply_id: str,
    ) -> OwnerActionSendTerminalEvidence:
        """Seal one exact all-success presentation; public records are invalid."""

        handoff = _inspect_presentation_for_send(presentation)
        key = str(internal_reply_id or "").strip()
        with self._lock:
            binding = _presentation_send_binding(self, handoff, key)
            record = self._replies.get(key)
            if record is None:
                raise ValueError("owner_action_send_reply_not_canonical")
            if (
                _reply_binding_snapshot(record) != binding.binding_snapshot
                or len(record.segments) != len(handoff.final_segments)
                or tuple(segment.visible_text for segment in record.segments)
                != handoff.final_segments
                or any(
                    type(segment.status) is not SegmentSendStatus
                    or segment.status is not SegmentSendStatus.SUCCEEDED
                    or type(segment.completed_at) is not float
                    or segment.completed_at <= 0.0
                    for segment in record.segments
                )
            ):
                raise ValueError("owner_action_send_not_successful")
            return _issue_send_evidence(self, handoff, record)

    def issue_presentation_send_terminal_evidence(
        self,
        presentation: object,
        *,
        internal_reply_id: str,
    ) -> OwnerActionSendTerminalEvidence:
        """Seal one exact all-success presentation for downstream delivery.

        The evidence type predates the generic P6 presentation transaction and
        retains its legacy name for the E4 durable-finalize API.  Issuance and
        canonical inspection are identical for ordinary and action outcomes.
        """

        return self.issue_owner_action_send_terminal_evidence(
            presentation,
            internal_reply_id=internal_reply_id,
        )

    def _replace_segment(
        self,
        segment_id: str,
        *,
        expected: SegmentSendStatus,
        update: Callable[[SendSegmentAttempt], SendSegmentAttempt],
    ) -> InternalReplySend:
        key = str(segment_id or "").strip()
        internal_reply_id = self._segment_to_reply.get(key)
        if internal_reply_id is None:
            raise KeyError("unknown internal send segment")
        record = self._replies[internal_reply_id]
        index = next(
            (i for i, segment in enumerate(record.segments) if segment.segment_id == key),
            -1,
        )
        if index < 0:
            raise KeyError("internal send segment index is inconsistent")
        current = record.segments[index]
        if current.status != expected:
            raise ValueError(
                f"invalid send transition: {current.status.value} -> expected {expected.value}"
            )
        replacement = update(current)
        segments = list(record.segments)
        segments[index] = replacement
        updated = replace(record, segments=tuple(segments))
        self._replies[internal_reply_id] = updated
        return updated


def _inspect_owner_action_send_terminal_evidence(
    evidence: object,
    *,
    ledger: InternalSendReceiptLedger,
    presentation: object,
) -> _OwnerActionSendTerminalInspection:
    """Friend inspector for E4D; returns exact lineage and no visible bytes."""

    if type(ledger) is not InternalSendReceiptLedger:
        raise ValueError("owner_action_send_ledger_not_canonical")
    from .presentation_handoff import PresentationHandoff

    if type(presentation) is not PresentationHandoff:
        raise ValueError("owner_action_presentation_not_canonical")
    handoff = presentation
    with ledger._lock:
        evidence_record = _inspect_send_evidence_record(
            evidence,
            ledger=ledger,
            presentation=handoff,
        )
        reply = ledger._replies.get(evidence_record.internal_reply_id)
        if (
            reply is None
            or _presentation_send_snapshot(handoff)
            != evidence_record.presentation_snapshot
            or _reply_binding_snapshot(reply) != evidence_record.binding_snapshot
            or any(
                type(segment.status) is not SegmentSendStatus
                or segment.status is not SegmentSendStatus.SUCCEEDED
                for segment in reply.segments
            )
            or len(reply.segments) != evidence.segment_count
        ):
            raise ValueError("owner_action_send_evidence_corrupt")
        request = handoff.composer_request
        return _OwnerActionSendTerminalInspection(
            presentation=handoff,
            composer_request=request,
            action_outcome=handoff.action_outcome,
            action_outcome_authority=handoff.action_outcome_authority,
            segment_count=evidence.segment_count,
        )
