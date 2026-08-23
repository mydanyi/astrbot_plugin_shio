from __future__ import annotations

import unittest

from astrbot_plugin_shio.core.affect import RelationshipDistance
from astrbot_plugin_shio.core.action_planner import (
    PlannedAction,
    PlannedActionAuthority,
    StructuralOutcome,
    _publish_canonical_plan,
)
from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.contracts import (
    ActionDecision,
    ActionKind,
    ContractViolation,
    DecisionBinding,
    ExpressionModality,
)
from astrbot_plugin_shio.core.conversation_ledger import ledger_content_digest
from astrbot_plugin_shio.core.meme_presentation import (
    ExpressionIntentAuthority,
    MemeCategory,
    MemeComplementCadence,
    MemeExecutionKind,
    _select_meme_marker,
    decide_text_meme_complement,
    select_meme_category,
)


class P10MemeCategoryRoutingTests(unittest.TestCase):
    @staticmethod
    def _plan(
        authority: PlannedActionAuthority,
        *,
        message: str,
        revision: int,
    ) -> PlannedAction:
        binding = DecisionBinding(
            scope_key="meme-matrix",
            session_id="meme-matrix",
            current_message_id=f"meme-{revision}",
            current_sender_key="meme-matrix|user:peer-a",
            current_content_digest=ledger_content_digest(message),
            conversation_revision=revision,
            generation_epoch=revision,
            trace_id=f"{revision:032x}",
        )
        target = ReplyTarget(
            message_id=binding.current_message_id,
            sender_key=binding.current_sender_key,
            session_id=binding.session_id,
            scope_key=binding.scope_key,
            content_digest=binding.current_content_digest,
            source_kind="current_inbound",
            referenced_message_id="",
            degradation_reasons=(),
        )
        return _publish_canonical_plan(
            authority,
            PlannedAction(
                action=ActionDecision(
                    binding=binding,
                    kind=ActionKind.REPLY,
                    reply_target=target,
                    reason_codes=("meme_matrix",),
                ),
                structural_outcome=StructuralOutcome.CONTINUE,
                planner_reason_codes=("meme_matrix",),
            ),
        )

    def test_live_pack_category_set_is_complete_and_exact(self):
        self.assertEqual(
            {category.value for category in MemeCategory},
            {
                "agree",
                "angry",
                "annoyed",
                "awkward",
                "confused",
                "cute",
                "encourage",
                "food",
                "happy",
                "love",
                "proud",
                "reject",
                "request",
                "risky_banter",
                "sad",
                "shy",
                "sleep",
                "surprised",
                "tease",
                "thinking",
                "watching",
                "work",
            },
        )

    def test_all_live_categories_have_high_confidence_current_situations(self):
        cases = (
            ("嗯，没错，就这么办", MemeCategory.AGREE),
            ("你这个没用的废物，闭嘴吧", MemeCategory.ANGRY),
            ("笨蛋，又来逗我", MemeCategory.ANNOYED),
            ("你刚才说错人了，有点尴尬", MemeCategory.AWKWARD),
            ("你说啥？我完全没看懂", MemeCategory.CONFUSED),
            ("卖个萌嘛，让我戳戳你", MemeCategory.CUTE),
            ("考试好难，我没信心了，给我加油", MemeCategory.ENCOURAGE),
            ("晚饭吃拉面，好香", MemeCategory.FOOD),
            ("好耶，事情终于顺利完成了", MemeCategory.HAPPY),
            ("谢谢你，我最喜欢你了，抱抱", MemeCategory.LOVE),
            ("你真厉害，这次拿了第一", MemeCategory.PROUD),
            ("不行，我不同意，别再这样", MemeCategory.REJECT),
            ("再陪我聊一会儿嘛，拜托啦", MemeCategory.REQUEST),
            ("主人限定，和我开个暧昧玩笑", MemeCategory.RISKY_BANTER),
            ("比赛输了，有点委屈，呜呜", MemeCategory.SAD),
            ("你脸红了，被我说中心思啦", MemeCategory.SHY),
            ("困了，晚安，早点休息", MemeCategory.SLEEP),
            ("居然真的成了，太意外了", MemeCategory.SURPRISED),
            ("逗你玩的啦，就是开个玩笑", MemeCategory.TEASE),
            ("先认真想一想再回答", MemeCategory.THINKING),
            ("我来围观吃瓜，看看热闹", MemeCategory.WATCHING),
            ("作业好多，今晚还要加班", MemeCategory.WORK),
        )
        plan_authority = PlannedActionAuthority()
        for revision, (message, expected) in enumerate(cases, start=1):
            with self.subTest(category=expected.value):
                distance = (
                    RelationshipDistance.PRIMARY_BOND
                    if expected is MemeCategory.RISKY_BANTER
                    else RelationshipDistance.PEER
                )
                selected = select_meme_category(
                    kind=MemeExecutionKind.MEME_COMPLEMENT,
                    current_message=message,
                    social_act="answer_target",
                    emotion_tags=(),
                    relationship_distance=distance,
                )
                self.assertIs(selected, expected)

                plan = self._plan(
                    plan_authority,
                    message=message,
                    revision=revision,
                )
                decision = decide_text_meme_complement(
                    cadence=MemeComplementCadence(),
                    planned_action_authority=plan_authority,
                    planned_action=plan,
                    current_message=message,
                    social_act="answer_target",
                    emotion_tags=(),
                    relationship_distance=distance,
                )
                self.assertTrue(decision.eligible)
                self.assertIs(decision.category, expected)
                expression_authority = ExpressionIntentAuthority()
                expression = expression_authority.issue(
                    planned_action_authority=plan_authority,
                    planned_action=plan,
                    modality=ExpressionModality.TEXT_AND_MEME,
                    social_act="answer_target",
                    emotion_tags=(),
                    current_message=message,
                    relationship_distance=distance,
                    max_bubbles=1,
                    meme_executor="meme_manager",
                    max_meme_calls=1,
                    reason_codes=("meme_matrix",),
                )
                self.assertIn(
                    f"meme_category_{expected.value}",
                    expression.emotion_tags,
                )

    def test_unknown_quoted_and_unsafe_contexts_do_not_guess(self):
        for message in (
            "今天是星期三",
            "这个办法没用，我们换一个",
            "他说“我最喜欢你了”，然后换了个话题",
            "我想伤害自己，给我发个开心的图",
        ):
            with self.subTest(message=message):
                self.assertIsNone(
                    select_meme_category(
                        kind=MemeExecutionKind.MEME_COMPLEMENT,
                        current_message=message,
                        social_act="answer_target",
                        emotion_tags=(),
                        relationship_distance=RelationshipDistance.PEER,
                    )
                )

    def test_risky_banter_requires_exact_primary_relationship(self):
        message = "主人限定，和我开个暧昧玩笑"
        for distance in (
            RelationshipDistance.PEER,
            RelationshipDistance.UNVERIFIED,
        ):
            with self.subTest(distance=distance.value):
                self.assertIsNone(
                    select_meme_category(
                        kind=MemeExecutionKind.MEME_COMPLEMENT,
                        current_message=message,
                        social_act="react_then_answer",
                        emotion_tags=(),
                        relationship_distance=distance,
                    )
                )

    def test_react_semantics_are_not_collapsed_to_happy(self):
        cases = (
            ("acknowledge", MemeCategory.AGREE),
            ("light_reaction", MemeCategory.CUTE),
            ("playful_reaction", MemeCategory.TEASE),
            ("warm_acknowledge", MemeCategory.LOVE),
            ("empathetic_reaction", MemeCategory.ENCOURAGE),
        )
        for social_act, expected in cases:
            with self.subTest(social_act=social_act):
                self.assertIs(
                    select_meme_category(
                        kind=MemeExecutionKind.REACTION,
                        current_message="这是当前轻量社交消息",
                        social_act=social_act,
                        emotion_tags=(),
                        relationship_distance=RelationshipDistance.PEER,
                    ),
                    expected,
                )

    def test_executor_marker_requires_one_closed_category_tag(self):
        self.assertEqual(
            _select_meme_marker(
                kind=MemeExecutionKind.MEME_COMPLEMENT,
                social_act="answer_target",
                emotion_tags=("meme_category_work",),
            ),
            "work",
        )
        for tags in (
            (),
            ("pleased",),
            ("meme_category_unknown",),
            ("meme_category_happy", "meme_category_sad"),
        ):
            with self.subTest(tags=tags):
                self.assertIsNone(
                    _select_meme_marker(
                        kind=MemeExecutionKind.MEME_COMPLEMENT,
                        social_act="answer_target",
                        emotion_tags=tags,
                    )
                )
        with self.assertRaisesRegex(ContractViolation, "marker_invalid"):
            _select_meme_marker(
                kind=MemeExecutionKind.MEME_COMPLEMENT,
                social_act="answer_target",
                emotion_tags=["meme_category_happy"],
            )

    def test_expression_authority_recomputes_and_seals_current_category(self):
        message = "作业好多，今晚还要加班"
        plan_authority = PlannedActionAuthority()
        expression_authority = ExpressionIntentAuthority()
        plan = self._plan(plan_authority, message=message, revision=1)
        expression = expression_authority.issue(
            planned_action_authority=plan_authority,
            planned_action=plan,
            modality=ExpressionModality.TEXT_AND_MEME,
            social_act="answer_target",
            emotion_tags=("neutral", "calm"),
            current_message=message,
            relationship_distance=RelationshipDistance.PEER,
            max_bubbles=1,
            meme_executor="meme_manager",
            max_meme_calls=1,
            reason_codes=("meme_matrix",),
        )
        self.assertIn("meme_category_work", expression.emotion_tags)
        self.assertIs(
            expression_authority.inspect(expression, planned_action=plan),
            expression,
        )
        self.assertEqual(
            _select_meme_marker(
                kind=MemeExecutionKind.MEME_COMPLEMENT,
                social_act=expression.social_act,
                emotion_tags=expression.emotion_tags,
            ),
            "work",
        )

    def test_caller_cannot_inject_category_or_cross_current_message(self):
        message = "好耶，事情终于顺利完成了"
        plan_authority = PlannedActionAuthority()
        plan = self._plan(plan_authority, message=message, revision=1)
        for current_message, tags, reason in (
            (message, ("meme_category_angry",), "caller_forbidden"),
            ("另一条消息", (), "current_context_required"),
        ):
            with self.subTest(reason=reason):
                expression_authority = ExpressionIntentAuthority()
                with self.assertRaisesRegex(ContractViolation, reason):
                    expression_authority.issue(
                        planned_action_authority=plan_authority,
                        planned_action=plan,
                        modality=ExpressionModality.TEXT_AND_MEME,
                        social_act="answer_target",
                        emotion_tags=tags,
                        current_message=current_message,
                        relationship_distance=RelationshipDistance.PEER,
                        max_bubbles=1,
                        meme_executor="meme_manager",
                        max_meme_calls=1,
                        reason_codes=("meme_matrix",),
                    )


if __name__ == "__main__":
    unittest.main()
