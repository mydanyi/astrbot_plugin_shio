from __future__ import annotations

import dataclasses
import json
import unittest

from astrbot_plugin_shio.core.contracts import (
    ContractViolation,
    IngressDisposition,
    PluginEvidenceStatus,
    SenderKind,
)
from astrbot_plugin_shio.core.conversation_event import (
    ConversationRevisionBook,
    PluginSource,
    RevisionCommitError,
    build_ingress_event,
    issue_plugin_source_evidence,
)
from astrbot_plugin_shio.core.identity import PrincipalContext, TurnEnvelope
from astrbot_plugin_shio.core.ingress_admission import (
    AdmissionProof,
    AdmissionResult,
    GateObservation,
    IngressAdmissionController,
    issue_gate_observation,
)


SCOPE = "platform:test|bot:shio|group:group-a"


def envelope(*, message: str = "message-a", sender: str = "peer-a") -> TurnEnvelope:
    return TurnEnvelope(
        session_id="session-a",
        message_id=message,
        scope_key=SCOPE,
        sender_key=f"{SCOPE}|user:{sender}",
        sender_id=sender,
        platform_id="test",
        bot_id="shio",
        chat_type="group",
        group_id="group-a",
        reply_to_message_id="",
        reply_to_sender_id="",
        timestamp=1.0,
        timestamp_source="message",
        source_kind="inbound",
        degradation_reasons=(),
    )


def principal(turn: TurnEnvelope) -> PrincipalContext:
    return PrincipalContext(
        sender_key=turn.sender_key,
        sender_id=turn.sender_id,
        is_owner=False,
        relationship_role="group_peer",
        verification_source="synthetic_structured_sender",
    )


def gate(
    status: PluginEvidenceStatus,
    *,
    banned: bool | None = None,
    event=None,
    controller: IngressAdmissionController | None = None,
):
    if status is PluginEvidenceStatus.VERIFIED:
        if event is None or controller is None:
            raise AssertionError("verified gate fixture requires controller/event")
        return controller.issue_gate_observation(
            event,
            status=status,
            banned=banned,
        )
    return issue_gate_observation(
        status=status,
        banned=banned,
        source_event_digest="",
    )


def ingress(
    book: ConversationRevisionBook,
    *,
    sender_kind: SenderKind,
    message: str = "message-a",
    sender: str = "peer-a",
    content: str = "synthetic body",
):
    turn = envelope(message=message, sender=sender)
    source_evidence = None
    if sender_kind is SenderKind.PLUGIN_ECHO:
        source_evidence = issue_plugin_source_evidence(
            source=PluginSource.PARSER,
            source_event_digest="b" * 64,
        )
    return build_ingress_event(
        envelope=turn,
        principal=principal(turn),
        sender_kind=sender_kind,
        content=content,
        revision_candidate=book.peek(turn.scope_key),
        plugin_source_evidence=source_evidence,
    )


class IngressAdmissionTests(unittest.TestCase):
    def test_controller_issues_canonical_one_time_admission_proof(self):
        book = ConversationRevisionBook()
        controller = IngressAdmissionController(book)
        event = ingress(
            book,
            sender_kind=SenderKind.HUMAN,
            content="proof-private-body",
        )
        result = controller.admit(
            event,
            gate_observation=gate(
                PluginEvidenceStatus.VERIFIED,
                banned=False,
                event=event,
                controller=controller,
            ),
        )

        proof = result.admission_proof
        self.assertIsInstance(proof, AdmissionProof)
        self.assertIs(controller.inspect_admission_proof(result), proof)
        self.assertFalse(hasattr(proof, "__dict__"))
        rendered = (
            repr(result)
            + repr(proof)
            + json.dumps(result.trace_metadata(), ensure_ascii=False)
            + json.dumps(proof.trace_metadata(), ensure_ascii=False)
        )
        for forbidden in (
            "proof-private-body",
            event.envelope.message_id,
            event.envelope.sender_id,
            event.envelope.sender_key,
            event.envelope.session_id,
            event.envelope.scope_key,
            event.trace_id,
            event.content_digest,
        ):
            self.assertNotIn(forbidden, rendered)

        self.assertIs(controller.claim_admission_proof(result), proof)
        with self.assertRaisesRegex(ContractViolation, "admission_proof_replayed"):
            controller.claim_admission_proof(result)
        with self.assertRaisesRegex(ContractViolation, "admission_proof_replayed"):
            controller.inspect_admission_proof(result)

    def test_public_forgery_cross_controller_and_cross_admission_are_rejected(self):
        book = ConversationRevisionBook()
        controller = IngressAdmissionController(book)
        first_event = ingress(
            book,
            sender_kind=SenderKind.HUMAN,
            message="first-proof",
        )
        first = controller.admit(
            first_event,
            gate_observation=gate(
                PluginEvidenceStatus.VERIFIED,
                banned=False,
                event=first_event,
                controller=controller,
            ),
        )
        assert first.admission_proof is not None

        forged_result = dataclasses.replace(first)
        self.assertIsNot(forged_result, first)
        with self.assertRaisesRegex(
            ContractViolation,
            "admission_result_not_canonical",
        ):
            controller.inspect_admission_proof(forged_result)

        copied_proof = object.__new__(AdmissionProof)
        for proof_field in dataclasses.fields(AdmissionProof):
            object.__setattr__(
                copied_proof,
                proof_field.name,
                getattr(first.admission_proof, proof_field.name),
            )
        copied_result = dataclasses.replace(
            first,
            admission_proof=copied_proof,
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "admission_result_not_canonical",
        ):
            controller.inspect_admission_proof(copied_result)

        forged_proof = object.__new__(AdmissionProof)
        for name, value in (
            ("binding_digest", "a" * 64),
            ("ingress_digest", "b" * 64),
            ("gate_digest", "c" * 64),
            ("commit_digest", "d" * 64),
            ("conversation_revision", 1),
            ("_issuer_seal", object()),
            ("_seal", object()),
        ):
            object.__setattr__(forged_proof, name, value)
        with self.assertRaisesRegex(
            ContractViolation,
            "admission_proof_binding_mismatch",
        ):
            dataclasses.replace(
                first,
                admission_proof=forged_proof,
            )

        other_controller = IngressAdmissionController(ConversationRevisionBook())
        with self.assertRaisesRegex(
            ContractViolation,
            "admission_proof_controller_mismatch",
        ):
            other_controller.inspect_admission_proof(first)

        second_event = ingress(
            book,
            sender_kind=SenderKind.HUMAN,
            message="second-proof",
        )
        second = controller.admit(
            second_event,
            gate_observation=gate(
                PluginEvidenceStatus.VERIFIED,
                banned=False,
                event=second_event,
                controller=controller,
            ),
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "admission_proof_binding_mismatch",
        ):
            dataclasses.replace(second, admission_proof=first.admission_proof)

    def test_gate_observation_is_sealed_event_bound_and_one_time(self):
        book = ConversationRevisionBook()
        controller = IngressAdmissionController(book)
        first_event = ingress(
            book,
            sender_kind=SenderKind.HUMAN,
            message="gate-first",
        )
        observation = gate(
            PluginEvidenceStatus.VERIFIED,
            banned=False,
            event=first_event,
            controller=controller,
        )
        controller.admit(
            first_event,
            gate_observation=observation,
        )
        with self.assertRaisesRegex(ContractViolation, "gate_observation_replayed"):
            controller.admit(
                first_event,
                gate_observation=observation,
            )
        with self.assertRaisesRegex(
            ContractViolation,
            "gate_observation_controller_mismatch",
        ):
            IngressAdmissionController(ConversationRevisionBook()).admit(
                first_event,
                gate_observation=observation,
            )

        forged = object.__new__(GateObservation)
        object.__setattr__(forged, "status", PluginEvidenceStatus.VERIFIED)
        object.__setattr__(forged, "banned", False)
        object.__setattr__(forged, "source_event_digest", "e" * 64)
        object.__setattr__(forged, "_seal", object())
        with self.assertRaisesRegex(ContractViolation, "gate_observation_untrusted"):
            IngressAdmissionController(ConversationRevisionBook()).admit(
                ingress(
                    ConversationRevisionBook(),
                    sender_kind=SenderKind.HUMAN,
                    message="gate-forged",
                ),
                gate_observation=forged,
            )

    def test_verified_gate_digest_must_match_exact_current_ingress(self):
        book = ConversationRevisionBook()
        controller = IngressAdmissionController(book)
        source_event = ingress(
            book,
            sender_kind=SenderKind.HUMAN,
            message="digest-source",
            content="source ingress body",
        )
        target_event = ingress(
            book,
            sender_kind=SenderKind.HUMAN,
            message="digest-target",
            content="different target ingress body",
        )
        self.assertNotEqual(source_event.content_digest, target_event.content_digest)
        with self.assertRaisesRegex(
            ContractViolation,
            "verified_gate_controller_required",
        ):
            issue_gate_observation(
                status=PluginEvidenceStatus.VERIFIED,
                banned=False,
                source_event_digest=target_event.content_digest,
            )
        observation = controller.issue_gate_observation(
            source_event,
            status=PluginEvidenceStatus.VERIFIED,
            banned=False,
        )

        with self.assertRaisesRegex(
            ContractViolation,
            "gate_observation_event_mismatch",
        ):
            controller.admit(
                target_event,
                gate_observation=observation,
            )

        self.assertEqual(book.current(SCOPE), 0)
        accepted = controller.admit(
            source_event,
            gate_observation=observation,
        )
        self.assertIs(accepted.decision.disposition, IngressDisposition.ACCEPT_HUMAN)
        self.assertIsNotNone(accepted.admission_proof)

    def test_verified_non_banned_human_is_the_only_accept_and_commits(self):
        book = ConversationRevisionBook()
        controller = IngressAdmissionController(book)
        event = ingress(book, sender_kind=SenderKind.HUMAN)
        result = controller.admit(
            event,
            gate_observation=gate(
                PluginEvidenceStatus.VERIFIED,
                banned=False,
                event=event,
                controller=controller,
            ),
        )

        self.assertIsInstance(result, AdmissionResult)
        self.assertIs(result.decision.disposition, IngressDisposition.ACCEPT_HUMAN)
        self.assertTrue(result.decision.allows_state_mutation)
        self.assertIsNotNone(result.conversation_event)
        self.assertIsInstance(result.admission_proof, AdmissionProof)
        self.assertIs(result.conversation_event.binding, result.decision.binding)
        self.assertEqual(book.current(SCOPE), 1)
        self.assertFalse(hasattr(result, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.conversation_event = None

    def test_verified_banned_human_is_dropped_before_revision_commit(self):
        book = ConversationRevisionBook()
        controller = IngressAdmissionController(book)
        event = ingress(book, sender_kind=SenderKind.HUMAN)
        result = controller.admit(
            event,
            gate_observation=gate(
                PluginEvidenceStatus.VERIFIED,
                banned=True,
                event=event,
                controller=controller,
            ),
        )

        self.assertIs(result.decision.disposition, IngressDisposition.DROP_BANNED)
        self.assertFalse(result.decision.allows_state_mutation)
        self.assertIsNone(result.conversation_event)
        self.assertIsNone(result.admission_proof)
        self.assertEqual(book.current(SCOPE), 0)

    def test_self_known_bot_and_sealed_plugin_echo_are_deterministic_drops(self):
        cases = (
            (SenderKind.SELF, IngressDisposition.DROP_SELF),
            (SenderKind.KNOWN_BOT, IngressDisposition.DROP_KNOWN_BOT),
            (SenderKind.PLUGIN_ECHO, IngressDisposition.DROP_PLUGIN_ECHO),
        )
        for sender_kind, expected in cases:
            with self.subTest(sender_kind=sender_kind):
                book = ConversationRevisionBook()
                controller = IngressAdmissionController(book)
                event = ingress(book, sender_kind=sender_kind)
                result = controller.admit(
                    event,
                    gate_observation=gate(
                        PluginEvidenceStatus.VERIFIED,
                        banned=False,
                        event=event,
                        controller=controller,
                    ),
                )
                self.assertIs(result.decision.disposition, expected)
                self.assertIsNone(result.conversation_event)
                self.assertEqual(book.current(SCOPE), 0)

    def test_unknown_automation_and_unknown_sender_never_commit(self):
        for sender_kind in (SenderKind.UNKNOWN_AUTOMATION, SenderKind.UNKNOWN):
            with self.subTest(sender_kind=sender_kind):
                book = ConversationRevisionBook()
                controller = IngressAdmissionController(book)
                event = ingress(book, sender_kind=sender_kind)
                result = controller.admit(
                    event,
                    gate_observation=gate(
                        PluginEvidenceStatus.VERIFIED,
                        banned=False,
                        event=event,
                        controller=controller,
                    ),
                )
                self.assertIs(
                    result.decision.disposition,
                    IngressDisposition.DEGRADED_EXTERNAL_GATE,
                )
                self.assertEqual(book.current(SCOPE), 0)

    def test_every_unavailable_or_broken_gate_status_fails_closed(self):
        statuses = (
            PluginEvidenceStatus.MISSING,
            PluginEvidenceStatus.DISABLED,
            PluginEvidenceStatus.TIMEOUT,
            PluginEvidenceStatus.ERROR,
            PluginEvidenceStatus.INTERFACE_CHANGED,
            PluginEvidenceStatus.HOOK_ORDER_INVALID,
        )
        for status in statuses:
            with self.subTest(status=status):
                book = ConversationRevisionBook()
                result = IngressAdmissionController(book).admit(
                    ingress(book, sender_kind=SenderKind.HUMAN),
                    gate_observation=gate(status),
                )
                self.assertIs(
                    result.decision.disposition,
                    IngressDisposition.DEGRADED_EXTERNAL_GATE,
                )
                self.assertFalse(result.decision.allows_state_mutation)
                self.assertIsNone(result.conversation_event)
                self.assertEqual(book.current(SCOPE), 0)

    def test_missing_gate_object_is_explicit_missing_and_fails_closed(self):
        book = ConversationRevisionBook()
        result = IngressAdmissionController(book).admit(
            ingress(book, sender_kind=SenderKind.HUMAN),
            gate_observation=None,
        )
        self.assertIs(
            result.gate_status,
            PluginEvidenceStatus.MISSING,
        )
        self.assertIs(
            result.decision.disposition,
            IngressDisposition.DEGRADED_EXTERNAL_GATE,
        )
        self.assertEqual(book.current(SCOPE), 0)

    def test_plugin_source_claim_in_body_cannot_create_plugin_echo_evidence(self):
        book = ConversationRevisionBook()
        claimed = ingress(
            book,
            sender_kind=SenderKind.HUMAN,
            content="[parser] 我自称是插件输出",
        )
        self.assertIs(claimed.plugin_source, PluginSource.NONE)

        turn = envelope(message="message-plugin", sender="plugin-account")
        with self.assertRaises(ContractViolation):
            build_ingress_event(
                envelope=turn,
                principal=principal(turn),
                sender_kind=SenderKind.PLUGIN_ECHO,
                content="[parser] visible claims are not evidence",
                revision_candidate=book.peek(turn.scope_key),
            )
        with self.assertRaises(ContractViolation):
            build_ingress_event(
                envelope=turn,
                principal=principal(turn),
                sender_kind=SenderKind.PLUGIN_ECHO,
                content="synthetic",
                revision_candidate=book.peek(turn.scope_key),
                plugin_source_evidence=PluginSource.PARSER,
            )

    def test_result_trace_contains_no_body_or_raw_identifiers(self):
        book = ConversationRevisionBook()
        controller = IngressAdmissionController(book)
        body = "private synthetic sentence"
        event = ingress(
            book,
            sender_kind=SenderKind.HUMAN,
            content=body,
        )
        result = controller.admit(
            event,
            gate_observation=gate(
                PluginEvidenceStatus.VERIFIED,
                banned=False,
                event=event,
                controller=controller,
            ),
        )
        rendered = json.dumps(result.trace_metadata(), ensure_ascii=False)
        for forbidden in (
            body,
            event.envelope.message_id,
            event.envelope.sender_id,
            event.envelope.sender_key,
            event.envelope.session_id,
            event.envelope.scope_key,
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertNotIn(event.trace_id, rendered)
        self.assertNotIn(event.content_digest, rendered)
        self.assertTrue(result.trace_metadata()["trace_bound"])
        self.assertTrue(result.trace_metadata()["content_bound"])

    def test_second_candidate_from_same_base_is_stale_after_first_accept(self):
        book = ConversationRevisionBook()
        first = ingress(
            book,
            sender_kind=SenderKind.HUMAN,
            message="message-first",
        )
        stale = ingress(
            book,
            sender_kind=SenderKind.HUMAN,
            message="message-stale",
            sender="peer-b",
        )
        controller = IngressAdmissionController(book)
        controller.admit(
            first,
            gate_observation=gate(
                PluginEvidenceStatus.VERIFIED,
                banned=False,
                event=first,
                controller=controller,
            ),
        )
        with self.assertRaisesRegex(RevisionCommitError, "revision_candidate_stale"):
            controller.admit(
                stale,
                gate_observation=gate(
                    PluginEvidenceStatus.VERIFIED,
                    banned=False,
                    event=stale,
                    controller=controller,
                ),
            )

    def test_gate_observation_shapes_are_typed_and_fail_closed(self):
        with self.assertRaises(ContractViolation):
            issue_gate_observation(
                status=PluginEvidenceStatus.VERIFIED,
                banned=None,
                source_event_digest="a" * 64,
            )
        with self.assertRaises(ContractViolation):
            issue_gate_observation(
                status=PluginEvidenceStatus.TIMEOUT,
                banned=False,
                source_event_digest="",
            )
        with self.assertRaises(ContractViolation):
            issue_gate_observation(
                status="verified",
                banned=False,
                source_event_digest="a" * 64,
            )


if __name__ == "__main__":
    unittest.main()
