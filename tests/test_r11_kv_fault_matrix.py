"""R11 D-075 fault matrix using the fixed official FIFO/overlay contract."""

from __future__ import annotations

import asyncio
import unittest

try:
    from test_name_semantic_provider_routing import MAIN, SYS001
except ModuleNotFoundError:  # pragma: no cover
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import MAIN, SYS001


SCOPE = "qq:group:202"


class OfficialFifoKV:
    """Recorded SharedPreferences shape: overlay first, one FIFO, then durable DB."""

    def __init__(self, durable=None, *, paused=False, ignore_cancel=False, block_calls=()):
        self.durable = {} if durable is None else durable
        self.overlay = {}
        self.paused = paused
        self.ignore_cancel = ignore_cancel
        self.release = asyncio.Event()
        if not paused:
            self.release.set()
        self.entered = asyncio.Event()
        self.calls = []
        self.block_calls = set(block_calls)
        self.block_release = asyncio.Event()

    async def put(self, key, value):
        # SharedPreferences applies the overlay before the queued DB operation.
        self.overlay[key] = value
        self.calls.append((key, value))
        self.entered.set()
        if len(self.calls) in self.block_calls:
            try:
                await self.block_release.wait()
            except asyncio.CancelledError:
                if not self.ignore_cancel:
                    raise
                await self.block_release.wait()
            self.durable[key] = value
            return
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            if not self.ignore_cancel:
                raise
            await self.release.wait()
        self.durable[key] = value

    async def get(self, key, default=None):
        return self.overlay.get(key, self.durable.get(key, default))

    def restart(self):
        """A full process restart carries DB only, never overlay/tasks."""
        return OfficialFifoKV(self.durable)


def plugin(kv: OfficialFifoKV):
    value = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
    value.config = {"sys001": {"group": {
        "natural_group_scopes": [SCOPE], "natural_reply_cooldown_seconds": 45,
        "natural_frequency_window_minutes": 5, "natural_max_replies_per_window": 2,
        "natural_no_action_backoff_base_seconds": 2, "natural_no_action_backoff_max_seconds": 30,
    }}}
    value._natural_generations = {SCOPE: 1}
    value._natural_cadence = {SCOPE: SYS001.NaturalCadence.empty()}
    value._natural_bindings = {}
    value._natural_ready = {SCOPE: True}
    value._natural_locks = {}
    value._natural_candidates = {}
    value._natural_candidate_sequences = {}
    value._natural_active_candidate_watermarks = {}
    value._natural_terminated = False
    value._auxiliary_epoch = 1
    value._auxiliary_terminated = False
    value._continuous_scopes = {}
    value._continuous_terminated = False
    value._master_alert_terminated = False
    value._master_alert_timer = None
    value.put_kv_data = kv.put
    value.get_kv_data = kv.get
    value._NATURAL_KV_AWAIT_SECONDS = 1.0
    return value


class R11KvFaultMatrix(unittest.IsolatedAsyncioTestCase):
    async def _persist(self, instance):
        return await instance._persist_natural_state(
            scope=SCOPE, state=SYS001.NaturalCadence.empty(), generation=1, message_id="old-event"
        )

    async def test_pending_hang_times_out_fail_closed_and_leaves_no_committed_success(self):
        kv = OfficialFifoKV(paused=True)
        instance = plugin(kv)
        instance._NATURAL_KV_AWAIT_SECONDS = 0.05
        self.assertFalse(await self._persist(instance))
        self.assertFalse(instance._natural_ready[SCOPE])
        self.assertEqual("pending", kv.overlay[instance._natural_kv_key(SCOPE)]["status"])
        self.assertNotIn(instance._natural_kv_key(SCOPE), kv.durable)
        self.assertEqual(1, len(kv.calls))

    async def test_cancelled_pending_late_completion_reloads_fail_closed(self):
        kv = OfficialFifoKV(paused=True, ignore_cancel=True)
        instance = plugin(kv)
        instance._NATURAL_KV_AWAIT_SECONDS = 0.05
        task = asyncio.create_task(self._persist(instance))
        await kv.entered.wait()
        self.assertFalse(await task)
        kv.release.set()
        key = instance._natural_kv_key(SCOPE)
        for _ in range(8):
            if key in kv.durable:
                break
            await asyncio.sleep(0)
        self.assertEqual("pending", kv.durable[key]["status"])
        reloaded = plugin(kv.restart())
        reloaded._natural_ready = {}
        reloaded._natural_cadence = {}
        await reloaded._ensure_natural_scope_ready(SCOPE)
        self.assertFalse(reloaded._natural_ready[SCOPE])

    async def test_pending_then_terminate_never_commits_old_event(self):
        kv = OfficialFifoKV(paused=True, ignore_cancel=True)
        instance = plugin(kv)
        instance._NATURAL_KV_AWAIT_SECONDS = 0.05
        task = asyncio.create_task(self._persist(instance))
        await kv.entered.wait()
        terminate = asyncio.create_task(instance.terminate())
        await asyncio.sleep(0.06)
        self.assertTrue(terminate.done())
        kv.release.set()
        self.assertFalse(await task)
        await asyncio.sleep(0.01)
        self.assertEqual("pending", kv.durable[instance._natural_kv_key(SCOPE)]["status"])

    async def test_committed_record_survives_clean_process_restart_but_not_old_generation(self):
        kv = OfficialFifoKV()
        old = plugin(kv)
        self.assertTrue(await self._persist(old))
        fresh = plugin(kv.restart())
        fresh._natural_ready = {}
        fresh._natural_cadence = {}
        self.assertTrue(await fresh._ensure_natural_scope_ready(SCOPE))
        self.assertTrue(fresh._natural_ready[SCOPE])
        # The restored cadence never revives an old process/event generation.
        self.assertFalse(await old._persist_natural_state(
            scope=SCOPE, state=SYS001.NaturalCadence.empty(), generation=0, message_id="stale"
        ))

    async def test_fifo_pending_then_committed_has_one_transaction_and_no_overlay_leak_after_restart(self):
        kv = OfficialFifoKV()
        instance = plugin(kv)
        self.assertTrue(await self._persist(instance))
        self.assertEqual(["pending", "committed"], [row[1]["status"] for row in kv.calls])
        self.assertEqual(kv.calls[0][1]["transaction_id"], kv.calls[1][1]["transaction_id"])
        restarted = kv.restart()
        self.assertEqual({}, restarted.overlay)
        self.assertEqual("committed", restarted.durable[instance._natural_kv_key(SCOPE)]["status"])

    async def test_committed_hang_is_visible_in_same_process_overlay_but_not_durable_restart(self):
        kv = OfficialFifoKV(ignore_cancel=True, block_calls=(2,))
        instance = plugin(kv)
        # First write completes; the second is deterministically blocked by
        # OfficialFifoKV's call-2 Event, not by scheduler startup timing.
        instance._NATURAL_KV_AWAIT_SECONDS = 1.0
        self.assertFalse(await self._persist(instance))
        key = instance._natural_kv_key(SCOPE)
        self.assertEqual("committed", kv.overlay[key]["status"])
        # Hot reload in the same fixed AstrBot process sees the official overlay.
        hot = plugin(kv)
        hot._natural_ready = {}
        self.assertTrue(await hot._ensure_natural_scope_ready(SCOPE))
        self.assertTrue(hot._natural_ready[SCOPE])
        # A process restart has only durable pending data until the queued write ends.
        cold = plugin(kv.restart())
        cold._natural_ready = {}
        self.assertFalse(await cold._ensure_natural_scope_ready(SCOPE))
        self.assertFalse(cold._natural_ready[SCOPE])
        kv.block_release.set()
        await asyncio.sleep(0.01)
        durable = plugin(kv.restart())
        durable._natural_ready = {}
        self.assertTrue(await durable._ensure_natural_scope_ready(SCOPE))
        self.assertTrue(durable._natural_ready[SCOPE])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
