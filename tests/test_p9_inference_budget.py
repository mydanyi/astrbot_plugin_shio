import asyncio
import copy
import unittest
from pathlib import Path

from astrbot_plugin_shio.core.action_planner import (
    PlannedActionAuthority,
    plan_action,
)
from astrbot_plugin_shio.core.capability_policy import build_guest_capability_policy
from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.contracts import (
    AddressDecision,
    AddressEvidence,
    AddressKind,
    AttentionDecision,
    AttentionLevel,
    DecisionBinding,
    IngressDecision,
    IngressDisposition,
    KnowledgeGapDecision,
    KnowledgeNeed,
    ParticipationDecision,
    ParticipationLevel,
    SenderKind,
)
from astrbot_plugin_shio.core.generation_epoch import GenerationEpochRegistry
from astrbot_plugin_shio.core.identity import PrincipalContext, TurnEnvelope
from astrbot_plugin_shio.core.inference_budget import (
    InferenceBudgetError,
    InferencePermit,
    InferencePriority,
    InferencePurpose,
    InferenceBudgetAuthority,
)


def _envelope(scope: str, message: str) -> TurnEnvelope:
    return TurnEnvelope(
        session_id=scope,
        message_id=message,
        scope_key=scope,
        sender_key=f"{scope}|user:peer-a",
        sender_id="peer-a",
        platform_id="platform-a",
        bot_id="bot-a",
        chat_type="group",
        group_id=scope,
        reply_to_message_id="",
        reply_to_sender_id="",
        timestamp=1000.0,
        timestamp_source="message",
        source_kind="inbound",
        degradation_reasons=(),
    )


def _principal(scope: str) -> PrincipalContext:
    return PrincipalContext(
        sender_key=f"{scope}|user:peer-a",
        sender_id="peer-a",
        is_owner=False,
        relationship_role="group_peer",
        verification_source="adapter:owner_allowlist_miss",
    )


def _plan(
    epochs: GenerationEpochRegistry,
    authority: PlannedActionAuthority,
    *,
    scope: str,
    message: str,
    direct: bool,
):
    snapshot = epochs.advance(_envelope(scope, message))
    binding = DecisionBinding(
        scope_key=scope,
        session_id=scope,
        current_message_id=message,
        current_sender_key=f"{scope}|user:peer-a",
        current_content_digest=("a" if direct else "b") * 64,
        conversation_revision=1,
        generation_epoch=snapshot.epoch,
        trace_id=("c" if direct else "d") * 32,
    )
    ingress = IngressDecision(
        binding=binding,
        sender_kind=SenderKind.HUMAN,
        disposition=IngressDisposition.ACCEPT_HUMAN,
    )
    address = AddressDecision(
        binding=binding,
        kind=AddressKind.DIRECT_SELF if direct else AddressKind.ABOUT_SELF,
        evidence=(
            (AddressEvidence.STRUCTURED_MENTION,)
            if direct
            else (AddressEvidence.THIRD_PERSON_REFERENCE,)
        ),
        confidence=1.0 if direct else 0.8,
        is_meta_discussion=not direct,
    )
    attention = AttentionDecision(
        binding=binding,
        level=AttentionLevel.FORCE if direct else AttentionLevel.CONSIDER,
        self_relevance=1.0 if direct else 0.7,
        response_value=0.9,
    )
    participation = ParticipationDecision(
        binding=binding,
        level=ParticipationLevel.MUST_REPLY if direct else ParticipationLevel.MAY_JOIN,
    )
    target = ReplyTarget(
        message_id=message,
        sender_key=binding.current_sender_key,
        session_id=scope,
        scope_key=scope,
        content_digest=binding.current_content_digest,
        source_kind="current_inbound",
        referenced_message_id="",
        degradation_reasons=(),
    )
    gap = KnowledgeGapDecision(
        binding=binding,
        need=KnowledgeNeed.NONE,
        requires_evidence=False,
        reason_codes=("common_knowledge",),
    )
    policy = build_guest_capability_policy(
        _principal(scope),
        configured_tool_names=(),
        conversation_mode="direct_reply",
    )
    planned = plan_action(
        ingress=ingress,
        address=address,
        attention=attention,
        participation=participation,
        reply_target=target,
        knowledge_gap=gap,
        capability_policy=policy,
        planned_action_authority=authority,
    )
    authority.inspect_plan(planned)
    return snapshot, _principal(scope), planned


class InferenceBudgetAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.epochs = GenerationEpochRegistry(max_scopes=256, now_fn=lambda: 1000.0)
        self.plans = PlannedActionAuthority(max_plans=256)

    def _authority(self, **kwargs):
        return InferenceBudgetAuthority(
            self.epochs,
            self.plans,
            max_active=kwargs.pop("max_active", 1),
            max_waiters=kwargs.pop("max_waiters", 16),
            queue_timeout_seconds=kwargs.pop("queue_timeout_seconds", 1.0),
            **kwargs,
        )

    async def test_direct_priority_overtakes_earlier_ordinary_waiter(self):
        budget = self._authority()
        holder = _plan(
            self.epochs, self.plans, scope="scope-holder", message="holder", direct=True
        )
        ordinary = _plan(
            self.epochs, self.plans, scope="scope-ordinary", message="ordinary", direct=False
        )
        direct = _plan(
            self.epochs, self.plans, scope="scope-direct", message="direct", direct=True
        )
        holder_permit = await budget.acquire_event(
            *holder,
            purpose=InferencePurpose.PRIMARY,
        )
        entered: list[str] = []
        direct_entered = asyncio.Event()
        release_direct = asyncio.Event()

        async def ordinary_work():
            entered.append("ordinary")
            return "ordinary"

        async def direct_work():
            entered.append("direct")
            direct_entered.set()
            await release_direct.wait()
            return "direct"

        ordinary_task = asyncio.create_task(
            budget.run_event_call(
                *ordinary,
                purpose=InferencePurpose.PRIMARY,
                work_factory=ordinary_work,
            )
        )
        await asyncio.sleep(0)
        direct_task = asyncio.create_task(
            budget.run_event_call(
                *direct,
                purpose=InferencePurpose.PRIMARY,
                work_factory=direct_work,
            )
        )
        await asyncio.sleep(0)
        await budget.release(holder_permit)
        await asyncio.wait_for(direct_entered.wait(), timeout=1.0)
        self.assertEqual(entered, ["direct"])
        release_direct.set()
        self.assertEqual(await direct_task, "direct")
        self.assertEqual(await ordinary_task, "ordinary")
        self.assertEqual(entered, ["direct", "ordinary"])

    async def test_primary_and_single_repair_are_the_only_event_calls(self):
        budget = self._authority(max_active=2)
        turn = _plan(
            self.epochs, self.plans, scope="scope-a", message="message-a", direct=True
        )
        primary = await budget.acquire_event(*turn, purpose=InferencePurpose.PRIMARY)
        self.assertIs(type(primary), InferencePermit)
        self.assertIs(primary.priority, InferencePriority.DIRECT)
        self.assertIs(primary.purpose, InferencePurpose.PRIMARY)
        await budget.release(primary)
        repair = await budget.acquire_event(*turn, purpose=InferencePurpose.REPAIR)
        await budget.release(repair)

        for purpose in (InferencePurpose.PRIMARY, InferencePurpose.REPAIR):
            with self.subTest(purpose=purpose.value):
                with self.assertRaisesRegex(InferenceBudgetError, "call_budget_exhausted"):
                    await budget.acquire_event(*turn, purpose=purpose)

    async def test_timeout_and_waiter_cap_do_not_create_provider_work(self):
        budget = self._authority(max_waiters=1, queue_timeout_seconds=0.01)
        holder = _plan(
            self.epochs, self.plans, scope="scope-holder", message="holder", direct=True
        )
        blocked = _plan(
            self.epochs, self.plans, scope="scope-blocked", message="blocked", direct=False
        )
        extra = _plan(
            self.epochs, self.plans, scope="scope-extra", message="extra", direct=False
        )
        permit = await budget.acquire_event(*holder, purpose=InferencePurpose.PRIMARY)
        created = 0

        def work_factory():
            nonlocal created
            created += 1
            return asyncio.sleep(0)

        blocked_task = asyncio.create_task(
            budget.run_event_call(
                *blocked,
                purpose=InferencePurpose.PRIMARY,
                work_factory=work_factory,
            )
        )
        await asyncio.sleep(0)
        with self.assertRaisesRegex(InferenceBudgetError, "waiter_limit"):
            await budget.run_event_call(
                *extra,
                purpose=InferencePurpose.PRIMARY,
                work_factory=work_factory,
            )
        with self.assertRaisesRegex(InferenceBudgetError, "queue_timeout"):
            await blocked_task
        self.assertEqual(created, 0)
        self.assertEqual(budget.waiting_count, 0)
        await budget.release(permit)

    async def test_cancel_and_base_exception_release_capacity(self):
        budget = self._authority()
        first = _plan(
            self.epochs, self.plans, scope="scope-a", message="message-a", direct=True
        )

        for error_type in (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            async def interrupted(error_type=error_type):
                raise error_type("marker")

            with self.subTest(error_type=error_type.__name__):
                with self.assertRaises(error_type):
                    await budget.run_event_call(
                        *first,
                        purpose=InferencePurpose.PRIMARY,
                        work_factory=interrupted,
                    )
                self.assertEqual(budget.active_count, 0)
                # Each purpose is one-shot. Use a new canonical turn next.
                first = _plan(
                    self.epochs,
                    self.plans,
                    scope=f"scope-{error_type.__name__}",
                    message=f"message-{error_type.__name__}",
                    direct=True,
                )

    async def test_stale_waiter_is_rejected_before_work_creation(self):
        budget = self._authority(queue_timeout_seconds=1.0)
        holder = _plan(
            self.epochs, self.plans, scope="scope-holder", message="holder", direct=True
        )
        old = _plan(
            self.epochs, self.plans, scope="scope-old", message="old", direct=False
        )
        permit = await budget.acquire_event(*holder, purpose=InferencePurpose.PRIMARY)
        created = 0

        def factory():
            nonlocal created
            created += 1
            return asyncio.sleep(0)

        waiter = asyncio.create_task(
            budget.run_event_call(
                *old,
                purpose=InferencePurpose.PRIMARY,
                work_factory=factory,
            )
        )
        await asyncio.sleep(0)
        self.epochs.advance(_envelope("scope-old", "new"))
        await budget.release(permit)
        with self.assertRaisesRegex(InferenceBudgetError, "superseded"):
            await waiter
        self.assertEqual(created, 0)

    async def test_copy_mutation_cross_authority_and_trace_fail_closed(self):
        budget = self._authority()
        turn = _plan(
            self.epochs, self.plans, scope="scope-sensitive", message="message-sensitive", direct=True
        )
        permit = await budget.acquire_event(*turn, purpose=InferencePurpose.PRIMARY)
        self.assertNotIn("scope-sensitive", repr(permit) + repr(permit.trace_metadata()))
        self.assertNotIn("message-sensitive", repr(permit) + repr(permit.trace_metadata()))
        with self.assertRaises(InferenceBudgetError):
            budget.inspect(copy.copy(permit))
        original = permit.priority
        object.__setattr__(permit, "priority", InferencePriority.PROACTIVE)
        with self.assertRaisesRegex(InferenceBudgetError, "permit_corrupt"):
            budget.inspect(permit)
        self.assertNotIn("scope-sensitive", repr(permit) + repr(permit.trace_metadata()))
        object.__setattr__(permit, "priority", original)
        budget.inspect(permit)
        with self.assertRaises(InferenceBudgetError):
            self._authority().inspect(permit)
        await budget.release(permit)

    async def test_close_fails_waiters_and_releases_active_slots(self):
        budget = self._authority()
        holder = _plan(
            self.epochs, self.plans, scope="scope-holder", message="holder", direct=True
        )
        waiter_turn = _plan(
            self.epochs, self.plans, scope="scope-waiter", message="waiter", direct=False
        )
        permit = await budget.acquire_event(*holder, purpose=InferencePurpose.PRIMARY)
        waiter = asyncio.create_task(
            budget.acquire_event(*waiter_turn, purpose=InferencePurpose.PRIMARY)
        )
        await asyncio.sleep(0)
        await budget.close()
        with self.assertRaisesRegex(InferenceBudgetError, "closed"):
            await waiter
        self.assertEqual(budget.active_count, 0)
        self.assertEqual(budget.waiting_count, 0)
        self.assertFalse(await budget.release(permit))

    async def test_exact_active_permit_can_release_after_plan_registry_eviction(self):
        plans = PlannedActionAuthority(max_plans=1)
        budget = InferenceBudgetAuthority(
            self.epochs,
            plans,
            max_active=1,
            max_waiters=2,
            queue_timeout_seconds=1.0,
        )
        turn = _plan(
            self.epochs,
            plans,
            scope="scope-holder",
            message="holder",
            direct=True,
        )
        permit = await budget.acquire_event(*turn, purpose=InferencePurpose.PRIMARY)
        _plan(
            self.epochs,
            plans,
            scope="scope-evictor",
            message="evictor",
            direct=True,
        )
        self.assertTrue(await budget.release(permit))
        self.assertEqual(budget.active_count, 0)

    async def test_active_timeout_fails_closed_until_late_permit_is_released(self):
        budget = self._authority(active_timeout_seconds=0.01)
        first = _plan(
            self.epochs, self.plans, scope="scope-timeout", message="old", direct=True
        )
        second = _plan(
            self.epochs, self.plans, scope="scope-next", message="new", direct=True
        )
        permit = await budget.acquire_event(*first, purpose=InferencePurpose.PRIMARY)
        await asyncio.sleep(0.02)
        self.assertEqual(await budget.sweep_expired(), 1)
        self.assertEqual(budget.active_count, 1)
        with self.assertRaisesRegex(InferenceBudgetError, "active_timeout"):
            await budget.acquire_event(*second, purpose=InferencePurpose.PRIMARY)
        self.assertFalse(await budget.release(permit))
        self.assertEqual(budget.active_count, 0)
        next_permit = await budget.acquire_event(
            *second,
            purpose=InferencePurpose.PRIMARY,
        )
        self.assertTrue(await budget.release(next_permit))
        self.assertEqual(
            budget.trace_metadata()["inference_expired_count"],
            1,
        )

    def test_main_primary_repair_and_proactive_paths_use_budget_authority(self):
        source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("InferenceBudgetAuthority", source)
        self.assertIn("SHIO_INFERENCE_PERMIT", source)
        self.assertIn("acquire_event", source)
        self.assertIn("run_event_call", source)
        self.assertIn("run_proactive_call", source)
        self.assertIn("await self.inference_budget.release", source)


if __name__ == "__main__":
    unittest.main()
