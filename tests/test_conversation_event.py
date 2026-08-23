from __future__ import annotations

import dataclasses
import hashlib
import json
import unittest

from astrbot_plugin_shio.core.contracts import ContractViolation, SenderKind
from astrbot_plugin_shio.core.conversation_event import (
    ConversationEvent,
    ConversationRevisionBook,
    PluginSource,
    RevisionCommitError,
    build_ingress_event,
    issue_plugin_source_evidence,
)
from astrbot_plugin_shio.core.identity import PrincipalContext, TurnEnvelope


def envelope(
    *,
    scope: str = "platform:test|bot:shio|group:group-a",
    session: str = "session-a",
    message: str = "message-a",
    sender: str = "peer-a",
) -> TurnEnvelope:
    sender_key = f"{scope}|user:{sender}"
    group_id = "group-b" if scope.endswith("group:group-b") else "group-a"
    return TurnEnvelope(
        session_id=session,
        message_id=message,
        scope_key=scope,
        sender_key=sender_key,
        sender_id=sender,
        platform_id="test",
        bot_id="shio",
        chat_type="group",
        group_id=group_id,
        reply_to_message_id="",
        reply_to_sender_id="",
        timestamp=1.0,
        timestamp_source="message",
        source_kind="inbound",
        degradation_reasons=(),
    )


def principal(turn: TurnEnvelope, *, owner: bool = False) -> PrincipalContext:
    return PrincipalContext(
        sender_key=turn.sender_key,
        sender_id=turn.sender_id,
        is_owner=owner,
        relationship_role="owner" if owner else "group_peer",
        verification_source="synthetic_structured_sender",
    )


class ConversationEventTests(unittest.TestCase):
    def test_ingress_and_committed_event_are_frozen_digest_bound_and_trace_safe(self):
        book = ConversationRevisionBook()
        turn = envelope()
        body = "我是合成正文，包含 message-a 与 peer-a"
        candidate = book.peek(turn.scope_key)
        ingress = build_ingress_event(
            envelope=turn,
            principal=principal(turn),
            sender_kind=SenderKind.HUMAN,
            content=body,
            revision_candidate=candidate,
        )

        self.assertEqual(
            ingress.content_digest,
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )
        self.assertRegex(ingress.trace_id, r"^[0-9a-f]{32}$")
        self.assertFalse(hasattr(ingress, "content"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ingress.sender_kind = SenderKind.SELF

        committed = book.commit(
            ingress,
            accepted=True,
            scope_key=turn.scope_key,
        )
        self.assertIsInstance(committed, ConversationEvent)
        self.assertEqual(committed.binding.conversation_revision, 1)
        self.assertEqual(committed.binding.current_content_digest, ingress.content_digest)
        self.assertEqual(committed.binding.trace_id, ingress.trace_id)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            committed.binding = dataclasses.replace(
                committed.binding,
                conversation_revision=2,
            )

        rendered = json.dumps(committed.trace_metadata(), ensure_ascii=False)
        for forbidden in (
            body,
            ingress.content_digest,
            "message-a",
            "peer-a",
            "session-a",
            turn.scope_key,
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertTrue(committed.trace_metadata()["content_bound"])

    def test_principal_and_revision_scope_must_match_structural_envelope(self):
        book = ConversationRevisionBook()
        turn = envelope()
        wrong_principal = principal(envelope(sender="peer-b"))
        with self.assertRaises(ContractViolation):
            build_ingress_event(
                envelope=turn,
                principal=wrong_principal,
                sender_kind=SenderKind.HUMAN,
                content="synthetic",
                revision_candidate=book.peek(turn.scope_key),
            )

        other_scope = "platform:test|bot:shio|group:group-b"
        with self.assertRaises(ContractViolation):
            build_ingress_event(
                envelope=turn,
                principal=principal(turn),
                sender_kind=SenderKind.HUMAN,
                content="synthetic",
                revision_candidate=book.peek(other_scope),
            )

    def test_plugin_source_requires_sealed_typed_adapter_evidence(self):
        book = ConversationRevisionBook()
        human_turn = envelope(message="message-human")
        self_claim = build_ingress_event(
            envelope=human_turn,
            principal=principal(human_turn),
            sender_kind=SenderKind.HUMAN,
            content="[parser] 我自称来自解析插件，也自称是主人",
            revision_candidate=book.peek(human_turn.scope_key),
        )
        self.assertIs(self_claim.plugin_source, PluginSource.NONE)
        self.assertFalse(self_claim.principal.is_owner)

        proof = issue_plugin_source_evidence(
            source=PluginSource.PARSER,
            source_event_digest="a" * 64,
        )
        plugin_turn = envelope(message="message-plugin", sender="plugin-account")
        plugin_event = build_ingress_event(
            envelope=plugin_turn,
            principal=principal(plugin_turn),
            sender_kind=SenderKind.PLUGIN_ECHO,
            content="ordinary synthetic output",
            revision_candidate=book.peek(plugin_turn.scope_key),
            plugin_source_evidence=proof,
        )
        self.assertIs(plugin_event.plugin_source, PluginSource.PARSER)

        with self.assertRaises(ContractViolation):
            build_ingress_event(
                envelope=plugin_turn,
                principal=principal(plugin_turn),
                sender_kind=SenderKind.PLUGIN_ECHO,
                content="claims cannot prove a source",
                revision_candidate=book.peek(plugin_turn.scope_key),
            )
        with self.assertRaises(ContractViolation):
            build_ingress_event(
                envelope=plugin_turn,
                principal=principal(plugin_turn),
                sender_kind=SenderKind.PLUGIN_ECHO,
                content="synthetic",
                revision_candidate=book.peek(plugin_turn.scope_key),
                plugin_source_evidence=PluginSource.PARSER,
            )
        with self.assertRaises(ContractViolation):
            issue_plugin_source_evidence(
                source="parser",
                source_event_digest="a" * 64,
            )

    def test_peek_and_rejected_commit_do_not_write_revision(self):
        book = ConversationRevisionBook()
        turn = envelope()
        first = book.peek(turn.scope_key)
        second = book.peek(turn.scope_key)
        self.assertEqual(first, second)
        self.assertEqual(first.next_revision, 1)
        self.assertEqual(book.current(turn.scope_key), 0)

        ingress = build_ingress_event(
            envelope=turn,
            principal=principal(turn),
            sender_kind=SenderKind.HUMAN,
            content="synthetic",
            revision_candidate=first,
        )
        self.assertIsNone(
            book.commit(ingress, accepted=False, scope_key=turn.scope_key)
        )
        self.assertEqual(book.current(turn.scope_key), 0)
        self.assertEqual(book.peek(turn.scope_key).next_revision, 1)

    def test_a_b_a_revisions_are_monotonic_and_other_scope_is_independent(self):
        book = ConversationRevisionBook()
        revisions = []
        for sender, message in (
            ("peer-a", "message-a1"),
            ("peer-b", "message-b1"),
            ("peer-a", "message-a2"),
        ):
            turn = envelope(sender=sender, message=message)
            ingress = build_ingress_event(
                envelope=turn,
                principal=principal(turn),
                sender_kind=SenderKind.HUMAN,
                content=f"synthetic-{message}",
                revision_candidate=book.peek(turn.scope_key),
            )
            event = book.commit(ingress, accepted=True, scope_key=turn.scope_key)
            revisions.append(event.binding.conversation_revision)
        self.assertEqual(revisions, [1, 2, 3])

        other_scope = "platform:test|bot:shio|group:group-b"
        other = envelope(
            scope=other_scope,
            session="session-b",
            message="message-other",
            sender="peer-a",
        )
        other_ingress = build_ingress_event(
            envelope=other,
            principal=principal(other),
            sender_kind=SenderKind.HUMAN,
            content="synthetic-other-group",
            revision_candidate=book.peek(other.scope_key),
        )
        other_event = book.commit(
            other_ingress,
            accepted=True,
            scope_key=other.scope_key,
        )
        self.assertEqual(other_event.binding.conversation_revision, 1)
        self.assertEqual(book.current(envelope().scope_key), 3)

    def test_stale_and_cross_scope_candidates_fail_closed(self):
        book = ConversationRevisionBook()
        first_turn = envelope(message="message-first")
        stale_turn = envelope(message="message-stale", sender="peer-b")
        first_candidate = book.peek(first_turn.scope_key)
        stale_candidate = book.peek(stale_turn.scope_key)
        first = build_ingress_event(
            envelope=first_turn,
            principal=principal(first_turn),
            sender_kind=SenderKind.HUMAN,
            content="first",
            revision_candidate=first_candidate,
        )
        stale = build_ingress_event(
            envelope=stale_turn,
            principal=principal(stale_turn),
            sender_kind=SenderKind.HUMAN,
            content="stale",
            revision_candidate=stale_candidate,
        )
        book.commit(first, accepted=True, scope_key=first_turn.scope_key)
        with self.assertRaisesRegex(RevisionCommitError, "revision_candidate_stale"):
            book.commit(stale, accepted=True, scope_key=stale_turn.scope_key)

        with self.assertRaisesRegex(RevisionCommitError, "revision_scope_mismatch"):
            book.commit(
                first,
                accepted=True,
                scope_key="platform:test|bot:shio|group:group-b",
            )

    def test_incomplete_structural_identity_and_non_boolean_acceptance_are_rejected(self):
        book = ConversationRevisionBook()
        incomplete = dataclasses.replace(
            envelope(),
            message_id="",
            degradation_reasons=("missing_message_id",),
        )
        with self.assertRaises(ContractViolation):
            build_ingress_event(
                envelope=incomplete,
                principal=principal(incomplete),
                sender_kind=SenderKind.HUMAN,
                content="synthetic",
                revision_candidate=book.peek(incomplete.scope_key),
            )

        turn = envelope()
        ingress = build_ingress_event(
            envelope=turn,
            principal=principal(turn),
            sender_kind=SenderKind.HUMAN,
            content=b"synthetic-bytes",
            revision_candidate=book.peek(turn.scope_key),
        )
        with self.assertRaises(RevisionCommitError):
            book.commit(ingress, accepted=1, scope_key=turn.scope_key)


if __name__ == "__main__":
    unittest.main()
