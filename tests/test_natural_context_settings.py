"""Natural decisions must see bounded official context and the selected role."""
import asyncio
import json
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_name_semantic_provider_routing import MAIN, SYS001, FakeProvider
from test_r36_ambient_batching import _Inbound, _plugin, _close_window, SCOPE


class NaturalContextTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self):
        plugin = _plugin(natural=True)
        plugin.config['sys001']['group']['continuous_window_enabled'] = False
        plugin.context.current = FakeProvider('{"decision":"REPLY"}')
        self.records = SimpleNamespace(
            raw_records={SCOPE: deque(['群友：刚买了猫玩具', '群友：她很喜欢'])},
            _record_ids={SCOPE: deque(['earlier', 'current'])},
        )
        plugin.context.get_registered_star = lambda name: SimpleNamespace(
            star_cls=SimpleNamespace(group_chat_context=self.records))
        plugin.context.get_config = lambda **kw: {
            'provider_ltm_settings': {'group_icl_enable': True},
            'provider_settings': {'default_personality': 'official'},
        }
        self.persona_calls = []
        async def resolve(**kw):
            self.persona_calls.append(kw)
            return ('chosen', {'prompt': '喜欢猫但是嘴硬。' * 1200}, [], [])
        plugin.context.persona_manager = SimpleNamespace(resolve_selected_persona=resolve)
        self.conversation = SimpleNamespace(persona_id='group-persona', updated_at=1704067190,
            history=json.dumps([{'role': 'user', 'content': '昨天聊猫'},
                                {'role': 'assistant', 'content': '我也想养一只'}]))
        plugin.context.conversation_manager.get_conversation = lambda *a: asyncio.sleep(0, result=self.conversation)
        return plugin

    def event(self, **kwargs):
        event = _Inbound('n1', '她很喜欢', directed=False, **kwargs)
        event.get_platform_name = lambda: 'aiocqhttp'
        event.set_extra('_group_context_record_id', 'current')
        return event

    async def test_official_history_and_full_persona_reach_decision_without_consumption(self):
        plugin = self.plugin()
        event = self.event()
        result = await anext(plugin.request_event_bound_reply(event), None)
        self.assertIsNotNone(result)
        prompt = plugin.context.current.calls[0]['prompt']
        self.assertIn('刚买了猫玩具', prompt)
        self.assertIn('我也想养一只', prompt)
        self.assertIn('assistant', prompt)
        self.assertIn('喜欢猫但是嘴硬。' * 1200, prompt)
        self.assertEqual('group-persona', self.persona_calls[0]['conversation_persona_id'])
        self.assertEqual(['earlier', 'current'], list(self.records._record_ids[SCOPE]))
        self.assertEqual(2, len(self.records.raw_records[SCOPE]))

    async def test_probability_zero_blocks_only_natural_and_does_not_commit_cadence(self):
        plugin = self.plugin()
        plugin.config['sys001']['group']['natural_participation_probability'] = 0
        before = plugin._natural_cadence[SCOPE]
        self.assertIsNone(await anext(plugin.request_event_bound_reply(self.event()), None))
        self.assertEqual(before, plugin._natural_cadence[SCOPE])
        direct = _Inbound('direct', '@机器人你好', directed=True)
        self.assertIsNotNone(await anext(plugin.request_event_bound_reply(direct), None))

    async def test_mid_probability_draws_once_for_merged_batch(self):
        plugin = self.plugin()
        group = plugin.config['sys001']['group']
        group.update(continuous_window_enabled=True, natural_participation_probability=50)
        events = [self.event(), _Inbound('n2', '还买了猫粮', directed=False)]
        tasks = []
        with patch.object(MAIN.random, 'random', return_value=0.75) as draw:
            try:
                tasks = [asyncio.create_task(anext(plugin.request_event_bound_reply(e), None)) for e in events]
                for _ in range(50):
                    await asyncio.sleep(0)
                await _close_window(plugin)
                self.assertEqual([None, None], await asyncio.wait_for(asyncio.gather(*tasks), 2))
                self.assertEqual(1, draw.call_count)
                self.assertEqual(1, len(plugin.context.current.calls))
            finally:
                for task in tasks:
                    if not task.done(): task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_future_buffer_and_conversation_update_do_not_leak(self):
        plugin = self.plugin()
        async def name_gate(event):
            self.records.raw_records[SCOPE].append('FUTURE_MESSAGE')
            self.records._record_ids[SCOPE].append('future')
            self.conversation.updated_at = 1704067201
            self.conversation.history = json.dumps([{'role':'assistant','content':'FUTURE_REPLY'}])
            return False
        plugin._visible_name_wake = name_gate
        await anext(plugin.request_event_bound_reply(self.event()), None)
        prompt = plugin.context.current.calls[0]['prompt']
        self.assertIn('刚买了猫玩具', prompt)
        self.assertNotIn('FUTURE_MESSAGE', prompt)
        self.assertNotIn('FUTURE_REPLY', prompt)

    async def test_missing_watermark_never_reads_other_records(self):
        plugin = self.plugin()
        event = self.event()
        event.set_extra('_group_context_record_id', 'missing')
        await anext(plugin.request_event_bound_reply(event), None)
        self.assertNotIn('刚买了猫玩具', plugin.context.current.calls[0]['prompt'])

    async def test_probability_accepts_lower_draw_and_never_draws_for_direct(self):
        plugin = self.plugin()
        plugin.config['sys001']['group']['natural_participation_probability'] = 50
        with patch.object(MAIN.random, 'random', return_value=0.49) as draw:
            self.assertIsNotNone(await anext(plugin.request_event_bound_reply(self.event()), None))
            self.assertEqual(1, draw.call_count)
        plugin = self.plugin()
        plugin.config['sys001']['group']['natural_participation_probability'] = 0
        with patch.object(MAIN.random, 'random') as draw:
            self.assertIsNotNone(await anext(plugin.request_event_bound_reply(_Inbound('d', '你好')), None))
            draw.assert_not_called()
        self.assertEqual([], plugin.context.current.calls)

    async def test_no_action_does_not_draw(self):
        plugin = self.plugin()
        plugin.context.current = FakeProvider('{"decision":"NO_ACTION"}')
        plugin.config['sys001']['group']['natural_participation_probability'] = 50
        with patch.object(MAIN.random, 'random') as draw:
            self.assertIsNone(await anext(plugin.request_event_bound_reply(self.event()), None))
            draw.assert_not_called()

    async def test_history_limits_keep_recent_content_without_truncating_persona(self):
        plugin = self.plugin()
        plugin.config['sys001']['group'].update(natural_context_max_messages=1, natural_context_max_chars=1000)
        self.records.raw_records[SCOPE][0] = 'recent:' + '猫' * 2000
        await anext(plugin.request_event_bound_reply(self.event()), None)
        prompt = plugin.context.current.calls[0]['prompt']
        payload = json.loads(prompt.split('RECENT_CONTEXT=', 1)[1].split('\nRULES:', 1)[0])
        self.assertEqual(1, len(payload['messages']))
        self.assertEqual(1000, len(payload['messages'][0]['content']))
        self.assertTrue(payload['truncated'])
        self.assertNotIn('昨天聊猫', prompt)
        self.assertIn('喜欢猫但是嘴硬。' * 1200, prompt)

    async def test_snapshot_survives_official_consumption_during_wait(self):
        plugin = self.plugin()
        async def name_gate(event):
            self.records.raw_records[SCOPE].clear()
            self.records._record_ids[SCOPE].clear()
            return False
        plugin._visible_name_wake = name_gate
        await anext(plugin.request_event_bound_reply(self.event()), None)
        self.assertIn('刚买了猫玩具', plugin.context.current.calls[0]['prompt'])
        self.assertFalse(self.records.raw_records[SCOPE])

    async def test_same_second_conversation_is_not_proven_past(self):
        plugin = self.plugin()
        self.conversation.updated_at = self.event().created_at
        await anext(plugin.request_event_bound_reply(self.event()), None)
        prompt = plugin.context.current.calls[0]['prompt']
        self.assertNotIn('我也想养一只', prompt)
        self.assertIn('newer_than_event', prompt)

    async def test_context_disabled_and_other_group_never_leak(self):
        plugin = self.plugin()
        self.records.raw_records['other:group'] = deque(['OTHER_GROUP_SECRET'])
        plugin.context.get_config = lambda **kw: {'provider_ltm_settings': {'group_icl_enable': False}}
        await anext(plugin.request_event_bound_reply(self.event()), None)
        prompt = plugin.context.current.calls[0]['prompt']
        self.assertNotIn('刚买了猫玩具', prompt)
        self.assertNotIn('OTHER_GROUP_SECRET', prompt)
        self.assertIn('"group_context_status":"disabled"', prompt)

    def test_web_exposes_probability_and_context_limits(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / '_conf_schema.json').read_text('utf-8'))
        group = schema['sys001']['items']['group']['items']
        self.assertEqual(100, group['natural_participation_probability']['default'])
        for key in ('natural_context_max_messages', 'natural_context_max_chars'):
            self.assertGreater(group[key]['default'], 0)
            self.assertEqual({'natural_participation_enabled': True}, group[key]['condition'])
        self.assertEqual(MAIN.DEFAULT_NATURAL_PROMPT, group['natural_participation_prompt']['default'])

    def test_current_batch_never_uses_history_count_or_character_allowance(self):
        for count, current_text in ((1, 'CURRENT_A'), (50, 'CURRENT_A' * 1000)):
            with self.subTest(count=count):
                plugin = self.plugin()
                settings = plugin.config['sys001']['group']
                settings.update(natural_context_max_messages=count, natural_context_max_chars=1000)
                self.records.raw_records[SCOPE] = deque(['HISTORY_BEFORE_BATCH', current_text, 'CURRENT_B'])
                self.records._record_ids[SCOPE] = deque(['history', 'a', 'b'])
                event = self.event()
                event.set_extra('_group_context_record_id', 'b')
                MAIN.capture_group_context(plugin.context, event)
                history = MAIN.decision_history(event, {}, settings, {'a', 'b'})
                self.assertEqual(['HISTORY_BEFORE_BATCH'], [row['content'] for row in history['messages']])
