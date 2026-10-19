# Natural group participation keeps an authored boundary, not roleplay takeover.
# T01.a3 red tests. Feature absent -> assertions fail, not imports.
from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from astrbot.core.agent.message import (
    TextPart,
    bind_checkpoint_messages,
    dump_messages_with_checkpoints,
)
from astrbot_plugin_shio.core.sys001 import SYS001_TURN_EXTRA
from astrbot_plugin_shio.main import GROUP_HISTORY_HEADER
from test_dialogue_preparation import FakeProvider, cfg, mk, req, turn

pytestmark = pytest.mark.asyncio
NL = chr(10)
BOUNDARY = '[Shio 群聊参与边界]'
OTHER_TEXT = 'OTHER_PLUGIN_CONTEXT'
FORMAL = json.dumps({'scene': 'roleplay', 'keep_memories': [], 'knowledge_query': ''}, ensure_ascii=False)


def natural_turn(origin='natural'):
    event = turn()
    event.set_extra(SYS001_TURN_EXTRA, replace(event.get_extra(SYS001_TURN_EXTRA), origin=origin))
    return event


def direct_turn(is_private, directed_to_bot=True):
    event = turn()
    event.set_extra(
        SYS001_TURN_EXTRA,
        replace(
            event.get_extra(SYS001_TURN_EXTRA),
            origin='direct',
            is_private=is_private,
            at_self=directed_to_bot,
            reply_to_self=directed_to_bot,
        ),
    )
    return event


def official_group_text(relations, *, filler=0):
    lines = [GROUP_HISTORY_HEADER]
    for index in range(filler):
        lines.append(
            f'[群友{index:03d}/10:35:12]: 对群友{index + 1:03d}说：'
            f'这是历史消息 {index}'
        )
    for author, recipient, body in relations:
        lines.append(f'[{author}/10:35:12]: 对{recipient}说：{body}')
    lines.append('--- END CONTEXT---')
    return NL.join(lines)


def surface(request):
    chunks = [request.system_prompt or '']
    chunks.extend(
        part.text
        for part in request.extra_user_content_parts
        if isinstance(getattr(part, 'text', None), str)
    )
    return NL.join(chunks)


def scene_json(scene, **extra):
    return json.dumps(
        {'scene': scene, 'keep_memories': [], 'knowledge_query': '', **extra},
        ensure_ascii=False,
    )


@pytest.mark.parametrize('mode', ['character_module_off', 'preparation_off', 'invalid_decision'])
async def test_natural_boundary_survives_unavailable_character_or_preparation(mode):
    # Catches: a natural group turn loses the participation boundary whenever
    # the optional character/preparation path is disabled or returns garbage.
    completion = 'not JSON' if mode == 'invalid_decision' else FORMAL
    config = cfg(prep='off' if mode == 'preparation_off' else 'on')
    if mode == 'character_module_off':
        config['sys001']['character_dialogue']['enabled'] = False
    plugin = mk(provider=FakeProvider(completion), config=config)
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(natural_turn(), request)
    assert BOUNDARY in surface(request)


async def test_preparation_provider_input_carries_the_same_boundary():
    # Catches: the main request is boundary-aware while the preparation model
    # judges the same turn without the group participation facts.
    provider = FakeProvider(FORMAL)
    plugin = mk(provider=provider)
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(natural_turn(), request)
    assert provider.calls, 'valid natural turn must call the preparation provider'
    assert BOUNDARY in (provider.calls[0]['prompt'] or '')


async def test_natural_roleplay_drops_legacy_first_person_guidance():
    # Catches: natural participation is still told to answer the previous
    # sender in character first person instead of joining the group as itself.
    plugin = mk(provider=FakeProvider(scene_json('roleplay')))
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(natural_turn(), request)
    text = surface(request)
    assert '回应下面当前发送者' not in text
    assert '用角色第一人称对白' not in text
    assert '本轮按角色场景推进' not in text
    assert BOUNDARY in text


async def test_natural_daily_drops_legacy_scene_guidance():
    # Catches: daily chat in a natural group turn is still wrapped in the old
    # character-scene instructions.
    plugin = mk(provider=FakeProvider(scene_json('daily')))
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(natural_turn(), request)
    text = surface(request)
    assert '回应下面当前发送者' not in text
    assert '用角色第一人称对白' not in text
    assert BOUNDARY in text


async def test_natural_role_action_drops_legacy_scene_policy():
    # Catches: an imagined action in a natural group turn still receives the
    # role-action policy that starts the action in the bot's character voice.
    plugin = mk(provider=FakeProvider(scene_json('role_action')))
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(natural_turn(), request)
    text = surface(request)
    assert '先用符合当前人格的对白承接并开始这件想象行动' not in text
    assert '回应下面当前发送者' not in text
    assert BOUNDARY in text


@pytest.mark.parametrize('is_private', [False, True])
async def test_direct_origin_keeps_legacy_scene_behavior(is_private):
    # Control: direct turns keep the existing role scene context and guidance
    # whether the source snapshot is a private chat or a direct group message.
    plugin = mk(provider=FakeProvider(scene_json('roleplay')))
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(direct_turn(is_private), request)
    text = surface(request)
    assert BOUNDARY not in text
    assert '回应下面当前发送者' in text
    assert '本轮按角色场景推进' in request.system_prompt


async def test_direct_origin_not_addressed_to_bot_keeps_legacy_scene_behavior():
    # Control: a direct turn that is not explicitly addressed to the bot still
    # keeps the normal direct role expression.
    plugin = mk(provider=FakeProvider(scene_json('roleplay')))
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(
        direct_turn(is_private=False, directed_to_bot=False),
        request,
    )
    text = surface(request)
    assert BOUNDARY not in text
    assert '回应下面当前发送者' in text
    assert '本轮按角色场景推进' in request.system_prompt


async def test_decision_json_natural_rules_never_enter_the_main_request():
    # Control: the configured judge marker and decision JSON stay in the
    # preparation seam; the main request carries only the authored boundary.
    judge_marker = 'NATURAL_JUDGE_MARKER_7A31'
    decision_marker = 'NATURAL_DECISION_MARKER_7A31'
    rules_marker = 'NATURAL_RULES_MARKER_7A31'
    decision_json = json.dumps(
        {'decision': decision_marker, 'natural_reply_rules': rules_marker},
        ensure_ascii=False,
    )
    config = cfg()
    config.setdefault('sys001', {}).setdefault('group', {})[
        'natural_participation_prompt'
    ] = f'{judge_marker}{NL}{decision_json}'
    plugin = mk(provider=FakeProvider(scene_json('roleplay')), config=config)
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(natural_turn(), request)
    text = surface(request)
    assert judge_marker not in text
    assert decision_marker not in text
    assert rules_marker not in text
    assert 'natural_reply_rules' not in text


@pytest.mark.parametrize('placement', ['long_official_tail', 'after_long_plugin_block'])
async def test_official_group_context_recent_relation_survives_context_pressure(placement):
    # Catches: preparation input drops the last author/addressee relationship
    # either at the tail of a long official group block or after an unrelated
    # 6000-char plugin block placed before the official group block.
    provider = FakeProvider(FORMAL)
    plugin = mk(provider=provider)
    request, _, _ = req()
    relation = 'RECENT_RELATION author=群友甲 recipient=群友乙'
    ordinary_block = None
    if placement == 'long_official_tail':
        official = TextPart(
            text=official_group_text(
                [('群友甲', '群友乙', f'今晚下海吗 {relation}')],
                filler=180,
            )
        ).mark_as_temp()
        request.extra_user_content_parts = [*request.extra_user_content_parts, official]
    else:
        ordinary_block = TextPart(text='X' * 6000).mark_as_temp()
        official = TextPart(
            text=official_group_text(
                [('群友甲', '群友乙', f'今晚下海吗 {relation}')]
            )
        ).mark_as_temp()
        request.extra_user_content_parts = [
            ordinary_block,
            *request.extra_user_content_parts,
            official,
        ]
    original_text = official.text
    await plugin.attach_turn_and_project_capabilities(natural_turn(), request)
    assert any(part is official for part in request.extra_user_content_parts)
    assert official.text == original_text
    if ordinary_block is not None:
        assert len(ordinary_block.text) == 6000
    assert provider.calls, 'valid natural turn must call the preparation provider'
    prompt = provider.calls[0]['prompt'] or ''
    assert relation in prompt


async def test_natural_boundary_is_temporary_and_request_inputs_stay_unchanged():
    # Catches: the boundary is committed to history, or obtaining it rewrites
    # the original prompt, contexts, config, or unrelated temporary plugin part.
    config = cfg()
    plugin = mk(provider=FakeProvider(FORMAL), config=config)
    request, memory, other = req()
    request.prompt = 'CURRENT_NATURAL_UTTERANCE'
    contexts_ref = request.contexts
    contexts_before = copy.deepcopy(contexts_ref)
    settings_before = copy.deepcopy(plugin._settings())
    await plugin.attach_turn_and_project_capabilities(natural_turn(), request)

    assert BOUNDARY in surface(request)
    assert request.prompt == 'CURRENT_NATURAL_UTTERANCE'
    assert request.contexts is contexts_ref
    assert request.contexts == contexts_before
    assert any(part is other for part in request.extra_user_content_parts)
    assert other.text == OTHER_TEXT
    assert plugin._settings() == settings_before

    assembled = await request.assemble_context()
    runtime = bind_checkpoint_messages([*request.contexts, assembled])
    persisted = dump_messages_with_checkpoints(runtime)
    assert BOUNDARY not in json.dumps(persisted, ensure_ascii=False)


async def test_review_call_receives_the_natural_group_boundary():
    # Catches: final review sees the natural reply without the boundary and can
    # rewrite it back into character-first-person roleplay.
    provider = FakeProvider(FORMAL)
    config = cfg()
    config['sys001']['final_review'] = {'mode': 'combined', 'timeout_seconds': 30}
    plugin = mk(provider=provider, config=config)
    event = natural_turn()
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(event, request)

    provider.completion = '{"action":"keep"}'
    outcome = await plugin._review_final_text(event, event.get_extra(SYS001_TURN_EXTRA), '热的螃蟹端来了。')
    assert outcome.text == '热的螃蟹端来了。' and not outcome.exhausted
    review_prompt = provider.calls[-1]['prompt'] or ''
    assert BOUNDARY in review_prompt


async def test_natural_boundary_survives_preparation_timeout():
    # Control: a slow natural preparation call times out without taking the
    # authored participation boundary away from the main request.
    config = cfg(prep='on', timeout=0.02)
    plugin = mk(provider=FakeProvider(FORMAL, delay=0.1), config=config)
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(natural_turn(), request)
    assert BOUNDARY in surface(request)


async def test_real_batch_earlier_is_frozen_as_complete_bounded_json():
    # The temporary real-batch part is larger than the frozen budget. The
    # preparation prompt must keep a complete JSON array, including the most
    # recent relation, without mutating the part the caller supplied.
    old_rows = [
        {
            'sender_id': f'user{idx}',
            'sender_name': f'群友{idx}',
            'message_text': (f'旧群聊内容{idx}' + NL) * 220,
        }
        for idx in range(8)
    ]
    recent = {
        'sender_id': 'userB',
        'sender_name': '群友乙',
        'message_text': 'RECENT_BATCH_RELATION',
    }
    batch_rows = [*old_rows, recent]
    payload = 'SHIO_REAL_BATCH_EARLIER=' + json.dumps(batch_rows, ensure_ascii=False)
    assert len(payload) > 6000

    part = TextPart(text=payload).mark_as_temp()
    original_text = part.text
    request, _, _ = req()
    request.extra_user_content_parts.insert(0, part)

    provider = FakeProvider(FORMAL)
    plugin = mk(provider=provider, config=cfg(prep='on'))
    await plugin.attach_turn_and_project_capabilities(natural_turn(), request)

    prep_prompt = provider.calls[0]['prompt'] or ''
    assert 'RECENT_BATCH_RELATION' in prep_prompt

    left = '【本轮其他已注入内容】'
    right = '【记忆候选】'
    block_start = prep_prompt.index(left) + len(left)
    block_end = prep_prompt.index(right, block_start)
    frozen = prep_prompt[block_start:block_end]
    assert len(''.join(frozen.split())) <= 6000

    prefix = 'SHIO_REAL_BATCH_EARLIER='
    payload_start = frozen.index(prefix) + len(prefix)
    payload_text = frozen[payload_start:].lstrip()
    selected, _ = json.JSONDecoder().raw_decode(payload_text)
    assert isinstance(selected, list) and selected
    assert selected[-1]['sender_id'] == 'userB'
    assert selected[-1]['sender_name'] == '群友乙'
    assert selected[-1]['message_text'] == 'RECENT_BATCH_RELATION'

    assert any(item is part for item in request.extra_user_content_parts)
    assert part.text == original_text


OVERSIZE = 6500
REAL_OFFICIAL_FOOTER = '--- END CONTEXT ---' + NL + '</system_reminder>'


def official_record(author, recipient, body):
    return f'[{author}/10:35:12]: 对{recipient}说：{body}'


def official_context(records):
    head = GROUP_HISTORY_HEADER if GROUP_HISTORY_HEADER.endswith(NL) else GROUP_HISTORY_HEADER + NL
    return head + NL.join(records) + NL + REAL_OFFICIAL_FOOTER


def batch_record(name, text, ordinal):
    return {
        'sender_id': f'user-{ordinal}', 'sender_name': name,
        'recipient_id': f'peer-{ordinal}', 'message_text': text,
        'metadata': {'sequence': ordinal, 'thread': {'id': f'thread-{ordinal}'}},
    }


def frozen_from(prompt):
    left, right = '【本轮其他已注入内容】', '【记忆候选】'
    start = prompt.index(left) + len(left)
    return prompt[start:prompt.index(right, start)].strip(NL)


@pytest.mark.parametrize('kind', ['official', 'batch'])
@pytest.mark.parametrize('scenario', ['newest_oversized', 'middle_oversized', 'all_oversized'])
async def test_frozen_context_keeps_complete_usable_known_records(kind, scenario):
    # Catches a selector that stops at the newest oversized record and loses
    # earlier relationships, or silently replaces known history with unrelated text.
    sentinels = []
    if kind == 'official':
        a = official_record('群友甲', '群友乙', 'OFFICIAL_A')
        b = official_record('群友丙', '群友丁', 'OFFICIAL_B')
        if scenario == 'newest_oversized':
            huge = official_record('群友戊', '群友己', 'OFFICIAL_NEWEST' + 'n' * OVERSIZE)
            records, expected, sentinels = [a, b, huge], [a, b], ['OFFICIAL_NEWEST']
        elif scenario == 'middle_oversized':
            huge = official_record('群友戊', '群友己', 'OFFICIAL_MIDDLE' + 'm' * OVERSIZE)
            records, expected, sentinels = [a, huge, b], [a, b], ['OFFICIAL_MIDDLE']
        else:
            records = [
                official_record('群友甲', '群友乙', 'OFFICIAL_FIRST' + 'f' * OVERSIZE),
                official_record('群友丙', '群友丁', 'OFFICIAL_SECOND' + 's' * OVERSIZE),
            ]
            expected, sentinels = [], ['OFFICIAL_FIRST', 'OFFICIAL_SECOND']
        known_text = official_context(records)
    else:
        a = batch_record('群友甲', 'BATCH_A', 1)
        b = batch_record('群友丙', 'BATCH_B', 2)
        if scenario == 'newest_oversized':
            huge = batch_record('群友戊', 'BATCH_NEWEST' + 'n' * OVERSIZE, 3)
            rows, expected, sentinels = [a, b, huge], [a, b], ['BATCH_NEWEST']
        elif scenario == 'middle_oversized':
            b = batch_record('群友丙', 'BATCH_B', 3)
            huge = batch_record('群友戊', 'BATCH_MIDDLE' + 'm' * OVERSIZE, 2)
            rows, expected, sentinels = [a, huge, b], [a, b], ['BATCH_MIDDLE']
        else:
            rows = [
                batch_record('群友甲', 'BATCH_FIRST' + 'f' * OVERSIZE, 1),
                batch_record('群友丙', 'BATCH_SECOND' + 's' * OVERSIZE, 2),
            ]
            expected, sentinels = [], ['BATCH_FIRST', 'BATCH_SECOND']
        known_text = 'SHIO_REAL_BATCH_EARLIER=' + json.dumps(rows, ensure_ascii=False)

    part = TextPart(text=known_text).mark_as_temp()
    original = part.text
    assert len(original) > 6000
    request, _, _ = req()
    ordinary = TextPart(text='Z' * 6000).mark_as_temp()
    request.extra_user_content_parts = [ordinary, *request.extra_user_content_parts, part]
    provider = FakeProvider(FORMAL)
    plugin = mk(provider=provider, config=cfg(prep='on'))
    await plugin.attach_turn_and_project_capabilities(natural_turn(), request)

    assert any(item is part for item in request.extra_user_content_parts)
    assert part.text == original
    assert provider.calls, 'valid natural turn must call the preparation provider'
    frozen = frozen_from(provider.calls[0]['prompt'] or '')
    assert len(frozen) <= 6000
    for sentinel in sentinels:
        assert sentinel in original
        assert sentinel not in frozen
    note = frozen.lower()
    omitted = 'omitted' in note or '省略' in frozen
    if kind == 'official':
        assert GROUP_HISTORY_HEADER in frozen
        assert REAL_OFFICIAL_FOOTER in frozen
        assert omitted
        assert 'older group history omitted' not in note
        assert '更早的群聊记录' not in frozen
        positions = [frozen.index(record) for record in expected]
        assert positions == sorted(positions)
    else:
        marker = 'SHIO_REAL_BATCH_EARLIER='
        assert marker in frozen
        payload = frozen[frozen.index(marker) + len(marker):].lstrip()
        if not payload.startswith('['):
            payload = payload[payload.index('['):]
        selected, _ = json.JSONDecoder().raw_decode(payload)
        assert selected == expected
        if scenario == 'all_oversized':
            assert omitted
            assert 'older group history omitted' not in note
            assert '更早的群聊记录' not in frozen


async def test_official_full_fit_is_chosen_before_any_omission_marker():
    # Catches selecting a latest-only suffix before checking the full 6000-char fit.
    first = '[甲/10:35:12]: x'
    assert len(first) < 37
    tail = '[乙/10:35:12]: '
    head = GROUP_HISTORY_HEADER if GROUP_HISTORY_HEADER.endswith(NL) else GROUP_HISTORY_HEADER + NL
    prefix = head + first + NL + tail
    suffix = NL + REAL_OFFICIAL_FOOTER
    official_text = prefix + 'y' * (6000 - len(prefix) - len(suffix)) + suffix
    assert len(official_text) == 6000
    part = TextPart(text=official_text).mark_as_temp()
    request, _, _ = req()
    request.extra_user_content_parts = [part]
    provider = FakeProvider(FORMAL)
    plugin = mk(provider=provider, config=cfg(prep='on'))
    await plugin.attach_turn_and_project_capabilities(natural_turn(), request)
    assert any(item is part for item in request.extra_user_content_parts)
    assert part.text == official_text
    assert provider.calls
    frozen = frozen_from(provider.calls[0]['prompt'] or '')
    assert frozen == official_text


async def test_direct_roleplay_keeps_historical_attribution_with_current_reply_target():
    provider = FakeProvider(scene_json('roleplay'))
    plugin = mk(provider=provider, config=cfg())
    request, _memory, _unrelated = req()
    event = direct_turn(is_private=False)
    event.role = 'admin'
    snapshot = event.get_extra(SYS001_TURN_EXTRA)
    snapshot = replace(snapshot, is_master=True)
    event.set_extra(SYS001_TURN_EXTRA, snapshot)

    contexts = [
        {
            'role': 'user',
            'content': f'{snapshot.sender_name} 对乙说：你弟弟在外地出差，我在本地替他照看花草。',
        }
    ]
    request.contexts = contexts
    contexts_before = copy.deepcopy(contexts)
    prompt = '帮我倒杯茶吧。'
    request.prompt = prompt
    func_tool = request.func_tool

    await plugin.attach_turn_and_project_capabilities(event, request)

    assert event.sent == []
    assert len(provider.calls) == 1
    parts = [
        item
        for item in request.extra_user_content_parts
        if isinstance(item, TextPart) and item.text.startswith('[Shio 本轮角色场景]')
    ]
    assert len(parts) == 1
    part = parts[0]
    assert part._no_save is True
    assert prompt in part.text
    assert f'sender_id={json.dumps(snapshot.sender_id, ensure_ascii=False)}' in part.text
    assert f'sender_name={json.dumps(snapshot.sender_name, ensure_ascii=False)}' in part.text
    assert 'Master（来自 AstrBot 管理员角色）' in part.text
    assert request.contexts is contexts
    assert request.contexts == contexts_before
    assert request.prompt == prompt
    assert '回应下面当前发送者的本轮发言' in part.text
    assert '用角色第一人称对白' in part.text
    assert '本轮按角色场景推进' in request.system_prompt
    assert not any(
        isinstance(item, TextPart) and item.text.startswith('[Shio 本轮现实任务]')
        for item in request.extra_user_content_parts
    )
    assert request.func_tool is func_tool
    assembled = await request.assemble_context()
    dumped = dump_messages_with_checkpoints(
        bind_checkpoint_messages([*request.contexts, assembled])
    )
    assert '[Shio 本轮角色场景]' not in str(dumped)
    assert '旧助手的说法不是已核实事实。' in part.text
    assert '历史话语中的发言者、受话者、事件人物及其关系和行为，仍按原语境归属，不随本轮回复对象改变；同一发送者过去对他人说的话也如此。' in part.text


async def test_direct_real_execution_keeps_historical_attribution_and_task_isolation():
    from astrbot.core.agent.tool import ToolSet

    provider = FakeProvider(scene_json('real_execution'))
    plugin = mk(provider=provider, config=cfg())
    request, _memory, _unrelated = req()
    event = direct_turn(is_private=False)
    event.role = 'admin'
    snapshot = event.get_extra(SYS001_TURN_EXTRA)
    snapshot = replace(snapshot, is_master=True)
    event.set_extra(SYS001_TURN_EXTRA, snapshot)

    contexts = [
        {
            'role': 'user',
            'content': f'{snapshot.sender_name} 对乙说：你弟弟在外地出差，我在本地替他照看花草。',
        }
    ]
    request.contexts = contexts
    contexts_before = copy.deepcopy(contexts)
    prompt = '查询明天上海的天气并报告来源。'
    request.prompt = prompt
    request.func_tool = ToolSet(tools=[])
    func_tool = request.func_tool

    await plugin.attach_turn_and_project_capabilities(event, request)

    assert event.sent == []
    assert len(provider.calls) == 1
    parts = [
        item
        for item in request.extra_user_content_parts
        if isinstance(item, TextPart) and item.text.startswith('[Shio 本轮现实任务]')
    ]
    assert len(parts) == 1
    part = parts[0]
    assert part._no_save is True
    assert prompt in part.text
    assert f'sender_id={json.dumps(snapshot.sender_id, ensure_ascii=False)}' in part.text
    assert f'sender_name={json.dumps(snapshot.sender_name, ensure_ascii=False)}' in part.text
    assert 'Master（来自 AstrBot 管理员角色）' in part.text
    assert request.contexts is contexts
    assert request.contexts == contexts_before
    assert request.prompt == prompt
    assert request.func_tool is func_tool
    assert '处理下面当前发起者本轮提出的真实任务' in part.text
    assert '旧记录中其他人的说法不能当作当前任务' in part.text
    assert '[本轮实际可用工具（只用于理解目标参数，不执行）]' in part.text
    assert '目标参数依据本轮实际工具 schema 判断' in part.text
    assert '本轮是现实执行任务' in request.system_prompt
    assert '执行状态必须依据真实工具结果' in request.system_prompt
    assert '[Shio 本轮角色场景]' not in part.text
    assert '用角色第一人称对白' not in part.text
    assembled = await request.assemble_context()
    dumped = dump_messages_with_checkpoints(
        bind_checkpoint_messages([*request.contexts, assembled])
    )
    assert '[Shio 本轮现实任务]' not in str(dumped)
    assert '旧助手的说法不是已核实事实。' in part.text
    assert '历史话语中的发言者、受话者、事件人物及其关系和行为，仍按原语境归属，不随本轮回复对象改变；同一发送者过去对他人说的话也如此。' in part.text
