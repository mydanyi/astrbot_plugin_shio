"""R16 Master authority races for one process-local plugin instance."""
from __future__ import annotations

import asyncio
import time
import unittest

try:
    from test_name_semantic_provider_routing import SYS001
    import test_r13_master_termination_regressions as R13
except ModuleNotFoundError:  # pragma: no cover
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import SYS001
    from astrbot_plugin_shio.tests import test_r13_master_termination_regressions as R13


class R16MasterAuthorityRevisionTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, kv: R13.MasterKVSpy | None = None):
        kv = kv or R13.MasterKVSpy({"master_alert_state_v1": {"legacy": True}})
        plugin = R13.R13MasterTerminationTests().plugin(kv)
        plugin.config["sys001"]["master_alert"]["consecutive_threshold"] = 100
        return plugin, kv

    def _event_snapshot(self, umo="qq:person:master"):
        event = R13.RequestEvent()
        event.unified_msg_origin = umo
        snapshot = R13.R13MasterTerminationTests()._private_snapshot(event)
        return event, snapshot

    async def _wait_for_terminating(self, plugin):
        for _ in range(50):
            if plugin._master_alert_terminating:
                return
            await asyncio.sleep(0)
        self.fail("termination pre-fence was not published")

    async def test_two_terminal_updates_serialize_without_master_kv(self):
        plugin, kv = self._plugin()
        plugin._master_alert_record = SYS001.MasterAlertRecord()
        first_event, first_snapshot = self._event_snapshot()
        second_event, second_snapshot = self._event_snapshot()
        lock = plugin._master_alert_mutex()
        await lock.acquire()
        first = asyncio.create_task(plugin._record_master_alert_terminal(
            first_event, first_snapshot, success=False, terminal_reason="final_error"
        ))
        second = asyncio.create_task(plugin._record_master_alert_terminal(
            second_event, second_snapshot, success=False, terminal_reason="final_error"
        ))
        await asyncio.sleep(0)
        lock.release()
        await asyncio.gather(first, second)
        self.assertEqual(1, plugin._master_alert_record.consecutive_count)
        self.assertTrue(second_event.get_extra("shio.sys001.error_master_done"))
        self.assertEqual("final_error", plugin._master_alert_record.error_type)
        kv.assert_unused(self)
        await plugin.terminate()

    async def test_binding_then_terminal_keeps_contact_and_failure_under_mutex(self):
        plugin, kv = self._plugin()
        plugin._master_alert_record = SYS001.MasterAlertRecord()
        event, snapshot = self._event_snapshot("qq:person:new-master")
        lock = plugin._master_alert_mutex()
        await lock.acquire()
        binding = asyncio.create_task(plugin.capture_master_alert_binding(event, snapshot))
        await asyncio.sleep(0)
        terminal = asyncio.create_task(plugin._record_master_alert_terminal(
            event, snapshot, success=False, terminal_reason="final_error"
        ))
        lock.release()
        self.assertTrue(await binding)
        await terminal
        record = plugin._master_alert_record
        self.assertEqual("qq:person:new-master", record.master_umo)
        self.assertEqual(1, record.consecutive_count)
        self.assertEqual("final_error", record.error_type)
        kv.assert_unused(self)
        await plugin.terminate()

    async def test_terminal_then_binding_replaces_only_contact_under_mutex(self):
        plugin, kv = self._plugin()
        plugin._master_alert_record = SYS001.MasterAlertRecord()
        event, snapshot = self._event_snapshot("qq:person:new-master")
        lock = plugin._master_alert_mutex()
        await lock.acquire()
        terminal = asyncio.create_task(plugin._record_master_alert_terminal(
            event, snapshot, success=False, terminal_reason="final_error"
        ))
        await asyncio.sleep(0)
        binding = asyncio.create_task(plugin.capture_master_alert_binding(event, snapshot))
        lock.release()
        await terminal
        self.assertTrue(await binding)
        record = plugin._master_alert_record
        self.assertEqual("qq:person:new-master", record.master_umo)
        self.assertEqual(1, record.consecutive_count)
        self.assertEqual("final_error", record.error_type)
        kv.assert_unused(self)
        await plugin.terminate()

    async def test_every_runtime_publication_occurs_while_master_mutex_is_owned(self):
        plugin, kv = self._plugin()
        plugin._master_alert_record = SYS001.MasterAlertRecord()
        lock = plugin._master_alert_mutex()
        observed: list[bool] = []
        original = plugin._publish_master_alert_record_locked

        def observe(record, *, ready=True):
            observed.append(lock.locked())
            return original(record, ready=ready)

        plugin._publish_master_alert_record_locked = observe
        event, snapshot = self._event_snapshot()
        self.assertTrue(await plugin.capture_master_alert_binding(event, snapshot))
        await plugin._record_master_alert_terminal(
            event, snapshot, success=False, terminal_reason="final_error"
        )
        await plugin.terminate()
        self.assertGreaterEqual(len(observed), 3)
        self.assertTrue(all(observed))
        kv.assert_unused(self)

    async def test_termination_prefence_blocks_terminal_waiting_on_mutex(self):
        plugin, kv = self._plugin()
        plugin._master_alert_record = SYS001.MasterAlertRecord()
        event, snapshot = self._event_snapshot()
        lock = plugin._master_alert_mutex()
        await lock.acquire()
        terminal = asyncio.create_task(plugin._record_master_alert_terminal(
            event, snapshot, success=False, terminal_reason="final_error"
        ))
        await asyncio.sleep(0)
        ending = asyncio.create_task(plugin.terminate())
        await self._wait_for_terminating(plugin)
        lock.release()
        await terminal
        await ending
        self.assertEqual(0, plugin._master_alert_record.consecutive_count)
        self.assertFalse(plugin._master_alert_ready)
        kv.assert_unused(self)

    async def test_termination_prefence_blocks_binding_waiting_on_mutex(self):
        plugin, kv = self._plugin()
        plugin._master_alert_record = SYS001.MasterAlertRecord()
        event, snapshot = self._event_snapshot("qq:person:late")
        lock = plugin._master_alert_mutex()
        await lock.acquire()
        binding = asyncio.create_task(plugin.capture_master_alert_binding(event, snapshot))
        await asyncio.sleep(0)
        ending = asyncio.create_task(plugin.terminate())
        await self._wait_for_terminating(plugin)
        lock.release()
        self.assertFalse(await binding)
        await ending
        self.assertEqual("", plugin._master_alert_record.master_umo)
        self.assertFalse(plugin._master_alert_ready)
        kv.assert_unused(self)

    async def test_late_timer_after_termination_never_sends_or_publishes(self):
        plugin, kv = self._plugin()
        deadline = time.time() - 1.0
        plugin._master_alert_record = SYS001.MasterAlertRecord(
            master_umo="qq:person:bound",
            report_id="late",
            report_status="pending",
            quiet_deadline=deadline,
        )
        await plugin.terminate()
        revision = plugin._master_alert_revision
        await plugin._master_alert_timer_worker("late", deadline)
        self.assertEqual(revision, plugin._master_alert_revision)
        self.assertEqual([], plugin.context.send_trace)
        self.assertIsNone(plugin._master_alert_timer)
        kv.assert_unused(self)

    async def test_revision_advances_only_for_local_transitions_and_never_kv(self):
        plugin, kv = self._plugin()
        # Make the immediate-send transitions independent of the current hour.
        plugin.config["sys001"]["master_alert"]["master_alert_quiet_enabled"] = False
        plugin._master_alert_record = SYS001.MasterAlertRecord()
        start = plugin._master_alert_revision
        event, snapshot = self._event_snapshot()
        self.assertTrue(await plugin.capture_master_alert_binding(event, snapshot))
        after_binding = plugin._master_alert_revision
        await plugin._record_master_alert_terminal(
            event, snapshot, success=False, terminal_reason="final_error"
        )
        after_terminal = plugin._master_alert_revision
        self.assertEqual(start + 1, after_binding)
        # Fault, submitting, and rejected-send transitions each hold the lock.
        self.assertEqual(after_binding + 3, after_terminal)
        kv.assert_unused(self)
        await plugin.terminate()
        self.assertEqual(after_terminal + 1, plugin._master_alert_revision)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
