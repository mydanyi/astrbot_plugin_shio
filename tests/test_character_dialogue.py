"""Independent character guidance through the existing request/review hooks."""
import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import test_name_semantic_provider_routing as fixtures

MAIN = fixtures.MAIN
MARKER = "[Shio 角色化自然表达]"
REVIEW_MARKER = "CHARACTER_EXPRESSION_REFERENCE:"


class CharacterDialogueTests(unittest.IsolatedAsyncioTestCase):
    async def prepare(self, settings=None, *, master=False, private=False, review="off"):
        provider = fixtures.FakeProvider('{"action":"keep"}')
        context = fixtures.FakeContext(current=provider)
        context.persona_manager = SimpleNamespace(resolve_selected_persona=AsyncMock(
            return_value=("character", {"prompt": "PERSONA_KEEP: 嘴硬心软，但认真解答技术问题。"}, None, False)))
        plugin = fixtures.NameSemanticProviderRoutingTests().plugin(context)
        plugin.config['sys001'].update({
            'identity': {'master_relationship_enabled': True, 'master_relationship_prompt': 'MASTER_ONLY_STYLE'},
            'final_review': {'mode': review},
            'presentation': {'text_component_mode': 'plugin', 'text_component_max_segments': 2},
        })
        if settings is not None:
            plugin.config['sys001']['character_dialogue'] = settings
        before = copy.deepcopy(plugin.config)
        event, _ = fixtures.FinalAgentObservationTests.event_with_live_turn()
        snapshot = replace(event.get_extra(MAIN.SYS001_TURN_EXTRA), is_master=master, is_private=private)
        event.set_extra(MAIN.SYS001_TURN_EXTRA, snapshot)
        event.unified_msg_origin = snapshot.scope
        event.get_platform_name = lambda: 'aiocqhttp'
        req = SimpleNamespace(system_prompt='OFFICIAL_PERSONA_UNCHANGED', prompt='我有点累。',
                              contexts=[{'role': 'user', 'content': 'CHAT_HISTORY_UNCHANGED'}],
                              func_tool=None, extra_user_content_parts=[],
                              conversation=SimpleNamespace(persona_id='character'))
        await plugin.attach_turn_and_project_capabilities(event, req)
        self.assertEqual(before, plugin.config)
        self.assertEqual('我有点累。', req.prompt)
        self.assertEqual([{'role': 'user', 'content': 'CHAT_HISTORY_UNCHANGED'}], req.contexts)
        self.assertTrue(req.system_prompt.startswith('OFFICIAL_PERSONA_UNCHANGED\n\n'))
        self.assertEqual([], provider.calls, 'Guidance must not add an LLM call')
        return plugin, event, req, provider

    async def test_missing_disabled_and_invalid_module_leave_legacy_prompt_identical(self):
        _, _, baseline, _ = await self.prepare()
        for settings in ({'enabled': False, 'situation_prompt': 'MUST_NOT_APPEAR'},
                         {}, None, 'invalid', {'enabled': 'false'}):
            with self.subTest(settings=settings):
                _, _, req, _ = await self.prepare(settings)
                self.assertEqual(baseline.system_prompt, req.system_prompt)

    async def test_enabled_works_for_group_private_master_member_without_replacing_persona(self):
        for master in (False, True):
            for private in (False, True):
                with self.subTest(master=master, private=private):
                    _, _, req, _ = await self.prepare({'enabled': True}, master=master, private=private)
                    self.assertIn(MARKER, req.system_prompt)
                    self.assertEqual(master, 'MASTER_ONLY_STYLE' in req.system_prompt)

    async def test_editable_prompts_reach_main_model_but_empty_fields_add_no_defaults(self):
        _, _, req, _ = await self.prepare({'enabled': True, 'situation_prompt': 'CUSTOM_SITUATION',
                                          'expression_prompt': 'CUSTOM_EXPRESSION'})
        self.assertIn('CUSTOM_SITUATION', req.system_prompt)
        self.assertIn('CUSTOM_EXPRESSION', req.system_prompt)
        _, _, empty, _ = await self.prepare({'enabled': True, 'situation_prompt': '', 'expression_prompt': ''})
        fields = self.schema_fields()
        self.assertNotIn(fields['situation_prompt']['default'], empty.system_prompt)
        self.assertNotIn(fields['expression_prompt']['default'], empty.system_prompt)

    @staticmethod
    def schema_fields():
        schema = json.loads((Path(__file__).resolve().parents[1] / '_conf_schema.json').read_text(encoding='utf-8'))
        return schema['sys001']['items']['character_dialogue']['items']

    async def test_webui_defaults_match_runtime_and_are_independently_disabled(self):
        fields = self.schema_fields()
        self.assertIs(False, fields['enabled']['default'])
        self.assertIs(True, fields['preserve_character_in_review']['default'])
        _, _, req, _ = await self.prepare({'enabled': True})
        for key in ('situation_prompt', 'expression_prompt'):
            self.assertEqual('text', fields[key]['type'])
            self.assertEqual({'enabled': True}, fields[key]['condition'])
            self.assertIn(fields[key]['default'], req.system_prompt)

    async def test_review_off_remains_off_and_does_not_resolve_persona_again(self):
        plugin, event, _, provider = await self.prepare({'enabled': True})
        result = await plugin._review_final_text(event, event.get_extra(MAIN.SYS001_TURN_EXTRA), '哼，也不是不能帮你。')
        self.assertEqual('哼，也不是不能帮你。', result.text)
        self.assertEqual([], provider.calls)
        plugin.context.persona_manager.resolve_selected_persona.assert_not_awaited()

    async def test_review_gets_official_persona_and_master_reference_only_when_applicable(self):
        for master in (False, True):
            with self.subTest(master=master):
                plugin, event, _, provider = await self.prepare({'enabled': True}, master=master, review='core')
                await plugin._review_final_text(event, event.get_extra(MAIN.SYS001_TURN_EXTRA), '哼，也不是不能帮你。')
                prompt = provider.calls[0]['prompt']
                self.assertIn(REVIEW_MARKER, prompt)
                self.assertIn('PERSONA_KEEP', prompt)
                self.assertEqual(master, 'MASTER_ONLY_STYLE' in prompt)
                self.assertNotIn('CHAT_HISTORY_UNCHANGED', prompt)
                self.assertIsNone(provider.calls[0]['func_tool'])

    async def test_review_style_switch_does_not_disable_original_review(self):
        plugin, event, req, provider = await self.prepare({'enabled': True, 'preserve_character_in_review': False}, review='core')
        await plugin._review_final_text(event, event.get_extra(MAIN.SYS001_TURN_EXTRA), '候选')
        self.assertIn(MARKER, req.system_prompt)
        self.assertEqual(1, len(provider.calls))
        self.assertNotIn(REVIEW_MARKER, provider.calls[0]['prompt'])

    async def test_separate_event_does_not_inherit_previous_persona_reference(self):
        plugin, _, _, provider = await self.prepare({'enabled': True}, review='core')
        event, _ = fixtures.FinalAgentObservationTests.event_with_live_turn()
        event.unified_msg_origin = event.get_extra(MAIN.SYS001_TURN_EXTRA).scope
        await plugin._review_final_text(event, event.get_extra(MAIN.SYS001_TURN_EXTRA), '新事件')
        self.assertNotIn('PERSONA_KEEP', provider.calls[0]['prompt'])
