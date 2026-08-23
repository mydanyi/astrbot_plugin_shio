from __future__ import annotations

import hashlib
import secrets
import threading
from dataclasses import dataclass, field
from enum import Enum

from .contracts import ContractViolation, DecisionBinding, SenderKind
from .contracts._validation import require_hex, require_text
from .identity import PrincipalContext, TurnEnvelope
from .runtime_continuity import RuntimeContinuityError, RuntimeContinuityStore
from .observability import diagnostic_digest


class PluginSource(str, Enum):
    """Closed inbound plugin/source taxonomy; never inferred from visible text."""

    NONE = "none"
    SHIO_REPLY = "shio_reply"
    PARSER = "parser"
    KEYWORDS = "keywords"
    PROGRESS = "progress"
    MANAGEMENT = "management"
    MEME_MANAGER = "meme_manager"
    EXTERNAL_OTHER = "external_other"


_PLUGIN_SOURCE_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class PluginSourceEvidence:
    """Opaque proof issued by a trusted adapter, not by message text or a model."""

    source: PluginSource
    source_event_digest: str
    _seal: object = field(repr=False, compare=False)


def issue_plugin_source_evidence(
    *,
    source: PluginSource,
    source_event_digest: str,
) -> PluginSourceEvidence:
    if not isinstance(source, PluginSource) or source is PluginSource.NONE:
        raise ContractViolation("plugin_source_invalid")
    digest = require_hex(
        source_event_digest,
        "source_event_digest",
        lengths=(64,),
    )
    evidence = object.__new__(PluginSourceEvidence)
    object.__setattr__(evidence, "source", source)
    object.__setattr__(evidence, "source_event_digest", digest)
    object.__setattr__(evidence, "_seal", _PLUGIN_SOURCE_SEAL)
    return evidence


@dataclass(frozen=True, slots=True)
class RevisionCandidate:
    scope_key: str
    base_revision: int
    next_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_key", require_text(self.scope_key, "scope_key"))
        if (
            isinstance(self.base_revision, bool)
            or not isinstance(self.base_revision, int)
            or self.base_revision < 0
        ):
            raise ContractViolation("revision_base_invalid")
        if (
            isinstance(self.next_revision, bool)
            or not isinstance(self.next_revision, int)
        ):
            raise ContractViolation("revision_candidate_invalid")
        if self.next_revision != self.base_revision + 1:
            raise ContractViolation("revision_candidate_invalid")

    def trace_metadata(self) -> dict[str, str | int]:
        return {
            "scope_digest": diagnostic_digest(self.scope_key),
            "revision_base": self.base_revision,
            "revision_candidate": self.next_revision,
        }


@dataclass(frozen=True, slots=True)
class IngressEvent:
    """Pre-commit immutable event containing no visible message body."""

    envelope: TurnEnvelope
    principal: PrincipalContext
    sender_kind: SenderKind
    plugin_source: PluginSource
    plugin_source_evidence: PluginSourceEvidence | None
    content_digest: str
    revision_candidate: RevisionCandidate
    generation_epoch: int
    trace_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, TurnEnvelope):
            raise ContractViolation("turn_envelope_required")
        if not isinstance(self.principal, PrincipalContext):
            raise ContractViolation("principal_context_required")
        if not isinstance(self.sender_kind, SenderKind):
            raise ContractViolation("sender_kind_invalid")
        if not isinstance(self.plugin_source, PluginSource):
            raise ContractViolation("plugin_source_invalid")
        if not isinstance(self.revision_candidate, RevisionCandidate):
            raise ContractViolation("revision_candidate_required")
        object.__setattr__(
            self,
            "content_digest",
            require_hex(self.content_digest, "content_digest", lengths=(64,)),
        )
        object.__setattr__(
            self,
            "trace_id",
            require_hex(self.trace_id, "trace_id", lengths=(32,)),
        )
        if (
            isinstance(self.generation_epoch, bool)
            or not isinstance(self.generation_epoch, int)
            or self.generation_epoch < 1
        ):
            raise ContractViolation("generation_epoch_invalid")

        turn = self.envelope
        for value, reason in (
            (turn.scope_key, "event_scope_required"),
            (turn.session_id, "event_session_required"),
            (turn.message_id, "event_message_required"),
            (turn.sender_key, "event_sender_required"),
        ):
            if not str(value or "").strip():
                raise ContractViolation(reason)
        if self.revision_candidate.scope_key != turn.scope_key:
            raise ContractViolation("revision_event_scope_mismatch")
        if (
            self.principal.sender_key != turn.sender_key
            or self.principal.sender_id != turn.sender_id
        ):
            raise ContractViolation("principal_event_sender_mismatch")

        evidence = self.plugin_source_evidence
        if evidence is None:
            if self.plugin_source is not PluginSource.NONE:
                raise ContractViolation("plugin_source_evidence_required")
            if self.sender_kind is SenderKind.PLUGIN_ECHO:
                raise ContractViolation("plugin_source_evidence_required")
        else:
            if not isinstance(evidence, PluginSourceEvidence):
                raise ContractViolation("plugin_source_evidence_invalid")
            if getattr(evidence, "_seal", None) is not _PLUGIN_SOURCE_SEAL:
                raise ContractViolation("plugin_source_evidence_untrusted")
            if self.plugin_source is not evidence.source:
                raise ContractViolation("plugin_source_evidence_mismatch")
            if evidence.source is PluginSource.SHIO_REPLY:
                if self.sender_kind is not SenderKind.SELF:
                    raise ContractViolation("plugin_source_sender_kind_mismatch")
            elif self.sender_kind is not SenderKind.PLUGIN_ECHO:
                raise ContractViolation("plugin_source_sender_kind_mismatch")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "trace_id": self.trace_id,
            "scope_digest": diagnostic_digest(self.envelope.scope_key),
            "message_digest": diagnostic_digest(self.envelope.message_id),
            "sender_digest": diagnostic_digest(self.envelope.sender_key),
            "content_bound": bool(self.content_digest),
            "sender_kind": self.sender_kind.value,
            "plugin_source": self.plugin_source.value,
            "has_plugin_source_evidence": self.plugin_source_evidence is not None,
            "conversation_revision_candidate": self.revision_candidate.next_revision,
            "generation_epoch": self.generation_epoch,
            "has_reference": bool(self.envelope.reply_to_message_id),
            "principal_is_owner": self.principal.is_owner,
        }


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    """Accepted immutable event with one committed DecisionBinding."""

    ingress: IngressEvent
    binding: DecisionBinding

    def __post_init__(self) -> None:
        if self.binding.scope_key != self.ingress.envelope.scope_key:
            raise ContractViolation("conversation_event_scope_mismatch")
        if self.binding.session_id != self.ingress.envelope.session_id:
            raise ContractViolation("conversation_event_session_mismatch")
        if self.binding.current_message_id != self.ingress.envelope.message_id:
            raise ContractViolation("conversation_event_message_mismatch")
        if self.binding.current_sender_key != self.ingress.envelope.sender_key:
            raise ContractViolation("conversation_event_sender_mismatch")
        if self.binding.current_content_digest != self.ingress.content_digest:
            raise ContractViolation("conversation_event_content_mismatch")
        if (
            self.binding.conversation_revision
            != self.ingress.revision_candidate.next_revision
        ):
            raise ContractViolation("conversation_event_revision_mismatch")
        if self.binding.generation_epoch != self.ingress.generation_epoch:
            raise ContractViolation("conversation_event_epoch_mismatch")
        if self.binding.trace_id != self.ingress.trace_id:
            raise ContractViolation("conversation_event_trace_mismatch")

    @property
    def envelope(self) -> TurnEnvelope:
        return self.ingress.envelope

    @property
    def principal(self) -> PrincipalContext:
        return self.ingress.principal

    @property
    def sender_kind(self) -> SenderKind:
        return self.ingress.sender_kind

    @property
    def plugin_source(self) -> PluginSource:
        return self.ingress.plugin_source

    @property
    def content_digest(self) -> str:
        return self.ingress.content_digest

    def trace_metadata(self) -> dict[str, str | int | bool]:
        metadata = self.ingress.trace_metadata()
        metadata["conversation_revision"] = self.binding.conversation_revision
        metadata["event_committed"] = True
        return metadata


class RevisionCommitError(ContractViolation):
    pass


class ConversationRevisionBook:
    """Atomic per-scope revisions; peeking and rejected commits never write."""

    def __init__(
        self,
        *,
        continuity_store: RuntimeContinuityStore | None = None,
    ) -> None:
        if continuity_store is not None and type(continuity_store) is not RuntimeContinuityStore:
            raise RevisionCommitError("revision_continuity_store_invalid")
        self._continuity_store = continuity_store
        self._revisions: dict[str, int] = {}
        self._restored_revisions: dict[str, int] = {}
        self._lock = threading.RLock()

    def _current_locked(self, scope: str) -> int:
        existing = self._revisions.get(scope)
        if existing is not None:
            return existing
        restored = 0
        store = self._continuity_store
        if store is not None:
            try:
                restored = store.revision_for_scope(scope)
            except RuntimeContinuityError:
                restored = 0
        self._revisions[scope] = restored
        if restored > 0:
            self._restored_revisions[scope] = restored
        return restored

    def current(self, scope_key: str) -> int:
        scope = require_text(scope_key, "scope_key")
        with self._lock:
            return self._current_locked(scope)

    def restored_revision(self, scope_key: str) -> int:
        scope = require_text(scope_key, "scope_key")
        with self._lock:
            self._current_locked(scope)
            return self._restored_revisions.get(scope, 0)

    @property
    def persistence_available(self) -> bool:
        store = self._continuity_store
        return bool(store is not None and store.enabled)

    def peek(self, scope_key: str) -> RevisionCandidate:
        scope = require_text(scope_key, "scope_key")
        with self._lock:
            current = self._current_locked(scope)
        return RevisionCandidate(
            scope_key=scope,
            base_revision=current,
            next_revision=current + 1,
        )

    def commit(
        self,
        ingress: IngressEvent,
        *,
        accepted: bool,
        scope_key: str,
    ) -> ConversationEvent | None:
        if not isinstance(ingress, IngressEvent):
            raise RevisionCommitError("ingress_event_required")
        if type(accepted) is not bool:
            raise RevisionCommitError("accepted_flag_invalid")
        scope = require_text(scope_key, "scope_key")
        if scope != ingress.envelope.scope_key:
            raise RevisionCommitError("revision_scope_mismatch")
        candidate = ingress.revision_candidate
        if candidate.scope_key != scope:
            raise RevisionCommitError("revision_scope_mismatch")

        binding = DecisionBinding(
            scope_key=scope,
            session_id=ingress.envelope.session_id,
            current_message_id=ingress.envelope.message_id,
            current_sender_key=ingress.envelope.sender_key,
            current_content_digest=ingress.content_digest,
            conversation_revision=candidate.next_revision,
            generation_epoch=ingress.generation_epoch,
            trace_id=ingress.trace_id,
        )
        event = ConversationEvent(ingress=ingress, binding=binding)

        with self._lock:
            current = self._current_locked(scope)
            if (
                candidate.base_revision != current
                or candidate.next_revision != current + 1
            ):
                raise RevisionCommitError("revision_candidate_stale")
            if not accepted:
                return None
            store = self._continuity_store
            if store is not None:
                try:
                    store.commit_revision(
                        scope,
                        base_revision=current,
                        next_revision=candidate.next_revision,
                    )
                except RuntimeContinuityError:
                    # Direct/current-message handling remains available in this
                    # process. Cadence sees the same disabled store and blocks
                    # unsolicited participation until persistence is repaired.
                    pass
            self._revisions[scope] = candidate.next_revision
        return event


def build_ingress_event(
    *,
    envelope: TurnEnvelope,
    principal: PrincipalContext,
    sender_kind: SenderKind,
    content: str | bytes,
    revision_candidate: RevisionCandidate,
    plugin_source_evidence: PluginSourceEvidence | None = None,
    generation_epoch: int | None = None,
) -> IngressEvent:
    if not isinstance(envelope, TurnEnvelope):
        raise ContractViolation("turn_envelope_required")
    if not isinstance(principal, PrincipalContext):
        raise ContractViolation("principal_context_required")
    if not isinstance(sender_kind, SenderKind):
        raise ContractViolation("sender_kind_invalid")
    if not isinstance(revision_candidate, RevisionCandidate):
        raise ContractViolation("revision_candidate_required")
    if isinstance(content, str):
        content_bytes = content.encode("utf-8")
    elif isinstance(content, bytes):
        content_bytes = content
    else:
        raise ContractViolation("event_content_type_invalid")

    if plugin_source_evidence is None:
        plugin_source = PluginSource.NONE
    elif isinstance(plugin_source_evidence, PluginSourceEvidence):
        if getattr(plugin_source_evidence, "_seal", None) is not _PLUGIN_SOURCE_SEAL:
            raise ContractViolation("plugin_source_evidence_untrusted")
        plugin_source = plugin_source_evidence.source
    else:
        raise ContractViolation("plugin_source_evidence_invalid")

    epoch = (
        revision_candidate.next_revision
        if generation_epoch is None
        else generation_epoch
    )
    return IngressEvent(
        envelope=envelope,
        principal=principal,
        sender_kind=sender_kind,
        plugin_source=plugin_source,
        plugin_source_evidence=plugin_source_evidence,
        content_digest=hashlib.sha256(content_bytes).hexdigest(),
        revision_candidate=revision_candidate,
        generation_epoch=epoch,
        trace_id=secrets.token_hex(16),
    )


__all__ = [
    "ConversationEvent",
    "ConversationRevisionBook",
    "IngressEvent",
    "PluginSource",
    "PluginSourceEvidence",
    "RevisionCandidate",
    "RevisionCommitError",
    "build_ingress_event",
    "issue_plugin_source_evidence",
]
