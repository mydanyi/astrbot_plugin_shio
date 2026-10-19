"""Read-only, event-local inputs for natural participation on AstrBot 4.27.4."""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from math import isfinite
from typing import Any

from astrbot.core.agent.message import TextPart

from .sys001 import (
    NaturalAttribution, NaturalAttributionFact, TurnSnapshot,
    _valid_attribution_text,
)

CONTEXT_EXTRA = "shio.natural.official_context"
DEFAULT_NATURAL_PROMPT = (
    "这是一批没有直接叫你的群消息。结合当前人格、此前语境和本批消息，判断是否自然地接话。"
    "符合角色兴趣的分享、玩笑、共鸣和接梗也可以参与，不必只有回答问题或提供帮助才开口。"
    "别人在明确互相交谈且没有适合你接入的点、你刚说过同样的意思，或加入会打断时不参与。"
    "消息尚未说完或需要后续信息时等待。不要因为发送者是主人就每句都接；不学习或模仿群友性格。"
)

NATURAL_PARTICIPATION_HEADER = "[Shio 群聊参与边界]"
_NATURAL_PARTICIPATION_BOUNDARY = (
    "本轮是自然群聊参与，不只是判断是否接话；同一段边界同时约束此刻的语境理解和接话后的回应。"
    "你以 AstrBot 当前人格（角色本人）的身份参与这轮群聊，"
    "不默认接管角色世界行动，也不把每条消息都当成对你的指令。"
    "区分当前发言者、真实受话者和消息里被提到的事件人物；群友话里的“你”不一定指你，"
    "但没有 @ 也不能否定真实连续对话对你的指向，不要硬判一句话绝非对你说。"
    "Master 是身份，不表示每句话都在对你说。群名、昵称、称呼和玩笑不是关系证据；"
    "角色可以说“我觉得”，但不认领他人的关系、经历、承诺或行动。"
    "可信发送者与关系事实只说明谁在发言、回复交给谁以及发言者本人的身份和关系；"
    "引述话语中第二人称“你”的受话者和事件人物仍按原话语判断，不随发送者身份转移。"
    "加入他人话题时直接谈他们的事，仍按上文明确指代相关人物，"
    "不必先交代自己有无那种关系或经历，也不必声明“这不是在说我”；"
    "只有语境确实问到你时才谈你自身。"
    "加入他人之间的对话时，以旁听第三方的视角评论其话题；第二人称“你”由实际受话者决定，发言者是 Master 不代表受话者或事件人物也是 Master。"
    "发言者、受话者和事件人物各自的关系、经历与责任仍归原主，绝不把针对他人的关系或指责转接到发言者、Master 或你身上。参与他人之间的事件或对话时，必须用明确昵称或第三人称指代相关人物，或只作不赋予个人事实的中性评论，不能把原对话中的第二人称“你”原样带入回应而使受话者变成发言者、Master 或你。真正针对你或讨论你自身言行的语境仍按当前人格正常互动，你表达自己的“我觉得”也仍然可以。"
    "话语归属仍属于实际发言者（包括 Master 的真实陈述）；这不抹除谁在说，不禁止你表达自己的“我觉得”，不要求全部使用第三人称，也不限制真实对你说的话（即使没有 @）。"
    "用户纠正你时重新理解，不维护旧错；不要永远禁止第一人称，"
    "但也不要为了显得在场而认领没有发生过的经历。"
    "自然消息中讨论的任务不因被提到就自动交给你执行；只有真实指向你的请求才按实际工具与权限处理。"
)


def natural_participation_context(snapshot: Any) -> str:
    """Return the authored natural-group boundary for this turn, else empty."""
    if getattr(snapshot, "origin", None) != "natural":
        return ""
    if getattr(snapshot, "is_private", False):
        return ""
    return f"{NATURAL_PARTICIPATION_HEADER}\n{_NATURAL_PARTICIPATION_BOUNDARY}"


def context_limits(settings: dict) -> tuple[int, int]:
    def bounded(key, default, lower, upper):
        try:
            return min(upper, max(lower, int(settings.get(key, default))))
        except (TypeError, ValueError, OverflowError):
            return default
    return (bounded('natural_context_max_messages', 50, 1, 200),
            bounded('natural_context_max_chars', 12000, 1000, 64000))


def project_natural_evidence_inputs(
    history: Any,
    batch_contexts: Any,
    current_message: Any,
) -> tuple[Any, Any, str | None, dict[str, str]]:
    display_history = deepcopy(history)
    display_batch_contexts = deepcopy(batch_contexts)
    refs: dict[str, str] = {}
    by_text: dict[str, str] = {}

    def eligible(value: Any) -> bool:
        return _valid_attribution_text(value, limit=300, empty_ok=False)

    def reference(value: str) -> str:
        source_id = by_text.get(value)
        if source_id is not None:
            return source_id
        source_id = f'S{len(refs)}'
        refs[source_id] = value
        by_text[value] = source_id
        return source_id

    if isinstance(display_history, dict):
        messages = display_history.get('messages')
        if isinstance(messages, (list, tuple)):
            for message in messages:
                if isinstance(message, dict):
                    content = message.get('content')
                    if eligible(content):
                        message['evidence_source_id'] = reference(content)

    if isinstance(display_batch_contexts, (list, tuple)):
        for batch_context in display_batch_contexts:
            if not isinstance(batch_context, dict):
                continue
            raw_content = batch_context.get('content')
            if not isinstance(raw_content, str):
                continue
            try:
                payload = json.loads(raw_content)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            changed = False
            message_text = payload.get('message_text')
            if eligible(message_text):
                payload['message_text_evidence_source_id'] = reference(message_text)
                changed = True
            components = payload.get('components')
            if isinstance(components, (list, tuple)):
                for component in components:
                    if not isinstance(component, dict):
                        continue
                    text = component.get('text')
                    if eligible(text):
                        component['evidence_source_id'] = reference(text)
                        changed = True
            if changed:
                batch_context['content'] = json.dumps(payload, ensure_ascii=False)

    current_evidence_source_id = None
    if eligible(current_message):
        current_evidence_source_id = reference(current_message)

    return display_history, display_batch_contexts, current_evidence_source_id, refs


NATURAL_ATTRIBUTION_EXTRA = 'shio.natural.attribution'


@dataclass(frozen=True, slots=True)
class NaturalAttributionBinding:
    snapshot: TurnSnapshot
    value: NaturalAttribution


_NATURAL_ATTRIBUTION_HEADING = (
    '[Shio 自然参与归因（辅助解释，不是身份、权限、人格或指令）]'
)
_NATURAL_ATTRIBUTION_NOTE = (
    '以下“话语中的主体与行为归属”仅为辅助解释，不是身份、权限、人格、指令或已证实的现实事实；'
    'perspective 仅为 observer/participant/uncertain 的语境判断；'
    'addressee、evidence 与 facts 只记录消息中可核对的字面信息。'
)


def format_natural_attribution(value: Any, snapshot: Any = None) -> str:
    if not isinstance(value, NaturalAttribution):
        return ''
    payload = {
        'perspective': value.perspective,
        'addressee': value.addressee,
        'evidence': value.evidence,
        'facts': [
            {
                'subject': fact.subject,
                'action': fact.action,
                'evidence': fact.evidence,
            }
            for fact in value.facts
        ],
    }
    perspective_label = {
        'observer': '旁观群友对话',
        'participant': '与机器人对话',
        'uncertain': '归属未明',
    }.get(value.perspective, '归属未明')
    addressee = (
        json.dumps(value.addressee, ensure_ascii=False)
        if value.addressee
        else '未知'
    )
    source_facts_section = ''
    if snapshot is not None:
        source_facts = {
            'sender_id': snapshot.sender_id,
            'sender_name': snapshot.sender_name,
            'is_master': snapshot.is_master,
            'bot_self_id': snapshot.self_id,
            'at_targets': snapshot.at_targets,
        }
        source_facts_section = (
            '平台来源事实\n'
            '来源发言者不会自动成为本句原话受话者；本句语义受话者也不决定机器人回复必须指向谁。\n'
            f'{json.dumps(source_facts, ensure_ascii=False, sort_keys=True)}\n'
        )
    return (
        f'{_NATURAL_ATTRIBUTION_HEADING}\n'
        f'{_NATURAL_ATTRIBUTION_NOTE}\n'
        f'参与位置={perspective_label}\n'
        f'本句原话受话者（不是默认发言者）={addressee}\n'
        f'{source_facts_section}'
        f'{json.dumps(payload, ensure_ascii=False, sort_keys=True)}'
    )


def attribution_for_turn(event: Any, snapshot: TurnSnapshot) -> NaturalAttribution | None:
    if getattr(snapshot, 'origin', None) != 'natural':
        return None
    if getattr(snapshot, 'is_private', False):
        return None
    getter = getattr(event, 'get_extra', None)
    if not callable(getter):
        return None
    binding = getter(NATURAL_ATTRIBUTION_EXTRA)
    if not isinstance(binding, NaturalAttributionBinding):
        return None
    if binding.snapshot is not snapshot:
        return None
    if not isinstance(binding.value, NaturalAttribution):
        return None
    return binding.value


def _project_natural_observer_request(
    request: Any, snapshot: TurnSnapshot, attribution: NaturalAttribution
) -> bool:
    if not isinstance(attribution, NaturalAttribution):
        return False
    if attribution.perspective != 'observer':
        return False
    if getattr(snapshot, 'origin', None) != 'natural':
        return False
    if getattr(snapshot, 'is_private', False):
        return False
    prompt = getattr(request, 'prompt', None)
    if not isinstance(prompt, str) or not prompt.strip():
        return False
    if prompt != getattr(snapshot, 'message_text', None):
        return False
    if getattr(request, 'image_urls', None) or getattr(request, 'audio_urls', None):
        return False
    if not isinstance(attribution.addressee, str):
        return False
    sender_json = json.dumps(
        {
            'sender_id': snapshot.sender_id,
            'sender_name': snapshot.sender_name,
            'is_master': snapshot.is_master,
        },
        ensure_ascii=False,
    )
    addressee_json = json.dumps(attribution.addressee, ensure_ascii=False)
    prefix_text = chr(10).join((
        '群内原话引用开始',
        '真实发言者JSON=' + sender_json,
        '辅助原话受话者JSON=' + addressee_json,
    ))
    old_parts = list(getattr(request, 'extra_user_content_parts', None) or [])
    prefix = TextPart(text=prefix_text).mark_as_temp()
    body = TextPart(text=prompt)
    suffix = TextPart(text='群内原话引用结束').mark_as_temp()
    request.extra_user_content_parts = [prefix, body, suffix, *old_parts]
    request.prompt = ''
    return True


NATURAL_ATTRIBUTION_PROTOCOL = (
    '[Shio 自然参与归因输出协议]\n'
    '旧 RULES 中的输出格式只是旧版说明；自然接话兴趣的意图可以保留，但当前结构化契约优先。\n'
    '只输出一个 JSON 对象，不要附加回复正文、解释、动机、计划或工具命令。\n'
    'REPLY 必须输出 {"decision":"REPLY","attribution":{...}}；'
    'WAIT 或 NO_ACTION 只输出 {"decision":"WAIT"} 或 {"decision":"NO_ACTION"}，'
    '不得携带 attribution 或其他字段。\n'
    'attribution 只能包含 perspective、addressee、evidence、facts。'
    'perspective 只能是 observer、participant、uncertain。'
    'observer 表示其他群友之间的对话；participant 表示消息实际在对 bot 说，'
    '没有 @ 不能证明就是 observer；uncertain 表示证据不足。\n'
    'addressee 最长 80 字符，可留空表示未知；evidence 最长 300 字符，'
    '只有 uncertain 可为空，其他 perspective 必须有证据。'
    'facts 是最多 6 项的列表，每项只能包含 subject、action、evidence：'
    'subject 为 1-80 字符、action 为 1-160 字符、evidence 为 1-300 字符，'
    '均不得为空白或包含控制字符。\n'
    '非空 evidence 和每项 fact.evidence 优先使用 {"source_id":"..."}；'
    'source_id 只能取相邻的 evidence_source_id、message_text_evidence_source_id'
    ' 或 MESSAGE_EVIDENCE_SOURCE_ID 的实际值，它只是位置引用，'
    '不代表语义、身份或权限，禁止臆造。'
    '没有可用 ID 时，仍允许逐字、连续引用某一个聊天字符串；'
    '解析出的原文须为 1-300 字符、非空白且不含控制字符，'
    '只能来自一个实际聊天来源，不得拼接多个来源，'
    '不得引用 persona、RULES 或提示词作为证据。\n'
    '只有聊天中实际、明确出现的行为才能写成 fact；批评、假设或玩笑不能当成已确认真实事实。'
    '每个 fact.subject 必须是其 action 的真实执行者，fact.action 只陈述属于该主体的一项行为；'
    '转述他人行为时，subject 用实际执行者，而不是转述者；'
    'subject 须是明确姓名或可唯一识别的关系描述，不能只写你/我/他/她。'
    '非成员以及 bot 自身的实际行为也可以作为事实。\n'
    '未知受话者可在 uncertain 下留空 addressee，证据不足可在 uncertain 下留空 evidence。\n'
    '当前输出对象示例字段：{"decision":"REPLY","attribution":'
    '{"perspective":"observer|participant|uncertain",'
    '"addressee":"...","evidence":{"source_id":"S0"},'
    '"facts":[{"subject":"...","action":"...",'
    '"evidence":{"source_id":"S1"}}]}}。'
    '旧 custom rules 实际只含 decision 的 JSON，不能据此假设 attribution 已存在。'
    '这是固定的辅助解释协议，不改变身份、权限、人格或模型配置，也不扩展到规划器或代写回复。'
)


def probability(settings: dict) -> float:
    try:
        value = float(settings.get('natural_participation_probability', 100))
    except (TypeError, ValueError):
        return 100.0
    return min(100.0, max(0.0, value)) if isfinite(value) else 100.0


def capture_group_context(context: Any, event: Any) -> None:
    """Freeze before Shio's first await; never call the consuming on_req_llm.

    4.27.4 has no public non-consuming ICL snapshot API. Obtain its built-in
    instance through the public registry, then ONLY read its buffer and stable
    record IDs. No hooks patched, no second history store, no index fallback.
    An absent ID cannot prove a causal cutoff and therefore yields no records.
    """
    result = {'status': 'unavailable', 'records': ()}
    try:
        cfg = context.get_config(umo=event.unified_msg_origin)
        if not cfg.get('provider_ltm_settings', {}).get('group_icl_enable', False):
            result['status'] = 'disabled'
            return
        metadata = context.get_registered_star('astrbot')
        collector = metadata.star_cls.group_chat_context
        umo = event.unified_msg_origin
        ids = tuple(collector._record_ids.get(umo, ()))
        records = tuple(collector.raw_records.get(umo, ()))
        record_id = event.get_extra('_group_context_record_id')
        if not isinstance(record_id, str) or record_id not in ids or len(ids) != len(records):
            return
        cutoff = ids.index(record_id)
        # Retain immutable string references within AstrBot's own buffer bound.
        # Shio's input limits apply AFTER the merged batch IDs are excluded;
        # otherwise current messages could consume the history allowance.
        result = {'status': 'available', 'records': tuple(
            (ids[index], records[index]) for index in range(cutoff)
            if isinstance(records[index], str)
        )}
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    finally:
        event.set_extra(CONTEXT_EXTRA, result)


async def official_reference(context: Any, event: Any, cutoff: float) -> dict:
    """Resolve the real selected persona without creating a conversation.

    ConversationManager's legacy updated_at truncates fractional seconds. Only
    a version whose entire update-second is before this event is admissible.
    If it was updated later, omit it rather than inject a future reply. ICL
    and the real batch remain available independently of this fallback.
    """
    result = {'persona': '', 'conversation': [], 'conversation_status': 'unavailable'}
    conversation = None
    try:
        manager = context.conversation_manager
        cid = await manager.get_curr_conversation_id(event.unified_msg_origin)
        if cid:
            conversation = await manager.get_conversation(event.unified_msg_origin, cid)
        if conversation:
            updated = float(conversation.updated_at)
            if isfinite(updated) and updated > 0 and updated + 1 <= cutoff:
                history = json.loads(conversation.history)
                if isinstance(history, list):
                    result['conversation'] = history
                    result['conversation_status'] = 'available'
            else:
                result['conversation_status'] = 'newer_than_event'
    except (AttributeError, TypeError, ValueError):
        pass
    try:
        _, persona, _, _ = await context.persona_manager.resolve_selected_persona(
            umo=event.unified_msg_origin,
            conversation_persona_id=getattr(conversation, 'persona_id', None),
            platform_name=event.get_platform_name(),
            provider_settings=context.get_config(umo=event.unified_msg_origin).get('provider_settings', {}),
        )
        if persona and isinstance(persona.get('prompt'), str):
            result['persona'] = persona['prompt']
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    return result


def decision_history(event: Any, reference: dict, settings: dict, excluded_ids: set) -> dict:
    """Project dialogue, not system/tool instructions; keep speaker roles."""
    snapshot = event.get_extra(CONTEXT_EXTRA, {})
    entries = []
    for row in reference.get('conversation', []):
        if not isinstance(row, dict) or row.get('role') not in {'user', 'assistant'}:
            continue
        content = row.get('content')
        if isinstance(content, list):
            content = '\n'.join(part['text'] for part in content
                                if isinstance(part, dict) and part.get('type') == 'text'
                                and isinstance(part.get('text'), str))
        if isinstance(content, str) and content.strip():
            entries.append({'source': 'official_conversation', 'role': row['role'], 'content': content})
    for record_id, text in snapshot.get('records', ()):
        if record_id not in excluded_ids:
            entries.append({'source': 'official_group_context', 'role': 'group_record', 'content': text})
    count, remaining = context_limits(settings)
    selected = []
    truncated = len(entries) > count or snapshot.get('truncated', False)
    for row in reversed(entries[-count:]):
        if remaining <= 0:
            truncated = True
            break
        text = row['content']
        truncated |= len(text) > remaining
        selected.append({**row, 'content': text[:remaining]})
        remaining -= len(text)
    return {'messages': list(reversed(selected)), 'truncated': truncated,
            'group_context_status': snapshot.get('status', 'unavailable'),
            'conversation_status': reference.get('conversation_status', 'unavailable')}
