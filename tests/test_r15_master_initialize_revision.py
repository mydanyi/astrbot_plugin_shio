"""R15 regressions for process-local Master initialization and reload."""
from __future__ import annotations

import asyncio
import unittest

try:
    from test_name_semantic_provider_routing import SYS001
    import test_r13_master_termination_regressions as R13
except ModuleNotFoundError:  # pragma: no cover
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import SYS001
    from astrbot_plugin_shio.tests import test_r13_master_termination_regressions as R13


class R15MasterInitializeRevisionTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, kv: R13.MasterKVSpy):
        plugin = R13.R13MasterTerminationTests().plugin(kv)
        plugin._initialized = False
        plugin._initialize_lock = asyncio.Lock()
        plugin._master_alert_record = SYS001.MasterAlertRecord(
            master_umo="qq:person:old",
            error_type="old",
            consecutive_count=2,
            window_started_at=1.0,
            report_id="old-pending",
            report_status="pending",
        )
        plugin._master_alert_ready = True
        return plugin

    async def test_initialize_ignores_legacy_pending_and_uses_zero_master_kv(self):
        kv = R13.MasterKVSpy({
            "master_alert_state_v1": {"report_status": "pending", "legacy": True}
        })
        plugin = self._plugin(kv)
        await plugin.initialize()
        self.assertEqual(SYS001.MasterAlertRecord(), plugin._master_alert_record)
        self.assertTrue(plugin._master_alert_ready)
        self.assertIsNone(plugin._master_alert_timer)
        self.assertEqual([], plugin.context.send_trace)
        kv.assert_unused(self)
        await plugin.terminate()

    async def test_concurrent_initialize_publishes_one_fresh_local_record(self):
        kv = R13.MasterKVSpy({"master_alert_state_v1": RuntimeError("must not read")})
        plugin = self._plugin(kv)
        before = plugin._master_alert_revision
        await asyncio.gather(plugin.initialize(), plugin.initialize())
        self.assertEqual(SYS001.MasterAlertRecord(), plugin._master_alert_record)
        self.assertEqual(before + 1, plugin._master_alert_revision)
        self.assertTrue(plugin._master_alert_ready)
        kv.assert_unused(self)
        await plugin.terminate()

    async def test_second_instance_does_not_inherit_binding_or_failure_count(self):
        kv = R13.MasterKVSpy({"master_alert_state_v1": {"legacy": True}})
        first = self._plugin(kv)
        first._initialized = True
        first._master_alert_record = SYS001.MasterAlertRecord(
            master_umo="qq:person:bound",
            error_type="final_error",
            consecutive_count=3,
            window_started_at=1.0,
        )
        second = self._plugin(kv)
        await second.initialize()
        self.assertEqual("", second._master_alert_record.master_umo)
        self.assertEqual(0, second._master_alert_record.consecutive_count)
        self.assertEqual("none", second._master_alert_record.report_status)
        kv.assert_unused(self)
        await first.terminate()
        await second.terminate()

    async def test_second_instance_drops_pending_timer_and_never_replays_send(self):
        kv = R13.MasterKVSpy({"master_alert_state_v1": {"legacy": True}})
        first = self._plugin(kv)
        first._master_alert_record = SYS001.MasterAlertRecord(
            master_umo="qq:person:bound",
            report_id="old-pending",
            report_status="pending",
            quiet_deadline=9_999_999_999.0,
        )
        second = self._plugin(kv)
        await second.initialize()
        await asyncio.sleep(0)
        self.assertEqual(SYS001.MasterAlertRecord(), second._master_alert_record)
        self.assertIsNone(second._master_alert_timer)
        self.assertEqual(set(), second._master_alert_send_tasks)
        self.assertEqual([], second.context.send_trace)
        kv.assert_unused(self)
        await first.terminate()
        await second.terminate()

    async def test_terminated_instance_cannot_be_revived_by_initialize(self):
        kv = R13.MasterKVSpy({"master_alert_state_v1": {"legacy": True}})
        plugin = self._plugin(kv)
        await plugin.terminate()
        revision = plugin._master_alert_revision
        await plugin.initialize()
        self.assertTrue(plugin._master_alert_terminated)
        self.assertFalse(plugin._master_alert_ready)
        self.assertEqual(revision, plugin._master_alert_revision)
        self.assertIsNone(plugin._master_alert_timer)
        self.assertEqual([], plugin.context.send_trace)
        kv.assert_unused(self)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
