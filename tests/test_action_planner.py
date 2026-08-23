from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
import json
import unittest
from dataclasses import dataclass, replace
from pathlib import Path

from astrbot_plugin_shio.core.action_planner import (
    PlannedAction,
    PlannedActionAuthority,
    StructuralOutcome,
    plan_action,
    plan_initiative,
    reconcile_action,
    structural_action_gate,
)
from astrbot_plugin_shio.core.accepted_turn_authority import (
    AcceptedTurnAuthority,
    AcceptedTurnConsumer,
)
from astrbot_plugin_shio.core.capability_policy import (
    CapabilityClass,
    CapabilityPolicy,
    build_guest_capability_policy,
    build_owner_capability_policy,
)
from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.contracts import (
    ActionDecision,
    ActionKind,
    AddressDecision,
    AddressEvidence,
    AddressKind,
    AttentionDecision,
    AttentionLevel,
    ContractViolation,
    DecisionBinding,
    IngressDecision,
    IngressDisposition,
    KnowledgeGapDecision,
    KnowledgeNeed,
    ModelActionSuggestion,
    OwnerActionOperation,
    OwnerActionProposal,
    ParticipationDecision,
    ParticipationLevel,
    PluginEvidenceStatus,
    SenderKind,
)
from astrbot_plugin_shio.core.conversation_event import (
    ConversationRevisionBook,
    build_ingress_event,
)
from astrbot_plugin_shio.core.identity import PrincipalContext, TurnEnvelope
from astrbot_plugin_shio.core.ingress_admission import IngressAdmissionController
from astrbot_plugin_shio.core.owner_action_router import (
    OwnerActionRouteStatus,
    OwnerActionRouter,
    route_owner_action,
)


ROOT = Path(__file__).parents[1]


@dataclass(frozen=True, slots=True)
class PlannerFixture:
    binding: DecisionBinding
    ingress: IngressDecision
    address: AddressDecision
    attention: AttentionDecision
    participation: ParticipationDecision
    reply_target: ReplyTarget
    knowledge_gap: KnowledgeGapDecision
    capability_policy: CapabilityPolicy

    def kwargs(self) -> dict[str, object]:
        return {
            "ingress": self.ingress,
            "address": self.address,
            "attention": self.attention,
            "participation": self.participation,
            "reply_target": self.reply_target,
            "knowledge_gap": self.knowledge_gap,
            "capability_policy": self.capability_policy,
        }


def _binding(*, revision: int = 1, sender: str = "peer-a") -> DecisionBinding:
    scope = "platform:test|bot:shio|group:group-a"
    return DecisionBinding(
        scope_key=scope,
        session_id="session-sensitive-a",
        current_message_id="message-sensitive-a",
        current_sender_key=f"{scope}|user:{sender}",
        current_content_digest="c" * 64,
        conversation_revision=revision,
        generation_epoch=4,
        trace_id="d" * 32,
    )


def _principal(binding: DecisionBinding, *, owner: bool) -> PrincipalContext:
    sender_id = binding.current_sender_key.rsplit("|user:", 1)[-1]
    return PrincipalContext(
        sender_key=binding.current_sender_key,
        sender_id=sender_id,
        is_owner=owner,
        relationship_role="owner" if owner else "group_peer",
        verification_source=(
            "astrbot_event_sender_id:configured_owner_id"
            if owner
            else "astrbot_event_sender_id:owner_allowlist_miss"
        ),
    )


def _fixture(
    *,
    revision: int = 1,
    sender: str = "peer-a",
    owner: bool = False,
    disposition: IngressDisposition = IngressDisposition.ACCEPT_HUMAN,
    address_kind: AddressKind = AddressKind.DIRECT_SELF,
    attention_level: AttentionLevel = AttentionLevel.FORCE,
    participation_level: ParticipationLevel = ParticipationLevel.MUST_REPLY,
    cooldown_remaining_s: float = 0.0,
    knowledge_need: KnowledgeNeed = KnowledgeNeed.NONE,
    requested_capability: CapabilityClass | None = None,
    policy_allows_web: bool = True,
    bound: DecisionBinding | None = None,
) -> PlannerFixture:
    binding = bound or _binding(revision=revision, sender=sender)
    ingress = IngressDecision(
        binding=binding,
        sender_kind=SenderKind.HUMAN,
        disposition=disposition,
        reason_codes=(
            () if disposition is IngressDisposition.ACCEPT_HUMAN else ("fixture_denied",)
        ),
    )
    address_evidence = {
        AddressKind.DIRECT_SELF: (AddressEvidence.STRUCTURED_MENTION,),
        AddressKind.ABOUT_SELF: (AddressEvidence.THIRD_PERSON_REFERENCE,),
        AddressKind.OTHER_PERSON: (AddressEvidence.OTHER_PERSON_TARGET,),
        AddressKind.OPEN_GROUP: (AddressEvidence.OPEN_GROUP_MARKER,),
        AddressKind.UNCERTAIN: (AddressEvidence.NONE,),
    }[address_kind]
    address = AddressDecision(
        binding=binding,
        kind=address_kind,
        evidence=address_evidence,
        confidence=1.0 if address_kind is AddressKind.DIRECT_SELF else 0.7,
        is_meta_discussion=address_kind is AddressKind.ABOUT_SELF,
    )
    attention = AttentionDecision(
        binding=binding,
        level=attention_level,
        self_relevance=1.0 if attention_level is AttentionLevel.FORCE else 0.5,
        response_value=0.8,
    )
    participation = ParticipationDecision(
        binding=binding,
        level=participation_level,
        cooldown_remaining_s=cooldown_remaining_s,
    )
    reply_target = ReplyTarget(
        message_id=binding.current_message_id,
        sender_key=binding.current_sender_key,
        session_id=binding.session_id,
        scope_key=binding.scope_key,
        content_digest=binding.current_content_digest,
        source_kind="current_inbound",
        referenced_message_id="",
        degradation_reasons=(),
    )
    knowledge_gap = KnowledgeGapDecision(
        binding=binding,
        need=knowledge_need,
        requires_evidence=knowledge_need is not KnowledgeNeed.NONE,
        requested_capability=(
            requested_capability
            if knowledge_need is not KnowledgeNeed.NONE
            else None
        ),
        max_tool_calls=1 if knowledge_need is not KnowledgeNeed.NONE else 0,
    )
    principal = _principal(binding, owner=owner)
    capability_policy = (
        build_owner_capability_policy(principal)
        if owner
        else build_guest_capability_policy(
            principal,
            configured_tool_names=("anysearch_search",),
        )
    )
    if not policy_allows_web:
        capability_policy = replace(
            capability_policy,
            public_web_read=False,
            max_external_tool_calls=0,
        )
    return PlannerFixture(
        binding=binding,
        ingress=ingress,
        address=address,
        attention=attention,
        participation=participation,
        reply_target=reply_target,
        knowledge_gap=knowledge_gap,
        capability_policy=capability_policy,
    )


class OwnerRouteFixture:
    def __init__(self) -> None:
        self.book = ConversationRevisionBook()
        self.admission = IngressAdmissionController(self.book, max_proofs=32)
        self.authority = AcceptedTurnAuthority(self.admission, max_turns=64)
        self.router = OwnerActionRouter(self.authority, max_routes=64)
        self.plan_authority = PlannedActionAuthority(max_plans=64)
        self._epoch = 0

    def routed(
        self,
        content: str,
        *,
        owner: bool = True,
        chat_type: str = "private",
        verification_source: str | None = None,
        knowledge_need: KnowledgeNeed = KnowledgeNeed.NONE,
        requested_capability: CapabilityClass | None = None,
    ):
        self._epoch += 1
        sender = "owner-a" if owner else "guest-a"
        session = f"route-session-{self._epoch}"
        group_id = f"route-group-{self._epoch}" if chat_type == "group" else ""
        conversation_id = group_id if group_id else session
        scope = f"platform:test|bot:shio|{chat_type}:{conversation_id}"
        envelope = TurnEnvelope(
            session_id=session,
            message_id=f"route-message-{self._epoch}",
            scope_key=scope,
            sender_key=f"{scope}|user:{sender}",
            sender_id=sender,
            platform_id="test",
            bot_id="shio",
            chat_type=chat_type,
            group_id=group_id,
            reply_to_message_id="",
            reply_to_sender_id="",
            timestamp=100.0 + self._epoch,
            timestamp_source="message",
            source_kind="inbound",
            degradation_reasons=(),
        )
        principal = PrincipalContext(
            sender_key=envelope.sender_key,
            sender_id=sender,
            is_owner=owner,
            relationship_role=("owner" if owner else "private_peer"),
            verification_source=(
                verification_source
                or (
                    "astrbot_event_sender_id:configured_owner_id"
                    if owner
                    else "astrbot_event_sender_id:owner_allowlist_miss"
                )
            ),
        )
        ingress_event = build_ingress_event(
            envelope=envelope,
            principal=principal,
            sender_kind=SenderKind.HUMAN,
            content=content,
            revision_candidate=self.book.peek(scope),
            generation_epoch=self._epoch,
        )
        gate = self.admission.issue_gate_observation(
            ingress_event,
            status=PluginEvidenceStatus.VERIFIED,
            banned=False,
        )
        admitted = self.admission.admit(
            ingress_event,
            gate_observation=gate,
        )
        dispatch = self.authority.dispatch(admitted)
        ticket = self.authority.ticket_for(
            dispatch,
            AcceptedTurnConsumer.OWNER_ACTION,
        )
        decision = route_owner_action(
            self.router,
            ticket,
            current_message=content,
        )
        fixture = _fixture(
            owner=owner,
            bound=ticket.binding,
            knowledge_need=knowledge_need,
            requested_capability=requested_capability,
        )
        return ticket, decision, fixture


def _gate_for(fixture: PlannerFixture) -> object:
    return structural_action_gate(
        ingress=fixture.ingress,
        address=fixture.address,
        attention=fixture.attention,
        participation=fixture.participation,
        reply_target=fixture.reply_target,
        now=100.0,
    )


class ActionPlannerTests(unittest.TestCase):
    def test_two_stage_direct_force_must_reply_binds_exact_target(self):
        fixture = _fixture()
        gate = structural_action_gate(
            ingress=fixture.ingress,
            address=fixture.address,
            attention=fixture.attention,
            participation=fixture.participation,
            reply_target=fixture.reply_target,
            now=100.0,
        )
        self.assertIs(gate.outcome, StructuralOutcome.CONTINUE)

        planned = reconcile_action(
            gate=gate,
            **fixture.kwargs(),
        )

        self.assertIsInstance(planned, PlannedAction)
        self.assertIs(planned.action.kind, ActionKind.REPLY)
        self.assertEqual(planned.action.reply_target, fixture.reply_target)
        self.assertEqual(planned.binding, fixture.binding)

    def test_structural_terminal_gates_cover_no_action_wait_and_react(self):
        denied = _fixture(disposition=IngressDisposition.DROP_BANNED)
        no_action = plan_action(**denied.kwargs(), now=100.0)
        self.assertIs(no_action.action.kind, ActionKind.NO_ACTION)
        self.assertIsNone(no_action.action.reply_target)

        ignored = _fixture(
            address_kind=AddressKind.UNCERTAIN,
            attention_level=AttentionLevel.IGNORE,
            participation_level=ParticipationLevel.NO_ACTION,
        )
        ignored_plan = plan_action(**ignored.kwargs(), now=100.0)
        self.assertIs(ignored_plan.action.kind, ActionKind.NO_ACTION)

        waiting = _fixture(
            address_kind=AddressKind.OPEN_GROUP,
            attention_level=AttentionLevel.CONSIDER,
            participation_level=ParticipationLevel.WAIT,
            cooldown_remaining_s=5.0,
        )
        wait_plan = plan_action(**waiting.kwargs(), now=100.0)
        self.assertIs(wait_plan.action.kind, ActionKind.WAIT)
        self.assertEqual(wait_plan.action.wait_until, 105.0)
        self.assertIsNone(wait_plan.action.reply_target)

        reacting = _fixture(
            address_kind=AddressKind.ABOUT_SELF,
            attention_level=AttentionLevel.CONSIDER,
            participation_level=ParticipationLevel.REACT_ONLY,
        )
        react_plan = plan_action(**reacting.kwargs(), now=100.0)
        self.assertIs(react_plan.action.kind, ActionKind.REACT)
        self.assertEqual(react_plan.action.reply_target, reacting.reply_target)
        self.assertTrue(react_plan.action.expression_intent)

    def test_knowledge_and_policy_reconcile_to_capability_only_use_tool(self):
        fixture = _fixture(
            knowledge_need=KnowledgeNeed.UNKNOWN_TERM,
            requested_capability=CapabilityClass.PUBLIC_WEB_READ,
        )

        planned = plan_action(**fixture.kwargs(), now=100.0)

        self.assertIs(planned.action.kind, ActionKind.USE_TOOL)
        self.assertEqual(
            planned.action.capability_intent,
            CapabilityClass.PUBLIC_WEB_READ.value,
        )
        self.assertEqual(planned.action.reply_target, fixture.reply_target)
        for forbidden in (
            "tool_name",
            "exact_tool",
            "arguments",
            "tool_arguments",
            "max_tool_calls",
        ):
            self.assertFalse(hasattr(planned.action, forbidden))

    def test_model_cannot_force_tool_and_denied_capability_reconciles_to_reply(self):
        no_gap = _fixture()
        model_forced = plan_action(
            **no_gap.kwargs(),
            now=100.0,
            model_suggestion={
                "action_hint": "use_tool",
                "confidence": 1.0,
                "capability_intent": CapabilityClass.PUBLIC_WEB_READ.value,
            },
        )
        self.assertIs(model_forced.action.kind, ActionKind.REPLY)

        denied = _fixture(
            knowledge_need=KnowledgeNeed.UNKNOWN_TERM,
            requested_capability=CapabilityClass.PUBLIC_WEB_READ,
            policy_allows_web=False,
        )
        denied_plan = plan_action(**denied.kwargs(), now=100.0)
        self.assertIs(denied_plan.action.kind, ActionKind.REPLY)
        self.assertEqual(denied_plan.action.capability_intent, "")

    def test_verified_owner_model_broad_capability_cannot_reach_use_tool(self):
        fixture = _fixture(sender="owner-a", owner=True)
        suggestion = ModelActionSuggestion(
            action_hint=ActionKind.USE_TOOL,
            confidence=0.95,
            capability_intent=CapabilityClass.SHELL_EXEC.value,
        )
        gate = structural_action_gate(
            ingress=fixture.ingress,
            address=fixture.address,
            attention=fixture.attention,
            participation=fixture.participation,
            reply_target=fixture.reply_target,
            model_suggestion=suggestion,
            now=100.0,
        )

        planned = reconcile_action(gate=gate, **fixture.kwargs())

        self.assertIs(planned.action.kind, ActionKind.REPLY)
        self.assertEqual(planned.action.capability_intent, "")
        self.assertIsNone(planned.action.operation_intent)
        self.assertEqual(planned.action.reply_target, fixture.reply_target)
        self.assertFalse(planned.model_hint_applied)
        for forbidden in (
            "tool_name",
            "exact_tool",
            "source",
            "arguments",
            "deadline",
            "call_budget",
        ):
            self.assertFalse(hasattr(planned.action, forbidden))

    def test_exact_owner_route_becomes_execute_action_before_knowledge_tool(self):
        routed = OwnerRouteFixture()
        ticket, route, fixture = routed.routed(
            '请读取文件 path="C:\\trusted\\owner.txt" offset=0 limit=20',
            knowledge_need=KnowledgeNeed.UNKNOWN_TERM,
            requested_capability=CapabilityClass.PUBLIC_WEB_READ,
        )

        planned = reconcile_action(
            gate=_gate_for(fixture),
            **fixture.kwargs(),
            owner_action_router=routed.router,
            owner_action_ticket=ticket,
            owner_action_route=route,
            planned_action_authority=routed.plan_authority,
        )

        self.assertIs(planned.action.kind, ActionKind.EXECUTE_ACTION)
        self.assertEqual(
            planned.action.capability_intent,
            CapabilityClass.ARTIFACT_READ.value,
        )
        self.assertEqual(
            planned.action.operation_intent,
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        self.assertEqual(planned.action.reply_target, fixture.reply_target)
        self.assertFalse(planned.model_hint_applied)
        self.assertIn("owner_action_route_verified", planned.planner_reason_codes)
        self.assertIs(
            routed.router.inspect_route(route, ticket=ticket),
            route,
        )
        self.assertIs(routed.plan_authority.inspect_plan(planned), planned)

    def test_owner_route_requires_exact_router_handle_ticket_and_complete_triplet(self):
        routed = OwnerRouteFixture()
        ticket, route, fixture = routed.routed(
            '请读取文件 path="C:\\trusted\\exact.txt" offset=0 limit=20'
        )
        copied = copy.copy(route)
        other_router = OwnerActionRouter(routed.authority, max_routes=8)
        other_ticket, _, _ = routed.routed(
            '请读取文件 path="C:\\trusted\\other.txt" offset=0 limit=20'
        )
        base = {
            "gate": _gate_for(fixture),
            **fixture.kwargs(),
            "planned_action_authority": routed.plan_authority,
        }
        self.assertNotIn(
            "owner_action_proposal",
            inspect.signature(reconcile_action).parameters,
        )

        with self.assertRaisesRegex(ContractViolation, "owner_action_route_not_canonical"):
            reconcile_action(
                **base,
                owner_action_router=routed.router,
                owner_action_ticket=ticket,
                owner_action_route=copied,
            )
        with self.assertRaisesRegex(ContractViolation, "owner_action_route_not_canonical"):
            reconcile_action(
                **base,
                owner_action_router=other_router,
                owner_action_ticket=ticket,
                owner_action_route=route,
            )
        with self.assertRaisesRegex(
            ContractViolation,
            "planner_owner_action_ticket_binding_mismatch",
        ):
            reconcile_action(
                **base,
                owner_action_router=routed.router,
                owner_action_ticket=other_ticket,
                owner_action_route=route,
            )
        for partial in (
            {"owner_action_router": routed.router},
            {"owner_action_ticket": ticket},
            {"owner_action_route": route},
            {
                "owner_action_router": routed.router,
                "owner_action_ticket": ticket,
            },
        ):
            with self.subTest(partial=tuple(partial)), self.assertRaisesRegex(
                ContractViolation,
                "planner_owner_action_route_inputs_incomplete",
            ):
                reconcile_action(**base, **partial)

    def test_owner_route_policy_private_and_structural_gates_fail_closed(self):
        routed = OwnerRouteFixture()
        ticket, route, fixture = routed.routed(
            '请读取文件 path="C:\\trusted\\policy.txt" offset=0 limit=20'
        )
        for name, policy in (
            (
                "untrusted",
                replace(
                    fixture.capability_policy,
                    verification_source="nickname_claim:self_declared_owner",
                ),
            ),
            (
                "degraded",
                replace(
                    fixture.capability_policy,
                    degradation_reasons=("owner_verification_degraded",),
                ),
            ),
            (
                "capability_denied",
                replace(fixture.capability_policy, artifact_read=False),
            ),
        ):
            kwargs = fixture.kwargs()
            kwargs["capability_policy"] = policy
            with self.subTest(name=name):
                planned = reconcile_action(
                    gate=_gate_for(fixture),
                    **kwargs,
                    owner_action_router=routed.router,
                    owner_action_ticket=ticket,
                    owner_action_route=route,
                    planned_action_authority=routed.plan_authority,
                )
                self.assertIs(planned.action.kind, ActionKind.REPLY)
                self.assertIsNone(planned.action.operation_intent)

        group_ticket, group_route, group_fixture = routed.routed(
            '请读取文件 path="C:\\trusted\\group.txt" offset=0 limit=20',
            chat_type="group",
        )
        self.assertIs(group_route.status, OwnerActionRouteStatus.PRIVATE_REQUIRED)
        group_plan = reconcile_action(
            gate=_gate_for(group_fixture),
            **group_fixture.kwargs(),
            owner_action_router=routed.router,
            owner_action_ticket=group_ticket,
            owner_action_route=group_route,
            planned_action_authority=routed.plan_authority,
        )
        self.assertIs(group_plan.action.kind, ActionKind.REPLY)

        for name, options in (
            ("guest", {"owner": False}),
            (
                "untrusted_owner",
                {"verification_source": "nickname_claim:self_declared_owner"},
            ),
        ):
            rejected_ticket, rejected_route, rejected_fixture = routed.routed(
                '请读取文件 path="C:\\trusted\\rejected.txt" offset=0 limit=20',
                **options,
            )
            with self.subTest(name=name):
                self.assertIs(
                    rejected_route.status,
                    OwnerActionRouteStatus.IDENTITY_REJECTED,
                )
                rejected_plan = reconcile_action(
                    gate=_gate_for(rejected_fixture),
                    **rejected_fixture.kwargs(),
                    owner_action_router=routed.router,
                    owner_action_ticket=rejected_ticket,
                    owner_action_route=rejected_route,
                    planned_action_authority=routed.plan_authority,
                )
                self.assertIs(rejected_plan.action.kind, ActionKind.REPLY)
                self.assertIsNone(rejected_plan.action.operation_intent)

        waiting = _fixture(
            owner=True,
            bound=fixture.binding,
            participation_level=ParticipationLevel.WAIT,
            cooldown_remaining_s=3.0,
        )
        waiting_plan = reconcile_action(
            gate=_gate_for(waiting),
            **waiting.kwargs(),
            owner_action_router=routed.router,
            owner_action_ticket=ticket,
            owner_action_route=copy.copy(route),
            planned_action_authority=routed.plan_authority,
        )
        self.assertIs(waiting_plan.action.kind, ActionKind.WAIT)

    def test_execute_plan_authority_rejects_copy_cross_authority_and_mutation(self):
        routed = OwnerRouteFixture()

        def canonical_plan(label: str):
            ticket, route, fixture = routed.routed(
                f'请读取文件 path="C:\\trusted\\{label}.txt" offset=0 limit=20'
            )
            return reconcile_action(
                gate=_gate_for(fixture),
                **fixture.kwargs(),
                owner_action_router=routed.router,
                owner_action_ticket=ticket,
                owner_action_route=route,
                planned_action_authority=routed.plan_authority,
            )

        original = canonical_plan("canonical")
        self.assertIs(routed.plan_authority.inspect_plan(original), original)
        for copied in (copy.copy(original), copy.deepcopy(original)):
            with self.assertRaisesRegex(ContractViolation, "planned_action_not_canonical"):
                routed.plan_authority.inspect_plan(copied)
        with self.assertRaisesRegex(ContractViolation, "planned_action_not_canonical"):
            PlannedActionAuthority().inspect_plan(original)

        forged = PlannedAction(
            action=original.action,
            structural_outcome=original.structural_outcome,
            planner_reason_codes=original.planner_reason_codes,
        )
        with self.assertRaisesRegex(ContractViolation, "planned_action_not_canonical"):
            routed.plan_authority.inspect_plan(forged)

        mutated_id = canonical_plan("mutated-id")
        object.__setattr__(mutated_id, "action_id", "0" * 64)
        with self.assertRaisesRegex(ContractViolation, "planned_action_corrupt"):
            routed.plan_authority.inspect_plan(mutated_id)

        mutated_operation = canonical_plan("mutated-operation")
        object.__setattr__(
            mutated_operation.action,
            "operation_intent",
            OwnerActionOperation.ARTIFACT_GREP,
        )
        with self.assertRaisesRegex(ContractViolation, "planned_action_corrupt"):
            routed.plan_authority.inspect_plan(mutated_operation)

        replaced_action = canonical_plan("replaced-action")
        object.__setattr__(
            replaced_action,
            "action",
            dataclasses.replace(replaced_action.action),
        )
        with self.assertRaisesRegex(ContractViolation, "planned_action_corrupt"):
            routed.plan_authority.inspect_plan(replaced_action)

        mutated_binding = canonical_plan("mutated-binding")
        object.__setattr__(
            mutated_binding.action,
            "binding",
            dataclasses.replace(
                mutated_binding.binding,
                current_message_id="forged-message",
            ),
        )
        with self.assertRaisesRegex(ContractViolation, "planned_action_corrupt"):
            routed.plan_authority.inspect_plan(mutated_binding)

    def test_owner_tool_hint_cannot_upgrade_guest_forgery_or_weak_suggestion(self):
        owner = _fixture(sender="owner-a", owner=True)
        guest = _fixture(sender="peer-a", owner=False)
        explicit_shell = {
            "action_hint": "use_tool",
            "confidence": 0.95,
            "capability_intent": CapabilityClass.SHELL_EXEC.value,
        }
        forged_owner_policy = replace(
            guest.capability_policy,
            is_owner=True,
            relationship_role="owner",
            verification_source="nickname_claim:self_declared_owner",
            shell_exec=True,
            agent_full=True,
        )
        degraded_owner_policy = replace(
            owner.capability_policy,
            degradation_reasons=("owner_verification_degraded",),
        )
        unverified_owner_policy = replace(
            owner.capability_policy,
            verification_source="nickname_claim:self_declared_owner",
        )
        cases = (
            ("guest", guest, guest.capability_policy, explicit_shell),
            ("self_claim", guest, forged_owner_policy, explicit_shell),
            ("unverified_owner", owner, unverified_owner_policy, explicit_shell),
            ("degraded_owner", owner, degraded_owner_policy, explicit_shell),
            (
                "public_web_without_gap",
                owner,
                owner.capability_policy,
                {
                    "action_hint": "use_tool",
                    "confidence": 0.95,
                    "capability_intent": CapabilityClass.PUBLIC_WEB_READ.value,
                },
            ),
            (
                "low_confidence",
                owner,
                owner.capability_policy,
                {
                    "action_hint": "use_tool",
                    "confidence": 0.79,
                    "capability_intent": CapabilityClass.SHELL_EXEC.value,
                },
            ),
            (
                "not_explicit_use_tool",
                owner,
                owner.capability_policy,
                {
                    "action_hint": "reply",
                    "confidence": 1.0,
                    "capability_intent": CapabilityClass.SHELL_EXEC.value,
                },
            ),
        )
        for name, fixture, policy, suggestion in cases:
            kwargs = fixture.kwargs()
            kwargs["capability_policy"] = policy
            with self.subTest(name=name):
                planned = plan_action(
                    **kwargs,
                    now=100.0,
                    model_suggestion=suggestion,
                )
                self.assertIs(planned.action.kind, ActionKind.REPLY)
                self.assertEqual(planned.action.capability_intent, "")
                self.assertFalse(planned.model_hint_applied)

        with self.assertRaisesRegex(
            ContractViolation,
            "planner_model_execute_action_forbidden",
        ):
            plan_action(
                **owner.kwargs(),
                now=100.0,
                model_suggestion=ModelActionSuggestion(
                    action_hint=ActionKind.EXECUTE_ACTION,
                    confidence=1.0,
                ),
            )

    def test_every_decision_target_gap_and_policy_must_bind_current_turn(self):
        current = _fixture()
        stale = _fixture(revision=2)
        other_principal = _fixture(sender="peer-b")
        cases = {
            "ingress": stale.ingress,
            "address": stale.address,
            "attention": stale.attention,
            "participation": stale.participation,
            "reply_target": other_principal.reply_target,
            "knowledge_gap": stale.knowledge_gap,
            "capability_policy": other_principal.capability_policy,
        }
        for field, replacement in cases.items():
            kwargs = current.kwargs()
            kwargs[field] = replacement
            with self.subTest(field=field), self.assertRaises(ContractViolation):
                plan_action(**kwargs, now=100.0)

    def test_model_authority_exact_tool_source_arguments_and_budget_are_rejected(self):
        fixture = _fixture()
        forged_fields = (
            "owner",
            "target",
            "binding",
            "tool_name",
            "exact_tool",
            "source",
            "arguments",
            "tool_arguments",
            "max_tool_calls",
            "call_budget",
            "budget",
            "deadline",
            "generation_epoch",
            "operation",
            "operation_intent",
        )
        for forged in forged_fields:
            suggestion = {"action_hint": "reply", "confidence": 0.9, forged: "forged"}
            with self.subTest(forged=forged), self.assertRaises(ContractViolation):
                plan_action(
                    **fixture.kwargs(),
                    now=100.0,
                    model_suggestion=suggestion,
                )

        for capability_intent in ("anysearch_search", "unknown_capability_name"):
            suggestion = ModelActionSuggestion(
                action_hint=ActionKind.USE_TOOL,
                confidence=1.0,
                capability_intent=capability_intent,
            )
            with self.subTest(capability_intent=capability_intent), self.assertRaises(
                ContractViolation
            ):
                plan_action(
                    **fixture.kwargs(),
                    now=100.0,
                    model_suggestion=suggestion,
                )

        with self.assertRaisesRegex(
            ContractViolation,
            "model_execute_action_forbidden",
        ):
            plan_action(
                **fixture.kwargs(),
                now=100.0,
                model_suggestion={
                    "action_hint": "execute_action",
                    "confidence": 1.0,
                },
            )

    def test_soft_model_hint_cannot_lower_code_owned_direct_reply_gate(self):
        fixture = _fixture()
        for action_hint in ("no_action", "wait", "react", "initiate"):
            with self.subTest(action_hint=action_hint):
                planned = plan_action(
                    **fixture.kwargs(),
                    now=100.0,
                    model_suggestion={
                        "action_hint": action_hint,
                        "confidence": 1.0,
                        "expression_intent": "light_reaction",
                    },
                )
                self.assertIs(planned.action.kind, ActionKind.REPLY)

    def test_guest_and_owner_are_policy_inputs_not_message_or_persona_guesses(self):
        parameters = inspect.signature(plan_action).parameters
        for forbidden in (
            "message",
            "content",
            "nickname",
            "owner_claim",
            "history",
            "persona",
            "tool_name",
            "arguments",
        ):
            self.assertNotIn(forbidden, parameters)

        guest = _fixture(sender="peer-a", owner=False)
        owner = _fixture(sender="owner-a", owner=True)
        self.assertIs(
            plan_action(**guest.kwargs(), now=100.0).action.kind,
            ActionKind.REPLY,
        )
        self.assertIs(
            plan_action(**owner.kwargs(), now=100.0).action.kind,
            ActionKind.REPLY,
        )

    def test_action_id_is_stable_revision_sensitive_frozen_and_private(self):
        fixture = _fixture()
        first = plan_action(**fixture.kwargs(), now=100.0)
        second = plan_action(**fixture.kwargs(), now=100.0)
        next_revision = _fixture(revision=2)
        later = plan_action(**next_revision.kwargs(), now=100.0)

        self.assertRegex(first.action_id, r"^[0-9a-f]{64}$")
        self.assertEqual(first.action_id, second.action_id)
        self.assertNotEqual(first.action_id, later.action_id)
        self.assertFalse(hasattr(first, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.action_id = "0" * 64

        rendered = repr(first)
        trace = json.dumps(first.trace_metadata(), ensure_ascii=False)
        for forbidden in (
            fixture.binding.scope_key,
            fixture.binding.session_id,
            fixture.binding.current_message_id,
            fixture.binding.current_sender_key,
            fixture.reply_target.message_id,
        ):
            self.assertNotIn(forbidden, rendered)
            self.assertNotIn(forbidden, trace)

    def test_execute_action_operation_is_part_of_plan_identity(self):
        fixture = _fixture(sender="owner-a", owner=True)
        plans = []
        for operation in (
            OwnerActionOperation.ARTIFACT_READ_EXACT,
            OwnerActionOperation.ARTIFACT_GREP,
        ):
            action = ActionDecision(
                binding=fixture.binding,
                kind=ActionKind.EXECUTE_ACTION,
                reply_target=fixture.reply_target,
                capability_intent=CapabilityClass.ARTIFACT_READ.value,
                operation_intent=operation,
                reason_codes=("owner_action_route_verified",),
            )
            plans.append(
                PlannedAction(
                    action=action,
                    structural_outcome=StructuralOutcome.CONTINUE,
                    planner_reason_codes=("owner_action_route_verified",),
                )
            )

        self.assertNotEqual(plans[0].action_id, plans[1].action_id)
        self.assertEqual(
            plans[0].action.operation_intent,
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "planned_action_structural_outcome_invalid",
        ):
            PlannedAction(
                action=plans[0].action,
                structural_outcome="continue",
            )

    def test_initiate_is_contract_only_public_scope_and_not_model_selectable(self):
        binding = _binding()
        planned = plan_initiative(
            binding=binding,
            public_scope_key=binding.scope_key,
            expression_intent="public_check_in",
        )
        self.assertIs(planned.action.kind, ActionKind.INITIATE)
        self.assertEqual(planned.action.public_scope_key, binding.scope_key)
        self.assertIsNone(planned.action.reply_target)
        self.assertIs(planned.structural_outcome, StructuralOutcome.INITIATE)

        with self.assertRaises(ContractViolation):
            plan_initiative(
                binding=binding,
                public_scope_key="platform:test|bot:shio|group:other",
                expression_intent="public_check_in",
            )

        fixture = _fixture()
        model_attempt = plan_action(
            **fixture.kwargs(),
            now=100.0,
            model_suggestion={
                "action_hint": "initiate",
                "confidence": 1.0,
                "expression_intent": "public_check_in",
            },
        )
        self.assertIs(model_attempt.action.kind, ActionKind.REPLY)

    def test_module_has_no_persona_legacy_planner_or_fast_path_import(self):
        source = (ROOT / "core" / "action_planner.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(str(node.module or ""))
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for forbidden in ("persona", "fast_path", "planner", "planner_v2"):
            self.assertFalse(
                any(module == forbidden or module.endswith(f".{forbidden}") for module in imported),
                imported,
            )
        self.assertNotIn("LocalChatPlan", source)


if __name__ == "__main__":
    unittest.main()
