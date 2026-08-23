from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field

from .contracts import (
    ContractViolation,
    DecisionBinding,
    EvidenceConsumer,
    ExternalPluginEvidence,
    IngressDecision,
    IngressDisposition,
    PluginEvidenceKind,
    PluginEvidenceStatus,
    SenderKind,
)
from .contracts._validation import require_hex
from .conversation_event import (
    ConversationEvent,
    ConversationRevisionBook,
    IngressEvent,
    PluginSource,
)


_GATE_OBSERVATION_SEAL = object()
_ADMISSION_PROOF_SEAL = object()
_GATE_OBSERVATION_CLAIM_LOCK = threading.RLock()
_DEFAULT_MAX_PROOFS = 1024


@dataclass(frozen=True, slots=True, init=False)
class GateObservation:
    """Opaque ReNeBan compatibility observation issued at the adapter boundary."""

    status: PluginEvidenceStatus
    banned: bool | None
    source_event_digest: str = field(repr=False)
    binding_digest: str = field(repr=False)
    _issuer_seal: object | None = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)
    _claimed_by: object | None = field(repr=False, compare=False)


def issue_gate_observation(
    *,
    status: PluginEvidenceStatus,
    banned: bool | None,
    source_event_digest: str = "",
) -> GateObservation:
    """Issue only fail-closed degraded observations without controller trust.

    VERIFIED observations must be bound to an exact IngressEvent by
    ``IngressAdmissionController.issue_gate_observation``.
    """

    if not isinstance(status, PluginEvidenceStatus):
        raise ContractViolation("gate_status_invalid")
    if status is PluginEvidenceStatus.VERIFIED:
        if type(banned) is not bool:
            raise ContractViolation("verified_gate_verdict_required")
        require_hex(
            source_event_digest,
            "source_event_digest",
            lengths=(64,),
        )
        raise ContractViolation("verified_gate_controller_required")
    else:
        if banned is not None:
            raise ContractViolation("unverified_gate_verdict_forbidden")
        digest = (
            require_hex(
                source_event_digest,
                "source_event_digest",
                lengths=(64,),
            )
            if source_event_digest
            else ""
        )
    observation = object.__new__(GateObservation)
    object.__setattr__(observation, "status", status)
    object.__setattr__(observation, "banned", banned)
    object.__setattr__(observation, "source_event_digest", digest)
    object.__setattr__(observation, "binding_digest", "")
    object.__setattr__(observation, "_issuer_seal", None)
    object.__setattr__(observation, "_seal", _GATE_OBSERVATION_SEAL)
    object.__setattr__(observation, "_claimed_by", None)
    return observation


def _digest_parts(*parts: object) -> str:
    encoded = json.dumps(
        parts,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _binding_digest(binding: DecisionBinding) -> str:
    return _digest_parts(
        binding.scope_key,
        binding.session_id,
        binding.current_message_id,
        binding.current_sender_key,
        binding.current_content_digest,
        binding.conversation_revision,
        binding.generation_epoch,
        binding.trace_id,
    )


def _ingress_digest(event: IngressEvent) -> str:
    return _digest_parts(
        _binding_digest(_candidate_binding(event)),
        event.sender_kind.value,
        event.plugin_source.value,
        event.content_digest,
        event.revision_candidate.base_revision,
        event.revision_candidate.next_revision,
        event.generation_epoch,
        event.trace_id,
    )


def _gate_digest(
    *,
    status: PluginEvidenceStatus,
    banned: bool | None,
    source_event_digest: str,
) -> str:
    return _digest_parts(
        status.value,
        banned,
        source_event_digest,
    )


@dataclass(frozen=True, slots=True, init=False)
class AdmissionProof:
    """Opaque, one-time proof issued only after a canonical revision commit."""

    binding_digest: str = field(repr=False)
    ingress_digest: str = field(repr=False)
    gate_digest: str = field(repr=False)
    commit_digest: str = field(repr=False)
    conversation_revision: int
    _issuer_seal: object = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def trace_metadata(self) -> dict[str, int | bool]:
        return {
            "schema_version": 1,
            "admission_proof_issued": True,
            "binding_bound": bool(self.binding_digest),
            "ingress_bound": bool(self.ingress_digest),
            "gate_bound": bool(self.gate_digest),
            "commit_bound": bool(self.commit_digest),
            "conversation_revision": self.conversation_revision,
        }


def _candidate_binding(event: IngressEvent) -> DecisionBinding:
    return DecisionBinding(
        scope_key=event.envelope.scope_key,
        session_id=event.envelope.session_id,
        current_message_id=event.envelope.message_id,
        current_sender_key=event.envelope.sender_key,
        current_content_digest=event.content_digest,
        conversation_revision=event.revision_candidate.next_revision,
        generation_epoch=event.generation_epoch,
        trace_id=event.trace_id,
    )


def _external_gate_evidence(
    binding: DecisionBinding,
    observation: GateObservation,
) -> ExternalPluginEvidence:
    verified = observation.status is PluginEvidenceStatus.VERIFIED
    return ExternalPluginEvidence(
        binding=binding,
        plugin_id="reneban",
        evidence_kind=PluginEvidenceKind.GATE_DECISION,
        status=observation.status,
        trusted=verified,
        allowed_consumers=((EvidenceConsumer.INGRESS,) if verified else ()),
        source_event_digest=observation.source_event_digest,
        reason_codes=(
            ()
            if verified
            else (f"reneban_{observation.status.value}",)
        ),
    )


def _disposition(
    event: IngressEvent,
    observation: GateObservation,
) -> tuple[IngressDisposition, tuple[str, ...]]:
    sender_kind = event.sender_kind
    if sender_kind is SenderKind.SELF:
        return IngressDisposition.DROP_SELF, ("sender_self",)
    if sender_kind is SenderKind.KNOWN_BOT:
        return IngressDisposition.DROP_KNOWN_BOT, ("sender_known_bot",)
    if sender_kind is SenderKind.PLUGIN_ECHO:
        return IngressDisposition.DROP_PLUGIN_ECHO, ("sender_plugin_echo",)
    if sender_kind in {SenderKind.UNKNOWN_AUTOMATION, SenderKind.UNKNOWN}:
        return (
            IngressDisposition.DEGRADED_EXTERNAL_GATE,
            ("sender_unverified_automation",),
        )
    if sender_kind is not SenderKind.HUMAN:
        raise ContractViolation("sender_kind_unsupported")
    if observation.status is not PluginEvidenceStatus.VERIFIED:
        return (
            IngressDisposition.DEGRADED_EXTERNAL_GATE,
            (f"reneban_{observation.status.value}",),
        )
    if observation.banned is True:
        return IngressDisposition.DROP_BANNED, ("reneban_banned",)
    return IngressDisposition.ACCEPT_HUMAN, ()


def _proof_matches_result(
    proof: AdmissionProof,
    *,
    ingress_event: IngressEvent,
    gate_status: PluginEvidenceStatus,
    gate_evidence: ExternalPluginEvidence,
    decision: IngressDecision,
    conversation_event: ConversationEvent,
) -> bool:
    return bool(
        isinstance(proof, AdmissionProof)
        and getattr(proof, "_seal", None) is _ADMISSION_PROOF_SEAL
        and proof.binding_digest == _binding_digest(decision.binding)
        and proof.ingress_digest == _ingress_digest(ingress_event)
        and proof.gate_digest
        == _gate_digest(
            status=gate_status,
            banned=False,
            source_event_digest=gate_evidence.source_event_digest,
        )
        and proof.conversation_revision
        == conversation_event.binding.conversation_revision
        and conversation_event.binding == decision.binding
        and conversation_event.ingress is ingress_event
    )


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    ingress_event: IngressEvent = field(repr=False)
    gate_status: PluginEvidenceStatus
    gate_evidence: ExternalPluginEvidence = field(repr=False)
    decision: IngressDecision = field(repr=False)
    conversation_event: ConversationEvent | None = field(repr=False)
    admission_proof: AdmissionProof | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.decision.binding != self.gate_evidence.binding:
            raise ContractViolation("admission_gate_binding_mismatch")
        if self.decision.binding.trace_id != self.ingress_event.trace_id:
            raise ContractViolation("admission_trace_binding_mismatch")
        if self.gate_status is not self.gate_evidence.status:
            raise ContractViolation("admission_gate_status_mismatch")
        if self.decision.allows_state_mutation:
            if self.conversation_event is None:
                raise ContractViolation("accepted_conversation_event_required")
            if self.conversation_event.binding != self.decision.binding:
                raise ContractViolation("admission_conversation_binding_mismatch")
            if self.admission_proof is None:
                raise ContractViolation("accepted_admission_proof_required")
            if not _proof_matches_result(
                self.admission_proof,
                ingress_event=self.ingress_event,
                gate_status=self.gate_status,
                gate_evidence=self.gate_evidence,
                decision=self.decision,
                conversation_event=self.conversation_event,
            ):
                raise ContractViolation("admission_proof_binding_mismatch")
        elif self.conversation_event is not None:
            raise ContractViolation("dropped_conversation_event_forbidden")
        elif self.admission_proof is not None:
            raise ContractViolation("dropped_admission_proof_forbidden")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        ingress = self.ingress_event.trace_metadata()
        return {
            "schema_version": 1,
            "scope_digest": ingress["scope_digest"],
            "message_digest": ingress["message_digest"],
            "sender_digest": ingress["sender_digest"],
            "trace_bound": bool(self.ingress_event.trace_id),
            "content_bound": bool(self.ingress_event.content_digest),
            "sender_kind": self.decision.sender_kind.value,
            "plugin_source": self.ingress_event.plugin_source.value,
            "gate_status": self.gate_status.value,
            "ingress_status": self.decision.disposition.value,
            "ingress_allowed": self.decision.allows_state_mutation,
            "admission_proof_issued": self.admission_proof is not None,
            "conversation_revision_candidate": (
                self.ingress_event.revision_candidate.next_revision
            ),
            "conversation_revision_committed": (
                self.conversation_event.binding.conversation_revision
                if self.conversation_event is not None
                else 0
            ),
        }


class IngressAdmissionController:
    """Deterministic admission; only ACCEPT_HUMAN advances the revision book."""

    __slots__ = (
        "_consumed_proofs",
        "_controller_nonce",
        "_issuer_seal",
        "_lock",
        "_max_proofs",
        "_proofs",
        "_revision_book",
    )

    def __init__(
        self,
        revision_book: ConversationRevisionBook,
        *,
        max_proofs: int = _DEFAULT_MAX_PROOFS,
    ):
        if not isinstance(revision_book, ConversationRevisionBook):
            raise ContractViolation("conversation_revision_book_required")
        if (
            isinstance(max_proofs, bool)
            or not isinstance(max_proofs, int)
            or max_proofs < 1
        ):
            raise ContractViolation("admission_proof_limit_invalid")
        self._revision_book = revision_book
        self._max_proofs = max_proofs
        self._issuer_seal = object()
        self._controller_nonce = secrets.token_hex(16)
        self._proofs: dict[int, tuple[AdmissionProof, AdmissionResult]] = {}
        self._consumed_proofs: dict[int, AdmissionProof] = {}
        self._lock = threading.RLock()

    def issue_gate_observation(
        self,
        event: IngressEvent,
        *,
        status: PluginEvidenceStatus,
        banned: bool | None,
    ) -> GateObservation:
        """Bind one gate verdict to this controller and exact ingress event."""

        if not isinstance(event, IngressEvent):
            raise ContractViolation("ingress_event_required")
        if not isinstance(status, PluginEvidenceStatus):
            raise ContractViolation("gate_status_invalid")
        if status is PluginEvidenceStatus.VERIFIED:
            if type(banned) is not bool:
                raise ContractViolation("verified_gate_verdict_required")
        elif banned is not None:
            raise ContractViolation("unverified_gate_verdict_forbidden")
        observation = object.__new__(GateObservation)
        object.__setattr__(observation, "status", status)
        object.__setattr__(observation, "banned", banned)
        object.__setattr__(
            observation,
            "source_event_digest",
            event.content_digest,
        )
        object.__setattr__(
            observation,
            "binding_digest",
            _binding_digest(_candidate_binding(event)),
        )
        object.__setattr__(observation, "_issuer_seal", self._issuer_seal)
        object.__setattr__(observation, "_seal", _GATE_OBSERVATION_SEAL)
        object.__setattr__(observation, "_claimed_by", None)
        return observation

    def _verify_gate_observation_binding(
        self,
        event: IngressEvent,
        observation: GateObservation,
    ) -> None:
        expected_binding = _binding_digest(_candidate_binding(event))
        binding_digest = str(getattr(observation, "binding_digest", "") or "")
        issuer_seal = getattr(observation, "_issuer_seal", None)
        if observation.status is PluginEvidenceStatus.VERIFIED:
            if issuer_seal is not self._issuer_seal:
                raise ContractViolation("gate_observation_controller_mismatch")
            if binding_digest != expected_binding:
                raise ContractViolation("gate_observation_event_mismatch")
            if observation.source_event_digest != event.content_digest:
                raise ContractViolation("gate_observation_event_mismatch")
            return
        if binding_digest:
            if issuer_seal is not self._issuer_seal:
                raise ContractViolation("gate_observation_controller_mismatch")
            if binding_digest != expected_binding:
                raise ContractViolation("gate_observation_event_mismatch")
        if (
            observation.source_event_digest
            and observation.source_event_digest != event.content_digest
        ):
            raise ContractViolation("gate_observation_event_mismatch")

    def _claim_gate_observation(self, observation: GateObservation) -> None:
        with _GATE_OBSERVATION_CLAIM_LOCK:
            claimed_by = getattr(observation, "_claimed_by", None)
            if claimed_by is not None:
                raise ContractViolation("gate_observation_replayed")
            object.__setattr__(observation, "_claimed_by", self._issuer_seal)

    def _issue_proof(
        self,
        *,
        ingress_event: IngressEvent,
        observation: GateObservation,
        gate_evidence: ExternalPluginEvidence,
        decision: IngressDecision,
        conversation_event: ConversationEvent,
    ) -> AdmissionProof:
        binding_digest = _binding_digest(decision.binding)
        ingress_digest = _ingress_digest(ingress_event)
        gate_digest = _gate_digest(
            status=observation.status,
            banned=observation.banned,
            source_event_digest=observation.source_event_digest,
        )
        revision = conversation_event.binding.conversation_revision
        proof = object.__new__(AdmissionProof)
        object.__setattr__(proof, "binding_digest", binding_digest)
        object.__setattr__(proof, "ingress_digest", ingress_digest)
        object.__setattr__(proof, "gate_digest", gate_digest)
        object.__setattr__(
            proof,
            "commit_digest",
            _digest_parts(
                self._controller_nonce,
                binding_digest,
                ingress_digest,
                gate_digest,
                revision,
            ),
        )
        object.__setattr__(proof, "conversation_revision", revision)
        object.__setattr__(proof, "_issuer_seal", self._issuer_seal)
        object.__setattr__(proof, "_seal", _ADMISSION_PROOF_SEAL)
        return proof

    def _canonical_proof_entry(
        self,
        admission: AdmissionResult,
    ) -> tuple[AdmissionProof, AdmissionResult]:
        if not isinstance(admission, AdmissionResult):
            raise ContractViolation("admission_result_required")
        proof = admission.admission_proof
        if (
            not isinstance(proof, AdmissionProof)
            or getattr(proof, "_seal", None) is not _ADMISSION_PROOF_SEAL
        ):
            raise ContractViolation("admission_proof_untrusted")
        if getattr(proof, "_issuer_seal", None) is not self._issuer_seal:
            raise ContractViolation("admission_proof_controller_mismatch")
        proof_key = id(proof)
        if self._consumed_proofs.get(proof_key) is proof:
            raise ContractViolation("admission_proof_replayed")
        entry = self._proofs.get(proof_key)
        if entry is None or entry[0] is not proof or entry[1] is not admission:
            raise ContractViolation("admission_result_not_canonical")
        if admission.conversation_event is None or not _proof_matches_result(
            proof,
            ingress_event=admission.ingress_event,
            gate_status=admission.gate_status,
            gate_evidence=admission.gate_evidence,
            decision=admission.decision,
            conversation_event=admission.conversation_event,
        ):
            raise ContractViolation("admission_proof_binding_mismatch")
        expected_commit = _digest_parts(
            self._controller_nonce,
            proof.binding_digest,
            proof.ingress_digest,
            proof.gate_digest,
            proof.conversation_revision,
        )
        if proof.commit_digest != expected_commit:
            raise ContractViolation("admission_proof_commit_mismatch")
        if self._revision_book.current(admission.decision.binding.scope_key) < (
            proof.conversation_revision
        ):
            raise ContractViolation("admission_proof_commit_missing")
        return entry

    def inspect_admission_proof(
        self,
        admission: AdmissionResult,
    ) -> AdmissionProof:
        """Return only an active proof for this exact canonical result object."""

        with self._lock:
            proof, _ = self._canonical_proof_entry(admission)
            return proof

    def claim_admission_proof(
        self,
        admission: AdmissionResult,
    ) -> AdmissionProof:
        """Atomically consume an active proof; replay and cross-controller fail."""

        with self._lock:
            proof, _ = self._canonical_proof_entry(admission)
            proof_key = id(proof)
            self._proofs.pop(proof_key, None)
            self._consumed_proofs[proof_key] = proof
            while len(self._consumed_proofs) > self._max_proofs:
                oldest = next(iter(self._consumed_proofs))
                self._consumed_proofs.pop(oldest, None)
            return proof

    def admit(
        self,
        event: IngressEvent,
        *,
        gate_observation: GateObservation | None,
    ) -> AdmissionResult:
        if not isinstance(event, IngressEvent):
            raise ContractViolation("ingress_event_required")
        if event.sender_kind is SenderKind.PLUGIN_ECHO:
            if (
                event.plugin_source is PluginSource.NONE
                or event.plugin_source_evidence is None
            ):
                raise ContractViolation("plugin_source_evidence_required")
        if gate_observation is None:
            observation = issue_gate_observation(
                status=PluginEvidenceStatus.MISSING,
                banned=None,
            )
        else:
            if not isinstance(gate_observation, GateObservation):
                raise ContractViolation("gate_observation_invalid")
            if getattr(gate_observation, "_seal", None) is not _GATE_OBSERVATION_SEAL:
                raise ContractViolation("gate_observation_untrusted")
            observation = gate_observation
        self._verify_gate_observation_binding(event, observation)
        self._claim_gate_observation(observation)

        binding = _candidate_binding(event)
        evidence = _external_gate_evidence(binding, observation)
        disposition, reason_codes = _disposition(event, observation)
        candidate_decision = IngressDecision(
            binding=binding,
            sender_kind=event.sender_kind,
            disposition=disposition,
            gate_evidence=(evidence,),
            reason_codes=reason_codes,
        )
        conversation_event = self._revision_book.commit(
            event,
            accepted=candidate_decision.allows_state_mutation,
            scope_key=event.envelope.scope_key,
        )
        decision = (
            IngressDecision(
                binding=conversation_event.binding,
                sender_kind=candidate_decision.sender_kind,
                disposition=candidate_decision.disposition,
                gate_evidence=candidate_decision.gate_evidence,
                reason_codes=candidate_decision.reason_codes,
            )
            if conversation_event is not None
            else candidate_decision
        )
        proof = (
            self._issue_proof(
                ingress_event=event,
                observation=observation,
                gate_evidence=evidence,
                decision=decision,
                conversation_event=conversation_event,
            )
            if conversation_event is not None
            else None
        )
        result = AdmissionResult(
            ingress_event=event,
            gate_status=observation.status,
            gate_evidence=evidence,
            decision=decision,
            conversation_event=conversation_event,
            admission_proof=proof,
        )
        if proof is not None:
            with self._lock:
                while len(self._proofs) >= self._max_proofs:
                    oldest = next(iter(self._proofs))
                    self._proofs.pop(oldest, None)
                self._proofs[id(proof)] = (proof, result)
        return result


__all__ = [
    "AdmissionProof",
    "AdmissionResult",
    "GateObservation",
    "IngressAdmissionController",
    "issue_gate_observation",
]
