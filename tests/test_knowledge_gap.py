from __future__ import annotations

import hashlib
import unittest

from astrbot_plugin_shio.core.content_intent_builder import (
    build_content_intent_seed,
)
from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.contracts import DecisionBinding, KnowledgeNeed
from astrbot_plugin_shio.core.current_question_anchor import (
    build_current_question_anchor,
)


def _binding(message: str, *, revision: int = 1) -> DecisionBinding:
    return DecisionBinding(
        scope_key="platform:bot:group:knowledge-gap",
        session_id="session-knowledge-gap",
        current_message_id=f"knowledge-message-{revision}",
        current_sender_key="platform:bot:group:knowledge-gap|user:peer-a",
        current_content_digest=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        conversation_revision=revision,
        generation_epoch=revision,
        trace_id=f"{revision + 50:032x}",
    )


def _seed(message: str, *, revision: int = 1):
    binding = _binding(message, revision=revision)
    target = ReplyTarget(
        message_id=binding.current_message_id,
        sender_key=binding.current_sender_key,
        session_id=binding.session_id,
        scope_key=binding.scope_key,
        content_digest=binding.current_content_digest,
        source_kind="inbound",
        referenced_message_id="",
        degradation_reasons=(),
    )
    return build_content_intent_seed(
        anchor=build_current_question_anchor(binding, message),
        reply_target=target,
    )


class KnowledgeGapTests(unittest.TestCase):
    def test_proactive_topic_uses_kb_for_stable_terms_web_for_current_facts_only(self):
        from astrbot_plugin_shio.core.capability_policy import CapabilityClass
        from astrbot_plugin_shio.core.knowledge_gap import (
            decide_proactive_knowledge_need,
        )

        self.assertIs(
            decide_proactive_knowledge_need(
                "仿生人这个网络黑话是什么意思？"
            ).capability,
            CapabilityClass.CHAT_RETRIEVAL,
        )
        self.assertIs(
            decide_proactive_knowledge_need(
                "现在晚饭价格多少？"
            ).capability,
            CapabilityClass.PUBLIC_WEB_READ,
        )
        self.assertIsNone(
            decide_proactive_knowledge_need("今晚的晚饭好香").capability
        )

    def test_common_and_emotional_chat_do_not_search(self):
        from astrbot_plugin_shio.core.knowledge_gap import decide_knowledge_gap

        for message in (
            "一加一等于几？",
            "我今天真的很难过，你陪我聊一会儿吧。",
            "我现在很难过，该怎么办？",
            "现在这个项目应该怎么修？",
            "Python 里的 list 是什么？",
        ):
            with self.subTest(message=message):
                decision = decide_knowledge_gap(
                    content_seed=_seed(message),
                    current_message=message,
                )
                self.assertIs(decision.need, KnowledgeNeed.NONE)
                self.assertFalse(decision.requires_evidence)
                self.assertEqual(decision.max_tool_calls, 0)

    def test_embedded_lookup_substring_in_local_check_does_not_search(self):
        from astrbot_plugin_shio.core.knowledge_gap import decide_knowledge_gap

        for message in (
            "醒醒起床，让我检查一下身体，看看有没有修好",
            "你排查一下插件有没有修好",
            "我先自查一下状态，不要联网",
        ):
            with self.subTest(message=message):
                decision = decide_knowledge_gap(
                    content_seed=_seed(message),
                    current_message=message,
                )
                self.assertIs(decision.need, KnowledgeNeed.NONE)
                self.assertFalse(decision.requires_evidence)

    def test_explicit_lookup_boundaries_still_request_evidence(self):
        from astrbot_plugin_shio.core.knowledge_gap import decide_knowledge_gap

        for message in (
            "查一下北京今天的天气",
            "帮我查一下这个公开型号的发布时间",
            "请联网核实这条公开消息",
        ):
            with self.subTest(message=message):
                decision = decide_knowledge_gap(
                    content_seed=_seed(message),
                    current_message=message,
                )
                self.assertIs(decision.need, KnowledgeNeed.EXPLICIT_VERIFY)
                self.assertTrue(decision.requires_evidence)

    def test_unknown_slang_requests_one_native_knowledge_base_read(self):
        from astrbot_plugin_shio.core.capability_policy import CapabilityClass
        from astrbot_plugin_shio.core.knowledge_gap import decide_knowledge_gap

        message = "“city不city”是什么新梗？"
        decision = decide_knowledge_gap(
            content_seed=_seed(message),
            current_message=message,
        )

        self.assertIs(decision.need, KnowledgeNeed.UNKNOWN_TERM)
        self.assertIs(decision.requested_capability, CapabilityClass.CHAT_RETRIEVAL)
        self.assertEqual(decision.max_tool_calls, 1)

    def test_natural_do_you_know_question_requests_native_knowledge_base(self):
        from astrbot_plugin_shio.core.capability_policy import CapabilityClass
        from astrbot_plugin_shio.core.knowledge_gap import decide_knowledge_gap

        message = "亚托莉 你知道华强买瓜吗？"
        decision = decide_knowledge_gap(
            content_seed=_seed(message),
            current_message=message,
        )

        self.assertIs(decision.need, KnowledgeNeed.UNKNOWN_TERM)
        self.assertIs(decision.requested_capability, CapabilityClass.CHAT_RETRIEVAL)
        self.assertEqual(decision.max_tool_calls, 1)
        self.assertIn("knowledge_base_question_present", decision.reason_codes)

    def test_time_sensitive_fact_requests_one_public_read(self):
        from astrbot_plugin_shio.core.knowledge_gap import decide_knowledge_gap

        for message in (
            "今天港币兑人民币的实时汇率是多少？",
            "现在这个项目的最新稳定版本是什么？",
        ):
            with self.subTest(message=message):
                decision = decide_knowledge_gap(
                    content_seed=_seed(message),
                    current_message=message,
                )
                self.assertIs(decision.need, KnowledgeNeed.TIME_SENSITIVE)
                self.assertEqual(decision.max_tool_calls, 1)

    def test_explicit_verification_is_hard_evidence_need(self):
        from astrbot_plugin_shio.core.knowledge_gap import (
            KnowledgeGapSuggestion,
            decide_knowledge_gap,
        )

        message = "请联网查证这条消息是否真实，并给出来源。"
        decision = decide_knowledge_gap(
            content_seed=_seed(message),
            current_message=message,
            suggestion=KnowledgeGapSuggestion(
                need=KnowledgeNeed.NONE,
                confidence=1.0,
                reason_codes=("soft_no_gap",),
            ),
        )

        self.assertIs(decision.need, KnowledgeNeed.EXPLICIT_VERIFY)
        self.assertTrue(decision.requires_evidence)
        self.assertIn("explicit_verification_requested", decision.reason_codes)

    def test_explicit_no_search_overrides_time_or_verify_words(self):
        from astrbot_plugin_shio.core.knowledge_gap import decide_knowledge_gap

        message = "不要联网搜索，也不用查证最新资料，只说说你的看法。"
        decision = decide_knowledge_gap(
            content_seed=_seed(message),
            current_message=message,
        )

        self.assertIs(decision.need, KnowledgeNeed.NONE)
        self.assertIn("external_evidence_declined", decision.reason_codes)

    def test_conflicting_evidence_is_code_owned_hard_need(self):
        from astrbot_plugin_shio.core.knowledge_gap import decide_knowledge_gap

        message = "这两个说法哪个是对的？"
        decision = decide_knowledge_gap(
            content_seed=_seed(message),
            current_message=message,
            conflicting_evidence=True,
        )

        self.assertIs(decision.need, KnowledgeNeed.CONFLICTING_EVIDENCE)
        self.assertEqual(decision.max_tool_calls, 1)

    def test_soft_suggestion_can_only_raise_unknown_term(self):
        from astrbot_plugin_shio.core.knowledge_gap import (
            KnowledgeGapSuggestion,
            decide_knowledge_gap,
        )

        message = "朋友说这是 florp，我没看懂。"
        decision = decide_knowledge_gap(
            content_seed=_seed(message),
            current_message=message,
            suggestion=KnowledgeGapSuggestion(
                need=KnowledgeNeed.UNKNOWN_TERM,
                confidence=0.91,
                reason_codes=("ambiguous_term_detected",),
            ),
        )
        self.assertIs(decision.need, KnowledgeNeed.UNKNOWN_TERM)

        with self.assertRaises(ValueError):
            KnowledgeGapSuggestion(
                need=KnowledgeNeed.TIME_SENSITIVE,
                confidence=1.0,
            )

    def test_digest_mismatch_and_trace_leak_fail_closed(self):
        from astrbot_plugin_shio.core.knowledge_gap import (
            KnowledgeGapError,
            decide_knowledge_gap,
        )

        secret = "请查证不能泄漏的真实问题"
        seed = _seed(secret)
        with self.assertRaises(KnowledgeGapError):
            decide_knowledge_gap(
                content_seed=seed,
                current_message="被替换的问题",
            )

        decision = decide_knowledge_gap(
            content_seed=seed,
            current_message=secret,
        )
        rendered = repr(decision.trace_metadata())
        self.assertNotIn(secret, rendered)
        self.assertNotIn(seed.binding.current_message_id, rendered)
        self.assertNotIn(seed.binding.current_sender_key, rendered)


if __name__ == "__main__":
    unittest.main()
