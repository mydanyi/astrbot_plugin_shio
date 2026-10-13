"""NAS-only sys001 dialogue-preparation contract tests. Feature absent -> assertions fail, not imports."""
from __future__ import annotations
import asyncio, json, logging
from types import SimpleNamespace
from dataclasses import replace
from datetime import datetime, timezone
import pytest, pytest_asyncio
from test_bug001_boundary_pipeline import _event, _official_fixture, _run_official_pipeline
from astrbot.api import logger as official_logger
from astrbot.core.agent.message import TextPart
from astrbot.core.message.components import Plain, At  # noqa: F401
from astrbot.core.provider.entities import ProviderRequest, LLMResponse
from astrbot.core.tools import knowledge_base_tools
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata, star_handlers_registry
from astrbot_plugin_shio.main import ShioPlugin
from astrbot_plugin_shio.core.sys001 import SYS001_TURN_EXTRA, create_snapshot

pytestmark = pytest.mark.asyncio
NL = chr(10)

RAG = NL.join([
    '<RAG-Faiss-Memory>', '标题', '',
    '记忆 #1 / Memory #1 (Importance: 0.65), Memory write time: 2026-10-01 11:00',
    'Topics: BIOS | Participants: other-user', 'UNRELATED_BIOS_INJECTION', '',
    '记忆 #2 / Memory #2 (Importance: 0.75), Memory write time: 2026-10-01 12:00',
    'Topics: food | Participants: master', 'MASTER_LIKES_CRAB', '',
    '结尾', '</RAG-Faiss-Memory>',
])
UNKNOWN = NL.join(['<RAG-Faiss-Memory>', '无标准记忆标题', 'NO_STANDARD_HEADING', '</RAG-Faiss-Memory>'])
FORMAL = json.dumps({'scene': 'roleplay', 'keep_memories': [], 'knowledge_query': ''}, ensure_ascii=False)
FACTUAL = json.dumps({'scene': 'factual', 'keep_memories': [], 'knowledge_query': 'SKOS安装方法'}, ensure_ascii=False)
RECALL = json.dumps({'scene': 'personal_recall', 'keep_memories': [2], 'knowledge_query': ''}, ensure_ascii=False)
NOFOCUS = FORMAL
PERSONA = 'PERSONA_COMPLETE:仿生人可以游泳，嘴硬心软，技术问答准确。'
FOCUS, KB = '本轮按角色场景推进', 'VERIFIED_SKOS_INSTALL_GUIDE'
OTHER, TXT, BASE = 'OTHER_PLUGIN_CONTEXT', '去下海抓个螃蟹', 'ORIGINAL_PERSONA_COMPLETE'
HISTORY = [{'role': 'user', 'content': 'RECENT_REAL_HISTORY'}]


def cfg(prep='on', streaming=False, timeout=30):
    c = {'enabled': True}
    if prep in ('on', 'off'):
        c['preparation'] = {'enabled': prep == 'on', 'provider_id': '', 'streaming': streaming, 'timeout_seconds': timeout}
    return {'sys001': {'character_dialogue': c, 'final_review': {'mode': 'off'}}}


class FakeProvider:
    def __init__(self, completion=FORMAL, streaming=False, final=True, delay=0.0, boom=False):
        self.provider_config = {'id': 'test-provider'}
        self.completion, self.streaming, self.final = completion, streaming, final
        self.delay, self.boom, self.plug = delay, boom, None
        self.calls, self.stream_calls = [], []

    async def _pre(self):
        if self.boom:
            raise AssertionError('auxiliary provider must not be called')
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.plug is not None:
            self.plug._auxiliary_terminated = True

    async def text_chat(self, *, prompt=None, func_tool=None, **k):
        self.calls.append({'prompt': prompt, 'func_tool': func_tool})
        await self._pre()
        return LLMResponse(role='assistant', completion_text=self.completion)

    async def text_chat_stream(self, *, prompt=None, func_tool=None, **k):
        self.stream_calls.append({'prompt': prompt, 'func_tool': func_tool})
        await self._pre()
        yield LLMResponse(role='assistant', completion_text=self.completion[:16], is_chunk=True)
        if self.final:
            yield LLMResponse(role='assistant', completion_text=self.completion, is_chunk=False)


class FakeKB:
    def __init__(self, fail=False, delay=0):
        self.fail, self.n, self.delay = fail, 0, delay
        self.kb = SimpleNamespace(kb_name='SKOS', doc_count=1, chunk_count=1)

    async def retrieve(self, *a, **k):
        self.n += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError('kb retrieve failed')
        return {'context_text': KB, 'results': [{'content': KB}]}

    async def get_kb_by_name(self, name):
        return SimpleNamespace(kb=self.kb)

    async def get_kb(self, kb_id):
        return SimpleNamespace(kb=self.kb)


def ctx(provider, kb):
    async def persona(*a, **k):
        return ('atri', {'prompt': PERSONA}, None, False)
    async def gp(*a, **k):
        return provider
    return SimpleNamespace(
        persona_manager=SimpleNamespace(resolve_selected_persona=persona),
        get_config=lambda *a, **k: {'provider_settings': {}, 'provider_ltm_settings': {'group_icl_enable': False},
                                    'kb_names': ['SKOS'], 'kb_final_top_k': 5, 'kb_fusion_top_k': 20, 'kb_agentic_mode': True},
        get_using_provider_async=gp,
        get_provider_by_id=lambda *a, **k: provider,
        kb_manager=kb,
    )


def mk(provider=None, kb=None, config=None):
    return ShioPlugin(ctx(provider or FakeProvider(), kb or FakeKB()), config or cfg())


def turn(mid='m1'):
    ev = _event(mid, 'u1', TXT, [Plain(TXT)], scope='bug001-group')
    ev.set_extra(SYS001_TURN_EXTRA, create_snapshot(ev, origin='directed'))
    return ev


def req(rag_text=RAG, extra=()):
    rp = TextPart(text=rag_text).mark_as_temp()
    op = TextPart(text=OTHER).mark_as_temp()
    r = ProviderRequest(prompt=TXT, system_prompt=BASE, contexts=list(HISTORY),
                        extra_user_content_parts=[rp, *extra, op])
    r.conversation = SimpleNamespace(persona_id='atri')
    return r, rp, op


def joined(r):
    return NL.join(p.text for p in r.extra_user_content_parts if isinstance(getattr(p, 'text', None), str))


def has(r, o):
    return any(p is o for p in r.extra_user_content_parts)


def calls(p):
    return p.calls + p.stream_calls


@pytest_asyncio.fixture(autouse=True)
def _kbstore(monkeypatch):
    async def _empty(*a, **k):
        return {}
    monkeypatch.setattr(knowledge_base_tools.sp, 'session_get', _empty, raising=False)
    yield


@pytest.mark.parametrize('prep', ['off', 'missing'])
async def test_preparation_disabled_keeps_rag(prep):
    prov = FakeProvider(boom=True)
    plug = mk(provider=prov, config=cfg(prep=prep))
    ev = turn()
    r, rp, op = req()
    await plug.attach_turn_and_project_capabilities(ev, r)
    assert rp.text == RAG
    assert has(r, rp) and has(r, op)
    assert r.prompt == TXT
    assert BASE in r.system_prompt
    assert calls(prov) == [] and ev.sent == []


async def test_roleplay_drops_memories_keeps_persona():
    prov = FakeProvider(FORMAL)
    plug = mk(provider=prov)
    ev = turn()
    r, rp, op = req()
    await plug.attach_turn_and_project_capabilities(ev, r)
    assert rp.text == RAG and not has(r, rp) and has(r, op)
    j = joined(r)
    assert 'UNRELATED_BIOS_INJECTION' not in j and 'MASTER_LIKES_CRAB' not in j
    assert BASE in r.system_prompt and r.prompt == TXT and r.contexts == HISTORY
    assert FOCUS in r.system_prompt
    assert prov.calls and PERSONA in prov.calls[0]['prompt'] and TXT in prov.calls[0]['prompt']
    assert ev.sent == []


class _DialoguePrepareRecorder(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.mark.parametrize(
    'provider_response, expected_status',
    [(FORMAL, 'valid'), ('NOT_A_VALID_DIALOGUE_PREPARATION_RESPONSE', 'invalid')],
)
async def test_dialogue_prepare_status_reaches_official_logger(provider_response, expected_status):
    prov = FakeProvider(provider_response)
    plug = mk(provider=prov)
    ev = turn()
    r, rp, op = req()
    recorder = _DialoguePrepareRecorder()
    previous_level = official_logger.level
    official_logger.setLevel(logging.INFO)
    official_logger.addHandler(recorder)
    try:
        await plug.attach_turn_and_project_capabilities(ev, r)
    finally:
        official_logger.removeHandler(recorder)
        official_logger.setLevel(previous_level)
    messages = [record.getMessage() for record in recorder.records]
    matches = [message for message in messages if message.startswith('dialogue_prepare status=')]
    assert len(matches) == 1
    message = matches[0]
    assert f'status={expected_status}' in message
    assert 'elapsed=' in message and 'candidates=' in message
    assert '%s' not in message and '%.2f' not in message and '%d' not in message
    assert TXT not in message
    assert ev.sent == []
    assert r.prompt == TXT and BASE in r.system_prompt


async def test_personal_recall_keeps_only_requested():
    plug = mk(provider=FakeProvider(RECALL))
    r, rp, op = req()
    await plug.attach_turn_and_project_capabilities(turn(), r)
    j = joined(r)
    assert 'MASTER_LIKES_CRAB' in j and 'UNRELATED_BIOS_INJECTION' not in j
    kept = [p for p in r.extra_user_content_parts if 'MASTER_LIKES_CRAB' in getattr(p, 'text', '')]
    assert kept and kept[0] is not rp and getattr(kept[0], '_no_save', None) is True


async def test_factual_retrieval_injects_verified_kb():
    prov, kb = FakeProvider(FACTUAL), FakeKB()
    plug = mk(provider=prov, kb=kb)
    r, rp, op = req()
    r.prompt = '如何安装SKOS？'
    ev = _event('m1', 'u1', r.prompt, [Plain(r.prompt)], scope='bug001-group')
    ev.set_extra(SYS001_TURN_EXTRA, create_snapshot(ev, origin='directed'))
    await plug.attach_turn_and_project_capabilities(ev, r)
    assert KB in joined(r) and kb.n >= 1
    kept = [p for p in r.extra_user_content_parts if KB in getattr(p, 'text', '')]
    assert kept and getattr(kept[0], '_no_save', None) is True


async def test_existing_official_kb_result_not_reretrieved():
    kb = FakeKB()
    plug = mk(provider=FakeProvider(FACTUAL), kb=kb)
    existing = TextPart(text='[Related Knowledge Base Results]:' + NL + 'EXISTING_OFFICIAL_RESULT').mark_as_temp()
    r, rp, op = req(extra=[existing])
    await plug.attach_turn_and_project_capabilities(turn(), r)
    assert has(r, existing) and kb.n == 0


BAD = [
    '{"scene":',
    json.dumps({'scene': 'roleplay', 'keep_memories': True, 'knowledge_query': ''}, ensure_ascii=False),
    json.dumps({'scene': 'roleplay', 'keep_memories': ['2'], 'knowledge_query': ''}, ensure_ascii=False),
    json.dumps({'scene': 'roleplay', 'keep_memories': [99], 'knowledge_query': ''}, ensure_ascii=False),
    NOFOCUS, NOFOCUS,
    json.dumps({'scene': 'roleplay', 'keep_memories': [True], 'knowledge_query': ''}, ensure_ascii=False),
    json.dumps({'scene': 'roleplay', 'keep_memories': [1, 1], 'knowledge_query': ''}, ensure_ascii=False),
]
OPTS = [{}, {}, {}, {}, {'delay': 0.1, 'timeout': 0.02}, {'terminated': True}, {}, {}]


@pytest.mark.parametrize('payload,opt', list(zip(BAD, OPTS)))
async def test_bad_or_failed_aux_keeps_rag(payload, opt):
    prov = FakeProvider(payload, delay=opt.get('delay', 0.0))
    plug = mk(provider=prov, config=cfg(timeout=opt.get('timeout', 30)))
    if opt.get('terminated'):
        prov.plug = plug
    r, rp, op = req()
    await plug.attach_turn_and_project_capabilities(turn(), r)
    assert rp.text == RAG and has(r, rp) and has(r, op)
    assert r.prompt == TXT
    j = joined(r)
    assert FOCUS not in j and KB not in j


@pytest.mark.parametrize('streaming,final', [(False, True), (True, True), (True, False)])
async def test_aux_stream_modes(streaming, final):
    prov = FakeProvider(FORMAL, streaming=streaming, final=final)
    plug = mk(provider=prov, config=cfg(streaming=streaming))
    r, rp, op = req()
    await plug.attach_turn_and_project_capabilities(turn(), r)
    if streaming:
        assert prov.stream_calls
    else:
        assert prov.calls
    if streaming and not final:
        assert has(r, rp) and 'MASTER_LIKES_CRAB' in joined(r)
    else:
        assert not has(r, rp) and 'MASTER_LIKES_CRAB' not in joined(r)


async def test_unknown_rag_kept_intact():
    plug = mk(provider=FakeProvider(FORMAL))
    r, rp, op = req(rag_text=UNKNOWN)
    await plug.attach_turn_and_project_capabilities(turn(), r)
    assert has(r, rp) and rp.text == UNKNOWN and has(r, op)


PREFIX_REAL_QUOTE = 'REAL_QUOTE_PREFIX:这是我真实说过的原话'
SUFFIX_REAL_NOTE = 'REAL_NOTE_SUFFIX:这是我真实补充的备注'


async def test_session_kb_disabled_does_not_fall_back_to_global(monkeypatch):
    async def _disabled(*a, **k):
        return {'kb_ids': []}

    monkeypatch.setattr(knowledge_base_tools.sp, 'session_get', _disabled, raising=False)
    kb = FakeKB()
    plug = mk(provider=FakeProvider(FACTUAL), kb=kb)
    r, rp, op = req()
    r.prompt = '请问SKOS该怎么安装？'
    ev = _event('m1', 'u1', r.prompt, [Plain(r.prompt)], scope='bug001-group')
    ev.set_extra(SYS001_TURN_EXTRA, create_snapshot(ev, origin='directed'))
    await plug.attach_turn_and_project_capabilities(ev, r)
    assert KB not in joined(r)
    assert kb.n == 0


async def test_factual_kb_failure_keeps_incomplete_without_fabricating():
    kb = FakeKB(fail=True)
    plug = mk(provider=FakeProvider(FACTUAL), kb=kb)
    r, rp, op = req()
    r.prompt = '请问SKOS该怎么安装？'
    ev = _event('m1', 'u1', r.prompt, [Plain(r.prompt)], scope='bug001-group')
    ev.set_extra(SYS001_TURN_EXTRA, create_snapshot(ev, origin='directed'))
    await plug.attach_turn_and_project_capabilities(ev, r)
    assert kb.n >= 1
    assert KB not in joined(r)
    assert '未完成' in r.system_prompt
    assert '没有相关文档' not in r.system_prompt
    assert has(r, op) and op.text == OTHER
    assert ev.sent == []


async def test_real_quote_and_note_survive_memory_pruning():
    plug = mk(provider=FakeProvider(FORMAL))
    full = PREFIX_REAL_QUOTE + RAG + SUFFIX_REAL_NOTE
    rp = TextPart(text=full).mark_as_temp()
    solid = TextPart(text=full)
    op = TextPart(text=OTHER).mark_as_temp()
    r = ProviderRequest(prompt=TXT, system_prompt=BASE, contexts=list(HISTORY),
                        extra_user_content_parts=[rp, solid, op])
    r.conversation = SimpleNamespace(persona_id='atri')
    await plug.attach_turn_and_project_capabilities(turn(), r)
    assert rp.text == full
    assert has(r, solid) and solid.text == full
    assert has(r, op) and op.text == OTHER
    j = joined(r)
    assert PREFIX_REAL_QUOTE in j and SUFFIX_REAL_NOTE in j and OTHER in j
    pruned = [p for p in r.extra_user_content_parts
              if p is not rp and p is not solid and p is not op
              and PREFIX_REAL_QUOTE in getattr(p, 'text', '')]
    assert pruned, 'pruned real-quote part must remain'
    assert 'UNRELATED_BIOS_INJECTION' not in pruned[0].text
    assert 'MASTER_LIKES_CRAB' not in pruned[0].text


async def test_preparation_reads_actual_dict_history_roles_not_media_or_tools():
    provider = FakeProvider(FORMAL)
    plugin = mk(provider=provider)
    request, _, _ = req()
    request.contexts = [
        {'role': 'tool', 'content': 'MUST_NOT_BE_DIALOGUE'},
        {'role': 'user', 'content': 'REAL_PREVIOUS_USER'},
        {'role': 'assistant', 'content': [
            {'type': 'text', 'text': 'REAL_PREVIOUS_ASSISTANT'},
            {'type': 'image_url', 'image_url': {'url': 'SECRET_MEDIA_URL'}},
        ]},
    ]
    await plugin.attach_turn_and_project_capabilities(turn(), request)
    prompt = provider.calls[0]['prompt']
    assert 'user: REAL_PREVIOUS_USER' in prompt
    assert 'assistant: REAL_PREVIOUS_ASSISTANT' in prompt
    assert 'MUST_NOT_BE_DIALOGUE' not in prompt and 'SECRET_MEDIA_URL' not in prompt


async def test_preparation_knows_configured_knowledge_library_names():
    provider = FakeProvider(FACTUAL)
    plugin = mk(provider=provider)
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(turn(), request)
    prompt = provider.calls[0]['prompt']
    assert '全局已配置知识库（实际选择以官方会话配置为准）' in prompt
    assert '["SKOS"]' in prompt


async def test_preparation_uses_existing_character_guidance_without_rewriting_it():
    provider = FakeProvider(FORMAL)
    config = cfg()
    config['sys001']['character_dialogue'].update({
        'situation_prompt': 'CURRENT_SITUATION_RULE',
        'expression_prompt': 'CURRENT_EXPRESSION_RULE',
    })
    plugin = mk(provider=provider, config=config)
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(turn(), request)
    prompt = provider.calls[0]['prompt']
    assert 'CURRENT_SITUATION_RULE' in prompt and 'CURRENT_EXPRESSION_RULE' in prompt
    assert PERSONA in prompt


@pytest.mark.parametrize('master', [False, True])
async def test_role_action_master_direction_uses_only_official_admin_identity(master):
    provider = FakeProvider(json.dumps({'scene':'role_action','keep_memories':[],'knowledge_query':''}))
    plugin = mk(provider=provider)
    event = turn()
    event.role = 'admin' if master else 'member'
    event.set_extra(SYS001_TURN_EXTRA, create_snapshot(event, origin='directed'))
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(event, request)
    rule = '性格体现在行动中的态度，不把嘴硬或撒娇当作继续拒绝、拖延指令的理由'
    assert (rule in request.system_prompt) is master


async def test_preparation_accepts_selection_only_not_generated_story_instructions():
    payload = json.dumps({'scene': 'roleplay', 'keep_memories': [], 'knowledge_query': ''})
    plugin = mk(provider=FakeProvider(payload))
    request, memory, _ = req()
    await plugin.attach_turn_and_project_capabilities(turn(), request)
    assert not has(request, memory)
    assert '本轮按角色场景推进' in request.system_prompt
    assert '本轮回应重点' not in request.system_prompt


async def test_generated_story_field_is_rejected_without_mutating_request():
    payload = json.loads(FORMAL)
    payload['response_focus'] = 'INVENTED_NEW_ROLE_LIMIT'
    plugin = mk(provider=FakeProvider(json.dumps(payload)))
    request, memory, other = req()
    await plugin.attach_turn_and_project_capabilities(turn(), request)
    assert has(request, memory) and has(request, other)
    assert 'INVENTED_NEW_ROLE_LIMIT' not in request.system_prompt


async def test_real_event_time_does_not_add_unrelated_month_facts():
    provider = FakeProvider(FORMAL)
    plugin = mk(provider=provider)
    event = turn()
    snapshot = replace(event.get_extra(SYS001_TURN_EXTRA),
                       created_at=datetime(2026, 10, 1, 5, 18, tzinfo=timezone.utc))
    event.set_extra(SYS001_TURN_EXTRA, snapshot)
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(event, request)
    assert '当前本地时间：2026-10-01 13:18:00' in provider.calls[0]['prompt']
    assert 'Shio 本轮日历事实' not in request.system_prompt
    assert '本月起点' not in request.system_prompt
    assert '本月到本轮已过' not in request.system_prompt


async def test_knowledge_timeout_does_not_publish_partial_preparation():
    plugin = mk(provider=FakeProvider(FACTUAL), kb=FakeKB(delay=0.2),
                config=cfg(timeout=0.04))
    request, memory, other = req()
    await asyncio.wait_for(plugin.attach_turn_and_project_capabilities(turn(), request), 1)
    assert has(request, memory) and has(request, other)
    assert '本轮是事实问答' not in request.system_prompt
    assert KB not in joined(request)


async def test_oversized_response_is_not_truncated_into_valid_json():
    payload = FORMAL + (' ' * 4000) + 'INVALID_TRAILING_TEXT'
    plugin = mk(provider=FakeProvider(payload))
    request, memory, _ = req()
    await plugin.attach_turn_and_project_capabilities(turn(), request)
    assert has(request, memory)
    assert FOCUS not in request.system_prompt


async def test_official_pipeline_dispatches_preparation_without_sending():
    provider = FakeProvider(FACTUAL)
    kb = FakeKB()
    async with _official_fixture() as (plugin, pipeline_context, _):
        plugin.context = ctx(provider, kb)
        # Reuse the fixture manager for a real ProviderRequest hand-off.
        from test_bug001_boundary_pipeline import _ConversationManager
        plugin.context.conversation_manager = _ConversationManager()
        plugin.config['sys001'].update(cfg()['sys001'])
        module = ShioPlugin.__module__
        star_handlers_registry.append(StarHandlerMetadata(
            event_type=EventType.OnLLMRequestEvent,
            handler_full_name=f'{module}_attach_turn_and_project_capabilities',
            handler_name='attach_turn_and_project_capabilities',
            handler_module_path=module,
            handler=plugin.attach_turn_and_project_capabilities, event_filters=[]))
        event = _event('pipeline-preparation', 'master', '@bot 如何安装SKOS？',
                       [At(qq='bot'), Plain('如何安装SKOS？')])
        await _run_official_pipeline(event, pipeline_context)
        request = event.get_extra('provider_request')
        assert request is not None
        memory = TextPart(text=RAG).mark_as_temp()
        other = TextPart(text=OTHER).mark_as_temp()
        request.extra_user_content_parts = [memory, other]
        assert await call_event_hook(event, EventType.OnLLMRequestEvent, request) is False
        assert kb.n == 1 and KB in joined(request)
        assert not has(request, memory) and has(request, other)
        assert PERSONA in provider.calls[0]['prompt']
        assert event.sent == []


async def test_web_schema_and_runtime_default_preparation_agree():
    from pathlib import Path
    from astrbot_plugin_shio.core.dialogue_preparation import DEFAULT_PREPARATION_PROMPT
    schema = json.loads((Path(__file__).parents[1] / '_conf_schema.json').read_text(encoding='utf-8-sig'))
    fields = schema['sys001']['items']['character_dialogue']['items']['preparation']['items']
    assert fields['enabled']['default'] is False
    assert fields['provider_id']['_special'] == 'select_provider'
    assert fields['streaming']['default'] is False
    assert fields['timeout_seconds']['default'] == 30
    assert fields['prompt']['default'] == DEFAULT_PREPARATION_PROMPT
    provider = FakeProvider(FORMAL)
    plugin = mk(provider=provider)
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(turn(), request)
    assert DEFAULT_PREPARATION_PROMPT in provider.calls[0]['prompt']

