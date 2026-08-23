from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from .contracts import (
    ContractViolation,
    IngressDecision,
    IngressDisposition,
    SenderKind,
)
from .contracts._validation import require_hex, require_text
from .conversation_event import (
    ConversationEvent,
    ConversationRevisionBook,
    PluginSource,
)
from .conversation_ledger import (
    ConversationLedger,
    LedgerRecord,
    LedgerRole,
    LedgerSourceKind,
)
from .identity import build_sender_key
from .send_receipt import SegmentSendStatus, SentReplyRecord


class SceneEntrySource(str, Enum):
    HUMAN_INBOUND = "human_inbound"
    SHIO_OUTBOUND = "shio_outbound"
    BANNED_HUMAN = "banned_human"
    KNOWN_BOT = "known_bot"
    EXTERNAL_PLUGIN = "external_plugin"
    PARSER = "parser"
    MEME_MANAGER = "meme_manager"
    UNKNOWN_ASSISTANT = "unknown_assistant"


class SceneMutationStatus(str, Enum):
    ACCEPTED_HUMAN = "accepted_human"
    ACCEPTED_SHIO_OUTBOUND = "accepted_shio_outbound"
    REJECTED = "rejected"


class SceneRejectReason(str, Enum):
    INGRESS_NOT_ACCEPTED = "ingress_not_accepted"
    BINDING_MISMATCH = "binding_mismatch"
    EVENT_SOURCE_UNVERIFIED = "event_source_unverified"
    NOT_GROUP_EVENT = "not_group_event"
    CONTENT_MISMATCH = "content_mismatch"
    PERSONAL_FACT_INVALID = "personal_fact_invalid"
    PERSONAL_FACT_SUBJECT_MISMATCH = "personal_fact_subject_mismatch"
    REVISION_STALE = "revision_stale"
    REVISION_GAP = "revision_gap"
    SOURCE_NOT_ALLOWED = "source_not_allowed"
    RECEIPT_NOT_SUCCESSFUL = "receipt_not_successful"
    OUTBOUND_SOURCE_UNVERIFIED = "outbound_source_unverified"
    OUTBOUND_TARGET_MISMATCH = "outbound_target_mismatch"
    OUTBOUND_DUPLICATE = "outbound_duplicate"


def _content_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _segment_digest(value: str) -> str:
    return _content_digest(value)


def _positive_integer(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractViolation(f"{field_name}_invalid")
    return value


def _required_content(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractViolation(f"{field_name}_required")
    return value


def _scene_snapshot_integrity(snapshot: GroupSceneSnapshot) -> tuple[object, ...]:
    if type(snapshot) is not GroupSceneSnapshot:
        raise ContractViolation("scene_snapshot_required")
    try:
        scope_key = snapshot.scope_key
        revision = snapshot.conversation_revision
        topics = snapshot.public_topics
        participants = snapshot.participants
    except AttributeError as exc:
        raise ContractViolation("scene_snapshot_corrupt") from exc
    if (
        type(scope_key) is not str
        or not scope_key
        or type(revision) is not int
        or revision < 0
        or type(topics) is not tuple
        or type(participants) is not tuple
    ):
        raise ContractViolation("scene_snapshot_corrupt")
    topic_values: list[tuple[object, ...]] = []
    for topic in topics:
        if type(topic) is not PublicTopic:
            raise ContractViolation("scene_snapshot_corrupt")
        try:
            topic_snapshot = (
                topic.source,
                topic.conversation_revision,
                topic.author_sender_key,
                topic.target_sender_key,
                topic.content,
                topic.content_digest,
            )
        except AttributeError as exc:
            raise ContractViolation("scene_snapshot_corrupt") from exc
        if (
            type(topic.source) is not SceneEntrySource
            or type(topic.conversation_revision) is not int
            or topic.conversation_revision < 1
            or any(type(value) is not str for value in topic_snapshot[2:])
            or _content_digest(topic.content) != topic.content_digest
        ):
            raise ContractViolation("scene_snapshot_corrupt")
        topic_values.append(topic_snapshot)
    participant_values: list[tuple[object, ...]] = []
    for participant in participants:
        if type(participant) is not ParticipantState:
            raise ContractViolation("scene_snapshot_corrupt")
        try:
            facts = participant.personal_facts
            participant_snapshot = (
                participant.sender_key,
                participant.first_seen_revision,
                participant.last_seen_revision,
                participant.human_turn_count,
                participant.shio_reply_count,
            )
        except AttributeError as exc:
            raise ContractViolation("scene_snapshot_corrupt") from exc
        if (
            type(participant.sender_key) is not str
            or not participant.sender_key
            or any(type(value) is not int for value in participant_snapshot[1:])
            or type(facts) is not tuple
        ):
            raise ContractViolation("scene_snapshot_corrupt")
        fact_values: list[tuple[object, ...]] = []
        for fact in facts:
            if type(fact) is not PersonalFact:
                raise ContractViolation("scene_snapshot_corrupt")
            try:
                fact_snapshot = (
                    fact.subject_key,
                    fact.content,
                    fact.content_digest,
                    fact.observed_revision,
                )
            except AttributeError as exc:
                raise ContractViolation("scene_snapshot_corrupt") from exc
            if (
                any(type(value) is not str for value in fact_snapshot[:3])
                or type(fact.observed_revision) is not int
                or _content_digest(fact.content) != fact.content_digest
            ):
                raise ContractViolation("scene_snapshot_corrupt")
            fact_values.append(fact_snapshot)
        participant_values.append((*participant_snapshot, tuple(fact_values)))
    return (scope_key, revision, tuple(topic_values), tuple(participant_values))


@dataclass(frozen=True, slots=True)
class PersonalFactCandidate:
    """A caller-classified personal fact that still must match this sender."""

    subject_key: str = field(repr=False)
    content: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subject_key",
            require_text(self.subject_key, "personal_fact_subject_key"),
        )
        object.__setattr__(
            self,
            "content",
            _required_content(self.content, "personal_fact_content"),
        )

    def trace_metadata(self) -> dict[str, bool]:
        return {"personal_fact_candidate_present": True}


@dataclass(frozen=True, slots=True)
class PersonalFact:
    subject_key: str = field(repr=False)
    content: str = field(repr=False)
    content_digest: str = field(repr=False)
    observed_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subject_key",
            require_text(self.subject_key, "personal_fact_subject_key"),
        )
        content = _required_content(self.content, "personal_fact_content")
        object.__setattr__(self, "content", content)
        digest = require_hex(
            self.content_digest,
            "personal_fact_content_digest",
            lengths=(64,),
        )
        if digest != _content_digest(content):
            raise ContractViolation("personal_fact_content_digest_mismatch")
        object.__setattr__(self, "content_digest", digest)
        _positive_integer(self.observed_revision, "personal_fact_observed_revision")

    def trace_metadata(self) -> dict[str, int]:
        return {"personal_fact_observed_revision": self.observed_revision}


@dataclass(frozen=True, slots=True)
class PublicTopic:
    source: SceneEntrySource
    conversation_revision: int
    author_sender_key: str = field(default="", repr=False)
    target_sender_key: str = field(default="", repr=False)
    content: str = field(default="", repr=False)
    content_digest: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.source not in {
            SceneEntrySource.HUMAN_INBOUND,
            SceneEntrySource.SHIO_OUTBOUND,
        }:
            raise ContractViolation("public_topic_source_invalid")
        _positive_integer(self.conversation_revision, "public_topic_revision")
        content = _required_content(self.content, "public_topic_content")
        object.__setattr__(self, "content", content)
        digest = require_hex(
            self.content_digest,
            "public_topic_content_digest",
            lengths=(64,),
        )
        if digest != _content_digest(content):
            raise ContractViolation("public_topic_content_digest_mismatch")
        object.__setattr__(self, "content_digest", digest)
        if self.source is SceneEntrySource.HUMAN_INBOUND:
            object.__setattr__(
                self,
                "author_sender_key",
                require_text(self.author_sender_key, "public_topic_author_sender_key"),
            )
            if self.target_sender_key:
                raise ContractViolation("human_public_topic_target_forbidden")
        else:
            object.__setattr__(
                self,
                "target_sender_key",
                require_text(self.target_sender_key, "public_topic_target_sender_key"),
            )
            if self.author_sender_key:
                raise ContractViolation("shio_public_topic_author_forbidden")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "scene_entry_source": self.source.value,
            "conversation_revision": self.conversation_revision,
            "scene_topic_is_outbound": (
                self.source is SceneEntrySource.SHIO_OUTBOUND
            ),
        }


@dataclass(frozen=True, slots=True)
class ParticipantState:
    sender_key: str = field(repr=False)
    first_seen_revision: int
    last_seen_revision: int
    human_turn_count: int
    shio_reply_count: int
    personal_facts: tuple[PersonalFact, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sender_key",
            require_text(self.sender_key, "participant_sender_key"),
        )
        first = _positive_integer(
            self.first_seen_revision,
            "participant_first_seen_revision",
        )
        last = _positive_integer(
            self.last_seen_revision,
            "participant_last_seen_revision",
        )
        if first > last:
            raise ContractViolation("participant_revision_order_invalid")
        for name in ("human_turn_count", "shio_reply_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractViolation(f"participant_{name}_invalid")
        facts = tuple(self.personal_facts)
        if any(
            not isinstance(fact, PersonalFact) or fact.subject_key != self.sender_key
            for fact in facts
        ):
            raise ContractViolation("participant_personal_fact_subject_mismatch")
        object.__setattr__(self, "personal_facts", facts)

    def trace_metadata(self) -> dict[str, int]:
        return {
            "participant_first_seen_revision": self.first_seen_revision,
            "participant_last_seen_revision": self.last_seen_revision,
            "participant_human_turn_count": self.human_turn_count,
            "participant_shio_reply_count": self.shio_reply_count,
            "participant_personal_fact_count": len(self.personal_facts),
        }


@dataclass(frozen=True, slots=True)
class GroupSceneSnapshot:
    scope_key: str = field(repr=False)
    conversation_revision: int
    public_topics: tuple[PublicTopic, ...] = field(default=(), repr=False)
    participants: tuple[ParticipantState, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_key", require_text(self.scope_key, "scope_key"))
        revision = self.conversation_revision
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ContractViolation("scene_revision_invalid")
        topics = tuple(self.public_topics)
        participants = tuple(self.participants)
        if any(
            not isinstance(topic, PublicTopic)
            or topic.conversation_revision > revision
            for topic in topics
        ):
            raise ContractViolation("scene_public_topic_revision_invalid")
        if any(not isinstance(value, ParticipantState) for value in participants):
            raise ContractViolation("scene_participant_invalid")
        sender_keys = [value.sender_key for value in participants]
        if len(sender_keys) != len(set(sender_keys)):
            raise ContractViolation("scene_participant_duplicate")
        if any(value.last_seen_revision > revision for value in participants):
            raise ContractViolation("scene_participant_revision_invalid")
        object.__setattr__(self, "public_topics", topics)
        object.__setattr__(self, "participants", participants)

    def participant(self, sender_key: str) -> ParticipantState | None:
        key = str(sender_key or "").strip()
        return next(
            (value for value in self.participants if value.sender_key == key),
            None,
        )

    def trace_metadata(self) -> dict[str, int]:
        return {
            "conversation_revision": self.conversation_revision,
            "scene_public_topic_count": len(self.public_topics),
            "scene_participant_count": len(self.participants),
            "scene_personal_fact_count": sum(
                len(participant.personal_facts)
                for participant in self.participants
            ),
        }


@dataclass(frozen=True, slots=True)
class SceneMutationResult:
    status: SceneMutationStatus
    source: SceneEntrySource
    reason: SceneRejectReason | None
    state_changed: bool
    snapshot: GroupSceneSnapshot = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, SceneMutationStatus):
            raise ContractViolation("scene_mutation_status_invalid")
        if not isinstance(self.source, SceneEntrySource):
            raise ContractViolation("scene_mutation_source_invalid")
        if not isinstance(self.snapshot, GroupSceneSnapshot):
            raise ContractViolation("scene_mutation_snapshot_invalid")
        if type(self.state_changed) is not bool:
            raise ContractViolation("scene_mutation_changed_invalid")
        if self.status is SceneMutationStatus.REJECTED:
            if not isinstance(self.reason, SceneRejectReason) or self.state_changed:
                raise ContractViolation("scene_rejection_shape_invalid")
        elif self.reason is not None or not self.state_changed:
            raise ContractViolation("scene_acceptance_shape_invalid")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        snapshot = self.snapshot.trace_metadata()
        return {
            "scene_mutation_status": self.status.value,
            "scene_entry_source": self.source.value,
            "scene_reject_reason": self.reason.value if self.reason else "none",
            "scene_state_changed": self.state_changed,
            **snapshot,
        }


@dataclass(frozen=True, slots=True)
class _AcceptedTarget:
    conversation_event: ConversationEvent = field(repr=False)
    snapshot: GroupSceneSnapshot = field(repr=False)
    snapshot_integrity: tuple[object, ...] = field(repr=False)
    scope_key: str = field(repr=False)
    session_id: str = field(repr=False)
    message_id: str = field(repr=False)
    sender_key: str = field(repr=False)
    content_digest: str = field(repr=False)
    trace_id: str = field(repr=False)
    referenced_message_id: str = field(repr=False)
    conversation_revision: int


@dataclass(frozen=True, slots=True)
class _ScopeState:
    snapshot: GroupSceneSnapshot = field(repr=False)
    snapshot_integrity: tuple[object, ...] = field(repr=False)
    targets: tuple[_AcceptedTarget, ...] = field(default=(), repr=False)
    outbound_reply_ids: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class _RestoredProactiveGroup:
    platform_id: str = field(repr=False)
    bot_id: str = field(repr=False)
    group_id: str = field(repr=False)
    unified_msg_origin: str = field(repr=False)
    scope_key: str = field(repr=False)
    scene: GroupSceneSnapshot = field(repr=False)


class GroupSceneBook:
    """Bounded per-scope public scene with sender-isolated participant state."""

    __slots__ = (
        "_lock",
        "_max_outbound_receipts",
        "_max_participants",
        "_max_personal_facts_per_participant",
        "_max_public_topics",
        "_max_target_bindings",
        "_revision_book",
        "_restored_proactive_groups",
        "_scenes",
    )

    def __init__(
        self,
        *,
        max_public_topics: int = 32,
        max_participants: int = 64,
        max_personal_facts_per_participant: int = 16,
        max_target_bindings: int = 64,
        max_outbound_receipts: int = 64,
        revision_book: ConversationRevisionBook | None = None,
        conversation_ledger: ConversationLedger | None = None,
    ) -> None:
        if revision_book is not None and type(revision_book) is not ConversationRevisionBook:
            raise ContractViolation("scene_revision_book_invalid")
        if conversation_ledger is not None and type(conversation_ledger) is not ConversationLedger:
            raise ContractViolation("scene_conversation_ledger_invalid")
        self._revision_book = revision_book
        self._max_public_topics = _positive_integer(
            max_public_topics,
            "max_public_topics",
        )
        self._max_participants = _positive_integer(
            max_participants,
            "max_participants",
        )
        self._max_personal_facts_per_participant = _positive_integer(
            max_personal_facts_per_participant,
            "max_personal_facts_per_participant",
        )
        self._max_target_bindings = _positive_integer(
            max_target_bindings,
            "max_target_bindings",
        )
        self._max_outbound_receipts = _positive_integer(
            max_outbound_receipts,
            "max_outbound_receipts",
        )
        self._scenes: dict[str, _ScopeState] = {}
        self._restored_proactive_groups: tuple[_RestoredProactiveGroup, ...] = ()
        self._lock = threading.RLock()
        if conversation_ledger is not None:
            self._restore_verified_public_tail(conversation_ledger)

    def _restore_verified_public_tail(self, ledger: ConversationLedger) -> None:
        records = ledger.restored_inbound_records()
        by_scope: dict[str, list[LedgerRecord]] = {}
        for record in records:
            if (
                type(record) is LedgerRecord
                and record.source_kind is LedgerSourceKind.INBOUND
                and record.role is LedgerRole.USER
                and record.attribution_status == "verified"
            ):
                by_scope.setdefault(record.scope_key, []).append(record)
        restored_groups: list[_RestoredProactiveGroup] = []
        for scope_key, values in by_scope.items():
            latest = values[-1]
            if not all(
                (
                    latest.platform_id,
                    latest.bot_id,
                    latest.group_id,
                    latest.unified_msg_origin,
                )
            ):
                continue
            expected_scope = (
                f"platform:{latest.platform_id}|bot:{latest.bot_id}|"
                f"group:{latest.group_id}"
            )
            if scope_key != expected_scope:
                continue
            revision = (
                self._revision_book.restored_revision(scope_key)
                if self._revision_book is not None
                else 0
            )
            if revision < 1:
                continue
            tail = values[-min(self._max_public_topics, revision) :]
            first_revision = revision - len(tail) + 1
            topics: list[PublicTopic] = []
            participants: dict[str, ParticipantState] = {}
            for offset, record in enumerate(tail):
                record_revision = first_revision + offset
                topics.append(
                    PublicTopic(
                        source=SceneEntrySource.HUMAN_INBOUND,
                        conversation_revision=record_revision,
                        author_sender_key=record.sender_key,
                        content=record.content,
                        content_digest=record.content_digest,
                    )
                )
                current = participants.get(record.sender_key)
                participants[record.sender_key] = ParticipantState(
                    sender_key=record.sender_key,
                    first_seen_revision=(
                        current.first_seen_revision
                        if current is not None
                        else record_revision
                    ),
                    last_seen_revision=record_revision,
                    human_turn_count=(
                        current.human_turn_count + 1 if current is not None else 1
                    ),
                    shio_reply_count=(
                        current.shio_reply_count if current is not None else 0
                    ),
                    personal_facts=(
                        current.personal_facts if current is not None else ()
                    ),
                )
            snapshot = GroupSceneSnapshot(
                scope_key=scope_key,
                conversation_revision=revision,
                public_topics=tuple(topics),
                participants=tuple(participants.values()),
            )
            self._scenes[scope_key] = _ScopeState(
                snapshot=snapshot,
                snapshot_integrity=_scene_snapshot_integrity(snapshot),
            )
            restored_groups.append(
                _RestoredProactiveGroup(
                    platform_id=latest.platform_id,
                    bot_id=latest.bot_id,
                    group_id=latest.group_id,
                    unified_msg_origin=latest.unified_msg_origin,
                    scope_key=scope_key,
                    scene=snapshot,
                )
            )
        self._restored_proactive_groups = tuple(restored_groups)

    def _proactive_restart_groups(self) -> tuple[_RestoredProactiveGroup, ...]:
        with self._lock:
            for restored in self._restored_proactive_groups:
                state = self._scenes.get(restored.scope_key)
                if (
                    type(restored) is not _RestoredProactiveGroup
                    or state is None
                    or state.snapshot is not restored.scene
                    or _scene_snapshot_integrity(restored.scene)
                    != state.snapshot_integrity
                ):
                    raise ContractViolation("scene_restored_target_corrupt")
            return self._restored_proactive_groups

    @property
    def scope_count(self) -> int:
        with self._lock:
            return len(self._scenes)

    def _empty_snapshot(self, scope_key: str) -> GroupSceneSnapshot:
        return GroupSceneSnapshot(
            scope_key=scope_key,
            conversation_revision=0,
        )

    def snapshot(self, scope_key: str) -> GroupSceneSnapshot:
        scope = require_text(scope_key, "scope_key")
        with self._lock:
            state = self._scenes.get(scope)
            return state.snapshot if state is not None else self._empty_snapshot(scope)

    def inspect_current_snapshot(
        self,
        snapshot: GroupSceneSnapshot,
    ) -> GroupSceneSnapshot:
        """Verify the exact latest canonical public scene for one scope."""

        if type(snapshot) is not GroupSceneSnapshot:
            raise ContractViolation("scene_snapshot_required")
        with self._lock:
            try:
                scope_key = snapshot.scope_key
                integrity = _scene_snapshot_integrity(snapshot)
            except (AttributeError, ContractViolation) as exc:
                raise ContractViolation("scene_snapshot_corrupt") from exc
            state = self._scenes.get(scope_key)
            if state is None or snapshot is not state.snapshot:
                raise ContractViolation("scene_snapshot_not_canonical")
            if integrity != state.snapshot_integrity:
                raise ContractViolation("scene_snapshot_corrupt")
            return snapshot

    def inspect_current_human_scene(
        self,
        event: ConversationEvent,
        snapshot: GroupSceneSnapshot,
    ) -> GroupSceneSnapshot:
        """Verify the exact current scene recorded from one canonical event."""

        self.inspect_recorded_human_scene(event, snapshot)
        with self._lock:
            state = self._scenes.get(event.envelope.scope_key)
            if state is None or snapshot is not state.snapshot:
                raise ContractViolation("scene_snapshot_not_canonical")
            return snapshot

    def inspect_recorded_human_scene(
        self,
        event: ConversationEvent,
        snapshot: GroupSceneSnapshot,
    ) -> GroupSceneSnapshot:
        """Verify an exact bounded historical scene after newer group turns."""

        if type(event) is not ConversationEvent:
            raise ContractViolation("scene_event_required")
        if type(snapshot) is not GroupSceneSnapshot:
            raise ContractViolation("scene_snapshot_required")
        with self._lock:
            state = self._scenes.get(event.envelope.scope_key)
            if state is None:
                raise ContractViolation("scene_snapshot_not_canonical")
            try:
                integrity = _scene_snapshot_integrity(snapshot)
            except ContractViolation as exc:
                raise ContractViolation("scene_snapshot_corrupt") from exc
            target = next(
                (
                    value
                    for value in reversed(state.targets)
                    if type(value) is _AcceptedTarget
                    and value.conversation_event is event
                    and value.snapshot is snapshot
                ),
                None,
            )
            if target is not None and integrity != target.snapshot_integrity:
                raise ContractViolation("scene_snapshot_corrupt")
            if (
                target is None
                or target.scope_key != event.envelope.scope_key
                or target.session_id != event.envelope.session_id
                or target.message_id != event.envelope.message_id
                or target.sender_key != event.envelope.sender_key
                or target.content_digest != event.content_digest
                or target.trace_id != event.binding.trace_id
                or target.referenced_message_id
                != event.envelope.reply_to_message_id
                or target.conversation_revision
                != event.binding.conversation_revision
                or snapshot.conversation_revision
                != event.binding.conversation_revision
            ):
                raise ContractViolation("scene_snapshot_event_mismatch")
            return snapshot

    def target_binding_count(self, scope_key: str) -> int:
        scope = require_text(scope_key, "scope_key")
        with self._lock:
            state = self._scenes.get(scope)
            return len(state.targets) if state is not None else 0

    def _reject(
        self,
        *,
        scope_key: str,
        source: SceneEntrySource,
        reason: SceneRejectReason,
    ) -> SceneMutationResult:
        state = self._scenes.get(scope_key)
        snapshot = (
            state.snapshot
            if state is not None
            else self._empty_snapshot(scope_key)
        )
        return SceneMutationResult(
            status=SceneMutationStatus.REJECTED,
            source=source,
            reason=reason,
            state_changed=False,
            snapshot=snapshot,
        )

    def record_human(
        self,
        event: ConversationEvent,
        *,
        decision: IngressDecision,
        public_content: str,
        personal_facts: Iterable[PersonalFactCandidate] = (),
    ) -> SceneMutationResult:
        if not isinstance(event, ConversationEvent):
            raise ContractViolation("conversation_event_required")
        scope = event.envelope.scope_key
        source = SceneEntrySource.HUMAN_INBOUND
        with self._lock:
            if not isinstance(decision, IngressDecision) or decision.binding != event.binding:
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.BINDING_MISMATCH,
                )
            if (
                decision.disposition is not IngressDisposition.ACCEPT_HUMAN
                or not decision.allows_state_mutation
            ):
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.INGRESS_NOT_ACCEPTED,
                )
            if (
                event.sender_kind is not SenderKind.HUMAN
                or decision.sender_kind is not SenderKind.HUMAN
                or event.plugin_source is not PluginSource.NONE
            ):
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.EVENT_SOURCE_UNVERIFIED,
                )
            if event.envelope.chat_type != "group":
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.NOT_GROUP_EVENT,
                )
            if not isinstance(public_content, str) or (
                _content_digest(public_content) != event.content_digest
            ):
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.CONTENT_MISMATCH,
                )
            if isinstance(personal_facts, (str, bytes)):
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.PERSONAL_FACT_INVALID,
                )
            try:
                fact_candidates = tuple(personal_facts)
            except TypeError:
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.PERSONAL_FACT_INVALID,
                )
            if any(
                not isinstance(candidate, PersonalFactCandidate)
                for candidate in fact_candidates
            ):
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.PERSONAL_FACT_INVALID,
                )
            if any(
                candidate.subject_key != event.envelope.sender_key
                for candidate in fact_candidates
            ):
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.PERSONAL_FACT_SUBJECT_MISMATCH,
                )

            current_state = self._scenes.get(scope)
            revision = event.binding.conversation_revision
            if current_state is not None:
                current_snapshot = current_state.snapshot
            else:
                restored = (
                    self._revision_book.restored_revision(scope)
                    if self._revision_book is not None
                    else 0
                )
                current_snapshot = GroupSceneSnapshot(
                    scope_key=scope,
                    conversation_revision=(
                        restored if revision == restored + 1 else 0
                    ),
                )
            if revision <= current_snapshot.conversation_revision:
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.REVISION_STALE,
                )
            if revision != current_snapshot.conversation_revision + 1:
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.REVISION_GAP,
                )

            topics = list(current_snapshot.public_topics)
            if public_content.strip():
                topics.append(
                    PublicTopic(
                        source=source,
                        conversation_revision=revision,
                        author_sender_key=event.envelope.sender_key,
                        content=public_content,
                        content_digest=event.content_digest,
                    )
                )
            topics = topics[-self._max_public_topics :]

            participants = {
                participant.sender_key: participant
                for participant in current_snapshot.participants
            }
            existing = participants.get(event.envelope.sender_key)
            new_facts = tuple(
                PersonalFact(
                    subject_key=candidate.subject_key,
                    content=candidate.content,
                    content_digest=_content_digest(candidate.content),
                    observed_revision=revision,
                )
                for candidate in fact_candidates
            )
            retained_facts = (
                *((existing.personal_facts if existing is not None else ())),
                *new_facts,
            )[-self._max_personal_facts_per_participant :]
            participants[event.envelope.sender_key] = ParticipantState(
                sender_key=event.envelope.sender_key,
                first_seen_revision=(
                    existing.first_seen_revision if existing is not None else revision
                ),
                last_seen_revision=revision,
                human_turn_count=(
                    existing.human_turn_count + 1 if existing is not None else 1
                ),
                shio_reply_count=(
                    existing.shio_reply_count if existing is not None else 0
                ),
                personal_facts=retained_facts,
            )
            while len(participants) > self._max_participants:
                oldest_key = min(
                    participants,
                    key=lambda key: (
                        participants[key].last_seen_revision,
                        key,
                    ),
                )
                participants.pop(oldest_key)
            participant_values = tuple(
                sorted(
                    participants.values(),
                    key=lambda value: (
                        value.first_seen_revision,
                        value.last_seen_revision,
                        value.sender_key,
                    ),
                )
            )
            snapshot = GroupSceneSnapshot(
                scope_key=scope,
                conversation_revision=revision,
                public_topics=tuple(topics),
                participants=participant_values,
            )
            targets = list(current_state.targets if current_state else ())
            targets.append(
                _AcceptedTarget(
                    conversation_event=event,
                    snapshot=snapshot,
                    snapshot_integrity=_scene_snapshot_integrity(snapshot),
                    scope_key=scope,
                    session_id=event.envelope.session_id,
                    message_id=event.envelope.message_id,
                    sender_key=event.envelope.sender_key,
                    content_digest=event.content_digest,
                    trace_id=event.binding.trace_id,
                    referenced_message_id=event.envelope.reply_to_message_id,
                    conversation_revision=revision,
                )
            )
            targets = targets[-self._max_target_bindings :]
            self._scenes[scope] = _ScopeState(
                snapshot=snapshot,
                snapshot_integrity=_scene_snapshot_integrity(snapshot),
                targets=tuple(targets),
                outbound_reply_ids=(
                    current_state.outbound_reply_ids if current_state else ()
                ),
            )
            return SceneMutationResult(
                status=SceneMutationStatus.ACCEPTED_HUMAN,
                source=source,
                reason=None,
                state_changed=True,
                snapshot=snapshot,
            )

    def record_outbound(
        self,
        receipt: SentReplyRecord,
        *,
        source: SceneEntrySource,
    ) -> SceneMutationResult:
        if not isinstance(receipt, SentReplyRecord):
            raise ContractViolation("sent_reply_record_required")
        scope = str(receipt.scope_key or "").strip()
        if not scope:
            raise ContractViolation("outbound_scope_required")
        normalized_source = (
            source
            if isinstance(source, SceneEntrySource)
            else SceneEntrySource.UNKNOWN_ASSISTANT
        )
        with self._lock:
            if source is not SceneEntrySource.SHIO_OUTBOUND:
                return self._reject(
                    scope_key=scope,
                    source=normalized_source,
                    reason=SceneRejectReason.SOURCE_NOT_ALLOWED,
                )
            state = self._scenes.get(scope)
            if state is None:
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.OUTBOUND_TARGET_MISMATCH,
                )
            if receipt.reply_id in state.outbound_reply_ids:
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.OUTBOUND_DUPLICATE,
                )
            if (
                receipt.target_source_kind != "current_inbound"
                or not receipt.reply_id.startswith("shio-")
                or not receipt.trace_id
            ):
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.OUTBOUND_SOURCE_UNVERIFIED,
                )
            successful = receipt.successful_segments
            if (
                not receipt.is_terminal
                or not successful
                or receipt.sent_at <= 0
                or any(
                    segment.status is not SegmentSendStatus.SUCCEEDED
                    or segment.internal_reply_id != receipt.reply_id
                    or segment.completed_at <= 0
                    or not segment.visible_text
                    or segment.visible_text_digest
                    != _segment_digest(segment.visible_text)
                    for segment in successful
                )
            ):
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.RECEIPT_NOT_SUCCESSFUL,
                )

            target = next(
                (
                    value
                    for value in reversed(state.targets)
                    if value.message_id == receipt.target_message_id
                ),
                None,
            )
            if target is None or any(
                (
                    receipt.scope_key != target.scope_key,
                    receipt.session_id != target.session_id,
                    receipt.target_message_id != target.message_id,
                    receipt.target_sender_key != target.sender_key,
                    receipt.target_content_digest != target.content_digest,
                    receipt.trace_id != target.trace_id,
                    receipt.target_referenced_message_id
                    != target.referenced_message_id,
                )
            ):
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.OUTBOUND_TARGET_MISMATCH,
                )
            participant = state.snapshot.participant(target.sender_key)
            if participant is None:
                return self._reject(
                    scope_key=scope,
                    source=source,
                    reason=SceneRejectReason.OUTBOUND_TARGET_MISMATCH,
                )

            visible_content = receipt.successful_visible_text
            topics = (
                *state.snapshot.public_topics,
                PublicTopic(
                    source=source,
                    conversation_revision=target.conversation_revision,
                    target_sender_key=target.sender_key,
                    content=visible_content,
                    content_digest=_content_digest(visible_content),
                ),
            )[-self._max_public_topics :]
            participants = tuple(
                ParticipantState(
                    sender_key=value.sender_key,
                    first_seen_revision=value.first_seen_revision,
                    last_seen_revision=value.last_seen_revision,
                    human_turn_count=value.human_turn_count,
                    shio_reply_count=(
                        value.shio_reply_count + 1
                        if value.sender_key == target.sender_key
                        else value.shio_reply_count
                    ),
                    personal_facts=value.personal_facts,
                )
                for value in state.snapshot.participants
            )
            snapshot = GroupSceneSnapshot(
                scope_key=scope,
                conversation_revision=state.snapshot.conversation_revision,
                public_topics=topics,
                participants=participants,
            )
            reply_ids = (
                *state.outbound_reply_ids,
                receipt.reply_id,
            )[-self._max_outbound_receipts :]
            self._scenes[scope] = _ScopeState(
                snapshot=snapshot,
                snapshot_integrity=_scene_snapshot_integrity(snapshot),
                targets=state.targets,
                outbound_reply_ids=reply_ids,
            )
            return SceneMutationResult(
                status=SceneMutationStatus.ACCEPTED_SHIO_OUTBOUND,
                source=source,
                reason=None,
                state_changed=True,
                snapshot=snapshot,
            )

    def record_proactive_outbound(
        self,
        presentation: object,
        *,
        ledger: object,
        internal_reply_id: str,
    ) -> SceneMutationResult:
        """Record one sent group topic without manufacturing a user target."""

        getter = getattr(ledger, "proactive_sent_reply_record", None)
        if not callable(getter):
            raise ContractViolation("proactive_send_ledger_required")
        try:
            receipt = getter(
                presentation,
                internal_reply_id=internal_reply_id,
            )
            plan = presentation.request.plan
            target = presentation.target
        except Exception as exc:
            raise ContractViolation("proactive_send_receipt_invalid") from exc
        if type(receipt) is not SentReplyRecord:
            raise ContractViolation("proactive_send_receipt_invalid")
        scope = receipt.scope_key
        with self._lock:
            state = self._scenes.get(scope)
            if (
                state is None
                or target.scope_key != scope
                or target.unified_msg_origin != receipt.session_id
                or state.snapshot.conversation_revision != plan.scene_revision
                or receipt.reply_id in state.outbound_reply_ids
            ):
                return self._reject(
                    scope_key=scope,
                    source=SceneEntrySource.SHIO_OUTBOUND,
                    reason=SceneRejectReason.OUTBOUND_TARGET_MISMATCH,
                )
            visible_content = receipt.successful_visible_text
            topics = (
                *state.snapshot.public_topics,
                PublicTopic(
                    source=SceneEntrySource.SHIO_OUTBOUND,
                    conversation_revision=state.snapshot.conversation_revision,
                    target_sender_key=build_sender_key(scope, target.bot_id),
                    content=visible_content,
                    content_digest=_content_digest(visible_content),
                ),
            )[-self._max_public_topics :]
            snapshot = GroupSceneSnapshot(
                scope_key=scope,
                conversation_revision=state.snapshot.conversation_revision,
                public_topics=topics,
                participants=state.snapshot.participants,
            )
            reply_ids = (
                *state.outbound_reply_ids,
                receipt.reply_id,
            )[-self._max_outbound_receipts :]
            self._scenes[scope] = _ScopeState(
                snapshot=snapshot,
                snapshot_integrity=_scene_snapshot_integrity(snapshot),
                targets=state.targets,
                outbound_reply_ids=reply_ids,
            )
            return SceneMutationResult(
                status=SceneMutationStatus.ACCEPTED_SHIO_OUTBOUND,
                source=SceneEntrySource.SHIO_OUTBOUND,
                reason=None,
                state_changed=True,
                snapshot=snapshot,
            )


__all__ = [
    "GroupSceneBook",
    "GroupSceneSnapshot",
    "ParticipantState",
    "PersonalFact",
    "PersonalFactCandidate",
    "PublicTopic",
    "SceneEntrySource",
    "SceneMutationResult",
    "SceneMutationStatus",
    "SceneRejectReason",
]
