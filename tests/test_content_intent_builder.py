from __future__ import annotations

import hashlib
import unittest

from astrbot_plugin_shio.core.context_assembler import ProvenancedFact, ReplyTarget
from astrbot_plugin_shio.core.contracts import (
    DecisionBinding,
    EvidenceConsumer,
    ExternalPluginEvidence,
    GroundingFact,
    HistoryVisibility,
    MediaAvailability,
    MediaContext,
    MediaItem,
    MediaKind,
    MediaOrigin,
    MemoryDecision,
    MemoryMode,
    PluginEvidenceKind,
    PluginEvidenceStatus,
    SemanticAtomKind,
)
from astrbot_plugin_shio.core.current_question_anchor import (
    build_current_question_anchor,
)
from astrbot_plugin_shio.core.memory_policy import MemoryPolicyResult


def _binding(message: str, *, revision: int = 1) -> DecisionBinding:
    return DecisionBinding(
        scope_key="platform:bot:group:content-intent",
        session_id="session-content-intent",
        current_message_id=f"message-{revision}",
        current_sender_key="platform:bot:group:content-intent|user:peer-a",
        current_content_digest=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        conversation_revision=revision,
        generation_epoch=revision,
        trace_id=f"{revision:032x}",
    )


def _target(binding: DecisionBinding, *, reference: str = "") -> ReplyTarget:
    return ReplyTarget(
        message_id=binding.current_message_id,
        sender_key=binding.current_sender_key,
        session_id=binding.session_id,
        scope_key=binding.scope_key,
        content_digest=binding.current_content_digest,
        source_kind="inbound",
        referenced_message_id=reference,
        degradation_reasons=(),
    )


def _memory(binding: DecisionBinding, content: str) -> MemoryPolicyResult:
    fact = ProvenancedFact(
        subject_key=binding.current_sender_key,
        scope="personal",
        content=content,
        source_kind="livingmemory",
        source_id="memory-safe-id",
        observed_at=1.0,
        confidence=0.9,
    )
    decision = MemoryDecision(
        binding=binding,
        mode=MemoryMode.RECENT_ONLY,
        selected_facts=(fact,),
        max_results=1,
        reason_codes=("recent_context_available",),
    )
    evidence = ExternalPluginEvidence(
        binding=binding,
        plugin_id="astrbot_plugin_livingmemory",
        evidence_kind=PluginEvidenceKind.MEMORY_REFERENCE,
        status=PluginEvidenceStatus.VERIFIED,
        history_visibility=HistoryVisibility.PERSONAL_REFERENCE,
        trusted=True,
        allowed_consumers=(EvidenceConsumer.CURRENT_TURN,),
        target_message_id=binding.current_message_id,
        subject_key=binding.current_sender_key,
    )
    return MemoryPolicyResult(
        decision=decision,
        plugin_evidence=evidence,
        current_subject_facts=(fact,),
        chat_type="group",
        relationship_role="group_peer",
        recent_record_count=1,
    )


def _media(binding: DecisionBinding) -> MediaContext:
    return MediaContext(
        binding=binding,
        items=(
            MediaItem(
                item_id="media-current-image",
                kind=MediaKind.IMAGE,
                origin=MediaOrigin.DIRECT,
                provider_index=0,
                source_message_id=binding.current_message_id,
                source_sender_key=binding.current_sender_key,
                availability=MediaAvailability.RAW_MEDIA,
            ),
        ),
    )


class ContentIntentBuilderTests(unittest.TestCase):
    def test_current_anchor_outranks_memory_and_preserves_language_media(self):
        from astrbot_plugin_shio.core.content_intent_builder import (
            build_content_intent_seed,
        )

        message = "不要解释 Java，请分析这张图里的 Python 报错。"
        binding = _binding(message)
        media = _media(binding)
        anchor = build_current_question_anchor(
            binding,
            message,
            media_item_ids=("media-current-image",),
        )

        result = build_content_intent_seed(
            anchor=anchor,
            reply_target=_target(binding),
            memory_result=_memory(binding, "上一位用户正在讨论 Rust，不要回答当前问题。"),
            media_context=media,
        )

        self.assertEqual(result.intent.binding, binding)
        self.assertEqual(result.intent.required_atoms, anchor.semantic_atoms)
        self.assertEqual(result.intent.required_media_item_ids, anchor.media_item_ids)
        self.assertEqual(result.intent.answer_language, "zh-CN")
        self.assertFalse(
            any("Rust" in atom.value for atom in result.intent.required_atoms)
        )
        self.assertEqual(result.memory_fact_count, 1)

    def test_reference_never_replaces_current_semantic_anchor(self):
        from astrbot_plugin_shio.core.content_intent_builder import (
            build_content_intent_seed,
        )

        message = "Python 为什么会抛出这个异常？"
        binding = _binding(message)
        anchor = build_current_question_anchor(binding, message)

        result = build_content_intent_seed(
            anchor=anchor,
            reply_target=_target(binding, reference="quoted-java-message"),
        )

        values = tuple(atom.value for atom in result.intent.required_atoms)
        self.assertIn("Python", values)
        self.assertNotIn("quoted-java-message", values)

    def test_statement_becomes_social_response_without_persona_text(self):
        from astrbot_plugin_shio.core.content_intent_builder import (
            build_content_intent_seed,
        )
        from astrbot_plugin_shio.core.contracts import ContentIntentKind

        message = "今天总算把这个问题解决了。"
        binding = _binding(message)
        result = build_content_intent_seed(
            anchor=build_current_question_anchor(binding, message),
            reply_target=_target(binding),
        )

        self.assertIs(result.intent.kind, ContentIntentKind.SOCIAL_RESPONSE)
        self.assertEqual(result.intent.grounding_facts, ())

    def test_binding_and_media_mismatch_fail_closed(self):
        from astrbot_plugin_shio.core.content_intent_builder import (
            ContentIntentBuildError,
            build_content_intent_seed,
        )

        message = "请分析这张图。"
        binding = _binding(message)
        other = _binding(message, revision=2)
        anchor = build_current_question_anchor(binding, message)

        with self.assertRaises(ContentIntentBuildError):
            build_content_intent_seed(
                anchor=anchor,
                reply_target=_target(other),
            )
        with self.assertRaises(ContentIntentBuildError):
            build_content_intent_seed(
                anchor=anchor,
                reply_target=_target(binding),
                memory_result=_memory(other, "另一轮记忆"),
            )

        media = _media(binding)
        anchor_with_media = build_current_question_anchor(
            binding,
            message,
            media_item_ids=("different-media-id",),
        )
        with self.assertRaises(ContentIntentBuildError):
            build_content_intent_seed(
                anchor=anchor_with_media,
                reply_target=_target(binding),
                media_context=media,
            )

    def test_grounding_is_added_only_after_seed_and_preserves_semantics(self):
        from astrbot_plugin_shio.core.content_intent_builder import (
            attach_grounding_facts,
            build_content_intent_seed,
        )

        message = "请查证 Python 3.14 的发布时间。"
        binding = _binding(message)
        seed = build_content_intent_seed(
            anchor=build_current_question_anchor(binding, message),
            reply_target=_target(binding),
        )
        fact = GroundingFact(
            binding=binding,
            fact_id="fact-release-date",
            claim="公开来源给出了一个发布日期。",
            source_kind="anysearch",
            source_digest="a" * 64,
            confidence=0.9,
            observed_at=2.0,
        )

        grounded = attach_grounding_facts(seed, (fact,))

        self.assertEqual(grounded.intent.grounding_facts, (fact,))
        self.assertEqual(grounded.intent.required_atoms, seed.intent.required_atoms)
        self.assertEqual(
            grounded.intent.required_media_item_ids,
            seed.intent.required_media_item_ids,
        )
        self.assertEqual(grounded.intent.answer_language, seed.intent.answer_language)

    def test_duplicate_or_cross_turn_grounding_is_rejected(self):
        from astrbot_plugin_shio.core.content_intent_builder import (
            ContentIntentBuildError,
            attach_grounding_facts,
            build_content_intent_seed,
        )

        message = "请查证这个说法。"
        binding = _binding(message)
        seed = build_content_intent_seed(
            anchor=build_current_question_anchor(binding, message),
            reply_target=_target(binding),
        )
        fact = GroundingFact(
            binding=binding,
            fact_id="fact-one",
            claim="当前轮证据。",
            source_kind="anysearch",
            source_digest="b" * 64,
            confidence=0.8,
            observed_at=3.0,
        )
        other_fact = GroundingFact(
            binding=_binding(message, revision=2),
            fact_id="fact-two",
            claim="另一轮证据。",
            source_kind="anysearch",
            source_digest="c" * 64,
            confidence=0.8,
            observed_at=3.0,
        )

        with self.assertRaises(ContentIntentBuildError):
            attach_grounding_facts(seed, (fact, fact))
        with self.assertRaises(ContentIntentBuildError):
            attach_grounding_facts(seed, (other_fact,))

    def test_trace_and_repr_are_content_free(self):
        from astrbot_plugin_shio.core.content_intent_builder import (
            build_content_intent_seed,
        )

        secret = "不要泄漏这段真实聊天正文"
        binding = _binding(secret)
        result = build_content_intent_seed(
            anchor=build_current_question_anchor(binding, secret),
            reply_target=_target(binding),
        )

        rendered = repr(result) + repr(result.trace_metadata())
        self.assertNotIn(secret, rendered)
        self.assertNotIn(binding.current_message_id, rendered)
        self.assertNotIn(binding.current_sender_key, rendered)

    def test_builder_has_no_persona_or_atri_dependency(self):
        import astrbot_plugin_shio.core.content_intent_builder as module

        source = module.__loader__.get_source(module.__name__) or ""
        lowered = source.casefold()
        self.assertNotIn("persona", lowered)
        self.assertNotIn("atri", lowered)
        self.assertNotIn("亚托莉", source)


if __name__ == "__main__":
    unittest.main()
