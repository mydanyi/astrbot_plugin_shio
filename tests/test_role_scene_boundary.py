"""Role-world context belongs to this request, never stored chat/persona state."""
import copy
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from astrbot.core.agent.message import bind_checkpoint_messages, dump_messages_with_checkpoints
from test_dialogue_preparation import FakeProvider, cfg, mk, req, turn
from astrbot_plugin_shio.core.sys001 import SYS001_TURN_EXTRA

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('scene', ['role_action', 'roleplay', 'daily', 'factual', 'personal_recall', 'real_execution'])
async def test_only_valid_role_scene_gets_provider_facing_boundary(scene):
    provider = FakeProvider(json.dumps({'scene':scene, 'keep_memories':[], 'knowledge_query':''}))
    plugin = mk(provider=provider)
    request, memory, other = req()
    original_history = request.contexts
    history_before = copy.deepcopy(original_history)
    await plugin.attach_turn_and_project_capabilities(turn(), request)
    new_parts = [p for p in request.extra_user_content_parts if p is not memory and p is not other]
    assert len(new_parts) == (1 if scene in ('role_action','roleplay','daily') else 0)
    assert request.contexts is original_history and request.contexts == history_before
    if scene in ('role_action','roleplay','daily'):
        assert new_parts[0]._no_save is True
        assembled = await request.assemble_context()
        assert any(p.get('text') == new_parts[0].text for p in assembled['content'])
        runtime = bind_checkpoint_messages([*request.contexts, assembled])
        persisted = dump_messages_with_checkpoints(runtime)
        assert persisted[-1]['content'] == [{'type':'text', 'text':'去下海抓个螃蟹'}]
        assert persisted[0]['content'] == 'RECENT_REAL_HISTORY'


@pytest.mark.parametrize('scene', ['roleplay', 'daily'])
async def test_other_character_chat_does_not_force_master_action_policy(scene):
    provider = FakeProvider(json.dumps({'scene':scene, 'keep_memories':[], 'knowledge_query':''}))
    plugin = mk(provider=provider)
    event = turn()
    event.set_extra(SYS001_TURN_EXTRA, replace(event.get_extra(SYS001_TURN_EXTRA),is_master=True))
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(event,request)
    assert '不额外要求对方先满足陪同、奖励等前置条件' not in request.system_prompt


async def test_role_action_has_selected_action_policy_without_changed_real_tools():
    provider = FakeProvider(json.dumps({'scene':'role_action', 'keep_memories':[], 'knowledge_query':''}))
    plugin = mk(provider=provider)
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(turn(),request)
    assert '先用符合当前人格的对白承接并开始这件想象行动' in request.system_prompt
    assert request.func_tool is None
    assert '这是想象互动，不证明现实工具执行过' in request.system_prompt


async def test_current_reply_target_not_previous_users_is_in_temporary_scene():
    plugin = mk(provider=FakeProvider(json.dumps({'scene':'roleplay', 'keep_memories':[], 'knowledge_query':''})))
    request, memory, other = req()
    request.prompt = 'CURRENT_REAL_UTTERANCE，我没吃东西，快去吧。'
    request.contexts = [{'role':'user','content':'另一位群友说自己吃了十天的食物'}]
    await plugin.attach_turn_and_project_capabilities(turn(), request)
    scene = [p for p in request.extra_user_content_parts if p is not memory and p is not other]
    assert len(scene) == 1
    assert 'CURRENT_REAL_UTTERANCE，我没吃东西，快去吧。' in scene[0].text
    assert '另一位群友说自己吃了十天的食物' not in scene[0].text
    assert scene[0]._no_save is True


async def test_real_execution_is_prepared_without_role_world_or_changed_tools():
    provider = FakeProvider(json.dumps({'scene':'real_execution', 'keep_memories':[2], 'knowledge_query':''}))
    plugin = mk(provider=provider)
    request, memory, other = req()
    request.prompt = '现在登录我的真实服务器，清理临时文件后报告数量。'
    await plugin.attach_turn_and_project_capabilities(turn(), request)
    assert all(p is not memory for p in request.extra_user_content_parts)
    retained = '\n'.join(p.text for p in request.extra_user_content_parts)
    assert 'UNRELATED_BIOS_INJECTION' not in retained and 'MASTER_LIKES_CRAB' in retained
    assert not any(p.text.startswith('[Shio 本轮角色场景]') for p in request.extra_user_content_parts)
    assert '本轮是现实执行任务' in request.system_prompt
    assert request.func_tool is None


async def test_role_scene_uses_current_event_local_time_not_old_history():
    plugin = mk(provider=FakeProvider(json.dumps({'scene':'roleplay', 'keep_memories':[], 'knowledge_query':''})))
    event = turn()
    event.set_extra(SYS001_TURN_EXTRA, replace(event.get_extra(SYS001_TURN_EXTRA),
                    created_at=datetime(2026,10,1,5,18,tzinfo=timezone.utc)))
    request, memory, other = req()
    request.contexts = [{'role':'assistant','content':'旧回复说现在已经过了十天'}]
    await plugin.attach_turn_and_project_capabilities(event, request)
    scene_parts = [p for p in request.extra_user_content_parts if p is not memory and p is not other]
    assert len(scene_parts) == 1
    assert '2026-10-01 13:18:00' in scene_parts[0].text
    assert scene_parts[0]._no_save is True
    assert request.contexts == [{'role':'assistant','content':'旧回复说现在已经过了十天'}]


@pytest.mark.parametrize('failure', ['disabled', 'invalid', 'unfinished', 'timeout', 'stale'])
async def test_failed_or_disabled_preparation_cannot_publish_role_boundary(failure):
    provider = FakeProvider(
        completion='not JSON' if failure == 'invalid' else json.dumps(
            {'scene':'roleplay', 'keep_memories':[], 'knowledge_query':''}),
        final=failure != 'unfinished', delay=0.1 if failure == 'timeout' else 0,
    )
    plugin = mk(provider=provider, config=cfg(
        prep='off' if failure == 'disabled' else 'on',
        streaming=failure == 'unfinished', timeout=0.02 if failure == 'timeout' else 30,
    ))
    if failure == 'stale':
        provider.plug = plugin
    request, memory, other = req()
    original_parts = list(request.extra_user_content_parts)
    await plugin.attach_turn_and_project_capabilities(turn(), request)
    assert len(request.extra_user_content_parts) == len(original_parts)
    assert all(a is b for a,b in zip(request.extra_user_content_parts, original_parts))
    assert request.contexts == [{'role':'user','content':'RECENT_REAL_HISTORY'}]
