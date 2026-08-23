import asyncio
import unittest

from astrbot_plugin_shio.core.generation_cancellation import (
    GenerationTaskRegistry,
    SupersededGeneration,
    provider_supports_cancellation,
)
from astrbot_plugin_shio.core.generation_epoch import GenerationEpochRegistry
from astrbot_plugin_shio.core.identity import TurnEnvelope


def envelope(scope: str, message: str) -> TurnEnvelope:
    return TurnEnvelope(
        session_id=scope,
        message_id=message,
        scope_key=scope,
        sender_key=f"{scope}|user:a",
        sender_id="user-a",
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


class GenerationTaskRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_cancels_all_owned_tasks_and_rejects_new_work(self):
        epochs = GenerationEpochRegistry(now_fn=lambda: 1000.0)
        registry = GenerationTaskRegistry(epochs)
        safe_snapshot = epochs.advance(envelope("scope-safe", "message-safe"))
        unsafe_snapshot = epochs.advance(envelope("scope-unsafe", "message-unsafe"))
        entered = asyncio.Event()
        entered_count = 0

        async def hanging():
            nonlocal entered_count
            entered_count += 1
            if entered_count == 2:
                entered.set()
            await asyncio.Event().wait()

        tasks = (
            asyncio.create_task(registry.run(safe_snapshot, hanging(), cancel_safe=True)),
            asyncio.create_task(registry.run(unsafe_snapshot, hanging(), cancel_safe=False)),
        )
        await entered.wait()
        await registry.close()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(isinstance(value, asyncio.CancelledError) for value in results))
        self.assertEqual(registry.active_count, 0)
        self.assertTrue(registry.closed)

        blocked = asyncio.sleep(0)
        with self.assertRaisesRegex(SupersededGeneration, "registry_closed"):
            await registry.run(safe_snapshot, blocked, cancel_safe=True)

    async def test_cancel_safe_old_task_is_cancelled_by_new_epoch(self):
        epochs = GenerationEpochRegistry(now_fn=lambda: 1000.0)
        registry = GenerationTaskRegistry(epochs)
        old_snapshot = epochs.advance(envelope("scope-a", "message-a"))
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.Event().wait()

        old = asyncio.create_task(
            registry.run(old_snapshot, slow(), cancel_safe=True)
        )
        await started.wait()

        new_snapshot = epochs.advance(envelope("scope-a", "message-b"))
        self.assertEqual(registry.cancel_older(new_snapshot), 1)
        with self.assertRaises(SupersededGeneration):
            await old
        self.assertEqual(registry.active_count, 0)

    async def test_unsafe_task_is_not_cancelled_but_result_is_discarded_by_epoch(self):
        epochs = GenerationEpochRegistry(now_fn=lambda: 1000.0)
        registry = GenerationTaskRegistry(epochs)
        old_snapshot = epochs.advance(envelope("scope-a", "message-a"))
        release = asyncio.Event()

        async def slow():
            await release.wait()
            return "finished-but-must-be-validated"

        old = asyncio.create_task(
            registry.run(old_snapshot, slow(), cancel_safe=False)
        )
        await asyncio.sleep(0)

        new_snapshot = epochs.advance(envelope("scope-a", "message-b"))
        self.assertEqual(registry.cancel_older(new_snapshot), 0)
        release.set()
        with self.assertRaises(SupersededGeneration):
            await old
        self.assertEqual(registry.active_count, 0)

    async def test_new_epoch_only_cancels_same_scope(self):
        epochs = GenerationEpochRegistry(now_fn=lambda: 1000.0)
        registry = GenerationTaskRegistry(epochs)
        other_snapshot = epochs.advance(envelope("scope-b", "message-a"))
        release = asyncio.Event()
        started = asyncio.Event()

        async def slow():
            started.set()
            await release.wait()
            return "ok"

        other = asyncio.create_task(
            registry.run(other_snapshot, slow(), cancel_safe=True)
        )
        await started.wait()

        current = epochs.advance(envelope("scope-a", "message-b"))
        self.assertEqual(registry.cancel_older(current), 0)
        release.set()
        self.assertEqual(await other, "ok")

    def test_provider_requires_explicit_cancellation_capability(self):
        explicit = type("Provider", (), {"supports_cancellation": True})()
        metadata = type(
            "Provider",
            (),
            {"metadata": {"supports_cancellation": True}},
        )()
        ordinary = type("Provider", (), {})()

        self.assertTrue(provider_supports_cancellation(explicit))
        self.assertTrue(provider_supports_cancellation(metadata))
        self.assertFalse(provider_supports_cancellation(ordinary))
        self.assertFalse(provider_supports_cancellation(None))


if __name__ == "__main__":
    unittest.main()
