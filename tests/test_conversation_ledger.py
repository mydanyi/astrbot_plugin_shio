import json
import hashlib
import hmac
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_shio.core.context_assembler import (
    AssembledContext,
    FactSelection,
    ReplyTarget,
)
from astrbot_plugin_shio.core.conversation_ledger import (
    ConversationLedger,
    InboundIdentityMetadata,
    LedgerRecord,
    LedgerRole,
    LedgerSourceKind,
    PlatformGroupHistoryStatus,
    adapt_astrbot_history,
    build_inbound_identity_metadata,
    build_participant_display,
    ledger_content_digest,
    read_astrbot_group_history,
    records_for_sender_thread,
)
from astrbot_plugin_shio.core.identity import TurnEnvelope
from astrbot_plugin_shio.core.model_input_contract import (
    project_model_identity_prompt_data,
    project_model_messages,
    project_provider_contexts,
)


def envelope(
    *,
    scope_key="platform:p|bot:b|group:g",
    message_id="msg-current",
    sender_key="platform:p|bot:b|group:g|user:guest-a",
    sender_id="guest-a",
    reply_to_message_id="msg-old",
    reply_to_sender_id="guest-b",
    timestamp=1_765_000_000,
):
    return TurnEnvelope(
        session_id="g",
        message_id=message_id,
        scope_key=scope_key,
        sender_key=sender_key,
        sender_id=sender_id,
        platform_id="p",
        bot_id="b",
        chat_type="group",
        group_id="g",
        reply_to_message_id=reply_to_message_id,
        reply_to_sender_id=reply_to_sender_id,
        timestamp=timestamp,
        timestamp_source="message",
        source_kind="inbound",
        degradation_reasons=(),
    )


def accepted_history_record(
    *,
    sequence: int,
    sender_id: str,
    content: str,
    timestamp: float,
    identity_metadata: InboundIdentityMetadata | None = None,
    reply_to_message_id: str = "",
    referenced_sender_key: str = "",
) -> LedgerRecord:
    scope = "platform:p|bot:b|group:g"
    return LedgerRecord(
        sequence=sequence,
        source_kind=LedgerSourceKind.INBOUND,
        role=LedgerRole.USER,
        scope_key=scope,
        session_id="g",
        message_id=f"accepted-{sequence}",
        sender_key=f"{scope}|user:{sender_id}",
        reply_to_message_id=reply_to_message_id,
        referenced_sender_key=referenced_sender_key,
        target_sender_key="",
        timestamp=timestamp,
        content=content,
        content_digest=ledger_content_digest(content),
        source_id=f"accepted-{sequence}",
        attribution_status="verified",
        identity_metadata=identity_metadata or InboundIdentityMetadata(),
    )


class _HistoryManager:
    def __init__(self, rows):
        self.rows = rows

    async def get(self, **_kwargs):
        return self.rows


class _HistoryEvent:
    unified_msg_origin = "origin:g"

    def __init__(self, current_id: int):
        self.current_id = current_id

    def get_extra(self, key, default=None):
        if key == "_current_platform_message_history_id":
            return self.current_id
        return default

    @staticmethod
    def get_platform_id():
        return "p"


class PlatformGroupHistoryIdentityIsolationTests(unittest.IsolatedAsyncioTestCase):
    scope = "platform:p|bot:b|group:g"

    @staticmethod
    def _row(*, row_id, sender_id, sender_name, timestamp, message):
        return SimpleNamespace(
            id=row_id,
            platform_id="p",
            user_id="origin:g",
            sender_id=sender_id,
            sender_name=sender_name,
            created_at=datetime.fromtimestamp(timestamp, tz=timezone.utc),
            content={"type": "user", "message": message},
        )

    async def test_display_name_that_looks_like_topic_never_becomes_message_content(self):
        timestamp = 1_765_000_000.0
        nickname_topic = "四十岁开始学编程也不晚"
        actual_topic = "四十岁开始学编程也不晚"
        rows = [
            self._row(
                row_id=41,
                sender_id="peer-a",
                sender_name=nickname_topic,
                timestamp=timestamp,
                message=[{"type": "plain", "text": "[表情]"}],
            ),
            self._row(
                row_id=42,
                sender_id="peer-b",
                sender_name="普通昵称",
                timestamp=timestamp + 10,
                message=[{"type": "plain", "text": actual_topic}],
            ),
        ]
        accepted = [
            accepted_history_record(
                sequence=1,
                sender_id="peer-a",
                content="[表情]",
                timestamp=timestamp,
            ),
            accepted_history_record(
                sequence=2,
                sender_id="peer-b",
                content=actual_topic,
                timestamp=timestamp + 10,
            ),
        ]

        result = await read_astrbot_group_history(
            context=SimpleNamespace(message_history_manager=_HistoryManager(rows)),
            event=_HistoryEvent(current_id=50),
            accepted_records=accepted,
            scope_key=self.scope,
            session_id="g",
        )

        self.assertIs(result.status, PlatformGroupHistoryStatus.VERIFIED)
        self.assertEqual([record.content for record in result.records], ["[表情]", actual_topic])
        self.assertEqual(
            [record.display_name for record in result.records],
            [nickname_topic, "普通昵称"],
        )
        self.assertNotIn(nickname_topic, result.records[0].content)
        self.assertEqual(
            result.records[0].content_digest,
            ledger_content_digest("[表情]"),
        )
        assembled = AssembledContext(
            reply_target=ReplyTarget(
                message_id="current",
                sender_key=f"{self.scope}|user:current",
                session_id="g",
                scope_key=self.scope,
                content_digest=ledger_content_digest("现在在聊什么？"),
                source_kind="inbound",
                referenced_message_id="",
                degradation_reasons=(),
            ),
            reference=None,
            planner_records=result.records,
            replyer_thread=(),
            public_background=result.records,
            fact_selection=FactSelection((), (), (), ()),
        )
        provider_contexts = project_provider_contexts(assembled)
        identity_data = project_model_identity_prompt_data(
            project_model_messages(assembled)
        )
        provider_rendered = repr(provider_contexts)
        self.assertEqual(
            [context["content"] for context in provider_contexts],
            ["[表情]", actual_topic],
        )
        self.assertNotIn("同群成员", provider_rendered)
        self.assertNotIn("群友1", provider_rendered)
        self.assertNotIn("peer-a", provider_rendered)
        self.assertNotIn("peer-b", provider_rendered)
        self.assertEqual(
            [
                item["speaker"]["display_name"]
                for item in identity_data["history"]
            ],
            [nickname_topic, "普通昵称"],
        )
        self.assertIn("peer-a", repr(identity_data))
        self.assertIn("peer-b", repr(identity_data))

    async def test_structured_components_match_pure_ledger_body_and_keep_names_as_metadata(self):
        timestamp = 1_765_000_100.0
        cases = (
            {
                "name": "reply",
                "sender_id": "peer-reply",
                "body": "回复消息的纯正文",
                "reply_id": "reply-target",
                "reply_name": "真实引用者",
                "mention_ids": (),
                "mention_names": (),
                "message": [
                    {
                        "type": "reply",
                        "sender_id": "reply-target",
                        "sender_name": "真实引用者",
                        "text": "上一条正文",
                    },
                    {"type": "plain", "text": "回复消息的纯正文"},
                ],
            },
            {
                "name": "single_at",
                "sender_id": "peer-single-at",
                "body": "单个提及的纯正文",
                "reply_id": "",
                "reply_name": "",
                "mention_ids": ("single-target",),
                "mention_names": ("真实提及者",),
                "message": [
                    {
                        "type": "at",
                        "qq": "single-target",
                        "name": "真实提及者",
                    },
                    {"type": "plain", "text": "单个提及的纯正文"},
                ],
            },
            {
                "name": "multiple_at",
                "sender_id": "peer-multiple-at",
                "body": "多个提及的纯正文",
                "reply_id": "",
                "reply_name": "",
                "mention_ids": ("multi-target-a", "multi-target-b"),
                "mention_names": ("多目标甲", "多目标乙"),
                "message": [
                    {
                        "type": "at",
                        "qq": "multi-target-a",
                        "name": "多目标甲",
                    },
                    {
                        "type": "at",
                        "qq": "multi-target-b",
                        "name": "多目标乙",
                    },
                    {"type": "plain", "text": "多个提及的纯正文"},
                ],
            },
            {
                "name": "reply_multiple_at",
                "sender_id": "peer-reply-multiple-at",
                "body": "字面 @姓名 与 [回复 普通方括号] 保持正文",
                "reply_id": "reply-multi-target",
                "reply_name": "组合引用者",
                "mention_ids": ("combo-target-a", "combo-target-b"),
                "mention_names": ("组合目标甲", "组合目标乙"),
                "message": [
                    {
                        "type": "reply",
                        "sender_id": "reply-multi-target",
                        "sender_name": "组合引用者",
                        "text": "上一条正文",
                    },
                    {
                        "type": "at",
                        "qq": "combo-target-a",
                        "name": "组合目标甲",
                    },
                    {
                        "type": "at",
                        "qq": "combo-target-b",
                        "name": "组合目标乙",
                    },
                    {
                        "type": "plain",
                        "text": "字面 @姓名 与 [回复 普通方括号] 保持正文",
                    },
                ],
            },
        )
        ledger = ConversationLedger()
        rows = []
        accepted = []
        for index, case in enumerate(cases):
            case_timestamp = timestamp + index * 10
            referenced_sender = (
                build_participant_display(
                    f"{self.scope}|user:{case['reply_id']}",
                )
                if case["reply_id"]
                else None
            )
            mention_targets = tuple(
                build_participant_display(f"{self.scope}|user:{sender_id}")
                for sender_id in case["mention_ids"]
            )
            metadata = build_inbound_identity_metadata(
                referenced_sender=referenced_sender,
                mention_targets=mention_targets,
                has_reply_edge=bool(case["reply_id"]),
                open_group=not case["reply_id"] and not mention_targets,
            )
            accepted.append(
                ledger.record_inbound(
                    envelope(
                        message_id=f"accepted-{case['name']}",
                        sender_key=f"{self.scope}|user:{case['sender_id']}",
                        sender_id=case["sender_id"],
                        reply_to_message_id=(
                            f"prior-{case['name']}" if case["reply_id"] else ""
                        ),
                        reply_to_sender_id=case["reply_id"],
                        timestamp=case_timestamp,
                    ),
                    case["body"],
                    identity_metadata=metadata,
                )
            )
            rows.append(
                self._row(
                    row_id=43 + index,
                    sender_id=case["sender_id"],
                    sender_name=f"真实发言者{index + 1}",
                    timestamp=case_timestamp,
                    message=case["message"],
                )
            )

        self.assertEqual([record.content for record in accepted], [case["body"] for case in cases])
        result = await read_astrbot_group_history(
            context=SimpleNamespace(message_history_manager=_HistoryManager(rows)),
            event=_HistoryEvent(current_id=50),
            accepted_records=accepted,
            scope_key=self.scope,
            session_id="g",
        )

        self.assertIs(result.status, PlatformGroupHistoryStatus.VERIFIED)
        self.assertEqual([record.content for record in result.records], [case["body"] for case in cases])
        self.assertEqual(
            [record.display_name for record in result.records],
            [f"真实发言者{index + 1}" for index in range(len(cases))],
        )
        self.assertEqual(
            [record.addressing_status for record in result.records],
            ["reply", "mentions", "mentions", "reply_and_mentions"],
        )
        self.assertEqual(result.records[0].referenced_display_name, "真实引用者")
        self.assertEqual(
            [target.display.value for target in result.records[1].mention_targets],
            ["真实提及者"],
        )
        self.assertEqual(
            [target.display.value for target in result.records[2].mention_targets],
            ["多目标甲", "多目标乙"],
        )
        self.assertEqual(result.records[3].referenced_display_name, "组合引用者")
        self.assertEqual(
            [target.display.value for target in result.records[3].mention_targets],
            ["组合目标甲", "组合目标乙"],
        )
        rendered = repr(result.records)
        for hidden in (
            "reply-target",
            "single-target",
            "multi-target-a",
            "multi-target-b",
            "reply-multi-target",
            "combo-target-a",
            "combo-target-b",
        ):
            self.assertNotIn(hidden, "".join(record.content for record in result.records))

    async def test_platform_relation_names_bind_only_to_verified_target_keys(self):
        timestamp = 1_765_000_400.0
        ledger = ConversationLedger()
        accepted = []
        rows = []

        def target(sender_id: str, display_name: str = ""):
            return build_participant_display(
                f"{self.scope}|user:{sender_id}",
                display_name=display_name,
                display_name_source=(
                    "mention_component" if display_name else "unavailable"
                ),
            )

        def add_case(
            *,
            index: int,
            sender_id: str,
            body: str,
            metadata: InboundIdentityMetadata,
            message: list[dict[str, object]],
            reply_to_message_id: str = "",
            reply_to_sender_id: str = "",
            accepted_sender_name: str = "",
            row_sender_name: str = "平台发言者",
        ) -> None:
            accepted.append(
                ledger.record_inbound(
                    envelope(
                        message_id=f"relation-message-{index}",
                        sender_key=f"{self.scope}|user:{sender_id}",
                        sender_id=sender_id,
                        reply_to_message_id=reply_to_message_id,
                        reply_to_sender_id=reply_to_sender_id,
                        timestamp=timestamp + index * 10,
                    ),
                    body,
                    identity_metadata=(
                        build_inbound_identity_metadata(
                            display_name=accepted_sender_name,
                            display_name_source=(
                                "event_sender"
                                if accepted_sender_name
                                else "unavailable"
                            ),
                            referenced_sender=metadata.referenced_sender,
                            mention_targets=metadata.mention_targets,
                            has_reply_edge=bool(reply_to_message_id),
                            open_group=(
                                not reply_to_message_id
                                and not metadata.mention_targets
                            ),
                        )
                    ),
                )
            )
            rows.append(
                self._row(
                    row_id=60 + index,
                    sender_id=sender_id,
                    sender_name=row_sender_name,
                    timestamp=timestamp + index * 10,
                    message=[*message, {"type": "plain", "text": body}],
                )
            )

        add_case(
            index=0,
            sender_id="relation-reply-speaker",
            body="Reply 目标换人仍只接纳正文",
            metadata=build_inbound_identity_metadata(
                referenced_sender=target("relation-reply-target-a"),
                has_reply_edge=True,
            ),
            reply_to_message_id="prior-relation-reply",
            reply_to_sender_id="relation-reply-target-a",
            message=[
                {
                    "type": "reply",
                    "sender_id": "relation-reply-target-b",
                    "sender_name": "不得错绑的 Reply 姓名",
                    "text": "引用正文",
                }
            ],
        )
        add_case(
            index=1,
            sender_id="relation-set-speaker",
            body="同数量目标集合变化",
            metadata=build_inbound_identity_metadata(
                mention_targets=(target("relation-set-a"), target("relation-set-b")),
            ),
            message=[
                {"type": "at", "qq": "relation-set-a", "name": "集合目标甲"},
                {"type": "at", "qq": "relation-set-c", "name": "错误集合目标丙"},
            ],
        )
        add_case(
            index=2,
            sender_id="relation-order-speaker",
            body="相同目标集合交换顺序",
            metadata=build_inbound_identity_metadata(
                mention_targets=(
                    target("relation-order-a"),
                    target("relation-order-b"),
                ),
            ),
            message=[
                {"type": "at", "qq": "relation-order-b", "name": "顺序目标乙"},
                {"type": "at", "qq": "relation-order-a", "name": "顺序目标甲"},
            ],
        )
        add_case(
            index=3,
            sender_id="relation-same-name-speaker",
            body="同名仍按稳定目标区分",
            metadata=build_inbound_identity_metadata(
                mention_targets=(
                    target("relation-same-name-a"),
                    target("relation-same-name-b"),
                ),
            ),
            message=[
                {
                    "type": "at",
                    "qq": "relation-same-name-b",
                    "name": "同名受话者",
                },
                {
                    "type": "at",
                    "qq": "relation-same-name-a",
                    "name": "同名受话者",
                },
            ],
        )
        add_case(
            index=4,
            sender_id="relation-trusted-speaker",
            body="可信姓名不被平台冲突覆盖",
            metadata=build_inbound_identity_metadata(
                mention_targets=(target("relation-trusted-target", "可信目标"),),
            ),
            message=[
                {
                    "type": "at",
                    "qq": "relation-trusted-target",
                    "name": "平台冲突目标",
                }
            ],
            accepted_sender_name="可信发言者",
            row_sender_name="平台冲突发言者",
        )
        add_case(
            index=5,
            sender_id="relation-verified-speaker",
            body="有目标 ID 才能补名",
            metadata=build_inbound_identity_metadata(
                mention_targets=(target("relation-verified-target"),),
            ),
            message=[
                {
                    "type": "at",
                    "qq": "relation-verified-target",
                    "name": "可验证目标名",
                }
            ],
        )
        add_case(
            index=6,
            sender_id="relation-missing-id-speaker",
            body="缺目标 ID 不猜姓名",
            metadata=build_inbound_identity_metadata(
                mention_targets=(target("relation-missing-id-target"),),
            ),
            message=[{"type": "at", "name": "不得按位置猜的姓名"}],
        )
        add_case(
            index=7,
            sender_id="relation-trusted-reply-speaker",
            body="可信 Reply 姓名不被 row 冲突覆盖",
            metadata=build_inbound_identity_metadata(
                referenced_sender=build_participant_display(
                    f"{self.scope}|user:relation-trusted-reply-target",
                    display_name="可信 Reply 目标",
                    display_name_source="reply_component",
                ),
                has_reply_edge=True,
            ),
            reply_to_message_id="prior-trusted-reply",
            reply_to_sender_id="relation-trusted-reply-target",
            message=[
                {
                    "type": "reply",
                    "sender_id": "relation-trusted-reply-target",
                    "sender_name": "平台冲突 Reply 目标",
                    "text": "引用正文",
                }
            ],
        )

        result = await read_astrbot_group_history(
            context=SimpleNamespace(message_history_manager=_HistoryManager(rows)),
            event=_HistoryEvent(current_id=90),
            accepted_records=accepted,
            scope_key=self.scope,
            session_id="g",
        )

        self.assertIs(result.status, PlatformGroupHistoryStatus.VERIFIED)
        self.assertEqual(
            result.matched_ledger_message_ids,
            tuple(f"relation-message-{index}" for index in range(8)),
        )
        by_source = {record.source_id: record for record in result.records}
        self.assertEqual(
            by_source["relation-message-0"].identity_metadata.referenced_sender.sender_key,
            f"{self.scope}|user:relation-reply-target-a",
        )
        self.assertEqual(
            by_source["relation-message-0"].referenced_display_name,
            "",
        )
        self.assertEqual(
            [target.display.value for target in by_source["relation-message-1"].mention_targets],
            ["集合目标甲", ""],
        )
        self.assertEqual(
            [target.display.value for target in by_source["relation-message-2"].mention_targets],
            ["顺序目标甲", "顺序目标乙"],
        )
        same_name_targets = by_source["relation-message-3"].mention_targets
        self.assertEqual(
            [target.sender_key for target in same_name_targets],
            [
                f"{self.scope}|user:relation-same-name-a",
                f"{self.scope}|user:relation-same-name-b",
            ],
        )
        self.assertEqual(
            [target.display.value for target in same_name_targets],
            ["同名受话者", "同名受话者"],
        )
        trusted = by_source["relation-message-4"]
        self.assertEqual(trusted.display_name, "可信发言者")
        self.assertEqual(trusted.mention_targets[0].display.value, "可信目标")
        self.assertEqual(
            by_source["relation-message-5"].mention_targets[0].display.value,
            "可验证目标名",
        )
        self.assertEqual(
            by_source["relation-message-6"].mention_targets[0].display.status,
            "unavailable",
        )
        self.assertEqual(
            by_source["relation-message-7"].referenced_display_name,
            "可信 Reply 目标",
        )
        self.assertEqual(
            [record.content for record in result.records],
            [
                "Reply 目标换人仍只接纳正文",
                "同数量目标集合变化",
                "相同目标集合交换顺序",
                "同名仍按稳定目标区分",
                "可信姓名不被平台冲突覆盖",
                "有目标 ID 才能补名",
                "缺目标 ID 不猜姓名",
                "可信 Reply 姓名不被 row 冲突覆盖",
            ],
        )

    async def test_platform_message_admission_requires_one_source_candidate(self):
        timestamp = 1_765_000_500.0
        sender_id = "relation-ambiguous-speaker"
        body = "同 sender body window 不能靠最近时间或关系数量猜"
        ledger = ConversationLedger()
        accepted = []
        for index, target_id in enumerate(
            ("relation-ambiguous-target-a", "relation-ambiguous-target-b")
        ):
            accepted.append(
                ledger.record_inbound(
                    envelope(
                        message_id=f"relation-ambiguous-{index}",
                        sender_key=f"{self.scope}|user:{sender_id}",
                        sender_id=sender_id,
                        reply_to_message_id="",
                        reply_to_sender_id="",
                        timestamp=timestamp + index * 20,
                    ),
                    body,
                    identity_metadata=build_inbound_identity_metadata(
                        mention_targets=(build_participant_display(
                            f"{self.scope}|user:{target_id}"
                        ),),
                    ),
                )
            )
        row = self._row(
            row_id=80,
            sender_id=sender_id,
            sender_name="歧义发言者",
            timestamp=timestamp + 1,
            message=[
                {
                    "type": "at",
                    "qq": "relation-ambiguous-target-a",
                    "name": "最近关系目标甲",
                },
                {"type": "plain", "text": body},
            ],
        )

        result = await read_astrbot_group_history(
            context=SimpleNamespace(message_history_manager=_HistoryManager([row])),
            event=_HistoryEvent(current_id=90),
            accepted_records=accepted,
            scope_key=self.scope,
            session_id="g",
        )

        self.assertIs(result.status, PlatformGroupHistoryStatus.EMPTY)
        self.assertEqual(result.records, ())
        self.assertEqual(result.matched_ledger_message_ids, ())

    async def test_missing_platform_display_name_keeps_body_with_unavailable_metadata(self):
        timestamp = 1_765_000_200.0
        row = self._row(
            row_id=44,
            sender_id="peer-missing",
            sender_name=None,
            timestamp=timestamp,
            message=[{"type": "plain", "text": "缺名仍保留正文"}],
        )
        accepted = [
            accepted_history_record(
                sequence=4,
                sender_id="peer-missing",
                content="缺名仍保留正文",
                timestamp=timestamp,
            )
        ]

        result = await read_astrbot_group_history(
            context=SimpleNamespace(message_history_manager=_HistoryManager([row])),
            event=_HistoryEvent(current_id=50),
            accepted_records=accepted,
            scope_key=self.scope,
            session_id="g",
        )

        self.assertIs(result.status, PlatformGroupHistoryStatus.VERIFIED)
        self.assertEqual(result.records[0].content, "缺名仍保留正文")
        self.assertEqual(result.records[0].display_name, "")
        self.assertEqual(result.records[0].display_name_status, "unavailable")
        self.assertEqual(result.records[0].addressing_status, "unspecified")

    async def test_structured_admission_is_exact_and_fails_closed_on_missing_evidence(self):
        timestamp = 1_765_000_250.0
        sender_id = "exact-platform-sender"
        target_id = "exact-platform-target"
        first_body = "第一条纯正文只靠来源证据接纳"
        second_body = "第二条纯正文不能重用 accepted message"
        metadata = build_inbound_identity_metadata(
            mention_targets=(
                build_participant_display(f"{self.scope}|user:{target_id}"),
            ),
        )
        ledger = ConversationLedger()
        first = ledger.record_inbound(
            envelope(
                message_id="exact-message-first",
                sender_key=f"{self.scope}|user:{sender_id}",
                sender_id=sender_id,
                reply_to_message_id="",
                reply_to_sender_id="",
                timestamp=timestamp,
            ),
            first_body,
            identity_metadata=metadata,
        )
        second = ledger.record_inbound(
            envelope(
                message_id="exact-message-second",
                sender_key=f"{self.scope}|user:{sender_id}",
                sender_id=sender_id,
                reply_to_message_id="",
                reply_to_sender_id="",
                timestamp=timestamp + 20,
            ),
            second_body,
            identity_metadata=metadata,
        )
        other_scope = "platform:p|bot:b|group:other"
        other_scope_metadata = build_inbound_identity_metadata(
            mention_targets=(
                build_participant_display(f"{other_scope}|user:{target_id}"),
            ),
        )
        other = ConversationLedger().record_inbound(
            envelope(
                scope_key=other_scope,
                message_id="other-scope-message",
                sender_key=f"{other_scope}|user:{sender_id}",
                sender_id=sender_id,
                reply_to_message_id="",
                reply_to_sender_id="",
                timestamp=timestamp,
            ),
            first_body,
            identity_metadata=other_scope_metadata,
        )
        rows = [
            self._row(
                row_id=46,
                sender_id="different-sender",
                sender_name="错误发言者",
                timestamp=timestamp,
                message=[
                    {"type": "at", "qq": target_id, "name": "真实目标"},
                    {"type": "plain", "text": first_body},
                ],
            ),
            self._row(
                row_id=47,
                sender_id=sender_id,
                sender_name="精确发言者",
                timestamp=timestamp,
                message=[
                    {"type": "at", "qq": target_id, "name": 12345},
                    {"type": "plain", "text": first_body},
                ],
            ),
            self._row(
                row_id=48,
                sender_id=sender_id,
                sender_name="精确发言者",
                timestamp=timestamp,
                message=[{"type": "plain", "text": first_body}],
            ),
            self._row(
                row_id=49,
                sender_id=sender_id,
                sender_name="精确发言者",
                timestamp=timestamp + 20,
                message=[
                    {"type": "at", "qq": target_id, "name": "真实目标"},
                    {"type": "plain", "text": second_body},
                ],
            ),
            self._row(
                row_id=50,
                sender_id=sender_id,
                sender_name="不得重用的行",
                timestamp=timestamp + 21,
                message=[
                    {"type": "at", "qq": target_id, "name": "真实目标"},
                    {"type": "plain", "text": second_body},
                ],
            ),
        ]

        result = await read_astrbot_group_history(
            context=SimpleNamespace(message_history_manager=_HistoryManager(rows)),
            event=_HistoryEvent(current_id=60),
            accepted_records=(first, second, other),
            scope_key=self.scope,
            session_id="g",
        )

        self.assertIs(result.status, PlatformGroupHistoryStatus.VERIFIED)
        self.assertEqual(
            result.matched_ledger_message_ids,
            ("exact-message-first", "exact-message-second"),
        )
        self.assertEqual(
            [record.content for record in result.records],
            [first_body, second_body],
        )
        self.assertEqual(
            [record.display_name for record in result.records],
            ["精确发言者", "精确发言者"],
        )
        self.assertEqual(
            result.records[0].mention_targets[0].display.status,
            "unavailable",
        )
        self.assertEqual(
            result.records[1].mention_targets[0].display.value,
            "真实目标",
        )

        ambiguous = ConversationLedger()
        ambiguous_body = "重复正文不能按时间或关系数量猜"
        ambiguous_records = tuple(
            ambiguous.record_inbound(
                envelope(
                    message_id=f"ambiguous-{index}",
                    sender_key=f"{self.scope}|user:{sender_id}",
                    sender_id=sender_id,
                    reply_to_message_id="",
                    reply_to_sender_id="",
                    timestamp=timestamp + 100,
                ),
                ambiguous_body,
                identity_metadata=metadata,
            )
            for index in range(2)
        )
        ambiguous_result = await read_astrbot_group_history(
            context=SimpleNamespace(
                message_history_manager=_HistoryManager(
                    [
                        self._row(
                            row_id=52,
                            sender_id=sender_id,
                            sender_name="不应猜测的发言者",
                            timestamp=timestamp + 100,
                            message=[
                                {
                                    "type": "at",
                                    "qq": target_id,
                                    "name": "真实目标",
                                },
                                {"type": "plain", "text": ambiguous_body},
                            ],
                        )
                    ]
                )
            ),
            event=_HistoryEvent(current_id=60),
            accepted_records=ambiguous_records,
            scope_key=self.scope,
            session_id="g",
        )
        self.assertIs(ambiguous_result.status, PlatformGroupHistoryStatus.EMPTY)
        self.assertEqual(ambiguous_result.records, ())

    async def test_platform_account_id_fallback_is_not_a_display_name(self):
        timestamp = 1_765_000_300.0
        row = self._row(
            row_id=45,
            sender_id="raw-peer-id-45",
            sender_name="raw-peer-id-45",
            timestamp=timestamp,
            message=[
                {
                    "type": "reply",
                    "sender_id": "raw-reply-id-46",
                    "sender_name": "raw-reply-id-46",
                    "text": "引用正文",
                },
                {
                    "type": "at",
                    "qq": "raw-at-id-47",
                    "name": "raw-at-id-47",
                },
                {"type": "plain", "text": "账号回退仍保留正文"},
            ],
        )
        accepted = [
            accepted_history_record(
                sequence=5,
                sender_id="raw-peer-id-45",
                content="账号回退仍保留正文",
                timestamp=timestamp,
                reply_to_message_id="prior-account-message",
                referenced_sender_key=f"{self.scope}|user:raw-reply-id-46",
                identity_metadata=build_inbound_identity_metadata(
                    referenced_sender=build_participant_display(
                        f"{self.scope}|user:raw-reply-id-46",
                    ),
                    mention_targets=(
                        build_participant_display(
                            f"{self.scope}|user:raw-at-id-47",
                        ),
                    ),
                    has_reply_edge=True,
                ),
            )
        ]

        result = await read_astrbot_group_history(
            context=SimpleNamespace(message_history_manager=_HistoryManager([row])),
            event=_HistoryEvent(current_id=50),
            accepted_records=accepted,
            scope_key=self.scope,
            session_id="g",
        )

        self.assertIs(result.status, PlatformGroupHistoryStatus.VERIFIED)
        self.assertEqual(result.records[0].content, "账号回退仍保留正文")
        self.assertEqual(result.records[0].display_name, "")
        self.assertEqual(result.records[0].display_name_status, "unavailable")
        self.assertEqual(result.records[0].referenced_display_name, "")
        self.assertEqual(
            result.records[0].mention_targets[0].display.status,
            "unavailable",
        )
        self.assertEqual(result.records[0].addressing_status, "reply_and_mentions")


class ConversationLedgerTests(unittest.TestCase):
    def test_inbound_keeps_real_display_reply_and_multiple_mentions_outside_content(self):
        ledger = ConversationLedger()
        turn = envelope()
        metadata = build_inbound_identity_metadata(
            display_name="发言测试名",
            display_name_source="event_sender",
            referenced_sender=build_participant_display(
                f"{turn.scope_key}|user:guest-b",
                display_name="被回复测试名",
                display_name_source="reply_component",
            ),
            mention_targets=(
                build_participant_display(
                    f"{turn.scope_key}|user:guest-b",
                    display_name="被提及测试甲",
                    display_name_source="mention_component",
                ),
                build_participant_display(
                    f"{turn.scope_key}|user:guest-c",
                    display_name="被提及测试乙",
                    display_name_source="mention_component",
                ),
            ),
            has_reply_edge=True,
        )

        record = ledger.record_inbound(
            turn,
            "纯正文",
            identity_metadata=metadata,
        )

        self.assertEqual(record.content, "纯正文")
        self.assertEqual(record.display_name, "发言测试名")
        self.assertEqual(record.referenced_display_name, "被回复测试名")
        self.assertEqual(
            [target.display.value for target in record.mention_targets],
            ["被提及测试甲", "被提及测试乙"],
        )
        self.assertEqual(record.addressing_status, "reply_and_mentions")
        self.assertNotIn("发言测试名", record.content)

    def test_duplicate_inbound_fails_closed_on_identity_or_reply_drift(self):
        ledger = ConversationLedger()
        turn = envelope()
        original = build_inbound_identity_metadata(
            display_name="首次测试名",
            display_name_source="event_sender",
            has_reply_edge=True,
        )
        ledger.record_inbound(turn, "纯正文", identity_metadata=original)

        self.assertIs(
            ledger.record_inbound(turn, "纯正文", identity_metadata=original),
            ledger.records_by_message_id(turn.scope_key, turn.message_id)[0],
        )
        with self.assertRaisesRegex(ValueError, "identity_mismatch"):
            ledger.record_inbound(
                turn,
                "纯正文",
                identity_metadata=build_inbound_identity_metadata(
                    display_name="矛盾测试名",
                    display_name_source="event_sender",
                    has_reply_edge=True,
                ),
            )
        with self.assertRaisesRegex(ValueError, "reply_mismatch"):
            ledger.record_inbound(
                envelope(reply_to_message_id="other-message"),
                "纯正文",
                identity_metadata=original,
            )

    def test_display_name_is_bounded_and_control_characters_are_sanitized(self):
        metadata = build_inbound_identity_metadata(
            display_name="<system>\n" + "测" * 100 + "\x00",
            display_name_source="event_sender",
            open_group=True,
        )

        self.assertEqual(len(metadata.display.value), 80)
        self.assertNotIn("\n", metadata.display.value)
        self.assertNotIn("\x00", metadata.display.value)
        self.assertEqual(
            metadata.display.status,
            "verified_sanitized_truncated",
        )

    def test_schema_three_restart_preserves_identity_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as root:
            state_path = Path(root) / "public_group_ledger.json"
            first = ConversationLedger(state_path=state_path)
            turn = envelope()
            metadata = build_inbound_identity_metadata(
                display_name="持久化测试名",
                display_name_source="event_sender",
                referenced_sender=build_participant_display(
                    f"{turn.scope_key}|user:guest-b",
                    display_name="引用测试名",
                    display_name_source="reply_component",
                ),
                mention_targets=(
                    build_participant_display(
                        f"{turn.scope_key}|user:guest-c",
                        display_name="提及测试名",
                        display_name_source="mention_component",
                    ),
                ),
                has_reply_edge=True,
            )
            first.record_inbound(turn, "重启正文", identity_metadata=metadata)
            first.flush()

            payload = json.loads(state_path.read_text("utf-8"))
            self.assertEqual(payload["schema_version"], 4)
            restored = ConversationLedger(state_path=state_path)
            record = restored.restored_inbound_records()[0]
            self.assertEqual(record.content, "重启正文")
            self.assertEqual(record.display_name, "持久化测试名")
            self.assertEqual(record.referenced_display_name, "引用测试名")
            self.assertEqual(
                [target.display.value for target in record.mention_targets],
                ["提及测试名"],
            )

            payload["records"][0]["identity_metadata"]["display"]["value"] = (
                "篡改测试名"
            )
            state_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            rejected = ConversationLedger(state_path=state_path)
            self.assertEqual(rejected.restored_inbound_records(), ())
            self.assertEqual(
                rejected.persistence_metadata()["persistence_failure_code"],
                "state_invalid",
            )

    def test_valid_schema_one_and_two_migrate_without_inventing_names(self):
        base = {
            "scope_key": "platform:p|bot:b|group:g",
            "session_id": "g",
            "message_id": "legacy-record",
            "sender_key": "platform:p|bot:b|group:g|user:guest-a",
            "reply_to_message_id": "legacy-replied-message",
            "timestamp": 1_765_000_000.0,
            "content": "旧状态正文",
            "content_digest": ledger_content_digest("旧状态正文"),
            "source_id": "legacy-record",
            "attribution_status": "verified",
        }
        target = {
            "platform_id": "p",
            "bot_id": "b",
            "group_id": "g",
            "unified_msg_origin": "origin:g",
        }
        for schema_version in (1, 2):
            with self.subTest(schema_version=schema_version), tempfile.TemporaryDirectory() as root:
                state_path = Path(root) / "public_group_ledger.json"
                bootstrap = ConversationLedger(state_path=state_path)
                secret = (Path(root) / bootstrap._SECRET_FILENAME).read_bytes()
                row = dict(base)
                if schema_version == 2:
                    row.update(target)
                payload = {"schema_version": schema_version, "records": [row]}
                if schema_version == 2:
                    rows_encoded = json.dumps(
                        payload["records"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    payload["state_mac"] = hmac.new(
                        secret,
                        b"public-group-ledger-v2\x00" + rows_encoded,
                        hashlib.sha256,
                    ).hexdigest()
                state_path.write_text(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )

                restored = ConversationLedger(state_path=state_path)
                record = restored.restored_inbound_records()[0]
                self.assertEqual(record.content, "旧状态正文")
                self.assertEqual(record.display_name_status, "unavailable")
                self.assertEqual(
                    record.addressing_status,
                    "reply_target_unavailable",
                )
                self.assertEqual(
                    record.reply_to_message_id,
                    "legacy-replied-message",
                )
                restored.flush()
                self.assertEqual(
                    json.loads(state_path.read_text("utf-8"))["schema_version"],
                    4,
                )

    def test_persisted_group_inbound_survives_restart_with_bounded_tail(self):
        with tempfile.TemporaryDirectory() as root:
            state_path = Path(root) / "public_group_ledger.json"
            first = ConversationLedger(
                max_records_per_scope=16,
                state_path=state_path,
                max_persisted_records_per_scope=8,
            )
            for index in range(10):
                first.record_inbound(
                    envelope(message_id=f"persist-{index}"),
                    f"公开群聊-{index}",
                )
            first.record_outbound(
                scope_key="platform:p|bot:b|group:g",
                session_id="g",
                message_id="private-bot-output",
                bot_sender_key="platform:p|bot:b|group:g|user:bot",
                target_sender_key="platform:p|bot:b|group:g|user:guest-a",
                reply_to_message_id="persist-9",
                timestamp=1_765_000_001,
                content="不应作为可恢复公开入站消息持久化",
            )
            first.flush()

            restored = ConversationLedger(
                max_records_per_scope=16,
                state_path=state_path,
                max_persisted_records_per_scope=8,
            )
            records = restored.records("platform:p|bot:b|group:g")

            self.assertEqual(len(records), 8)
            self.assertEqual(
                [record.message_id for record in records],
                [f"persist-{index}" for index in range(2, 10)],
            )
            self.assertTrue(
                all(record.source_kind is LedgerSourceKind.INBOUND for record in records)
            )
            self.assertNotIn("private-bot-output", state_path.read_text("utf-8"))
            self.assertTrue(restored.persistence_metadata()["persistence_loaded"])

    def test_corrupt_persisted_ledger_fails_closed_without_exposing_payload(self):
        with tempfile.TemporaryDirectory() as root:
            state_path = Path(root) / "public_group_ledger.json"
            state_path.write_text(
                json.dumps({"schema_version": 1, "records": [{"content": "marker"}]}),
                encoding="utf-8",
            )

            restored = ConversationLedger(state_path=state_path)

            self.assertEqual(restored.records("platform:p|bot:b|group:g"), ())
            metadata = restored.persistence_metadata()
            self.assertFalse(metadata["persistence_loaded"])
            self.assertEqual(metadata["persistence_failure_code"], "state_invalid")
            self.assertNotIn("marker", repr(metadata))

    def test_four_source_kinds_remain_typed(self):
        ledger = ConversationLedger()
        turn = envelope()
        inbound = ledger.record_inbound(turn, "当前问题")
        reference = ledger.record_reference(
            turn,
            referenced_content="被引用内容",
            referenced_sender_key="platform:p|bot:b|group:g|user:guest-b",
        )
        outbound = ledger.record_outbound(
            scope_key=turn.scope_key,
            session_id=turn.session_id,
            message_id="bot-msg",
            bot_sender_key="platform:p|bot:b|group:g|user:bot",
            target_sender_key=turn.sender_key,
            reply_to_message_id=turn.message_id,
            timestamp=1_765_000_001,
            content="机器人回复",
        )
        tool = ledger.record_tool_result(
            scope_key=turn.scope_key,
            session_id=turn.session_id,
            source_id="call-1",
            tool_name="public_web_read",
            timestamp=1_765_000_000.5,
            content="公开资料结果",
        )

        self.assertEqual(
            [record.source_kind for record in ledger.records(turn.scope_key)],
            [
                LedgerSourceKind.INBOUND,
                LedgerSourceKind.REFERENCE,
                LedgerSourceKind.OUTBOUND,
                LedgerSourceKind.TOOL_RESULT,
            ],
        )
        self.assertEqual(inbound.role, LedgerRole.USER)
        self.assertEqual(reference.role, LedgerRole.CONTEXT)
        self.assertEqual(outbound.role, LedgerRole.ASSISTANT)
        self.assertEqual(tool.role, LedgerRole.TOOL)

    def test_reference_keeps_actor_and_referenced_sender_distinct(self):
        ledger = ConversationLedger()
        turn = envelope()

        record = ledger.record_reference(
            turn,
            referenced_content="旧消息",
            referenced_sender_key="platform:p|bot:b|group:g|user:owner-b",
        )

        self.assertEqual(record.sender_key, turn.sender_key)
        self.assertNotEqual(record.sender_key, record.referenced_sender_key)
        self.assertEqual(record.reply_to_message_id, "msg-old")
        self.assertEqual(record.source_id, "msg-old")

    def test_tool_result_cannot_become_assistant_history(self):
        ledger = ConversationLedger()
        record = ledger.record_tool_result(
            scope_key="platform:p|bot:b|group:g",
            session_id="g",
            source_id="call-1",
            tool_name="search",
            timestamp=1,
            content="result",
        )

        self.assertEqual(record.source_kind, LedgerSourceKind.TOOL_RESULT)
        self.assertEqual(record.role, LedgerRole.TOOL)
        self.assertEqual(record.sender_key, "")

    def test_scope_buckets_are_separate_and_bounded(self):
        ledger = ConversationLedger(max_records_per_scope=8)
        for index in range(10):
            ledger.record_inbound(
                envelope(message_id=f"a-{index}"),
                f"message-{index}",
            )
        ledger.record_inbound(
            envelope(
                scope_key="platform:p|bot:b|group:other",
                message_id="other-1",
                sender_key="platform:p|bot:b|group:other|user:guest-a",
            ),
            "other",
        )

        self.assertEqual(len(ledger.records("platform:p|bot:b|group:g")), 8)
        self.assertEqual(
            [record.message_id for record in ledger.records("platform:p|bot:b|group:g")],
            [f"a-{index}" for index in range(2, 10)],
        )
        self.assertEqual(len(ledger.records("platform:p|bot:b|group:other")), 1)

    def test_unscoped_records_fail_closed(self):
        ledger = ConversationLedger()

        with self.assertRaisesRegex(ValueError, "scope_key"):
            ledger.record_inbound(envelope(scope_key=""), "不能进入共享桶")

    def test_content_digest_is_stable_but_not_plaintext(self):
        digest = ledger_content_digest("一段测试内容")

        self.assertEqual(digest, ledger_content_digest("一段测试内容"))
        self.assertNotIn("测试内容", digest)
        self.assertEqual(len(digest), 64)

    def test_message_reply_and_sender_indexes_are_exact(self):
        ledger = ConversationLedger()
        turn = envelope()
        inbound = ledger.record_inbound(turn, "当前问题")
        reference = ledger.record_reference(turn, referenced_content="引用")
        outbound = ledger.record_outbound(
            scope_key=turn.scope_key,
            session_id=turn.session_id,
            message_id="bot-msg",
            bot_sender_key="platform:p|bot:b|group:g|user:bot",
            target_sender_key=turn.sender_key,
            reply_to_message_id=turn.message_id,
            timestamp=1_765_000_001,
            content="回复",
        )

        self.assertEqual(
            ledger.records_by_message_id(turn.scope_key, turn.message_id),
            (inbound, reference),
        )
        self.assertEqual(
            ledger.records_replying_to(turn.scope_key, turn.message_id),
            (outbound,),
        )
        self.assertEqual(
            ledger.records_by_sender(turn.scope_key, turn.sender_key),
            (inbound, reference),
        )

    def test_indexes_never_cross_scope(self):
        ledger = ConversationLedger()
        first = ledger.record_inbound(envelope(), "scope-a")
        other_scope = "platform:p|bot:b|group:other"
        second = ledger.record_inbound(
            envelope(
                scope_key=other_scope,
                sender_key=f"{other_scope}|user:guest-a",
            ),
            "scope-b",
        )

        self.assertEqual(
            ledger.records_by_message_id(first.scope_key, "msg-current"),
            (first,),
        )
        self.assertEqual(
            ledger.records_by_message_id(second.scope_key, "msg-current"),
            (second,),
        )

    def test_pruned_records_are_removed_from_all_indexes(self):
        ledger = ConversationLedger(max_records_per_scope=8)
        records = []
        for index in range(9):
            records.append(
                ledger.record_inbound(
                    envelope(message_id=f"msg-{index}"),
                    f"content-{index}",
                )
            )

        self.assertEqual(
            ledger.records_by_message_id(records[0].scope_key, "msg-0"),
            (),
        )
        self.assertNotIn(
            records[0],
            ledger.records_by_sender(records[0].scope_key, records[0].sender_key),
        )
        self.assertEqual(
            ledger.records_by_message_id(records[-1].scope_key, "msg-8"),
            (records[-1],),
        )


class LegacyHistoryAdapterTests(unittest.TestCase):
    scope = "platform:p|bot:b|group:g"
    guest_a = f"{scope}|user:guest-a"
    guest_b = f"{scope}|user:guest-b"

    def test_unknown_assistant_is_not_claimed_by_adjacent_user(self):
        records = adapt_astrbot_history(
            [
                {"role": "user", "content": "A 的问题", "sender_id": "guest-a"},
                {"role": "assistant", "content": "没有目标元数据的旧回复"},
                {"role": "user", "content": "B 的问题", "sender_id": "guest-b"},
            ],
            scope_key=self.scope,
            session_id="g",
            group_id="g",
            bot_sender_key=f"{self.scope}|user:bot",
        )

        self.assertEqual(records[1].attribution_status, "unknown_target")
        self.assertEqual(records[1].target_sender_key, "")
        self.assertEqual(
            [record.content for record in records_for_sender_thread(records, self.guest_b)],
            ["B 的问题"],
        )

    def test_explicit_assistant_target_enters_only_that_sender_thread(self):
        records = adapt_astrbot_history(
            [
                {"role": "user", "content": "A", "sender_id": "guest-a"},
                {
                    "role": "assistant",
                    "content": "明确回复 A",
                    "target_sender_id": "guest-a",
                },
                {"role": "user", "content": "B", "sender_id": "guest-b"},
            ],
            scope_key=self.scope,
            session_id="g",
            group_id="g",
        )

        self.assertEqual(records[1].attribution_status, "verified_target")
        self.assertEqual(
            [record.content for record in records_for_sender_thread(records, self.guest_a)],
            ["A", "明确回复 A"],
        )
        self.assertEqual(
            [record.content for record in records_for_sender_thread(records, self.guest_b)],
            ["B"],
        )

    def test_unlabelled_user_and_wrong_group_are_not_attributed(self):
        records = adapt_astrbot_history(
            [
                {"role": "user", "content": "无 sender"},
                {
                    "role": "user",
                    "content": "另一个群",
                    "sender_id": "guest-a",
                    "group_id": "other-group",
                },
            ],
            scope_key=self.scope,
            session_id="g",
            group_id="g",
        )

        self.assertEqual(records[0].attribution_status, "unknown_sender")
        self.assertEqual(records[1].attribution_status, "scope_mismatch")
        self.assertEqual(records_for_sender_thread(records, self.guest_a), ())

    def test_metadata_fields_are_read_without_modifying_source(self):
        source = {
            "role": "assistant",
            "content": "旧回复",
            "metadata": {
                "target_sender_id": "guest-b",
                "message_id": "old-bot-msg",
            },
        }
        snapshot = repr(source)

        records = adapt_astrbot_history(
            [source],
            scope_key=self.scope,
            session_id="g",
        )

        self.assertEqual(records[0].target_sender_key, self.guest_b)
        self.assertEqual(records[0].message_id, "old-bot-msg")
        self.assertEqual(repr(source), snapshot)


if __name__ == "__main__":
    unittest.main()
