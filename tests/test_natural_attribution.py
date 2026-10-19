import asyncio
import copy
import importlib
import inspect
import json
from contextlib import suppress
from dataclasses import FrozenInstanceError, replace

import pytest

import astrbot.core.agent.message as agent_message
import astrbot_plugin_shio.core.character_dialogue as character_dialogue
import astrbot_plugin_shio.core.natural_context as natural_context
import astrbot_plugin_shio.core.sys001 as sys001
from astrbot.core.message.components import Plain
from test_bug001_boundary_pipeline import _event
from test_dialogue_preparation import FORMAL, FakeProvider, cfg, mk, req


_MISSING = object()
_LEGACY_RULES = (
    '旧规则：只输出 {"decision":"REPLY"}。\n'
    '对聊天内容有兴趣时才参与。'
)


def _required(module, name):
    value = getattr(module, name, _MISSING)
    assert value is not _MISSING, f"{module.__name__}.{name} is required"
    return value


def _named_key(name):
    for module in (sys001, natural_context, character_dialogue):
        value = getattr(module, name, _MISSING)
        if value is not _MISSING:
            return value
    raise AssertionError(f"{name} is required")


def _turn_key():
    return _named_key("SYS001_TURN_EXTRA")


def _context_key():
    return _named_key("CONTEXT_EXTRA")


def _review_key():
    return _named_key("REVIEW_REFERENCE_EXTRA")


def _snapshot(event, origin="natural"):
    for module in (sys001, natural_context, character_dialogue):
        factory = getattr(module, "create_snapshot", None)
        if callable(factory):
            return factory(event, origin=origin)
    raise AssertionError("create_snapshot is required")


def _natural_event(mid, text, scope="natural-group"):
    event = _event(mid, "u1", text, [Plain(text)], scope=scope)
    snapshot = _snapshot(event, "natural")
    event.set_extra(_turn_key(), snapshot)
    return event, snapshot


def _parse(text, sources=()):
    parser = _required(sys001, "parse_natural_participation_decision")
    assert "sources" in inspect.signature(parser).parameters, (
        "parse_natural_participation_decision must accept sources="
    )
    return parser(text, sources=tuple(sources))


def _attribution_for(event, snapshot):
    resolver = getattr(natural_context, "attribution_for_turn", None)
    assert callable(resolver), "natural_context.attribution_for_turn is required"
    return resolver(event, snapshot)


def _format(value, snapshot=None):
    formatter = getattr(natural_context, "format_natural_attribution", None)
    assert callable(formatter), "natural_context.format_natural_attribution is required"
    if snapshot is None:
        return formatter(value)
    assert 'snapshot' in inspect.signature(formatter).parameters, (
        'format_natural_attribution must accept snapshot='
    )
    return formatter(value, snapshot=snapshot)


def _decision_text(decision="REPLY", attribution=_MISSING):
    payload = {"decision": decision}
    if attribution is not _MISSING:
        payload["attribution"] = attribution
    return json.dumps(payload, ensure_ascii=False)


def _valid_attr(perspective="observer", addressee="小明", evidence="小明说你好", facts=_MISSING):
    if facts is _MISSING:
        facts = [{"subject": "小明", "action": "说你好", "evidence": evidence}] if evidence else []
    return {
        "perspective": perspective,
        "addressee": addressee,
        "evidence": evidence,
        "facts": facts,
    }


def _assert_decision_without_attr(result, decision):
    assert result is not None
    assert getattr(result, "decision", None) == decision
    assert getattr(result, "attribution", _MISSING) is None


def _require_attr(result):
    assert result is not None
    assert getattr(result, "decision", None) == "REPLY"
    value = getattr(result, "attribution", _MISSING)
    assert value is not _MISSING, 'attribution field is required'
    assert value is not None, "valid REPLY attribution must be stored"
    return value


def _plugin(completion=FORMAL, *, prep="off", config=None):
    provider = FakeProvider(completion)
    plugin = mk(provider, config=config or cfg(prep))
    return plugin, provider


def _set_group_icl(plugin, enabled=True):
    original = plugin.context.get_config

    def get_config(*args, **kwargs):
        value = dict(original(*args, **kwargs))
        value["provider_settings"] = {}
        value["provider_ltm_settings"] = {"group_icl_enable": enabled}
        value["kb_names"] = []
        value["kb_agentic_mode"] = False
        return value

    plugin.context.get_config = get_config


def _set_persona(plugin, text):
    async def resolve(*args, **kwargs):
        return "atri", {"prompt": text}, None, False

    plugin.context.persona_manager.resolve_selected_persona = resolve


def _calls(provider):
    return provider.calls + provider.stream_calls


def _batch_item(event, snapshot):
    module = importlib.import_module('astrbot_plugin_shio.main')
    cls = getattr(module, "_BatchMessage", None)
    assert callable(cls), "main._BatchMessage is required"
    return cls(event, snapshot, mandatory=False, component_facts=(), history_row_id=None)


async def _persisted_text(request):
    assembled = await request.assemble_context()
    runtime = agent_message.bind_checkpoint_messages([*request.contexts, assembled])
    persisted = agent_message.dump_messages_with_checkpoints(runtime)
    return json.dumps(persisted, ensure_ascii=False)


@pytest.mark.parametrize("decision", ["REPLY", "WAIT", "NO_ACTION"])
def test_legacy_decisions_keep_decision_without_attribution(decision):
    result = _parse(_decision_text(decision))
    _assert_decision_without_attr(result, decision)


@pytest.mark.parametrize(
    "text",
    [
        "not-json",
        "[]",
        json.dumps({"decision": "REPLY", "extra": 1}),
        json.dumps({"decision": 1}),
        json.dumps({"decision": "reply"}),
        json.dumps({"decision": "REPLY", "attribution": _valid_attr(), "extra": 1}),
        json.dumps({"decision": "WAIT", "attribution": _valid_attr()}),
        json.dumps({"decision": "WAIT", "attribution": None}),
        json.dumps({"decision": "NO_ACTION", "attribution": _valid_attr()}),
        json.dumps({"decision": "NO_ACTION", "attribution": "invalid"}),
    ],
)
def test_invalid_outer_schema_returns_none(text):
    assert _parse(text, sources=("小明说你好",)) is None


@pytest.mark.parametrize(
    "perspective,addressee,evidence,sources,expected_facts",
    [
        (
            "observer",
            "小明的表弟",
            "小明的表弟说：我也想去",
            ("小明的表弟说：我也想去",),
            [("小明的表弟", "说：我也想去", "小明的表弟说：我也想去")],
        ),
        (
            "participant",
            "阿蓝",
            "阿蓝：我们走吧",
            ("阿蓝：我们走吧", "Shio：好"),
            [("Shio", "说好", "Shio：好")],
        ),
        ("uncertain", "", "", ("x",), []),
    ],
)
def test_valid_attribution_fields_and_immutable_values(
    perspective, addressee, evidence, sources, expected_facts
):
    facts = [
        {"subject": subject, "action": action, "evidence": fact_evidence}
        for subject, action, fact_evidence in expected_facts
    ]
    result = _parse(
        _decision_text(attribution=_valid_attr(perspective, addressee, evidence, facts)),
        sources=sources,
    )
    attr = _require_attr(result)
    attr_cls = _required(natural_context, "NaturalAttribution")
    fact_cls = _required(natural_context, "NaturalAttributionFact")
    assert isinstance(attr, attr_cls)
    assert (attr.perspective, attr.addressee, attr.evidence) == (perspective, addressee, evidence)
    assert isinstance(attr.facts, tuple)
    assert len(attr.facts) == len(expected_facts)
    for fact, expected in zip(attr.facts, expected_facts):
        assert isinstance(fact, fact_cls)
        assert (fact.subject, fact.action, fact.evidence) == expected
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        attr.addressee = "changed"
    if attr.facts:
        with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
            attr.facts[0].subject = "changed"


def _bad_attr(case):
    if case == 'attribution-string':
        return 'not-an-object'
    attr = _valid_attr()
    if case == "missing-perspective":
        del attr["perspective"]
    elif case == "unknown-attr-key":
        attr["extra"] = 1
    elif case == "bad-perspective":
        attr["perspective"] = "bot"
    elif case == "addressee-nonstring":
        attr["addressee"] = 7
    elif case == "addressee-whitespace":
        attr["addressee"] = " \t"
    elif case == "evidence-whitespace":
        attr["evidence"] = " \t"
        attr["facts"] = []
    elif case == "evidence-empty":
        attr["evidence"] = ""
        attr["facts"] = []
    elif case == "fact-unknown-key":
        attr["facts"][0]["extra"] = 1
    elif case == "fact-missing-action":
        del attr["facts"][0]["action"]
    elif case == "fact-control":
        attr["facts"][0]["subject"] = "a\nb"
    elif case == "fact-nonstring":
        attr["facts"][0]["action"] = 3
    elif case == 'facts-nonlist':
        attr['facts'] = 'not-a-list'
    elif case == 'fact-nondict':
        attr['facts'] = ['not-a-dict']
    elif case == 'addressee-nul':
        attr['addressee'] = 'a\x00b'
    elif case == 'addressee-del':
        attr['addressee'] = 'a\x7fb'
    elif case == 'evidence-tab':
        attr['evidence'] = '小明\t说你好'
        attr['facts'] = []
    elif case == 'fact-evidence-nul':
        attr['facts'][0]['evidence'] = '小明\x00说你好'
    elif case == 'fact-evidence-empty':
        attr['facts'][0]['evidence'] = ''
    elif case == 'fact-evidence-overlength':
        attr['facts'][0]['evidence'] = 'f' * 301
    return attr


@pytest.mark.parametrize(
    "case,decision",
    [
        ("missing-perspective", "REPLY"),
        ("unknown-attr-key", "REPLY"),
        ("bad-perspective", "REPLY"),
        ("addressee-nonstring", "REPLY"),
        ("addressee-whitespace", "REPLY"),
        ("evidence-whitespace", "REPLY"),
        ("evidence-empty", "REPLY"),
        ("fact-unknown-key", "REPLY"),
        ("fact-missing-action", "REPLY"),
        ("fact-control", "REPLY"),
        ("fact-nonstring", "REPLY"),
        ('attribution-string', 'REPLY'),
        ('facts-nonlist', 'REPLY'),
        ('fact-nondict', 'REPLY'),
        ('addressee-nul', 'REPLY'),
        ('addressee-del', 'REPLY'),
        ('evidence-tab', 'REPLY'),
        ('fact-evidence-nul', 'REPLY'),
        ('fact-evidence-empty', 'REPLY'),
        ('fact-evidence-overlength', 'REPLY'),
    ],
)
def test_invalid_attribution_preserves_decision_without_attr(case, decision):
    attr = _bad_attr(case)
    sources = ['小明说你好', ' \t']
    if isinstance(attr, dict):
        evidence = attr.get('evidence')
        if isinstance(evidence, str) and evidence:
            sources.append(evidence)
        facts = attr.get('facts')
        if isinstance(facts, list):
            for fact in facts:
                if isinstance(fact, dict):
                    evidence = fact.get('evidence')
                    if isinstance(evidence, str) and evidence:
                        sources.append(evidence)
    result = _parse(_decision_text(decision, attr), sources=tuple(sources))
    _assert_decision_without_attr(result, decision)


def _boundary_payload(kind, size):
    source = "小明说你好"
    if kind == "addressee":
        return _valid_attr("observer", "a" * size, source, facts=[]), (source,)
    if kind == "subject":
        facts = [{"subject": "s" * size, "action": "点头", "evidence": source}]
        return _valid_attr("observer", "小明", source, facts), (source,)
    if kind == "action":
        facts = [{"subject": "小明", "action": "a" * size, "evidence": source}]
        return _valid_attr("observer", "小明", source, facts), (source,)
    if kind == "evidence":
        evidence = "e" * size
        return _valid_attr("observer", "小明", evidence, facts=[]), ((evidence,) if evidence else ("x",))
    if kind == "facts":
        facts = [{"subject": f"s{i}", "action": "a", "evidence": source} for i in range(size)]
        return _valid_attr("observer", "小明", source, facts), (source,)
    if kind == 'fact-evidence':
        evidence = 'f' * size
        facts = [{'subject': '小明', 'action': '点头', 'evidence': evidence}]
        return _valid_attr('observer', '小明', source, facts), ((source, evidence) if evidence else (source, 'x'))
    raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind,size,expected",
    [
        ("addressee", 80, True),
        ("addressee", 81, False),
        ("subject", 0, False),
        ("subject", 1, True),
        ("subject", 80, True),
        ("subject", 81, False),
        ("action", 0, False),
        ("action", 1, True),
        ("action", 160, True),
        ("action", 161, False),
        ("evidence", 0, False),
        ("evidence", 1, True),
        ("evidence", 300, True),
        ("evidence", 301, False),
        ('fact-evidence', 0, False),
        ('fact-evidence', 1, True),
        ('fact-evidence', 300, True),
        ('fact-evidence', 301, False),
        ("facts", 0, True),
        ("facts", 6, True),
        ("facts", 7, False),
    ],
)
def test_literal_field_boundaries(kind, size, expected):
    attr, sources = _boundary_payload(kind, size)
    result = _parse(_decision_text(attribution=attr), sources=sources)
    if expected:
        _require_attr(result)
    else:
        _assert_decision_without_attr(result, "REPLY")


def test_evidence_must_be_verbatim_single_chat_source_only():
    source1 = "甲：周末去爬山吗？"
    source2 = "乙：好呀"
    good = _parse(_decision_text(attribution=_valid_attr("observer", "乙", "好呀", facts=[])), (source1, source2))
    assert _require_attr(good).evidence == "好呀"
    for evidence, sources in [
        ("去游泳", (source1, source2)),
        ("周末去爬山吗？乙：好呀", (source1, source2)),
    ]:
        result = _parse(_decision_text(attribution=_valid_attr("observer", "乙", evidence, facts=[])), sources)
        _assert_decision_without_attr(result, "REPLY")
    normalized = _parse(
        _decision_text(attribution=_valid_attr("observer", "甲", "周末去爬山吗？", facts=[])),
        ("甲：周末 去爬山吗？",),
    )
    _assert_decision_without_attr(normalized, "REPLY")


def test_formatter_json_encodes_dynamic_fields():
    addressee = 'a"b\\c'
    subject = 's"t\\u'
    action = 'say"hi\\'
    evidence = 'e: a"b\\c'
    facts = [{"subject": subject, "action": action, "evidence": evidence}]
    attr = _require_attr(
        _parse(
            _decision_text(attribution=_valid_attr("observer", addressee, evidence, facts)),
            sources=(evidence,),
        )
    )
    text = _format(attr)
    assert json.dumps(addressee, ensure_ascii=False) in text
    assert json.dumps(subject, ensure_ascii=False) in text
    assert json.dumps(action, ensure_ascii=False) in text
    assert addressee not in text


@pytest.mark.parametrize('is_master', [False, True])
def test_formatter_keeps_trusted_source_facts_separate_from_original_addressee(is_master):
    evidence = '丙点头'
    _, original = _natural_event('source-facts', evidence)
    sender_name = '甲' + chr(34) + chr(92) + chr(10) + '乙'
    snapshot = replace(
        original,
        sender_id='speaker-7',
        sender_name=sender_name,
        is_master=is_master,
        self_id='bot-self-9',
        at_targets=('peer-2', 'bot-self-9'),
    )
    facts = [{'subject': '丙', 'action': '点头', 'evidence': evidence}]
    attr = _require_attr(
        _parse(
            _decision_text(attribution=_valid_attr('observer', '乙', evidence, facts)),
            sources=(evidence,),
        )
    )

    text = _format(attr, snapshot=snapshot)
    label_index = text.index('平台来源事实')
    json_start = text.index('{', label_index)
    metadata, _ = json.JSONDecoder().raw_decode(text[json_start:])
    assert metadata['sender_id'] == 'speaker-7'
    assert metadata['sender_name'] == sender_name
    assert metadata['is_master'] is is_master
    assert metadata['bot_self_id'] == 'bot-self-9'
    assert metadata['at_targets'] == list(snapshot.at_targets)
    assert metadata['sender_name'] != '乙'
    assert 'addressee' not in metadata

    last_line = [line for line in text.splitlines() if line.strip()][-1]
    original_frame = json.loads(last_line)
    assert original_frame == {
        'perspective': 'observer',
        'addressee': '乙',
        'evidence': evidence,
        'facts': facts,
    }
    assert json.dumps('乙', ensure_ascii=False) in text
    assert json.dumps('丙', ensure_ascii=False) in text
    assert json.dumps('点头', ensure_ascii=False) in text
    assert json.dumps(sender_name, ensure_ascii=False) in text
    assert sender_name not in text

    plain = _format(attr)
    assert plain.strip()
    assert json.loads([line for line in plain.splitlines() if line.strip()][-1]) == original_frame
    assert _format(None, snapshot=snapshot) == ''
    assert _format({}, snapshot=snapshot) == ''


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'mode',
    [
        'observer-naturalgroup',
        'participant-naturalgroup',
        'directedgroup',
        'directedprivate',
        'naturaloriginprivate',
    ],
)
async def test_master_style_survives_source_and_addressee_axes(mode):
    sentinel = 'MASTER_STYLE_SENTINEL'
    perspective = 'participant' if mode.startswith('participant-') else 'observer'
    control = mode in {'directedgroup', 'directedprivate', 'naturaloriginprivate'}
    evidence = '甲说：丙点头'
    config = cfg('on')
    identity = config['sys001'].setdefault('identity', {})
    identity['master_relationship_enabled'] = True
    identity['master_relationship_prompt'] = sentinel
    config['sys001']['final_review']['mode'] = 'combined'
    plugin, provider = _plugin(
        _decision_text(attribution=_valid_attr(perspective, '乙', evidence, facts=[])),
        prep='on',
        config=config,
    )
    event, original = _natural_event('master-style', evidence)
    snapshot = replace(
        original,
        is_master=True,
        sender_id='master-1',
        sender_name='甲',
    )
    event.set_extra(_turn_key(), snapshot)
    assert await plugin._decide_natural_participation(event, snapshot) == 'REPLY'

    if mode == 'directedgroup':
        current = replace(snapshot, origin='directed', is_private=False)
    elif mode == 'directedprivate':
        current = replace(snapshot, origin='directed', is_private=True)
    elif mode == 'naturaloriginprivate':
        current = replace(snapshot, is_private=True)
    else:
        current = snapshot
    if current is not snapshot:
        event.set_extra(_turn_key(), current)

    attribution = _attribution_for(event, current)
    if control:
        assert attribution is None
    else:
        assert attribution is not None

    provider.completion = FORMAL
    request, _, _ = req()
    original_system = request.system_prompt
    original_contexts = request.contexts
    expected_contexts = copy.deepcopy(original_contexts)
    original_model_context = current.model_context()
    original_identity = copy.deepcopy(config['sys001']['identity'])
    await plugin.attach_turn_and_project_capabilities(event, request)
    prep_prompts = [call['prompt'] for call in _calls(provider)]
    assert prep_prompts
    prep_prompt = prep_prompts[-1]
    main_text = '\n'.join(
        getattr(part, 'text', '') for part in request.extra_user_content_parts
    )

    assert request.system_prompt.startswith(original_system)
    assert request.contexts is original_contexts
    assert request.contexts == expected_contexts
    assert sentinel in request.system_prompt
    assert current.is_master is True
    assert current.model_context() == original_model_context
    assert config['sys001']['identity'] == original_identity
    assert sentinel in prep_prompt
    assert event.sent == []

    reference = event.get_extra(_review_key())
    assert isinstance(reference, str)
    assert sentinel in reference
    if control:
        assert '平台来源事实' not in main_text
        assert all('平台来源事实' not in prompt for prompt in prep_prompts)
        assert '平台来源事实' not in reference
    else:
        assert '平台来源事实' in main_text

    provider.completion = json.dumps({'action': 'keep'})
    review = getattr(plugin, '_review_final_text', None)
    assert callable(review), '_review_final_text is required'
    await review(event, current, '回复')
    review_prompt = _calls(provider)[-1]['prompt']
    assert sentinel in review_prompt
    if control:
        assert '平台来源事实' not in review_prompt
    assert event.sent == []


def _gate_transport_case(kind, streaming=False):
    if kind == "current":
        text = "小明：今晚一起看星星吗？"
        event, snapshot = _natural_event("gate-current", text)
        attr = _valid_attr("observer", "小明", text, facts=[])
    elif kind == "participant-no-at":
        text = "小明：今晚一起看星星吗？\nShio：好"
        event, snapshot = _natural_event("gate-participant", text)
        attr = _valid_attr(
            "participant", "小明", "今晚一起看星星吗？",
            [{"subject": "Shio", "action": "说好", "evidence": "Shio：好"}],
        )
    elif kind == "history":
        event, snapshot = _natural_event("gate-history", "当前消息")
        event.set_extra(_context_key(), {"status": "available", "records": (("h1", "旧记录：明天带伞"),)})
        attr = _valid_attr("observer", "旧记录", "明天带伞", facts=[])
    elif kind == "batch":
        event, snapshot = _natural_event("gate-batch", "当前消息")
        earlier_event, earlier_snapshot = _natural_event("gate-batch-old", '小A：他说 "好" 呀\n第二行')
        event.set_extra("shio.sys001.batch", (_batch_item(earlier_event, earlier_snapshot), _batch_item(event, snapshot)))
        attr = _valid_attr("observer", "小A", '他说 "好" 呀', facts=[])
    else:
        raise AssertionError(kind)
    config = cfg('off')
    config['sys001'].setdefault('model_transport', {})['stream_provider_ids'] = (
        ['test-provider'] if streaming else []
    )
    config['sys001']['natural_participation_prompt'] = _LEGACY_RULES
    plugin, provider = _plugin(_decision_text(attribution=attr), config=config)
    original_group_settings = plugin._group_settings
    plugin._group_settings = lambda: {
        **original_group_settings(),
        'natural_decision_streaming': streaming,
        'natural_participation_prompt': _LEGACY_RULES,
    }
    if kind == "history":
        _set_group_icl(plugin, True)
    return plugin, provider, event, snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('kind', ['current', 'participant-no-at', 'history', 'batch'])
async def test_real_gate_transports_only_actual_chat_sources(kind, streaming):
    plugin, provider, event, snapshot = _gate_transport_case(kind, streaming)
    assert await plugin._decide_natural_participation(event, snapshot) == "REPLY"
    attr = _attribution_for(event, snapshot)
    assert attr is not None
    assert attr.perspective == ('participant' if kind == 'participant-no-at' else 'observer')
    assert event.get_extra(natural_context.NATURAL_ATTRIBUTION_EXTRA) is not None
    if streaming:
        assert len(provider.stream_calls) == 1
        assert not provider.calls
        call = provider.stream_calls[0]
    else:
        assert len(provider.calls) == 1
        assert not provider.stream_calls
        call = provider.calls[0]
    assert call["func_tool"] is None
    prompt = call["prompt"]
    assert _LEGACY_RULES in prompt
    message_pos = prompt.index(snapshot.message_text)
    tail = prompt[message_pos + len(snapshot.message_text):]
    for term in (
        'attribution', 'perspective', 'addressee', 'evidence', 'facts',
        'observer', 'participant', 'uncertain', 'subject', 'action',
    ):
        assert term in tail
    lower_tail = tail.lower()
    assert 'rules' in lower_tail or '旧规则' in tail or 'legacy' in lower_tail or '旧格式' in tail
    assert any(
        marker in lower_tail
        for marker in (
            'subordinate', 'subordinat', 'override', 'supersed',
            'precedence', '优先', '覆盖', '不再', '为准',
        )
    )
    if kind == 'participant-no-at':
        assert '@' not in snapshot.message_text
    if kind == 'batch':
        assert '他说 "好" 呀' not in prompt


@pytest.mark.asyncio
async def test_gate_rejects_attribution_only_from_persona_or_omitted_history():
    plugin, provider = _plugin(
        _decision_text(attribution=_valid_attr("observer", "人格", "蓝鲸会唱歌", facts=[]))
    )
    _set_persona(plugin, "人格专属证据：蓝鲸会唱歌")
    event, snapshot = _natural_event("gate-persona", "当前消息")
    assert await plugin._decide_natural_participation(event, snapshot) == "REPLY"
    assert _attribution_for(event, snapshot) is None
    assert len(_calls(provider)) == 1

    old = "旧历史证据：海豚会飞"
    records = (("old", old),) + tuple((f"h{i}", f"近况{i}") for i in range(120))
    plugin2, provider2 = _plugin(
        _decision_text(attribution=_valid_attr("observer", "旧历史", "海豚会飞", facts=[]))
    )
    _set_group_icl(plugin2, True)
    event2, snapshot2 = _natural_event("gate-old", "当前消息")
    event2.set_extra(_context_key(), {"status": "available", "records": records})
    assert await plugin2._decide_natural_participation(event2, snapshot2) == "REPLY"
    assert _attribution_for(event2, snapshot2) is None
    assert len(_calls(provider2)) == 1

    rules_prompt = _LEGACY_RULES + '\n规则专属证据：鲸鱼会写字'
    config3 = cfg('off')
    config3['sys001']['natural_participation_prompt'] = rules_prompt
    plugin3, provider3 = _plugin(
        _decision_text(attribution=_valid_attr('observer', '规则', '鲸鱼会写字', facts=[])),
        config=config3,
    )
    groups3 = plugin3._group_settings
    plugin3._group_settings = lambda: {
        **groups3(),
        'natural_participation_prompt': rules_prompt,
    }
    event3, snapshot3 = _natural_event('gate-rules', '当前消息')
    assert await plugin3._decide_natural_participation(event3, snapshot3) == 'REPLY'
    assert _attribution_for(event3, snapshot3) is None
    assert len(_calls(provider3)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["wait", "no-action", "legacy-reply", "invalid", "timeout-zero"])
async def test_next_gate_start_clears_previous_attribution(case):
    plugin, provider = _plugin(_decision_text(attribution=_valid_attr()))
    event, snapshot = _natural_event("clear", "小明说你好")
    assert await plugin._decide_natural_participation(event, snapshot) == "REPLY"
    assert _attribution_for(event, snapshot) is not None
    if case == "timeout-zero":
        original = plugin._group_settings
        plugin._group_settings = lambda: {**original(), "natural_decision_timeout_seconds": 0}
        expected = ""
    else:
        payload = {
            "wait": _decision_text("WAIT"),
            "no-action": _decision_text("NO_ACTION"),
            "legacy-reply": _decision_text("REPLY"),
            "invalid": "not-json",
        }[case]
        provider.completion = payload
        expected = "" if case == "invalid" else payload and json.loads(payload)["decision"]
    assert await plugin._decide_natural_participation(event, snapshot) == expected
    assert _attribution_for(event, snapshot) is None


@pytest.mark.asyncio
async def test_attribution_is_bound_to_exact_snapshot_instance():
    plugin, _ = _plugin(_decision_text(attribution=_valid_attr()))
    event, snapshot = _natural_event("identity", "小明说你好")
    assert await plugin._decide_natural_participation(event, snapshot) == "REPLY"
    assert _attribution_for(event, snapshot) is not None
    equal = replace(snapshot)
    assert equal is not snapshot
    assert _attribution_for(event, equal) is None
    scope_event, scope_snapshot = _natural_event("identity-scope", "小明说你好", scope="other")
    assert _attribution_for(event, scope_snapshot) is None
    message_event, message_snapshot = _natural_event("identity-message", "另一条")
    assert _attribution_for(event, message_snapshot) is None
    other_event, other_snapshot = _natural_event("identity-other", "小明说你好")
    assert _attribution_for(other_event, other_snapshot) is None
    plugin._natural_generations[snapshot.scope] = (
        plugin._natural_generations.get(snapshot.scope, 0) + 1
    )
    plugin._bind_auxiliary_call(event, snapshot)
    assert _attribution_for(event, snapshot) is not None


class _HeldProvider(FakeProvider):
    def __init__(self, completion):
        super().__init__(completion)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def text_chat(self, *, prompt=None, func_tool=None, **kwargs):
        self.started.set()
        await self.release.wait()
        return await super().text_chat(prompt=prompt, func_tool=func_tool, **kwargs)


@pytest.mark.asyncio
async def test_late_or_cancelled_gate_never_stores_attribution():
    provider = _HeldProvider(_decision_text(attribution=_valid_attr()))
    plugin = mk(provider, config=cfg("off"))
    event, snapshot = _natural_event("late", "小明说你好")
    task = asyncio.create_task(plugin._decide_natural_participation(event, snapshot))
    try:
        await asyncio.wait_for(provider.started.wait(), 1)
        plugin._auxiliary_terminated = True
        provider.release.set()
        assert await asyncio.wait_for(task, 1) == ""
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    assert _attribution_for(event, snapshot) is None

    provider2 = _HeldProvider(_decision_text(attribution=_valid_attr()))
    plugin2 = mk(provider2, config=cfg("off"))
    event2, snapshot2 = _natural_event("cancel", "小明说你好")
    task2 = asyncio.create_task(plugin2._decide_natural_participation(event2, snapshot2))
    try:
        await asyncio.wait_for(provider2.started.wait(), 1)
        task2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task2
    finally:
        if not task2.done():
            task2.cancel()
        with suppress(asyncio.CancelledError):
            await task2
    assert _attribution_for(event2, snapshot2) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["character-disabled", "preparation-off", "preparation-invalid"])
async def test_main_request_transport_in_all_preparation_modes(mode):
    config = cfg("on" if mode == "preparation-invalid" else "off")
    if mode == "character-disabled":
        config["sys001"]["character_dialogue"]["enabled"] = False
    plugin, provider = _plugin(_decision_text(attribution=_valid_attr()), config=config)
    event, snapshot = _natural_event("attach", "小明说你好")
    assert await plugin._decide_natural_participation(event, snapshot) == "REPLY"
    formatted = _format(_attribution_for(event, snapshot), snapshot)
    if mode == "preparation-invalid":
        provider.completion = "not-json"
    request, _, _ = req()
    original_system = request.system_prompt
    original_contexts = request.contexts
    expected_contexts = copy.deepcopy(original_contexts)
    await plugin.attach_turn_and_project_capabilities(event, request)
    last = request.extra_user_content_parts[-1]
    assert isinstance(last, _required(agent_message, "TextPart"))
    assert last.text == formatted
    assert getattr(last, "_no_save", False) is True
    assert request.system_prompt.startswith(original_system)
    assert request.contexts is original_contexts
    assert request.contexts == expected_contexts
    assert event.sent == []
    reference = event.get_extra(_review_key())
    assert isinstance(reference, str) and formatted in reference
    assert formatted not in await _persisted_text(request)


@pytest.mark.asyncio
async def test_preparation_sees_full_attribution_at_current_message_tail():
    evidence = "关键证据"
    long_text = ("普通内容" * 1600) + evidence
    plugin, provider = _plugin(
        _decision_text(attribution=_valid_attr("observer", "小明", evidence, facts=[])),
        config=cfg("on"),
    )
    event, snapshot = _natural_event("budget", long_text)
    assert len(snapshot.message_text) > 6000
    assert await plugin._decide_natural_participation(event, snapshot) == "REPLY"
    formatted = _format(_attribution_for(event, snapshot), snapshot)
    provider.completion = FORMAL
    text_part = _required(agent_message, "TextPart")
    request, _, _ = req(rag_text="rag", extra=(text_part(text="z" * 6000).mark_as_temp(),))
    request.prompt = long_text
    await plugin.attach_turn_and_project_capabilities(event, request)
    calls = _calls(provider)
    assert len(calls) == 2
    assert evidence in calls[0]["prompt"]
    assert formatted in calls[1]["prompt"]
    assert request.extra_user_content_parts[-1].text == formatted
    plugin._bind_auxiliary_call(event, snapshot)
    assert _attribution_for(event, snapshot) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('is_private', [False, True])
async def test_review_and_direct_private_controls(is_private):
    config = cfg("off")
    config["sys001"]["final_review"]["mode"] = "combined"
    plugin, provider = _plugin(_decision_text(attribution=_valid_attr()), config=config)
    event, snapshot = _natural_event("review", "小明说你好")
    assert await plugin._decide_natural_participation(event, snapshot) == "REPLY"
    formatted = _format(_attribution_for(event, snapshot), snapshot)
    request, _, _ = req()
    await plugin.attach_turn_and_project_capabilities(event, request)
    assert formatted in event.get_extra(_review_key())
    provider.completion = '{"action":"keep"}'
    review = getattr(plugin, "_review_final_text", None)
    assert callable(review), "_review_final_text is required"
    await review(event, snapshot, "reply")
    assert formatted in provider.calls[-1]["prompt"]

    plugin2, _ = _plugin(_decision_text(attribution=_valid_attr()))
    event2, _ = _natural_event("direct", "小明说你好")
    assert await plugin2._decide_natural_participation(event2, event2.get_extra(_turn_key())) == "REPLY"
    natural = event2.get_extra(_turn_key())
    direct = replace(natural, origin='directed', is_private=is_private)
    event2.set_extra(_turn_key(), direct)
    assert _attribution_for(event2, direct) is None
    request2, _, _ = req()
    original_system = request2.system_prompt
    await plugin2.attach_turn_and_project_capabilities(event2, request2)
    assert all(getattr(part, "text", None) != formatted for part in request2.extra_user_content_parts)
    assert request2.system_prompt.startswith(original_system)


@pytest.mark.asyncio
async def test_natural_observer_projection_runtime_contract_with_real_gate_preparation_meme_and_master():
    nl = chr(10)
    quote = chr(34)
    slash = chr(92)
    master_style = 'MASTER_STYLE_T09_SENTINEL'
    character = 'CHARACTER_SITUATION_T09_SENTINEL'
    raw = '原始轮次消息' + quote + '引号' + nl + '第二行' + slash + '反斜杠'
    sender_id = 'sender' + quote + 'id' + nl + slash + '01'
    sender_name = '甲' + quote + '名' + nl + '下一行' + slash + 'x'
    addressee = '乙' + quote + '受话者' + slash + '路径'
    evidence = '原始轮次消息'
    config = cfg('on')
    identity = config['sys001'].setdefault('identity', {})
    identity['master_relationship_enabled'] = True
    identity['master_relationship_prompt'] = master_style
    dialogue = config['sys001']['character_dialogue']
    dialogue['enabled'] = True
    dialogue['situation_prompt'] = character
    config['sys001']['final_review']['mode'] = 'combined'
    plugin, provider = _plugin(
        _decision_text(attribution=_valid_attr('observer', addressee, evidence, facts=[])),
        prep='on',
        config=config,
    )
    event, original = _natural_event('t09-runtime-projection', raw)
    snapshot = replace(original, is_master=True, sender_id=sender_id, sender_name=sender_name)
    event.set_extra(_turn_key(), snapshot)
    assert await plugin._decide_natural_participation(event, snapshot) == 'REPLY'
    attribution = _attribution_for(event, snapshot)
    assert attribution is not None
    formatted = _format(attribution, snapshot)
    provider.completion = FORMAL
    event.set_extra('meme_manager_emotion_context_lines', ['old-meme-line'])
    event.set_extra('meme_manager_semantic_mode', 'llm')
    text_part = _required(agent_message, 'TextPart')
    old_non_temp = text_part(text='OLD_NON_TEMP_T09_CONTEXT')
    request, _, _ = req(rag_text='NON_RAG_OLD_TEMP', extra=(old_non_temp,))
    request.prompt = raw
    event_message = event.get_message_str()
    snapshot_text = snapshot.message_text
    model_context = snapshot.model_context()
    original_system = request.system_prompt
    original_contexts = request.contexts
    original_contexts_content = copy.deepcopy(original_contexts)
    old_parts = list(request.extra_user_content_parts)
    await plugin.attach_turn_and_project_capabilities(event, request)
    assert request.prompt == ''
    assert len(provider.calls) == 2
    assert raw in provider.calls[1]['prompt']
    assert '群内原话引用开始' not in provider.calls[1]['prompt']
    assert event.get_extra('meme_manager_emotion_context_lines') == [
        'old-meme-line',
        '当前用户消息（本轮选图以此为准）: ' + raw.strip(),
    ]
    parts = request.extra_user_content_parts
    assert len(parts) == len(old_parts) + 5
    assert all(parts[3 + index] is part for index, part in enumerate(old_parts))
    prefix, body, suffix = parts[:3]
    assert isinstance(prefix, text_part)
    assert isinstance(body, text_part)
    assert isinstance(suffix, text_part)
    assert getattr(prefix, '_no_save', False) is True
    assert getattr(body, '_no_save', False) is False
    assert getattr(suffix, '_no_save', False) is True
    assert body.text == raw
    assert sum(isinstance(part, text_part) and part.text == raw for part in parts) == 1
    assert not any(
        isinstance(part, text_part)
        and isinstance(getattr(part, 'text', None), str)
        and part.text.startswith('群内原话引用开始')
        for part in parts[3:]
    )
    assert prefix.text.startswith('群内原话引用开始')
    assert '真实发言者JSON=' in prefix.text
    assert '辅助原话受话者JSON=' in prefix.text
    sender_json = prefix.text.split('真实发言者JSON=', 1)[1].split('辅助原话受话者JSON=', 1)[0].strip()
    addressee_json = prefix.text.split('辅助原话受话者JSON=', 1)[1].strip()
    assert json.loads(sender_json) == {
        'sender_id': sender_id,
        'sender_name': sender_name,
        'is_master': True,
    }
    assert json.loads(addressee_json) == addressee
    assert suffix.text == '群内原话引用结束'
    frame = parts[-1]
    assert all(frame is not part for part in old_parts)
    assert frame.text == formatted
    assert getattr(frame, '_no_save', False) is True
    assert formatted not in (request.system_prompt or '')
    system = request.system_prompt
    assert system.startswith(original_system)
    assert master_style in system
    assert character in system
    assert request.contexts is original_contexts
    assert request.contexts == original_contexts_content
    assert event.get_message_str() == event_message
    assert event.get_extra(_turn_key()) is snapshot
    assert snapshot.message_text == snapshot_text
    assert snapshot.model_context() == model_context
    reference = event.get_extra(_review_key())
    assert isinstance(reference, str) and formatted in reference
    assert event.sent == []


@pytest.mark.asyncio
async def test_natural_observer_projection_matches_official_saved_shape_after_attach():
    nl = chr(10)
    quote = chr(34)
    slash = chr(92)
    raw = '持久化原文' + quote + '引号' + nl + '第二行' + slash + '路径'
    addressee = '持久化受话者' + quote + '名' + slash + '行'
    event, snapshot = _natural_event('t09-persist', raw)
    parsed = _parse(
        _decision_text(attribution=_valid_attr('observer', addressee, '持久化原文')),
        sources=(raw,),
    )
    attribution = _require_attr(parsed)
    text_part = _required(agent_message, 'TextPart')
    old_temp = text_part(text='OLD_TEMP_PERSISTENCE').mark_as_temp()
    old_non_temp = text_part(text='OLD_NON_TEMP_PERSISTENCE')
    request, _, _ = req(rag_text='NON_RAG_OLD_TEMP', extra=(old_temp, old_non_temp))
    request.prompt = raw
    formatted = _format(attribution, snapshot)
    request.extra_user_content_parts = [
        *request.extra_user_content_parts,
        text_part(text=formatted).mark_as_temp(),
    ]
    assert request.extra_user_content_parts[-1].text == formatted

    async def saved():
        assembled = await request.assemble_context()
        assert isinstance(assembled, dict)
        assert isinstance(assembled.get('content'), list)
        runtime = agent_message.bind_checkpoint_messages([*request.contexts, assembled])
        return agent_message.dump_messages_with_checkpoints(runtime)

    baseline = await saved()
    helper = _required(natural_context, '_project_natural_observer_request')
    assert helper(request, snapshot, attribution) is True
    assert request.prompt == ''
    after = await saved()
    assert after == baseline
    projected_parts = request.extra_user_content_parts
    projected_body = projected_parts[1]
    assert helper(request, snapshot, attribution) is False
    assert request.extra_user_content_parts is projected_parts
    assert request.extra_user_content_parts[1] is projected_body
    assert len(request.extra_user_content_parts) == len(projected_parts)
    assert await saved() == baseline
    assert isinstance(after, list)
    saved_user = next(
        message
        for message in after
        if isinstance(message, dict)
        and isinstance(message.get('content'), list)
        and any(
            isinstance(part, dict) and part.get('text') == raw
            for part in message['content']
        )
    )
    texts = [
        part.get('text')
        for part in saved_user['content']
        if isinstance(part, dict) and isinstance(part.get('text'), str)
    ]
    assert texts.count(raw) == 1
    assert texts.count('OLD_NON_TEMP_PERSISTENCE') == 1
    assert 'OLD_TEMP_PERSISTENCE' not in texts
    assert formatted not in texts
    assert not any('群内原话引用开始' in text for text in texts)
    assert not any('群内原话引用结束' in text for text in texts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'case',
    [
        'participant',
        'uncertain',
        'missing-attribution-legacy-reply',
        'invalid-attribution-reply-degrade',
        'same-equal-different-snapshot',
        'directed',
        'private',
        'empty-prompt',
        'none-prompt',
        'image-urls',
        'audio-urls',
        'prompt-prefix-mismatch',
    ],
)
async def test_natural_observer_projection_bypasses_ineligible_inputs(case):
    nl = chr(10)
    quote = chr(34)
    slash = chr(92)
    raw = '绕过原文' + quote + '引号' + nl + '第二行' + slash + '路径'
    addressee = '绕过受话者' + quote + '名' + slash + '行'
    config = cfg('off')
    config['sys001']['final_review']['mode'] = 'combined'
    perspective = 'observer'
    if case == 'participant':
        perspective = 'participant'
    elif case == 'uncertain':
        perspective = 'uncertain'
    completion = _decision_text(
        attribution=_valid_attr(perspective, addressee, '绕过原文', facts=[])
    )
    if case == 'missing-attribution-legacy-reply':
        completion = _decision_text('REPLY')
    elif case == 'invalid-attribution-reply-degrade':
        completion = _decision_text(attribution={
            'perspective': 'observer',
            'addressee': 3,
            'evidence': ['bad'],
            'facts': 'bad',
        })
    plugin, provider = _plugin(completion, prep='off', config=config)
    event, original = _natural_event('t09-bypass-' + case, raw)
    assert await plugin._decide_natural_participation(event, original) == 'REPLY'
    snapshot = original
    if case == 'same-equal-different-snapshot':
        assert _attribution_for(event, original) is not None
        snapshot = replace(original)
        event.set_extra(_turn_key(), snapshot)
        assert _attribution_for(event, snapshot) is None
    elif case == 'directed':
        assert _attribution_for(event, original) is not None
        snapshot = replace(original, origin='directed')
        event.set_extra(_turn_key(), snapshot)
        assert _attribution_for(event, snapshot) is None
    elif case == 'private':
        assert _attribution_for(event, original) is not None
        snapshot = replace(original, is_private=True)
        event.set_extra(_turn_key(), snapshot)
        assert _attribution_for(event, snapshot) is None

    current_attr = _attribution_for(event, snapshot)
    if case in {
        'missing-attribution-legacy-reply',
        'invalid-attribution-reply-degrade',
        'same-equal-different-snapshot',
        'directed',
        'private',
    }:
        assert current_attr is None

    text_part = _required(agent_message, 'TextPart')
    old_non_temp = text_part(text='OLD_NON_TEMP_BYPASS')
    request, _, _ = req(rag_text='NON_RAG_OLD_TEMP', extra=(old_non_temp,))
    request.prompt = raw
    if case == 'empty-prompt':
        request.prompt = ''
    elif case == 'none-prompt':
        request.prompt = None
    elif case == 'image-urls':
        request.image_urls = ['https://example.invalid/t09.png']
    elif case == 'audio-urls':
        request.audio_urls = ['https://example.invalid/t09.wav']
    elif case == 'prompt-prefix-mismatch':
        request.prompt = 'T09_PREFIX_' + raw
    before_prompt = request.prompt
    old_parts = list(request.extra_user_content_parts)
    await plugin.attach_turn_and_project_capabilities(event, request)
    if before_prompt is None:
        assert request.prompt is None
    else:
        assert request.prompt == before_prompt
    parts = request.extra_user_content_parts
    assert len(parts) >= len(old_parts)
    assert all(parts[index] is old_parts[index] for index in range(len(old_parts)))
    if current_attr is None:
        assert len(parts) == len(old_parts)
    else:
        assert len(parts) == len(old_parts) + 1
        frame = parts[-1]
        assert frame.text == _format(current_attr, snapshot)
        assert getattr(frame, '_no_save', False) is True
    assert not any(
        isinstance(part, text_part)
        and isinstance(getattr(part, 'text', None), str)
        and part.text.startswith('群内原话引用开始')
        for part in parts
    )
    assert isinstance(event.sent, list)
    assert event.sent == []


@pytest.mark.parametrize('variant', ['valid', 'invalid', 'controls'])
def test_source_refs_parser_contract(variant):
    parser = _required(sys001, 'parse_natural_participation_decision')
    parameters = inspect.signature(parser).parameters
    assert 'sources' in parameters
    assert 'source_refs' in parameters
    assert parameters['source_refs'].default is None
    assert parameters['source_refs'].kind is inspect.Parameter.KEYWORD_ONLY
    sources = ('记录：小明说你好。', '补充：乙点头。')
    refs = {'S0': '小明说你好', 'S1': '乙点头'}

    def attr_for(evidence, fact_evidence=_MISSING):
        facts = []
        if fact_evidence is not _MISSING:
            facts = [{'subject': '小明', 'action': '说你好', 'evidence': fact_evidence}]
        return {
            'perspective': 'observer',
            'addressee': '小明',
            'evidence': evidence,
            'facts': facts,
        }

    def parse_attr(attr, *, mapping=refs, source_texts=sources):
        attr_before = copy.deepcopy(attr)
        map_before = copy.deepcopy(mapping)
        result = parser(
            _decision_text(attribution=attr),
            sources=source_texts,
            source_refs=mapping,
        )
        assert attr == attr_before
        assert mapping == map_before
        return result

    if variant == 'valid':
        for top, fact_obj, expected_top, expected_fact in (
            ({'source_id': 'S0'}, {'source_id': 'S0'}, '小明说你好', '小明说你好'),
            ({'source_id': 'S1'}, '乙点头', '乙点头', '乙点头'),
            ('小明说你好', {'source_id': 'S1'}, '小明说你好', '乙点头'),
        ):
            value = _require_attr(parse_attr(attr_for(top, fact_obj)))
            assert value.evidence == expected_top
            assert isinstance(value.evidence, str)
            assert value.facts[0].evidence == expected_fact
            assert isinstance(value.facts[0].evidence, str)
        legacy = attr_for('小明说你好', '乙点头')
        value = _require_attr(parser(_decision_text(attribution=legacy), sources=sources))
        assert value.evidence == '小明说你好'
        assert value.facts[0].evidence == '乙点头'
        one, boundary = '甲', '边' * 300
        value = _require_attr(parser(
            _decision_text(attribution=attr_for({'source_id': 'S1'}, {'source_id': 'S0'})),
            sources=(f'前{one}后', f'前{boundary}后'),
            source_refs={'S0': one, 'S1': boundary},
        ))
        assert value.evidence == boundary
        assert value.facts[0].evidence == one
    elif variant == 'invalid':
        for evidence, mapping, source_texts in (
            ({'source_id': 'S0', 'extra': 1}, refs, sources),
            ({}, refs, sources),
            ({'source_id': 0}, refs, sources),
            ({'source_id': 'S9'}, refs, sources),
            ({'source_id': 's0'}, refs, sources),
            ({'source_id': 'S0'}, None, sources),
            ({'source_id': 'S0'}, {'S0': 1}, sources),
            ({'source_id': 'S0'}, {'S0': '未出现'}, sources),
        ):
            _assert_decision_without_attr(
                parse_attr(attr_for(evidence), mapping=mapping, source_texts=source_texts),
                'REPLY',
            )
        for mapped in ('', '   ', 'a' + chr(10) + 'b', 'a' + chr(1) + 'b', '长' * 301):
            _assert_decision_without_attr(
                parse_attr(
                    attr_for({'source_id': 'S0'}),
                    mapping={'S0': mapped},
                    source_texts=(f'前{mapped}后',),
                ),
                'REPLY',
            )
        for fact_evidence, mapping, source_texts in (
            ({'source_id': 'S0', 'extra': 1}, refs, sources),
            ({'source_id': 'S9'}, refs, sources),
            ({'source_id': 'S0'}, {'S0': 1}, sources),
            ({'source_id': 'S0'}, {'S0': '未出现'}, sources),
        ):
            _assert_decision_without_attr(
                parse_attr(
                    attr_for('小明说你好', fact_evidence),
                    mapping=mapping,
                    source_texts=source_texts,
                ),
                'REPLY',
            )
        for mapped in ('', '   ', 'a' + chr(10) + 'b', 'a' + chr(1) + 'b', '长' * 301):
            _assert_decision_without_attr(
                parse_attr(
                    attr_for('小明说你好', {'source_id': 'S0'}),
                    mapping={'S0': mapped},
                    source_texts=('记录：小明说你好。', f'前{mapped}后'),
                ),
                'REPLY',
            )
    else:
        valid_attr = attr_for({'source_id': 'S0'}, {'source_id': 'S1'})
        for decision in ('WAIT', 'NO_ACTION'):
            assert parser(
                _decision_text(decision, valid_attr),
                sources=sources,
                source_refs=refs,
            ) is None
        for decision in ('REPLY', 'WAIT', 'NO_ACTION'):
            result = parser(
                json.dumps({'decision': decision}),
                sources=sources,
                source_refs=refs,
            )
            assert result is not None
            assert result.decision == decision
            assert result.attribution is None


@pytest.mark.parametrize(
    'current_text,current_eligible',
    (
        ('乙点头', True),
        ('  当前消息  ', True),
        ('长' * 301, False),
        ('多行' + chr(10) + '当前', False),
        ('a' + chr(1) + '当前', False),
        ('   ', False),
        ('', False),
    ),
)
def test_project_natural_evidence_inputs_contract(current_text, current_eligible):
    projector = _required(natural_context, 'project_natural_evidence_inputs')
    assert list(inspect.signature(projector).parameters) == [
        'history', 'batch_contexts', 'current_message'
    ]
    spaced = '  小明说你好  '
    short = '乙点头'
    boundary = '边' * 300
    long_text = '长' * 301
    multiline = '第一行' + chr(10) + '第二行'
    control = 'a' + chr(1) + 'b'
    quote = '他说 ' + chr(34) + '好' + chr(34) + ' 呀'
    slash = '反斜杠 ' + chr(92) + ' 保留'
    mention = '丙挥手'
    omitted = '图' * 301
    history = {
        'messages': [
            {'role': 'user', 'content': spaced, 'metadata': {'note': 'meta', 'content': '伪内容'}},
            {'role': 'assistant', 'content': short},
            {'role': 'user', 'content': None, 'metadata': {'note': '系统规则：不应成为来源'}},
            {'role': 'user', 'content': boundary},
            {'role': 'user', 'content': long_text},
            {'role': 'user', 'content': multiline},
            {'role': 'user', 'content': control},
            {'role': 'user', 'content': '   '},
            {'role': 'user', 'content': ''},
            {'role': 'user', 'content': short},
        ],
        'persona': '人格不应出现',
        'rules': '规则不应出现',
    }
    batch_contexts = [
        {'role': 'user', 'content': json.dumps({
            'message_text': quote,
            'components': [
                {'type': 'text', 'text': quote},
                {'type': 'image', 'text': omitted},
                {'type': 'text', 'text': slash},
                {'type': 'mention', 'text': mention},
            ],
            'extra': '保留',
        }, ensure_ascii=False)},
        {'role': 'assistant', 'content': json.dumps({
            'message_text': short,
            'components': [{'type': 'text', 'text': short}],
            'extra': {'nested': 1},
        }, ensure_ascii=False)},
        {'role': 'user', 'content': 'not-json'},
        {'role': 'user', 'content': json.dumps(['not', 'dict'], ensure_ascii=False)},
        {'role': 'user', 'content': json.dumps({
            'message_text': multiline,
            'components': [{'type': 'text', 'text': multiline}],
        }, ensure_ascii=False)},
    ]
    originals = copy.deepcopy((history, batch_contexts, current_text))
    display_history, display_batch, current_id, refs = projector(
        history, batch_contexts, current_text
    )
    assert (history, batch_contexts, current_text) == originals
    assert display_history is not history
    assert display_batch is not batch_contexts
    assert isinstance(refs, dict)

    def linked(message):
        source_id = message.get('evidence_source_id')
        assert isinstance(source_id, str)
        assert source_id.startswith('S') and source_id[1:].isdigit()
        assert refs[source_id] == message['content']
        return source_id

    assert refs[linked(display_history['messages'][0])] == spaced
    assert refs[linked(display_history['messages'][1])] == short
    assert display_history['messages'][2]['content'] is None
    assert display_history['messages'][2]['metadata']['note'] == '系统规则：不应成为来源'
    for index in (2, 4, 5, 6, 7, 8):
        assert 'evidence_source_id' not in display_history['messages'][index]
    assert refs[linked(display_history['messages'][3])] == boundary
    assert refs[linked(display_history['messages'][9])] == short
    assert display_history['messages'][4]['content'] == long_text
    assert display_history['messages'][5]['content'] == multiline
    assert display_history['messages'][6]['content'] == control

    payload0 = json.loads(display_batch[0]['content'])
    assert payload0['message_text'] == quote
    assert refs[payload0['message_text_evidence_source_id']] == quote
    assert payload0['components'][0]['text'] == quote
    assert refs[payload0['components'][0]['evidence_source_id']] == quote
    assert 'evidence_source_id' not in payload0['components'][1]
    assert payload0['components'][1]['text'] == omitted
    assert refs[payload0['components'][2]['evidence_source_id']] == slash
    assert refs[payload0['components'][3]['evidence_source_id']] == mention
    assert payload0['extra'] == '保留'

    payload1 = json.loads(display_batch[1]['content'])
    assert refs[payload1['message_text_evidence_source_id']] == short
    assert refs[payload1['components'][0]['evidence_source_id']] == short
    assert payload1['extra'] == {'nested': 1}
    assert display_batch[2]['content'] == 'not-json'
    assert json.loads(display_batch[3]['content']) == ['not', 'dict']
    payload4 = json.loads(display_batch[4]['content'])
    assert payload4['message_text'] == multiline
    assert 'message_text_evidence_source_id' not in payload4
    assert 'evidence_source_id' not in payload4['components'][0]

    if current_eligible:
        assert isinstance(current_id, str)
        assert refs[current_id] == current_text
    else:
        assert current_id is None
        assert current_text not in refs.values()

    allowed = {spaced, short, boundary, quote, slash, mention}
    if current_eligible:
        allowed.add(current_text)
    assert set(refs.values()) <= allowed
    assert long_text not in refs.values()
    assert long_text[:300] not in refs.values()
    assert multiline not in refs.values()
    assert '第一行' not in refs.values()
    assert control not in refs.values()
    assert '系统规则：不应成为来源' not in refs.values()
    assert '人格不应出现' not in refs.values()
    assert 'meta' not in refs.values()
    assert '保留' not in refs.values()
    assert 'image' not in refs.values()

    def strip_history(value):
        cleaned = copy.deepcopy(value)
        for message in cleaned['messages']:
            message.pop('evidence_source_id', None)
        return cleaned

    def strip_batch(value):
        cleaned = copy.deepcopy(value)
        for context in cleaned:
            raw = context.get('content')
            if not isinstance(raw, str):
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            payload.pop('message_text_evidence_source_id', None)
            components = payload.get('components')
            if isinstance(components, list):
                for component in components:
                    if isinstance(component, dict):
                        component.pop('evidence_source_id', None)
            context['content'] = json.dumps(payload, ensure_ascii=False)
        return cleaned

    assert set(display_history) == set(history)
    assert strip_history(display_history) == history
    assert strip_batch(display_batch) == batch_contexts
    blob = json.dumps(display_history, ensure_ascii=False) + json.dumps(display_batch, ensure_ascii=False)
    assert 'refs' not in blob
    assert 'source_body' not in blob


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['current', 'history'])
async def test_source_id_gate_uses_projected_original_evidence(kind):
    parser = _required(sys001, 'parse_natural_participation_decision')
    parameters = inspect.signature(parser).parameters
    assert 'source_refs' in parameters
    assert parameters['source_refs'].default is None
    assert callable(_required(natural_context, 'project_natural_evidence_inputs'))
    if kind == 'current':
        expected = '乙点头'
        event, snapshot = _natural_event('t10-current', expected)
        attr_payload = {
            'perspective': 'observer',
            'addressee': '乙',
            'evidence': {'source_id': 'S0'},
            'facts': [{'subject': '乙', 'action': '点头', 'evidence': {'source_id': 'S0'}}],
        }
    else:
        expected = '乙说你好'
        current = '长' * 301
        event, snapshot = _natural_event('t10-history', current)
        event.set_extra(_context_key(), {'status': 'available', 'records': (('h1', expected),)})
        attr_payload = {
            'perspective': 'observer',
            'addressee': '乙',
            'evidence': {'source_id': 'S0'},
            'facts': [{'subject': '乙', 'action': '说你好', 'evidence': {'source_id': 'S0'}}],
        }
    plugin, provider = _plugin(_decision_text(attribution=attr_payload), config=cfg('off'))
    if kind == 'history':
        _set_group_icl(plugin, True)
    snapshot_before = snapshot
    snapshot_text = snapshot.message_text
    raw_event_text = event.get_message_str()
    request, _, _ = req()
    request.prompt = snapshot.message_text
    contexts = request.contexts
    contexts_before = copy.deepcopy(contexts)
    assert await plugin._decide_natural_participation(event, snapshot) == 'REPLY'
    calls = _calls(provider)
    assert len(calls) == 1
    prompt = calls[0]['prompt']
    assert snapshot.message_text in prompt
    assert 'SOURCE_BODY' not in prompt
    assert 'source_refs' not in prompt
    if kind == 'current':
        assert prompt.count(expected) == 1
        assert 'MESSAGE_EVIDENCE_SOURCE_ID=' + json.dumps('S0') in prompt
        assert 'MESSAGE_EVIDENCE_SOURCE_ID=null' not in prompt
    else:
        assert len(current) > 300
        assert prompt.count(current) == 1
        assert 'MESSAGE_EVIDENCE_SOURCE_ID=null' in prompt
        marker = 'RECENT_CONTEXT='
        start = prompt.index(marker) + len(marker)
        end = prompt.index(chr(10), start)
        recent = json.loads(prompt[start:end])
        messages = recent.get('messages') if isinstance(recent, dict) else None
        assert isinstance(messages, list)
        matches = [message for message in messages if isinstance(message, dict) and message.get('content') == expected]
        assert len(matches) == 1
        assert matches[0].get('evidence_source_id') == 'S0'
    tail = prompt[prompt.index('CURRENT_OUTPUT_CONTRACT'):]
    assert 'source_id' in tail
    assert any(marker in tail.lower() or marker in tail for marker in ('legacy', 'literal', 'string', '旧', '原', '字符串', '文本'))

    value = _attribution_for(event, snapshot)
    assert value is not None
    assert isinstance(value.evidence, str) and value.evidence == expected
    assert isinstance(value.facts[0].evidence, str) and value.facts[0].evidence == expected
    assert event.get_extra(natural_context.NATURAL_ATTRIBUTION_EXTRA) is not None
    equal_snapshot = replace(snapshot)
    assert equal_snapshot == snapshot
    assert _attribution_for(event, equal_snapshot) is None

    formatted = _format(value, snapshot=snapshot)
    final = json.loads(formatted.rsplit(chr(10), 1)[-1])
    assert final['evidence'] == expected
    assert final['facts'][0]['evidence'] == expected
    before_parts = list(request.extra_user_content_parts)
    calls_before = len(_calls(provider))
    await plugin.attach_turn_and_project_capabilities(event, request)
    parts = list(request.extra_user_content_parts)
    assert len(parts) == len(before_parts) + 4
    assert parts[3:-1] == before_parts
    assert parts[-1].text == formatted
    assert getattr(parts[-1], '_no_save', False) is True
    assert len(_calls(provider)) == calls_before
    assert snapshot is snapshot_before
    assert snapshot.message_text == snapshot_text
    assert event.get_message_str() == raw_event_text
    assert event.get_extra(_turn_key()) is snapshot
    assert request.contexts is contexts
    assert request.contexts == contexts_before

    fallback_current = '长' * 301
    fallback_event, fallback_snapshot = _natural_event('t10-no-map-' + kind, fallback_current)
    fallback_plugin, fallback_provider = _plugin(
        _decision_text(attribution=attr_payload), config=cfg('off')
    )
    assert await fallback_plugin._decide_natural_participation(
        fallback_event, fallback_snapshot
    ) == 'REPLY'
    fallback_calls = _calls(fallback_provider)
    assert len(fallback_calls) == 1
    assert fallback_calls[0]['prompt'].count(fallback_current) == 1
    assert 'MESSAGE_EVIDENCE_SOURCE_ID=null' in fallback_calls[0]['prompt']
    assert _attribution_for(fallback_event, fallback_snapshot) is None
    assert fallback_event.get_extra(natural_context.NATURAL_ATTRIBUTION_EXTRA) is None

    fallback_provider.completion = _decision_text(attribution={
        'perspective': 'observer',
        'addressee': '乙',
        'evidence': '长',
        'facts': [],
    })
    assert await fallback_plugin._decide_natural_participation(
        fallback_event, fallback_snapshot
    ) == 'REPLY'
    fallback_calls = _calls(fallback_provider)
    assert len(fallback_calls) == 2
    assert fallback_calls[1]['prompt'].count(fallback_current) == 1
    assert 'MESSAGE_EVIDENCE_SOURCE_ID=null' in fallback_calls[1]['prompt']
    fallback_value = _attribution_for(fallback_event, fallback_snapshot)
    assert fallback_value is not None
    assert fallback_value.evidence == '长'
    assert fallback_value.facts == ()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['terminate', 'cancel'])
async def test_late_or_cancelled_source_id_gate_never_binds(mode):
    parser = _required(sys001, 'parse_natural_participation_decision')
    assert 'source_refs' in inspect.signature(parser).parameters
    assert callable(_required(natural_context, 'project_natural_evidence_inputs'))
    attr_payload = {
        'perspective': 'observer',
        'addressee': '乙',
        'evidence': {'source_id': 'S0'},
        'facts': [{'subject': '乙', 'action': '点头', 'evidence': {'source_id': 'S0'}}],
    }
    provider = _HeldProvider(_decision_text(attribution=attr_payload))
    plugin = mk(provider, config=cfg('off'))
    event, snapshot = _natural_event('late-source-' + mode, '乙点头')
    request, _, _ = req()
    request.prompt = snapshot.message_text
    task = asyncio.create_task(plugin._decide_natural_participation(event, snapshot))
    try:
        await asyncio.wait_for(provider.started.wait(), 1)
        if mode == 'terminate':
            plugin._auxiliary_terminated = True
            provider.release.set()
            assert await asyncio.wait_for(task, 1) == ''
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    assert provider.started.is_set()
    assert _attribution_for(event, snapshot) is None
    assert event.get_extra(natural_context.NATURAL_ATTRIBUTION_EXTRA) is None
    old_parts = list(request.extra_user_content_parts)
    await plugin.attach_turn_and_project_capabilities(event, request)
    parts = list(request.extra_user_content_parts)
    assert len(parts) == len(old_parts)
    assert all(current is old for current, old in zip(parts, old_parts))
