"""Final review receives the selected scene without treating fiction as a receipt."""
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from astrbot_plugin_shio.core.character_dialogue import REVIEW_REFERENCE_EXTRA
from astrbot_plugin_shio.core.sys001 import SYS001_TURN_EXTRA
from test_dialogue_preparation import FakeProvider, PERSONA, cfg, mk, req, turn

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('scene,expected', [
    ('role_action', '先用符合当前人格的对白承接并开始这件想象行动'),
    ('roleplay', '本轮按角色场景推进'),
    ('daily', '本轮是日常闲聊'),
    ('real_execution', '本轮是现实执行任务'),
])
async def test_actual_review_call_has_this_turn_prepared_scene_and_original_persona(scene, expected):
    provider = FakeProvider(json.dumps({'scene':scene,'keep_memories':[],'knowledge_query':''}))
    config = cfg()
    config['sys001']['final_review'] = {'mode':'combined','timeout_seconds':30}
    plugin = mk(provider=provider,config=config)
    event = turn()
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(event,request)
    provider.completion = '{"action":"keep"}'
    outcome = await plugin._review_final_text(event,event.get_extra(SYS001_TURN_EXTRA),'热的螃蟹端来了。')
    assert outcome.text == '热的螃蟹端来了。' and not outcome.exhausted
    review_prompt = provider.calls[-1]['prompt']
    assert PERSONA in review_prompt
    assert '[Shio 本轮准备场景说明]' in review_prompt
    assert expected in review_prompt


async def test_review_evidence_contains_local_event_time_alongside_utc():
    provider = FakeProvider('{"action":"keep"}')
    config = cfg(prep='off')
    config['sys001']['final_review'] = {'mode':'core','timeout_seconds':30}
    plugin = mk(provider=provider,config=config)
    event = turn()
    snapshot = replace(event.get_extra(SYS001_TURN_EXTRA),
        created_at=datetime(2026,10,1,5,18,tzinfo=timezone.utc))
    event.set_extra(SYS001_TURN_EXTRA,snapshot)
    await plugin._review_final_text(event,snapshot,'本月到现在十三小时。')
    prompt = provider.calls[-1]['prompt']
    evidence = json.loads(prompt.split('CURRENT_EVENT_CONTEXT:\n',1)[1].split('\n\nTEXT:',1)[0])
    assert evidence['current_message']['created_at'] == '2026-10-01T05:18:00+00:00'
    assert evidence['current_message']['created_at_local'] == '2026-10-01T13:18:00+08:00'


@pytest.mark.parametrize('enabled,completion', [(False,'not JSON'),(True,'not JSON')])
async def test_unavailable_preparation_does_not_fake_scene_in_review(enabled,completion):
    provider = FakeProvider(completion)
    config = cfg(prep='on' if enabled else 'off')
    config['sys001']['final_review'] = {'mode':'core'}
    plugin = mk(provider=provider,config=config)
    event = turn()
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(event,request)
    reference = event.get_extra(REVIEW_REFERENCE_EXTRA)
    assert PERSONA in reference
    assert '[Shio 本轮准备场景说明]' not in reference
