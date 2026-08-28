import asyncio
import copy
import unittest

from astrbot_plugin_shio.core.generation_cancellation import SupersededGeneration
from astrbot_plugin_shio.core.generation_epoch import GenerationEpochRegistry
from astrbot_plugin_shio.core.identity import PrincipalContext, TurnEnvelope
from astrbot_plugin_shio.core.scope_concurrency import (
    ScopeConcurrencyError,
    ScopeWorkKind,
    TurnScopeCoordinator,
)


def envelope(scope: str, message: str, *, sender: str = "user-a") -> TurnEnvelope:
    return TurnEnvelope(
        session_id=scope,
        message_id=message,
        scope_key=scope,
        sender_key=f"{scope}|user:{sender}",
        sender_id=sender,
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


def principal(scope: str, *, sender: str = "user-a", owner: bool = False):
    return PrincipalContext(
        sender_key=f"{scope}|user:{sender}",
        sender_id=sender,
        is_owner=owner,
        relationship_role="owner" if owner else "group_peer",
        verification_source="adapter:configured_owner_id" if owner else "adapter:miss",
    )


class TurnScopeCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_cancels_active_and_waiting_work_then_rejects_restart(self):
        epochs = GenerationEpochRegistry(now_fn=lambda: 1000.0)
        coordinator = TurnScopeCoordinator(epochs, max_parallel_scopes=1)
        snapshot = epochs.advance(envelope("scope-a", "message-a"))
        entered = asyncio.Event()
        created = 0

        async def holding():
            entered.set()
            await asyncio.Event().wait()

        def queued_factory():
            nonlocal created
            created += 1
            return asyncio.sleep(0)

        active = asyncio.create_task(
            coordinator.run_event(
                snapshot,
                principal("scope-a"),
                kind=ScopeWorkKind.DIRECT,
                work_factory=holding,
            )
        )
        await entered.wait()
        waiting = asyncio.create_task(
            coordinator.run_event(
                snapshot,
                principal("scope-a"),
                kind=ScopeWorkKind.REACT,
                work_factory=queued_factory,
            )
        )
        await asyncio.sleep(0)
        await coordinator.close()
        results = await asyncio.gather(active, waiting, return_exceptions=True)
        self.assertTrue(all(isinstance(value, asyncio.CancelledError) for value in results))
        self.assertEqual(created, 0)
        self.assertEqual(coordinator.active_count, 0)
        self.assertEqual(coordinator.waiting_count, 0)
        self.assertTrue(coordinator.trace_metadata()["scope_concurrency_closed"])
        with self.assertRaisesRegex(ScopeConcurrencyError, "coordinator_closed"):
            await coordinator.run_event(
                snapshot,
                principal("scope-a"),
                kind=ScopeWorkKind.DIRECT,
                work_factory=lambda: asyncio.sleep(0),
            )

    async def test_same_scope_runs_in_arrival_order_without_overlap(self):
        epochs = GenerationEpochRegistry(now_fn=lambda: 1000.0)
        coordinator = TurnScopeCoordinator(epochs, max_parallel_scopes=4)
        snapshot = epochs.advance(envelope("scope-a", "message-a"))
        release_first = asyncio.Event()
        first_entered = asyncio.Event()
        order: list[str] = []

        async def first_work():
            order.append("first-enter")
            first_entered.set()
            await release_first.wait()
            order.append("first-exit")
            return "first"

        async def second_work():
            order.append("second-enter")
            order.append("second-exit")
            return "second"

        first = asyncio.create_task(
            coordinator.run_event(
                snapshot,
                principal("scope-a"),
                kind=ScopeWorkKind.DIRECT,
                work_factory=first_work,
            )
        )
        await first_entered.wait()
        second = asyncio.create_task(
            coordinator.run_event(
                snapshot,
                principal("scope-a"),
                kind=ScopeWorkKind.ACTION,
                work_factory=second_work,
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(order, ["first-enter"])
        self.assertEqual(coordinator.active_count, 1)
        self.assertEqual(coordinator.waiting_count, 1)

        release_first.set()
        self.assertEqual(await first, "first")
        self.assertEqual(await second, "second")
        self.assertEqual(
            order,
            ["first-enter", "first-exit", "second-enter", "second-exit"],
        )
        self.assertEqual(coordinator.active_count, 0)
        self.assertEqual(coordinator.waiting_count, 0)

    async def test_cross_scope_parallelism_is_bounded(self):
        epochs = GenerationEpochRegistry(now_fn=lambda: 1000.0)
        coordinator = TurnScopeCoordinator(epochs, max_parallel_scopes=2)
        snapshots = [
            epochs.advance(envelope(f"scope-{index}", f"message-{index}"))
            for index in range(3)
        ]
        release = asyncio.Event()
        two_entered = asyncio.Event()
        active = 0
        peak = 0

        async def work():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_entered.set()
            await release.wait()
            active -= 1
            return True

        tasks = [
            asyncio.create_task(
                coordinator.run_event(
                    snapshot,
                    principal(f"scope-{index}"),
                    kind=ScopeWorkKind.DIRECT,
                    work_factory=work,
                )
            )
            for index, snapshot in enumerate(snapshots)
        ]
        await two_entered.wait()
        await asyncio.sleep(0)
        self.assertEqual(peak, 2)
        self.assertEqual(coordinator.active_count, 2)
        self.assertEqual(coordinator.waiting_count, 1)
        release.set()
        self.assertEqual(await asyncio.gather(*tasks), [True, True, True])
        self.assertEqual(coordinator.active_count, 0)

    async def test_principal_is_snapshotted_and_never_shared_with_work(self):
        epochs = GenerationEpochRegistry(now_fn=lambda: 1000.0)
        coordinator = TurnScopeCoordinator(epochs, max_parallel_scopes=2)
        left_snapshot = epochs.advance(envelope("scope-left", "message-left"))
        right_snapshot = epochs.advance(envelope("scope-right", "message-right"))
        left_principal = principal("scope-left", owner=False)
        right_principal = principal("scope-right", owner=True)
        release = asyncio.Event()
        entered = asyncio.Event()
        count = 0

        async def work():
            nonlocal count
            count += 1
            if count == 2:
                entered.set()
            await release.wait()
            return True

        left = asyncio.create_task(
            coordinator.run_event(
                left_snapshot,
                left_principal,
                kind=ScopeWorkKind.DIRECT,
                work_factory=work,
            )
        )
        right = asyncio.create_task(
            coordinator.run_event(
                right_snapshot,
                right_principal,
                kind=ScopeWorkKind.REACT,
                work_factory=work,
            )
        )
        await entered.wait()
        object.__setattr__(left_principal, "is_owner", True)
        object.__setattr__(right_principal, "sender_key", "scope-left|user:user-a")
        metadata = coordinator.trace_metadata()
        self.assertEqual(metadata["scope_concurrency_active"], 2)
        self.assertNotIn("scope-left", repr(metadata))
        self.assertNotIn("user-a", repr(metadata))
        release.set()
        self.assertEqual(await asyncio.gather(left, right), [True, True])

    async def test_waiter_cancel_and_base_exception_release_every_slot(self):
        epochs = GenerationEpochRegistry(now_fn=lambda: 1000.0)
        coordinator = TurnScopeCoordinator(epochs, max_parallel_scopes=1)
        snapshot = epochs.advance(envelope("scope-a", "message-a"))
        release = asyncio.Event()
        entered = asyncio.Event()

        async def holding():
            entered.set()
            await release.wait()

        first = asyncio.create_task(
            coordinator.run_event(
                snapshot,
                principal("scope-a"),
                kind=ScopeWorkKind.DIRECT,
                work_factory=holding,
            )
        )
        await entered.wait()
        waiter = asyncio.create_task(
            coordinator.run_event(
                snapshot,
                principal("scope-a"),
                kind=ScopeWorkKind.REACT,
                work_factory=lambda: asyncio.sleep(0),
            )
        )
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(coordinator.waiting_count, 0)
        release.set()
        await first

        for error_type in (KeyboardInterrupt, SystemExit):
            async def interrupted(error_type=error_type):
                raise error_type("marker-must-not-stick")

            with self.subTest(error_type=error_type.__name__):
                with self.assertRaises(error_type):
                    await coordinator.run_event(
                        snapshot,
                        principal("scope-a"),
                        kind=ScopeWorkKind.ACTION,
                        work_factory=interrupted,
                    )
                self.assertEqual(coordinator.active_count, 0)
                self.assertEqual(coordinator.waiting_count, 0)
        self.assertEqual(
            await coordinator.run_event(
                snapshot,
                principal("scope-a"),
                kind=ScopeWorkKind.DIRECT,
                work_factory=lambda: asyncio.sleep(0, result="recovered"),
            ),
            "recovered",
        )

    async def test_old_epoch_finishes_first_but_result_is_rejected(self):
        epochs = GenerationEpochRegistry(now_fn=lambda: 1000.0)
        coordinator = TurnScopeCoordinator(epochs, max_parallel_scopes=2)
        old_snapshot = epochs.advance(envelope("scope-a", "message-old"))
        old_entered = asyncio.Event()
        release_old = asyncio.Event()

        async def old_work():
            old_entered.set()
            await release_old.wait()
            return "stale"

        old = asyncio.create_task(
            coordinator.run_event(
                old_snapshot,
                principal("scope-a"),
                kind=ScopeWorkKind.DIRECT,
                work_factory=old_work,
            )
        )
        await old_entered.wait()
        new_snapshot = epochs.advance(envelope("scope-a", "message-new"))
        new = asyncio.create_task(
            coordinator.run_event(
                new_snapshot,
                principal("scope-a"),
                kind=ScopeWorkKind.DIRECT,
                work_factory=lambda: asyncio.sleep(0, result="current"),
            )
        )
        await asyncio.sleep(0)
        self.assertFalse(new.done())
        release_old.set()
        with self.assertRaises(SupersededGeneration):
            await old
        self.assertEqual(await new, "current")

    async def test_queue_caps_fail_closed_without_creating_work(self):
        epochs = GenerationEpochRegistry(now_fn=lambda: 1000.0)
        coordinator = TurnScopeCoordinator(
            epochs,
            max_parallel_scopes=1,
            max_waiters_per_scope=1,
            max_waiters_total=1,
        )
        snapshot = epochs.advance(envelope("scope-a", "message-a"))
        release = asyncio.Event()
        entered = asyncio.Event()
        created = 0

        async def holding():
            entered.set()
            await release.wait()

        def should_not_run():
            nonlocal created
            created += 1
            return asyncio.sleep(0)

        first = asyncio.create_task(
            coordinator.run_event(
                snapshot,
                principal("scope-a"),
                kind=ScopeWorkKind.DIRECT,
                work_factory=holding,
            )
        )
        await entered.wait()
        queued = asyncio.create_task(
            coordinator.run_event(
                snapshot,
                principal("scope-a"),
                kind=ScopeWorkKind.DIRECT,
                work_factory=lambda: asyncio.sleep(0),
            )
        )
        await asyncio.sleep(0)
        with self.assertRaisesRegex(ScopeConcurrencyError, "scope_waiter_limit"):
            await coordinator.run_event(
                snapshot,
                principal("scope-a"),
                kind=ScopeWorkKind.ACTION,
                work_factory=should_not_run,
            )
        self.assertEqual(created, 0)
        release.set()
        await first
        await queued

    async def test_exact_snapshot_principal_and_kind_are_required(self):
        epochs = GenerationEpochRegistry(now_fn=lambda: 1000.0)
        coordinator = TurnScopeCoordinator(epochs)
        snapshot = epochs.advance(envelope("scope-a", "message-a"))

        for bad_snapshot in (copy.copy(snapshot), object()):
            with self.assertRaises((ScopeConcurrencyError, SupersededGeneration)):
                await coordinator.run_event(
                    bad_snapshot,
                    principal("scope-a"),
                    kind=ScopeWorkKind.DIRECT,
                    work_factory=lambda: asyncio.sleep(0),
                )
        with self.assertRaises(ScopeConcurrencyError):
            await coordinator.run_event(
                snapshot,
                principal("scope-other"),
                kind=ScopeWorkKind.DIRECT,
                work_factory=lambda: asyncio.sleep(0),
            )
        with self.assertRaises(ScopeConcurrencyError):
            await coordinator.run_event(
                snapshot,
                principal("scope-a"),
                kind="direct",
                work_factory=lambda: asyncio.sleep(0),
            )

    async def test_idle_scope_lanes_are_bounded(self):
        epochs = GenerationEpochRegistry(max_scopes=128, now_fn=lambda: 1000.0)
        coordinator = TurnScopeCoordinator(
            epochs,
            max_parallel_scopes=4,
            max_scope_lanes=8,
        )
        for index in range(64):
            scope = f"scope-{index}"
            snapshot = epochs.advance(envelope(scope, f"message-{index}"))
            await coordinator.run_event(
                snapshot,
                principal(scope),
                kind=ScopeWorkKind.DIRECT,
                work_factory=lambda: asyncio.sleep(0),
            )
        metadata = coordinator.trace_metadata()
        self.assertLessEqual(metadata["scope_concurrency_lane_count"], 8)
        self.assertEqual(metadata["scope_concurrency_active"], 0)
        self.assertEqual(metadata["scope_concurrency_waiting"], 0)

    def test_main_routes_all_shio_owned_async_hot_paths_through_coordinator(self):
        source = (
            __import__("pathlib")
            .Path(__file__)
            .resolve()
            .parents[1]
            .joinpath("main.py")
            .read_text(encoding="utf-8")
        )

        self.assertIn("self.scope_concurrency = TurnScopeCoordinator", source)
        self.assertIn("self.scope_concurrency.run_proactive", source)
        # Direct/natural Meme selection belongs to Manager's normal hook and
        # no longer creates a fifth Shio-owned post-send async path.
        self.assertGreaterEqual(source.count("self.scope_concurrency.run_event"), 4)
        self.assertIn("kind=ScopeWorkKind.REACT", source)
        self.assertIn("ScopeWorkKind.ACTION", source)
        self.assertIn("ScopeWorkKind.DIRECT", source)


if __name__ == "__main__":
    unittest.main()
