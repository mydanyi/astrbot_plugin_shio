from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import unittest

from astrbot_plugin_shio.core.contracts import DecisionBinding, SemanticAtomKind
from astrbot_plugin_shio.core.current_question_anchor import (
    CurrentTurnKind,
    build_current_question_anchor,
)


def binding(message: str) -> DecisionBinding:
    return DecisionBinding(
        scope_key="platform:test|bot:shio|group:synthetic",
        session_id="session-synthetic",
        current_message_id="message-synthetic",
        current_sender_key="scope-synthetic|user:peer-synthetic",
        current_content_digest=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        conversation_revision=1,
        generation_epoch=1,
        trace_id="a" * 32,
    )


class CurrentQuestionAnchorTests(unittest.TestCase):
    def test_anchor_is_frozen_digest_bound_and_trace_contains_no_content(self):
        message = "请解释量子纠缠是什么意思？"
        anchor = build_current_question_anchor(binding(message), message)

        self.assertFalse(hasattr(anchor, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            anchor.answer_language = "en"
        rendered = json.dumps(anchor.trace_metadata(), ensure_ascii=False)
        self.assertNotIn(message, rendered)
        self.assertNotIn("量子纠缠", rendered)
        self.assertNotIn(binding(message).current_sender_key, rendered)

    def test_digest_mismatch_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "anchor_content_binding_mismatch"):
            build_current_question_anchor(binding("问题甲"), "问题乙")

    def test_question_request_and_plain_statement_are_distinct(self):
        question = "这个设置为什么失效了？"
        request = "帮我查一下这个术语的意思"
        statement = "今天群里很热闹"

        self.assertIs(
            build_current_question_anchor(binding(question), question).turn_kind,
            CurrentTurnKind.QUESTION,
        )
        self.assertIs(
            build_current_question_anchor(binding(request), request).turn_kind,
            CurrentTurnKind.REQUEST,
        )
        self.assertIs(
            build_current_question_anchor(binding(statement), statement).turn_kind,
            CurrentTurnKind.STATEMENT,
        )

    def test_negation_action_entity_and_parallel_questions_become_atoms(self):
        message = "不要解释旧版本；请比较 ATRI 和新版插件，为什么还会串人？"
        anchor = build_current_question_anchor(binding(message), message)
        atoms = {(atom.kind, atom.value) for atom in anchor.semantic_atoms}

        self.assertIn((SemanticAtomKind.NEGATION, "不要"), atoms)
        self.assertIn((SemanticAtomKind.ACTION, "比较"), atoms)
        self.assertIn((SemanticAtomKind.ENTITY, "ATRI"), atoms)
        self.assertGreaterEqual(anchor.question_count, 1)
        self.assertGreaterEqual(anchor.clause_count, 2)

    def test_media_reference_is_bound_to_typed_item_ids(self):
        message = "看看我引用的这张图片，里面为什么报错？"
        anchor = build_current_question_anchor(
            binding(message),
            message,
            media_item_ids=("quoted-image-1",),
        )

        self.assertEqual(anchor.media_item_ids, ("quoted-image-1",))
        self.assertTrue(
            any(atom.kind is SemanticAtomKind.MEDIA_REFERENCE for atom in anchor.semantic_atoms)
        )

    def test_explicit_language_request_is_preserved_but_default_is_chinese(self):
        english = "Please answer this one in English: what is ATRI?"
        chinese = "ATRI 是什么意思？"

        self.assertEqual(
            build_current_question_anchor(binding(english), english).answer_language,
            "en",
        )
        self.assertEqual(
            build_current_question_anchor(binding(chinese), chinese).answer_language,
            "zh-CN",
        )

    def test_negated_or_meta_english_mentions_keep_default_chinese(self):
        samples = (
            "不要用英文回答，请正常解释 ATRI。",
            "别用英语回复。",
            "为什么用英文回答？我刚才明明在说中文。",
            "Why did you answer in English?",
        )

        for message in samples:
            with self.subTest(message=message):
                self.assertEqual(
                    build_current_question_anchor(
                        binding(message),
                        message,
                    ).answer_language,
                    "zh-CN",
                )

    def test_only_affirmative_explicit_english_requests_switch_language(self):
        samples = (
            "请用英文回答：ATRI 是什么？",
            "Please answer this one in English: what is ATRI?",
            "Could you reply in English?",
        )

        for message in samples:
            with self.subTest(message=message):
                self.assertEqual(
                    build_current_question_anchor(
                        binding(message),
                        message,
                    ).answer_language,
                    "en",
                )

    def test_prior_memory_topic_cannot_replace_current_anchor(self):
        current = "为什么 Docker 容器没有启动？"
        anchor = build_current_question_anchor(binding(current), current)

        wrong = anchor.coverage("蛋糕需要先把鸡蛋和面粉搅拌均匀。")
        relevant = anchor.coverage("Docker 容器没启动，先看容器状态和启动日志。")

        self.assertEqual(wrong.matched_atom_count, 0)
        self.assertGreater(relevant.matched_atom_count, wrong.matched_atom_count)
        self.assertFalse(wrong.current_topic_supported)
        self.assertTrue(relevant.current_topic_supported)

    def test_builder_has_no_history_or_memory_override_parameter(self):
        parameters = inspect.signature(build_current_question_anchor).parameters

        self.assertNotIn("history", parameters)
        self.assertNotIn("memory", parameters)
        self.assertNotIn("facts", parameters)


if __name__ == "__main__":
    unittest.main()
