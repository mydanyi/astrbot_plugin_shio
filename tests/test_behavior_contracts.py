from __future__ import annotations

import dataclasses
import json
import unittest
from pathlib import Path

from astrbot_plugin_shio.core.capability_policy import CapabilityClass
from astrbot_plugin_shio.core.context_assembler import (
    ProvenancedFact,
    ReplyTarget,
)
from astrbot_plugin_shio.core.contracts import (
    ActionDecision,
    ActionKind,
    AddressDecision,
    AddressEvidence,
    AddressKind,
    AttentionDecision,
    AttentionLevel,
    BehaviorDecisionFrame,
    ContentIntent,
    ContentIntentKind,
    ContractViolation,
    DecisionBinding,
    EffectStatus,
    EvidenceConsumer,
    ExpressionIntent,
    ExpressionModality,
    ExternalPluginEvidence,
    HistoryVisibility,
    IngressDecision,
    IngressDisposition,
    KnowledgeGapDecision,
    KnowledgeNeed,
    MediaAvailability,
    MediaContext,
    MediaItem,
    MediaKind,
    MediaOrigin,
    MemoryDecision,
    MemoryMode,
    ModelActionSuggestion,
    OwnerActionOperation,
    ParticipationDecision,
    ParticipationLevel,
    PluginEvidenceKind,
    PluginEvidenceStatus,
    PresentationEffectKind,
    PresentationEffectReceipt,
    PresentationReceipt,
    SemanticAtom,
    SemanticAtomKind,
    SenderKind,
    parse_model_action_suggestion,
)


ROOT = Path(__file__).parents[1]


def binding(*, message_id: str = "msg-current", sender: str = "peer-a") -> DecisionBinding:
    return DecisionBinding(
        scope_key="platform:test|bot:shio|group:group-a",
        session_id="session-a",
        current_message_id=message_id,
        current_sender_key=f"platform:test|bot:shio|group:group-a|user:{sender}",
        current_content_digest="a" * 64,
        conversation_revision=3,
        generation_epoch=5,
        trace_id="b" * 32,
    )


def target(bound: DecisionBinding | None = None) -> ReplyTarget:
    bound = bound or binding()
    return ReplyTarget(
        message_id=bound.current_message_id,
        sender_key=bound.current_sender_key,
        session_id=bound.session_id,
        scope_key=bound.scope_key,
        content_digest=bound.current_content_digest,
        source_kind="current_inbound",
        referenced_message_id="",
        degradation_reasons=(),
    )


class BindingContractTests(unittest.TestCase):
    def test_binding_is_frozen_and_rejects_missing_or_unversioned_identity(self):
        value = binding()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            value.current_message_id = "changed"
        for change in (
            {"scope_key": ""},
            {"current_sender_key": ""},
            {"conversation_revision": 0},
            {"generation_epoch": 0},
            {"trace_id": "not-a-trace"},
            {"current_content_digest": "raw-content"},
        ):
            with self.subTest(change=change), self.assertRaises(ContractViolation):
                dataclasses.replace(value, **change)

    def test_binding_trace_is_content_free_and_uses_only_digests(self):
        metadata = binding().trace_metadata()
        rendered = json.dumps(metadata, ensure_ascii=False)
        self.assertNotIn("msg-current", rendered)
        self.assertNotIn("peer-a", rendered)
        self.assertNotIn("session-a", rendered)
        self.assertRegex(metadata["message_digest"], r"^[0-9a-f]{16}$")
        self.assertRegex(metadata["sender_digest"], r"^[0-9a-f]{16}$")


class IngressContractTests(unittest.TestCase):
    def test_only_accept_human_can_mutate_shio_state(self):
        accepted = IngressDecision(
            binding=binding(),
            sender_kind=SenderKind.HUMAN,
            disposition=IngressDisposition.ACCEPT_HUMAN,
        )
        self.assertTrue(accepted.allows_state_mutation)

        denied = (
            (SenderKind.HUMAN, IngressDisposition.DROP_BANNED),
            (SenderKind.SELF, IngressDisposition.DROP_SELF),
            (SenderKind.KNOWN_BOT, IngressDisposition.DROP_KNOWN_BOT),
            (SenderKind.PLUGIN_ECHO, IngressDisposition.DROP_PLUGIN_ECHO),
            (SenderKind.UNKNOWN, IngressDisposition.DEGRADED_EXTERNAL_GATE),
        )
        for sender_kind, disposition in denied:
            with self.subTest(disposition=disposition):
                decision = IngressDecision(
                    binding=binding(),
                    sender_kind=sender_kind,
                    disposition=disposition,
                    reason_codes=(disposition.value,),
                )
                self.assertFalse(decision.allows_state_mutation)

    def test_accepting_a_bot_or_mismatched_drop_shape_is_rejected(self):
        for sender_kind, disposition in (
            (SenderKind.KNOWN_BOT, IngressDisposition.ACCEPT_HUMAN),
            (SenderKind.HUMAN, IngressDisposition.DROP_SELF),
            (SenderKind.SELF, IngressDisposition.DROP_PLUGIN_ECHO),
        ):
            with self.subTest(sender_kind=sender_kind, disposition=disposition):
                with self.assertRaises(ContractViolation):
                    IngressDecision(
                        binding=binding(),
                        sender_kind=sender_kind,
                        disposition=disposition,
                        reason_codes=("invalid_shape",),
                    )

    def test_plugin_evidence_visibility_requires_trust_and_verified_interface(self):
        safe = ExternalPluginEvidence(
            binding=binding(),
            plugin_id="astrbot_plugin_livingmemory",
            evidence_kind=PluginEvidenceKind.MEMORY_REFERENCE,
            status=PluginEvidenceStatus.VERIFIED,
            history_visibility=HistoryVisibility.CURRENT_TURN_REFERENCE,
            trusted=True,
            allowed_consumers=(EvidenceConsumer.CURRENT_TURN,),
            source_event_digest="c" * 64,
        )
        self.assertTrue(safe.can_feed(EvidenceConsumer.CURRENT_TURN))
        self.assertFalse(safe.can_feed(EvidenceConsumer.PERSONA))

        for change in (
            {"trusted": False},
            {"status": PluginEvidenceStatus.TIMEOUT},
            {"status": PluginEvidenceStatus.INTERFACE_CHANGED},
        ):
            with self.subTest(change=change), self.assertRaises(ContractViolation):
                dataclasses.replace(safe, **change)

    def test_presentation_and_external_output_evidence_cannot_become_history(self):
        for kind in (
            PluginEvidenceKind.PRESENTATION_EFFECT,
            PluginEvidenceKind.EXTERNAL_OUTPUT,
            PluginEvidenceKind.GATE_DECISION,
        ):
            with self.subTest(kind=kind), self.assertRaises(ContractViolation):
                ExternalPluginEvidence(
                    binding=binding(),
                    plugin_id="synthetic_plugin",
                    evidence_kind=kind,
                    status=PluginEvidenceStatus.VERIFIED,
                    history_visibility=HistoryVisibility.PUBLIC_SCENE_REFERENCE,
                    trusted=True,
                    allowed_consumers=(EvidenceConsumer.PUBLIC_SCENE,),
                    source_event_digest="d" * 64,
                )


class BehaviorContractTests(unittest.TestCase):
    def test_private_channel_evidence_is_closed_and_address_repr_hides_ids(self):
        private_binding = binding(
            message_id="private-sensitive-message",
            sender="private-sensitive-sender",
        )
        decision = AddressDecision(
            binding=private_binding,
            kind=AddressKind.DIRECT_SELF,
            evidence=(AddressEvidence.PRIVATE_CHANNEL,),
            confidence=1.0,
            reason_codes=("private_channel_direct",),
        )

        self.assertEqual(AddressEvidence.PRIVATE_CHANNEL.value, "private_channel")
        rendered = repr(decision)
        for forbidden in (
            private_binding.scope_key,
            private_binding.session_id,
            private_binding.current_message_id,
            private_binding.current_sender_key,
        ):
            self.assertNotIn(forbidden, rendered)

    def test_address_attention_participation_and_action_stay_independent(self):
        direct = AddressDecision(
            binding=binding(),
            kind=AddressKind.DIRECT_SELF,
            evidence=(AddressEvidence.STRUCTURED_MENTION,),
            confidence=1.0,
        )
        about = AddressDecision(
            binding=binding(),
            kind=AddressKind.ABOUT_SELF,
            evidence=(AddressEvidence.SELF_REFERENCE,),
            confidence=0.8,
            is_meta_discussion=True,
        )
        self.assertNotEqual(direct.kind, about.kind)
        self.assertFalse(hasattr(direct, "must_reply"))

        for participation, action_kind in (
            (ParticipationLevel.MAY_JOIN, ActionKind.REPLY),
            (ParticipationLevel.REACT_ONLY, ActionKind.REACT),
            (ParticipationLevel.NO_ACTION, ActionKind.NO_ACTION),
        ):
            with self.subTest(participation=participation):
                action = ActionDecision(
                    binding=binding(),
                    kind=action_kind,
                    reply_target=(target() if action_kind in {ActionKind.REPLY, ActionKind.REACT} else None),
                    expression_intent=("light_reaction" if action_kind == ActionKind.REACT else ""),
                    reason_codes=("synthetic_case",),
                )
                self.assertEqual(action.kind, action_kind)

    def test_behavior_enum_fields_reject_bare_string_lookalikes(self):
        with self.assertRaisesRegex(ContractViolation, "address_kind_invalid"):
            AddressDecision(
                binding=binding(),
                kind="direct_self",
                evidence=(AddressEvidence.STRUCTURED_MENTION,),
                confidence=1.0,
            )
        with self.assertRaisesRegex(ContractViolation, "address_evidence_invalid"):
            AddressDecision(
                binding=binding(),
                kind=AddressKind.DIRECT_SELF,
                evidence=("structured_mention",),
                confidence=1.0,
            )
        with self.assertRaisesRegex(ContractViolation, "attention_level_invalid"):
            AttentionDecision(
                binding=binding(),
                level="force",
            )
        with self.assertRaisesRegex(ContractViolation, "participation_level_invalid"):
            ParticipationDecision(
                binding=binding(),
                level="must_reply",
            )
        with self.assertRaisesRegex(ContractViolation, "model_action_hint_invalid"):
            ModelActionSuggestion(
                action_hint="reply",
                confidence=1.0,
            )

    def test_action_shapes_bind_visible_actions_and_public_initiation(self):
        ActionDecision(binding=binding(), kind=ActionKind.REPLY, reply_target=target())
        ActionDecision(
            binding=binding(),
            kind=ActionKind.EXECUTE_ACTION,
            reply_target=target(),
            capability_intent=CapabilityClass.ARTIFACT_READ.value,
            operation_intent=OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        ActionDecision(
            binding=binding(),
            kind=ActionKind.USE_TOOL,
            reply_target=target(),
            capability_intent=CapabilityClass.PUBLIC_WEB_READ.value,
        )
        ActionDecision(
            binding=binding(),
            kind=ActionKind.REACT,
            reply_target=target(),
            expression_intent="friendly_ack",
        )
        ActionDecision(
            binding=binding(),
            kind=ActionKind.INITIATE,
            public_scope_key=binding().scope_key,
            expression_intent="public_topic",
        )
        for invalid in (
            {"kind": "execute_action", "reply_target": target()},
            {"kind": ActionKind.REPLY},
            {"kind": ActionKind.USE_TOOL, "reply_target": target()},
            {
                "kind": ActionKind.USE_TOOL,
                "reply_target": target(),
                "capability_intent": CapabilityClass.PUBLIC_WEB_READ.value,
                "operation_intent": OwnerActionOperation.ARTIFACT_READ_EXACT,
            },
            {
                "kind": ActionKind.EXECUTE_ACTION,
                "reply_target": target(),
                "capability_intent": CapabilityClass.ARTIFACT_READ.value,
            },
            {
                "kind": ActionKind.EXECUTE_ACTION,
                "reply_target": target(),
                "operation_intent": OwnerActionOperation.ARTIFACT_READ_EXACT,
            },
            {
                "kind": ActionKind.EXECUTE_ACTION,
                "reply_target": target(),
                "capability_intent": CapabilityClass.ARTIFACT_READ.value,
                "operation_intent": "arbitrary_tool_operation",
            },
            {
                "kind": ActionKind.EXECUTE_ACTION,
                "reply_target": target(),
                "capability_intent": CapabilityClass.ARTIFACT_READ.value,
                "operation_intent": OwnerActionOperation.ARTIFACT_READ_EXACT.value,
            },
            {
                "kind": ActionKind.REPLY,
                "reply_target": target(),
                "capability_intent": CapabilityClass.ARTIFACT_READ.value,
            },
            {
                "kind": ActionKind.REPLY,
                "reply_target": target(),
                "operation_intent": OwnerActionOperation.ARTIFACT_READ_EXACT,
            },
            {"kind": ActionKind.REACT, "reply_target": target()},
            {"kind": ActionKind.INITIATE, "reply_target": target()},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ContractViolation):
                ActionDecision(binding=binding(), **invalid)

        exact_target = target()
        forged_target = type(
            "ForgedReplyTarget",
            (),
            {
                "message_id": exact_target.message_id,
                "sender_key": exact_target.sender_key,
                "session_id": exact_target.session_id,
                "scope_key": exact_target.scope_key,
                "content_digest": exact_target.content_digest,
                "is_actionable": True,
            },
        )()
        with self.assertRaisesRegex(ContractViolation, "reply_target_binding_mismatch"):
            ActionDecision(
                binding=binding(),
                kind=ActionKind.REPLY,
                reply_target=forged_target,
            )
        with self.assertRaises(TypeError):
            class ForgedActionKind(ActionKind):
                FORGED = "forged"

    def test_model_suggestion_cannot_set_authoritative_fields(self):
        suggestion = parse_model_action_suggestion(
            {
                "action_hint": "use_tool",
                "confidence": 0.8,
                "capability_intent": "public_web_read",
                "reason_codes": ["unknown_term"],
            }
        )
        self.assertEqual(suggestion.action_hint, ActionKind.USE_TOOL)
        for protected in (
            "sender_key",
            "is_owner",
            "target_message_id",
            "capability_policy",
            "plugin_source",
            "media_source",
            "operation",
            "operation_intent",
        ):
            with self.subTest(protected=protected), self.assertRaises(ContractViolation):
                parse_model_action_suggestion(
                    {"action_hint": "reply", protected: "model-forged"}
                )

        with self.assertRaisesRegex(ContractViolation, "model_execute_action_forbidden"):
            parse_model_action_suggestion(
                {"action_hint": "execute_action", "confidence": 1.0}
            )

    def test_denied_ingress_forces_zero_behavior_frame(self):
        denied = IngressDecision(
            binding=binding(),
            sender_kind=SenderKind.HUMAN,
            disposition=IngressDisposition.DROP_BANNED,
            reason_codes=("reneban_match",),
        )
        frame = BehaviorDecisionFrame(
            ingress=denied,
            address=AddressDecision(
                binding=binding(),
                kind=AddressKind.DIRECT_SELF,
                evidence=(AddressEvidence.VOCATIVE_ALIAS,),
                confidence=0.9,
            ),
            attention=AttentionDecision(
                binding=binding(),
                level=AttentionLevel.IGNORE,
                reason_codes=("ingress_denied",),
            ),
            participation=ParticipationDecision(
                binding=binding(),
                level=ParticipationLevel.NO_ACTION,
                reason_codes=("ingress_denied",),
            ),
            action=ActionDecision(
                binding=binding(),
                kind=ActionKind.NO_ACTION,
                reason_codes=("ingress_denied",),
            ),
        )
        self.assertFalse(frame.ingress.allows_state_mutation)

        with self.assertRaises(ContractViolation):
            dataclasses.replace(
                frame,
                action=ActionDecision(
                    binding=binding(),
                    kind=ActionKind.REPLY,
                    reply_target=target(),
                ),
            )


class ContextContractTests(unittest.TestCase):
    def test_memory_decision_rejects_other_subject_and_unsafe_plugin_fact(self):
        current = ProvenancedFact(
            subject_key=binding().current_sender_key,
            scope="personal",
            content="合成的当前用户偏好",
            source_kind="livingmemory_semantic",
            source_id="fact-current",
            observed_at=1.0,
            confidence=0.9,
        )
        MemoryDecision(
            binding=binding(),
            mode=MemoryMode.SEMANTIC_RECALL,
            selected_facts=(current,),
            max_results=5,
        )
        other = dataclasses.replace(
            current,
            subject_key=binding(sender="peer-b").current_sender_key,
            source_id="fact-other",
        )
        unsafe = dataclasses.replace(
            current,
            source_kind="external_plugin_output",
            source_id="fact-plugin",
        )
        for fact in (other, unsafe):
            with self.subTest(fact=fact.source_id), self.assertRaises(ContractViolation):
                MemoryDecision(
                    binding=binding(),
                    mode=MemoryMode.SEMANTIC_RECALL,
                    selected_facts=(fact,),
                    max_results=5,
                )

    def test_memory_skip_and_knowledge_need_have_strict_budgets(self):
        MemoryDecision(binding=binding(), mode=MemoryMode.SKIP)
        with self.assertRaises(ContractViolation):
            MemoryDecision(
                binding=binding(),
                mode=MemoryMode.SKIP,
                max_results=1,
            )
        no_gap = KnowledgeGapDecision(
            binding=binding(),
            need=KnowledgeNeed.NONE,
            requires_evidence=False,
        )
        self.assertEqual(no_gap.max_tool_calls, 0)
        needs_web = KnowledgeGapDecision(
            binding=binding(),
            need=KnowledgeNeed.UNKNOWN_TERM,
            requires_evidence=True,
            requested_capability=CapabilityClass.PUBLIC_WEB_READ,
            max_tool_calls=1,
        )
        self.assertTrue(needs_web.requires_evidence)
        self.assertFalse(hasattr(needs_web, "authorized"))

    def test_media_context_binds_direct_and_quoted_sources_without_locators(self):
        direct = MediaItem(
            item_id="media-direct-1",
            kind=MediaKind.IMAGE,
            origin=MediaOrigin.DIRECT,
            provider_index=0,
            source_message_id=binding().current_message_id,
            source_sender_key=binding().current_sender_key,
            availability=MediaAvailability.RAW_MEDIA,
        )
        quoted = MediaItem(
            item_id="media-quoted-1",
            kind=MediaKind.IMAGE,
            origin=MediaOrigin.QUOTED,
            provider_index=1,
            source_message_id="msg-quoted",
            source_sender_key="platform:test|bot:shio|group:group-a|user:peer-b",
            reference_message_id="msg-quoted",
            reference_sender_key="platform:test|bot:shio|group:group-a|user:peer-b",
            availability=MediaAvailability.NATIVE_CAPTION,
            native_caption_digest="e" * 64,
        )
        context = MediaContext(binding=binding(), items=(direct, quoted))
        self.assertEqual(context.binding.current_sender_key, binding().current_sender_key)
        self.assertEqual(context.for_repair().items, context.items)
        field_names = {
            field.name
            for value in (direct, quoted, context)
            for field in dataclasses.fields(value)
        }
        for forbidden in ("url", "path", "base64", "locator"):
            self.assertNotIn(forbidden, field_names)
        rendered = json.dumps(context.trace_metadata(), ensure_ascii=False)
        self.assertNotIn("msg-quoted", rendered)
        self.assertNotIn("peer-b", rendered)

    def test_media_context_rejects_misattributed_or_duplicate_items(self):
        wrong_direct = MediaItem(
            item_id="media-direct-1",
            kind=MediaKind.IMAGE,
            origin=MediaOrigin.DIRECT,
            provider_index=0,
            source_message_id="other-message",
            source_sender_key=binding().current_sender_key,
            availability=MediaAvailability.RAW_MEDIA,
        )
        with self.assertRaises(ContractViolation):
            MediaContext(binding=binding(), items=(wrong_direct,))

    def test_content_intent_binds_target_and_traces_only_semantic_counts(self):
        intent = ContentIntent(
            binding=binding(),
            reply_target=target(),
            kind=ContentIntentKind.ANSWER,
            required_atoms=(
                SemanticAtom(SemanticAtomKind.TOPIC, "合成问题主题"),
                SemanticAtom(SemanticAtomKind.NEGATION, "保留否定范围"),
            ),
            forbidden_atoms=(
                SemanticAtom(SemanticAtomKind.FACT, "另一位用户的事实"),
            ),
            answer_language="zh-CN",
        )
        rendered = json.dumps(intent.trace_metadata(), ensure_ascii=False)
        self.assertNotIn("合成问题主题", rendered)
        self.assertNotIn("另一位用户", rendered)
        self.assertEqual(intent.reply_target.message_id, binding().current_message_id)

        with self.assertRaises(ContractViolation):
            dataclasses.replace(intent, reply_target=target(binding(message_id="msg-other")))


class PresentationContractTests(unittest.TestCase):
    def test_expression_intent_contains_no_visible_script_or_tool_arguments(self):
        expression = ExpressionIntent(
            binding=binding(),
            reply_target=target(),
            modality=ExpressionModality.TEXT_AND_MEME,
            social_act="playful_reply",
            emotion_tags=("amused",),
            max_bubbles=2,
            meme_executor="meme_manager",
            max_meme_calls=1,
        )
        fields = {field.name for field in dataclasses.fields(expression)}
        for forbidden in ("visible_text", "query", "tool_args", "principal", "is_owner"):
            self.assertNotIn(forbidden, fields)

    def test_presentation_receipt_requires_actual_success_and_single_meme_effect(self):
        text_effect = PresentationEffectReceipt(
            binding=binding(),
            effect_kind=PresentationEffectKind.TEXT,
            executor="astrbot_send",
            status=EffectStatus.SUCCEEDED,
            target_message_id=binding().current_message_id,
            attempt_count=1,
            success_count=1,
            external_receipt_digest="f" * 64,
        )
        meme_effect = PresentationEffectReceipt(
            binding=binding(),
            effect_kind=PresentationEffectKind.MEME,
            executor="meme_manager",
            status=EffectStatus.SUCCEEDED,
            target_message_id=binding().current_message_id,
            attempt_count=1,
            success_count=1,
            external_receipt_digest="1" * 64,
        )
        receipt = PresentationReceipt(
            binding=binding(),
            effects=(text_effect, meme_effect),
            terminal=True,
        )
        self.assertTrue(receipt.succeeded)
        with self.assertRaises(ContractViolation):
            PresentationEffectReceipt(
                binding=binding(),
                effect_kind=PresentationEffectKind.TEXT,
                executor="astrbot_send",
                status=EffectStatus.SUCCEEDED,
                target_message_id=binding().current_message_id,
                attempt_count=1,
                success_count=0,
            )
        with self.assertRaises(ContractViolation):
            PresentationReceipt(
                binding=binding(),
                effects=(meme_effect, dataclasses.replace(meme_effect, executor="second_meme")),
                terminal=True,
            )

    def test_contract_source_is_persona_agnostic(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "core" / "contracts").glob("*.py"))
        ).casefold()
        for forbidden in ("亚托莉", "atri", "才没有", "高性能机器人"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
