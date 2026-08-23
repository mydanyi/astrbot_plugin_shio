from __future__ import annotations

import copy
import dataclasses
import json
import unittest

from astrbot_plugin_shio.core.accepted_turn_authority import (
    AcceptedTurnAuthority,
    AcceptedTurnConsumer,
)
from astrbot_plugin_shio.core.contracts import (
    PluginEvidenceStatus,
    SenderKind,
)
from astrbot_plugin_shio.core.contracts.owner_action import OwnerActionOperation
from astrbot_plugin_shio.core.conversation_event import (
    ConversationRevisionBook,
    build_ingress_event,
)
from astrbot_plugin_shio.core.identity import PrincipalContext, TurnEnvelope
from astrbot_plugin_shio.core.ingress_admission import IngressAdmissionController
from astrbot_plugin_shio.core.owner_action_router import (
    OwnerActionRouteDecision,
    OwnerActionRouteStatus,
    OwnerActionRouter,
    route_owner_action,
)


class RouterFixture:
    def __init__(self) -> None:
        self.book = ConversationRevisionBook()
        self.controller = IngressAdmissionController(self.book, max_proofs=64)
        self.authority = AcceptedTurnAuthority(self.controller, max_turns=64)
        self.router = OwnerActionRouter(self.authority, max_routes=64)
        self.epoch = 0

    def turn(
        self,
        content: str,
        *,
        owner: bool = True,
        chat_type: str = "private",
        verification_source: str = "astrbot_event_sender_id:configured_owner_id",
        reply_to_message_id: str = "",
    ):
        self.epoch += 1
        session = f"router-session-{self.epoch}"
        group = "router-group" if chat_type == "group" else ""
        conversation_id = group if chat_type == "group" else session
        scope = f"platform:test|bot:shio|{chat_type}:{conversation_id}"
        envelope = TurnEnvelope(
            session_id=session,
            message_id=f"router-message-{self.epoch}",
            scope_key=scope,
            sender_key=f"{scope}|user:router-sender",
            sender_id="router-sender",
            platform_id="test",
            bot_id="shio",
            chat_type=chat_type,
            group_id=group,
            reply_to_message_id=reply_to_message_id,
            reply_to_sender_id="quoted-sender" if reply_to_message_id else "",
            timestamp=100.0 + self.epoch,
            timestamp_source="message",
            source_kind="inbound",
            degradation_reasons=(),
        )
        principal = PrincipalContext(
            sender_key=envelope.sender_key,
            sender_id=envelope.sender_id,
            is_owner=owner,
            relationship_role="owner" if owner else "private_peer",
            verification_source=verification_source,
        )
        ingress = build_ingress_event(
            envelope=envelope,
            principal=principal,
            sender_kind=SenderKind.HUMAN,
            content=content,
            revision_candidate=self.book.peek(scope),
            generation_epoch=self.epoch,
        )
        gate = self.controller.issue_gate_observation(
            ingress,
            status=PluginEvidenceStatus.VERIFIED,
            banned=False,
        )
        admission = self.controller.admit(ingress, gate_observation=gate)
        dispatch = self.authority.dispatch(admission)
        ticket = self.authority.ticket_for(
            dispatch,
            AcceptedTurnConsumer.OWNER_ACTION,
        )
        return ticket

    def claim_ticket_directly(self, ticket):
        context = self.authority.context_for(
            ticket,
            consumer=AcceptedTurnConsumer.OWNER_ACTION,
            binding=ticket.binding,
        )
        return self.authority.claim_ticket(
            ticket,
            consumer=AcceptedTurnConsumer.OWNER_ACTION,
            binding=ticket.binding,
            context=context,
        )


class OwnerActionRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = RouterFixture()

    def route(self, content: str, **kwargs):
        ticket = self.fixture.turn(content, **kwargs)
        decision = route_owner_action(
            self.fixture.router,
            ticket,
            current_message=content,
        )
        return ticket, decision

    def test_closed_current_message_routes_each_supported_operation(self):
        cases = (
            (
                '请读取文件 path="/safe/note.txt" offset=0 limit=20',
                OwnerActionOperation.ARTIFACT_READ_EXACT,
            ),
            (
                '请搜索文件 path="/safe" literal="needle"',
                OwnerActionOperation.ARTIFACT_GREP,
            ),
            (
                "请你记住：明天检查测试结果",
                OwnerActionOperation.MEMORY_WRITE_LITERAL,
            ),
            (
                "请执行下面命令\n```bash\necho ok\n```",
                OwnerActionOperation.SANDBOX_SHELL_ONCE,
            ),
        )
        for content, operation in cases:
            with self.subTest(operation=operation.value):
                ticket, result = self.route(content)
                self.assertIs(result.status, OwnerActionRouteStatus.MATCHED)
                self.assertIsNotNone(result.proposal)
                self.assertIs(result.proposal.operation, operation)
                self.assertEqual(result.proposal.confidence, 1.0)
                self.assertIn("code_owned_current_message_route", result.proposal.reason_codes)
                self.assertIs(
                    self.fixture.router.inspect_route(result, ticket=ticket),
                    result,
                )

    def test_no_parameters_or_visible_body_are_copied_into_the_proposal(self):
        secret_path = "/safe/private-name.txt"
        ticket, result = self.route(f'请读取文件 path="{secret_path}"')
        rendered = repr(result) + repr(result.proposal) + json.dumps(
            result.trace_metadata(),
            ensure_ascii=False,
        )
        self.assertNotIn(secret_path, rendered)
        self.assertNotIn("private-name", rendered)
        self.assertFalse(hasattr(result.proposal, "parameters"))
        self.assertFalse(hasattr(result.proposal, "tool_name"))
        self.assertIs(self.fixture.router.inspect_route(result, ticket=ticket), result)

    def test_guest_and_untrusted_owner_claims_cannot_route(self):
        _, guest = self.route('请读取文件 path="/safe/a.txt"', owner=False)
        self.assertIs(guest.status, OwnerActionRouteStatus.IDENTITY_REJECTED)

        _, claimed = self.route(
            '请读取文件 path="/safe/a.txt"',
            owner=True,
            verification_source="message_text:configured_owner_id",
        )
        self.assertIs(claimed.status, OwnerActionRouteStatus.IDENTITY_REJECTED)

    def test_owner_group_action_requires_private_channel(self):
        _, result = self.route(
            '请读取文件 path="/safe/a.txt"',
            chat_type="group",
        )
        self.assertIs(result.status, OwnerActionRouteStatus.PRIVATE_REQUIRED)
        self.assertIsNone(result.proposal)

    def test_structured_reply_never_supplies_an_action(self):
        _, result = self.route(
            '请读取文件 path="/safe/a.txt"',
            reply_to_message_id="quoted-message",
        )
        self.assertIs(result.status, OwnerActionRouteStatus.REFERENCE_REJECTED)
        self.assertIsNone(result.proposal)

    def test_discussion_negation_and_examples_are_not_actions(self):
        cases = (
            '不要读取文件 path="/safe/a.txt"',
            '我觉得“记住：明天检查”这个说法很有趣',
            '示例：请搜索文件 path="/safe" literal="x"',
            '如果要执行下面命令应该怎么写？\n```bash\necho ok\n```',
            '普通聊天，不需要调用任何能力',
        )
        for content in cases:
            with self.subTest(content=content):
                _, result = self.route(content)
                self.assertIs(result.status, OwnerActionRouteStatus.NO_MATCH)
                self.assertIsNone(result.proposal)

    def test_multiple_operation_signals_fail_ambiguous(self):
        _, result = self.route(
            '请读取并搜索文件 path="/safe/a.txt" literal="needle"',
        )
        self.assertIs(result.status, OwnerActionRouteStatus.AMBIGUOUS)
        self.assertIsNone(result.proposal)

    def test_current_message_digest_mismatch_fails_closed(self):
        content = '请读取文件 path="/safe/a.txt"'
        ticket = self.fixture.turn(content)
        with self.assertRaisesRegex(ValueError, "current_message_binding_mismatch"):
            route_owner_action(
                self.fixture.router,
                ticket,
                current_message='请读取文件 path="/safe/b.txt"',
            )

    def test_ticket_copy_cannot_route(self):
        content = '请读取文件 path="/safe/a.txt"'
        ticket = self.fixture.turn(content)
        with self.assertRaisesRegex(ValueError, "ticket_not_canonical"):
            route_owner_action(
                self.fixture.router,
                copy.copy(ticket),
                current_message=content,
            )

    def test_decision_is_module_issued_and_cannot_be_constructed_or_replaced(self):
        ticket, decision = self.route('请读取文件 path="/safe/a.txt"')
        with self.assertRaises(TypeError):
            OwnerActionRouteDecision(
                status=decision.status,
                proposal=decision.proposal,
            )
        with self.assertRaises((TypeError, ValueError)):
            dataclasses.replace(decision, proposal=decision.proposal)
        for forged in (copy.copy(decision), copy.deepcopy(decision)):
            with self.assertRaisesRegex(ValueError, "route_not_canonical"):
                self.fixture.router.inspect_route(forged, ticket=ticket)

    def test_non_consuming_inspection_then_composite_claims_route_and_ticket_once(self):
        ticket, decision = self.route('请读取文件 path="/safe/a.txt"')
        self.assertIs(self.fixture.router.inspect_route(decision, ticket=ticket), decision)
        self.assertIs(self.fixture.router.inspect_route(decision, ticket=ticket), decision)
        self.assertIs(
            self.fixture.router.claim_route_and_ticket(decision, ticket=ticket),
            decision,
        )
        # Inspection is read-only even after transfer; execution authority is the
        # one-shot composite claim, not access to the proposal fields.
        self.assertIs(self.fixture.router.inspect_route(decision, ticket=ticket), decision)
        with self.assertRaisesRegex(ValueError, "route_replayed"):
            self.fixture.router.claim_route_and_ticket(decision, ticket=ticket)
        with self.assertRaisesRegex(ValueError, "ticket_replayed"):
            self.fixture.claim_ticket_directly(ticket)
        self.assertFalse(hasattr(self.fixture.router, "claim_route"))

    def test_composite_authority_failure_leaves_route_unclaimed(self):
        ticket, decision = self.route('请读取文件 path="/safe/a.txt"')
        self.assertIs(self.fixture.claim_ticket_directly(ticket), ticket)
        with self.assertRaisesRegex(ValueError, "ticket_replayed"):
            self.fixture.router.claim_route_and_ticket(decision, ticket=ticket)
        metadata = self.fixture.router.trace_metadata()
        self.assertEqual(metadata["claimed_route_count"], 0)
        self.assertEqual(metadata["active_route_count"], 1)
        self.assertIs(self.fixture.router.inspect_route(decision, ticket=ticket), decision)

    def test_finalize_noop_route_covers_every_terminal_router_status(self):
        cases = (
            ("ordinary owner chat", {}, OwnerActionRouteStatus.NO_MATCH),
            (
                '请读取并搜索文件 path="/safe/a.txt" literal="needle"',
                {},
                OwnerActionRouteStatus.AMBIGUOUS,
            ),
            (
                '请读取文件 path="/safe/a.txt"',
                {"owner": False},
                OwnerActionRouteStatus.IDENTITY_REJECTED,
            ),
            (
                '请读取文件 path="/safe/a.txt"',
                {"chat_type": "group"},
                OwnerActionRouteStatus.PRIVATE_REQUIRED,
            ),
            (
                '请读取文件 path="/safe/a.txt"',
                {"reply_to_message_id": "quoted-message"},
                OwnerActionRouteStatus.REFERENCE_REJECTED,
            ),
            # A MATCHED route can still be policy-denied downstream.  The no-op
            # terminal path must consume both authorities without a fake receipt.
            (
                '请读取文件 path="/safe/a.txt"',
                {},
                OwnerActionRouteStatus.MATCHED,
            ),
        )
        for content, kwargs, expected in cases:
            with self.subTest(status=expected.value):
                ticket, decision = self.route(content, **kwargs)
                self.assertIs(decision.status, expected)
                self.assertIsNone(
                    self.fixture.router.finalize_noop_route(decision, ticket=ticket)
                )
                with self.assertRaisesRegex(ValueError, "route_replayed"):
                    self.fixture.router.finalize_noop_route(decision, ticket=ticket)
                with self.assertRaisesRegex(ValueError, "ticket_replayed"):
                    self.fixture.claim_ticket_directly(ticket)

    def test_cross_router_cross_ticket_and_cross_authority_routes_fail_closed(self):
        content = '请读取文件 path="/safe/a.txt"'
        ticket, decision = self.route(content)
        other_ticket = self.fixture.turn(content)
        with self.assertRaisesRegex(ValueError, "route_ticket_mismatch"):
            self.fixture.router.inspect_route(decision, ticket=other_ticket)

        other_router = OwnerActionRouter(self.fixture.authority, max_routes=8)
        with self.assertRaisesRegex(ValueError, "route_not_canonical"):
            other_router.inspect_route(decision, ticket=ticket)

        foreign = RouterFixture()
        foreign_ticket = foreign.turn(content)
        with self.assertRaisesRegex(ValueError, "route_not_canonical"):
            foreign.router.inspect_route(decision, ticket=foreign_ticket)

    def test_self_consistent_proposal_replacement_and_handle_mutation_are_rejected(self):
        content = '请读取文件 path="/safe/a.txt"'
        ticket, decision = self.route(content)
        replacement = dataclasses.replace(decision.proposal)
        object.__setattr__(decision, "proposal", replacement)
        with self.assertRaisesRegex(ValueError, "route_corrupt"):
            self.fixture.router.inspect_route(decision, ticket=ticket)

        ticket2, decision2 = self.route('请搜索文件 path="/safe" literal="x"')
        object.__setattr__(decision2, "status", OwnerActionRouteStatus.NO_MATCH)
        object.__setattr__(decision2, "proposal", None)
        with self.assertRaisesRegex(ValueError, "route_corrupt"):
            self.fixture.router.claim_route_and_ticket(decision2, ticket=ticket2)

        ticket3, decision3 = self.route('请读取文件 path="/safe/b.txt"')
        object.__setattr__(
            decision3.proposal,
            "binding",
            dataclasses.replace(decision3.proposal.binding),
        )
        with self.assertRaisesRegex(ValueError, "route_corrupt"):
            self.fixture.router.inspect_route(decision3, ticket=ticket3)

    def test_routing_same_ticket_is_idempotent_before_claim_and_rejected_after(self):
        content = '请读取文件 path="/safe/a.txt"'
        ticket = self.fixture.turn(content)
        first = route_owner_action(self.fixture.router, ticket, current_message=content)
        second = route_owner_action(self.fixture.router, ticket, current_message=content)
        self.assertIs(first, second)
        self.fixture.router.claim_route_and_ticket(first, ticket=ticket)
        with self.assertRaisesRegex(ValueError, "route_already_claimed"):
            route_owner_action(self.fixture.router, ticket, current_message=content)

    def test_repr_and_trace_do_not_reveal_message_or_route_provenance_secrets(self):
        hidden = "/safe/owner-only-token-name.txt"
        ticket, decision = self.route(f'请读取文件 path="{hidden}"')
        rendered = repr(decision) + json.dumps(
            decision.trace_metadata(), ensure_ascii=False, sort_keys=True
        )
        self.assertNotIn(hidden, rendered)
        self.assertNotIn("token-name", rendered)
        self.assertNotIn(ticket.ticket_digest, rendered)
        self.assertNotIn(ticket.binding.trace_id, rendered)
        self.assertNotIn(ticket.binding.current_message_id, rendered)


if __name__ == "__main__":
    unittest.main()
