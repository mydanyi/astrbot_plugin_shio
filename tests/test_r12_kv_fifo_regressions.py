"""R12 recorded-failure oracle: public waiters do not own AstrBot's FIFO writer."""
from __future__ import annotations

import asyncio
import unittest

try:
    from test_r11_kv_fault_matrix import SCOPE, plugin
    from test_name_semantic_provider_routing import SYS001
except ModuleNotFoundError:  # pragma: no cover
    from astrbot_plugin_shio.tests.test_r11_kv_fault_matrix import SCOPE, plugin
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import SYS001


class RecordedFifoKV:
    """Recorded fixed-blob topology: overlay, Queue, one writer and completions."""
    def __init__(self, durable=None, *, block_head=False):
        self.durable = {} if durable is None else durable
        self.overlay = {}
        self.queue = asyncio.Queue()
        self.gate = asyncio.Event()
        if not block_head:
            self.gate.set()
        self.writer = None
        self.enqueued = asyncio.Event()
        self.second_enqueued = asyncio.Event()
        self.put_count = 0
        self.head_taken = asyncio.Event()
        self.order = []

    async def _drain(self):
        while True:
            key, value, completion = await self.queue.get()
            try:
                self.head_taken.set()
                await self.gate.wait()
                self.durable[key] = value
                self.order.append((key, value["status"], value.get("transaction_id", "")))
                if not completion.done():
                    completion.set_result(None)
            finally:
                self.queue.task_done()
            if self.queue.empty():
                return

    async def put(self, key, value):
        self.overlay[key] = value
        completion = asyncio.get_running_loop().create_future()
        await self.queue.put((key, value, completion))
        self.put_count += 1
        if self.put_count >= 2:
            self.second_enqueued.set()
        self.enqueued.set()
        if self.writer is None or self.writer.done():
            self.writer = asyncio.create_task(self._drain())
        await completion

    async def get(self, key, default=None):
        return self.overlay.get(key, self.durable.get(key, default))

    def restart(self):
        return RecordedFifoKV(self.durable)


class R12KvFifoRegression(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_outer_waiter_fails_closed_while_writer_keeps_pending(self):
        kv = RecordedFifoKV(block_head=True)
        instance = plugin(kv)
        instance._NATURAL_KV_AWAIT_SECONDS = 1.0
        task = asyncio.create_task(instance._persist_natural_state(
            scope=SCOPE, state=SYS001.NaturalCadence.empty(), generation=1, message_id="r12"
        ))
        await kv.enqueued.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(instance._natural_ready[SCOPE])
        self.assertEqual("pending", kv.overlay[instance._natural_kv_key(SCOPE)]["status"])
        kv.gate.set()
        await asyncio.sleep(0)
        self.assertEqual("pending", kv.durable[instance._natural_kv_key(SCOPE)]["status"])

    async def test_head_of_line_blocks_committed_until_pending_drains_in_order(self):
        kv = RecordedFifoKV(block_head=True)
        instance = plugin(kv)
        instance._NATURAL_KV_AWAIT_SECONDS = 1.0
        pending = asyncio.create_task(instance._persist_natural_state(
            scope=SCOPE, state=SYS001.NaturalCadence.empty(), generation=1, message_id="r12"
        ))
        await kv.enqueued.wait()
        self.assertEqual(["pending"], [row[1]["status"] for row in list(kv.queue._queue)])
        kv.gate.set()
        self.assertTrue(await pending)
        self.assertEqual(["pending", "committed"], [row[1] for row in kv.order])
        cold = plugin(kv.restart())
        cold._NATURAL_KV_AWAIT_SECONDS = 1.0
        cold._natural_ready = {}
        await cold.initialize()
        self.assertTrue(cold._natural_ready[SCOPE])

    async def test_blocked_old_head_precedes_hot_instance_pending_then_committed(self):
        """The follower is enqueued after the writer has already taken its head."""
        kv = RecordedFifoKV(block_head=True)
        old_key = "natural_respondstage_completed:old"
        old = asyncio.create_task(kv.put(old_key, {"status": "committed", "transaction_id": "old"}))
        await kv.head_taken.wait()
        hot = plugin(kv)
        hot._NATURAL_KV_AWAIT_SECONDS = 1.0
        follower = asyncio.create_task(hot._persist_natural_state(
            scope=SCOPE, state=SYS001.NaturalCadence.empty(), generation=1, message_id="hot"
        ))
        await kv.second_enqueued.wait()
        self.assertEqual({}, kv.durable)
        kv.gate.set()
        await old
        self.assertTrue(await follower)
        self.assertEqual(["committed", "pending", "committed"], [row[1] for row in kv.order])
        self.assertEqual(kv.order[1][2], kv.order[2][2])
        fresh = kv.restart()
        cold = plugin(fresh)
        cold._NATURAL_KV_AWAIT_SECONDS = 1.0
        cold._natural_ready = {}
        cold._natural_cadence = {}
        await cold.initialize()
        self.assertTrue(cold._natural_ready[SCOPE])
        self.assertEqual("committed", fresh.durable[hot._natural_kv_key(SCOPE)]["status"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
