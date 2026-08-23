from __future__ import annotations

import copy
import dataclasses
import gc
import inspect
import unittest
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_shio.core.action_outcome import (
    ActionOutcomeKind,
)
from astrbot_plugin_shio.core.affect import appraise_affect
from astrbot_plugin_shio.core.affect_state import _issue_test_affect_render_context
from astrbot_plugin_shio.core.capability_policy import build_owner_capability_policy
from astrbot_plugin_shio.core.content_intent_builder import (
    ContentIntentBuildError,
    attach_action_outcome,
    attach_grounding_facts,
    build_content_intent_seed,
)
from astrbot_plugin_shio.core.contracts import (
    ActionEffectState,
    ActionKind,
    ActionReceiptStatus,
    ContractViolation,
    ExpressionIntent,
    ExpressionModality,
    GroundingFact,
)
from astrbot_plugin_shio.core.current_question_anchor import (
    build_current_question_anchor,
)
from astrbot_plugin_shio.core.expression_retrieval import (
    retrieve_expression_candidates,
)
from astrbot_plugin_shio.core.identity import resolve_principal
from astrbot_plugin_shio.core.owner_action_controller import PendingDecision
from astrbot_plugin_shio.core.persona import load_persona_package
from astrbot_plugin_shio.core.persona_expression import (
    build_persona_expression_plan,
)
from astrbot_plugin_shio.core.reply_composer import (
    ReplyComposerRequest,
    ReplyComposerRequestError,
    build_reply_composer_request,
)
from astrbot_plugin_shio.tests import test_action_outcome as outcome_support


ROOT = Path(__file__).parents[1]
PERSONA_DIR = ROOT / "assets" / "personas"


class ActionOutcomeDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.persona = load_persona_package(PERSONA_DIR / "atri.json")

    def setUp(self) -> None:
        self.case = outcome_support.ActionOutcomeTests(
            methodName=(
                "test_direct_receipt_maps_to_sealed_minimal_intent_and_consumes_once"
            )
        )
        self.case.setUp()

    @staticmethod
    def _message_for_plan(plan, *, continuation: PendingDecision | None = None) -> str:
        if continuation is PendingDecision.CONFIRM:
            return "确认保存"
        if continuation is PendingDecision.DENY:
            return "拒绝保存"
        if continuation is PendingDecision.CANCEL:
            return "取消保存"
        message_id = plan.binding.current_message_id
        return (
            f'请读取文件 path="C:\\trusted\\{message_id}.txt" '
            "offset=0 limit=20"
        )

    def _issue_direct(self, *, status, effect):
        plan, request, receipt = self.case._direct_receipt(
            status=status,
            effect=effect,
        )
        outcome = self.case.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        return plan, outcome, self._message_for_plan(plan)

    def _issue_denial(self):
        label = self.case._next("delivery-denial")
        admission = self.case.harness.fixture.admitted(message=label)
        decision = self.case.harness.route_decision(admission)
        proposal = decision.proposal
        assert proposal is not None
        plan = self.case.harness.planned_action(
            admission,
            capability=proposal.capability,
            operation=proposal.operation,
        )
        route = self.case.harness.controller.seal_action_route(
            self.case.harness.fixture.ticket_for(admission),
            plan,
            decision,
        )
        denial = self.case.harness.controller.deny_action_route(
            self.case.harness.fixture.ticket_for(admission),
            route,
            denied_at=110.0,
            reason_codes=("adapter_not_enabled",),
        )
        outcome = self.case.authority.issue_from_denial(
            current_planned_action=plan,
            denial=denial,
        )
        return plan, outcome, self._message_for_plan(plan)

    def _issue_confirmation_required(self):
        plan, request, _pending, receipt = self.case._pending_origin()
        outcome = self.case.authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        return (
            plan,
            outcome,
            outcome_support.controller_support.second_turn_message(
                plan.binding.current_message_id
            ),
        )

    def _issue_continuation(self, decision: PendingDecision):
        (
            _origin_plan,
            request,
            _pending_receipt,
            current_plan,
            resolved,
            lineage,
        ) = self.case._continuation(decision)
        if decision is PendingDecision.CONFIRM:
            claim = self.case.harness.controller.claim_execution(
                request,
                pending_confirmation=resolved,
                current_binding=resolved.confirmation_binding,
                now=131.0,
            )
            assert claim.lease is not None
            self.case.harness.complete_fixture(
                request,
                claim.lease,
                status=ActionReceiptStatus.SUCCEEDED,
                effect_state=ActionEffectState.COMMITTED,
                result_digest=outcome_support._digest("delivery-committed"),
                completed_at=140.0,
            )
        outcome = self.case.authority.issue_from_continuation(
            current_planned_action=current_plan,
            lineage=lineage,
        )
        return (
            current_plan,
            outcome,
            self._message_for_plan(current_plan, continuation=decision),
        )

    def _composer_inputs(self, plan, outcome, message: str):
        target = plan.action.reply_target
        assert target is not None
        principal = resolve_principal(
            sender_id="owner-a",
            sender_key=plan.binding.current_sender_key,
            chat_type="private",
            owner_ids=("owner-a",),
            verification_source="astrbot_event_sender_id",
            identity_verified=True,
        )
        policy = build_owner_capability_policy(principal)
        anchor = build_current_question_anchor(plan.binding, message)
        seed = build_content_intent_seed(anchor=anchor, reply_target=target)
        seed = attach_action_outcome(
            seed,
            outcome,
            self.case.authority,
            current_planned_action=plan,
        )
        appraisal = appraise_affect(
            principal=principal,
            reply_target=target,
            current_message=message,
        )
        persona_expression = build_persona_expression_plan(
            self.persona,
            appraisal,
            principal=principal,
        )
        retrieval = retrieve_expression_candidates(self.persona, persona_expression)
        expression_intent = ExpressionIntent(
            binding=plan.binding,
            reply_target=target,
            modality=ExpressionModality.TEXT,
            social_act=persona_expression.topic_return,
            emotion_tags=(appraisal.trigger.value, appraisal.surface_emotion.value),
            max_bubbles=3,
            reason_codes=("action_outcome_delivery",),
        )
        return {
            "planned_action": plan,
            "content_seed": seed,
            "expression_intent": expression_intent,
            "affect_appraisal": appraisal,
            "continuous_affect": _issue_test_affect_render_context(plan.binding),
            "persona_expression": persona_expression,
            "expression_candidates": retrieval.candidates,
            "persona_package": self.persona,
            "capability_policy": policy,
            "current_message": message,
            "sender_name": "主人",
            "current_question_anchor": anchor,
        }

    def test_content_attachment_is_exact_non_consuming_and_exclusive_with_grounding(self):
        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        values = self._composer_inputs(plan, outcome, message)
        seed = values["content_seed"]

        self.assertIs(seed.action_outcome, outcome)
        self.assertIs(seed.action_outcome_authority, self.case.authority)
        self.assertEqual(seed.intent.grounding_facts, ())
        self.assertIs(
            self.case.authority.inspect_outcome(
                outcome,
                current_planned_action=plan,
            ),
            outcome,
        )
        with self.assertRaises(ContentIntentBuildError):
            attach_grounding_facts(
                seed,
                (
                    GroundingFact(
                        binding=plan.binding,
                        fact_id="forbidden-action-fact",
                        claim="动作结果不能成为 GroundingFact。",
                        source_kind="anysearch",
                        source_digest="a" * 64,
                        confidence=0.9,
                        observed_at=1.0,
                    ),
                ),
            )

    def test_builder_claims_only_after_complete_validation_and_inspection_is_non_consuming(self):
        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        values = self._composer_inputs(plan, outcome, message)
        with self.assertRaises(ReplyComposerRequestError):
            build_reply_composer_request(**{**values, "reply_shape": "invalid"})
        self.assertIs(
            self.case.authority.inspect_outcome(
                outcome,
                current_planned_action=plan,
            ),
            outcome,
        )

        request = build_reply_composer_request(**values)
        self.assertIs(request.planned_action, plan)
        self.assertIs(request.content_seed, values["content_seed"])
        self.assertIs(request.action_outcome, outcome)
        self.assertIs(request.action_outcome_authority, self.case.authority)
        self.assertIs(
            self.case.authority.inspect_for_composer(
                outcome,
                plan,
                consumer=request,
            ),
            outcome,
        )
        self.assertIs(
            self.case.authority.inspect_for_composer(
                outcome,
                plan,
                consumer=request,
            ),
            outcome,
        )
        with self.assertRaisesRegex(ContractViolation, "action_outcome_replayed"):
            self.case.authority.claim_for_composer(
                outcome,
                plan,
                consumer=request,
            )

    def test_current_question_anchor_is_required_and_exact_even_for_raw_constructor(self):
        from astrbot_plugin_shio.tests.test_reply_composer import build_request

        _turn, canonical = build_request(self.persona, "今天见到你还挺开心的")
        parameter = inspect.signature(ReplyComposerRequest).parameters[
            "current_question_anchor"
        ]
        self.assertIs(parameter.default, inspect.Parameter.empty)
        raw_values = {
            item.name: getattr(canonical, item.name)
            for item in dataclasses.fields(canonical)
            if item.name != "current_question_anchor"
        }
        with self.assertRaises(TypeError):
            ReplyComposerRequest(**raw_values)
        with self.assertRaises(ReplyComposerRequestError):
            ReplyComposerRequest(**raw_values, current_question_anchor=None)

    def test_only_vault_minted_exact_request_can_claim_without_consuming_on_rejection(self):
        import astrbot_plugin_shio.core.reply_composer as reply_composer_module
        from astrbot_plugin_shio.core.reply_composer import (
            _mint_reply_composer_request,
        )

        self.assertFalse(
            hasattr(reply_composer_module, "_build_reply_composer_request_unclaimed")
        )
        mint_parameters = inspect.signature(_mint_reply_composer_request).parameters
        self.assertEqual(
            tuple(mint_parameters),
            tuple(inspect.signature(build_reply_composer_request).parameters),
        )
        self.assertNotIn("request", mint_parameters)
        self.assertNotIn("candidate", mint_parameters)
        self.assertNotIn("snapshot", mint_parameters)

        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        values = self._composer_inputs(plan, outcome, message)
        with self.assertRaisesRegex(ReplyComposerRequestError, "reply_shape"):
            _mint_reply_composer_request(**values, reply_shape="invalid")
        self.assertIs(
            self.case.authority.inspect_outcome(
                outcome,
                current_planned_action=plan,
            ),
            outcome,
        )
        canonical = _mint_reply_composer_request(**values)
        forged_requests = (
            dataclasses.replace(canonical),
            ReplyComposerRequest(
                **{
                    item.name: getattr(canonical, item.name)
                    for item in dataclasses.fields(canonical)
                }
            ),
        )
        for forged in forged_requests:
            with self.subTest(forgery=type(forged).__name__):
                with self.assertRaisesRegex(
                    ContractViolation,
                    "action_outcome_composer_consumer_invalid",
                ):
                    self.case.authority.claim_for_composer(
                        outcome,
                        plan,
                        consumer=forged,
                    )
                self.assertIs(
                    self.case.authority.inspect_outcome(
                        outcome,
                        current_planned_action=plan,
                    ),
                    outcome,
                )
        self.assertIs(
            self.case.authority.claim_for_composer(
                outcome,
                plan,
                consumer=canonical,
            ),
            outcome,
        )

    def test_canonical_snapshot_covers_every_field_and_rejects_nested_equal_substitutes(self):
        from astrbot_plugin_shio.core.reply_composer import (
            _inspect_canonical_reply_composer_request,
            _mint_reply_composer_request,
        )

        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        request = _mint_reply_composer_request(
            **self._composer_inputs(plan, outcome, message)
        )
        snapshot = _inspect_canonical_reply_composer_request(request)
        self.assertEqual(
            tuple(name for name, _value in snapshot),
            tuple(item.name for item in dataclasses.fields(ReplyComposerRequest)),
        )

        substitutions = {
            "system_prompt": request.system_prompt + " ",
            "current_question_anchor": dataclasses.replace(
                request.current_question_anchor
            ),
            "planned_action": dataclasses.replace(request.planned_action),
            "content_seed": dataclasses.replace(request.content_seed),
            "persona_package": dataclasses.replace(request.persona_package),
            "call_budget": dataclasses.replace(request.call_budget),
        }
        for field_name, substitute in substitutions.items():
            with self.subTest(field=field_name):
                original = getattr(request, field_name)
                object.__setattr__(request, field_name, substitute)
                with self.assertRaisesRegex(
                    ContractViolation,
                    "action_outcome_composer_consumer_invalid",
                ):
                    self.case.authority.claim_for_composer(
                        outcome,
                        plan,
                        consumer=request,
                    )
                self.assertIs(
                    self.case.authority.inspect_outcome(
                        outcome,
                        current_planned_action=plan,
                    ),
                    outcome,
                )
                object.__setattr__(request, field_name, original)
                self.assertEqual(
                    _inspect_canonical_reply_composer_request(request),
                    snapshot,
                )
        self.assertIs(
            self.case.authority.claim_for_composer(
                outcome,
                plan,
                consumer=request,
            ),
            outcome,
        )

    def test_request_copy_mutation_and_outcome_substitution_fail_closed(self):
        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        other_plan, other_outcome, other_message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        copied_outcome = copy.copy(other_outcome)
        other_values = self._composer_inputs(other_plan, other_outcome, other_message)
        copied_seed = dataclasses.replace(
            other_values["content_seed"],
            action_outcome=copied_outcome,
        )
        with self.assertRaises(ReplyComposerRequestError):
            build_reply_composer_request(
                **{**other_values, "content_seed": copied_seed}
            )

        request = build_reply_composer_request(
            **self._composer_inputs(plan, outcome, message)
        )
        request_copy = dataclasses.replace(request)
        with self.assertRaisesRegex(ContractViolation, "composer_consumer_not_exact"):
            self.case.authority.inspect_for_composer(
                outcome,
                plan,
                consumer=request_copy,
            )

        object.__setattr__(request, "package_id", "mutated-persona")
        with self.assertRaisesRegex(
            ContractViolation,
            "action_outcome_(?:composer_consumer_corrupt|authority_state_corrupt)",
        ):
            self.case.authority.inspect_for_composer(
                outcome,
                plan,
                consumer=request,
            )

    def test_outcome_and_evidence_are_mutually_exclusive_before_claim(self):
        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        values = self._composer_inputs(plan, outcome, message)
        with self.assertRaisesRegex(ReplyComposerRequestError, "互斥"):
            build_reply_composer_request(
                **values,
                evidence_outcome=SimpleNamespace(),
            )
        request = build_reply_composer_request(**values)
        self.assertEqual(request.grounding_fact_count, 0)

    def test_concurrent_claim_cross_plan_and_cross_authority_fail_closed(self):
        from astrbot_plugin_shio.core.reply_composer import (
            _mint_reply_composer_request,
        )

        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        values = self._composer_inputs(plan, outcome, message)
        requests = tuple(
            _mint_reply_composer_request(**values) for _ in range(2)
        )

        def claim(request):
            try:
                self.case.authority.claim_for_composer(
                    outcome,
                    plan,
                    consumer=request,
                )
                return "claimed"
            except ContractViolation as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(claim, requests))
        self.assertEqual(results.count("claimed"), 1)
        self.assertEqual(results.count("action_outcome_replayed"), 1)
        winner = requests[results.index("claimed")]

        other_plan, _other_outcome, _other_message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        with self.assertRaisesRegex(ContractViolation, "action_outcome_plan_mismatch"):
            self.case.authority.inspect_for_composer(
                outcome,
                other_plan,
                consumer=winner,
            )

        other_case = outcome_support.ActionOutcomeTests(
            methodName=(
                "test_direct_receipt_maps_to_sealed_minimal_intent_and_consumes_once"
            )
        )
        other_case.setUp()
        with self.assertRaises(ContractViolation):
            other_case.authority.inspect_for_composer(
                outcome,
                plan,
                consumer=winner,
            )

    def test_closed_chinese_action_semantics_cover_direct_and_continuations(self):
        cases = (
            self._issue_denial(),
            self._issue_continuation(PendingDecision.CONFIRM),
            self._issue_continuation(PendingDecision.DENY),
            self._issue_continuation(PendingDecision.CANCEL),
            self._issue_confirmation_required(),
        )
        expected_kinds = (
            ActionOutcomeKind.DENIED,
            ActionOutcomeKind.SUCCEEDED_COMMITTED,
            ActionOutcomeKind.DENIED,
            ActionOutcomeKind.CANCELLED,
            ActionOutcomeKind.CONFIRMATION_REQUIRED,
        )
        for (plan, outcome, message), expected_kind in zip(cases, expected_kinds):
            with self.subTest(kind=expected_kind.value):
                request = build_reply_composer_request(
                    **self._composer_inputs(plan, outcome, message)
                )
                self.assertIs(outcome.kind, expected_kind)
                self.assertIn('"action_outcome"', request.user_prompt)
                self.assertIn('"operation_hint":"', request.user_prompt)
                self.assertIn('"status_hint":"', request.user_prompt)
                self.assertNotIn(outcome.operation.value, request.user_prompt)
                self.assertNotIn(outcome.kind.value, request.user_prompt)
                self.assertIn("不能把动作状态改写成相反事实", request.system_prompt)
                self.assertFalse(request.action_outcome.has_output)

    def test_normal_reply_remains_outcome_free(self):
        from astrbot_plugin_shio.core.reply_composer import (
            _inspect_canonical_reply_composer_request,
        )
        from astrbot_plugin_shio.tests.test_reply_composer import build_request

        _turn, request = build_request(self.persona, "今天见到你还挺开心的")
        self.assertIsInstance(
            _inspect_canonical_reply_composer_request(request),
            tuple,
        )
        self.assertIsNone(request.action_outcome)
        self.assertIsNone(request.action_outcome_authority)
        self.assertNotIn('"action_outcome"', request.user_prompt)

    def test_vault_capacity_is_bounded_and_dead_normal_requests_release_slots(self):
        from astrbot_plugin_shio.core.reply_composer import (
            _mint_reply_composer_request,
            _reply_composer_vault_metrics,
        )
        from astrbot_plugin_shio.tests.test_reply_composer import build_request

        _turn, base = build_request(self.persona, "今天见到你还挺开心的")
        values = {
            "planned_action": base.planned_action,
            "content_seed": base.content_seed,
            "expression_intent": base.expression_intent,
            "affect_appraisal": base.affect_appraisal,
            "continuous_affect": base.continuous_affect,
            "persona_expression": base.persona_expression,
            "expression_candidates": base.expression_candidates,
            "persona_package": base.persona_package,
            "capability_policy": base.capability_policy,
            "current_message": base.current_message,
            "sender_name": base.sender_name,
            "current_question_anchor": base.current_question_anchor,
            "reply_shape": base.reply_shape,
            "assembled_context": base.assembled_context,
            "media_context": base.media_context,
            "media_prompt_evidence": base.media_prompt_evidence,
            "evidence_outcome": base.evidence_outcome,
        }
        live_before, capacity, bounded = _reply_composer_vault_metrics()
        self.assertTrue(bounded)
        held = [
            _mint_reply_composer_request(**values)
            for _ in range(capacity - live_before)
        ]
        self.assertEqual(_reply_composer_vault_metrics(), (capacity, capacity, True))

        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        action_values = self._composer_inputs(plan, outcome, message)
        with self.assertRaisesRegex(ReplyComposerRequestError, "容量"):
            build_reply_composer_request(**action_values)
        self.assertIs(
            self.case.authority.inspect_outcome(
                outcome,
                current_planned_action=plan,
            ),
            outcome,
        )

        victim = held.pop()
        victim_ref = weakref.ref(victim)
        del victim
        gc.collect()
        self.assertIsNone(victim_ref())
        self.assertEqual(_reply_composer_vault_metrics()[0], capacity - 1)
        action_request = build_reply_composer_request(**action_values)
        self.assertIs(action_request.action_outcome, outcome)
        self.assertEqual(_reply_composer_vault_metrics(), (capacity, capacity, True))


if __name__ == "__main__":
    unittest.main()
