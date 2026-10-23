"""Directed turns use a nonempty request and their own causal group context."""
from types import SimpleNamespace
import json

import pytest

from test_bug001_boundary_pipeline import (
    _ConversationManager, _event, _official_fixture, _run_official_pipeline,
)
from astrbot.api.message_components import At, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.builtin_stars.astrbot.group_chat_context import (
    GROUP_HISTORY_HEADER, GroupChatContext,
)
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.message_type import MessageType
from astrbot_plugin_shio.main import ShioPlugin
from astrbot_plugin_shio.core.sys001 import SYS001_TURN_EXTRA, create_snapshot

pytestmark = pytest.mark.asyncio


def make_plugin():
    config = {
        'provider_ltm_settings': {
            'group_icl_enable': True, 'group_message_max_cnt': 50,
            'image_caption': False,
            'active_reply': {'enable': False, 'method': 'possibility_reply',
                             'possibility_reply': 0},
        },
        'provider_settings': {'image_caption_prompt': ''},
    }
    context = SimpleNamespace(
        conversation_manager=_ConversationManager(),
        get_config=lambda **kwargs: config,
    )
    collector = GroupChatContext(None, context)
    context.get_registered_star = lambda name: SimpleNamespace(
        star_cls=SimpleNamespace(group_chat_context=collector))
    plugin = ShioPlugin(context, {'sys001': {
        'group': {'continuous_window_enabled': False,
                  'natural_participation_enabled': False},
        'final_review': {'mode': 'off'},
    }})
    return plugin, collector


def message(mid, sender, text, *, directed=False, scope='bug001-group'):
    parts = ([At(qq='bot')] if directed else []) + ([Plain(text)] if text else [])
    event = _event(mid, sender, text, parts, scope)
    event.is_at_or_wake_command = directed
    return event


async def request(plugin, event):
    requests = [item async for item in plugin.request_event_bound_reply(event)]
    assert len(requests) == 1
    return requests[0]


async def decorate(plugin, collector, event, req):
    await collector.on_req_llm(event, req)
    await plugin.attach_turn_and_project_capabilities(event, req)


def visible(req):
    return '\n'.join(part.text for part in req.extra_user_content_parts
                     if isinstance(part, TextPart))


@pytest.mark.parametrize('sender', ['master', 'member'])
@pytest.mark.parametrize('text', ['', '   '])
async def test_bare_at_enters_official_pipeline(sender, text):
    async with _official_fixture() as (plugin, pipeline, _metadata):
        plugin.config['sys001']['group']['continuous_window_enabled'] = False
        event = _event('bare', sender, text, [At(qq='bot')])
        await _run_official_pipeline(event, pipeline)
        req = event.get_extra('provider_request')
        assert req is not None, 'a real @ must yield a request even without text'
        assert req.prompt.strip(), 'official Agent must not receive an empty request'
        assert event.get_message_str().strip() == ''
        snap = event.get_extra(SYS001_TURN_EXTRA)
        assert snap.at_self and snap.origin == 'direct'
        assert snap.is_master == (sender == 'master')


async def test_empty_notices_and_mentions_of_others_do_not_request():
    plugin, _collector = make_plugin()
    notice = _event('notice', 'member', '', [])
    notice.message_obj.type = MessageType.FRIEND_MESSAGE
    other = _event('other', 'member', '', [At(qq='other-bot')])
    for event in (notice, other):
        assert [r async for r in plugin.request_event_bound_reply(event)] == []


async def test_direct_context_survives_consumption_before_request_decoration():
    plugin, collector = make_plugin()
    first = message('first', 'alice', '蓝色盒子叫海盐，接扩展坞时才会绿屏。')
    second = message('second', 'bob', '拔掉扩展坞显示就正常，不是无法开机。')
    current = message('current', 'alice', '那这个该换哪个？', directed=True)
    for event in (first, second, current):
        await collector.handle_message(event)
    req = await request(plugin, current)
    other = message('consumer', 'carol', '另一次请求')
    await collector.handle_message(other)
    await collector.on_req_llm(other, ProviderRequest(prompt='另一次请求'))
    await collector.handle_message(message('future', 'dave', 'FUTURE_ONLY'))
    await decorate(plugin, collector, current, req)
    text = visible(req)
    assert '蓝色盒子叫海盐' in text and '拔掉扩展坞显示就正常' in text
    assert 'alice' in text and 'bob' in text
    assert 'FUTURE_ONLY' not in text
    assert req.prompt == '那这个该换哪个？'
    assert all(part._no_save for part in req.extra_user_content_parts
               if isinstance(part, TextPart) and part.text.startswith(GROUP_HISTORY_HEADER))


async def test_unrecorded_at_only_uses_prior_context_not_later_messages():
    plugin, collector = make_plugin()
    await collector.handle_message(message('prior', 'alice', '我们刚才在讨论扩展坞绿屏。'))
    current = message('bare-context', 'alice', '', directed=True)
    assert current.get_extra('_group_context_record_id') is None
    req = await request(plugin, current)
    await collector.handle_message(message('later', 'bob', 'LATER_ONLY'))
    await decorate(plugin, collector, current, req)
    assert req.prompt.strip()
    assert '扩展坞绿屏' in visible(req)
    assert 'LATER_ONLY' not in visible(req)


async def test_partial_official_window_does_not_erase_batch_or_merge_equal_texts():
    plugin, collector = make_plugin()
    r1 = message('r1', 'alice', 'SAME_TEXT')
    r2 = message('r2', 'bob', '缺失的第二部分')
    r3 = message('r3', 'carol', 'SAME_TEXT')
    current = message('r4', 'alice', '最后一句', directed=True)
    await collector.handle_message(r1)
    await collector.handle_message(current)
    req = await request(plugin, current)
    current.set_extra('shio.sys001.batch', tuple(
        plugin._batch_message(e, create_snapshot(e, origin='direct'), e is current)
        for e in (r1, r2, r3, current)
    ))
    unrelated = TextPart(text='UNRELATED_PLUGIN').mark_as_temp()
    contexts = [{'role': 'user', 'content': 'EXISTING_CONVERSATION'}]
    req.extra_user_content_parts.append(unrelated)
    req.contexts = contexts
    other = message('other-group', 'dave', 'OTHER_GROUP', scope='other-group')
    await collector.handle_message(other)
    other_before = tuple(collector.raw_records[other.unified_msg_origin])
    await decorate(plugin, collector, current, req)
    batches = [json.loads(part.text.split('=', 1)[1])
               for part in req.extra_user_content_parts if isinstance(part, TextPart)
               and part.text.startswith('SHIO_REAL_BATCH_EARLIER=')]
    assert len(batches) == 1
    rows = [json.loads(row['content']) for row in batches[0]]
    assert [(r['platform_message_id'], r['sender_id'], r['message_text']) for r in rows] == [
        ('r1', 'alice', 'SAME_TEXT'), ('r2', 'bob', '缺失的第二部分'),
        ('r3', 'carol', 'SAME_TEXT'),
    ]
    assert not any('SAME_TEXT' in part.text for part in req.extra_user_content_parts
                   if isinstance(part, TextPart) and part.text.startswith(GROUP_HISTORY_HEADER))
    assert req.contexts is contexts and unrelated in req.extra_user_content_parts
    assert tuple(collector.raw_records[other.unified_msg_origin]) == other_before
    assert all(part._no_save for part in req.extra_user_content_parts if isinstance(part, TextPart))


async def test_expired_record_id_does_not_adopt_official_numeric_index_fallback():
    plugin, collector = make_plugin()
    old = message('old', 'alice', 'OLD')
    current = message('stale', 'bob', '接上文', directed=True)
    consumer = message('consume', 'carol', 'CONSUME')
    for event in (old, current, consumer):
        await collector.handle_message(event)
    await collector.on_req_llm(consumer, ProviderRequest(prompt='CONSUME'))
    for mid in ('FUTURE_ONE', 'FUTURE_TWO'):
        await collector.handle_message(message(mid, 'dave', mid))
    req = await request(plugin, current)
    await decorate(plugin, collector, current, req)
    assert 'FUTURE_ONE' not in visible(req) and 'FUTURE_TWO' not in visible(req)
