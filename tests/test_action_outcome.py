from __future__ import annotations

import ast
import copy
import dataclasses
import gc
import hashlib
import inspect
import json
import pickle
import subprocess
import sys
import threading
import unittest
import weakref
from concurrent.futures import ThreadPoolExecutor
from enum import Enum

from astrbot_plugin_shio.core.action_outcome import (
    ActionOutcomeAuthority,
    ActionOutcomeIntent,
    ActionOutcomeKind,
    ActionOutcomeOperation,
)
from astrbot_plugin_shio.core.contracts import (
    ActionEffectState,
    ActionKind,
    ActionOutput,
    ActionReceiptStatus,
    ContractViolation,
    GroundingFact,
    OwnerActionOperation,
)
from astrbot_plugin_shio.core.owner_action_controller import (
    ActionClaimStatus,
    PendingDecision,
    PendingResolutionStatus,
)
from astrbot_plugin_shio.tests import test_owner_action_controller as controller_support
from astrbot_plugin_shio.tests.harness.owner_action_durable import (
    DurableFinalizeHarness,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ActionOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reuse the already-audited Controller integration fixture rather than
        # forging Controller-owned requests or receipts in this consumer test.
        self.harness = controller_support.OwnerActionControllerTests(
            methodName=(
                "test_exact_private_configured_owner_admission_issues_canonical_request"
            )
        )
        self.harness.setUp()
        self.authority = ActionOutcomeAuthority.issue_for_runtime(
            self.harness.controller,
            self.harness.plan_authority,
            max_outcomes=64,
        )
        self.serial = 0

    def _next(self, label: str) -> str:
        self.serial += 1
        return f"{label}-{self.serial}"

    def _issue_request(
        self,
        *,
        second_turn: bool = False,
    ):
        label = self._next("outcome-request")
        if second_turn:
            admission = self.harness.fixture.admitted(
                message=label,
                content=controller_support.second_turn_message(label),
            )
            adapter = self.harness.second_turn_attestation
        else:
            admission = self.harness.fixture.admitted(message=label)
            adapter = self.harness.read_attestation
        decision = self.harness.route_decision(admission)
        proposal = decision.proposal
        assert proposal is not None
        plan = self.harness.planned_action(
            admission,
            capability=proposal.capability,
            operation=proposal.operation,
        )
        route = self.harness.controller.seal_action_route(
            self.harness.fixture.ticket_for(admission),
            plan,
            decision,
        )
        draft = self.harness.adapter_draft(
            admission,
            proposal,
            confirmation_policy=adapter.confirmation_policy,
        )
        request = self.harness.controller.issue_request(
            self.harness.fixture.ticket_for(admission),
            route,
            adapter_draft=draft,
            issued_at=100.0,
            deadline=220.0,
            idempotency_key=_digest(self._next("idempotency")),
            reason_codes=("owner_current_explicit_request",),
        )
        return admission, plan, request

    def _prepared_route_with_duplicate_plan(self):
        label = self._next("outcome-duplicate-plan")
        admission = self.harness.fixture.admitted(message=label)
        decision = self.harness.route_decision(admission)
        proposal = decision.proposal
        assert proposal is not None
        plan = self.harness.planned_action(
            admission,
            capability=proposal.capability,
            operation=proposal.operation,
        )
        duplicate = self.harness.planned_action(
            admission,
            capability=proposal.capability,
            operation=proposal.operation,
        )
        self.assertIsNot(plan, duplicate)
        self.assertEqual(plan.action_id, duplicate.action_id)
        self.assertIs(self.harness.plan_authority.inspect_plan(plan), plan)
        self.assertIs(self.harness.plan_authority.inspect_plan(duplicate), duplicate)
        route = self.harness.controller.seal_action_route(
            self.harness.fixture.ticket_for(admission),
            plan,
            decision,
        )
        return admission, proposal, plan, duplicate, route

    def _direct_receipt(
        self,
        *,
        status: ActionReceiptStatus,
        effect: ActionEffectState,
        output: ActionOutput | None = None,
    ):
        _, plan, request = self._issue_request()
        claim = self.harness.controller.claim_execution(
            request,
            current_binding=request.binding,
            now=120.0,
        )
        self.assertIs(claim.status, ActionClaimStatus.CLAIMED)
        assert claim.lease is not None
        receipt = self.harness.complete_fixture(
            request,
            claim.lease,
            status=status,
            effect_state=effect,
            result_digest=_digest(self._next("result")),
            completed_at=130.0,
            output=output,
            reason_codes=(
                () if status is ActionReceiptStatus.SUCCEEDED else ("bounded_failure",)
            ),
        )
        return plan, request, receipt

    def _pending_origin(self):
        _, plan, request = self._issue_request(second_turn=True)
        pending = self.harness.controller.start_confirmation(
            request,
            now=110.0,
            ttl=80.0,
        )
        receipt = self.harness.controller.canonical_receipt_for(request)
        assert receipt is not None
        return plan, request, pending, receipt

    def _continuation(self, decision: PendingDecision):
        origin_plan, request, pending, pending_receipt = self._pending_origin()
        phrase = {
            PendingDecision.CONFIRM: "确认保存",
            PendingDecision.DENY: "拒绝保存",
            PendingDecision.CANCEL: "取消保存",
        }[decision]
        followup = self.harness.fixture.admitted(
            message=self._next("outcome-continuation"),
            content=phrase,
        )
        route = self.harness.resolution_route(
            pending,
            followup,
            now=130.0,
            expected=PendingResolutionStatus(decision.value),
        )
        current_plan = self.harness._resolution_plans[id(route)]
        resolved = self.harness.resolve(route, now=130.0)
        lineage = self.harness.controller.continuation_lineage_for(route)
        return (
            origin_plan,
            request,
            pending_receipt,
            current_plan,
            resolved,
            lineage,
        )

    def _claimed_composer_outcome(self):
        """Build one exact C1 Composer claim through the audited public builder."""

        from astrbot_plugin_shio.core.reply_composer import (
            build_reply_composer_request,
        )
        from astrbot_plugin_shio.tests.test_action_outcome_delivery import (
            ActionOutcomeDeliveryTests,
        )

        ActionOutcomeDeliveryTests.setUpClass()
        delivery = ActionOutcomeDeliveryTests(
            methodName=(
                "test_builder_claims_only_after_complete_validation_and_inspection_is_non_consuming"
            )
        )
        delivery.setUp()
        plan, request, receipt = delivery.case._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        outcome = delivery.case.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        message = delivery._message_for_plan(plan)
        composer = build_reply_composer_request(
            **delivery._composer_inputs(plan, outcome, message)
        )
        return delivery, plan, request, receipt, outcome, composer

    def test_closed_outcome_enum_contains_required_semantics(self):
        required = {
            "confirmation_required",
            "denied",
            "cancelled",
            "stale_not_started",
            "succeeded_read",
            "succeeded_committed",
            "failed_no_effect",
            "failed_partial",
            "timed_out",
            "effect_unknown",
        }
        self.assertTrue(required.issubset({item.value for item in ActionOutcomeKind}))
        with self.assertRaises(TypeError):
            ActionOutcomeIntent(
                kind=ActionOutcomeKind.DENIED,
                operation=ActionOutcomeOperation.READ_ARTIFACT,
                attempted=False,
                has_output=False,
            )

    def test_operation_projection_is_closed_complete_and_safe(self):
        self.assertEqual(
            {item.value for item in ActionOutcomeOperation},
            {
                "read_artifact",
                "search_artifact",
                "save_memory",
                "run_sandbox_command",
            },
        )
        expected = {
            OwnerActionOperation.ARTIFACT_READ_EXACT:
                ActionOutcomeOperation.READ_ARTIFACT,
            OwnerActionOperation.ARTIFACT_GREP:
                ActionOutcomeOperation.SEARCH_ARTIFACT,
            OwnerActionOperation.MEMORY_WRITE_LITERAL:
                ActionOutcomeOperation.SAVE_MEMORY,
            OwnerActionOperation.SANDBOX_SHELL_ONCE:
                ActionOutcomeOperation.RUN_SANDBOX_COMMAND,
        }
        from astrbot_plugin_shio.core import action_outcome as outcome_module

        self.assertEqual(set(outcome_module._OWNER_OPERATION_PROJECTION), set(OwnerActionOperation))
        self.assertEqual(outcome_module._OWNER_OPERATION_PROJECTION, expected)

        plan, request, receipt = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        outcome = self.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        self.assertIs(outcome.operation, ActionOutcomeOperation.READ_ARTIFACT)
        public = repr(outcome) + json.dumps(outcome.trace_metadata(), sort_keys=True)
        self.assertIn("read_artifact", public)
        self.assertNotIn(OwnerActionOperation.ARTIFACT_READ_EXACT.value, public)

    def test_runtime_authority_constructor_and_duplicate_issuance_are_closed(self):
        with self.assertRaisesRegex(
            ContractViolation,
            "action_outcome_authority_constructor_forbidden",
        ):
            ActionOutcomeAuthority(
                self.harness.controller,
                self.harness.plan_authority,
            )
        with self.assertRaisesRegex(
            ContractViolation,
            "action_outcome_authority_already_issued",
        ):
            ActionOutcomeAuthority.issue_for_runtime(
                self.harness.controller,
                self.harness.plan_authority,
            )
        with self.assertRaisesRegex(
            ContractViolation,
            "action_outcome_plan_authority_mismatch",
        ):
            ActionOutcomeAuthority.issue_for_runtime(
                self.harness.controller,
                type(self.harness.plan_authority)(),
            )
        self.assertFalse(hasattr(self.harness.controller, "reset_action_outcome_authority"))
        self.assertFalse(hasattr(self.harness.controller, "unbind_action_outcome_authority"))

    def test_runtime_authority_copy_pickle_and_object_new_clone_are_rejected(self):
        with self.assertRaises(ContractViolation):
            copy.copy(self.authority)
        with self.assertRaises(ContractViolation):
            copy.deepcopy(self.authority)
        with self.assertRaises(ContractViolation):
            pickle.dumps(self.authority)

        clone = object.__new__(ActionOutcomeAuthority)
        for slot in ActionOutcomeAuthority.__slots__:
            if slot != "__weakref__":
                object.__setattr__(clone, slot, getattr(self.authority, slot))
        with self.assertRaisesRegex(
            ContractViolation,
            "action_outcome_authority_not_canonical",
        ):
            clone.trace_metadata()

    def test_runtime_authority_shape_and_plan_authority_mutation_fail_closed(self):
        original_limit = self.authority._max_outcomes
        object.__setattr__(self.authority, "_max_outcomes", original_limit + 1)
        with self.assertRaisesRegex(
            ContractViolation,
            "action_outcome_authority_not_canonical",
        ):
            self.authority.trace_metadata()
        object.__setattr__(self.authority, "_max_outcomes", original_limit)

        original_plan_authority = self.authority._planned_action_authority
        object.__setattr__(
            self.authority,
            "_planned_action_authority",
            type(original_plan_authority)(),
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "action_outcome_authority_not_canonical",
        ):
            self.authority.trace_metadata()
        object.__setattr__(
            self.authority,
            "_planned_action_authority",
            original_plan_authority,
        )
        self.assertFalse(self.authority.trace_metadata()["output_authority_connected"])

    def test_source_replay_survives_private_source_ledger_clear_attack(self):
        plan, request, receipt = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        self.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        self.assertFalse(hasattr(self.authority, "_source_records"))
        self.assertFalse(hasattr(self.authority, "_records"))
        with self.assertRaisesRegex(ContractViolation, "action_outcome_source_replayed"):
            self.authority.issue_from_receipt(
                current_planned_action=plan,
                origin_request=request,
                receipt=receipt,
            )

    def test_consumption_replay_survives_private_record_reset_attack(self):
        plan, request, receipt = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        outcome = self.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        self.authority.consume_outcome(outcome, current_planned_action=plan)
        from astrbot_plugin_shio.core import action_outcome as outcome_module

        for forbidden in (
            "_persistent_state_snapshot",
            "_lookup_persistent_record",
            "_append_persistent_record",
            "_consume_persistent_record",
        ):
            self.assertFalse(hasattr(outcome_module, forbidden), forbidden)
        with self.assertRaisesRegex(ContractViolation, "action_outcome_replayed"):
            self.authority.consume_outcome(outcome, current_planned_action=plan)

    def test_controller_registry_slot_clear_cannot_reissue_authority(self):
        controller = self.harness.controller
        mirrors = (
            controller._action_outcome_authority,
            controller._action_outcome_plan_authority,
            controller._action_outcome_authority_snapshot,
        )
        try:
            object.__setattr__(controller, "_action_outcome_authority", None)
            object.__setattr__(controller, "_action_outcome_plan_authority", None)
            object.__setattr__(controller, "_action_outcome_authority_snapshot", None)
            with self.assertRaisesRegex(
                ContractViolation,
                "action_outcome_authority_already_issued",
            ):
                ActionOutcomeAuthority.issue_for_runtime(
                    controller,
                    self.harness.plan_authority,
                )
        finally:
            object.__setattr__(controller, "_action_outcome_authority", mirrors[0])
            object.__setattr__(controller, "_action_outcome_plan_authority", mirrors[1])
            object.__setattr__(
                controller,
                "_action_outcome_authority_snapshot",
                mirrors[2],
            )

    def test_persistent_registration_and_state_are_immutable_tuples(self):
        from astrbot_plugin_shio.core import action_outcome as outcome_module

        registration = self.harness.controller._action_outcome_authority_snapshot
        self.assertIs(type(registration), tuple)
        with self.assertRaises((AttributeError, TypeError)):
            object.__setattr__(registration, "authority", self.authority)
        for forbidden in (
            "_RUNTIME_REGISTRATIONS",
            "_AUTHORITY_STATES",
            "_RUNTIME_REGISTRY_LOCK",
            "_build_runtime_registry",
            "_claim_runtime_authority",
            "_append_persistent_record",
            "_lookup_persistent_record",
            "_consume_persistent_record",
            "_persistent_state_snapshot",
            "_set_authority_state",
            "_replace_authority_state",
            "_clear_authority_state",
            "_unconsume_outcome",
        ):
            self.assertFalse(hasattr(outcome_module, forbidden), forbidden)
        lock_type = type(threading.RLock())
        self.assertFalse(
            any(type(value) is lock_type for value in vars(outcome_module).values())
        )
        self.assertFalse(
            any(
                type(value).__name__ == "WeakKeyDictionary"
                for value in vars(outcome_module).values()
            )
        )
        tree = ast.parse(inspect.getsource(outcome_module))
        module_assignment_calls = []
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if isinstance(value, ast.Call):
                    if isinstance(value.func, ast.Name):
                        module_assignment_calls.append(value.func.id)
                    elif isinstance(value.func, ast.Attribute):
                        module_assignment_calls.append(value.func.attr)
        self.assertNotIn("WeakKeyDictionary", module_assignment_calls)
        self.assertNotIn("RLock", module_assignment_calls)
        called_attributes = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        self.assertTrue({"clear", "pop", "setdefault"}.isdisjoint(called_attributes))
        function_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertFalse(
            any(
                token in name
                for name in function_names
                for token in (
                    "clear_state",
                    "reset_state",
                    "replace_state",
                    "set_state",
                    "unconsume",
                )
            )
        )
        issue_signature = inspect.signature(
            outcome_module._issue_persistent_outcome
        )
        self.assertTrue(
            {"record", "outcome", "max_outcomes"}.isdisjoint(
                issue_signature.parameters
            )
        )
        runtime_signature = inspect.signature(
            outcome_module._issue_runtime_authority
        )
        self.assertNotIn("authority", runtime_signature.parameters)

    def test_copied_outcome_and_forged_persistent_record_cannot_cross_index(self):
        from astrbot_plugin_shio.core import action_outcome as outcome_module

        plan, request, receipt = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        outcome = self.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        fake = copy.copy(outcome)
        self.assertFalse(hasattr(outcome_module, "_AUTHORITY_STATES"))
        for forbidden in (
            "_persistent_state_snapshot",
            "_lookup_persistent_record",
            "_append_persistent_record",
            "_consume_persistent_record",
        ):
            self.assertFalse(hasattr(outcome_module, forbidden), forbidden)
        with self.assertRaisesRegex(ContractViolation, "action_outcome_not_canonical"):
            self.authority.inspect_outcome(fake, current_planned_action=plan)
        self.authority.consume_outcome(outcome, current_planned_action=plan)
        with self.assertRaisesRegex(ContractViolation, "action_outcome_not_canonical"):
            self.authority.consume_outcome(fake, current_planned_action=plan)

    def test_raw_append_cannot_rebind_copied_outcome_to_second_exact_receipt(self):
        from astrbot_plugin_shio.core import action_outcome as outcome_module

        plan1, request1, receipt1 = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        outcome1 = self.authority.issue_from_receipt(
            current_planned_action=plan1,
            origin_request=request1,
            receipt=receipt1,
        )
        plan2, request2, receipt2 = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        fake = copy.copy(outcome1)
        for forbidden in (
            "_lookup_persistent_record",
            "_append_persistent_record",
        ):
            self.assertFalse(hasattr(outcome_module, forbidden), forbidden)
        with self.assertRaises(TypeError):
            outcome_module._issue_persistent_outcome(
                self.authority,
                source=outcome_module._OutcomeSource.RECEIPT,
                current_planned_action=plan2,
                origin_request=request2,
                receipt=receipt2,
                outcome=fake,
            )
        outcome2 = outcome_module._issue_persistent_outcome(
            self.authority,
            source=outcome_module._OutcomeSource.RECEIPT,
            current_planned_action=plan2,
            origin_request=request2,
            receipt=receipt2,
        )
        self.assertIsNot(outcome2, fake)
        with self.assertRaisesRegex(ContractViolation, "action_outcome_not_canonical"):
            self.authority.inspect_outcome(fake, current_planned_action=plan2)
        self.assertIs(
            self.authority.inspect_outcome(
                outcome2,
                current_planned_action=plan2,
            ),
            outcome2,
        )

    def test_raw_consume_cannot_bypass_current_plan_and_controller_reinspection(self):
        from astrbot_plugin_shio.core import action_outcome as outcome_module

        plan, request, receipt = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        outcome = self.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        wrong_plan, _wrong_request, _wrong_receipt = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        self.assertFalse(hasattr(outcome_module, "_consume_persistent_record"))
        self.assertFalse(hasattr(outcome_module, "_lookup_persistent_record"))
        with self.assertRaisesRegex(ContractViolation, "action_outcome_plan_mismatch"):
            outcome_module._consume_persistent_outcome(
                self.authority,
                outcome,
                current_planned_action=wrong_plan,
            )
        self.assertIs(
            self.authority.consume_outcome(
                outcome,
                current_planned_action=plan,
            ),
            outcome,
        )

    def test_raw_append_cannot_override_registered_outcome_limit(self):
        from astrbot_plugin_shio.core import action_outcome as outcome_module

        case = ActionOutcomeTests(
            methodName="test_direct_receipt_maps_to_sealed_minimal_intent_and_consumes_once"
        )
        case.harness = controller_support.OwnerActionControllerTests(
            methodName=(
                "test_exact_private_configured_owner_admission_issues_canonical_request"
            )
        )
        case.harness.setUp()
        case.authority = ActionOutcomeAuthority.issue_for_runtime(
            case.harness.controller,
            case.harness.plan_authority,
            max_outcomes=1,
        )
        case.serial = 0
        plan1, request1, receipt1 = case._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        outcome1 = case.authority.issue_from_receipt(
            current_planned_action=plan1,
            origin_request=request1,
            receipt=receipt1,
        )
        plan2, request2, receipt2 = case._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        self.assertFalse(hasattr(outcome_module, "_append_persistent_record"))
        with self.assertRaises(TypeError):
            outcome_module._issue_persistent_outcome(
                case.authority,
                source=outcome_module._OutcomeSource.RECEIPT,
                current_planned_action=plan2,
                origin_request=request2,
                receipt=receipt2,
                max_outcomes=2,
            )
        with self.assertRaisesRegex(ContractViolation, "action_outcome_ledger_full"):
            outcome_module._issue_persistent_outcome(
                case.authority,
                source=outcome_module._OutcomeSource.RECEIPT,
                current_planned_action=plan2,
                origin_request=request2,
                receipt=receipt2,
            )
        self.assertIs(
            case.authority.inspect_outcome(
                outcome1,
                current_planned_action=plan1,
            ),
            outcome1,
        )

    def test_private_claim_cannot_canonicalize_object_new_authority(self):
        from astrbot_plugin_shio.core import action_outcome as outcome_module

        harness = controller_support.OwnerActionControllerTests(
            methodName=(
                "test_exact_private_configured_owner_admission_issues_canonical_request"
            )
        )
        harness.setUp()
        fake = object.__new__(ActionOutcomeAuthority)
        object.__setattr__(
            fake,
            "_authority_seal",
            outcome_module._ACTION_OUTCOME_AUTHORITY_INSTANCE_SEAL,
        )
        object.__setattr__(fake, "_controller", harness.controller)
        object.__setattr__(
            fake,
            "_planned_action_authority",
            harness.plan_authority,
        )
        object.__setattr__(fake, "_max_outcomes", 64)
        with self.assertRaises((AttributeError, ContractViolation)):
            outcome_module._claim_runtime_authority(
                fake,
                controller=harness.controller,
                planned_action_authority=harness.plan_authority,
                max_outcomes=64,
            )
        canonical = outcome_module._issue_runtime_authority(
            harness.controller,
            harness.plan_authority,
            max_outcomes=64,
        )
        self.assertIs(type(canonical), ActionOutcomeAuthority)
        self.assertFalse(canonical.trace_metadata()["output_authority_connected"])
        with self.assertRaisesRegex(
            ContractViolation,
            "action_outcome_authority_already_issued",
        ):
            ActionOutcomeAuthority.issue_for_runtime(
                harness.controller,
                harness.plan_authority,
            )

    def test_full_slots_clone_cannot_take_over_controller_registry_slot(self):
        clone = object.__new__(ActionOutcomeAuthority)
        for slot in ActionOutcomeAuthority.__slots__:
            if slot != "__weakref__":
                object.__setattr__(clone, slot, getattr(self.authority, slot))
        controller = self.harness.controller
        original = controller._action_outcome_authority
        object.__setattr__(controller, "_action_outcome_authority", clone)
        try:
            with self.assertRaisesRegex(
                ContractViolation,
                "action_outcome_authority_not_canonical",
            ):
                clone.trace_metadata()
        finally:
            object.__setattr__(controller, "_action_outcome_authority", original)

    def test_runtime_authority_concurrent_issue_has_one_winner(self):
        harness = controller_support.OwnerActionControllerTests(
            methodName=(
                "test_exact_private_configured_owner_admission_issues_canonical_request"
            )
        )
        harness.setUp()

        def issue_once():
            try:
                return ActionOutcomeAuthority.issue_for_runtime(
                    harness.controller,
                    harness.plan_authority,
                )
            except ContractViolation as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(pool.map(lambda _: issue_once(), range(8)))
        winners = tuple(value for value in results if type(value) is ActionOutcomeAuthority)
        failures = tuple(value for value in results if type(value) is str)
        self.assertEqual(len(winners), 1)
        self.assertEqual(
            failures,
            ("action_outcome_authority_already_issued",) * 7,
        )

    def test_new_controller_graph_is_isolated_and_authority_cycle_is_collectable(self):
        from astrbot_plugin_shio.core import action_outcome as outcome_module

        gc.collect()
        registrations_before, states_before = outcome_module._runtime_registry_counts()

        def build_graph():
            harness = controller_support.OwnerActionControllerTests(
                methodName=(
                    "test_exact_private_configured_owner_admission_issues_canonical_request"
                )
            )
            harness.setUp()
            authority = ActionOutcomeAuthority.issue_for_runtime(
                harness.controller,
                harness.plan_authority,
            )
            return weakref.ref(harness.controller), weakref.ref(authority)

        controller_ref, authority_ref = build_graph()
        gc.collect()
        self.assertIsNone(controller_ref())
        self.assertIsNone(authority_ref())
        registrations_after, states_after = outcome_module._runtime_registry_counts()
        self.assertEqual(registrations_after, registrations_before)
        self.assertEqual(states_after, states_before)

    def test_module_reload_with_a_new_controller_graph_can_issue(self):
        code = r"""
import importlib
from astrbot_plugin_shio.core import action_outcome as ao
from astrbot_plugin_shio.tests import test_owner_action_controller as support

def graph(authority_type):
    harness = support.OwnerActionControllerTests(
        methodName='test_exact_private_configured_owner_admission_issues_canonical_request'
    )
    harness.setUp()
    authority = authority_type.issue_for_runtime(
        harness.controller,
        harness.plan_authority,
    )
    return harness, authority

old_harness, first = graph(ao.ActionOutcomeAuthority)
reloaded = importlib.reload(ao)
try:
    reloaded.ActionOutcomeAuthority.issue_for_runtime(
        old_harness.controller,
        old_harness.plan_authority,
    )
except Exception as exc:
    assert str(exc) == 'action_outcome_authority_already_issued'
else:
    raise AssertionError('old Controller accepted a second authority after reload')
_new_harness, second = graph(reloaded.ActionOutcomeAuthority)
assert type(first).__name__ == type(second).__name__
assert type(first) is not type(second)
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_direct_receipt_maps_to_sealed_minimal_intent_and_consumes_once(self):
        plan, request, receipt = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        outcome = self.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )

        self.assertIs(type(outcome), ActionOutcomeIntent)
        self.assertIs(outcome.kind, ActionOutcomeKind.FAILED_NO_EFFECT)
        self.assertIs(outcome.operation, ActionOutcomeOperation.READ_ARTIFACT)
        self.assertTrue(outcome.attempted)
        self.assertFalse(outcome.has_output)
        self.assertEqual(
            {field.name for field in dataclasses.fields(outcome)},
            {"kind", "operation", "attempted", "has_output"},
        )
        rendered = repr(outcome) + json.dumps(
            outcome.trace_metadata(),
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertEqual(
            set(outcome.trace_metadata()),
            {"outcome", "operation", "attempted", "has_output"},
        )
        self.assertFalse(hasattr(outcome, "__dict__"))
        for secret in (
            plan.action_id,
            request.request_digest,
            request.parameter_digest,
            request.binding.current_message_id,
            request.binding.current_sender_key,
            receipt.result_digest,
            "bounded_failure",
        ):
            self.assertNotIn(secret, rendered)

        self.assertIs(
            self.authority.inspect_outcome(
                outcome,
                current_planned_action=plan,
            ),
            outcome,
        )
        self.assertIs(
            self.authority.consume_outcome(
                outcome,
                current_planned_action=plan,
            ),
            outcome,
        )
        with self.assertRaisesRegex(ContractViolation, "action_outcome_replayed"):
            self.authority.consume_outcome(
                outcome,
                current_planned_action=plan,
            )

    def test_confirmation_required_and_stale_not_started_are_unattempted(self):
        plan, request, _, confirmation_receipt = self._pending_origin()
        confirmation = self.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=confirmation_receipt,
        )
        self.assertIs(
            confirmation.kind,
            ActionOutcomeKind.CONFIRMATION_REQUIRED,
        )
        self.assertFalse(confirmation.attempted)

        _, stale_plan, stale_request = self._issue_request()
        claim = self.harness.controller.claim_execution(
            stale_request,
            current_binding=stale_request.binding,
            now=120.0,
            stale=True,
        )
        assert claim.receipt is not None
        stale = self.authority.issue_from_receipt(
            current_planned_action=stale_plan,
            origin_request=stale_request,
            receipt=claim.receipt,
        )
        self.assertIs(stale.kind, ActionOutcomeKind.STALE_NOT_STARTED)
        self.assertFalse(stale.attempted)

    def test_controller_denial_is_bound_to_exact_execute_plan(self):
        label = self._next("outcome-denial")
        admission = self.harness.fixture.admitted(message=label)
        decision = self.harness.route_decision(admission)
        proposal = decision.proposal
        assert proposal is not None
        plan = self.harness.planned_action(
            admission,
            capability=proposal.capability,
            operation=proposal.operation,
        )
        route = self.harness.controller.seal_action_route(
            self.harness.fixture.ticket_for(admission),
            plan,
            decision,
        )
        denial = self.harness.controller.deny_action_route(
            self.harness.fixture.ticket_for(admission),
            route,
            denied_at=110.0,
            reason_codes=("adapter_not_enabled",),
        )
        outcome = self.authority.issue_from_denial(
            current_planned_action=plan,
            denial=denial,
        )
        self.assertIs(outcome.kind, ActionOutcomeKind.DENIED)
        self.assertFalse(outcome.attempted)
        self.assertFalse(outcome.has_output)

    def test_confirm_continuation_requires_current_execute_plan_and_origin_receipt(self):
        (
            _,
            request,
            _,
            current_plan,
            confirmed,
            lineage,
        ) = self._continuation(PendingDecision.CONFIRM)
        with self.assertRaisesRegex(
            ContractViolation,
            "continuation_receipt_required",
        ):
            self.authority.issue_from_continuation(
                current_planned_action=current_plan,
                lineage=lineage,
            )

        claim = self.harness.controller.claim_execution(
            request,
            pending_confirmation=confirmed,
            current_binding=confirmed.confirmation_binding,
            now=131.0,
        )
        assert claim.lease is not None
        self.harness.complete_fixture(
            request,
            claim.lease,
            status=ActionReceiptStatus.SUCCEEDED,
            effect_state=ActionEffectState.COMMITTED,
            result_digest=_digest(self._next("committed")),
            completed_at=140.0,
        )
        outcome = self.authority.issue_from_continuation(
            current_planned_action=current_plan,
            lineage=lineage,
        )
        self.assertIs(current_plan.kind, ActionKind.EXECUTE_ACTION)
        self.assertIs(outcome.kind, ActionOutcomeKind.SUCCEEDED_COMMITTED)
        self.assertTrue(outcome.attempted)

    def test_deny_and_cancel_continuations_require_reply_plan(self):
        for decision, expected in (
            (PendingDecision.DENY, ActionOutcomeKind.DENIED),
            (PendingDecision.CANCEL, ActionOutcomeKind.CANCELLED),
        ):
            with self.subTest(decision=decision):
                (
                    _,
                    _,
                    _,
                    current_plan,
                    _,
                    lineage,
                ) = self._continuation(decision)
                self.assertIs(current_plan.kind, ActionKind.REPLY)
                outcome = self.authority.issue_from_continuation(
                    current_planned_action=current_plan,
                    lineage=lineage,
                )
                self.assertIs(outcome.kind, expected)
                self.assertFalse(outcome.attempted)

    def test_partial_and_unknown_continuations_remain_uncertain(self):
        for status, effect, expected in (
            (
                ActionReceiptStatus.FAILED,
                ActionEffectState.PARTIAL,
                ActionOutcomeKind.FAILED_PARTIAL,
            ),
            (
                ActionReceiptStatus.EFFECT_UNKNOWN,
                ActionEffectState.UNKNOWN,
                ActionOutcomeKind.EFFECT_UNKNOWN,
            ),
            (
                ActionReceiptStatus.TIMED_OUT,
                ActionEffectState.UNKNOWN,
                ActionOutcomeKind.TIMED_OUT,
            ),
        ):
            with self.subTest(status=status, effect=effect):
                (
                    _,
                    request,
                    _,
                    current_plan,
                    confirmed,
                    lineage,
                ) = self._continuation(PendingDecision.CONFIRM)
                claim = self.harness.controller.claim_execution(
                    request,
                    pending_confirmation=confirmed,
                    current_binding=confirmed.confirmation_binding,
                    now=131.0,
                )
                assert claim.lease is not None
                self.harness.complete_fixture(
                    request,
                    claim.lease,
                    status=status,
                    effect_state=effect,
                    result_digest=_digest(self._next("uncertain-result")),
                    completed_at=140.0,
                    reason_codes=("effect_not_proven_absent",),
                )
                outcome = self.authority.issue_from_continuation(
                    current_planned_action=current_plan,
                    lineage=lineage,
                )
                self.assertIs(outcome.kind, expected)
                self.assertTrue(outcome.attempted)

    def test_attempted_stale_does_not_collapse_into_success_or_no_attempt(self):
        _, plan, request = self._issue_request()
        claim = self.harness.controller.claim_execution(
            request,
            current_binding=request.binding,
            now=120.0,
        )
        assert claim.lease is not None
        receipt = self.harness.complete_fixture(
            request,
            claim.lease,
            status=ActionReceiptStatus.STALE,
            effect_state=ActionEffectState.NO_SIDE_EFFECT,
            result_digest=_digest(self._next("stale-after-attempt")),
            completed_at=130.0,
            reason_codes=("generation_stale_after_attempt",),
        )
        outcome = self.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        self.assertIs(outcome.kind, ActionOutcomeKind.STALE_NO_EFFECT)
        self.assertTrue(outcome.attempted)

    def test_confirmed_terminal_receipt_cannot_bypass_continuation_lineage(self):
        (
            _,
            request,
            _,
            current_plan,
            confirmed,
            lineage,
        ) = self._continuation(PendingDecision.CONFIRM)
        claim = self.harness.controller.claim_execution(
            request,
            pending_confirmation=confirmed,
            current_binding=confirmed.confirmation_binding,
            now=131.0,
        )
        assert claim.lease is not None
        receipt = self.harness.complete_fixture(
            request,
            claim.lease,
            status=ActionReceiptStatus.FAILED,
            effect_state=ActionEffectState.NOT_COMMITTED,
            result_digest=_digest(self._next("confirmed-failure")),
            completed_at=140.0,
            reason_codes=("bounded_failure",),
        )
        with self.assertRaisesRegex(ContractViolation, "continuation_required"):
            self.authority.issue_from_receipt(
                current_planned_action=current_plan,
                origin_request=request,
                receipt=receipt,
            )
        outcome = self.authority.issue_from_continuation(
            current_planned_action=current_plan,
            lineage=lineage,
        )
        self.assertIs(outcome.kind, ActionOutcomeKind.FAILED_NO_EFFECT)

    def test_receipt_output_is_hard_rejected_until_safe_output_authority_exists(self):
        _, plan, request = self._issue_request()
        claim = self.harness.controller.claim_execution(
            request,
            current_binding=request.binding,
            now=120.0,
        )
        assert claim.lease is not None
        output = ActionOutput.from_safe_text(
            binding=request.binding,
            request_digest=request.request_digest,
            text="bounded synthetic output",
        )
        receipt = self.harness.complete_fixture(
            request,
            claim.lease,
            status=ActionReceiptStatus.SUCCEEDED,
            effect_state=ActionEffectState.NO_SIDE_EFFECT,
            result_digest=_digest(self._next("read-success")),
            completed_at=130.0,
            output=output,
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "action_outcome_output_authority_unavailable",
        ):
            self.authority.issue_from_receipt(
                current_planned_action=plan,
                origin_request=request,
                receipt=receipt,
            )

    def test_copy_cross_authority_cross_plan_and_duplicate_source_fail_closed(self):
        plan, request, receipt = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        outcome = self.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        with self.assertRaisesRegex(ContractViolation, "action_outcome_source_replayed"):
            self.authority.issue_from_receipt(
                current_planned_action=plan,
                origin_request=request,
                receipt=receipt,
            )

        copied = copy.copy(outcome)
        with self.assertRaisesRegex(ContractViolation, "action_outcome_not_canonical"):
            self.authority.inspect_outcome(
                copied,
                current_planned_action=plan,
            )

        _, other_plan, _ = self._issue_request()
        with self.assertRaisesRegex(ContractViolation, "action_outcome_plan_mismatch"):
            self.authority.inspect_outcome(
                outcome,
                current_planned_action=other_plan,
            )

        other_harness = controller_support.OwnerActionControllerTests(
            methodName=(
                "test_exact_private_configured_owner_admission_issues_canonical_request"
            )
        )
        other_harness.setUp()
        other_authority = ActionOutcomeAuthority.issue_for_runtime(
            other_harness.controller,
            other_harness.plan_authority,
        )
        with self.assertRaisesRegex(ContractViolation, "action_outcome_not_canonical"):
            other_authority.inspect_outcome(
                outcome,
                current_planned_action=plan,
            )
        with self.assertRaises(ContractViolation):
            other_authority.issue_from_receipt(
                current_planned_action=plan,
                origin_request=request,
                receipt=receipt,
            )

    def test_concurrent_duplicate_source_has_one_canonical_winner(self):
        plan, request, receipt = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )

        def issue_once():
            try:
                return self.authority.issue_from_receipt(
                    current_planned_action=plan,
                    origin_request=request,
                    receipt=receipt,
                )
            except ContractViolation as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(lambda _: issue_once(), range(2)))
        winners = tuple(value for value in results if type(value) is ActionOutcomeIntent)
        failures = tuple(value for value in results if type(value) is str)
        self.assertEqual(len(winners), 1)
        self.assertEqual(failures, ("action_outcome_source_replayed",))

    def test_concurrent_persistent_consume_has_one_winner(self):
        plan, request, receipt = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        outcome = self.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )

        def consume_once():
            try:
                return self.authority.consume_outcome(
                    outcome,
                    current_planned_action=plan,
                )
            except ContractViolation as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(pool.map(lambda _: consume_once(), range(8)))
        winners = tuple(value for value in results if value is outcome)
        failures = tuple(value for value in results if type(value) is str)
        self.assertEqual(len(winners), 1)
        self.assertEqual(failures, ("action_outcome_replayed",) * 7)

    def test_same_digest_different_canonical_plan_cannot_substitute_origin_plan(self):
        admission, proposal, plan, duplicate, route = (
            self._prepared_route_with_duplicate_plan()
        )
        draft = self.harness.adapter_draft(admission, proposal)
        request = self.harness.controller.issue_request(
            self.harness.fixture.ticket_for(admission),
            route,
            adapter_draft=draft,
            issued_at=100.0,
            deadline=220.0,
            idempotency_key=_digest(self._next("duplicate-receipt-idempotency")),
        )
        claim = self.harness.controller.claim_execution(
            request,
            current_binding=request.binding,
            now=120.0,
        )
        assert claim.lease is not None
        receipt = self.harness.complete_fixture(
            request,
            claim.lease,
            status=ActionReceiptStatus.FAILED,
            effect_state=ActionEffectState.NO_SIDE_EFFECT,
            result_digest=_digest(self._next("duplicate-receipt-result")),
            completed_at=130.0,
            reason_codes=("bounded_failure",),
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "action_outcome_origin_plan_mismatch",
        ):
            self.authority.issue_from_receipt(
                current_planned_action=duplicate,
                origin_request=request,
                receipt=receipt,
            )
        outcome = self.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        self.assertIs(outcome.kind, ActionOutcomeKind.FAILED_NO_EFFECT)

        admission, _, plan, duplicate, route = (
            self._prepared_route_with_duplicate_plan()
        )
        denial = self.harness.controller.deny_action_route(
            self.harness.fixture.ticket_for(admission),
            route,
            denied_at=110.0,
            reason_codes=("adapter_not_enabled",),
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "action_outcome_origin_plan_mismatch",
        ):
            self.authority.issue_from_denial(
                current_planned_action=duplicate,
                denial=denial,
            )
        denied = self.authority.issue_from_denial(
            current_planned_action=plan,
            denial=denial,
        )
        self.assertIs(denied.kind, ActionOutcomeKind.DENIED)

    def test_outcome_and_source_object_mutation_fail_closed(self):
        plan, request, receipt = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        outcome = self.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        object.__setattr__(outcome, "kind", ActionOutcomeKind.TIMED_OUT)
        with self.assertRaisesRegex(
            ContractViolation,
            "action_outcome_authority_state_corrupt",
        ):
            self.authority.inspect_outcome(
                outcome,
                current_planned_action=plan,
            )
        object.__setattr__(outcome, "kind", ActionOutcomeKind.FAILED_NO_EFFECT)

        operation_plan, operation_request, operation_receipt = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        operation_outcome = self.authority.issue_from_receipt(
            current_planned_action=operation_plan,
            origin_request=operation_request,
            receipt=operation_receipt,
        )
        object.__setattr__(
            operation_outcome,
            "operation",
            ActionOutcomeOperation.SAVE_MEMORY,
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "action_outcome_authority_state_corrupt",
        ):
            self.authority.inspect_outcome(
                operation_outcome,
                current_planned_action=operation_plan,
            )
        object.__setattr__(
            operation_outcome,
            "operation",
            ActionOutcomeOperation.READ_ARTIFACT,
        )

        other_plan, other_request, other_receipt = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        other_outcome = self.authority.issue_from_receipt(
            current_planned_action=other_plan,
            origin_request=other_request,
            receipt=other_receipt,
        )
        object.__setattr__(other_receipt, "status", ActionReceiptStatus.DENIED)
        with self.assertRaises(ContractViolation):
            self.authority.inspect_outcome(
                other_outcome,
                current_planned_action=other_plan,
            )

    def test_outcome_rendering_requires_exact_canonical_snapshot_and_recovers(self):
        class EqualKind(str, Enum):
            VALUE = ActionOutcomeKind.FAILED_NO_EFFECT.value

        class Marker:
            value = "SENSITIVE-OUTCOME-MARKER"

            def __bool__(self):
                return True

            def __eq__(self, other):
                del other
                return True

        plan, request, receipt = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        outcome = self.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        canonical_trace = outcome.trace_metadata()
        self.assertEqual(canonical_trace["outcome"], "failed_no_effect")
        self.assertIs(canonical_trace["attempted"], True)
        self.assertIs(canonical_trace["has_output"], False)

        copied = copy.copy(outcome)
        with self.assertRaisesRegex(ContractViolation, "action_outcome_corrupt"):
            copied.trace_metadata()
        with self.assertRaisesRegex(ContractViolation, "action_outcome_corrupt"):
            repr(copied)

        originals = {
            field_name: object.__getattribute__(outcome, field_name)
            for field_name in ("kind", "operation", "attempted", "has_output")
        }
        for field_name, replacement in (
            ("kind", ActionOutcomeKind.TIMED_OUT),
            ("kind", EqualKind.VALUE),
            ("kind", Marker()),
            ("operation", ActionOutcomeOperation.SAVE_MEMORY),
            ("operation", Marker()),
            ("attempted", 1),
            ("attempted", Marker()),
            ("has_output", 0),
            ("has_output", Marker()),
        ):
            with self.subTest(field=field_name, replacement=type(replacement)):
                object.__setattr__(outcome, field_name, replacement)
                try:
                    with self.assertRaisesRegex(
                        ContractViolation,
                        "action_outcome_corrupt",
                    ):
                        outcome.trace_metadata()
                    with self.assertRaisesRegex(
                        ContractViolation,
                        "action_outcome_corrupt",
                    ):
                        repr(outcome)
                    with self.assertRaises(ContractViolation):
                        self.authority.inspect_outcome(
                            outcome,
                            current_planned_action=plan,
                        )
                finally:
                    object.__setattr__(
                        outcome,
                        field_name,
                        originals[field_name],
                    )
                self.assertIs(
                    self.authority.inspect_outcome(
                        outcome,
                        current_planned_action=plan,
                    ),
                    outcome,
                )

        for field_name in ("kind", "operation", "attempted", "has_output"):
            with self.subTest(deleted_field=field_name):
                original = originals[field_name]
                object.__delattr__(outcome, field_name)
                try:
                    with self.assertRaisesRegex(
                        ContractViolation,
                        "action_outcome_corrupt",
                    ):
                        outcome.trace_metadata()
                    with self.assertRaisesRegex(
                        ContractViolation,
                        "action_outcome_corrupt",
                    ):
                        repr(outcome)
                    with self.assertRaises(ContractViolation):
                        self.authority.inspect_outcome(
                            outcome,
                            current_planned_action=plan,
                        )
                finally:
                    object.__setattr__(outcome, field_name, original)
                self.assertIs(
                    self.authority.inspect_outcome(
                        outcome,
                        current_planned_action=plan,
                    ),
                    outcome,
                )

        rendered = repr(outcome) + json.dumps(outcome.trace_metadata())
        self.assertNotIn("SENSITIVE-OUTCOME-MARKER", rendered)
        self.authority.abandon_before_composer(
            outcome,
            current_planned_action=plan,
        )
        self.assertEqual(outcome.trace_metadata(), canonical_trace)
        object.__setattr__(outcome, "attempted", 1)
        try:
            with self.assertRaisesRegex(
                ContractViolation,
                "action_outcome_corrupt",
            ):
                outcome.trace_metadata()
            with self.assertRaises(ContractViolation):
                self.authority._inspect_reclaimed_source(outcome, receipt)
        finally:
            object.__setattr__(outcome, "attempted", originals["attempted"])
        object.__delattr__(outcome, "kind")
        try:
            with self.assertRaisesRegex(
                ContractViolation,
                "action_outcome_corrupt",
            ):
                repr(outcome)
            with self.assertRaises(ContractViolation):
                self.authority._inspect_reclaimed_source(outcome, receipt)
        finally:
            object.__setattr__(outcome, "kind", originals["kind"])
        self.assertEqual(
            self.authority._inspect_reclaimed_source(outcome, receipt),
            "abandoned_before_composer",
        )

    def test_action_outcome_is_not_grounding_and_has_no_grounding_conversion(self):
        plan, request, receipt = self._direct_receipt(
            status=ActionReceiptStatus.TIMED_OUT,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        outcome = self.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        self.assertNotIsInstance(outcome, GroundingFact)
        self.assertFalse(hasattr(outcome, "to_grounding"))
        self.assertIs(outcome.kind, ActionOutcomeKind.TIMED_OUT)

    def test_abandon_before_composer_reclaims_only_unclaimed_exact_outcome(self):
        plan, request, receipt = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        outcome = self.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        copied = copy.copy(outcome)
        with self.assertRaisesRegex(ContractViolation, "action_outcome_not_canonical"):
            self.authority.abandon_before_composer(
                copied,
                current_planned_action=plan,
            )
        self.assertIs(
            self.authority.inspect_outcome(
                outcome,
                current_planned_action=plan,
            ),
            outcome,
        )

        def abandon_once():
            try:
                return self.authority.abandon_before_composer(
                    outcome,
                    current_planned_action=plan,
                )
            except ContractViolation as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(pool.map(lambda _: abandon_once(), range(8)))
        self.assertEqual(sum(value is outcome for value in results), 1)
        self.assertEqual(
            tuple(value for value in results if type(value) is str),
            ("action_outcome_reclaimed",) * 7,
        )
        self.assertEqual(self.authority.trace_metadata()["outcome_count"], 0)
        with self.assertRaisesRegex(ContractViolation, "action_outcome_reclaimed"):
            self.authority.inspect_outcome(
                outcome,
                current_planned_action=plan,
            )
        with self.assertRaisesRegex(ContractViolation, "action_outcome_reclaimed"):
            self.authority.abandon_before_composer(
                outcome,
                current_planned_action=plan,
            )
        with self.assertRaisesRegex(ContractViolation, "action_outcome_source_replayed"):
            self.authority.issue_from_receipt(
                current_planned_action=plan,
                origin_request=request,
                receipt=receipt,
            )

    def test_claimed_composer_cannot_be_abandoned_and_only_exact_delivery_ack_reclaims(self):
        delivery, plan, _request, _receipt, outcome, composer = (
            self._claimed_composer_outcome()
        )
        authority = delivery.case.authority
        with self.assertRaisesRegex(
            ContractViolation,
            "action_outcome_already_claimed",
        ):
            authority.abandon_before_composer(
                outcome,
                current_planned_action=plan,
            )
        self.assertIs(
            authority.inspect_for_composer(
                outcome,
                plan,
                consumer=composer,
            ),
            outcome,
        )

        copied_consumer = copy.copy(composer)
        with self.assertRaises(ContractViolation):
            authority.acknowledge_delivery_terminal(
                outcome,
                plan,
                consumer=copied_consumer,
            )
        self.assertIs(
            authority.inspect_for_composer(
                outcome,
                plan,
                consumer=composer,
            ),
            outcome,
        )
        self.assertIs(
            authority.acknowledge_delivery_terminal(
                outcome,
                plan,
                consumer=composer,
            ),
            outcome,
        )
        composer_ref = weakref.ref(composer)
        with self.assertRaisesRegex(ContractViolation, "action_outcome_reclaimed"):
            authority.inspect_for_composer(
                outcome,
                plan,
                consumer=composer,
            )
        with self.assertRaisesRegex(ContractViolation, "action_outcome_reclaimed"):
            authority.acknowledge_delivery_terminal(
                outcome,
                plan,
                consumer=composer,
            )
        del copied_consumer, composer
        gc.collect()
        self.assertIsNone(composer_ref())

    def test_delivery_ack_is_atomic_single_winner_under_copy_cross_and_concurrency(self):
        delivery, plan, _request, _receipt, outcome, composer = (
            self._claimed_composer_outcome()
        )
        authority = delivery.case.authority

        with self.assertRaisesRegex(ContractViolation, "action_outcome_not_canonical"):
            authority.acknowledge_delivery_terminal(
                copy.copy(outcome),
                plan,
                consumer=composer,
            )

        def acknowledge_once():
            try:
                return authority.acknowledge_delivery_terminal(
                    outcome,
                    plan,
                    consumer=composer,
                )
            except ContractViolation as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(pool.map(lambda _: acknowledge_once(), range(8)))
        self.assertEqual(sum(value is outcome for value in results), 1)
        self.assertEqual(
            tuple(value for value in results if type(value) is str),
            ("action_outcome_reclaimed",) * 7,
        )

        other_plan, other_request, other_receipt = self._direct_receipt(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        other_outcome = self.authority.issue_from_receipt(
            current_planned_action=other_plan,
            origin_request=other_request,
            receipt=other_receipt,
        )
        with self.assertRaisesRegex(ContractViolation, "action_outcome_not_canonical"):
            authority.abandon_before_composer(
                other_outcome,
                current_planned_action=other_plan,
            )

    def test_512_reclaimed_outcomes_do_not_exhaust_active_ledger(self):
        object.__setattr__(
            self.harness.fixture.admission_controller,
            "_max_proofs",
            1024,
        )
        object.__setattr__(
            self.harness.fixture.accepted_turn_authority,
            "_max_turns",
            1024,
        )
        object.__setattr__(
            self.harness.fixture.owner_action_router,
            "_max_routes",
            1024,
        )
        object.__setattr__(self.harness.plan_authority, "_max_plans", 1024)
        object.__setattr__(self.harness.controller, "_max_ledger_entries", 1024)

        durable = DurableFinalizeHarness(
            self.harness.controller,
            self.authority,
        )
        for index in range(512):
            admission = self.harness.fixture.admitted(
                message=f"reclaim-512-{index}",
            )
            decision = self.harness.route_decision(admission)
            proposal = decision.proposal
            assert proposal is not None
            plan = self.harness.planned_action(
                admission,
                capability=proposal.capability,
                operation=proposal.operation,
            )
            route = self.harness.controller.seal_action_route(
                self.harness.fixture.ticket_for(admission),
                plan,
                decision,
            )
            denial = self.harness.controller.deny_action_route(
                self.harness.fixture.ticket_for(admission),
                route,
                denied_at=110.0 + index,
                reason_codes=("adapter_not_enabled",),
            )
            outcome = self.authority.issue_from_denial(
                current_planned_action=plan,
                denial=denial,
            )
            self.authority.abandon_before_composer(
                outcome,
                current_planned_action=plan,
            )
            tickets = durable.abandoned_tickets(
                source=denial,
                outcome=outcome,
                current_planned_action=plan,
                now=1000.0 + index,
            )
            self.harness.controller.release_terminal_lineage(
                denial,
                outcome=outcome,
                outcome_authority=self.authority,
                ticket=tickets.controller,
                durable_authority=durable.authority,
                reclaimed_at=120.0 + index,
            )
            durable.finalize_outcome(
                outcome=outcome,
                current_planned_action=plan,
                ticket=tickets.outcome,
            )
        self.assertEqual(durable.authority.trace_metadata()["active_dispatches"], 0)
        self.assertEqual(durable.store.prune(now=5000.0), 512)
        self.assertEqual(durable.store.trace_metadata()["record_count"], 0)
        durable.close()

        metadata = self.authority.trace_metadata()
        self.assertEqual(metadata["outcome_count"], 0)
        self.assertEqual(metadata["consumed_count"], 0)
        self.assertEqual(metadata["reclaimed_count"], 0)
        self.assertTrue(metadata["ledger_bounded"])
        controller_metadata = self.harness.controller.trace_metadata()
        self.assertEqual(controller_metadata["ledger_count"], 0)
        self.assertEqual(controller_metadata["denial_count"], 0)
        self.assertEqual(controller_metadata["lifecycle_tombstone_count"], 0)

    def test_512_prepared_aborts_leave_no_controller_or_durable_debt(self):
        object.__setattr__(
            self.harness.fixture.admission_controller,
            "_max_proofs",
            1024,
        )
        object.__setattr__(
            self.harness.fixture.accepted_turn_authority,
            "_max_turns",
            1024,
        )
        object.__setattr__(
            self.harness.fixture.owner_action_router,
            "_max_routes",
            1024,
        )
        object.__setattr__(self.harness.plan_authority, "_max_plans", 1024)
        object.__setattr__(self.harness.controller, "_max_ledger_entries", 1024)

        durable = DurableFinalizeHarness(
            self.harness.controller,
            self.authority,
        )
        for index in range(512):
            _admission, _plan, request = self._issue_request()
            _handle, ticket = durable.prepared_abort_ticket(
                request,
                now=1000.0 + index * 2.0,
            )
            self.harness.controller.abort_prepared(
                request,
                ticket=ticket,
                durable_authority=durable.authority,
                aborted_at=1000.5 + index * 2.0,
            )
            durable.authority.complete_prepared_abort(
                ticket,
                now=1001.0 + index * 2.0,
            )
        self.assertEqual(durable.authority.trace_metadata()["active_dispatches"], 0)
        self.assertGreater(durable.store.prune(now=5000.0), 0)
        self.assertEqual(durable.store.trace_metadata()["record_count"], 0)
        durable.close()

        metadata = self.harness.controller.trace_metadata()
        self.assertEqual(metadata["ledger_count"], 0)
        self.assertEqual(metadata["lifecycle_tombstone_count"], 0)
        self.assertEqual(len(self.harness.controller._idempotency), 0)
        self.assertEqual(len(self.harness.controller._routes), 0)
        self.assertEqual(len(self.harness.controller._route_by_ticket), 0)

    def test_confirmation_required_delivery_ack_reclaims_only_outcome(self):
        from astrbot_plugin_shio.core.reply_composer import (
            build_reply_composer_request,
        )
        from astrbot_plugin_shio.tests.test_action_outcome_delivery import (
            ActionOutcomeDeliveryTests,
        )

        ActionOutcomeDeliveryTests.setUpClass()
        delivery = ActionOutcomeDeliveryTests(
            methodName=(
                "test_builder_claims_only_after_complete_validation_and_inspection_is_non_consuming"
            )
        )
        delivery.setUp()
        plan, request, pending, receipt = delivery.case._pending_origin()
        outcome = delivery.case.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        message = controller_support.second_turn_message(
            plan.binding.current_message_id
        )
        composer = build_reply_composer_request(
            **delivery._composer_inputs(plan, outcome, message)
        )
        delivery.case.authority.acknowledge_delivery_terminal(
            outcome,
            plan,
            consumer=composer,
        )
        with self.assertRaises(TypeError):
            delivery.case.harness.controller.release_terminal_lineage(
                receipt,
                outcome=outcome,
                outcome_authority=delivery.case.authority,
                reclaimed_at=120.0,
            )
        inspection = delivery.case.harness.controller.inspect_request(request)
        self.assertIs(inspection.state, controller_support.OwnerActionLedgerState.CONFIRMATION_REQUIRED)
        self.assertIs(
            delivery.case.harness.controller.canonical_receipt_for(request),
            receipt,
        )
        self.assertIs(pending.request, request)

    def test_continuation_terminal_release_clears_resolution_and_origin_graph(self):
        (
            _origin_plan,
            request,
            _pending_receipt,
            current_plan,
            _resolved,
            lineage,
        ) = self._continuation(PendingDecision.DENY)
        outcome = self.authority.issue_from_continuation(
            current_planned_action=current_plan,
            lineage=lineage,
        )
        self.authority.abandon_before_composer(
            outcome,
            current_planned_action=current_plan,
        )
        durable = DurableFinalizeHarness(
            self.harness.controller,
            self.authority,
        )
        tickets = durable.abandoned_tickets(
            source=lineage,
            outcome=outcome,
            current_planned_action=current_plan,
            now=150.0,
        )
        tombstone = self.harness.controller.release_terminal_lineage(
            lineage,
            outcome=outcome,
            outcome_authority=self.authority,
            ticket=tickets.controller,
            durable_authority=durable.authority,
            reclaimed_at=140.0,
        )
        durable.finalize_outcome(
            outcome=outcome,
            current_planned_action=current_plan,
            ticket=tickets.outcome,
        )
        durable.close()
        self.assertIs(tombstone.status, ActionReceiptStatus.DENIED)
        self.assertIs(tombstone.effect_state, ActionEffectState.NOT_STARTED)
        self.assertFalse(tombstone.attempted)
        self.assertEqual(self.harness.controller.ledger_size, 0)
        self.assertEqual(len(self.harness.controller._receipts), 0)
        self.assertEqual(len(self.harness.controller._continuations), 0)
        self.assertEqual(len(self.harness.controller._resolution_routes), 0)
        self.assertEqual(len(self.harness.controller._resolution_route_by_ticket), 0)
        with self.assertRaisesRegex(ContractViolation, "request_not_canonical"):
            self.harness.controller.inspect_request(request)
        with self.assertRaisesRegex(
            ContractViolation,
            "continuation_lineage_not_canonical",
        ):
            self.harness.controller.inspect_continuation_lineage(lineage)

    def test_denial_release_rolls_back_late_delete_baseexception_and_retries(self):
        class DeleteThenRaiseDict(dict):
            def __init__(self, values):
                super().__init__(values)
                self.armed = True

            def __delitem__(self, key):
                super().__delitem__(key)
                if self.armed:
                    self.armed = False
                    raise KeyboardInterrupt("delete_then_raise")

        admission = self.harness.fixture.admitted(message="denial-release-rollback")
        decision = self.harness.route_decision(admission)
        proposal = decision.proposal
        assert proposal is not None
        plan = self.harness.planned_action(
            admission,
            capability=proposal.capability,
            operation=proposal.operation,
        )
        ticket = self.harness.fixture.ticket_for(admission)
        route = self.harness.controller.seal_action_route(ticket, plan, decision)
        denial = self.harness.controller.deny_action_route(
            ticket,
            route,
            denied_at=110.0,
            reason_codes=("adapter_not_enabled",),
        )
        outcome = self.authority.issue_from_denial(
            current_planned_action=plan,
            denial=denial,
        )
        self.authority.abandon_before_composer(
            outcome,
            current_planned_action=plan,
        )
        durable = DurableFinalizeHarness(
            self.harness.controller,
            self.authority,
        )
        tickets = durable.abandoned_tickets(
            source=denial,
            outcome=outcome,
            current_planned_action=plan,
            now=150.0,
        )
        self.harness.controller._route_by_ticket = DeleteThenRaiseDict(
            self.harness.controller._route_by_ticket
        )

        with self.assertRaisesRegex(KeyboardInterrupt, "delete_then_raise"):
            self.harness.controller.release_terminal_lineage(
                denial,
                outcome=outcome,
                outcome_authority=self.authority,
                ticket=tickets.controller,
                durable_authority=durable.authority,
                reclaimed_at=120.0,
            )

        self.assertIs(self.harness.controller.inspect_denial(denial), denial)
        self.assertEqual(len(self.harness.controller._denials), 1)
        self.assertEqual(len(self.harness.controller._routes), 1)
        self.assertEqual(len(self.harness.controller._route_by_ticket), 1)
        self.assertEqual(len(self.harness.controller._tombstones), 0)

        tombstone = self.harness.controller.release_terminal_lineage(
            denial,
            outcome=outcome,
            outcome_authority=self.authority,
            ticket=tickets.controller,
            durable_authority=durable.authority,
            reclaimed_at=121.0,
        )
        self.assertIs(
            self.harness.controller.inspect_lifecycle_tombstone(tombstone),
            tombstone,
        )
        durable.finalize_outcome(
            outcome=outcome,
            current_planned_action=plan,
            ticket=tickets.outcome,
        )
        durable.close()
        self.assertEqual(len(self.harness.controller._denials), 0)
        self.assertEqual(len(self.harness.controller._routes), 0)
        self.assertEqual(len(self.harness.controller._route_by_ticket), 0)

    def test_continuation_release_rolls_back_late_delete_baseexception_and_retries(self):
        class DeleteThenRaiseDict(dict):
            def __init__(self, values):
                super().__init__(values)
                self.armed = True

            def __delitem__(self, key):
                super().__delitem__(key)
                if self.armed:
                    self.armed = False
                    raise KeyboardInterrupt("delete_then_raise")

        (
            _origin_plan,
            request,
            _pending_receipt,
            current_plan,
            _resolved,
            lineage,
        ) = self._continuation(PendingDecision.DENY)
        receipt = self.harness.controller.inspect_continuation_lineage(lineage).receipt
        assert receipt is not None
        outcome = self.authority.issue_from_continuation(
            current_planned_action=current_plan,
            lineage=lineage,
        )
        self.authority.abandon_before_composer(
            outcome,
            current_planned_action=current_plan,
        )
        durable = DurableFinalizeHarness(
            self.harness.controller,
            self.authority,
        )
        tickets = durable.abandoned_tickets(
            source=lineage,
            outcome=outcome,
            current_planned_action=current_plan,
            now=160.0,
        )
        self.harness.controller._continuations = DeleteThenRaiseDict(
            self.harness.controller._continuations
        )

        with self.assertRaisesRegex(KeyboardInterrupt, "delete_then_raise"):
            self.harness.controller.release_terminal_lineage(
                lineage,
                outcome=outcome,
                outcome_authority=self.authority,
                ticket=tickets.controller,
                durable_authority=durable.authority,
                reclaimed_at=140.0,
            )

        self.assertIs(
            self.harness.controller.inspect_continuation_lineage(lineage).receipt,
            receipt,
        )
        self.assertIs(self.harness.controller.inspect_receipt(receipt), receipt)
        self.assertIs(self.harness.controller.inspect_request(request).request, request)
        self.assertEqual(len(self.harness.controller._receipts), 2)
        self.assertEqual(len(self.harness.controller._continuations), 1)
        self.assertEqual(len(self.harness.controller._resolution_routes), 1)
        self.assertEqual(
            len(self.harness.controller._resolution_route_by_ticket),
            1,
        )
        self.assertEqual(len(self.harness.controller._tombstones), 0)

        tombstone = self.harness.controller.release_terminal_lineage(
            lineage,
            outcome=outcome,
            outcome_authority=self.authority,
            ticket=tickets.controller,
            durable_authority=durable.authority,
            reclaimed_at=141.0,
        )
        self.assertIs(
            self.harness.controller.inspect_lifecycle_tombstone(tombstone),
            tombstone,
        )
        durable.finalize_outcome(
            outcome=outcome,
            current_planned_action=current_plan,
            ticket=tickets.outcome,
        )
        durable.close()
        self.assertEqual(self.harness.controller.ledger_size, 0)
        self.assertEqual(len(self.harness.controller._receipts), 0)
        self.assertEqual(len(self.harness.controller._continuations), 0)
        self.assertEqual(len(self.harness.controller._resolution_routes), 0)
        self.assertEqual(
            len(self.harness.controller._resolution_route_by_ticket),
            0,
        )


if __name__ == "__main__":
    unittest.main()
