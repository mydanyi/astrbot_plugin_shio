from __future__ import annotations

import dataclasses
import hashlib
import json
import unittest

from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.contracts import (
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
    SceneEntrySource,
    SceneMutationStatus,
    SceneRejectReason,
)
from astrbot_plugin_shio.core.identity import PrincipalContext, TurnEnvelope
from astrbot_plugin_shio.core.send_receipt import (
    InternalSendReceiptLedger,
    SentReplyRecord,
)


def _scope(group: str) -> str:
    return f"platform:test|bot:shio|group:{group}"


def _sender(scope_key: str, name: str) -> str:
    return f"{scope_key}|user:{name}"


def _committed_event(
    revision_book: ConversationRevisionBook,
    *,
    group: str,
    sender: str,
    message: str,
    content: str,
    sender_kind: SenderKind = SenderKind.HUMAN,
    plugin_source: PluginSource = PluginSource.NONE,
) -> ConversationEvent:
    scope_key = _scope(group)
    sender_key = _sender(scope_key, sender)
    envelope = TurnEnvelope(
        session_id=f"session-{group}",
        message_id=message,
        scope_key=scope_key,
        sender_key=sender_key,
        sender_id=sender,
        platform_id="test",
        bot_id="shio",
        chat_type="group",
        group_id=group,
        reply_to_message_id="",
        reply_to_sender_id="",
        timestamp=100.0 + revision_book.current(scope_key),
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
    plugin_evidence = None
    if plugin_source is not PluginSource.NONE:
        plugin_evidence = issue_plugin_source_evidence(
            source=plugin_source,
            source_event_digest=hashlib.sha256(
                f"{group}:{message}:source".encode("utf-8")
            ).hexdigest(),
        )
    ingress = build_ingress_event(
        envelope=envelope,
        principal=principal,
        sender_kind=sender_kind,
        content=content,
        revision_candidate=revision_book.peek(scope_key),
        plugin_source_evidence=plugin_evidence,
    )
    event = revision_book.commit(
        ingress,
        accepted=True,
        scope_key=scope_key,
    )
    assert event is not None
    return event


def _decision(
    event: ConversationEvent,
    disposition: IngressDisposition = IngressDisposition.ACCEPT_HUMAN,
) -> IngressDecision:
    reasons = () if disposition is IngressDisposition.ACCEPT_HUMAN else ("fixture_drop",)
    return IngressDecision(
        binding=event.binding,
        sender_kind=event.sender_kind,
        disposition=disposition,
        reason_codes=reasons,
    )


def _sent_reply(
    event: ConversationEvent,
    text: str,
    *,
    succeeded: bool = True,
) -> SentReplyRecord:
    ticks = iter((200.0, 201.0, 202.0, 203.0))
    ledger = InternalSendReceiptLedger(
        id_factory=lambda: f"reply-{event.binding.conversation_revision}",
        now_fn=lambda: next(ticks),
    )
    target = ReplyTarget(
        message_id=event.envelope.message_id,
        sender_key=event.envelope.sender_key,
        session_id=event.envelope.session_id,
        scope_key=event.envelope.scope_key,
        content_digest=event.content_digest,
        source_kind="current_inbound",
        referenced_message_id=event.envelope.reply_to_message_id,
        degradation_reasons=(),
    )
    pending = ledger.begin_reply(
        target=target,
        visible_segments=(text,),
        trace_id=event.binding.trace_id,
    )
    segment_id = pending.segments[0].segment_id
    ledger.mark_attempted(segment_id)
    if succeeded:
        ledger.mark_succeeded(segment_id)
    else:
        ledger.mark_failed(segment_id, failure_kind="fixture_failure")
    record = ledger.sent_reply_record(pending.internal_reply_id)
    assert record is not None
    return record


class GroupSceneTests(unittest.TestCase):
    def test_accept_human_separates_public_topic_from_personal_facts(self):
        revisions = ConversationRevisionBook()
        event = _committed_event(
            revisions,
            group="group-private-boundary",
            sender="guest-a",
            message="msg-a1",
            content="今晚大家聊海边",
        )
        scene = GroupSceneBook()
        private_fact = PersonalFactCandidate(
            subject_key=event.envelope.sender_key,
            content="guest-a 私下怕冷",
        )

        result = scene.record_human(
            event,
            decision=_decision(event),
            public_content="今晚大家聊海边",
            personal_facts=(private_fact,),
        )

        self.assertIs(result.status, SceneMutationStatus.ACCEPTED_HUMAN)
        snapshot = scene.snapshot(event.envelope.scope_key)
        self.assertEqual(snapshot.conversation_revision, 1)
        self.assertEqual(
            [topic.content for topic in snapshot.public_topics],
            ["今晚大家聊海边"],
        )
        participant = snapshot.participant(event.envelope.sender_key)
        self.assertIsNotNone(participant)
        assert participant is not None
        self.assertEqual(
            [fact.content for fact in participant.personal_facts],
            ["guest-a 私下怕冷"],
        )
        self.assertNotIn(
            "guest-a 私下怕冷",
            [topic.content for topic in snapshot.public_topics],
        )

    def test_a_b_a_revisions_keep_participants_and_facts_by_sender_key(self):
        revisions = ConversationRevisionBook()
        scene = GroupSceneBook()
        scope_key = _scope("group-aba")
        events = (
            _committed_event(
                revisions,
                group="group-aba",
                sender="guest-a",
                message="msg-a1",
                content="A first",
            ),
            _committed_event(
                revisions,
                group="group-aba",
                sender="guest-b",
                message="msg-b1",
                content="B only",
            ),
            _committed_event(
                revisions,
                group="group-aba",
                sender="guest-a",
                message="msg-a2",
                content="A second",
            ),
        )
        fact_texts = ("A fact one", "B fact only", "A fact two")
        for event, content, fact_text in zip(
            events,
            ("A first", "B only", "A second"),
            fact_texts,
            strict=True,
        ):
            result = scene.record_human(
                event,
                decision=_decision(event),
                public_content=content,
                personal_facts=(
                    PersonalFactCandidate(
                        subject_key=event.envelope.sender_key,
                        content=fact_text,
                    ),
                ),
            )
            self.assertIs(result.status, SceneMutationStatus.ACCEPTED_HUMAN)

        snapshot = scene.snapshot(scope_key)
        self.assertEqual(snapshot.conversation_revision, 3)
        self.assertEqual(
            [topic.conversation_revision for topic in snapshot.public_topics],
            [1, 2, 3],
        )
        participant_a = snapshot.participant(_sender(scope_key, "guest-a"))
        participant_b = snapshot.participant(_sender(scope_key, "guest-b"))
        assert participant_a is not None and participant_b is not None
        self.assertEqual(participant_a.human_turn_count, 2)
        self.assertEqual(participant_b.human_turn_count, 1)
        self.assertEqual(
            [fact.content for fact in participant_a.personal_facts],
            ["A fact one", "A fact two"],
        )
        self.assertEqual(
            [fact.content for fact in participant_b.personal_facts],
            ["B fact only"],
        )

    def test_cross_group_scenes_start_at_one_and_never_mix(self):
        revisions = ConversationRevisionBook()
        scene = GroupSceneBook()
        first = _committed_event(
            revisions,
            group="group-one",
            sender="same-user",
            message="msg-g1",
            content="group one topic",
        )
        second = _committed_event(
            revisions,
            group="group-two",
            sender="same-user",
            message="msg-g2",
            content="group two topic",
        )
        for event, content in (
            (first, "group one topic"),
            (second, "group two topic"),
        ):
            scene.record_human(
                event,
                decision=_decision(event),
                public_content=content,
            )

        one = scene.snapshot(first.envelope.scope_key)
        two = scene.snapshot(second.envelope.scope_key)
        self.assertEqual(one.conversation_revision, 1)
        self.assertEqual(two.conversation_revision, 1)
        self.assertEqual([item.content for item in one.public_topics], ["group one topic"])
        self.assertEqual([item.content for item in two.public_topics], ["group two topic"])
        self.assertIsNone(one.participant(second.envelope.sender_key))
        self.assertIsNone(two.participant(first.envelope.sender_key))

    def test_banned_bot_parser_and_meme_ingress_are_rejected_without_state(self):
        cases = (
            (
                SenderKind.HUMAN,
                PluginSource.NONE,
                IngressDisposition.DROP_BANNED,
            ),
            (
                SenderKind.KNOWN_BOT,
                PluginSource.NONE,
                IngressDisposition.DROP_KNOWN_BOT,
            ),
            (
                SenderKind.PLUGIN_ECHO,
                PluginSource.PARSER,
                IngressDisposition.DROP_PLUGIN_ECHO,
            ),
            (
                SenderKind.PLUGIN_ECHO,
                PluginSource.MEME_MANAGER,
                IngressDisposition.DROP_PLUGIN_ECHO,
            ),
            (
                SenderKind.PLUGIN_ECHO,
                PluginSource.EXTERNAL_OTHER,
                IngressDisposition.DROP_PLUGIN_ECHO,
            ),
        )
        for index, (sender_kind, plugin_source, disposition) in enumerate(cases):
            with self.subTest(disposition=disposition, plugin_source=plugin_source):
                revisions = ConversationRevisionBook()
                event = _committed_event(
                    revisions,
                    group=f"group-drop-{index}",
                    sender=f"source-{index}",
                    message=f"msg-drop-{index}",
                    content="must never enter scene",
                    sender_kind=sender_kind,
                    plugin_source=plugin_source,
                )
                scene = GroupSceneBook()
                before = scene.snapshot(event.envelope.scope_key)
                result = scene.record_human(
                    event,
                    decision=_decision(event, disposition),
                    public_content="must never enter scene",
                )
                self.assertIs(result.status, SceneMutationStatus.REJECTED)
                self.assertIs(result.reason, SceneRejectReason.INGRESS_NOT_ACCEPTED)
                self.assertEqual(scene.scope_count, 0)
                self.assertEqual(scene.snapshot(event.envelope.scope_key), before)

    def test_external_outbound_sources_never_enter_scene(self):
        revisions = ConversationRevisionBook()
        event = _committed_event(
            revisions,
            group="group-outbound-source",
            sender="guest-a",
            message="msg-target",
            content="say something",
        )
        scene = GroupSceneBook()
        scene.record_human(
            event,
            decision=_decision(event),
            public_content="say something",
        )
        receipt = _sent_reply(event, "verified visible reply")
        before = scene.snapshot(event.envelope.scope_key)

        rejected_sources = (
            SceneEntrySource.EXTERNAL_PLUGIN,
            SceneEntrySource.KNOWN_BOT,
            SceneEntrySource.BANNED_HUMAN,
            SceneEntrySource.PARSER,
            SceneEntrySource.MEME_MANAGER,
            SceneEntrySource.UNKNOWN_ASSISTANT,
        )
        for source in rejected_sources:
            with self.subTest(source=source):
                result = scene.record_outbound(receipt, source=source)
                self.assertIs(result.status, SceneMutationStatus.REJECTED)
                self.assertIs(result.reason, SceneRejectReason.SOURCE_NOT_ALLOWED)
                self.assertEqual(scene.snapshot(event.envelope.scope_key), before)

    def test_only_successful_exact_shio_receipt_enters_public_scene_once(self):
        revisions = ConversationRevisionBook()
        event = _committed_event(
            revisions,
            group="group-shio-receipt",
            sender="guest-a",
            message="msg-target",
            content="current question",
        )
        scene = GroupSceneBook()
        scene.record_human(
            event,
            decision=_decision(event),
            public_content="current question",
        )
        failed = _sent_reply(event, "not sent", succeeded=False)
        failed_result = scene.record_outbound(
            failed,
            source=SceneEntrySource.SHIO_OUTBOUND,
        )
        self.assertIs(failed_result.status, SceneMutationStatus.REJECTED)
        self.assertIs(failed_result.reason, SceneRejectReason.RECEIPT_NOT_SUCCESSFUL)

        valid = _sent_reply(event, "sent reply")
        wrong_source = dataclasses.replace(valid, target_source_kind="legacy_guess")
        wrong_target = dataclasses.replace(valid, target_message_id="other-message")
        for receipt, reason in (
            (wrong_source, SceneRejectReason.OUTBOUND_SOURCE_UNVERIFIED),
            (wrong_target, SceneRejectReason.OUTBOUND_TARGET_MISMATCH),
        ):
            with self.subTest(reason=reason):
                result = scene.record_outbound(
                    receipt,
                    source=SceneEntrySource.SHIO_OUTBOUND,
                )
                self.assertIs(result.status, SceneMutationStatus.REJECTED)
                self.assertIs(result.reason, reason)

        accepted = scene.record_outbound(
            valid,
            source=SceneEntrySource.SHIO_OUTBOUND,
        )
        self.assertIs(accepted.status, SceneMutationStatus.ACCEPTED_SHIO_OUTBOUND)
        snapshot = scene.snapshot(event.envelope.scope_key)
        self.assertEqual(
            [topic.content for topic in snapshot.public_topics],
            ["current question", "sent reply"],
        )
        participant = snapshot.participant(event.envelope.sender_key)
        assert participant is not None
        self.assertEqual(participant.shio_reply_count, 1)

        duplicate = scene.record_outbound(
            valid,
            source=SceneEntrySource.SHIO_OUTBOUND,
        )
        self.assertIs(duplicate.status, SceneMutationStatus.REJECTED)
        self.assertIs(duplicate.reason, SceneRejectReason.OUTBOUND_DUPLICATE)
        self.assertEqual(scene.snapshot(event.envelope.scope_key), snapshot)

    def test_duplicate_gap_binding_and_content_mismatch_are_zero_write(self):
        revisions = ConversationRevisionBook()
        first = _committed_event(
            revisions,
            group="group-order",
            sender="guest-a",
            message="msg-one",
            content="one",
        )
        second = _committed_event(
            revisions,
            group="group-order",
            sender="guest-b",
            message="msg-two",
            content="two",
        )
        third = _committed_event(
            revisions,
            group="group-order",
            sender="guest-a",
            message="msg-three",
            content="three",
        )
        scene = GroupSceneBook()
        scene.record_human(
            first,
            decision=_decision(first),
            public_content="one",
        )
        baseline = scene.snapshot(first.envelope.scope_key)

        cases = (
            (first, _decision(first), "one", SceneRejectReason.REVISION_STALE),
            (third, _decision(third), "three", SceneRejectReason.REVISION_GAP),
            (second, _decision(first), "two", SceneRejectReason.BINDING_MISMATCH),
            (second, _decision(second), "wrong body", SceneRejectReason.CONTENT_MISMATCH),
        )
        for event, decision, content, reason in cases:
            with self.subTest(reason=reason):
                result = scene.record_human(
                    event,
                    decision=decision,
                    public_content=content,
                )
                self.assertIs(result.status, SceneMutationStatus.REJECTED)
                self.assertIs(result.reason, reason)
                self.assertEqual(scene.snapshot(first.envelope.scope_key), baseline)

    def test_snapshot_limits_bound_topics_participants_targets_and_personal_facts(self):
        revisions = ConversationRevisionBook()
        scene = GroupSceneBook(
            max_public_topics=3,
            max_participants=2,
            max_personal_facts_per_participant=2,
            max_target_bindings=3,
            max_outbound_receipts=2,
        )
        for revision, sender in enumerate(("guest-a", "guest-b", "guest-c", "guest-a"), 1):
            event = _committed_event(
                revisions,
                group="group-bounds",
                sender=sender,
                message=f"msg-{revision}",
                content=f"topic-{revision}",
            )
            result = scene.record_human(
                event,
                decision=_decision(event),
                public_content=f"topic-{revision}",
                personal_facts=tuple(
                    PersonalFactCandidate(
                        subject_key=event.envelope.sender_key,
                        content=f"fact-{revision}-{index}",
                    )
                    for index in range(3)
                ),
            )
            self.assertIs(result.status, SceneMutationStatus.ACCEPTED_HUMAN)

        snapshot = scene.snapshot(_scope("group-bounds"))
        self.assertEqual(snapshot.conversation_revision, 4)
        self.assertEqual(
            [topic.conversation_revision for topic in snapshot.public_topics],
            [2, 3, 4],
        )
        self.assertEqual(len(snapshot.participants), 2)
        for participant in snapshot.participants:
            self.assertLessEqual(len(participant.personal_facts), 2)
        self.assertLessEqual(scene.target_binding_count(_scope("group-bounds")), 3)

    def test_repr_trace_and_rejected_fact_subject_are_redacted_and_frozen(self):
        revisions = ConversationRevisionBook()
        event = _committed_event(
            revisions,
            group="group-secret",
            sender="sender-secret",
            message="message-secret",
            content="public secret body",
        )
        scene = GroupSceneBook()
        result = scene.record_human(
            event,
            decision=_decision(event),
            public_content="public secret body",
            personal_facts=(
                PersonalFactCandidate(
                    subject_key=_sender(event.envelope.scope_key, "other-secret"),
                    content="private secret fact",
                ),
            ),
        )
        self.assertIs(result.status, SceneMutationStatus.REJECTED)
        self.assertIs(result.reason, SceneRejectReason.PERSONAL_FACT_SUBJECT_MISMATCH)
        self.assertEqual(scene.scope_count, 0)

        accepted = scene.record_human(
            event,
            decision=_decision(event),
            public_content="public secret body",
            personal_facts=(
                PersonalFactCandidate(
                    subject_key=event.envelope.sender_key,
                    content="private secret fact",
                ),
            ),
        )
        snapshot = accepted.snapshot
        participant = snapshot.participant(event.envelope.sender_key)
        assert participant is not None
        objects = (
            accepted,
            snapshot,
            snapshot.public_topics[0],
            participant,
            participant.personal_facts[0],
        )
        rendered = "\n".join(repr(value) for value in objects)
        traces = json.dumps(
            [value.trace_metadata() for value in objects],
            ensure_ascii=False,
        )
        for forbidden in (
            event.envelope.scope_key,
            event.envelope.sender_key,
            event.envelope.message_id,
            "public secret body",
            "private secret fact",
        ):
            self.assertNotIn(forbidden, rendered)
            self.assertNotIn(forbidden, traces)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.conversation_revision = 99


if __name__ == "__main__":
    unittest.main()
