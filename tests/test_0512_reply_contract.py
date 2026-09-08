"""Owner-facing regressions: addressed turns and usable failure notifications."""
import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace

from test_name_semantic_provider_routing import MAIN, SYS001
from test_r36_ambient_batching import _Inbound, _plugin, _close_window, SCOPE
import test_r13_master_termination_regressions as alert_fixtures
import test_name_semantic_provider_routing as fixtures


class PersonEvent(_Inbound):
    def __init__(self, message_id, text, sender, **kwargs):
        super().__init__(message_id, text, **kwargs)
        self.sender = sender

    def get_sender_id(self):
        return self.sender


async def settle():
    for _ in range(30):
        await asyncio.sleep(0)


class DirectedBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_natural_batch_keeps_multiple_speakers(self):
        plugin = _plugin(natural=True)
        plugin.context.current = fixtures.FakeProvider('{"decision":"REPLY"}')
        events = [PersonEvent('n1', 'tea', 'alice', directed=False),
                  PersonEvent('n2', 'cake', 'bob', directed=False)]
        tasks = [asyncio.create_task(anext(plugin.request_event_bound_reply(e), None)) for e in events]
        try:
            await settle()
            await _close_window(plugin)
            first, second = await asyncio.wait_for(asyncio.gather(*tasks), 1)
            self.assertIsNone(first)
            self.assertEqual('cake', second.prompt)
            self.assertEqual(['alice', 'bob'], [item.snapshot.sender_id for item in events[1].get_extra('shio.sys001.batch')])
            self.assertIn('tea', plugin.context.current.calls[0]['prompt'])
            self.assertIn('cake', plugin.context.current.calls[0]['prompt'])
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_two_addressed_turns_stay_before_later_reply_boundary(self):
        plugin = _plugin()
        events = [PersonEvent('a', 'first', 'alice'), PersonEvent('b', 'second', 'bob')]
        tasks = [asyncio.create_task(anext(plugin.request_event_bound_reply(e), None)) for e in events]
        boundary = PersonEvent('c', 'quoted reply', 'carol')
        await settle()
        hold = asyncio.create_task(plugin._batch_hold_official_boundary(boundary, SYS001.create_snapshot(boundary, origin='direct')))
        try:
            await settle()
            self.assertTrue(tasks[0].done())
            await plugin.record_standard_send(events[0])
            await settle()
            self.assertFalse(hold.done(), 'A later Reply must not overtake the second @')
            self.assertTrue(tasks[1].done())
            self.assertIsNotNone(tasks[1].result())
            await plugin.record_standard_send(events[1])
            await asyncio.wait_for(hold, 1)
        finally:
            for task in (*tasks, hold):
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, hold, return_exceptions=True)

    async def test_two_senders_addressed_turns_both_get_requests(self):
        plugin = _plugin()
        a = PersonEvent('a', 'roast', 'alice')
        b = PersonEvent('b', 'hello', 'bob')
        tasks = [asyncio.create_task(anext(plugin.request_event_bound_reply(e), None)) for e in (a,b)]
        try:
            await settle()
            await _close_window(plugin)
            await settle()
            self.assertTrue(tasks[0].done(), 'First addressee must retain a request')
            self.assertIsNotNone(tasks[0].result(), 'Another sender must not retire the first @')
            self.assertFalse(tasks[1].done(), 'Second sender waits for the first send terminal')
            await plugin.record_standard_send(a)
            await _close_window(plugin)
            reply = await asyncio.wait_for(tasks[1], 1)
            self.assertEqual('hello', reply.prompt)
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_other_sender_chatter_does_not_extend_or_own_direct_turn(self):
        plugin = _plugin(natural=True)
        a = PersonEvent('a', 'question', 'alice')
        b = PersonEvent('b', 'ambient', 'bob', directed=False)
        first = asyncio.create_task(anext(plugin.request_event_bound_reply(a), None))
        await settle()
        deadline = plugin._batch_scopes[SCOPE].waiting_deadline
        second = asyncio.create_task(anext(plugin.request_event_bound_reply(b), None))
        try:
            await settle()
            self.assertEqual(deadline, plugin._batch_scopes[SCOPE].waiting_deadline)
            await _close_window(plugin)
            results = await asyncio.wait_for(asyncio.gather(first,second), 1)
            self.assertEqual('question', results[0].prompt)
            self.assertIsNone(results[1])
            self.assertEqual(['a'], [item.snapshot.message_id for item in a.get_extra('shio.sys001.batch')])
        finally:
            for task in (first,second):
                if not task.done(): task.cancel()
            await asyncio.gather(first,second,return_exceptions=True)


class AlertContractTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self):
        plugin = alert_fixtures.R13MasterTerminationTests().plugin()
        plugin.config['sys001']['master_alert'].update(consecutive_threshold=3, master_alert_quiet_enabled=False)
        plugin._master_alert_record = SYS001.MasterAlertRecord(master_umo='qq:person:202')
        plugin._master_alert_ready = True
        plugin._master_alert_binding = {'umo':'qq:person:202','sender_id':'202','platform_id':'qq','account_id':'999'}
        plugin.context.get_config = lambda **kwargs: {'admins_id':['202']}
        return plugin

    async def test_first_terminal_failure_notifies_despite_old_threshold_three(self):
        plugin = self.plugin()
        event = alert_fixtures.RequestEvent()
        snapshot = SYS001.create_snapshot(event, origin='direct')
        await plugin._record_master_alert_terminal(event,snapshot,success=False,terminal_reason='review_exhausted')
        self.assertEqual(1,len(plugin.context.send_trace))

    async def test_natural_generation_failure_notifies(self):
        plugin = self.plugin()
        event = alert_fixtures.RequestEvent()
        snapshot = SYS001.create_snapshot(event, origin='natural')
        await plugin._record_master_alert_terminal(event,snapshot,success=False,terminal_reason='final_error')
        self.assertEqual(1,len(plugin.context.send_trace))

    async def test_revoked_master_is_not_sent_an_alert(self):
        plugin = self.plugin()
        plugin.config['sys001']['master_alert']['consecutive_threshold']=1
        plugin.context.get_config = lambda **kwargs: {'admins_id':[]}
        event=alert_fixtures.RequestEvent()
        await plugin._record_master_alert_terminal(event,SYS001.create_snapshot(event,origin='direct'),success=False,terminal_reason='final_error')
        self.assertEqual([],plugin.context.send_trace)

    async def test_notification_transport_timeout_does_not_hold_reply_terminal(self):
        plugin = self.plugin()
        plugin._MASTER_ALERT_SEND_SECONDS = 0.02
        async def stuck(*args):
            await asyncio.Event().wait()
        plugin.context.send_message = stuck
        event = alert_fixtures.RequestEvent()
        await asyncio.wait_for(plugin._record_master_alert_terminal(event,
            SYS001.create_snapshot(event, origin='direct'), success=False, terminal_reason='final_error'), 0.2)
        self.assertEqual('failed', plugin._master_alert_record.report_status)

    async def test_verified_destination_survives_reload_without_failure_history(self):
        plugin = self.plugin()
        storage={}
        async def get(key, default=None):return storage.get(key,default)
        async def put(key,value):storage[key]=value
        plugin.get_kv_data=get
        plugin.put_kv_data=put
        event=alert_fixtures.RequestEvent()
        await plugin.capture_master_alert_binding(event,SYS001.create_snapshot(event,origin='private'))
        second=self.plugin()
        second.get_kv_data=get
        second.put_kv_data=put
        await second.initialize()
        self.assertEqual(event.unified_msg_origin,second._master_alert_record.master_umo)
        self.assertEqual(0,second._master_alert_record.consecutive_count)
        self.assertEqual('none',second._master_alert_record.report_status)
        self.assertTrue(storage)
        self.assertNotIn('error_type',str(storage))


class AlertDedupeTests(unittest.TestCase):
    def test_success_does_not_reopen_duplicate_failure_notification(self):
        state=SYS001.master_alert_failure(SYS001.MasterAlertRecord(),error_type='review_exhausted',now=100,window_seconds=600,threshold=1)
        state=replace(state, report_status='submitted')
        state=SYS001.master_alert_success(state)
        state=SYS001.master_alert_failure(state,error_type='review_exhausted',now=110,window_seconds=600,threshold=1)
        self.assertEqual('submitted',state.report_status)


class ReviewReasonTests(unittest.IsolatedAsyncioTestCase):
    async def test_repair_round_failure_reason_does_not_reuse_previous_round_invalid(self):
        first = fixtures.FakeProvider('bad JSON', RuntimeError('unavailable'))
        fallback = fixtures.FakeProvider('{"action":"replace","text":"repaired"}', RuntimeError('unavailable'))
        plugin = fixtures.NameSemanticProviderRoutingTests().plugin(fixtures.FakeContext(current=first, by_id={'backup':fallback}))
        plugin.config['sys001']['final_review'] = {'mode':'core', 'fallback_provider_ids':['backup'],
            'timeout_seconds':1, 'max_repair_attempts':1, 'repair_enabled':True, 'use_repaired_text':True}
        event,_=fixtures.FinalAgentObservationTests.event_with_live_turn()
        event.unified_msg_origin = 'qq:group:202'
        result=await plugin._review_final_text(event,event.get_extra(MAIN.SYS001_TURN_EXTRA),'original')
        self.assertEqual('unavailable',result.reason)
        self.assertEqual(2, len(first.calls))
        self.assertEqual(2, len(fallback.calls))

    async def test_invalid_and_rejected_have_distinct_noncontent_reasons(self):
        for output, reason in [('not json','invalid'), ('{"action":"replace","text":"changed"}','rejected'),
                               (RuntimeError('private provider detail'), 'unavailable'),
                               (('sleep', 0.1, '{"action":"keep"}'), 'timeout')]:
            plugin=fixtures.NameSemanticProviderRoutingTests().plugin(fixtures.FakeContext(current=fixtures.FakeProvider(output)))
            plugin.config['sys001']['final_review']={'mode':'core','timeout_seconds':0.05,'max_repair_attempts':0}
            event,_=fixtures.FinalAgentObservationTests.event_with_live_turn()
            event.unified_msg_origin = 'qq:group:202'
            result=await plugin._review_final_text(event,event.get_extra(MAIN.SYS001_TURN_EXTRA),'original')
            self.assertEqual(reason,result.reason)
            self.assertTrue(result.exhausted)
