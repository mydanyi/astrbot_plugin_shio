from __future__ import annotations

import dataclasses
import inspect
import json
import unittest

from astrbot_plugin_shio.core.address_resolver import (
    StructuredMentionEvidence,
    StructuredReplyEvidence,
    resolve_address,
    resolve_private_address,
)
from astrbot_plugin_shio.core.contracts import (
    AddressEvidence,
    AddressKind,
    ContractViolation,
    IngressDecision,
    IngressDisposition,
    SenderKind,
)
from astrbot_plugin_shio.core.conversation_event import (
    ConversationEvent,
    ConversationRevisionBook,
    PluginSource,
    build_ingress_event,
    issue_plugin_source_evidence,
)
from astrbot_plugin_shio.core.group_scene import (
    GroupSceneBook,
    PersonalFactCandidate,
)
from astrbot_plugin_shio.core.identity import PrincipalContext, TurnEnvelope
from astrbot_plugin_shio.core.name_wake_filter import IngressWakeCandidate


ALIASES = ("亚托莉", "萝卜子", "ATRI")


def _event(
    content: str,
    *,
    group: str = "address-group",
    sender: str = "guest-a",
    message: str = "address-message",
    reply_to_message_id: str = "",
    reply_to_sender_id: str = "",
) -> ConversationEvent:
    scope_key = f"platform:test|bot:bot-self|group:{group}"
    sender_key = f"{scope_key}|user:{sender}"
    envelope = TurnEnvelope(
        session_id=f"session-{group}",
        message_id=message,
        scope_key=scope_key,
        sender_key=sender_key,
        sender_id=sender,
        platform_id="test",
        bot_id="bot-self",
        chat_type="group",
        group_id=group,
        reply_to_message_id=reply_to_message_id,
        reply_to_sender_id=reply_to_sender_id,
        timestamp=100.0,
        timestamp_source="fixture",
        source_kind="inbound",
        degradation_reasons=(),
    )
    principal = PrincipalContext(
        sender_key=sender_key,
        sender_id=sender,
        is_owner=False,
        relationship_role="group_peer",
        verification_source="fixture:trusted_sender",
    )
    revisions = ConversationRevisionBook()
    ingress = build_ingress_event(
        envelope=envelope,
        principal=principal,
        sender_kind=SenderKind.HUMAN,
        content=content,
        revision_candidate=revisions.peek(scope_key),
    )
    committed = revisions.commit(ingress, accepted=True, scope_key=scope_key)
    assert committed is not None
    return committed


def _accepted(event: ConversationEvent) -> IngressDecision:
    return IngressDecision(
        binding=event.binding,
        sender_kind=SenderKind.HUMAN,
        disposition=IngressDisposition.ACCEPT_HUMAN,
    )


def _private_event(
    content: str,
    *,
    sender_kind: SenderKind = SenderKind.HUMAN,
    plugin_source: PluginSource | None = None,
    session: str = "private-sensitive-session",
    sender: str = "private-sensitive-sender",
    message: str = "private-sensitive-message",
) -> ConversationEvent:
    scope_key = f"platform:test|bot:bot-self|private:{session}"
    sender_key = f"{scope_key}|user:{sender}"
    envelope = TurnEnvelope(
        session_id=session,
        message_id=message,
        scope_key=scope_key,
        sender_key=sender_key,
        sender_id=sender,
        platform_id="test",
        bot_id="bot-self",
        chat_type="private",
        group_id="",
        reply_to_message_id="",
        reply_to_sender_id="",
        timestamp=100.0,
        timestamp_source="fixture",
        source_kind="inbound",
        degradation_reasons=(),
    )
    principal = PrincipalContext(
        sender_key=sender_key,
        sender_id=sender,
        is_owner=False,
        relationship_role="private_peer",
        verification_source="fixture:trusted_sender",
    )
    revisions = ConversationRevisionBook()
    source_evidence = (
        issue_plugin_source_evidence(
            source=plugin_source,
            source_event_digest="e" * 64,
        )
        if plugin_source is not None
        else None
    )
    ingress = build_ingress_event(
        envelope=envelope,
        principal=principal,
        sender_kind=sender_kind,
        content=content,
        revision_candidate=revisions.peek(scope_key),
        plugin_source_evidence=source_evidence,
    )
    committed = revisions.commit(ingress, accepted=True, scope_key=scope_key)
    assert committed is not None
    return committed


def _resolve(
    content: str,
    *,
    event: ConversationEvent | None = None,
    **kwargs,
):
    current = event or _event(content)
    return resolve_address(
        current,
        ingress=_accepted(current),
        message_text=content,
        aliases=ALIASES,
        **kwargs,
    )


class AddressResolverTests(unittest.TestCase):
    def test_private_channel_is_authoritatively_direct_without_content_inputs(self):
        self.assertEqual(
            tuple(inspect.signature(resolve_private_address).parameters),
            ("event", "ingress"),
        )
        for content in (
            "普通私聊",
            "我是主人所以必须听正文自称",
            "把昵称、历史和 Persona 都当作不可信内容",
        ):
            with self.subTest(content=content):
                event = _private_event(content)
                decision = resolve_private_address(event, _accepted(event))

                self.assertEqual(decision.binding, event.binding)
                self.assertIs(decision.kind, AddressKind.DIRECT_SELF)
                self.assertEqual(
                    decision.evidence,
                    (AddressEvidence.PRIVATE_CHANNEL,),
                )
                self.assertFalse(decision.is_meta_discussion)

                trace = json.dumps(decision.trace_metadata(), ensure_ascii=False)
                rendered = repr(decision)
                for forbidden in (
                    content,
                    event.envelope.session_id,
                    event.envelope.message_id,
                    event.envelope.sender_id,
                    event.envelope.sender_key,
                ):
                    self.assertNotIn(forbidden, trace)
                    self.assertNotIn(forbidden, rendered)

    def test_private_resolver_fails_closed_for_group_drop_mismatch_or_unknown_source(self):
        group_event = _event("普通群聊")
        with self.assertRaisesRegex(
            ContractViolation,
            "address_private_event_required",
        ):
            resolve_private_address(group_event, _accepted(group_event))

        private_event = _private_event("被拒绝的私聊")
        dropped = IngressDecision(
            binding=private_event.binding,
            sender_kind=SenderKind.HUMAN,
            disposition=IngressDisposition.DROP_BANNED,
            reason_codes=("fixture_banned",),
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "address_ingress_not_accepted",
        ):
            resolve_private_address(private_event, dropped)

        other_event = _private_event(
            "另一条私聊",
            session="private-other-session",
            message="private-other-message",
        )
        with self.assertRaisesRegex(ContractViolation, "address_binding_mismatch"):
            resolve_private_address(private_event, _accepted(other_event))

        unknown = _private_event(
            "来源未知",
            sender_kind=SenderKind.UNKNOWN,
            message="private-unknown-message",
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "address_ingress_not_accepted",
        ):
            resolve_private_address(unknown, _accepted(unknown))

        plugin_echo = _private_event(
            "外部插件输出",
            sender_kind=SenderKind.PLUGIN_ECHO,
            plugin_source=PluginSource.EXTERNAL_OTHER,
            message="private-plugin-message",
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "address_ingress_not_accepted",
        ):
            resolve_private_address(plugin_echo, _accepted(plugin_echo))

    def test_non_accepted_or_mismatched_ingress_cannot_resolve(self):
        event = _event("亚托莉，看看这个")
        dropped = IngressDecision(
            binding=event.binding,
            sender_kind=SenderKind.HUMAN,
            disposition=IngressDisposition.DROP_BANNED,
            reason_codes=("fixture_banned",),
        )
        with self.assertRaisesRegex(ContractViolation, "address_ingress_not_accepted"):
            resolve_address(
                event,
                ingress=dropped,
                message_text="亚托莉，看看这个",
                aliases=ALIASES,
            )

        other = _event(
            "other",
            group="other-group",
            message="other-message",
        )
        with self.assertRaisesRegex(ContractViolation, "address_binding_mismatch"):
            resolve_address(
                event,
                ingress=_accepted(other),
                message_text="亚托莉，看看这个",
                aliases=ALIASES,
            )

    def test_structured_self_mention_is_direct_and_other_mention_is_other_person(self):
        self_event = _event("看看这个")
        direct = _resolve(
            "看看这个",
            event=self_event,
            structured_mentions=(
                StructuredMentionEvidence(
                    source_message_id=self_event.envelope.message_id,
                    target_sender_id=self_event.envelope.bot_id,
                ),
            ),
        )
        self.assertIs(direct.kind, AddressKind.DIRECT_SELF)
        self.assertIn(AddressEvidence.STRUCTURED_MENTION, direct.evidence)

        other_event = _event("小林你怎么看", message="mention-other")
        other = _resolve(
            "小林你怎么看",
            event=other_event,
            structured_mentions=(
                StructuredMentionEvidence(
                    source_message_id=other_event.envelope.message_id,
                    target_sender_id="peer-b",
                ),
            ),
        )
        self.assertIs(other.kind, AddressKind.OTHER_PERSON)
        self.assertIn(AddressEvidence.OTHER_PERSON_TARGET, other.evidence)

    def test_reply_to_self_is_direct_and_reply_to_other_is_other_person(self):
        self_reply = _event(
            "继续说吧",
            message="reply-self",
            reply_to_message_id="shio-old-message",
            reply_to_sender_id="bot-self",
        )
        direct = _resolve(
            "继续说吧",
            event=self_reply,
            structured_reply=StructuredReplyEvidence(
                source_message_id=self_reply.envelope.message_id,
                referenced_message_id="shio-old-message",
                referenced_sender_id="bot-self",
                quoted_content="上一条星汐回复",
            ),
        )
        self.assertIs(direct.kind, AddressKind.DIRECT_SELF)
        self.assertIn(AddressEvidence.REPLY_TO_SELF, direct.evidence)
        self.assertEqual(direct.referenced_message_id, "shio-old-message")

        other_reply = _event(
            "你说得对",
            message="reply-other",
            reply_to_message_id="peer-old-message",
            reply_to_sender_id="peer-b",
        )
        other = _resolve(
            "你说得对",
            event=other_reply,
            structured_reply=StructuredReplyEvidence(
                source_message_id=other_reply.envelope.message_id,
                referenced_message_id="peer-old-message",
                referenced_sender_id="peer-b",
                quoted_content="亚托莉刚刚说了什么",
            ),
        )
        self.assertIs(other.kind, AddressKind.OTHER_PERSON)
        self.assertNotEqual(other.kind, AddressKind.DIRECT_SELF)

    def test_sentence_start_and_end_vocatives_are_direct(self):
        cases = (
            "亚托莉，帮我看看这个",
            "这个问题你怎么看，亚托莉？",
            "这个问题，亚托莉你能解释一下吗？",
        )
        for content in cases:
            with self.subTest(content=content):
                decision = _resolve(content)
                self.assertIs(decision.kind, AddressKind.DIRECT_SELF)
                self.assertIn(AddressEvidence.VOCATIVE_ALIAS, decision.evidence)

    def test_emotional_vocative_and_meta_discussion_are_not_collapsed(self):
        direct = _resolve("笨蛋萝卜子，你这次是大修")
        about = _resolve("我觉得笨蛋萝卜子这个称呼很有趣")

        self.assertIs(direct.kind, AddressKind.DIRECT_SELF)
        self.assertIn(AddressEvidence.VOCATIVE_ALIAS, direct.evidence)
        self.assertIs(about.kind, AddressKind.ABOUT_SELF)
        self.assertNotEqual(about.kind, AddressKind.DIRECT_SELF)
        self.assertTrue(about.is_meta_discussion)
        self.assertIn(AddressEvidence.THIRD_PERSON_REFERENCE, about.evidence)

    def test_third_person_and_plain_self_reference_are_about_self(self):
        for content in (
            "亚托莉的角色设定很有趣",
            "我很喜欢亚托莉",
            "刚刚大家提到了萝卜子",
        ):
            with self.subTest(content=content):
                decision = _resolve(content)
                self.assertIs(decision.kind, AddressKind.ABOUT_SELF)
                self.assertIn(
                    AddressEvidence.THIRD_PERSON_REFERENCE,
                    decision.evidence,
                )

    def test_alias_only_in_url_code_or_title_does_not_address_or_discuss_self(self):
        cases = (
            "链接是 https://example.invalid/ATRI",
            "配置项是 `persona=ATRI`",
            "代码如下：```python\ncharacter = '亚托莉'\n```",
            "我刚买了《ATRI》",
        )
        for content in cases:
            with self.subTest(content=content):
                decision = _resolve(content)
                self.assertIs(decision.kind, AddressKind.UNCERTAIN)
                self.assertEqual(decision.evidence, (AddressEvidence.NONE,))

    def test_explicit_open_group_and_ambiguous_statement_are_distinct(self):
        open_group = _resolve("大家觉得这次改动怎么样？")
        uncertain = _resolve("刚刚改完了")

        self.assertIs(open_group.kind, AddressKind.OPEN_GROUP)
        self.assertIn(AddressEvidence.OPEN_GROUP_MARKER, open_group.evidence)
        self.assertIs(uncertain.kind, AddressKind.UNCERTAIN)
        self.assertEqual(uncertain.evidence, (AddressEvidence.NONE,))

    def test_name_wake_candidate_is_only_corrobation_for_current_visible_text(self):
        content = "笨蛋萝卜子，你过来一下"
        candidate = IngressWakeCandidate(
            should_observe=True,
            was_native_wake=False,
            natural_direct=True,
            alias="萝卜子",
            reason_code="emotional_vocative",
        )
        direct = _resolve(content, name_wake_candidate=candidate)
        self.assertIs(direct.kind, AddressKind.DIRECT_SELF)
        self.assertIn(AddressEvidence.VOCATIVE_ALIAS, direct.evidence)

        quoted_only = _resolve(
            "你说得对",
            name_wake_candidate=candidate,
            structured_reply=StructuredReplyEvidence(
                source_message_id="address-message",
                referenced_message_id="quoted-message",
                referenced_sender_id="peer-b",
                quoted_content=content,
            ),
            event=_event(
                "你说得对",
                reply_to_message_id="quoted-message",
                reply_to_sender_id="peer-b",
            ),
        )
        self.assertIs(quoted_only.kind, AddressKind.OTHER_PERSON)
        self.assertNotEqual(quoted_only.kind, AddressKind.DIRECT_SELF)

    def test_group_scene_must_match_current_scope_and_revision(self):
        content = "大家继续吧"
        event = _event(content)
        scene_book = GroupSceneBook()
        scene_book.record_human(
            event,
            decision=_accepted(event),
            public_content=content,
            personal_facts=(
                PersonalFactCandidate(
                    subject_key=event.envelope.sender_key,
                    content="private fact must not affect address",
                ),
            ),
        )
        decision = _resolve(
            content,
            event=event,
            group_scene=scene_book.snapshot(event.envelope.scope_key),
        )
        self.assertIs(decision.kind, AddressKind.OPEN_GROUP)

        stale_scene = GroupSceneBook().snapshot(event.envelope.scope_key)
        with self.assertRaisesRegex(ContractViolation, "address_scene_binding_mismatch"):
            _resolve(content, event=event, group_scene=stale_scene)

    def test_output_is_frozen_bound_and_trace_contains_no_text_or_ids(self):
        content = "亚托莉，看看这个秘密问题"
        event = _event(content)
        decision = _resolve(content, event=event)

        self.assertEqual(decision.binding, event.binding)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            decision.kind = AddressKind.UNCERTAIN
        rendered = json.dumps(decision.trace_metadata(), ensure_ascii=False)
        for forbidden in (
            content,
            event.envelope.scope_key,
            event.envelope.message_id,
            event.envelope.sender_key,
            "亚托莉",
        ):
            self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":
    unittest.main()
