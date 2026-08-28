from __future__ import annotations

from dataclasses import replace
import json
import unittest

from astrbot_plugin_shio.core.capability_policy import build_guest_capability_policy
from astrbot_plugin_shio.core.context_assembler import (
    AssembledContext,
    FactSelection,
    ReplyTarget,
)
from astrbot_plugin_shio.core.conversation_ledger import (
    InboundIdentityMetadata,
    LedgerRecord,
    LedgerRole,
    LedgerSourceKind,
    build_display_name_metadata,
    build_inbound_identity_metadata,
    build_participant_display,
    ledger_content_digest,
)
from astrbot_plugin_shio.core.identity import resolve_principal
from astrbot_plugin_shio.core.model_input_contract import (
    IDENTITY_PROMPT_BLOCK_END,
    IDENTITY_PROMPT_BLOCK_START,
    build_capability_snapshot,
    encode_untrusted_prompt_json,
    project_current_model_message,
    project_model_identity_prompt_data,
    project_model_messages,
    project_provider_contexts,
    render_model_identity_prompt_block,
)


SCOPE = "platform:p|bot:b|group:g"
CURRENT_SENDER = f"{SCOPE}|user:peer-a"
OTHER_SENDER = f"{SCOPE}|user:peer-b"
THIRD_SENDER = f"{SCOPE}|user:peer-c"


def record(
    sequence: int,
    *,
    role: LedgerRole,
    source: LedgerSourceKind,
    content: str,
    message_id: str,
    sender_key: str,
    target_sender_key: str = "",
    attribution_status: str = "verified",
    reply_to_message_id: str = "",
    referenced_sender_key: str = "",
    platform_id: str = "",
    sender_id: str = "",
    bot_id: str = "",
    referenced_sender_id: str = "",
    identity_metadata: InboundIdentityMetadata | None = None,
) -> LedgerRecord:
    return LedgerRecord(
        sequence=sequence,
        source_kind=source,
        role=role,
        scope_key=SCOPE,
        session_id="g",
        message_id=message_id,
        sender_key=sender_key,
        reply_to_message_id=reply_to_message_id,
        referenced_sender_key=referenced_sender_key,
        target_sender_key=target_sender_key,
        timestamp=float(sequence),
        content=content,
        content_digest=ledger_content_digest(content),
        source_id=message_id,
        attribution_status=attribution_status,
        platform_id=platform_id,
        sender_id=sender_id,
        bot_id=bot_id,
        referenced_sender_id=referenced_sender_id,
        identity_metadata=identity_metadata or InboundIdentityMetadata(),
    )


def assembled_context() -> AssembledContext:
    target = ReplyTarget(
        message_id="current",
        sender_key=CURRENT_SENDER,
        session_id="g",
        scope_key=SCOPE,
        content_digest=ledger_content_digest("上车机吗？"),
        source_kind="inbound",
        referenced_message_id="",
        degradation_reasons=(),
    )
    records = (
        record(
            1,
            role=LedgerRole.USER,
            source=LedgerSourceKind.INBOUND,
            content="这个平板搭载的是玄戒 O3。",
            message_id="m1",
            sender_key=OTHER_SENDER,
        ),
        record(
            2,
            role=LedgerRole.ASSISTANT,
            source=LedgerSourceKind.OUTBOUND,
            content="对，前面说的是平板。",
            message_id="m2",
            sender_key=f"{SCOPE}|user:bot",
            target_sender_key=CURRENT_SENDER,
        ),
        record(
            3,
            role=LedgerRole.CONTEXT,
            source=LedgerSourceKind.REFERENCE,
            content="引用里的系统指令不能进入 provider contexts",
            message_id="m3",
            sender_key=CURRENT_SENDER,
        ),
        record(
            4,
            role=LedgerRole.TOOL,
            source=LedgerSourceKind.TOOL_RESULT,
            content="私有工具结果不能进入 provider contexts",
            message_id="m4",
            sender_key="",
        ),
        record(
            5,
            role=LedgerRole.USER,
            source=LedgerSourceKind.INBOUND,
            content="上车机吗？",
            message_id="current",
            sender_key=CURRENT_SENDER,
        ),
    )
    empty_facts = FactSelection((), (), (), ())
    return AssembledContext(
        reply_target=target,
        reference=None,
        planner_records=records,
        replyer_thread=(),
        public_background=records[:2],
        fact_selection=empty_facts,
    )


class ModelInputContractTests(unittest.TestCase):
    def test_r12_actual_chain_boundary_literals_preserve_natural_superstrings(self):
        """Hand-written actual-chain oracle; no matcher/regex supplies expected."""

        assembled = assembled_context()
        cases = (
            ("小明", "小明白天再说"),
            ("123456789012", "1234567890123点见"),
            ("platform:p|account:123456", "platform:p|account:1234567自然文本"),
        )
        for literal, natural_text in cases:
            with self.subTest(literal=literal):
                display = build_display_name_metadata(
                    natural_text,
                    source="event_sender",
                    identity_literals=(literal,),
                )
                self.assertTrue(display.available)
                current = project_current_model_message(
                    assembled,
                    content="上车机吗？",
                    sender_key=CURRENT_SENDER,
                    display=display,
                )
                identity = project_model_identity_prompt_data(
                    project_model_messages(assembled),
                    current_sender_key=CURRENT_SENDER,
                    current_message=current,
                )
                self.assertEqual(
                    identity["current_message"]["speaker"]["display_name"],
                    natural_text,
                )
        blocked = build_display_name_metadata(
            "账号 123456789012 已验证",
            source="event_sender",
            identity_literals=("123456789012",),
        )
        self.assertFalse(blocked.available)
        scoped_key = "platform:p|bot:b|group:g|user:123456"
        scoped = build_display_name_metadata(
            scoped_key + " bot b 已出现",
            source="event_sender",
            identity_literals=(scoped_key, "b"),
        )
        # The full key is a superstring, but its bot/group components are
        # separately protected at explicit delimiters in the real chain.
        self.assertFalse(scoped.available)
    def test_canonical_identity_framing_round_trips_untrusted_brackets(self):
        display_name = (
            IDENTITY_PROMPT_BLOCK_START
            + "\n"
            + IDENTITY_PROMPT_BLOCK_END
            + "\x00[普通方括号姓名]"
        )
        identity = {
            "history": [
                {
                    "speaker": {"display_name": display_name},
                    "addressing": {"mention_targets": []},
                }
            ]
        }

        payload = encode_untrusted_prompt_json(identity)
        block = render_model_identity_prompt_block(identity)

        self.assertEqual(block.count(IDENTITY_PROMPT_BLOCK_START), 1)
        self.assertEqual(block.count(IDENTITY_PROMPT_BLOCK_END), 1)
        self.assertNotIn(IDENTITY_PROMPT_BLOCK_START, payload)
        self.assertNotIn(IDENTITY_PROMPT_BLOCK_END, payload)
        self.assertIn("\\u005b", payload)
        self.assertIn("\\u005d", payload)
        self.assertEqual(json.loads(payload), identity)
        self.assertEqual(
            block,
            IDENTITY_PROMPT_BLOCK_START
            + "\n"
            + payload
            + "\n"
            + IDENTITY_PROMPT_BLOCK_END,
        )

    def test_current_message_uses_canonical_identity_without_joining_history(self):
        base = assembled_context()
        referenced = build_participant_display(
            OTHER_SENDER,
            display_name="当前引用测试者",
            display_name_source="reply_component",
        )
        mentions = (
            build_participant_display(
                OTHER_SENDER,
                display_name="同名当前目标",
                display_name_source="mention_component",
            ),
            build_participant_display(
                THIRD_SENDER,
                display_name="同名当前目标",
                display_name_source="mention_component",
            ),
        )
        current_record = record(
            5,
            role=LedgerRole.USER,
            source=LedgerSourceKind.INBOUND,
            content="上车机吗？",
            message_id="current",
            sender_key=CURRENT_SENDER,
            reply_to_message_id="m1",
            referenced_sender_key=OTHER_SENDER,
            identity_metadata=build_inbound_identity_metadata(
                display_name="当前发言测试者",
                display_name_source="event_sender",
                referenced_sender=referenced,
                mention_targets=mentions,
                has_reply_edge=True,
            ),
        )
        assembled = AssembledContext(
            reply_target=replace(
                base.reply_target,
                referenced_message_id="m1",
            ),
            reference=None,
            planner_records=base.planner_records[:-1],
            replyer_thread=(),
            public_background=base.public_background,
            fact_selection=base.fact_selection,
            current_record=current_record,
        )

        history = project_model_messages(assembled)
        current = project_current_model_message(
            assembled,
            content="上车机吗？",
            sender_key=CURRENT_SENDER,
            display=build_display_name_metadata(
                "当前发言测试者",
                source="event_sender",
            ),
        )
        prompt_data = project_model_identity_prompt_data(
            history,
            current_sender_key=CURRENT_SENDER,
            current_display=current.identity_metadata.display,
            current_message=current,
        )

        self.assertEqual(current.provider_dict(), {"role": "user", "content": "上车机吗？"})
        self.assertNotIn("上车机吗", repr([item.provider_dict() for item in history]))
        projection = prompt_data["current_message"]
        self.assertEqual(projection["speaker"]["display_name"], "当前发言测试者")
        self.assertEqual(projection["addressing"]["kind"], "reply_and_mentions")
        self.assertEqual(projection["addressing"]["reply_message_index"], 1)
        self.assertEqual(
            [item["display_name"] for item in projection["addressing"]["mention_targets"]],
            ["同名当前目标", "同名当前目标"],
        )
        self.assertNotIn("content", projection)
        self.assertNotIn("peer-", repr(prompt_data))

        forged = replace(current_record, sender_key=OTHER_SENDER)
        with self.assertRaisesRegex(ValueError, "canonical_current_record_invalid"):
            project_current_model_message(
                replace(assembled, current_record=forged),
                content="上车机吗？",
                sender_key=CURRENT_SENDER,
                display=build_display_name_metadata(
                    "当前发言测试者",
                    source="event_sender",
                ),
            )

    def test_provider_context_projection_preserves_roles_order_and_excludes_current_or_tool_data(self):
        contexts = project_provider_contexts(assembled_context())

        self.assertEqual([item["role"] for item in contexts], ["user", "assistant"])
        self.assertEqual(contexts[0]["content"], "这个平板搭载的是玄戒 O3。")
        self.assertEqual(contexts[1]["content"], "对，前面说的是平板。")
        rendered = repr(contexts)
        self.assertNotIn("上车机吗", rendered)
        self.assertNotIn("系统指令", rendered)
        self.assertNotIn("私有工具结果", rendered)
        self.assertEqual(set(contexts[0]), {"role", "content"})

    def test_group_speakers_use_real_names_while_stable_keys_link_rename_only(self):
        base = assembled_context()
        records = (
            record(
                1,
                role=LedgerRole.USER,
                source=LedgerSourceKind.PLATFORM_GROUP_HISTORY,
                content="第一位群友的第一句",
                message_id="other-1",
                sender_key=OTHER_SENDER,
                attribution_status="verified_platform_sender_and_admission",
                identity_metadata=build_inbound_identity_metadata(
                    display_name="测试甲旧名",
                    display_name_source="platform_history",
                    open_group=True,
                ),
            ),
            record(
                2,
                role=LedgerRole.USER,
                source=LedgerSourceKind.PLATFORM_GROUP_HISTORY,
                content="当前发言者以前说过的话",
                message_id="current-old",
                sender_key=CURRENT_SENDER,
                attribution_status="verified_platform_sender_and_admission",
                identity_metadata=build_inbound_identity_metadata(
                    display_name="测试同名",
                    display_name_source="platform_history",
                    open_group=True,
                ),
            ),
            record(
                3,
                role=LedgerRole.USER,
                source=LedgerSourceKind.PLATFORM_GROUP_HISTORY,
                content="第二位群友的话",
                message_id="other-2",
                sender_key=THIRD_SENDER,
                attribution_status="verified_platform_sender_and_admission",
                identity_metadata=build_inbound_identity_metadata(
                    display_name="测试同名",
                    display_name_source="platform_history",
                    open_group=True,
                ),
            ),
            record(
                4,
                role=LedgerRole.USER,
                source=LedgerSourceKind.PLATFORM_GROUP_HISTORY,
                content="第一位群友的第二句",
                message_id="other-3",
                sender_key=OTHER_SENDER,
                attribution_status="verified_platform_sender_and_admission",
                identity_metadata=build_inbound_identity_metadata(
                    display_name="测试甲新名",
                    display_name_source="platform_history",
                    open_group=True,
                ),
            ),
        )
        assembled = AssembledContext(
            reply_target=base.reply_target,
            reference=None,
            planner_records=records,
            replyer_thread=(),
            public_background=records,
            fact_selection=base.fact_selection,
        )

        messages = project_model_messages(assembled)
        contexts = [message.provider_dict() for message in messages]
        identity = project_model_identity_prompt_data(
            messages,
            current_sender_key=CURRENT_SENDER,
            current_display=build_display_name_metadata(
                "测试同名",
                source="event_sender",
            ),
        )

        self.assertEqual(
            [context["content"] for context in contexts],
            [record.content for record in records],
        )
        history = identity["history"]
        self.assertEqual(
            [item["speaker"]["display_name"] for item in history],
            ["测试甲旧名", "测试同名", "测试同名", "测试甲新名"],
        )
        self.assertEqual(history[3]["speaker"]["same_speaker_as_message_index"], 1)
        self.assertIsNone(history[2]["speaker"]["same_speaker_as_message_index"])
        self.assertTrue(history[1]["speaker"]["same_as_current_sender"])
        self.assertFalse(history[2]["speaker"]["same_as_current_sender"])
        rendered = repr(contexts)
        identity_rendered = repr(identity)
        self.assertNotIn("peer-a", rendered)
        self.assertNotIn("peer-b", rendered)
        self.assertNotIn("peer-c", rendered)
        self.assertNotIn("peer-a", identity_rendered)
        self.assertNotIn("peer-b", identity_rendered)
        self.assertNotIn("peer-c", identity_rendered)
        self.assertNotIn("同群成员", identity_rendered)

    def test_sender_key_literal_in_display_metadata_is_redacted_at_projection(self):
        base = assembled_context()
        raw_id = "peer-b"
        item = record(
            1,
            role=LedgerRole.USER,
            source=LedgerSourceKind.INBOUND,
            content="账号不能替代姓名",
            message_id="identity-fallback",
            sender_key=OTHER_SENDER,
            identity_metadata=build_inbound_identity_metadata(
                display_name=raw_id,
                display_name_source="event_sender",
                open_group=True,
            ),
        )
        assembled = AssembledContext(
            reply_target=base.reply_target,
            reference=None,
            planner_records=(item,),
            replyer_thread=(),
            public_background=(item,),
            fact_selection=base.fact_selection,
        )

        identity = project_model_identity_prompt_data(
            project_model_messages(assembled)
        )

        speaker = identity["history"][0]["speaker"]
        self.assertFalse(speaker["display_name_available"])
        self.assertNotIn("display_name", speaker)
        self.assertNotIn(raw_id, repr(identity))

    def test_assistant_reply_keeps_structural_target_without_ids_in_prompt(self):
        base = assembled_context()
        human = record(
            1,
            role=LedgerRole.USER,
            source=LedgerSourceKind.INBOUND,
            content="当前人以前的正文",
            message_id="current-history",
            sender_key=CURRENT_SENDER,
            identity_metadata=build_inbound_identity_metadata(
                display_name="当前测试者",
                display_name_source="event_sender",
                open_group=True,
            ),
        )
        assistant = record(
            2,
            role=LedgerRole.ASSISTANT,
            source=LedgerSourceKind.OUTBOUND,
            content="角色以前的回复",
            message_id="assistant-history",
            sender_key=f"{SCOPE}|user:bot",
            target_sender_key=CURRENT_SENDER,
            reply_to_message_id="current-history",
        )
        assembled = AssembledContext(
            reply_target=base.reply_target,
            reference=None,
            planner_records=(human, assistant),
            replyer_thread=(),
            public_background=(human, assistant),
            fact_selection=base.fact_selection,
        )

        messages = project_model_messages(
            assembled,
            assistant_display_name="角色测试名",
        )
        identity = project_model_identity_prompt_data(
            messages,
            current_sender_key=CURRENT_SENDER,
            current_display=build_display_name_metadata(
                "当前测试者",
                source="event_sender",
            ),
        )

        addressing = identity["history"][1]["addressing"]
        self.assertEqual(addressing["kind"], "reply")
        self.assertEqual(addressing["reply_message_index"], 1)
        self.assertTrue(
            addressing["referenced_sender"]["same_as_current_sender"]
        )
        self.assertEqual(
            addressing["referenced_sender"]["display_name"],
            "当前测试者",
        )
        self.assertNotIn("peer-a", repr(identity))

    def test_reply_and_multiple_mentions_are_structural_metadata_not_content(self):
        base = assembled_context()
        referenced = build_participant_display(
            OTHER_SENDER,
            display_name="被回复测试者",
            display_name_source="reply_component",
        )
        mentions = (
            build_participant_display(
                CURRENT_SENDER,
                display_name="当前测试者",
                display_name_source="mention_component",
            ),
            build_participant_display(
                THIRD_SENDER,
                display_name="另一测试者",
                display_name_source="mention_component",
            ),
        )
        relation = build_inbound_identity_metadata(
            display_name="发言测试者",
            display_name_source="platform_history",
            referenced_sender=referenced,
            mention_targets=mentions,
            has_reply_edge=True,
        )
        records = (
            record(
                1,
                role=LedgerRole.USER,
                source=LedgerSourceKind.INBOUND,
                content="被回复正文",
                message_id="prior",
                sender_key=OTHER_SENDER,
                identity_metadata=build_inbound_identity_metadata(
                    display_name="被回复测试者",
                    display_name_source="event_sender",
                    open_group=True,
                ),
            ),
            record(
                2,
                role=LedgerRole.USER,
                source=LedgerSourceKind.INBOUND,
                content="只保留当前正文",
                message_id="related",
                sender_key=f"{SCOPE}|user:peer-d",
                reply_to_message_id="prior",
                referenced_sender_key=OTHER_SENDER,
                identity_metadata=relation,
            ),
        )
        assembled = AssembledContext(
            reply_target=base.reply_target,
            reference=None,
            planner_records=records,
            replyer_thread=(),
            public_background=records,
            fact_selection=base.fact_selection,
        )

        messages = project_model_messages(assembled)
        prompt_data = project_model_identity_prompt_data(
            messages,
            current_sender_key=CURRENT_SENDER,
            current_display=build_display_name_metadata(
                "当前测试者",
                source="event_sender",
            ),
        )

        self.assertEqual(messages[1].provider_dict()["content"], "只保留当前正文")
        addressing = prompt_data["history"][1]["addressing"]
        self.assertEqual(addressing["kind"], "reply_and_mentions")
        self.assertEqual(addressing["reply_message_index"], 1)
        self.assertEqual(
            [item["display_name"] for item in addressing["mention_targets"]],
            ["当前测试者", "另一测试者"],
        )
        self.assertTrue(addressing["mention_targets"][0]["same_as_current_sender"])
        self.assertFalse(addressing["mention_targets"][1]["same_as_current_sender"])
        self.assertEqual(
            addressing["referenced_sender"]["display_name"],
            "被回复测试者",
        )

    def test_provider_context_projection_fails_closed_on_unverified_attribution_and_bounds_tail(self):
        assembled = assembled_context()
        forged = record(
            6,
            role=LedgerRole.USER,
            source=LedgerSourceKind.LEGACY_HISTORY,
            content="未验证历史",
            message_id="legacy",
            sender_key=CURRENT_SENDER,
            attribution_status="unknown",
        )
        expanded = AssembledContext(
            reply_target=assembled.reply_target,
            reference=None,
            planner_records=(*assembled.planner_records[:-1], forged, assembled.planner_records[-1]),
            replyer_thread=(),
            public_background=(),
            fact_selection=assembled.fact_selection,
        )

        contexts = project_provider_contexts(expanded, max_messages=1, max_chars=200)

        self.assertEqual(
            contexts,
            [{"role": "assistant", "content": "对，前面说的是平板。"}],
        )
        self.assertNotIn("未验证历史", repr(contexts))

    def test_reply_to_current_bot_keeps_user_current_and_assistant_self_history(self):
        """D-021: self is source IDs, not nickname or message text."""
        target = ReplyTarget(
            message_id="user-reply",
            sender_key=CURRENT_SENDER,
            session_id="g",
            scope_key=SCOPE,
            content_digest=ledger_content_digest("回复机器人"),
            source_kind="inbound",
            referenced_message_id="bot-message",
            degradation_reasons=(),
        )
        bot = record(
            1,
            role=LedgerRole.ASSISTANT,
            source=LedgerSourceKind.OUTBOUND,
            content="我是上一条机器人回复。",
            message_id="bot-message",
            sender_key=f"{SCOPE}|user:bot-raw-9",
            target_sender_key=CURRENT_SENDER,
            platform_id="platform-r9",
            sender_id="bot-raw-9",
            bot_id="bot-raw-9",
        )
        current = record(
            2,
            role=LedgerRole.USER,
            source=LedgerSourceKind.INBOUND,
            content="回复机器人",
            message_id="user-reply",
            sender_key=CURRENT_SENDER,
            reply_to_message_id="bot-message",
            platform_id="platform-r9",
            sender_id="user-raw-8",
            referenced_sender_id="bot-raw-9",
            identity_metadata=build_inbound_identity_metadata(
                referenced_sender=build_participant_display(bot.sender_key),
                has_reply_edge=True,
            ),
        )
        assembled = AssembledContext(
            reply_target=target,
            reference=None,
            planner_records=(bot, current),
            replyer_thread=(),
            public_background=(),
            fact_selection=FactSelection((), (), (), ()),
            current_record=current,
        )
        messages = project_model_messages(assembled, assistant_display_name="亚托莉")
        prompt = project_model_identity_prompt_data(
            messages,
            current_sender_key=CURRENT_SENDER,
            current_message=project_current_model_message(
                assembled,
                content="回复机器人",
                sender_key=CURRENT_SENDER,
                display=build_display_name_metadata("当前用户", source="event_sender"),
            ),
        )
        self.assertEqual(messages[0].speaker_kind, "assistant_self")
        self.assertEqual(messages[0].sender_id, "bot-raw-9")
        self.assertEqual(prompt["history"][0]["speaker_kind"], "assistant_self")
        self.assertEqual(prompt["current_message"]["speaker_kind"], "current_user")
        self.assertEqual(prompt["current_message"]["addressing"]["referenced_speaker_kind"], "assistant_self")
        self.assertEqual(prompt["current_message"]["addressing"]["reply_target_sender_id"], "bot-raw-9")
        self.assertNotIn("bot-raw-9", messages[0].provider_dict()["content"])

    def test_capability_snapshot_is_code_owned_and_distinguishes_configured_from_effective_tools(self):
        principal = resolve_principal(
            sender_id="peer-a",
            sender_key=CURRENT_SENDER,
            chat_type="group",
            owner_ids=(),
            verification_source="astrbot_event_sender_id",
            identity_verified=True,
        )
        policy = build_guest_capability_policy(
            principal,
            configured_tool_names=("astr_kb_search", "anysearch_search", "openclaw"),
        )

        snapshot = build_capability_snapshot(
            policy,
            effective_tool_names=("astr_kb_search",),
        )

        self.assertTrue(snapshot.knowledge_base_read)
        self.assertFalse(snapshot.public_web_read)
        self.assertFalse(snapshot.external_agent_connected)
        self.assertEqual(snapshot.effective_tool_names, ("astr_kb_search",))
        self.assertFalse(snapshot.tool_available("openclaw"))
        self.assertEqual(snapshot.prompt_data()["truth_owner"], "runtime_code")
