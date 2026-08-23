from __future__ import annotations

import copy
import dataclasses
import tempfile
import unittest

from astrbot_plugin_shio.core.contracts import (
    ActionEffectState,
    ActionReceiptStatus,
    ContractViolation,
)
from astrbot_plugin_shio.core.presentation_handoff import build_presentation_handoff
from astrbot_plugin_shio.core.owner_action_durable_finalize import (
    DurableFinalizeConsumer,
    OwnerActionDurableFinalizeAuthority,
)
from astrbot_plugin_shio.core.owner_action_lifecycle import (
    LifecyclePhase,
    LifecycleReserveDisposition,
    OwnerActionLifecycleStore,
)
from astrbot_plugin_shio.core.send_receipt import (
    InternalSendReceiptLedger,
    OwnerActionSendTerminalEvidence,
    _inspect_owner_action_send_terminal_evidence,
)
from astrbot_plugin_shio.tests import (
    test_action_outcome_delivery_pipeline as pipeline_support,
)


class OwnerActionDurableFinalizeTests(unittest.TestCase):
    """E4D red/green boundary for actual-send authority and durable cleanup."""

    def setUp(self) -> None:
        pipeline_support.ActionOutcomeDeliveryPipelineTests.setUpClass()
        self.pipeline = pipeline_support.ActionOutcomeDeliveryPipelineTests(
            methodName="test_contract_and_validation_context_require_exact_composer_authority"
        )
        self.pipeline.setUp()
        plan, origin_request, receipt = self.pipeline.case._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        outcome = self.pipeline.case.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=origin_request,
            receipt=receipt,
        )
        message = self.pipeline._message_for_plan(plan)
        request, contract, _context, result, report = self.pipeline._chain(
            plan,
            outcome,
            message,
            "试过了，但这次没成功，也没有产生副作用。",
        )
        self.plan = plan
        self.outcome = outcome
        self.origin_request = origin_request
        self.source = receipt
        self.request = request
        self.contract = contract
        self.handoff = build_presentation_handoff(
            composer_request=request,
            semantic_contract=contract,
            action_outcome=outcome,
            action_outcome_authority=self.pipeline.case.authority,
            planned_action=plan,
            expression_intent=request.expression_intent,
            affect_appraisal=request.affect_appraisal,
            result=result,
            validation=report,
            capability_policy=request.capability_policy,
        )
        ids = iter(f"e4d-{index}" for index in range(64))
        times = iter(float(index) for index in range(100, 500))
        self.ledger = InternalSendReceiptLedger(
            id_factory=lambda: next(ids),
            now_fn=lambda: next(times),
            max_replies=16,
        )
        self.tempdir = tempfile.TemporaryDirectory()
        self.lifecycle_store = OwnerActionLifecycleStore(
            self.tempdir.name,
            install_secret=b"e4d-test-install-secret-32-bytes!",
            recovery_now=10.0,
            max_records=64,
            tombstone_ttl_seconds=900.0,
        )

    def tearDown(self) -> None:
        self.lifecycle_store.close()
        self.tempdir.cleanup()

    def _successful_evidence(self):
        reply = self.ledger.begin_presentation_reply(self.handoff)
        for segment in reply.segments:
            self.ledger.mark_attempted(segment.segment_id)
            self.ledger.mark_succeeded(segment.segment_id)
        evidence = self.ledger.issue_owner_action_send_terminal_evidence(
            self.handoff,
            internal_reply_id=reply.internal_reply_id,
        )
        return reply, evidence

    def test_only_exact_successful_presentation_mints_canonical_evidence(self):
        reply, evidence = self._successful_evidence()

        self.assertIs(type(evidence), OwnerActionSendTerminalEvidence)
        inspection = _inspect_owner_action_send_terminal_evidence(
            evidence,
            ledger=self.ledger,
            presentation=self.handoff,
        )
        self.assertIs(inspection.presentation, self.handoff)
        self.assertIs(inspection.composer_request, self.request)
        self.assertIs(inspection.action_outcome, self.outcome)
        self.assertIs(
            inspection.action_outcome_authority,
            self.pipeline.case.authority,
        )
        self.assertEqual(inspection.segment_count, len(reply.segments))
        rendered = repr(evidence.trace_metadata()) + repr(evidence)
        self.assertNotIn(self.handoff.final_visible_text, rendered)
        self.assertNotIn(self.request.target_message_id, rendered)

    def test_public_sent_record_copy_cross_ledger_and_mutation_cannot_mint(self):
        reply = self.ledger.begin_presentation_reply(self.handoff)
        for segment in reply.segments:
            self.ledger.mark_attempted(segment.segment_id)
            self.ledger.mark_succeeded(segment.segment_id)
        public_record = self.ledger.sent_reply_record(reply.internal_reply_id)
        self.assertIsNotNone(public_record)

        with self.assertRaises(TypeError):
            self.ledger.issue_owner_action_send_terminal_evidence(
                public_record,
                internal_reply_id=reply.internal_reply_id,
            )

        evidence = self.ledger.issue_owner_action_send_terminal_evidence(
            self.handoff,
            internal_reply_id=reply.internal_reply_id,
        )
        for fake in (copy.copy(evidence), copy.deepcopy(evidence)):
            with self.assertRaises(ValueError):
                _inspect_owner_action_send_terminal_evidence(
                    fake,
                    ledger=self.ledger,
                    presentation=self.handoff,
                )
        other = InternalSendReceiptLedger()
        with self.assertRaises(ValueError):
            _inspect_owner_action_send_terminal_evidence(
                evidence,
                ledger=other,
                presentation=self.handoff,
            )
        object.__setattr__(evidence, "segment_count", 999)
        with self.assertRaises(ValueError):
            _inspect_owner_action_send_terminal_evidence(
                evidence,
                ledger=self.ledger,
                presentation=self.handoff,
            )

    def test_failed_missing_or_changed_segments_never_mint_and_remain_held(self):
        reply = self.ledger.begin_presentation_reply(self.handoff)
        first = reply.segments[0]
        self.ledger.mark_attempted(first.segment_id)
        self.ledger.mark_failed(first.segment_id, failure_kind="adapter_exception")
        with self.assertRaises(ValueError):
            self.ledger.issue_owner_action_send_terminal_evidence(
                self.handoff,
                internal_reply_id=reply.internal_reply_id,
            )

        self.tearDown()
        self.setUp()
        reply = self.ledger.begin_presentation_reply(self.handoff)
        for segment in reply.segments:
            self.ledger.mark_attempted(segment.segment_id)
            self.ledger.mark_succeeded(segment.segment_id)
        changed = copy.copy(self.handoff)
        object.__setattr__(
            changed,
            "final_segments",
            tuple(reversed(self.handoff.final_segments)),
        )
        with self.assertRaises(ValueError):
            self.ledger.issue_owner_action_send_terminal_evidence(
                changed,
                internal_reply_id=reply.internal_reply_id,
            )

        # A presentation-bound reply is never the fallback eviction victim.
        for index in range(20):
            target = self.plan.action.reply_target
            other = self.ledger.begin_reply(
                target=dataclasses.replace(target, message_id=f"other-{index}"),
                visible_segments=(f"ordinary-{index}",),
            )
            self.ledger.mark_attempted(other.segments[0].segment_id)
            self.ledger.mark_succeeded(other.segments[0].segment_id)
        self.assertIsNotNone(self.ledger.get_reply(reply.internal_reply_id))

    def test_exact_send_terminal_persists_ack_and_pins_until_both_tickets_finish(self):
        _reply, evidence = self._successful_evidence()
        reservation = self.lifecycle_store.reserve(
            self.origin_request.operation,
            request_digest=self.origin_request.idempotency_key,
            now=150.0,
        )
        self.assertIs(reservation.disposition, LifecycleReserveDisposition.RESERVED)
        self.assertTrue(
            self.lifecycle_store.mark_in_progress(
                reservation.handle,
                now=151.0,
            ).claimed
        )
        terminal = self.lifecycle_store.mark_terminal(
            reservation.handle,
            status=self.source.status,
            effect_state=self.source.effect_state,
            attempted=True,
            now=152.0,
        )
        self.assertIs(terminal.phase, LifecyclePhase.TERMINAL)
        self.pipeline.case.authority.acknowledge_delivery_terminal(
            self.outcome,
            self.plan,
            consumer=self.request,
        )
        authority = OwnerActionDurableFinalizeAuthority.issue_for_runtime(
            self.lifecycle_store,
            self.pipeline.case.harness.controller,
            self.pipeline.case.authority,
            max_dispatches=16,
        )

        dispatch = authority.acknowledge_delivered(
            lifecycle_handle=reservation.handle,
            source=self.source,
            outcome=self.outcome,
            current_planned_action=self.plan,
            composer_request=self.request,
            send_evidence=evidence,
            send_ledger=self.ledger,
            presentation=self.handoff,
            now=153.0,
        )

        self.assertIs(
            self.lifecycle_store.inspect_handle(reservation.handle).phase,
            LifecyclePhase.DELIVERY_ACK,
        )
        controller_ticket = authority.ticket_for(
            dispatch,
            DurableFinalizeConsumer.CONTROLLER_RELEASE,
        )
        outcome_ticket = authority.ticket_for(
            dispatch,
            DurableFinalizeConsumer.OUTCOME_RETIRE,
        )
        self.assertIs(
            controller_ticket.consumer,
            DurableFinalizeConsumer.CONTROLLER_RELEASE,
        )
        self.assertIs(
            outcome_ticket.consumer,
            DurableFinalizeConsumer.OUTCOME_RETIRE,
        )
        self.assertEqual(self.lifecycle_store.prune(now=2000.0), 0)

        with self.assertRaises(TypeError):
            self.pipeline.case.harness.controller.release_terminal_lineage(
                self.source,
                outcome=self.outcome,
                outcome_authority=self.pipeline.case.authority,
                reclaimed_at=154.0,
            )
        tombstone = self.pipeline.case.harness.controller.release_terminal_lineage(
            self.source,
            outcome=self.outcome,
            outcome_authority=self.pipeline.case.authority,
            ticket=controller_ticket,
            durable_authority=authority,
            reclaimed_at=154.0,
        )
        self.assertTrue(tombstone.retry_blocked)
        self.pipeline.case.authority.finalize_reclaimed_outcome(
            self.outcome,
            self.plan,
            ticket=outcome_ticket,
            durable_authority=authority,
        )
        self.assertEqual(
            self.pipeline.case.authority.trace_metadata()["reclaimed_count"],
            0,
        )
        self.assertEqual(self.lifecycle_store.prune(now=2000.0), 1)

    def test_prepared_abort_requires_durable_ticket_and_prunes_after_completion(self):
        _admission, plan, request = self.pipeline.case._issue_request()
        authority = OwnerActionDurableFinalizeAuthority.issue_for_runtime(
            self.lifecycle_store,
            self.pipeline.case.harness.controller,
            self.pipeline.case.authority,
            max_dispatches=16,
        )
        reservation = self.lifecycle_store.reserve(
            request.operation,
            request_digest=request.idempotency_key,
            now=210.0,
        )
        ticket = authority.prepare_abort(
            lifecycle_handle=reservation.handle,
            request=request,
            now=211.0,
        )
        self.assertIs(ticket.consumer, DurableFinalizeConsumer.PREPARED_ABORT)
        self.assertIs(
            self.lifecycle_store.inspect_handle(reservation.handle).phase,
            LifecyclePhase.TERMINAL,
        )
        with self.assertRaises(TypeError):
            self.pipeline.case.harness.controller.abort_prepared(
                request,
                aborted_at=212.0,
            )
        tombstone = self.pipeline.case.harness.controller.abort_prepared(
            request,
            ticket=ticket,
            durable_authority=authority,
            aborted_at=212.0,
        )
        self.assertIs(
            self.pipeline.case.harness.controller.inspect_lifecycle_tombstone(
                tombstone
            ),
            tombstone,
        )
        authority.complete_prepared_abort(ticket, now=213.0)
        with self.assertRaisesRegex(
            ContractViolation,
            "lifecycle_tombstone_not_canonical",
        ):
            self.pipeline.case.harness.controller.inspect_lifecycle_tombstone(
                tombstone
            )
        self.assertIsNone(
            self.pipeline.case.harness.controller.lookup_idempotent_request(
                request.idempotency_key
            )
        )
        self.assertEqual(self.lifecycle_store.prune(now=2000.0), 1)
        self.assertEqual(plan.action_id, request.action_id)

    def test_confirmation_delivery_retires_only_outcome_and_keeps_pending_request(self):
        from astrbot_plugin_shio.tests import test_action_outcome as outcome_support

        plan, request, pending, receipt = self.pipeline.case._pending_origin()
        outcome = self.pipeline.case.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        message = outcome_support.controller_support.second_turn_message(
            plan.binding.current_message_id
        )
        composer, contract, _context, result, report = self.pipeline._chain(
            plan,
            outcome,
            message,
            "要先等你确认，我还没动。",
        )
        handoff = build_presentation_handoff(
            composer_request=composer,
            semantic_contract=contract,
            action_outcome=outcome,
            action_outcome_authority=self.pipeline.case.authority,
            planned_action=plan,
            expression_intent=composer.expression_intent,
            affect_appraisal=composer.affect_appraisal,
            result=result,
            validation=report,
            capability_policy=composer.capability_policy,
        )
        ledger = InternalSendReceiptLedger()
        reply = ledger.begin_presentation_reply(handoff)
        for segment in reply.segments:
            ledger.mark_attempted(segment.segment_id)
            ledger.mark_succeeded(segment.segment_id)
        evidence = ledger.issue_owner_action_send_terminal_evidence(
            handoff,
            internal_reply_id=reply.internal_reply_id,
        )
        self.pipeline.case.authority.acknowledge_delivery_terminal(
            outcome,
            plan,
            consumer=composer,
        )
        authority = OwnerActionDurableFinalizeAuthority.issue_for_runtime(
            self.lifecycle_store,
            self.pipeline.case.harness.controller,
            self.pipeline.case.authority,
            max_dispatches=16,
        )
        ticket = authority.acknowledge_pending_delivered(
            source=receipt,
            outcome=outcome,
            current_planned_action=plan,
            composer_request=composer,
            send_evidence=evidence,
            send_ledger=ledger,
            presentation=handoff,
        )
        self.assertIs(
            ticket.consumer,
            DurableFinalizeConsumer.PENDING_OUTCOME_RETIRE,
        )
        self.pipeline.case.authority.finalize_reclaimed_outcome(
            outcome,
            plan,
            ticket=ticket,
            durable_authority=authority,
        )
        inspection = self.pipeline.case.harness.controller.inspect_request(request)
        self.assertEqual(inspection.state.value, "confirmation_required")
        self.assertIs(
            self.pipeline.case.harness.controller.canonical_receipt_for(request),
            receipt,
        )
        self.assertIs(pending.request, request)
        self.assertEqual(
            self.pipeline.case.authority.trace_metadata()["reclaimed_count"],
            0,
        )
        self.assertEqual(authority.trace_metadata()["active_dispatches"], 0)


if __name__ == "__main__":
    unittest.main()
