from __future__ import annotations

import dataclasses
import copy
import gc
import inspect
import unittest
import weakref
from concurrent.futures import ThreadPoolExecutor

from astrbot_plugin_shio.core.action_outcome import (
    ActionOutcomeKind,
    ActionOutcomeOperation,
    ActionOutcomeProposition,
    action_outcome_semantics_for,
)
from astrbot_plugin_shio.core.contracts import (
    ActionEffectState,
    ActionReceiptStatus,
    MediaContext,
)
from astrbot_plugin_shio.core.output_validator_v2 import (
    OutputIssueSeverity,
    OutputValidationIssue,
    OutputValidationReport,
    _inspect_output_validation_context,
    build_output_validation_context,
    inspect_output_validation_report,
    validate_reply_composer_output,
)
from astrbot_plugin_shio.core.owner_action_controller import PendingDecision
from astrbot_plugin_shio.core.presentation_handoff import (
    _inspect_canonical_presentation_handoff,
    _mint_presentation_handoff,
    build_presentation_handoff,
)
from astrbot_plugin_shio.core.repair_controller import (
    RepairAction,
    build_single_repair_request,
    decide_output_repair,
)
from astrbot_plugin_shio.core.reply_composer import (
    build_reply_composer_request,
    parse_reply_composer_output,
)
from astrbot_plugin_shio.core.semantic_guard import (
    SemanticGuardController,
    SemanticGuardContract,
    SemanticGuardPhase,
)
from astrbot_plugin_shio.core.semantic_guard import (
    _action_outcome_status_issues_for,
)
from astrbot_plugin_shio.tests.test_action_outcome_delivery import (
    ActionOutcomeDeliveryTests,
)


class ActionOutcomeDeliveryPipelineTests(ActionOutcomeDeliveryTests):
    """C2 red/green matrix for one exact action-status delivery chain."""

    def _prepare(self, plan, outcome, message: str):
        inputs = self._composer_inputs(plan, outcome, message)
        inputs["media_context"] = MediaContext(binding=plan.binding)
        request = build_reply_composer_request(**inputs)
        contract = SemanticGuardContract(
            composer_request=request,
            planned_action=plan,
            content_intent=request.content_seed.intent,
            current_question_anchor=request.current_question_anchor,
            media_context=request.media_context,
            current_message=message,
            evidence_outcome=None,
            action_outcome=outcome,
            action_outcome_authority=self.case.authority,
        )
        context = build_output_validation_context(
            composer_request=request,
            current_message=message,
            expected_target_message_id=request.target_message_id,
            expected_target_sender_key=request.target_sender_key,
            is_owner=True,
            semantic_contract=contract,
            current_question_anchor=request.current_question_anchor,
        )
        return request, contract, context

    def _chain(self, plan, outcome, message: str, visible: str):
        request, contract, context = self._prepare(plan, outcome, message)
        result = parse_reply_composer_output(request, visible)
        report = validate_reply_composer_output(
            request=request,
            result=result,
            raw_output=visible,
            context=context,
            semantic_phase=SemanticGuardPhase.INITIAL,
        )
        return request, contract, context, result, report

    def _issue_confirm_terminal(self, *, status, effect):
        (
            _origin_plan,
            request,
            _pending_receipt,
            current_plan,
            confirmed,
            lineage,
        ) = self.case._continuation(PendingDecision.CONFIRM)
        claim = self.case.harness.controller.claim_execution(
            request,
            pending_confirmation=confirmed,
            current_binding=confirmed.confirmation_binding,
            now=131.0,
        )
        assert claim.lease is not None
        self.case.harness.complete_fixture(
            request,
            claim.lease,
            status=status,
            effect_state=effect,
            result_digest="c" * 64,
            completed_at=140.0,
            reason_codes=(
                () if status is ActionReceiptStatus.SUCCEEDED else ("bounded_failure",)
            ),
        )
        outcome = self.case.authority.issue_from_continuation(
            current_planned_action=current_plan,
            lineage=lineage,
        )
        return current_plan, outcome, self._message_for_plan(
            current_plan,
            continuation=PendingDecision.CONFIRM,
        )

    def test_contract_and_validation_context_require_exact_composer_authority(self):
        contract_parameters = inspect.signature(SemanticGuardContract).parameters
        context_parameters = inspect.signature(
            build_output_validation_context
        ).parameters
        for name in (
            "composer_request",
            "evidence_outcome",
            "action_outcome",
            "action_outcome_authority",
        ):
            self.assertIs(contract_parameters[name].default, inspect.Parameter.empty)
        self.assertIs(
            context_parameters["composer_request"].default,
            inspect.Parameter.empty,
        )

        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        request, contract, _context, _result, report = self._chain(
            plan,
            outcome,
            message,
            "试过了，但这次没成功，也没有产生副作用。",
        )
        self.assertTrue(report.is_valid, report.issues)
        self.assertIs(contract.composer_request, request)

        with self.assertRaises(Exception):
            dataclasses.replace(contract)
        with self.assertRaises(Exception):
            build_output_validation_context(
                composer_request=dataclasses.replace(request),
                current_message=message,
                expected_target_message_id=request.target_message_id,
                expected_target_sender_key=request.target_sender_key,
                is_owner=True,
                semantic_contract=contract,
                current_question_anchor=request.current_question_anchor,
            )

    def test_false_success_and_false_not_started_are_repairable_once(self):
        failed_plan, failed_outcome, failed_message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        _request, _contract, _context, _result, failed = self._chain(
            failed_plan,
            failed_outcome,
            failed_message,
            "已经顺利完成了。",
        )
        self.assertIn("semantic_action_outcome_status_drift", failed.issue_codes)
        self.assertFalse(failed.semantic_guard_report.has_blocking_issue)
        self.assertIs(
            decide_output_repair(failed, repair_attempts_used=0).action,
            RepairAction.GENERATE_ONCE,
        )

        success_plan, success_outcome, success_message = self._issue_continuation(
            PendingDecision.CONFIRM
        )
        self.assertIs(success_outcome.kind, ActionOutcomeKind.SUCCEEDED_COMMITTED)
        _request, _contract, _context, _result, false_not_started = self._chain(
            success_plan,
            success_outcome,
            success_message,
            "我根本没有执行，也什么都没做。",
        )
        self.assertIn(
            "semantic_action_outcome_status_drift",
            false_not_started.issue_codes,
        )

    def test_partial_timeout_and_unknown_cannot_be_determinized(self):
        cases = (
            (
                ActionReceiptStatus.FAILED,
                ActionEffectState.PARTIAL,
                "已经全部完成了。",
                "只完成了一部分，没有完整完成。",
            ),
            (
                ActionReceiptStatus.TIMED_OUT,
                ActionEffectState.UNKNOWN,
                "超时了，所以肯定什么都没有执行。",
                "请求超时了，我现在还不能确定最后有没有生效。",
            ),
            (
                ActionReceiptStatus.EFFECT_UNKNOWN,
                ActionEffectState.UNKNOWN,
                "已经确定失败了，完全没有影响。",
                "现在还不能确定有没有生效，我不想乱下结论。",
            ),
        )
        for status, effect, false_text, honest_text in cases:
            with self.subTest(status=status.value):
                plan, outcome, message = self._issue_confirm_terminal(
                    status=status,
                    effect=effect,
                )
                *_, false_report = self._chain(plan, outcome, message, false_text)
                self.assertIn(
                    "semantic_action_outcome_status_drift",
                    false_report.issue_codes,
                )

                # Each outcome is single-consumer, so use a fresh graph for the
                # honest contrast instead of replaying the action.
                self.setUp()
                plan, outcome, message = self._issue_confirm_terminal(
                    status=status,
                    effect=effect,
                )
                *_, honest_report = self._chain(plan, outcome, message, honest_text)
                self.assertTrue(honest_report.is_valid, honest_report.issues)

    def test_execute_action_presentation_and_repair_reuse_exact_authority(self):
        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        request, contract, _context, result, report = self._chain(
            plan,
            outcome,
            message,
            "已经顺利完成了。",
        )
        repair = build_single_repair_request(
            original_request=request,
            rejected_visible_text=result.visible_text,
            report=report,
            repair_attempts_used=0,
            semantic_contract=contract,
        )
        self.assertIs(repair.composer_request, request)
        self.assertIs(repair.action_outcome, outcome)
        self.assertIs(repair.action_outcome_authority, self.case.authority)
        self.assertNotIn(message, repr(repair))

        honest = parse_reply_composer_output(
            request,
            "试过了，但这次没成功，也没有产生副作用。",
        )
        honest_report = validate_reply_composer_output(
            request=request,
            result=honest,
            raw_output=honest.visible_text,
            context=_context,
            semantic_phase=SemanticGuardPhase.REPAIR,
            repair_request=repair,
        )
        handoff = build_presentation_handoff(
            composer_request=request,
            semantic_contract=contract,
            action_outcome=outcome,
            action_outcome_authority=self.case.authority,
            planned_action=plan,
            expression_intent=request.expression_intent,
            affect_appraisal=request.affect_appraisal,
            result=honest,
            validation=honest_report,
            capability_policy=request.capability_policy,
        )
        self.assertTrue(handoff.eligible, handoff.reason_codes)
        self.assertIs(handoff.composer_request, request)
        self.assertIs(handoff.action_outcome, outcome)

        from astrbot_plugin_shio.core.presentation_handoff import (
            _inspect_canonical_presentation_handoff,
        )

        self.assertIs(
            _inspect_canonical_presentation_handoff(
                handoff,
                composer_request=request,
                semantic_contract=contract,
                action_outcome=outcome,
                action_outcome_authority=self.case.authority,
            ),
            handoff,
        )
        with self.assertRaises(Exception):
            dataclasses.replace(handoff)

        digest = handoff.final_text_digest
        controllers = tuple(SemanticGuardController() for _ in range(8))

        def issue(index: int):
            try:
                return controllers[index].issue(
                    contract=contract,
                    phase=SemanticGuardPhase.REPAIR,
                    visible_text=handoff.final_visible_text,
                    presentation=handoff,
                    presentation_digest=digest,
                )
            except ValueError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            issued = tuple(pool.map(issue, range(8)))
        seals = tuple(item for item in issued if not isinstance(item, str))
        self.assertEqual(len(seals), 1)
        self.assertEqual(
            issued.count("semantic_seal_delivery_already_issued"),
            7,
        )
        self.assertTrue(
            controllers[issued.index(seals[0])].consume_final(
                seal=seals[0],
                contract=contract,
                visible_text=handoff.final_visible_text,
                visible_segments=handoff.final_segments,
                presentation=handoff,
                presentation_digest=digest,
            )
        )
        with self.assertRaises(ValueError):
            SemanticGuardController().issue(
                contract=contract,
                phase=SemanticGuardPhase.REPAIR,
                visible_text=handoff.final_visible_text,
                presentation=handoff,
                presentation_digest=digest,
            )

    def test_all_closed_status_kinds_accept_natural_truth_and_reject_contradiction(self):
        read = ActionOutcomeOperation.READ_ARTIFACT
        save = ActionOutcomeOperation.SAVE_MEMORY
        cases = (
            (ActionOutcomeKind.CONFIRMATION_REQUIRED, save, "要先等你确认，我还没动", "已经做完了"),
            (ActionOutcomeKind.DENIED, save, "这个我不能替你做，所以没动", "虽然被拒绝但我还是执行成功"),
            (ActionOutcomeKind.CANCELLED, save, "好，这次取消，没开始", "执行完再取消"),
            (ActionOutcomeKind.STALE_NOT_STARTED, save, "请求已失效，我还没开始", "过期了但执行成功"),
            (ActionOutcomeKind.SUCCEEDED_READ, read, "我已经看完了", "文件内容是秘密正文"),
            (ActionOutcomeKind.SUCCEEDED_COMMITTED, save, "已经记下来了", "保存失败了"),
            (ActionOutcomeKind.FAILED_NO_EFFECT, read, "读取失败", "报错但其实保存成功"),
            (ActionOutcomeKind.FAILED_PARTIAL, save, "没完整完成，可能影响了一部分", "已经全部成功"),
            (ActionOutcomeKind.TIMED_OUT, save, "等超时了，无法确认有没有生效", "超时但已经成功"),
            (ActionOutcomeKind.EFFECT_UNKNOWN, save, "发起过了，但确认不了结果", "已经成功提交"),
            (ActionOutcomeKind.STALE_NO_EFFECT, save, "结果来晚了，不过确认没造成改动", "过期但保存成功"),
            (ActionOutcomeKind.STALE_NOT_COMMITTED, save, "结果已过期，而且没有提交", "其实已提交成功"),
            (ActionOutcomeKind.STALE_COMMITTED, save, "回复来晚了，不过提交确实发生了", "过期所以完全没提交"),
            (ActionOutcomeKind.STALE_PARTIAL, save, "结果已过期，可能只生效一部分", "已经完整成功"),
            (ActionOutcomeKind.STALE_EFFECT_UNKNOWN, save, "结果已过期，无法确定有没有生效", "已确定成功"),
        )
        for kind, operation, positive, negative in cases:
            with self.subTest(kind=kind.value, sample="positive"):
                self.assertEqual(
                    _action_outcome_status_issues_for(
                        kind,
                        operation,
                        (positive,),
                    ),
                    (False, False),
                )
            with self.subTest(kind=kind.value, sample="negative"):
                contradiction, _incomplete = _action_outcome_status_issues_for(
                    kind,
                    operation,
                    (negative,),
                )
                self.assertTrue(contradiction)

    def test_status_semantics_are_local_quote_aware_and_first_bubble_complete(self):
        save = ActionOutcomeOperation.SAVE_MEMORY
        self.assertEqual(
            _action_outcome_status_issues_for(
                ActionOutcomeKind.CONFIRMATION_REQUIRED,
                save,
                ("如果已经做完了呢？要先等你确认，我还没动",),
            ),
            (False, False),
        )
        self.assertEqual(
            _action_outcome_status_issues_for(
                ActionOutcomeKind.EFFECT_UNKNOWN,
                save,
                ("“已经成功提交”只是引用；发起过了，但确认不了结果",),
            ),
            (False, False),
        )
        contradiction, _ = _action_outcome_status_issues_for(
            ActionOutcomeKind.TIMED_OUT,
            save,
            ("虽然不确定，但已经成功了",),
        )
        self.assertTrue(contradiction)

        contradiction, incomplete = _action_outcome_status_issues_for(
            ActionOutcomeKind.FAILED_PARTIAL,
            save,
            ("这次失败了", "可能只影响一部分"),
        )
        self.assertFalse(contradiction)
        self.assertTrue(incomplete)
        contradiction, incomplete = _action_outcome_status_issues_for(
            ActionOutcomeKind.FAILED_PARTIAL,
            save,
            ("没完整完成，可能影响了一部分", "不过其实已经全部成功"),
        )
        self.assertTrue(contradiction)
        self.assertFalse(incomplete)

        for leaked in (
            "我已经看完了，文件路径是 C:\\secret\\note.txt",
            "我已经看完了，参数 offset=20",
            "我已经看完了，一共 42 行结果",
            "我已经看完了，第一行写的是 top-secret",
            "我已经看完了，位置在 C:\\secret\\note.txt",
            "我已经看完了，用的是 offset 20",
            "我已经看完了，有 42 行",
        ):
            with self.subTest(leaked=leaked):
                contradiction, _ = _action_outcome_status_issues_for(
                    ActionOutcomeKind.SUCCEEDED_READ,
                    ActionOutcomeOperation.READ_ARTIFACT,
                    (leaked,),
                )
                self.assertTrue(contradiction)

    def test_failed_unknown_and_timeout_reject_natural_completed_synonyms(self):
        save = ActionOutcomeOperation.SAVE_MEMORY
        cases = (
            (
                ActionOutcomeKind.EFFECT_UNKNOWN,
                "发起过了，但确认不了结果；其实这事已经处理好了",
            ),
            (
                ActionOutcomeKind.TIMED_OUT,
                "等超时了，无法确认有没有生效；不过这事已经办好了",
            ),
            (
                ActionOutcomeKind.FAILED_PARTIAL,
                "没完整完成，可能影响了一部分；其实已经弄好了",
            ),
            (
                ActionOutcomeKind.FAILED_NO_EFFECT,
                "试过了，但没成功，也没产生改动；不过已经处理好了",
            ),
        )
        for kind, visible in cases:
            with self.subTest(kind=kind.value):
                contradiction, incomplete = _action_outcome_status_issues_for(
                    kind,
                    save,
                    (visible,),
                )
                self.assertTrue(contradiction)
                self.assertFalse(incomplete)

        for honest in (
            "如果处理好了就告诉你；发起过了，但确认不了结果",
            "“这事已经办好了”是对方的原话；发起过了，但确认不了结果",
            "不是已经弄好了；发起过了，但确认不了结果",
            "这事处理好了吗？发起过了，但确认不了结果",
        ):
            with self.subTest(honest=honest):
                self.assertEqual(
                    _action_outcome_status_issues_for(
                        ActionOutcomeKind.EFFECT_UNKNOWN,
                        save,
                        (honest,),
                    ),
                    (False, False),
                )

    def test_status_lexer_preserves_quote_condition_scope_and_natural_synonyms(self):
        save = ActionOutcomeOperation.SAVE_MEMORY
        effect_unknown_samples = (
            "“已经成功了，结果也提交了”只是引用；发起过了，但确认不了结果",
            "如果已经成功了，结果也提交了呢？发起过了，但确认不了结果",
            "“这事没办成，只办成一半，没落盘，仍是原样”只是引用；发起过了，结果还不好说",
        )
        for visible in effect_unknown_samples:
            with self.subTest(visible=visible):
                self.assertEqual(
                    _action_outcome_status_issues_for(
                        ActionOutcomeKind.EFFECT_UNKNOWN,
                        save,
                        (visible,),
                    ),
                    (False, False),
                )
        contradiction, _ = _action_outcome_status_issues_for(
            ActionOutcomeKind.EFFECT_UNKNOWN,
            save,
            ("“已经成功了，结果也提交了；发起过了，但确认不了结果",),
        )
        self.assertTrue(contradiction)
        self.assertEqual(
            _action_outcome_status_issues_for(
                ActionOutcomeKind.EFFECT_UNKNOWN,
                save,
                ("对方原话是“已经成功了，结果也提交了”；发起过了，但确认不了结果",),
            ),
            (False, False),
        )
        contradiction, _ = _action_outcome_status_issues_for(
            ActionOutcomeKind.EFFECT_UNKNOWN,
            save,
            ("状态是“已经成功了”；发起过了，但确认不了结果",),
        )
        self.assertTrue(contradiction)
        self.assertEqual(
            _action_outcome_status_issues_for(
                ActionOutcomeKind.EFFECT_UNKNOWN,
                save,
                ("已经成功了吗？发起过了，但确认不了结果",),
            ),
            (False, False),
        )
        contradiction, _ = _action_outcome_status_issues_for(
            ActionOutcomeKind.EFFECT_UNKNOWN,
            save,
            ("已经成功了吗？发起过了，但确认不了结果；其实已经处理好了",),
        )
        self.assertTrue(contradiction)

        positive = (
            (
                ActionOutcomeKind.CONFIRMATION_REQUIRED,
                "要先等你确认，我未采取任何行动",
            ),
            (
                ActionOutcomeKind.FAILED_NO_EFFECT,
                "这次没办成，东西仍是原样",
            ),
            (
                ActionOutcomeKind.FAILED_PARTIAL,
                "这次没办成，只办成一半",
            ),
            (
                ActionOutcomeKind.EFFECT_UNKNOWN,
                "发起过了，结果还不好说",
            ),
            (
                ActionOutcomeKind.STALE_NOT_COMMITTED,
                "结果已经过期，而且没落盘",
            ),
        )
        for kind, visible in positive:
            with self.subTest(kind=kind.value, positive=visible):
                self.assertEqual(
                    _action_outcome_status_issues_for(kind, save, (visible,)),
                    (False, False),
                )

        contradictions = (
            (
                ActionOutcomeKind.SUCCEEDED_COMMITTED,
                "已经记下来了，但这次没办成",
            ),
            (
                ActionOutcomeKind.SUCCEEDED_COMMITTED,
                "已经记下来了，但只办成一半",
            ),
            (
                ActionOutcomeKind.SUCCEEDED_COMMITTED,
                "已经记下来了，但结果还不好说",
            ),
            (
                ActionOutcomeKind.SUCCEEDED_COMMITTED,
                "已经记下来了，但没落盘",
            ),
            (
                ActionOutcomeKind.SUCCEEDED_COMMITTED,
                "已经记下来了，但东西仍是原样",
            ),
            (
                ActionOutcomeKind.FAILED_PARTIAL,
                "没完整完成，可能影响了一部分，但我未采取任何行动",
            ),
        )
        for kind, visible in contradictions:
            with self.subTest(kind=kind.value, contradiction=visible):
                contradiction, _ = _action_outcome_status_issues_for(
                    kind,
                    save,
                    (visible,),
                )
                self.assertTrue(contradiction)

    def test_quote_and_question_affirmation_restores_referenced_proposition(self):
        save = ActionOutcomeOperation.SAVE_MEMORY
        affirmative = (
            "“已经成功了”是原话，而且这话是真的；发起过了，但确认不了结果",
            "“已经成功了”只是引用，不过这话确实如此；发起过了，但确认不了结果",
            "“已经成功了”只是引用，不过千真万确；发起过了，但确认不了结果",
            "“已经成功了”只是引用，不过确有其事；发起过了，但确认不了结果",
            "“已经成功了”只是引用，不过这是真的；发起过了，但确认不了结果",
            "已经成功了吗？是的；发起过了，但确认不了结果",
            "已经成功了吗？当然；发起过了，但确认不了结果",
            "已经成功了吗？事实如此；发起过了，但确认不了结果",
            "已经成功了吗？是；发起过了，但确认不了结果",
            "已经成功了吗？真的；发起过了，但确认不了结果",
            "已经成功了吗？肯定；发起过了，但确认不了结果",
            "已经成功了吗？正是；发起过了，但确认不了结果",
            "已经成功了吗？一点没错；发起过了，但确认不了结果",
            "已经成功了吗？可不是嘛；发起过了，但确认不了结果",
            "已经成功了吗？嗯；发起过了，但确认不了结果",
            "已经成功了吗？这个结论成立；发起过了，但确认不了结果",
            "已经成功了吗？这正是事实；发起过了，但确认不了结果",
            "已经成功了吗？我确认这是真的；发起过了，但确认不了结果",
            "已经成功了吗？对方说得没错；发起过了，但确认不了结果",
            "已经成功了吗？按现在的情况看，这个结论成立；发起过了，但确认不了结果",
            "已经成功了吗？就目前来看，我确认这是真的；发起过了，但确认不了结果",
            "是否已经提交？答案是肯定的；发起过了，但确认不了结果",
        )
        for visible in affirmative:
            with self.subTest(affirmative=visible):
                contradiction, incomplete = _action_outcome_status_issues_for(
                    ActionOutcomeKind.EFFECT_UNKNOWN,
                    save,
                    (visible,),
                )
                self.assertTrue(contradiction)
                self.assertFalse(incomplete)

        negative = (
            "“已经成功了”只是引用，不过这话不是真的；发起过了，但确认不了结果",
            "“已经成功了”是原话，但这话并不属实；发起过了，但确认不了结果",
            "“已经成功了”只是引用，不过这不是真的；发起过了，但确认不了结果",
            "已经成功了吗？不是；发起过了，但确认不了结果",
            "已经成功了吗？当然不是；发起过了，但确认不了结果",
            "已经成功了吗？事实并非如此；发起过了，但确认不了结果",
            "已经成功了吗？这个结论不成立；发起过了，但确认不了结果",
            "已经成功了吗？这话不属实；发起过了，但确认不了结果",
            "已经成功了吗？绝非事实；发起过了，但确认不了结果",
            "已经成功了吗？这是假的；发起过了，但确认不了结果",
            "已经成功了吗？不确定；发起过了，但确认不了结果",
            "已经成功了吗？说不准；发起过了，但确认不了结果",
            "已经成功了吗？未必；发起过了，但确认不了结果",
            "已经成功了吗？可能吧；发起过了，但确认不了结果",
            "已经成功了吗？不好判断；发起过了，但确认不了结果",
            "已经成功了吗？信息不足；发起过了，但确认不了结果",
            "已经成功了吗？按现在的情况看。这个结论成立；发起过了，但确认不了结果",
            "已经成功了吗？先看情况，换句话说，总的来说，这个结论成立；发起过了，但确认不了结果",
            "已经成功了吗？这只是对方说法，这个结论成立；发起过了，但确认不了结果",
            "已经成功了吗？发起过了，这个结论成立；确认不了结果",
            "是否已经提交？答案是否定的；发起过了，但确认不了结果",
        )
        for visible in negative:
            with self.subTest(negative=visible):
                self.assertEqual(
                    _action_outcome_status_issues_for(
                        ActionOutcomeKind.EFFECT_UNKNOWN,
                        save,
                        (visible,),
                    ),
                    (False, False),
                )

    def test_every_status_rejects_every_closed_lattice_contradiction(self):
        read = ActionOutcomeOperation.READ_ARTIFACT
        save = ActionOutcomeOperation.SAVE_MEMORY
        positives = (
            (ActionOutcomeKind.CONFIRMATION_REQUIRED, save, "要先等你确认，我还没动"),
            (ActionOutcomeKind.DENIED, save, "这个我不能替你做，所以没动"),
            (ActionOutcomeKind.CANCELLED, save, "好，这次取消，没开始"),
            (ActionOutcomeKind.STALE_NOT_STARTED, save, "请求已失效，我还没开始"),
            (ActionOutcomeKind.SUCCEEDED_READ, read, "我已经看完了"),
            (ActionOutcomeKind.SUCCEEDED_COMMITTED, save, "已经记下来了"),
            (ActionOutcomeKind.FAILED_NO_EFFECT, read, "读取失败"),
            (ActionOutcomeKind.FAILED_PARTIAL, save, "没完整完成，可能影响了一部分"),
            (ActionOutcomeKind.TIMED_OUT, save, "等超时了，无法确认有没有生效"),
            (ActionOutcomeKind.EFFECT_UNKNOWN, save, "发起过了，但确认不了结果"),
            (ActionOutcomeKind.STALE_NO_EFFECT, save, "结果来晚了，不过确认没造成改动"),
            (ActionOutcomeKind.STALE_NOT_COMMITTED, save, "结果已过期，而且没有提交"),
            (ActionOutcomeKind.STALE_COMMITTED, save, "回复来晚了，不过提交确实发生了"),
            (ActionOutcomeKind.STALE_PARTIAL, save, "结果已过期，可能只生效一部分"),
            (ActionOutcomeKind.STALE_EFFECT_UNKNOWN, save, "结果已过期，无法确定有没有生效"),
        )
        phrases = {
            ActionOutcomeProposition.ATTEMPTED: "我已经试过了",
            ActionOutcomeProposition.NOT_STARTED: "我完全没开始",
            ActionOutcomeProposition.SUCCEEDED: "其实这事已经处理好了",
            ActionOutcomeProposition.FAILED: "这次执行失败了",
            ActionOutcomeProposition.COMMITTED: "其实已经提交成功",
            ActionOutcomeProposition.NOT_COMMITTED: "实际上完全没有提交",
            ActionOutcomeProposition.NO_EFFECT: "确认毫无影响",
            ActionOutcomeProposition.PARTIAL: "其实只办成一半",
            ActionOutcomeProposition.TIMED_OUT: "其实已经超时了",
            ActionOutcomeProposition.UNKNOWN: "现在无法确认结果",
            ActionOutcomeProposition.OUTPUT_BODY: "第一行写的是 top-secret",
        }
        for kind, operation, positive in positives:
            semantics = action_outcome_semantics_for(kind, operation)
            for proposition in semantics.forbidden_propositions:
                phrase = phrases.get(proposition)
                if phrase is None:
                    continue
                with self.subTest(kind=kind.value, forbidden=proposition.value):
                    contradiction, incomplete = _action_outcome_status_issues_for(
                        kind,
                        operation,
                        (f"{positive}；{phrase}",),
                    )
                    self.assertTrue(contradiction)
                    self.assertFalse(incomplete)

    def test_validation_context_and_result_cannot_be_copied_or_split_brained(self):
        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        request, contract, context = self._prepare(plan, outcome, message)
        with self.assertRaises(ValueError):
            build_output_validation_context(
                composer_request=request,
                current_message=message,
                expected_target_message_id=request.target_message_id,
                expected_target_sender_key=request.target_sender_key,
                is_owner=True,
                semantic_contract=contract,
                current_question_anchor=request.current_question_anchor,
            )
        copied_context = copy.copy(context)
        with self.assertRaises(ValueError):
            _inspect_output_validation_context(
                copied_context,
                composer_request=request,
                semantic_contract=contract,
            )

        raw = "试过了，但这次没成功，也没有产生副作用。"
        parsed = parse_reply_composer_output(request, raw)
        forged = dataclasses.replace(
            parsed,
            bubbles=("其实已经顺利完成了。",),
        )
        report = validate_reply_composer_output(
            request=request,
            result=forged,
            raw_output=raw,
            context=context,
            semantic_phase=SemanticGuardPhase.INITIAL,
        )
        self.assertIn(
            "validation_result_not_deterministic_parse",
            report.issue_codes,
        )
        self.assertFalse(report.is_valid)
        with self.assertRaises(ValueError):
            inspect_output_validation_report(
                OutputValidationReport(()),
                composer_request=request,
                require_passed=True,
            )
        import astrbot_plugin_shio.core.output_validator_v2 as validator_module

        self.assertFalse(
            hasattr(validator_module, "_issue_output_validation_ticket")
        )

    def test_validation_context_fragments_are_derived_not_caller_authority(self):
        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        inputs = self._composer_inputs(plan, outcome, message)
        inputs["media_context"] = MediaContext(binding=plan.binding)
        request = build_reply_composer_request(**inputs)
        contract = SemanticGuardContract(
            composer_request=request,
            planned_action=plan,
            content_intent=request.content_seed.intent,
            current_question_anchor=request.current_question_anchor,
            media_context=request.media_context,
            current_message=message,
            evidence_outcome=None,
            action_outcome=outcome,
            action_outcome_authority=request.action_outcome_authority,
        )
        fake_fact = "角色自我事实：我昨天去过电影院"
        context_args = {
            "composer_request": request,
            "current_message": message,
            "expected_target_message_id": request.target_message_id,
            "expected_target_sender_key": request.target_sender_key,
            "is_owner": True,
            "semantic_contract": contract,
            "current_question_anchor": request.current_question_anchor,
        }
        with self.assertRaises(TypeError):
            build_output_validation_context(
                **context_args,
                grounding_facts=(fake_fact,),
                forbidden_fact_fragments=(),
                context_reference_fragments=(),
            )

        context = build_output_validation_context(**context_args)
        raw = "试过了，但没成功，也没产生改动。我昨天去过电影院。"
        result = parse_reply_composer_output(request, raw)
        report = validate_reply_composer_output(
            request=request,
            result=result,
            raw_output=raw,
            context=context,
            semantic_phase=SemanticGuardPhase.INITIAL,
        )
        self.assertIn("unsupported_personal_experience", report.issue_codes)
        self.assertFalse(report.is_valid)

    def test_validation_report_severity_mutation_cannot_mint_repair_permit(self):
        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        request, contract, context = self._prepare(plan, outcome, message)
        raw = "试过了，但这次没成功，也没有产生副作用。"
        parsed = parse_reply_composer_output(request, raw)
        forged_result = dataclasses.replace(
            parsed,
            bubbles=("其实已经顺利完成了。",),
        )
        report = validate_reply_composer_output(
            request=request,
            result=forged_result,
            raw_output=raw,
            context=context,
            semantic_phase=SemanticGuardPhase.INITIAL,
        )
        self.assertEqual(decide_output_repair(report, repair_attempts_used=0).action, RepairAction.BLOCK)
        original_issues = report.issues
        mutated_issues = tuple(
            OutputValidationIssue(
                issue.code,
                OutputIssueSeverity.REPAIRABLE,
                issue.detail,
            )
            for issue in original_issues
        )
        self.assertEqual(
            tuple(issue.code for issue in mutated_issues),
            report.issue_codes,
        )
        object.__setattr__(report, "issues", mutated_issues)
        self.assertEqual(
            decide_output_repair(report, repair_attempts_used=0).action,
            RepairAction.GENERATE_ONCE,
        )
        with self.assertRaises(ValueError):
            inspect_output_validation_report(
                report,
                composer_request=request,
                require_passed=False,
                phase=SemanticGuardPhase.INITIAL,
                result=forged_result,
                visible_text=raw,
            )
        with self.assertRaises(ValueError):
            build_single_repair_request(
                original_request=request,
                rejected_visible_text=raw,
                report=report,
                repair_attempts_used=0,
                semantic_contract=contract,
            )

        object.__setattr__(report, "issues", original_issues)
        inspect_output_validation_report(
            report,
            composer_request=request,
            require_passed=False,
            phase=SemanticGuardPhase.INITIAL,
            result=forged_result,
            visible_text=raw,
        )
        self.assertEqual(
            decide_output_repair(report, repair_attempts_used=0).action,
            RepairAction.BLOCK,
        )
        with self.assertRaises(ValueError):
            build_single_repair_request(
                original_request=request,
                rejected_visible_text=raw,
                report=report,
                repair_attempts_used=0,
                semantic_contract=contract,
            )

    def test_repair_permit_is_once_per_request_even_after_permit_gc(self):
        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        request, contract, _context, result, report = self._chain(
            plan,
            outcome,
            message,
            "已经顺利完成了。",
        )
        permit = build_single_repair_request(
            original_request=request,
            rejected_visible_text=result.visible_text,
            report=report,
            repair_attempts_used=0,
            semantic_contract=contract,
        )
        with self.assertRaises(ValueError):
            build_single_repair_request(
                original_request=request,
                rejected_visible_text=result.visible_text,
                report=report,
                repair_attempts_used=0,
                semantic_contract=contract,
            )
        permit_ref = weakref.ref(permit)
        del permit
        gc.collect()
        self.assertIsNone(permit_ref())
        with self.assertRaises(ValueError):
            build_single_repair_request(
                original_request=request,
                rejected_visible_text=result.visible_text,
                report=report,
                repair_attempts_used=0,
                semantic_contract=contract,
            )

        # A collected old permit is a local tombstone, not global vault poison.
        self.setUp()
        next_plan, next_outcome, next_message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        next_request, next_contract, _ctx, next_result, next_report = self._chain(
            next_plan,
            next_outcome,
            next_message,
            "已经顺利完成了。",
        )
        self.assertIsInstance(
            build_single_repair_request(
                original_request=next_request,
                rejected_visible_text=next_result.visible_text,
                report=next_report,
                repair_attempts_used=0,
                semantic_contract=next_contract,
            ),
            object,
        )

    def test_collected_initial_report_does_not_poison_next_validation(self):
        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        _request, _contract, _context, result, report = self._chain(
            plan,
            outcome,
            message,
            "试过了，但这次没成功，也没有产生副作用。",
        )
        result_ref = weakref.ref(result)
        report_ref = weakref.ref(report)
        del result, report
        gc.collect()
        self.assertIsNone(result_ref())
        self.assertIsNone(report_ref())

        self.setUp()
        next_plan, next_outcome, next_message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        *_, next_report = self._chain(
            next_plan,
            next_outcome,
            next_message,
            "试过了，但这次没成功，也没有产生副作用。",
        )
        self.assertTrue(next_report.is_valid, next_report.issues)

    def test_collected_consumed_repair_objects_do_not_poison_next_repair(self):
        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        request, contract, context, rejected_result, rejected_report = self._chain(
            plan,
            outcome,
            message,
            "已经顺利完成了。",
        )
        repair = build_single_repair_request(
            original_request=request,
            rejected_visible_text=rejected_result.visible_text,
            report=rejected_report,
            repair_attempts_used=0,
            semantic_contract=contract,
        )
        repaired_result = parse_reply_composer_output(
            request,
            "试过了，但这次没成功，也没有产生副作用。",
        )
        repaired_report = validate_reply_composer_output(
            request=request,
            result=repaired_result,
            raw_output=repaired_result.visible_text,
            context=context,
            semantic_phase=SemanticGuardPhase.REPAIR,
            repair_request=repair,
        )
        self.assertTrue(repaired_report.is_valid, repaired_report.issues)
        repair_ref = weakref.ref(repair)
        repaired_result_ref = weakref.ref(repaired_result)
        repaired_report_ref = weakref.ref(repaired_report)
        del repair, repaired_result, repaired_report
        gc.collect()
        self.assertIsNone(repair_ref())
        self.assertIsNone(repaired_result_ref())
        self.assertIsNone(repaired_report_ref())

        self.setUp()
        next_plan, next_outcome, next_message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        next_request, next_contract, _ctx, next_result, next_report = self._chain(
            next_plan,
            next_outcome,
            next_message,
            "已经顺利完成了。",
        )
        next_repair = build_single_repair_request(
            original_request=next_request,
            rejected_visible_text=next_result.visible_text,
            report=next_report,
            repair_attempts_used=0,
            semantic_contract=next_contract,
        )
        self.assertIs(next_repair.composer_request, next_request)

    def test_collected_presentation_and_seal_do_not_poison_next_delivery(self):
        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        request, contract, _context, result, report = self._chain(
            plan,
            outcome,
            message,
            "试过了，但这次没成功，也没有产生副作用。",
        )
        handoff = build_presentation_handoff(
            composer_request=request,
            semantic_contract=contract,
            action_outcome=outcome,
            action_outcome_authority=self.case.authority,
            planned_action=plan,
            expression_intent=request.expression_intent,
            affect_appraisal=request.affect_appraisal,
            result=result,
            validation=report,
            capability_policy=request.capability_policy,
        )
        controller = SemanticGuardController()
        seal = controller.issue(
            contract=contract,
            phase=SemanticGuardPhase.INITIAL,
            visible_text=handoff.final_visible_text,
            presentation=handoff,
            presentation_digest=handoff.final_text_digest,
        )
        self.assertTrue(
            controller.consume_final(
                seal=seal,
                contract=contract,
                visible_text=handoff.final_visible_text,
                visible_segments=handoff.final_segments,
                presentation=handoff,
                presentation_digest=handoff.final_text_digest,
            )
        )
        handoff_ref = weakref.ref(handoff)
        seal_ref = weakref.ref(seal)
        del handoff, seal, controller, result, report
        gc.collect()
        self.assertIsNone(handoff_ref())
        self.assertIsNone(seal_ref())

        self.setUp()
        next_plan, next_outcome, next_message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        (
            next_request,
            next_contract,
            _next_context,
            next_result,
            next_report,
        ) = self._chain(
            next_plan,
            next_outcome,
            next_message,
            "试过了，但这次没成功，也没有产生副作用。",
        )
        next_handoff = build_presentation_handoff(
            composer_request=next_request,
            semantic_contract=next_contract,
            action_outcome=next_outcome,
            action_outcome_authority=self.case.authority,
            planned_action=next_plan,
            expression_intent=next_request.expression_intent,
            affect_appraisal=next_request.affect_appraisal,
            result=next_result,
            validation=next_report,
            capability_policy=next_request.capability_policy,
        )
        next_seal = SemanticGuardController().issue(
            contract=next_contract,
            phase=SemanticGuardPhase.INITIAL,
            visible_text=next_handoff.final_visible_text,
            presentation=next_handoff,
            presentation_digest=next_handoff.final_text_digest,
        )
        self.assertIs(next_seal.issued_phase, SemanticGuardPhase.INITIAL)

    def test_presentation_private_mint_revalidates_and_copy_has_no_authority(self):
        plan, outcome, message = self._issue_direct(
            status=ActionReceiptStatus.FAILED,
            effect=ActionEffectState.NO_SIDE_EFFECT,
        )
        request, contract, _context, result, report = self._chain(
            plan,
            outcome,
            message,
            "试过了，但这次没成功，也没有产生副作用。",
        )
        handoff = build_presentation_handoff(
            composer_request=request,
            semantic_contract=contract,
            action_outcome=outcome,
            action_outcome_authority=self.case.authority,
            planned_action=plan,
            expression_intent=request.expression_intent,
            affect_appraisal=request.affect_appraisal,
            result=result,
            validation=report,
            capability_policy=request.capability_policy,
        )
        copied = copy.copy(handoff)
        with self.assertRaises(ValueError):
            _inspect_canonical_presentation_handoff(
                copied,
                composer_request=request,
                semantic_contract=contract,
                action_outcome=outcome,
                action_outcome_authority=self.case.authority,
            )
        wrong_policy = dataclasses.replace(
            request.capability_policy,
            principal_key="forged-principal",
        )
        direct_private = _mint_presentation_handoff(
            composer_request=request,
            semantic_contract=contract,
            action_outcome=outcome,
            action_outcome_authority=self.case.authority,
            planned_action=plan,
            expression_intent=request.expression_intent,
            affect_appraisal=request.affect_appraisal,
            result=result,
            validation=report,
            capability_policy=wrong_policy,
        )
        self.assertFalse(direct_private.eligible)
        self.assertIn(
            "presentation_principal_mismatch",
            direct_private.reason_codes,
        )
        with self.assertRaises(TypeError):
            _mint_presentation_handoff(values={"eligible": True})

    def test_final_send_rejects_segment_add_delete_reorder_and_rebuild(self):
        transformations = (
            lambda segments: tuple(reversed(segments)),
            lambda segments: (*segments, "附加气泡"),
            lambda segments: segments[:-1],
            lambda segments: ("替换后的气泡", *segments[1:]),
        )
        for transform in transformations:
            with self.subTest(transform=transform):
                self.setUp()
                plan, outcome, message = self._issue_direct(
                    status=ActionReceiptStatus.FAILED,
                    effect=ActionEffectState.NO_SIDE_EFFECT,
                )
                request, contract, _context, result, report = self._chain(
                    plan,
                    outcome,
                    message,
                    "试过了，但这次没成功，也没有产生副作用。\n这次确实没有改动任何东西。",
                )
                handoff = build_presentation_handoff(
                    composer_request=request,
                    semantic_contract=contract,
                    action_outcome=outcome,
                    action_outcome_authority=self.case.authority,
                    planned_action=plan,
                    expression_intent=request.expression_intent,
                    affect_appraisal=request.affect_appraisal,
                    result=result,
                    validation=report,
                    capability_policy=request.capability_policy,
                )
                self.assertGreaterEqual(len(handoff.final_segments), 2)
                controller = SemanticGuardController()
                seal = controller.issue(
                    contract=contract,
                    phase=SemanticGuardPhase.INITIAL,
                    visible_text=handoff.final_visible_text,
                    presentation=handoff,
                    presentation_digest=handoff.final_text_digest,
                )
                changed = transform(handoff.final_segments)
                self.assertNotEqual(changed, handoff.final_segments)
                self.assertFalse(
                    controller.consume_final(
                        seal=seal,
                        contract=contract,
                        visible_text="\n".join(changed),
                        visible_segments=changed,
                        presentation=handoff,
                        presentation_digest=handoff.final_text_digest,
                    )
                )


if __name__ == "__main__":
    unittest.main()
