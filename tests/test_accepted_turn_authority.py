from __future__ import annotations

import copy
import dataclasses
import inspect
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from astrbot_plugin_shio.core import accepted_turn_authority as authority_module
from astrbot_plugin_shio.core.accepted_turn_authority import (
    ACCEPTED_TURN_CONSUMERS,
    AcceptedTurnAuthority,
    AcceptedTurnConsumer,
    AcceptedTurnContext,
    AcceptedTurnDispatch,
    AcceptedTurnDisposition,
    AcceptedTurnFinalization,
    AcceptedTurnTicket,
)
from astrbot_plugin_shio.core.contracts import (
    ContractViolation,
    PluginEvidenceStatus,
    SenderKind,
)
from astrbot_plugin_shio.core.conversation_event import (
    ConversationRevisionBook,
    PluginSource,
    build_ingress_event,
)
from astrbot_plugin_shio.core.identity import PrincipalContext, TurnEnvelope
from astrbot_plugin_shio.core.ingress_admission import (
    AdmissionResult,
    IngressAdmissionController,
)


class AdmissionFixture:
    def __init__(self) -> None:
        self.book = ConversationRevisionBook()
        self.controller = IngressAdmissionController(self.book, max_proofs=64)
        self._counter = 0

    def accepted(self, label: str) -> AdmissionResult:
        self._counter += 1
        scope = "platform:test|bot:shio|group:authority-group-secret"
        envelope = TurnEnvelope(
            session_id="authority-session-secret",
            message_id=f"authority-message-secret-{label}",
            scope_key=scope,
            sender_key=f"{scope}|user:authority-sender-secret",
            sender_id="authority-sender-secret",
            platform_id="test",
            bot_id="shio",
            chat_type="group",
            group_id="authority-group-secret",
            reply_to_message_id="",
            reply_to_sender_id="",
            timestamp=float(self._counter),
            timestamp_source="message",
            source_kind="inbound",
            degradation_reasons=(),
        )
        principal = PrincipalContext(
            sender_key=envelope.sender_key,
            sender_id=envelope.sender_id,
            is_owner=False,
            relationship_role="group_peer",
            verification_source="astrbot_event_sender_id:structured",
        )
        ingress = build_ingress_event(
            envelope=envelope,
            principal=principal,
            sender_kind=SenderKind.HUMAN,
            content=f"private-visible-body-{label}",
            revision_candidate=self.book.peek(scope),
            generation_epoch=self._counter,
        )
        gate = self.controller.issue_gate_observation(
            ingress,
            status=PluginEvidenceStatus.VERIFIED,
            banned=False,
        )
        return self.controller.admit(ingress, gate_observation=gate)


class AcceptedTurnAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = AdmissionFixture()
        self.authority = AcceptedTurnAuthority(
            self.fixture.controller,
            max_turns=4,
        )

    def test_dispatch_claims_one_proof_and_issues_the_fixed_complete_set(self):
        admission = self.fixture.accepted("fixed-set")
        proof = self.fixture.controller.inspect_admission_proof(admission)

        dispatched = self.authority.dispatch(admission)

        self.assertIsInstance(dispatched, AcceptedTurnDispatch)
        self.assertEqual(ACCEPTED_TURN_CONSUMERS, tuple(AcceptedTurnConsumer))
        self.assertEqual(
            tuple(ticket.consumer for ticket in dispatched.tickets),
            ACCEPTED_TURN_CONSUMERS,
        )
        self.assertEqual(len(dispatched.tickets), 4)
        self.assertIs(admission.admission_proof, proof)
        with self.assertRaisesRegex(ContractViolation, "admission_proof_replayed"):
            self.fixture.controller.inspect_admission_proof(admission)
        with self.assertRaises(ContractViolation):
            self.authority.dispatch(admission)

    def test_public_consumer_view_cannot_reconfigure_the_code_owned_set(self):
        admission = self.fixture.accepted("fixed-code-owned")
        original = authority_module.ACCEPTED_TURN_CONSUMERS
        try:
            authority_module.ACCEPTED_TURN_CONSUMERS = (
                AcceptedTurnConsumer.OWNER_ACTION,
            )
            dispatched = self.authority.dispatch(admission)
        finally:
            authority_module.ACCEPTED_TURN_CONSUMERS = original
        self.assertEqual(
            tuple(ticket.consumer for ticket in dispatched.tickets),
            (
                AcceptedTurnConsumer.OWNER_ACTION,
                AcceptedTurnConsumer.AFFECT_STATE,
                AcceptedTurnConsumer.RELATIONSHIP_STATE,
                AcceptedTurnConsumer.OPPORTUNITY_ATTENTION,
            ),
        )

    def test_each_consumer_has_an_independent_one_shot_claim(self):
        admission = self.fixture.accepted("independent")
        dispatched = self.authority.dispatch(admission)
        binding = admission.conversation_event.binding

        owner_ticket = self.authority.ticket_for(
            dispatched,
            AcceptedTurnConsumer.OWNER_ACTION,
        )
        affect_ticket = self.authority.ticket_for(
            dispatched,
            AcceptedTurnConsumer.AFFECT_STATE,
        )
        owner_context = self.authority.context_for(
            owner_ticket,
            consumer=AcceptedTurnConsumer.OWNER_ACTION,
            binding=binding,
        )
        affect_context = self.authority.context_for(
            affect_ticket,
            consumer=AcceptedTurnConsumer.AFFECT_STATE,
            binding=binding,
        )
        self.assertIs(
            self.authority.claim_ticket(
                owner_ticket,
                consumer=AcceptedTurnConsumer.OWNER_ACTION,
                binding=binding,
                context=owner_context,
            ),
            owner_ticket,
        )
        self.assertIs(
            self.authority.claim_ticket(
                affect_ticket,
                consumer=AcceptedTurnConsumer.AFFECT_STATE,
                binding=binding,
                context=affect_context,
            ),
            affect_ticket,
        )
        with self.assertRaisesRegex(ContractViolation, "ticket_replayed"):
            self.authority.claim_ticket(
                owner_ticket,
                consumer=AcceptedTurnConsumer.OWNER_ACTION,
                binding=binding,
                context=owner_context,
            )

    def test_context_is_the_exact_content_free_projection_and_copy_cannot_claim(self):
        admission = self.fixture.accepted("context-canonical")
        dispatched = self.authority.dispatch(admission)
        ticket = self.authority.ticket_for(
            dispatched,
            AcceptedTurnConsumer.AFFECT_STATE,
        )
        binding = admission.conversation_event.binding
        context = self.authority.context_for(
            ticket,
            consumer=AcceptedTurnConsumer.AFFECT_STATE,
            binding=binding,
        )

        self.assertIsInstance(context, AcceptedTurnContext)
        self.assertIs(context.binding, binding)
        self.assertIs(context.conversation_event, admission.conversation_event)
        self.assertIs(context.envelope, admission.conversation_event.envelope)
        self.assertIs(context.principal, admission.conversation_event.principal)
        self.assertEqual(context.content_digest, binding.current_content_digest)

        copied = copy.copy(context)
        with self.assertRaisesRegex(ContractViolation, "context_mismatch"):
            self.authority.claim_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.AFFECT_STATE,
                binding=binding,
                context=copied,
            )
        self.assertIs(
            self.authority.claim_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.AFFECT_STATE,
                binding=binding,
                context=context,
            ),
            ticket,
        )

    def test_context_nested_identity_mutation_is_rejected_before_claim(self):
        mutations = (
            (
                "principal",
                lambda admission, other: PrincipalContext(
                    sender_key="forged-owner-key",
                    sender_id="forged-owner-id",
                    is_owner=True,
                    relationship_role="owner",
                    verification_source="forged",
                ),
            ),
            (
                "envelope",
                lambda admission, other: other.conversation_event.envelope,
            ),
            (
                "sender_kind",
                lambda admission, other: SenderKind.KNOWN_BOT,
            ),
            (
                "plugin_source",
                lambda admission, other: PluginSource.MANAGEMENT,
            ),
        )
        for field_name, replacement in mutations:
            with self.subTest(field_name=field_name):
                fixture = AdmissionFixture()
                authority = AcceptedTurnAuthority(fixture.controller, max_turns=4)
                admission = fixture.accepted(f"context-mutation-{field_name}")
                other = fixture.accepted(f"context-mutation-other-{field_name}")
                dispatched = authority.dispatch(admission)
                ticket = dispatched.tickets[0]
                context = authority.context_for(
                    ticket,
                    consumer=ticket.consumer,
                    binding=admission.conversation_event.binding,
                )
                original = getattr(context, field_name)
                object.__setattr__(
                    context,
                    field_name,
                    replacement(admission, other),
                )
                with self.assertRaisesRegex(ContractViolation, "ticket_corrupt"):
                    authority.claim_ticket(
                        ticket,
                        consumer=ticket.consumer,
                        binding=admission.conversation_event.binding,
                        context=context,
                    )
                object.__setattr__(context, field_name, original)
                self.assertIs(
                    authority.claim_ticket(
                        ticket,
                        consumer=ticket.consumer,
                        binding=admission.conversation_event.binding,
                        context=context,
                    ),
                    ticket,
                )

    def test_context_nested_object_field_mutation_is_rejected(self):
        mutations = (
            ("principal", "is_owner", True),
            ("principal", "verification_source", "forged-verification"),
            ("envelope", "session_id", "cross-turn-session"),
            ("envelope", "degradation_reasons", ("forged-reason",)),
        )
        for object_name, field_name, replacement in mutations:
            with self.subTest(object_name=object_name, field_name=field_name):
                fixture = AdmissionFixture()
                authority = AcceptedTurnAuthority(fixture.controller, max_turns=4)
                admission = fixture.accepted(
                    f"nested-mutation-{object_name}-{field_name}"
                )
                dispatched = authority.dispatch(admission)
                ticket = dispatched.tickets[0]
                context = authority.context_for(
                    ticket,
                    consumer=ticket.consumer,
                    binding=admission.conversation_event.binding,
                )
                nested = getattr(context, object_name)
                original = getattr(nested, field_name)
                object.__setattr__(nested, field_name, replacement)
                with self.assertRaisesRegex(ContractViolation, "ticket_corrupt"):
                    authority.claim_ticket(
                        ticket,
                        consumer=ticket.consumer,
                        binding=admission.conversation_event.binding,
                        context=context,
                    )
                object.__setattr__(nested, field_name, original)
                self.assertIs(
                    authority.claim_ticket(
                        ticket,
                        consumer=ticket.consumer,
                        binding=admission.conversation_event.binding,
                        context=context,
                    ),
                    ticket,
                )

    def test_context_deleted_nested_field_is_typed_failure_and_recoverable(self):
        admission = self.fixture.accepted("nested-field-deletion")
        dispatched = self.authority.dispatch(admission)
        ticket = dispatched.tickets[0]
        context = self.authority.context_for(
            ticket,
            consumer=ticket.consumer,
            binding=admission.conversation_event.binding,
        )
        original = context.principal.verification_source
        object.__delattr__(context.principal, "verification_source")
        with self.assertRaisesRegex(ContractViolation, "ticket_corrupt"):
            self.authority.claim_ticket(
                ticket,
                consumer=ticket.consumer,
                binding=admission.conversation_event.binding,
                context=context,
            )
        object.__setattr__(context.principal, "verification_source", original)
        self.assertIs(
            self.authority.claim_ticket(
                ticket,
                consumer=ticket.consumer,
                binding=admission.conversation_event.binding,
                context=context,
            ),
            ticket,
        )

    def test_integrity_snapshots_reject_equal_but_wrong_exact_types(self):
        class EqualString(str):
            pass

        class TruthyOwner:
            def __bool__(self):
                return True

            def __eq__(self, other):
                return other is False

            def __ne__(self, other):
                return not self.__eq__(other)

        mutations = (
            ("binding", "conversation_revision", lambda old: True),
            ("binding", "scope_key", EqualString),
            ("envelope", "timestamp", lambda old: True),
            ("envelope", "session_id", EqualString),
            ("principal", "is_owner", lambda old: TruthyOwner()),
            ("principal", "sender_id", EqualString),
            ("context", "content_digest", EqualString),
            ("ticket", "conversation_revision", lambda old: True),
            ("ticket", "ticket_digest", EqualString),
        )
        for object_name, field_name, replacement in mutations:
            with self.subTest(object_name=object_name, field_name=field_name):
                fixture = AdmissionFixture()
                authority = AcceptedTurnAuthority(fixture.controller, max_turns=4)
                admission = fixture.accepted(
                    f"equal-wrong-type-{object_name}-{field_name}"
                )
                dispatched = authority.dispatch(admission)
                ticket = dispatched.tickets[0]
                context = authority.context_for(
                    ticket,
                    consumer=ticket.consumer,
                    binding=admission.conversation_event.binding,
                )
                target = {
                    "binding": context.binding,
                    "envelope": context.envelope,
                    "principal": context.principal,
                    "context": context,
                    "ticket": ticket,
                }[object_name]
                original = getattr(target, field_name)
                object.__setattr__(target, field_name, replacement(original))
                with self.assertRaises(ContractViolation):
                    authority.claim_ticket(
                        ticket,
                        consumer=ticket.consumer,
                        binding=admission.conversation_event.binding,
                        context=context,
                    )
                object.__setattr__(target, field_name, original)
                self.assertIs(
                    authority.claim_ticket(
                        ticket,
                        consumer=ticket.consumer,
                        binding=admission.conversation_event.binding,
                        context=context,
                    ),
                    ticket,
                )

        for object_name, field_name, replacement in (
            ("dispatch", "conversation_revision", lambda old: True),
            ("dispatch", "dispatch_digest", EqualString),
        ):
            with self.subTest(object_name=object_name, field_name=field_name):
                fixture = AdmissionFixture()
                authority = AcceptedTurnAuthority(fixture.controller, max_turns=4)
                admission = fixture.accepted(
                    f"equal-wrong-type-{object_name}-{field_name}"
                )
                dispatched = authority.dispatch(admission)
                original = getattr(dispatched, field_name)
                object.__setattr__(
                    dispatched,
                    field_name,
                    replacement(original),
                )
                with self.assertRaises(ContractViolation):
                    authority.finalize_unclaimed_turn(
                        dispatched,
                        binding=admission.conversation_event.binding,
                        disposition=AcceptedTurnDisposition.SKIPPED,
                    )
                object.__setattr__(dispatched, field_name, original)
                self.assertEqual(
                    len(
                        authority.finalize_unclaimed_turn(
                            dispatched,
                            binding=admission.conversation_event.binding,
                            disposition=AcceptedTurnDisposition.SKIPPED,
                        )
                    ),
                    4,
                )

        for field_name, replacement in (
            ("conversation_revision", lambda old: True),
            ("finalization_digest", EqualString),
        ):
            with self.subTest(object_name="finalization", field_name=field_name):
                fixture = AdmissionFixture()
                authority = AcceptedTurnAuthority(fixture.controller, max_turns=4)
                admission = fixture.accepted(
                    f"equal-wrong-type-finalization-{field_name}"
                )
                dispatched = authority.dispatch(admission)
                ticket = dispatched.tickets[0]
                context = authority.context_for(
                    ticket,
                    consumer=ticket.consumer,
                    binding=admission.conversation_event.binding,
                )
                finalization = authority.finalize_unclaimed_ticket(
                    ticket,
                    consumer=ticket.consumer,
                    binding=admission.conversation_event.binding,
                    context=context,
                    disposition=AcceptedTurnDisposition.SKIPPED,
                )
                original = getattr(finalization, field_name)
                object.__setattr__(
                    finalization,
                    field_name,
                    replacement(original),
                )
                with self.assertRaisesRegex(
                    ContractViolation,
                    "finalization_corrupt",
                ):
                    authority.inspect_finalization(finalization)
                object.__setattr__(finalization, field_name, original)
                self.assertIs(
                    authority.inspect_finalization(finalization),
                    finalization,
                )

    def test_deleted_integrity_fields_are_typed_failures_and_recoverable(self):
        mutation_cases = (
            ("binding", "scope_key", "claim"),
            ("envelope", "session_id", "claim"),
            ("principal", "sender_id", "claim"),
            ("context", "content_digest", "claim"),
            ("ticket", "ticket_digest", "claim"),
            ("dispatch", "dispatch_digest", "turn"),
            ("finalization", "finalization_digest", "inspect"),
        )
        for object_name, field_name, operation in mutation_cases:
            with self.subTest(object_name=object_name, field_name=field_name):
                fixture = AdmissionFixture()
                authority = AcceptedTurnAuthority(fixture.controller, max_turns=4)
                admission = fixture.accepted(
                    f"deleted-field-{object_name}-{field_name}"
                )
                dispatched = authority.dispatch(admission)
                ticket = dispatched.tickets[0]
                context = authority.context_for(
                    ticket,
                    consumer=ticket.consumer,
                    binding=admission.conversation_event.binding,
                )
                finalization = None
                if operation == "inspect":
                    finalization = authority.finalize_unclaimed_ticket(
                        ticket,
                        consumer=ticket.consumer,
                        binding=admission.conversation_event.binding,
                        context=context,
                        disposition=AcceptedTurnDisposition.SKIPPED,
                    )
                target = {
                    "binding": context.binding,
                    "envelope": context.envelope,
                    "principal": context.principal,
                    "context": context,
                    "ticket": ticket,
                    "dispatch": dispatched,
                    "finalization": finalization,
                }[object_name]
                original = getattr(target, field_name)
                object.__delattr__(target, field_name)
                with self.assertRaises(ContractViolation):
                    if operation == "claim":
                        authority.claim_ticket(
                            ticket,
                            consumer=ticket.consumer,
                            binding=admission.conversation_event.binding,
                            context=context,
                        )
                    elif operation == "turn":
                        authority.finalize_unclaimed_turn(
                            dispatched,
                            binding=admission.conversation_event.binding,
                            disposition=AcceptedTurnDisposition.SKIPPED,
                        )
                    else:
                        authority.inspect_finalization(finalization)
                object.__setattr__(target, field_name, original)
                if operation == "claim":
                    self.assertIs(
                        authority.claim_ticket(
                            ticket,
                            consumer=ticket.consumer,
                            binding=admission.conversation_event.binding,
                            context=context,
                        ),
                        ticket,
                    )
                elif operation == "turn":
                    self.assertEqual(
                        len(
                            authority.finalize_unclaimed_turn(
                                dispatched,
                                binding=admission.conversation_event.binding,
                                disposition=AcceptedTurnDisposition.SKIPPED,
                            )
                        ),
                        4,
                    )
                else:
                    self.assertIs(
                        authority.inspect_finalization(finalization),
                        finalization,
                    )

    def test_public_constructor_shells_cannot_authenticate(self):
        admission = self.fixture.accepted("public-constructor-shells")
        dispatched = self.authority.dispatch(admission)
        ticket = dispatched.tickets[0]
        context = self.authority.context_for(
            ticket,
            consumer=ticket.consumer,
            binding=admission.conversation_event.binding,
        )
        finalization = self.authority.finalize_unclaimed_ticket(
            ticket,
            consumer=ticket.consumer,
            binding=admission.conversation_event.binding,
            context=context,
            disposition=AcceptedTurnDisposition.SKIPPED,
        )

        def shell_from(canonical):
            shell = type(canonical)()
            for dataclass_field in dataclasses.fields(type(canonical)):
                object.__setattr__(
                    shell,
                    dataclass_field.name,
                    getattr(canonical, dataclass_field.name),
                )
            return shell

        with self.assertRaisesRegex(ContractViolation, "dispatch_not_canonical"):
            self.authority.ticket_for(
                shell_from(dispatched),
                AcceptedTurnConsumer.OWNER_ACTION,
            )
        with self.assertRaisesRegex(ContractViolation, "ticket_not_canonical"):
            self.authority.context_for(
                shell_from(ticket),
                consumer=ticket.consumer,
                binding=admission.conversation_event.binding,
            )
        with self.assertRaisesRegex(ContractViolation, "context_mismatch"):
            second_ticket = dispatched.tickets[1]
            self.authority.claim_ticket(
                second_ticket,
                consumer=second_ticket.consumer,
                binding=admission.conversation_event.binding,
                context=shell_from(context),
            )
        with self.assertRaisesRegex(ContractViolation, "finalization_not_canonical"):
            self.authority.inspect_finalization(shell_from(finalization))

    def test_ticket_copy_fails_closed_without_consuming_the_canonical_ticket(self):
        admission = self.fixture.accepted("ticket-copy")
        dispatched = self.authority.dispatch(admission)
        canonical = self.authority.ticket_for(
            dispatched,
            AcceptedTurnConsumer.OWNER_ACTION,
        )
        copied = copy.copy(canonical)
        context = self.authority.context_for(
            canonical,
            consumer=AcceptedTurnConsumer.OWNER_ACTION,
            binding=admission.conversation_event.binding,
        )

        with self.assertRaisesRegex(ContractViolation, "ticket_not_canonical"):
            self.authority.claim_ticket(
                copied,
                consumer=AcceptedTurnConsumer.OWNER_ACTION,
                binding=admission.conversation_event.binding,
                context=context,
            )
        self.assertIs(
            self.authority.claim_ticket(
                canonical,
                consumer=AcceptedTurnConsumer.OWNER_ACTION,
                binding=admission.conversation_event.binding,
                context=context,
            ),
            canonical,
        )

    def test_dispatch_copy_is_not_a_canonical_registry_key(self):
        admission = self.fixture.accepted("dispatch-copy")
        dispatched = self.authority.dispatch(admission)
        copied = copy.copy(dispatched)

        with self.assertRaisesRegex(ContractViolation, "dispatch_not_canonical"):
            self.authority.ticket_for(
                copied,
                AcceptedTurnConsumer.AFFECT_STATE,
            )
        self.assertIsInstance(
            self.authority.ticket_for(
                dispatched,
                AcceptedTurnConsumer.AFFECT_STATE,
            ),
            AcceptedTurnTicket,
        )

    def test_cross_dispatcher_ticket_fails_and_original_remains_claimable(self):
        admission = self.fixture.accepted("cross-dispatcher")
        dispatched = self.authority.dispatch(admission)
        ticket = self.authority.ticket_for(
            dispatched,
            AcceptedTurnConsumer.AFFECT_STATE,
        )
        other = AcceptedTurnAuthority(self.fixture.controller, max_turns=4)
        context = self.authority.context_for(
            ticket,
            consumer=AcceptedTurnConsumer.AFFECT_STATE,
            binding=admission.conversation_event.binding,
        )

        with self.assertRaisesRegex(ContractViolation, "ticket_not_canonical"):
            other.claim_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.AFFECT_STATE,
                binding=admission.conversation_event.binding,
                context=context,
            )
        self.assertIs(
            self.authority.claim_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.AFFECT_STATE,
                binding=admission.conversation_event.binding,
                context=context,
            ),
            ticket,
        )

    def test_cross_turn_binding_fails_without_consuming_ticket(self):
        first = self.fixture.accepted("binding-a")
        second = self.fixture.accepted("binding-b")
        dispatched = self.authority.dispatch(first)
        ticket = self.authority.ticket_for(
            dispatched,
            AcceptedTurnConsumer.OWNER_ACTION,
        )
        context = self.authority.context_for(
            ticket,
            consumer=AcceptedTurnConsumer.OWNER_ACTION,
            binding=first.conversation_event.binding,
        )

        with self.assertRaisesRegex(ContractViolation, "ticket_binding_mismatch"):
            self.authority.claim_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.OWNER_ACTION,
                binding=second.conversation_event.binding,
                context=context,
            )
        copied_binding = dataclasses.replace(first.conversation_event.binding)
        with self.assertRaisesRegex(ContractViolation, "ticket_binding_mismatch"):
            self.authority.claim_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.OWNER_ACTION,
                binding=copied_binding,
                context=context,
            )
        self.assertIs(
            self.authority.claim_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.OWNER_ACTION,
                binding=first.conversation_event.binding,
                context=context,
            ),
            ticket,
        )
        self.assertIsNotNone(self.fixture.controller.inspect_admission_proof(second))

    def test_wrong_consumer_and_string_consumer_fail_before_claim(self):
        admission = self.fixture.accepted("wrong-consumer")
        dispatched = self.authority.dispatch(admission)
        ticket = self.authority.ticket_for(
            dispatched,
            AcceptedTurnConsumer.OWNER_ACTION,
        )
        binding = admission.conversation_event.binding
        context = self.authority.context_for(
            ticket,
            consumer=AcceptedTurnConsumer.OWNER_ACTION,
            binding=binding,
        )

        with self.assertRaisesRegex(ContractViolation, "ticket_consumer_mismatch"):
            self.authority.claim_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.AFFECT_STATE,
                binding=binding,
                context=context,
            )
        with self.assertRaisesRegex(ContractViolation, "consumer_invalid"):
            self.authority.claim_ticket(
                ticket,
                consumer="owner_action",  # type: ignore[arg-type]
                binding=binding,
                context=context,
            )
        self.assertIs(
            self.authority.claim_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.OWNER_ACTION,
                binding=binding,
                context=context,
            ),
            ticket,
        )

    def test_noncanonical_admission_copy_does_not_consume_original_proof(self):
        admission = self.fixture.accepted("admission-copy")
        copied = dataclasses.replace(admission)

        with self.assertRaisesRegex(ContractViolation, "not_canonical"):
            self.authority.dispatch(copied)
        self.assertIs(
            self.fixture.controller.inspect_admission_proof(admission),
            admission.admission_proof,
        )
        self.assertEqual(self.authority.trace_metadata()["turn_count"], 0)

    def test_claim_failure_cannot_publish_half_a_ticket_set(self):
        admission = self.fixture.accepted("claim-failure")
        with mock.patch.object(
            IngressAdmissionController,
            "claim_admission_proof",
            side_effect=ContractViolation("synthetic_claim_failure"),
        ):
            with self.assertRaisesRegex(ContractViolation, "synthetic_claim_failure"):
                self.authority.dispatch(admission)

        self.assertEqual(self.authority.trace_metadata()["turn_count"], 0)
        self.assertEqual(self.authority.trace_metadata()["ticket_count"], 0)
        self.assertIs(
            self.fixture.controller.inspect_admission_proof(admission),
            admission.admission_proof,
        )

    def test_full_active_ledger_fails_before_consuming_next_proof(self):
        authority = AcceptedTurnAuthority(self.fixture.controller, max_turns=1)
        first = self.fixture.accepted("full-a")
        second = self.fixture.accepted("full-b")
        authority.dispatch(first)

        with self.assertRaisesRegex(ContractViolation, "ledger_full"):
            authority.dispatch(second)
        self.assertIs(
            self.fixture.controller.inspect_admission_proof(second),
            second.admission_proof,
        )
        self.assertEqual(authority.trace_metadata()["turn_count"], 1)
        self.assertEqual(authority.trace_metadata()["ticket_count"], 4)

    def test_partially_claimed_turn_is_not_evictable(self):
        authority = AcceptedTurnAuthority(self.fixture.controller, max_turns=1)
        first = self.fixture.accepted("partial-a")
        second = self.fixture.accepted("partial-b")
        dispatched = authority.dispatch(first)
        owner_ticket = authority.ticket_for(
            dispatched,
            AcceptedTurnConsumer.OWNER_ACTION,
        )
        owner_context = authority.context_for(
            owner_ticket,
            consumer=AcceptedTurnConsumer.OWNER_ACTION,
            binding=first.conversation_event.binding,
        )
        authority.claim_ticket(
            owner_ticket,
            consumer=AcceptedTurnConsumer.OWNER_ACTION,
            binding=first.conversation_event.binding,
            context=owner_context,
        )

        with self.assertRaisesRegex(ContractViolation, "ledger_full"):
            authority.dispatch(second)
        self.assertIs(
            self.fixture.controller.inspect_admission_proof(second),
            second.admission_proof,
        )

    def test_partially_finalized_turn_is_not_evictable(self):
        authority = AcceptedTurnAuthority(self.fixture.controller, max_turns=1)
        first = self.fixture.accepted("partial-finalized-a")
        second = self.fixture.accepted("partial-finalized-b")
        dispatched = authority.dispatch(first)
        owner_ticket = dispatched.tickets[0]
        owner_context = authority.context_for(
            owner_ticket,
            consumer=owner_ticket.consumer,
            binding=first.conversation_event.binding,
        )
        authority.finalize_unclaimed_ticket(
            owner_ticket,
            consumer=owner_ticket.consumer,
            binding=first.conversation_event.binding,
            context=owner_context,
            disposition=AcceptedTurnDisposition.SKIPPED,
        )

        with self.assertRaisesRegex(ContractViolation, "ledger_full"):
            authority.dispatch(second)
        self.assertIs(
            self.fixture.controller.inspect_admission_proof(second),
            second.admission_proof,
        )
        metadata = authority.trace_metadata()
        self.assertEqual(metadata["active_turn_count"], 1)
        self.assertEqual(metadata["finalized_ticket_count"], 1)

    def test_completed_turn_is_evicted_as_one_unit_and_ledger_stays_bounded(self):
        authority = AcceptedTurnAuthority(self.fixture.controller, max_turns=1)
        first = self.fixture.accepted("evict-a")
        first_dispatch = authority.dispatch(first)
        first_tickets = first_dispatch.tickets
        for ticket in first_tickets:
            context = authority.context_for(
                ticket,
                consumer=ticket.consumer,
                binding=first.conversation_event.binding,
            )
            authority.claim_ticket(
                ticket,
                consumer=ticket.consumer,
                binding=first.conversation_event.binding,
                context=context,
            )

        second = self.fixture.accepted("evict-b")
        second_dispatch = authority.dispatch(second)
        self.assertEqual(authority.trace_metadata()["turn_count"], 1)
        self.assertEqual(authority.trace_metadata()["ticket_count"], 4)
        self.assertEqual(authority.trace_metadata()["claimed_ticket_count"], 0)
        with self.assertRaisesRegex(ContractViolation, "ticket_not_canonical"):
            authority.claim_ticket(
                first_tickets[0],
                consumer=first_tickets[0].consumer,
                binding=first.conversation_event.binding,
                context=context,
            )
        self.assertIsInstance(second_dispatch, AcceptedTurnDispatch)

    def test_unclaimed_ticket_can_be_finalized_once_with_typed_disposition(self):
        admission = self.fixture.accepted("finalize-ticket")
        dispatched = self.authority.dispatch(admission)
        ticket = self.authority.ticket_for(
            dispatched,
            AcceptedTurnConsumer.OWNER_ACTION,
        )
        context = self.authority.context_for(
            ticket,
            consumer=AcceptedTurnConsumer.OWNER_ACTION,
            binding=admission.conversation_event.binding,
        )

        finalization = self.authority.finalize_unclaimed_ticket(
            ticket,
            consumer=AcceptedTurnConsumer.OWNER_ACTION,
            binding=admission.conversation_event.binding,
            context=context,
            disposition=AcceptedTurnDisposition.SKIPPED,
        )

        self.assertIsInstance(finalization, AcceptedTurnFinalization)
        self.assertIs(finalization.consumer, AcceptedTurnConsumer.OWNER_ACTION)
        self.assertIs(finalization.disposition, AcceptedTurnDisposition.SKIPPED)
        with self.assertRaisesRegex(ContractViolation, "ticket_replayed"):
            self.authority.finalize_unclaimed_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.OWNER_ACTION,
                binding=admission.conversation_event.binding,
                context=context,
                disposition=AcceptedTurnDisposition.ABORTED,
            )
        with self.assertRaisesRegex(ContractViolation, "ticket_replayed"):
            self.authority.claim_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.OWNER_ACTION,
                binding=admission.conversation_event.binding,
                context=context,
            )

    def test_claim_and_finalize_are_mutually_exclusive_in_both_orders(self):
        admission = self.fixture.accepted("claim-finalize")
        dispatched = self.authority.dispatch(admission)
        owner_ticket, affect_ticket, _relationship_ticket, _attention_ticket = (
            dispatched.tickets
        )
        owner_context = self.authority.context_for(
            owner_ticket,
            consumer=owner_ticket.consumer,
            binding=admission.conversation_event.binding,
        )
        affect_context = self.authority.context_for(
            affect_ticket,
            consumer=affect_ticket.consumer,
            binding=admission.conversation_event.binding,
        )
        self.authority.claim_ticket(
            owner_ticket,
            consumer=owner_ticket.consumer,
            binding=admission.conversation_event.binding,
            context=owner_context,
        )
        with self.assertRaisesRegex(ContractViolation, "ticket_replayed"):
            self.authority.finalize_unclaimed_ticket(
                owner_ticket,
                consumer=owner_ticket.consumer,
                binding=admission.conversation_event.binding,
                context=owner_context,
                disposition=AcceptedTurnDisposition.REJECTED,
            )
        self.authority.finalize_unclaimed_ticket(
            affect_ticket,
            consumer=affect_ticket.consumer,
            binding=admission.conversation_event.binding,
            context=affect_context,
            disposition=AcceptedTurnDisposition.ABORTED,
        )
        with self.assertRaisesRegex(ContractViolation, "ticket_replayed"):
            self.authority.claim_ticket(
                affect_ticket,
                consumer=affect_ticket.consumer,
                binding=admission.conversation_event.binding,
                context=affect_context,
            )

    def test_whole_turn_finalization_only_terminates_unclaimed_tickets(self):
        admission = self.fixture.accepted("finalize-turn")
        dispatched = self.authority.dispatch(admission)
        owner_ticket, affect_ticket, _relationship_ticket, attention_ticket = (
            dispatched.tickets
        )
        owner_context = self.authority.context_for(
            owner_ticket,
            consumer=owner_ticket.consumer,
            binding=admission.conversation_event.binding,
        )
        self.authority.claim_ticket(
            owner_ticket,
            consumer=owner_ticket.consumer,
            binding=admission.conversation_event.binding,
            context=owner_context,
        )

        finalizations = self.authority.finalize_unclaimed_turn(
            dispatched,
            binding=admission.conversation_event.binding,
            disposition=AcceptedTurnDisposition.SKIPPED,
        )

        self.assertEqual(len(finalizations), 3)
        self.assertEqual(
            tuple(value.consumer for value in finalizations),
            (
                AcceptedTurnConsumer.AFFECT_STATE,
                AcceptedTurnConsumer.RELATIONSHIP_STATE,
                AcceptedTurnConsumer.OPPORTUNITY_ATTENTION,
            ),
        )
        self.assertTrue(
            all(
                value.disposition is AcceptedTurnDisposition.SKIPPED
                for value in finalizations
            )
        )
        with self.assertRaisesRegex(ContractViolation, "turn_already_terminal"):
            self.authority.finalize_unclaimed_turn(
                dispatched,
                binding=admission.conversation_event.binding,
                disposition=AcceptedTurnDisposition.ABORTED,
            )
        with self.assertRaisesRegex(ContractViolation, "ticket_replayed"):
            self.authority.claim_ticket(
                affect_ticket,
                consumer=affect_ticket.consumer,
                binding=admission.conversation_event.binding,
                context=self.authority.context_for(
                    affect_ticket,
                    consumer=affect_ticket.consumer,
                    binding=admission.conversation_event.binding,
                ),
            )
        with self.assertRaisesRegex(ContractViolation, "ticket_replayed"):
            self.authority.claim_ticket(
                attention_ticket,
                consumer=attention_ticket.consumer,
                binding=admission.conversation_event.binding,
                context=self.authority.context_for(
                    attention_ticket,
                    consumer=attention_ticket.consumer,
                    binding=admission.conversation_event.binding,
                ),
            )

    def test_whole_turn_finalization_rolls_back_on_publish_stage_failure(self):
        admission = self.fixture.accepted("finalize-turn-publish-failure")
        dispatched = self.authority.dispatch(admission)
        original_publish = AcceptedTurnAuthority._publish_finalization
        publish_count = 0

        def fail_second_publish(authority, record):
            nonlocal publish_count
            publish_count += 1
            published = original_publish(authority, record)
            if publish_count == 2:
                raise RuntimeError("injected_second_publish_failure")
            return published

        with mock.patch.object(
            AcceptedTurnAuthority,
            "_publish_finalization",
            new=fail_second_publish,
        ):
            with self.assertRaisesRegex(RuntimeError, "second_publish_failure"):
                self.authority.finalize_unclaimed_turn(
                    dispatched,
                    binding=admission.conversation_event.binding,
                    disposition=AcceptedTurnDisposition.ABORTED,
                )

        metadata = self.authority.trace_metadata()
        self.assertEqual(metadata["finalized_ticket_count"], 0)
        self.assertEqual(metadata["terminal_ticket_count"], 0)
        self.assertEqual(metadata["active_turn_count"], 1)
        finalizations = self.authority.finalize_unclaimed_turn(
            dispatched,
            binding=admission.conversation_event.binding,
            disposition=AcceptedTurnDisposition.SKIPPED,
        )
        self.assertEqual(len(finalizations), 4)

    def test_single_ticket_finalization_rolls_back_on_publish_stage_failure(self):
        admission = self.fixture.accepted("finalize-ticket-publish-failure")
        dispatched = self.authority.dispatch(admission)
        ticket = dispatched.tickets[0]
        context = self.authority.context_for(
            ticket,
            consumer=ticket.consumer,
            binding=admission.conversation_event.binding,
        )
        original_publish = AcceptedTurnAuthority._publish_finalization

        class InjectedPublishAbort(BaseException):
            pass

        def publish_then_abort(authority, record):
            original_publish(authority, record)
            raise InjectedPublishAbort("injected_single_publish_abort")

        with mock.patch.object(
            AcceptedTurnAuthority,
            "_publish_finalization",
            new=publish_then_abort,
        ):
            with self.assertRaisesRegex(
                InjectedPublishAbort,
                "single_publish_abort",
            ):
                self.authority.finalize_unclaimed_ticket(
                    ticket,
                    consumer=ticket.consumer,
                    binding=admission.conversation_event.binding,
                    context=context,
                    disposition=AcceptedTurnDisposition.ABORTED,
                )

        metadata = self.authority.trace_metadata()
        self.assertEqual(metadata["finalized_ticket_count"], 0)
        self.assertEqual(metadata["terminal_ticket_count"], 0)
        self.assertEqual(metadata["active_turn_count"], 1)
        finalization = self.authority.finalize_unclaimed_ticket(
            ticket,
            consumer=ticket.consumer,
            binding=admission.conversation_event.binding,
            context=context,
            disposition=AcceptedTurnDisposition.SKIPPED,
        )
        self.assertIs(
            self.authority.inspect_finalization(finalization),
            finalization,
        )

    def test_finalization_copy_cross_authority_and_binding_fail_closed(self):
        admission = self.fixture.accepted("finalize-exact")
        other_admission = self.fixture.accepted("finalize-exact-other")
        dispatched = self.authority.dispatch(admission)
        other_dispatched = self.authority.dispatch(other_admission)
        ticket = dispatched.tickets[0]
        context = self.authority.context_for(
            ticket,
            consumer=ticket.consumer,
            binding=admission.conversation_event.binding,
        )
        other_ticket = other_dispatched.tickets[0]
        other_context = self.authority.context_for(
            other_ticket,
            consumer=other_ticket.consumer,
            binding=other_admission.conversation_event.binding,
        )
        other = AcceptedTurnAuthority(self.fixture.controller, max_turns=4)

        for candidate_ticket, candidate_context, candidate_binding, authority in (
            (copy.copy(ticket), context, admission.conversation_event.binding, self.authority),
            (ticket, copy.copy(context), admission.conversation_event.binding, self.authority),
            (ticket, context, other_admission.conversation_event.binding, self.authority),
            (ticket, other_context, admission.conversation_event.binding, self.authority),
            (ticket, context, admission.conversation_event.binding, other),
        ):
            with self.assertRaises(ContractViolation):
                authority.finalize_unclaimed_ticket(
                    candidate_ticket,
                    consumer=AcceptedTurnConsumer.OWNER_ACTION,
                    binding=candidate_binding,
                    context=candidate_context,
                    disposition=AcceptedTurnDisposition.REJECTED,
                )
        finalization = self.authority.finalize_unclaimed_ticket(
            ticket,
            consumer=ticket.consumer,
            binding=admission.conversation_event.binding,
            context=context,
            disposition=AcceptedTurnDisposition.REJECTED,
        )
        copied_finalization = copy.copy(finalization)
        deepcopied_finalization = copy.deepcopy(finalization)
        self.assertIs(self.authority.inspect_finalization(finalization), finalization)
        with self.assertRaisesRegex(ContractViolation, "finalization_not_canonical"):
            self.authority.inspect_finalization(copied_finalization)
        with self.assertRaisesRegex(ContractViolation, "finalization_not_canonical"):
            self.authority.inspect_finalization(deepcopied_finalization)
        with self.assertRaisesRegex(ContractViolation, "finalization_not_canonical"):
            other.inspect_finalization(finalization)

    def test_finalization_disposition_and_receipt_fields_are_snapshotted(self):
        admission = self.fixture.accepted("finalization-snapshot")
        dispatched = self.authority.dispatch(admission)
        ticket = dispatched.tickets[0]
        context = self.authority.context_for(
            ticket,
            consumer=ticket.consumer,
            binding=admission.conversation_event.binding,
        )
        finalization = self.authority.finalize_unclaimed_ticket(
            ticket,
            consumer=ticket.consumer,
            binding=admission.conversation_event.binding,
            context=context,
            disposition=AcceptedTurnDisposition.SKIPPED,
        )
        mutations = (
            ("consumer", AcceptedTurnConsumer.AFFECT_STATE),
            ("disposition", AcceptedTurnDisposition.ABORTED),
            ("conversation_revision", finalization.conversation_revision + 1),
            ("finalization_digest", "0" * 64),
        )
        for field_name, replacement in mutations:
            with self.subTest(field_name=field_name):
                original = getattr(finalization, field_name)
                object.__setattr__(finalization, field_name, replacement)
                with self.assertRaisesRegex(ContractViolation, "finalization_corrupt"):
                    self.authority.inspect_finalization(finalization)
                object.__setattr__(finalization, field_name, original)
                self.assertIs(
                    self.authority.inspect_finalization(finalization),
                    finalization,
                )

    def test_claim_vs_finalize_is_atomic_under_concurrency(self):
        admission = self.fixture.accepted("claim-vs-finalize")
        dispatched = self.authority.dispatch(admission)
        ticket = dispatched.tickets[0]
        context = self.authority.context_for(
            ticket,
            consumer=ticket.consumer,
            binding=admission.conversation_event.binding,
        )
        barrier = threading.Barrier(8)

        def attempt(index: int):
            barrier.wait()
            try:
                if index % 2:
                    return self.authority.claim_ticket(
                        ticket,
                        consumer=ticket.consumer,
                        binding=admission.conversation_event.binding,
                        context=context,
                    )
                return self.authority.finalize_unclaimed_ticket(
                    ticket,
                    consumer=ticket.consumer,
                    binding=admission.conversation_event.binding,
                    context=context,
                    disposition=AcceptedTurnDisposition.ABORTED,
                )
            except ContractViolation as exc:
                return exc

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(pool.map(attempt, range(8)))
        self.assertEqual(
            sum(
                result is ticket or isinstance(result, AcceptedTurnFinalization)
                for result in results
            ),
            1,
        )
        self.assertEqual(
            sum(isinstance(result, ContractViolation) for result in results),
            7,
        )

    def test_finalize_vs_finalize_is_atomic_under_concurrency(self):
        admission = self.fixture.accepted("finalize-vs-finalize")
        dispatched = self.authority.dispatch(admission)
        ticket = dispatched.tickets[0]
        context = self.authority.context_for(
            ticket,
            consumer=ticket.consumer,
            binding=admission.conversation_event.binding,
        )
        barrier = threading.Barrier(8)

        def attempt():
            barrier.wait()
            try:
                return self.authority.finalize_unclaimed_ticket(
                    ticket,
                    consumer=ticket.consumer,
                    binding=admission.conversation_event.binding,
                    context=context,
                    disposition=AcceptedTurnDisposition.REJECTED,
                )
            except ContractViolation as exc:
                return exc

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(pool.map(lambda _: attempt(), range(8)))
        self.assertEqual(
            sum(type(result) is AcceptedTurnFinalization for result in results),
            1,
        )
        self.assertEqual(
            sum(isinstance(result, ContractViolation) for result in results),
            7,
        )

    def test_512_normal_and_exception_turns_do_not_fill_the_ledger(self):
        fixture = AdmissionFixture()
        authority = AcceptedTurnAuthority(fixture.controller, max_turns=4)
        for index in range(512):
            admission = fixture.accepted(f"bounded-{index}")
            dispatched = authority.dispatch(admission)
            if index % 3 == 0:
                owner_ticket = dispatched.tickets[0]
                owner_context = authority.context_for(
                    owner_ticket,
                    consumer=owner_ticket.consumer,
                    binding=admission.conversation_event.binding,
                )
                authority.claim_ticket(
                    owner_ticket,
                    consumer=owner_ticket.consumer,
                    binding=admission.conversation_event.binding,
                    context=owner_context,
                )
                disposition = AcceptedTurnDisposition.ABORTED
            elif index % 3 == 1:
                disposition = AcceptedTurnDisposition.SKIPPED
            else:
                disposition = AcceptedTurnDisposition.REJECTED
            authority.finalize_unclaimed_turn(
                dispatched,
                binding=admission.conversation_event.binding,
                disposition=disposition,
            )
        metadata = authority.trace_metadata()
        self.assertLessEqual(metadata["turn_count"], 4)
        self.assertEqual(metadata["active_turn_count"], 0)
        self.assertTrue(metadata["ledger_bounded"])

    def test_disposition_and_whole_turn_exact_type_boundaries(self):
        admission = self.fixture.accepted("finalize-types")
        dispatched = self.authority.dispatch(admission)
        with self.assertRaisesRegex(ContractViolation, "disposition_invalid"):
            self.authority.finalize_unclaimed_turn(
                dispatched,
                binding=admission.conversation_event.binding,
                disposition="skipped",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ContractViolation, "dispatch_not_canonical"):
            self.authority.finalize_unclaimed_turn(
                copy.copy(dispatched),
                binding=admission.conversation_event.binding,
                disposition=AcceptedTurnDisposition.SKIPPED,
            )
        self.assertEqual(self.authority.trace_metadata()["finalized_ticket_count"], 0)

    def test_trace_and_repr_never_render_raw_ids_or_visible_text(self):
        admission = self.fixture.accepted("repr-private-marker")
        dispatched = self.authority.dispatch(admission)
        context = self.authority.context_for(
            dispatched.tickets[0],
            consumer=dispatched.tickets[0].consumer,
            binding=admission.conversation_event.binding,
        )
        finalization = self.authority.finalize_unclaimed_ticket(
            dispatched.tickets[0],
            consumer=dispatched.tickets[0].consumer,
            binding=admission.conversation_event.binding,
            context=context,
            disposition=AcceptedTurnDisposition.REJECTED,
        )
        rendered = "\n".join(
            (
                repr(self.authority),
                repr(dispatched),
                repr(context),
                repr(finalization),
                *(repr(ticket) for ticket in dispatched.tickets),
                json.dumps(self.authority.trace_metadata(), ensure_ascii=False),
                json.dumps(dispatched.trace_metadata(), ensure_ascii=False),
                json.dumps(context.trace_metadata(), ensure_ascii=False),
                json.dumps(finalization.trace_metadata(), ensure_ascii=False),
                *(
                    json.dumps(ticket.trace_metadata(), ensure_ascii=False)
                    for ticket in dispatched.tickets
                ),
            )
        )
        for secret in (
            "authority-session-secret",
            "authority-message-secret",
            "authority-sender-secret",
            "authority-group-secret",
            "private-visible-body",
        ):
            self.assertNotIn(secret, rendered)

    def test_public_trace_and_repr_are_closed_for_corrupt_or_missing_fields(self):
        admission = self.fixture.accepted("trace-corrupt-fields")
        dispatched = self.authority.dispatch(admission)
        ticket = dispatched.tickets[0]
        context = self.authority.context_for(
            ticket,
            consumer=ticket.consumer,
            binding=admission.conversation_event.binding,
        )
        finalization = self.authority.finalize_unclaimed_ticket(
            ticket,
            consumer=ticket.consumer,
            binding=admission.conversation_event.binding,
            context=context,
            disposition=AcceptedTurnDisposition.REJECTED,
        )
        for value in (context, ticket, dispatched, finalization):
            for dataclass_field in dataclasses.fields(type(value)):
                with self.subTest(
                    value_type=type(value).__name__,
                    field_name=dataclass_field.name,
                ):
                    original = getattr(value, dataclass_field.name)
                    object.__delattr__(value, dataclass_field.name)
                    rendered = repr(value) + json.dumps(
                        value.trace_metadata(),
                        ensure_ascii=False,
                    )
                    self.assertNotIn("private-visible-body", rendered)
                    object.__setattr__(value, dataclass_field.name, original)

        private_marker = "trace-private-marker"
        mutations = (
            (context, "sender_kind", private_marker),
            (context, "plugin_source", private_marker),
            (ticket, "consumer", private_marker),
            (ticket, "conversation_revision", private_marker),
            (ticket, "ticket_digest", private_marker),
            (dispatched, "conversation_revision", private_marker),
            (dispatched, "dispatch_digest", private_marker),
            (finalization, "consumer", private_marker),
            (finalization, "disposition", private_marker),
            (finalization, "conversation_revision", private_marker),
            (finalization, "finalization_digest", private_marker),
        )
        for value, field_name, replacement in mutations:
            with self.subTest(
                value_type=type(value).__name__,
                field_name=field_name,
            ):
                original = getattr(value, field_name)
                object.__setattr__(value, field_name, replacement)
                rendered = repr(value) + json.dumps(
                    value.trace_metadata(),
                    ensure_ascii=False,
                )
                self.assertNotIn(private_marker, rendered)
                object.__setattr__(value, field_name, original)

    def test_context_trace_is_closed_after_nested_binding_mutation(self):
        admission = self.fixture.accepted("repr-mutated-binding")
        dispatched = self.authority.dispatch(admission)
        context = self.authority.context_for(
            dispatched.tickets[0],
            consumer=dispatched.tickets[0].consumer,
            binding=admission.conversation_event.binding,
        )
        object.__setattr__(
            context.binding,
            "conversation_revision",
            "trace-private-marker",
        )

        rendered = repr(context) + json.dumps(
            context.trace_metadata(),
            ensure_ascii=False,
        )

        self.assertNotIn("trace-private-marker", rendered)
        self.assertIn("conversation_revision=0", rendered)

    def test_same_dispatch_is_atomic_under_concurrency(self):
        admission = self.fixture.accepted("concurrent-dispatch")
        barrier = threading.Barrier(8)

        def attempt():
            barrier.wait()
            try:
                return self.authority.dispatch(admission)
            except ContractViolation as exc:
                return exc

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(pool.map(lambda _: attempt(), range(8)))
        successes = tuple(
            result for result in results if isinstance(result, AcceptedTurnDispatch)
        )
        failures = tuple(
            result for result in results if isinstance(result, ContractViolation)
        )
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 7)
        self.assertEqual(self.authority.trace_metadata()["turn_count"], 1)
        self.assertEqual(self.authority.trace_metadata()["ticket_count"], 4)

    def test_ticket_claim_is_one_shot_under_concurrency(self):
        admission = self.fixture.accepted("concurrent-claim")
        dispatched = self.authority.dispatch(admission)
        ticket = self.authority.ticket_for(
            dispatched,
            AcceptedTurnConsumer.AFFECT_STATE,
        )
        context = self.authority.context_for(
            ticket,
            consumer=AcceptedTurnConsumer.AFFECT_STATE,
            binding=admission.conversation_event.binding,
        )
        barrier = threading.Barrier(8)

        def attempt():
            barrier.wait()
            try:
                return self.authority.claim_ticket(
                    ticket,
                    consumer=AcceptedTurnConsumer.AFFECT_STATE,
                    binding=admission.conversation_event.binding,
                    context=context,
                )
            except ContractViolation as exc:
                return exc

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(pool.map(lambda _: attempt(), range(8)))
        self.assertEqual(sum(result is ticket for result in results), 1)
        self.assertEqual(
            sum(isinstance(result, ContractViolation) for result in results),
            7,
        )

    def test_configuration_and_type_boundaries_fail_closed(self):
        class IntSubclass(int):
            pass

        with self.assertRaisesRegex(ContractViolation, "ledger_limit_invalid"):
            AcceptedTurnAuthority(self.fixture.controller, max_turns=0)
        with self.assertRaisesRegex(ContractViolation, "ledger_limit_invalid"):
            AcceptedTurnAuthority(self.fixture.controller, max_turns=True)
        with self.assertRaisesRegex(ContractViolation, "ledger_limit_invalid"):
            AcceptedTurnAuthority(
                self.fixture.controller,
                max_turns=IntSubclass(4),
            )
        with self.assertRaisesRegex(ContractViolation, "controller_required"):
            AcceptedTurnAuthority(object())  # type: ignore[arg-type]

        admission = self.fixture.accepted("type-boundary")
        with self.assertRaisesRegex(ContractViolation, "admission_required"):
            self.authority.dispatch(object())  # type: ignore[arg-type]
        self.assertIsNotNone(self.fixture.controller.inspect_admission_proof(admission))

    def test_authority_layer_carries_principal_without_deciding_owner_policy(self):
        source = inspect.getsource(
            __import__(
                "astrbot_plugin_shio.core.accepted_turn_authority",
                fromlist=["AcceptedTurnAuthority"],
            )
        )
        for forbidden in ("resolve_principal(", "CapabilityPolicy", "owner_ids"):
            self.assertNotIn(forbidden, source)
        forbidden_parameters = {
            "is_owner",
            "relationship_role",
            "chat_type",
            "sender_id",
            "owner_id",
            "owner_ids",
            "principal",
            "policy",
            "capability_policy",
            "parameters",
        }
        for method_name in (
            "__init__",
            "dispatch",
            "ticket_for",
            "context_for",
            "claim_ticket",
            "finalize_unclaimed_ticket",
            "finalize_unclaimed_turn",
            "inspect_finalization",
        ):
            with self.subTest(method_name=method_name):
                parameters = set(
                    inspect.signature(
                        getattr(AcceptedTurnAuthority, method_name)
                    ).parameters
                )
                self.assertFalse(parameters & forbidden_parameters)


if __name__ == "__main__":
    unittest.main()
