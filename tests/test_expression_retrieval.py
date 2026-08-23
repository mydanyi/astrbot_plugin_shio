import unittest
from dataclasses import replace
from pathlib import Path

from astrbot_plugin_shio.core.affect import appraise_affect
from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.conversation_ledger import ledger_content_digest
from astrbot_plugin_shio.core.expression_retrieval import (
    retrieve_expression_candidates,
)
from astrbot_plugin_shio.core.identity import resolve_principal
from astrbot_plugin_shio.core.persona import load_persona_package
from astrbot_plugin_shio.core.persona_expression import (
    build_persona_expression_plan,
)


ROOT = Path(__file__).parents[1]
PERSONA_DIR = ROOT / "assets" / "personas"
SCOPE = "platform:p|bot:b|group:g"


def expression(package, message, *, owner=False, tags=()):
    sender = "owner-a" if owner else "peer-a"
    principal = resolve_principal(
        sender_id=sender,
        sender_key=f"{SCOPE}|user:{sender}",
        chat_type="group",
        owner_ids=("owner-a",),
        verification_source="astrbot_event_sender_id",
        identity_verified=True,
    )
    target = ReplyTarget(
        message_id="message-a",
        sender_key=principal.sender_key,
        session_id="g",
        scope_key=SCOPE,
        content_digest=ledger_content_digest(message),
        source_kind="current_inbound",
        referenced_message_id="",
        degradation_reasons=(),
    )
    appraisal = appraise_affect(
        principal=principal,
        reply_target=target,
        current_message=message,
    )
    plan = build_persona_expression_plan(
        package,
        appraisal,
        principal=principal,
        situational_tags=tuple(tags),
    )
    return plan


class ExpressionRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.atri = load_persona_package(PERSONA_DIR / "atri.json")
        cls.alternate = load_persona_package(PERSONA_DIR / "su_cheng.json")

    def test_no_matching_material_is_valid_empty_result(self):
        plan = expression(self.atri, "今天见到你还挺开心的")
        result = retrieve_expression_candidates(self.atri, plan)

        self.assertTrue(result.is_empty)
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.reason_codes, ("no_matching_expression_material",))

    def test_peer_praise_returns_only_matching_general_material(self):
        plan = expression(self.atri, "你今天真的很厉害")
        result = retrieve_expression_candidates(self.atri, plan)

        self.assertEqual(
            tuple(candidate.material_id for candidate in result.candidates),
            ("praise_softening",),
        )
        self.assertFalse(result.candidates[0].relationship_specific)

    def test_owner_praise_ranks_relationship_specific_material(self):
        plan = expression(self.atri, "你今天真的很厉害", owner=True)
        result = retrieve_expression_candidates(self.atri, plan)

        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.candidates[0].material_id, "primary_bond_intimacy")
        self.assertTrue(result.candidates[0].relationship_specific)
        self.assertEqual(result.candidates[1].material_id, "praise_softening")

    def test_bounded_feedback_can_reorder_matching_materials(self):
        plan = expression(self.atri, "你今天真的很厉害", owner=True)
        result = retrieve_expression_candidates(
            self.atri,
            plan,
            feedback_scores={
                "primary_bond_intimacy": -2.0,
                "praise_softening": 2.0,
            },
        )

        self.assertEqual(result.candidates[0].material_id, "praise_softening")
        self.assertGreater(result.candidates[0].score, result.candidates[1].score)

    def test_situational_tag_can_fill_but_never_exceed_three_candidates(self):
        tags = ("playful_loss",)
        plan = expression(self.atri, "急了？这一局你输了", tags=tags)
        result = retrieve_expression_candidates(
            self.atri,
            plan,
            situational_tags=tags,
            max_candidates=3,
        )

        self.assertEqual(len(result.candidates), 3)
        self.assertIn(
            "playful_retry",
            {candidate.material_id for candidate in result.candidates},
        )

    def test_recent_material_is_suppressed_and_empty_remains_legal(self):
        plan = expression(self.atri, "你今天真的很厉害")
        result = retrieve_expression_candidates(
            self.atri,
            plan,
            recent_material_ids=("praise_softening",),
        )

        self.assertTrue(result.is_empty)
        self.assertEqual(result.reason_codes, ("all_matching_materials_recent",))

    def test_caller_can_request_smaller_or_zero_candidate_budget(self):
        tags = ("playful_loss",)
        plan = expression(self.atri, "急了？这一局你输了", tags=tags)
        one = retrieve_expression_candidates(
            self.atri,
            plan,
            situational_tags=tags,
            max_candidates=1,
        )
        zero = retrieve_expression_candidates(
            self.atri,
            plan,
            situational_tags=tags,
            max_candidates=0,
        )

        self.assertEqual(len(one.candidates), 1)
        self.assertEqual(zero.candidates, ())
        self.assertEqual(zero.reason_codes, ("candidate_limit_zero",))

    def test_non_atri_package_uses_its_own_material_only(self):
        plan = expression(self.alternate, "为什么这个配置会失效？")
        result = retrieve_expression_candidates(self.alternate, plan)

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].material_id, "clear_answer")
        self.assertNotIn("高性能", result.candidates[0].instruction)

    def test_package_mismatch_or_degraded_plan_returns_empty(self):
        plan = expression(self.atri, "你今天真的很厉害")
        mismatch = retrieve_expression_candidates(self.alternate, plan)
        degraded = retrieve_expression_candidates(
            self.atri,
            replace(plan, requires_replan=True),
        )

        self.assertEqual(mismatch.reason_codes, ("persona_package_mismatch",))
        self.assertEqual(degraded.reason_codes, ("unactionable_expression_plan",))
        self.assertTrue(mismatch.is_empty)
        self.assertTrue(degraded.is_empty)

    def test_generic_retriever_has_no_role_specific_tokens(self):
        source = (ROOT / "core" / "expression_retrieval.py").read_text(
            encoding="utf-8"
        )

        for token in ("亚托莉", "ATRI", "高性能机器人", "才没有", "校准失误"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
