from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace

from astrbot_plugin_shio.core.contracts import (
    DecisionBinding,
    EvidenceConsumer,
    ExternalPluginEvidence,
    HistoryVisibility,
    PluginEvidenceKind,
    PluginEvidenceStatus,
)
from astrbot_plugin_shio.core.conversation_ledger import (
    ConversationLedger,
    LedgerRecord,
    LedgerRole,
    LedgerSourceKind,
    ledger_content_digest,
)
from astrbot_plugin_shio.core.history_normalizer import (
    HistoryDisposition,
    HistoryNormalizationContext,
    HistoryOrigin,
    PluginHistoryCandidate,
    normalize_assistant_history,
    normalize_plugin_history,
)
from astrbot_plugin_shio.core.send_receipt import (
    SegmentSendStatus,
    SendSegmentAttempt,
    SentReplyRecord,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _binding(
    *,
    sender: str = "scope:group-a|user:guest-a",
    message: str = "current-message-a",
    scope: str = "scope:group-a",
    session: str = "session-a",
    revision: int = 1,
) -> DecisionBinding:
    return DecisionBinding(
        scope_key=scope,
        session_id=session,
        current_message_id=message,
        current_sender_key=sender,
        current_content_digest=_digest(f"content:{message}"),
        conversation_revision=revision,
        generation_epoch=revision,
        trace_id=f"{revision:032x}",
    )


def _sent_reply(
    *,
    target_sender: str,
    target_message: str,
    text: str = "这条确实发出去了。",
    status: SegmentSendStatus = SegmentSendStatus.SUCCEEDED,
    reply_id: str = "shio-reply-a",
    scope: str = "scope:group-a",
    session: str = "session-a",
) -> SentReplyRecord:
    segment = SendSegmentAttempt(
        internal_reply_id=reply_id,
        segment_id=f"{reply_id}:0",
        segment_index=0,
        visible_text=text,
        visible_text_digest=_digest(text),
        visible_text_length=len(text),
        status=status,
        attempted_at=10.0,
        completed_at=(11.0 if status in {SegmentSendStatus.SUCCEEDED, SegmentSendStatus.FAILED} else 0.0),
        failure_kind=("SyntheticSendFailure" if status is SegmentSendStatus.FAILED else ""),
    )
    return SentReplyRecord(
        reply_id=reply_id,
        target_message_id=target_message,
        target_sender_key=target_sender,
        session_id=session,
        scope_key=scope,
        target_content_digest=_digest(f"target:{target_message}"),
        target_source_kind="current_inbound",
        target_referenced_message_id="",
        segments=(segment,),
        expression_ids=(),
        trace_id="a" * 32,
        created_at=9.0,
        sent_at=(11.0 if status is SegmentSendStatus.SUCCEEDED else 0.0),
    )


def _assistant_record(receipt: SentReplyRecord) -> LedgerRecord:
    segment = receipt.segments[0]
    ledger = ConversationLedger()
    return ledger.record_outbound(
        scope_key=receipt.scope_key,
        session_id=receipt.session_id,
        message_id=segment.segment_id,
        bot_sender_key=f"{receipt.scope_key}|bot:shio",
        target_sender_key=receipt.target_sender_key,
        reply_to_message_id=receipt.target_message_id,
        timestamp=segment.completed_at,
        content=segment.visible_text,
    )


def _user_record(*, sequence: int, sender: str, content: str) -> LedgerRecord:
    return LedgerRecord(
        sequence=sequence,
        source_kind=LedgerSourceKind.INBOUND,
        role=LedgerRole.USER,
        scope_key="scope:group-a",
        session_id="session-a",
        message_id=f"user-message-{sequence}",
        sender_key=sender,
        reply_to_message_id="",
        referenced_sender_key="",
        target_sender_key="",
        timestamp=float(sequence),
        content=content,
        content_digest=ledger_content_digest(content),
        source_id=f"user-message-{sequence}",
        attribution_status="verified",
    )


def _tool_record(
    binding: DecisionBinding,
    *,
    tool_name: str,
    tool_call_id: str,
    content: str,
) -> LedgerRecord:
    ledger = ConversationLedger()
    return ledger.record_tool_result(
        scope_key=binding.scope_key,
        session_id=binding.session_id,
        source_id=tool_call_id,
        tool_name=tool_name,
        timestamp=20.0,
        content=content,
    )


def _plugin_evidence(
    plugin_id: str,
    binding: DecisionBinding,
) -> ExternalPluginEvidence:
    contracts = {
        "livingmemory": (
            PluginEvidenceKind.MEMORY_REFERENCE,
            HistoryVisibility.CURRENT_TURN_REFERENCE,
            (EvidenceConsumer.CURRENT_TURN,),
        ),
        "anysearch": (
            PluginEvidenceKind.GROUNDING_FACT,
            HistoryVisibility.CURRENT_TURN_REFERENCE,
            (EvidenceConsumer.CURRENT_TURN,),
        ),
        "meme_manager": (
            PluginEvidenceKind.PRESENTATION_EFFECT,
            HistoryVisibility.DROP,
            (EvidenceConsumer.PRESENTATION,),
        ),
        "parser": (
            PluginEvidenceKind.EXTERNAL_OUTPUT,
            HistoryVisibility.DROP,
            (),
        ),
        "reneban": (
            PluginEvidenceKind.GATE_DECISION,
            HistoryVisibility.DROP,
            (EvidenceConsumer.INGRESS,),
        ),
        "qq_admin": (
            PluginEvidenceKind.EXTERNAL_OUTPUT,
            HistoryVisibility.DROP,
            (),
        ),
    }
    evidence_kind, visibility, consumers = contracts[plugin_id]
    return ExternalPluginEvidence(
        binding=binding,
        plugin_id=plugin_id,
        evidence_kind=evidence_kind,
        status=PluginEvidenceStatus.VERIFIED,
        history_visibility=visibility,
        trusted=True,
        allowed_consumers=consumers,
        target_message_id=binding.current_message_id,
    )


class HistoryNormalizerTests(unittest.TestCase):
    def test_shio_assistant_requires_successful_receipt_target_and_source(self):
        binding = _binding()
        context = HistoryNormalizationContext(binding=binding, current_action_id="action-a")
        receipt = _sent_reply(
            target_sender=binding.current_sender_key,
            target_message="older-message-from-a",
        )
        record = _assistant_record(receipt)

        accepted = normalize_assistant_history(
            (record,),
            receipts=(receipt,),
            context=context,
        )[0]
        missing = normalize_assistant_history(
            (record,),
            receipts=(),
            context=context,
        )[0]
        failed_receipt = _sent_reply(
            target_sender=binding.current_sender_key,
            target_message="older-message-from-a",
            status=SegmentSendStatus.FAILED,
        )
        failed = normalize_assistant_history(
            (record,),
            receipts=(failed_receipt,),
            context=context,
        )[0]
        legacy = normalize_assistant_history(
            (replace(record, source_kind=LedgerSourceKind.LEGACY_HISTORY),),
            receipts=(receipt,),
            context=context,
        )[0]

        self.assertIs(accepted.disposition, HistoryDisposition.CHARACTER_THREAD)
        self.assertIs(accepted.origin, HistoryOrigin.SHIO_ASSISTANT)
        self.assertEqual(accepted.content, "这条确实发出去了。")
        self.assertIs(accepted.history_visibility, HistoryVisibility.PERSONAL_REFERENCE)
        self.assertTrue(accepted.can_feed(EvidenceConsumer.CURRENT_TURN))
        for dropped in (missing, failed, legacy):
            self.assertIs(dropped.disposition, HistoryDisposition.DROP)
            self.assertIs(dropped.origin, HistoryOrigin.EXTERNAL_ASSISTANT)
            self.assertEqual(dropped.content, "")

    def test_a_b_adjacency_never_reassigns_assistant_target(self):
        sender_a = "scope:group-a|user:guest-a"
        sender_b = "scope:group-a|user:guest-b"
        receipt_to_a = _sent_reply(
            target_sender=sender_a,
            target_message="user-message-a",
        )
        assistant_to_a = _assistant_record(receipt_to_a)
        adjacent_records = (
            _user_record(sequence=1, sender=sender_a, content="A 的问题"),
            _user_record(sequence=2, sender=sender_b, content="B 的新问题"),
            assistant_to_a,
        )

        for_b = normalize_assistant_history(
            adjacent_records,
            receipts=(receipt_to_a,),
            context=HistoryNormalizationContext(
                binding=_binding(sender=sender_b, message="user-message-b", revision=2),
                current_action_id="action-b",
            ),
        )
        for_a = normalize_assistant_history(
            adjacent_records,
            receipts=(receipt_to_a,),
            context=HistoryNormalizationContext(
                binding=_binding(sender=sender_a, message="later-message-a", revision=3),
                current_action_id="action-a-2",
            ),
        )

        self.assertEqual(len(for_b), 1)
        self.assertIs(for_b[0].disposition, HistoryDisposition.DROP)
        self.assertIn("assistant_target_mismatch", for_b[0].reason_codes)
        self.assertIs(for_a[0].disposition, HistoryDisposition.CHARACTER_THREAD)

    def test_tool_reference_is_bound_to_current_action_scope_and_sender_only(self):
        current_binding = _binding()
        evidence = _plugin_evidence("anysearch", current_binding)
        record = _tool_record(
            current_binding,
            tool_name="anysearch_search",
            tool_call_id="tool-call-a",
            content="当前轮公开资料",
        )
        candidate = PluginHistoryCandidate(
            evidence=evidence,
            record=record,
            action_id="action-a",
            tool_name="anysearch_search",
            tool_call_id="tool-call-a",
        )

        current = normalize_plugin_history(
            candidate,
            context=HistoryNormalizationContext(current_binding, "action-a"),
        )
        next_turn = normalize_plugin_history(
            candidate,
            context=HistoryNormalizationContext(
                _binding(message="next-message-a", revision=2),
                "action-next",
            ),
        )
        other_sender = normalize_plugin_history(
            candidate,
            context=HistoryNormalizationContext(
                _binding(
                    sender="scope:group-a|user:guest-b",
                    message="current-message-b",
                    revision=2,
                ),
                "action-a",
            ),
        )

        self.assertIs(current.disposition, HistoryDisposition.CURRENT_TURN_REFERENCE)
        self.assertEqual(current.content, "当前轮公开资料")
        self.assertTrue(current.can_feed(EvidenceConsumer.CURRENT_TURN))
        for stale in (next_turn, other_sender):
            self.assertIs(stale.disposition, HistoryDisposition.DROP)
            self.assertEqual(stale.content, "")

    def test_five_plugin_sources_follow_closed_history_contracts(self):
        binding = _binding()
        context = HistoryNormalizationContext(binding, "action-a")
        plugin_inputs = {
            "livingmemory": PluginHistoryCandidate(
                evidence=_plugin_evidence("livingmemory", binding),
                record=_tool_record(
                    binding,
                    tool_name="recall_long_term_memory",
                    tool_call_id="memory-call-a",
                    content="与当前主体绑定的记忆参考",
                ),
                action_id="action-a",
                tool_name="recall_long_term_memory",
                tool_call_id="memory-call-a",
            ),
            "anysearch": PluginHistoryCandidate(
                evidence=_plugin_evidence("anysearch", binding),
                record=_tool_record(
                    binding,
                    tool_name="anysearch_search",
                    tool_call_id="search-call-a",
                    content="公开资料参考",
                ),
                action_id="action-a",
                tool_name="anysearch_search",
                tool_call_id="search-call-a",
            ),
            "meme_manager": PluginHistoryCandidate(
                evidence=_plugin_evidence("meme_manager", binding),
                record=_tool_record(
                    binding,
                    tool_name="search_memes",
                    tool_call_id="meme-call-a",
                    content='{"query":"不应回灌"}',
                ),
                action_id="action-a",
                tool_name="search_memes",
                tool_call_id="meme-call-a",
            ),
            "parser": PluginHistoryCandidate(
                evidence=_plugin_evidence("parser", binding),
                record=replace(
                    _assistant_record(
                        _sent_reply(
                            target_sender=binding.current_sender_key,
                            target_message=binding.current_message_id,
                            text="视频解析进度 75%",
                            reply_id="parser-output-a",
                        )
                    ),
                    source_kind=LedgerSourceKind.LEGACY_HISTORY,
                ),
            ),
            "reneban": PluginHistoryCandidate(
                evidence=_plugin_evidence("reneban", binding),
            ),
        }

        normalized = {
            plugin_id: normalize_plugin_history(candidate, context=context)
            for plugin_id, candidate in plugin_inputs.items()
        }

        for plugin_id in ("livingmemory", "anysearch"):
            self.assertIs(
                normalized[plugin_id].disposition,
                HistoryDisposition.CURRENT_TURN_REFERENCE,
            )
            self.assertTrue(
                normalized[plugin_id].can_feed(EvidenceConsumer.CURRENT_TURN)
            )
        for plugin_id in ("meme_manager", "parser", "reneban"):
            self.assertIs(normalized[plugin_id].disposition, HistoryDisposition.DROP)
            self.assertEqual(normalized[plugin_id].content, "")

        for item in normalized.values():
            for forbidden in (
                EvidenceConsumer.PUBLIC_SCENE,
                EvidenceConsumer.PERSONAL_MEMORY,
                EvidenceConsumer.PERSONA,
                EvidenceConsumer.LEARNING,
            ):
                self.assertFalse(item.can_feed(forbidden))

    def test_meme_parser_admin_and_unknown_external_output_never_enter_consumers(self):
        binding = _binding()
        context = HistoryNormalizationContext(binding, "action-a")
        external_candidates = (
            PluginHistoryCandidate(
                evidence=_plugin_evidence("meme_manager", binding),
                record=_tool_record(
                    binding,
                    tool_name="search_memes",
                    tool_call_id="meme-progress",
                    content="query/candidate/progress payload",
                ),
                action_id="action-a",
                tool_name="search_memes",
                tool_call_id="meme-progress",
            ),
            PluginHistoryCandidate(
                evidence=_plugin_evidence("parser", binding),
                record=_user_record(
                    sequence=4,
                    sender="scope:group-a|bot:parser",
                    content="下载与解析进度",
                ),
            ),
            PluginHistoryCandidate(
                evidence=_plugin_evidence("qq_admin", binding),
                record=_user_record(
                    sequence=5,
                    sender="scope:group-a|bot:admin",
                    content="管理操作状态",
                ),
            ),
            PluginHistoryCandidate(
                evidence=replace(
                    _plugin_evidence("qq_admin", binding),
                    plugin_id="unknown_external_plugin",
                ),
                record=_user_record(
                    sequence=6,
                    sender="scope:group-a|bot:unknown",
                    content="未知插件自动文案",
                ),
            ),
        )

        for candidate in external_candidates:
            with self.subTest(plugin_id=candidate.evidence.plugin_id):
                item = normalize_plugin_history(candidate, context=context)
                self.assertIs(item.disposition, HistoryDisposition.DROP)
                self.assertEqual(item.content, "")
                for consumer in EvidenceConsumer:
                    self.assertFalse(item.can_feed(consumer))

    def test_plugin_contract_mismatch_fails_closed(self):
        binding = _binding()
        malformed_livingmemory = replace(
            _plugin_evidence("livingmemory", binding),
            evidence_kind=PluginEvidenceKind.GROUNDING_FACT,
        )
        candidate = PluginHistoryCandidate(
            evidence=malformed_livingmemory,
            record=_tool_record(
                binding,
                tool_name="recall_long_term_memory",
                tool_call_id="memory-call-a",
                content="不应通过错误合同",
            ),
            action_id="action-a",
            tool_name="recall_long_term_memory",
            tool_call_id="memory-call-a",
        )

        item = normalize_plugin_history(
            candidate,
            context=HistoryNormalizationContext(binding, "action-a"),
        )

        self.assertIs(item.disposition, HistoryDisposition.DROP)
        self.assertIn("plugin_contract_mismatch", item.reason_codes)

    def test_trace_has_no_content_or_raw_identifiers_and_values_are_slotted(self):
        binding = _binding(
            sender="scope:secret-group|user:raw-secret-user",
            message="raw-secret-message",
            scope="scope:secret-group",
            session="raw-secret-session",
        )
        context = HistoryNormalizationContext(binding, "raw-secret-action")
        receipt = _sent_reply(
            target_sender=binding.current_sender_key,
            target_message="raw-secret-target-message",
            text="绝不能进入 trace 的正文",
            scope=binding.scope_key,
            session=binding.session_id,
        )
        item = normalize_assistant_history(
            (_assistant_record(receipt),),
            receipts=(receipt,),
            context=context,
        )[0]

        rendered = json.dumps(item.trace_metadata(), ensure_ascii=False, sort_keys=True)
        for forbidden in (
            item.content,
            binding.scope_key,
            binding.session_id,
            binding.current_message_id,
            binding.current_sender_key,
            context.current_action_id,
            receipt.reply_id,
            receipt.target_message_id,
        ):
            self.assertNotIn(forbidden, rendered)
        for value in (context, item):
            self.assertFalse(hasattr(value, "__dict__"))


if __name__ == "__main__":
    unittest.main()
