from __future__ import annotations

import copy
import dataclasses
import hashlib
import inspect
import json
import math
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from astrbot_plugin_shio.core.accepted_turn_authority import (
    AcceptedTurnAuthority,
    AcceptedTurnConsumer,
    AcceptedTurnDispatch,
    AcceptedTurnTicket,
)
from astrbot_plugin_shio.core.affect_state import (
    AFFECT_DECAY_INTERVAL_S,
    MAX_AFFECT_INTENSITY,
    AffectAdmissionEvidence,
    AffectAppraisalKind,
    AffectCause,
    AffectMutationStatus,
    AffectRejectReason,
    AffectState,
    AffectStateBook,
    ModelAffectAppraisal,
    ShioReceiptEvidence,
    issue_affect_admission_evidence,
    issue_shio_receipt_evidence,
    parse_model_affect_appraisal,
)
from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.contracts import (
    ContractViolation,
    IngressDisposition,
    PluginEvidenceStatus,
    SenderKind,
)
from astrbot_plugin_shio.core.conversation_event import (
    ConversationEvent,
    ConversationRevisionBook,
    PluginSource,
    build_ingress_event,
    issue_plugin_source_evidence,
)
from astrbot_plugin_shio.core.identity import PrincipalContext, TurnEnvelope
from astrbot_plugin_shio.core.ingress_admission import (
    AdmissionResult,
    GateObservation,
    IngressAdmissionController,
)
from astrbot_plugin_shio.core.send_receipt import (
    InternalSendReceiptLedger,
    SentReplyRecord,
)


ROOT = Path(__file__).parents[1]
_AUTHORITIES: dict[int, tuple[IngressAdmissionController, AcceptedTurnAuthority]] = {}


def _scope(group: str) -> str:
    return f"platform:test|bot:shio|group:{group}"


def _sender(scope_key: str, sender: str) -> str:
    return f"{scope_key}|user:{sender}"


def _admission(
    controller: IngressAdmissionController,
    revisions: ConversationRevisionBook,
    *,
    group: str,
    sender: str,
    message: str,
    content: str,
    sender_kind: SenderKind = SenderKind.HUMAN,
    plugin_source: PluginSource = PluginSource.NONE,
    banned: bool = False,
) -> tuple[AdmissionResult, GateObservation]:
    scope_key = _scope(group)
    sender_key = _sender(scope_key, sender)
    envelope = TurnEnvelope(
        session_id=f"session-{group}",
        message_id=message,
        scope_key=scope_key,
        sender_key=sender_key,
        sender_id=sender,
        platform_id="test",
        bot_id="shio",
        chat_type="group",
        group_id=group,
        reply_to_message_id="",
        reply_to_sender_id="",
        timestamp=100.0 + revisions.current(scope_key),
        timestamp_source="fixture",
        source_kind="inbound",
        degradation_reasons=(),
    )
    principal = PrincipalContext(
        sender_key=sender_key,
        sender_id=sender,
        is_owner=False,
        relationship_role="group_peer",
        verification_source="astrbot_event_sender_id:owner_allowlist_miss",
    )
    evidence = None
    if plugin_source is not PluginSource.NONE:
        evidence = issue_plugin_source_evidence(
            source=plugin_source,
            source_event_digest=hashlib.sha256(
                f"{group}:{message}:plugin-source".encode("utf-8")
            ).hexdigest(),
        )
    ingress = build_ingress_event(
        envelope=envelope,
        principal=principal,
        sender_kind=sender_kind,
        content=content,
        revision_candidate=revisions.peek(scope_key),
        plugin_source_evidence=evidence,
    )
    observation = controller.issue_gate_observation(
        ingress,
        status=PluginEvidenceStatus.VERIFIED,
        banned=banned,
    )
    result = controller.admit(
        ingress,
        gate_observation=observation,
    )
    return result, observation


def _accepted_turn_authority(
    controller: IngressAdmissionController,
) -> AcceptedTurnAuthority:
    entry = _AUTHORITIES.get(id(controller))
    if entry is not None and entry[0] is controller:
        return entry[1]
    authority = AcceptedTurnAuthority(controller, max_turns=128)
    _AUTHORITIES[id(controller)] = (controller, authority)
    return authority


def _ticketed_turn(
    controller: IngressAdmissionController,
    revisions: ConversationRevisionBook,
    **kwargs,
) -> tuple[
    AdmissionResult,
    ConversationEvent,
    AcceptedTurnAuthority,
    AcceptedTurnDispatch,
    AcceptedTurnTicket,
]:
    result, _ = _admission(controller, revisions, **kwargs)
    authority = _accepted_turn_authority(controller)
    dispatch = authority.dispatch(result)
    ticket = authority.ticket_for(
        dispatch,
        AcceptedTurnConsumer.AFFECT_STATE,
    )
    event = result.conversation_event
    assert event is not None
    return result, event, authority, dispatch, ticket


def _committed_event(
    controller: IngressAdmissionController,
    revisions: ConversationRevisionBook,
    **kwargs,
) -> AffectAdmissionEvidence:
    _, event, authority, dispatch, ticket = _ticketed_turn(
        controller,
        revisions,
        **kwargs,
    )
    return issue_affect_admission_evidence(
        ticket,
        dispatch=dispatch,
        accepted_turn_authority=authority,
    )


def _appraisal(
    kind: AffectAppraisalKind,
    confidence: float = 0.95,
) -> ModelAffectAppraisal:
    return ModelAffectAppraisal(appraisal_hint=kind, confidence=confidence)


def _sent_reply(
    event: AffectAdmissionEvidence,
    *,
    text: str = "synthetic visible reply",
    succeeded: bool = True,
    reply_token: str = "",
    trace_id: str = "",
    target_sender_key: str = "",
) -> tuple[InternalSendReceiptLedger, str, SentReplyRecord]:
    ticks = iter((200.0, 201.0, 202.0, 203.0))
    ledger = InternalSendReceiptLedger(
        id_factory=lambda: (
            f"reply-{event.binding.conversation_revision}{reply_token}"
        ),
        now_fn=lambda: next(ticks),
    )
    target = ReplyTarget(
        message_id=event.envelope.message_id,
        sender_key=target_sender_key or event.envelope.sender_key,
        session_id=event.envelope.session_id,
        scope_key=event.envelope.scope_key,
        content_digest=event.content_digest,
        source_kind="current_inbound",
        referenced_message_id=event.envelope.reply_to_message_id,
        degradation_reasons=(),
    )
    pending = ledger.begin_reply(
        target=target,
        visible_segments=(text,),
        trace_id=trace_id or event.binding.trace_id,
    )
    segment_id = pending.segments[0].segment_id
    ledger.mark_attempted(segment_id)
    if succeeded:
        ledger.mark_succeeded(segment_id)
    else:
        ledger.mark_failed(segment_id, failure_kind="fixture_failure")
    receipt = ledger.sent_reply_record(pending.internal_reply_id)
    assert receipt is not None
    return ledger, pending.internal_reply_id, receipt


def _sent_reply_evidence(
    event: AffectAdmissionEvidence,
    *,
    text: str = "synthetic visible reply",
    reply_token: str = "",
    trace_id: str = "",
    target_sender_key: str = "",
) -> ShioReceiptEvidence:
    ledger, reply_id, _ = _sent_reply(
        event,
        text=text,
        reply_token=reply_token,
        trace_id=trace_id,
        target_sender_key=target_sender_key,
    )
    return issue_shio_receipt_evidence(
        ledger,
        internal_reply_id=reply_id,
    )


class AffectStateTests(unittest.TestCase):
    def test_affect_state_is_frozen_bounded_subject_object_bound_and_private(self):
        scope = _scope("state-contract")
        state = AffectState(
            scope_key=scope,
            subject_key=f"{scope}|agent:shio",
            object_key=_sender(scope, "peer-a"),
            valence=0.25,
            arousal=0.4,
            intensity=0.5,
            cause=AffectCause.POSITIVE_SOCIAL,
            inertia=0.7,
            updated_at=100.0,
            version=1,
            conversation_revision=1,
        )

        self.assertFalse(hasattr(state, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            state.intensity = 0.9
        rendered = repr(state) + json.dumps(state.trace_metadata(), ensure_ascii=False)
        for forbidden in (scope, state.subject_key, state.object_key):
            self.assertNotIn(forbidden, rendered)
        with self.assertRaises(ContractViolation):
            replace(state, intensity=MAX_AFFECT_INTENSITY + 0.01)
        with self.assertRaises(ContractViolation):
            replace(state, object_key=_sender(_scope("other"), "peer-a"))

    def test_accept_human_updates_only_current_sender_object_from_closed_appraisal(self):
        revisions = ConversationRevisionBook()
        controller = IngressAdmissionController(revisions)
        event = _committed_event(
            controller,
            revisions,
            group="accepted",
            sender="peer-a",
            message="message-a",
            content="synthetic social turn",
        )
        book = AffectStateBook(
            accepted_turn_authority=_accepted_turn_authority(controller)
        )

        with self.assertRaisesRegex(
            ContractViolation,
            "affect_ticket_required",
        ):
            issue_affect_admission_evidence(
                event.context,  # type: ignore[arg-type]
                dispatch=object(),  # type: ignore[arg-type]
                accepted_turn_authority=_accepted_turn_authority(controller),
            )
        foreign_controller = IngressAdmissionController(ConversationRevisionBook())
        foreign_book = AffectStateBook(
            accepted_turn_authority=_accepted_turn_authority(foreign_controller)
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "affect_ticket_authority_mismatch",
        ):
            foreign_book.record_human(
                event,
                appraisal=_appraisal(AffectAppraisalKind.POSITIVE_SOCIAL),
                now=100.0,
            )
        self.assertEqual(foreign_book.state_count, 0)

        result = book.record_human(
            event,
            appraisal=_appraisal(AffectAppraisalKind.POSITIVE_SOCIAL),
            now=100.0,
        )
        replay_book = AffectStateBook(
            accepted_turn_authority=_accepted_turn_authority(controller)
        )
        with self.assertRaisesRegex(ContractViolation, "ticket_replayed"):
            replay_book.record_human(
                event,
                appraisal=_appraisal(AffectAppraisalKind.POSITIVE_SOCIAL),
                now=100.0,
            )
        self.assertEqual(replay_book.state_count, 0)

        self.assertIs(result.status, AffectMutationStatus.ACCEPTED_HUMAN)
        self.assertTrue(result.state_changed)
        state = result.state
        assert state is not None
        self.assertEqual(state.scope_key, event.envelope.scope_key)
        self.assertEqual(state.object_key, event.envelope.sender_key)
        self.assertEqual(
            state.subject_key,
            f"{event.envelope.scope_key}|agent:{event.envelope.bot_id}",
        )
        self.assertGreater(state.valence, 0)
        self.assertGreater(state.intensity, 0)
        self.assertIs(state.cause, AffectCause.POSITIVE_SOCIAL)
        self.assertEqual(state.version, 1)
        self.assertEqual(state.conversation_revision, 1)
        rendered = repr(event) + json.dumps(event.trace_metadata(), ensure_ascii=False)
        self.assertFalse(hasattr(event, "admission"))
        self.assertFalse(hasattr(event, "proof"))
        for forbidden in (
            event.envelope.scope_key,
            event.envelope.sender_key,
            event.envelope.message_id,
            event.ticket.ticket_digest,
        ):
            self.assertNotIn(forbidden, rendered)

    def test_ticket_claim_failure_is_zero_write_and_then_retryable(self):
        revisions = ConversationRevisionBook()
        controller = IngressAdmissionController(revisions)
        event = _committed_event(
            controller,
            revisions,
            group="claim-atomic",
            sender="peer-a",
            message="claim-a",
            content="claim atomic content",
        )
        book = AffectStateBook(
            accepted_turn_authority=_accepted_turn_authority(controller)
        )

        with mock.patch.object(
            AcceptedTurnAuthority,
            "claim_ticket",
            side_effect=ContractViolation("synthetic_ticket_claim_failure"),
        ):
            with self.assertRaisesRegex(
                ContractViolation,
                "synthetic_ticket_claim_failure",
            ):
                book.record_human(
                    event,
                    appraisal=_appraisal(AffectAppraisalKind.CARE),
                    now=100.0,
                )
        self.assertEqual(book.state_count, 0)
        self.assertEqual(book.scope_revision(event.envelope.scope_key), 0)

        accepted = book.record_human(
            event,
            appraisal=_appraisal(AffectAppraisalKind.CARE),
            now=100.0,
        )
        self.assertIs(accepted.status, AffectMutationStatus.ACCEPTED_HUMAN)
        self.assertEqual(book.state_count, 1)

    def test_a_b_a_and_cross_group_keep_independent_object_states(self):
        revisions = ConversationRevisionBook()
        controller = IngressAdmissionController(revisions)
        book = AffectStateBook(
            accepted_turn_authority=_accepted_turn_authority(controller)
        )
        events = (
            _committed_event(
                controller,
                revisions,
                group="aba",
                sender="peer-a",
                message="a-1",
                content="a first",
            ),
            _committed_event(
                controller,
                revisions,
                group="aba",
                sender="peer-b",
                message="b-1",
                content="b only",
            ),
            _committed_event(
                controller,
                revisions,
                group="aba",
                sender="peer-a",
                message="a-2",
                content="a second",
            ),
            _committed_event(
                controller,
                revisions,
                group="other-group",
                sender="peer-a",
                message="other-a",
                content="other scope",
            ),
        )
        kinds = (
            AffectAppraisalKind.POSITIVE_SOCIAL,
            AffectAppraisalKind.NEGATIVE_SOCIAL,
            AffectAppraisalKind.REPAIR,
            AffectAppraisalKind.PLAYFUL,
        )
        for event, kind in zip(events, kinds, strict=True):
            accepted = book.record_human(
                event,
                appraisal=_appraisal(kind),
                now=100.0 + event.binding.conversation_revision,
            )
            self.assertIs(accepted.status, AffectMutationStatus.ACCEPTED_HUMAN)

        scope = _scope("aba")
        state_a = book.get_state(scope, _sender(scope, "peer-a"))
        state_b = book.get_state(scope, _sender(scope, "peer-b"))
        other_scope = _scope("other-group")
        other_a = book.get_state(other_scope, _sender(other_scope, "peer-a"))
        assert state_a is not None and state_b is not None and other_a is not None
        self.assertEqual(state_a.version, 2)
        self.assertEqual(state_a.conversation_revision, 3)
        self.assertEqual(state_b.version, 1)
        self.assertEqual(state_b.conversation_revision, 2)
        self.assertEqual(other_a.version, 1)
        self.assertNotEqual(state_a.object_key, other_a.object_key)
        self.assertEqual(book.scope_revision(scope), 3)
        self.assertEqual(book.scope_revision(other_scope), 1)

    def test_banned_bot_self_plugin_cross_binding_and_stale_are_zero_write(self):
        rejected_cases = (
            (
                SenderKind.HUMAN,
                PluginSource.NONE,
                IngressDisposition.DROP_BANNED,
            ),
            (
                SenderKind.KNOWN_BOT,
                PluginSource.NONE,
                IngressDisposition.DROP_KNOWN_BOT,
            ),
            (
                SenderKind.SELF,
                PluginSource.SHIO_REPLY,
                IngressDisposition.DROP_SELF,
            ),
            (
                SenderKind.PLUGIN_ECHO,
                PluginSource.PARSER,
                IngressDisposition.DROP_PLUGIN_ECHO,
            ),
        )
        for sender_kind, plugin_source, disposition in rejected_cases:
            revisions = ConversationRevisionBook()
            controller = IngressAdmissionController(revisions)
            admission, _ = _admission(
                controller,
                revisions,
                group=f"reject-{sender_kind.value}",
                sender="synthetic-sender",
                message="message-rejected",
                content="rejected content",
                sender_kind=sender_kind,
                plugin_source=plugin_source,
                banned=disposition is IngressDisposition.DROP_BANNED,
            )
            book = AffectStateBook(
                accepted_turn_authority=_accepted_turn_authority(controller)
            )
            with self.subTest(sender_kind=sender_kind):
                self.assertIs(admission.decision.disposition, disposition)
                with self.assertRaises(ContractViolation):
                    _accepted_turn_authority(controller).dispatch(admission)
                with self.assertRaisesRegex(
                    ContractViolation,
                    "affect_ticket_required",
                ):
                    issue_affect_admission_evidence(
                        admission,  # type: ignore[arg-type]
                        dispatch=object(),  # type: ignore[arg-type]
                        accepted_turn_authority=_accepted_turn_authority(controller),
                    )
                self.assertEqual(book.state_count, 0)
                self.assertEqual(
                    book.scope_revision(admission.ingress_event.envelope.scope_key),
                    0,
                )

        revisions = ConversationRevisionBook()
        controller = IngressAdmissionController(revisions)
        _, first_event, authority, first_dispatch, first_ticket = _ticketed_turn(
            controller,
            revisions,
            group="stale",
            sender="peer-a",
            message="first",
            content="first content",
        )
        first = issue_affect_admission_evidence(
            first_ticket,
            dispatch=first_dispatch,
            accepted_turn_authority=authority,
        )
        other_revisions = ConversationRevisionBook()
        other_controller = IngressAdmissionController(other_revisions)
        (
            _,
            other_event,
            other_authority,
            other_dispatch,
            other_ticket,
        ) = _ticketed_turn(
            other_controller,
            other_revisions,
            group="other-binding",
            sender="peer-b",
            message="other",
            content="other content",
        )
        book = AffectStateBook(
            accepted_turn_authority=_accepted_turn_authority(controller)
        )
        accepted = book.record_human(
            first,
            appraisal=_appraisal(AffectAppraisalKind.POSITIVE_SOCIAL),
            now=100.0,
        )
        first_baseline = accepted.state
        repeated_first = book.record_human(
            first,
            appraisal=_appraisal(AffectAppraisalKind.NEGATIVE_SOCIAL),
            now=101.0,
        )
        self.assertIs(repeated_first.reason, AffectRejectReason.REVISION_STALE)
        self.assertEqual(
            book.get_state(first.envelope.scope_key, first.envelope.sender_key),
            first_baseline,
        )

        candidate_result, candidate_event, _, candidate_dispatch, candidate_ticket = (
            _ticketed_turn(
                controller,
                revisions,
                group="stale",
                sender="peer-a",
                message="second",
                content="second content",
            )
        )
        self.assertNotIn(
            "event",
            inspect.signature(issue_affect_admission_evidence).parameters,
        )
        with self.assertRaises(TypeError):
            issue_affect_admission_evidence(
                candidate_ticket,
                copy.copy(candidate_event),
                dispatch=candidate_dispatch,
                accepted_turn_authority=authority,
            )
        for raw in (candidate_result, candidate_result.admission_proof):
            with self.subTest(raw_type=type(raw).__name__), self.assertRaisesRegex(
                ContractViolation,
                "affect_ticket_required",
            ):
                issue_affect_admission_evidence(
                    raw,  # type: ignore[arg-type]
                    dispatch=candidate_dispatch,
                    accepted_turn_authority=authority,
                )
        with self.assertRaisesRegex(ContractViolation, "ticket_not_canonical"):
            issue_affect_admission_evidence(
                copy.copy(candidate_ticket),
                dispatch=candidate_dispatch,
                accepted_turn_authority=authority,
            )
        with self.assertRaisesRegex(ContractViolation, "dispatch_not_canonical"):
            issue_affect_admission_evidence(
                candidate_ticket,
                dispatch=copy.copy(candidate_dispatch),
                accepted_turn_authority=authority,
            )
        owner_ticket = authority.ticket_for(
            candidate_dispatch,
            AcceptedTurnConsumer.OWNER_ACTION,
        )
        with self.assertRaisesRegex(ContractViolation, "consumer_required"):
            issue_affect_admission_evidence(
                owner_ticket,
                dispatch=candidate_dispatch,
                accepted_turn_authority=authority,
            )
        with self.assertRaises(ContractViolation):
            issue_affect_admission_evidence(
                other_ticket,
                dispatch=other_dispatch,
                accepted_turn_authority=authority,
            )

        foreign = issue_affect_admission_evidence(
            other_ticket,
            dispatch=other_dispatch,
            accepted_turn_authority=other_authority,
        )
        with self.assertRaisesRegex(ContractViolation, "authority_mismatch"):
            book.record_human(
                foreign,
                appraisal=_appraisal(AffectAppraisalKind.NEGATIVE_SOCIAL),
                now=101.0,
            )
        self.assertEqual(
            book.get_state(first.envelope.scope_key, first.envelope.sender_key),
            first_baseline,
        )

        candidate = issue_affect_admission_evidence(
            candidate_ticket,
            dispatch=candidate_dispatch,
            accepted_turn_authority=authority,
        )
        copied_context_evidence = copy.copy(candidate)
        object.__setattr__(
            copied_context_evidence,
            "context",
            copy.copy(candidate.context),
        )
        with self.assertRaisesRegex(ContractViolation, "context_mismatch"):
            book.record_human(
                copied_context_evidence,
                appraisal=_appraisal(AffectAppraisalKind.REPAIR),
                now=101.0,
            )
        self.assertEqual(
            book.get_state(first.envelope.scope_key, first.envelope.sender_key),
            first_baseline,
        )

        wrong_binding_evidence = copy.copy(candidate)
        object.__setattr__(wrong_binding_evidence, "context", foreign.context)
        wrong_binding = book.record_human(
            wrong_binding_evidence,
            appraisal=_appraisal(AffectAppraisalKind.REPAIR),
            now=101.0,
        )
        self.assertIs(wrong_binding.reason, AffectRejectReason.BINDING_MISMATCH)
        self.assertEqual(
            book.get_state(first.envelope.scope_key, first.envelope.sender_key),
            first_baseline,
        )

        second = book.record_human(
            candidate,
            appraisal=_appraisal(AffectAppraisalKind.REPAIR),
            now=101.0,
        )
        baseline = second.state
        repeated_candidate = book.record_human(
            candidate,
            appraisal=_appraisal(AffectAppraisalKind.NEGATIVE_SOCIAL),
            now=102.0,
        )
        self.assertIs(repeated_candidate.reason, AffectRejectReason.REVISION_STALE)
        forged = object.__new__(AffectAdmissionEvidence)
        object.__setattr__(forged, "context", candidate.context)
        object.__setattr__(forged, "ticket", candidate.ticket)
        object.__setattr__(forged, "_authority", authority)
        object.__setattr__(forged, "_seal", object())
        with self.assertRaises(ContractViolation):
            book.record_human(
                forged,
                appraisal=_appraisal(AffectAppraisalKind.NEGATIVE_SOCIAL),
                now=101.0,
            )
        self.assertEqual(
            book.get_state(candidate.envelope.scope_key, candidate.envelope.sender_key),
            baseline,
        )

    def test_model_appraisal_is_closed_soft_and_cannot_override_authority(self):
        fixture = {
            "appraisal_hint": AffectAppraisalKind.POSITIVE_SOCIAL.value,
            "confidence": 0.95,
        }
        for forbidden in (
            "owner",
            "is_owner",
            "scope_key",
            "subject_key",
            "object_key",
            "target",
            "binding",
            "sender_id",
            "message_id",
            "time",
            "updated_at",
            "version",
            "valence",
            "arousal",
            "intensity",
            "inertia",
        ):
            with self.subTest(forbidden=forbidden), self.assertRaises(
                ContractViolation
            ):
                parse_model_affect_appraisal({**fixture, forbidden: "forged"})
        for malformed_keys in (
            {
                **fixture,
                " appraisal_hint ": AffectAppraisalKind.NEGATIVE_SOCIAL.value,
            },
            {
                **fixture,
                1: AffectAppraisalKind.NEGATIVE_SOCIAL.value,
            },
        ):
            with self.subTest(malformed_keys=malformed_keys), self.assertRaises(
                ContractViolation
            ):
                parse_model_affect_appraisal(malformed_keys)
        with self.assertRaises(ContractViolation):
            parse_model_affect_appraisal(
                {"appraisal_hint": "unknown_appraisal", "confidence": 1.0}
            )

        revisions = ConversationRevisionBook()
        controller = IngressAdmissionController(revisions)
        event = _committed_event(
            controller,
            revisions,
            group="model-soft",
            sender="peer-a",
            message="model-turn",
            content="ordinary factual turn",
        )
        book = AffectStateBook(
            accepted_turn_authority=_accepted_turn_authority(controller)
        )
        with self.assertRaises(ContractViolation):
            book.record_human(
                event,
                appraisal={**fixture, "object_key": "forged-object"},
                now=100.0,
            )
        self.assertEqual(book.state_count, 0)
        self.assertEqual(book.scope_revision(event.envelope.scope_key), 0)

        low_confidence = book.record_human(
            event,
            appraisal={
                "appraisal_hint": AffectAppraisalKind.POSITIVE_SOCIAL.value,
                "confidence": 0.49,
            },
            now=100.0,
        )
        assert low_confidence.state is not None
        self.assertEqual(low_confidence.state.intensity, 0.0)
        self.assertEqual(low_confidence.state.valence, 0.0)
        self.assertIs(low_confidence.state.cause, AffectCause.BASELINE)

    def test_ordinary_factual_turn_is_zero_or_decay_only(self):
        revisions = ConversationRevisionBook()
        controller = IngressAdmissionController(revisions)
        book = AffectStateBook(
            accepted_turn_authority=_accepted_turn_authority(controller)
        )
        neutral = _committed_event(
            controller,
            revisions,
            group="neutral",
            sender="peer-a",
            message="neutral-1",
            content="plain factual statement",
        )
        first = book.record_human(
            neutral,
            appraisal=_appraisal(AffectAppraisalKind.NEUTRAL_FACT),
            now=100.0,
        )
        assert first.state is not None
        self.assertEqual(
            (first.state.valence, first.state.arousal, first.state.intensity),
            (0.0, 0.0, 0.0),
        )

        positive = _committed_event(
            controller,
            revisions,
            group="neutral",
            sender="peer-a",
            message="positive",
            content="positive social event",
        )
        emotional = book.record_human(
            positive,
            appraisal=_appraisal(AffectAppraisalKind.POSITIVE_SOCIAL),
            now=101.0,
        )
        ordinary = _committed_event(
            controller,
            revisions,
            group="neutral",
            sender="peer-a",
            message="neutral-2",
            content="another factual statement",
        )
        settled = book.record_human(
            ordinary,
            appraisal=_appraisal(AffectAppraisalKind.NEUTRAL_FACT),
            now=101.0 + AFFECT_DECAY_INTERVAL_S,
        )
        assert emotional.state is not None and settled.state is not None
        self.assertLess(settled.state.intensity, emotional.state.intensity)
        self.assertLessEqual(abs(settled.state.valence), abs(emotional.state.valence))

    def test_decay_and_repeated_appraisals_are_deterministic_bounded_and_not_permanent(self):
        def run_sequence(group: str) -> tuple[AffectState, AffectState]:
            revisions = ConversationRevisionBook()
            controller = IngressAdmissionController(revisions)
            book = AffectStateBook(
                accepted_turn_authority=_accepted_turn_authority(controller)
            )
            state = None
            for index in range(20):
                event = _committed_event(
                    controller,
                    revisions,
                    group=group,
                    sender="peer-a",
                    message=f"positive-{index}",
                    content=f"positive event {index}",
                )
                result = book.record_human(
                    event,
                    appraisal=_appraisal(AffectAppraisalKind.POSITIVE_SOCIAL),
                    now=100.0,
                )
                state = result.state
            assert state is not None
            peak = state
            neutral = _committed_event(
                controller,
                revisions,
                group=group,
                sender="peer-a",
                message="later-neutral",
                content="ordinary later fact",
            )
            decayed = book.record_human(
                neutral,
                appraisal=_appraisal(AffectAppraisalKind.NEUTRAL_FACT),
                now=100.0 + (8 * AFFECT_DECAY_INTERVAL_S),
            ).state
            assert decayed is not None
            return peak, decayed

        first_peak, first_decayed = run_sequence("bounded")
        second_peak, second_decayed = run_sequence("bounded")
        self.assertLessEqual(first_peak.intensity, MAX_AFFECT_INTENSITY)
        self.assertLessEqual(first_peak.arousal, 1.0)
        self.assertLessEqual(abs(first_peak.valence), 1.0)
        self.assertLess(first_decayed.intensity, first_peak.intensity * 0.2)
        self.assertEqual(first_peak, second_peak)
        self.assertEqual(first_decayed, second_decayed)

    def test_only_verified_successful_exact_shio_receipt_can_settle_state_once(self):
        revisions = ConversationRevisionBook()
        controller = IngressAdmissionController(revisions)
        event = _committed_event(
            controller,
            revisions,
            group="receipt",
            sender="peer-a",
            message="target-message",
            content="social event",
        )
        book = AffectStateBook(
            accepted_turn_authority=_accepted_turn_authority(controller)
        )
        human = book.record_human(
            event,
            appraisal=_appraisal(AffectAppraisalKind.PLAYFUL),
            now=100.0,
        )
        assert human.state is not None
        receipt = _sent_reply_evidence(event)

        accepted = book.record_shio_receipt(receipt, now=203.0)

        self.assertIs(
            accepted.status,
            AffectMutationStatus.ACCEPTED_SHIO_OUTBOUND,
        )
        assert accepted.state is not None
        self.assertEqual(accepted.state.version, human.state.version + 1)
        self.assertIs(accepted.state.cause, AffectCause.SHIO_OUTBOUND_SETTLE)
        self.assertLessEqual(accepted.state.intensity, human.state.intensity)
        receipt_rendered = repr(receipt) + json.dumps(
            receipt.trace_metadata(),
            ensure_ascii=False,
        )
        for forbidden in (
            event.envelope.scope_key,
            event.envelope.sender_key,
            event.envelope.message_id,
            "synthetic visible reply",
        ):
            self.assertNotIn(forbidden, receipt_rendered)
        baseline = accepted.state

        duplicate = book.record_shio_receipt(receipt, now=204.0)
        self.assertIs(duplicate.reason, AffectRejectReason.OUTBOUND_DUPLICATE)
        self.assertEqual(
            book.get_state(event.envelope.scope_key, event.envelope.sender_key),
            baseline,
        )

        failed_ledger, failed_id, _ = _sent_reply(
            event,
            succeeded=False,
            reply_token="-failed",
        )
        with self.assertRaises(ContractViolation):
            issue_shio_receipt_evidence(
                failed_ledger,
                internal_reply_id=failed_id,
            )
        _, _, malformed = _sent_reply(event, reply_token="-malformed")
        malformed_segment = replace(
            malformed.segments[0],
            visible_text_digest="0" * 64,
        )
        forged = object.__new__(ShioReceiptEvidence)
        object.__setattr__(
            forged,
            "receipt",
            replace(malformed, segments=(malformed_segment,)),
        )
        with self.assertRaises(ContractViolation):
            book.record_shio_receipt(forged, now=204.0)
        with self.assertRaisesRegex(
            ContractViolation,
            "affect_receipt_evidence_required",
        ):
            book.record_shio_receipt(copy.copy(receipt), now=204.0)

        wrong_trace = _sent_reply_evidence(
            event,
            reply_token="-wrong-trace",
            trace_id="f" * 32,
        )
        mismatched = book.record_shio_receipt(
            wrong_trace,
            now=204.0,
        )
        self.assertIs(mismatched.reason, AffectRejectReason.OUTBOUND_TARGET_MISMATCH)
        self.assertEqual(
            book.get_state(event.envelope.scope_key, event.envelope.sender_key),
            baseline,
        )

    def test_receipt_cannot_cross_user_scope_or_overtake_newer_target(self):
        revisions = ConversationRevisionBook()
        controller = IngressAdmissionController(revisions)
        book = AffectStateBook(
            accepted_turn_authority=_accepted_turn_authority(controller)
        )
        event_a1 = _committed_event(
            controller,
            revisions,
            group="receipt-isolation",
            sender="peer-a",
            message="a-1",
            content="a one",
        )
        event_b = _committed_event(
            controller,
            revisions,
            group="receipt-isolation",
            sender="peer-b",
            message="b-1",
            content="b one",
        )
        for event, kind in (
            (event_a1, AffectAppraisalKind.POSITIVE_SOCIAL),
            (event_b, AffectAppraisalKind.NEGATIVE_SOCIAL),
        ):
            book.record_human(
                event,
                appraisal=_appraisal(kind),
                now=100.0 + event.binding.conversation_revision,
            )
        scope = event_a1.envelope.scope_key
        before_b = book.get_state(scope, event_b.envelope.sender_key)
        receipt_a = _sent_reply_evidence(event_a1)
        accepted_a = book.record_shio_receipt(receipt_a, now=203.0)
        self.assertIs(
            accepted_a.status,
            AffectMutationStatus.ACCEPTED_SHIO_OUTBOUND,
        )
        self.assertEqual(book.get_state(scope, event_b.envelope.sender_key), before_b)

        event_a2 = _committed_event(
            controller,
            revisions,
            group="receipt-isolation",
            sender="peer-a",
            message="a-2",
            content="a newer",
        )
        book.record_human(
            event_a2,
            appraisal=_appraisal(AffectAppraisalKind.REPAIR),
            now=204.0,
        )
        current_a = book.get_state(scope, event_a2.envelope.sender_key)
        late = book.record_shio_receipt(
            _sent_reply_evidence(event_a1, reply_token="-late"),
            now=205.0,
        )
        self.assertIs(late.reason, AffectRejectReason.OUTBOUND_STALE)
        self.assertEqual(book.get_state(scope, event_a2.envelope.sender_key), current_a)

        wrong_user = _sent_reply_evidence(
            event_a2,
            target_sender_key=event_b.envelope.sender_key,
        )
        rejected = book.record_shio_receipt(wrong_user, now=205.0)
        self.assertIs(rejected.reason, AffectRejectReason.OUTBOUND_TARGET_MISMATCH)
        self.assertEqual(book.get_state(scope, event_b.envelope.sender_key), before_b)

    def test_stale_clock_is_zero_write_and_projection_does_not_commit(self):
        revisions = ConversationRevisionBook()
        controller = IngressAdmissionController(revisions)
        event = _committed_event(
            controller,
            revisions,
            group="clock",
            sender="peer-a",
            message="clock-1",
            content="clock event",
        )
        book = AffectStateBook(
            accepted_turn_authority=_accepted_turn_authority(controller)
        )
        accepted = book.record_human(
            event,
            appraisal=_appraisal(AffectAppraisalKind.CARE),
            now=500.0,
        )
        assert accepted.state is not None
        baseline = accepted.state
        projected = book.project_state(
            event.envelope.scope_key,
            event.envelope.sender_key,
            now=500.0 + AFFECT_DECAY_INTERVAL_S,
        )
        assert projected is not None
        self.assertLess(projected.intensity, baseline.intensity)
        self.assertEqual(projected.version, baseline.version)
        self.assertEqual(
            book.get_state(event.envelope.scope_key, event.envelope.sender_key),
            baseline,
        )

        next_event = _committed_event(
            controller,
            revisions,
            group="clock",
            sender="peer-a",
            message="clock-2",
            content="stale clock event",
        )
        stale = book.record_human(
            next_event,
            appraisal=_appraisal(AffectAppraisalKind.NEGATIVE_SOCIAL),
            now=499.0,
        )
        self.assertIs(stale.reason, AffectRejectReason.CLOCK_STALE)
        self.assertEqual(
            book.get_state(event.envelope.scope_key, event.envelope.sender_key),
            baseline,
        )
        self.assertEqual(book.scope_revision(event.envelope.scope_key), 1)

        retried = book.record_human(
            next_event,
            appraisal=_appraisal(AffectAppraisalKind.NEGATIVE_SOCIAL),
            now=501.0,
        )
        self.assertIs(retried.status, AffectMutationStatus.ACCEPTED_HUMAN)
        self.assertEqual(book.scope_revision(event.envelope.scope_key), 2)
        assert retried.state is not None
        self.assertEqual(retried.state.conversation_revision, 2)

    def test_source_is_generic_and_contains_no_character_specific_policy(self):
        source = (ROOT / "core" / "affect_state.py").read_text(encoding="utf-8")
        lowered = source.lower()
        for forbidden in (
            "atri",
            "亚托莉",
            "才没有",
            "高性能机器人",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertNotIn("from .persona", lowered)
        self.assertNotIn("import persona", lowered)
        self.assertNotIn("nickname", lowered)
        self.assertNotIn("owner_claim", lowered)
        self.assertNotIn("ingress_admission", lowered)
        self.assertNotIn("admissionresult", lowered)
        self.assertNotIn("admissionproof", lowered)
        self.assertNotIn("ingressadmissioncontroller", lowered)
        self.assertNotIn(
            "current_message",
            inspect.signature(issue_affect_admission_evidence).parameters,
        )
        self.assertTrue(math.isfinite(AFFECT_DECAY_INTERVAL_S))


if __name__ == "__main__":
    unittest.main()
