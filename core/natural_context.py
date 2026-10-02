"""Read-only, event-local inputs for natural participation on AstrBot 4.27.4."""
from __future__ import annotations

import json
from math import isfinite
from typing import Any

CONTEXT_EXTRA = "shio.natural.official_context"
DEFAULT_NATURAL_PROMPT = (
    "这是一批没有直接叫你的群消息。结合当前人格、此前语境和本批消息，判断是否自然地接话。"
    "符合角色兴趣的分享、玩笑、共鸣和接梗也可以参与，不必只有回答问题或提供帮助才开口。"
    "别人在明确互相交谈且没有适合你接入的点、你刚说过同样的意思，或加入会打断时不参与。"
    "消息尚未说完或需要后续信息时等待。不要因为发送者是主人就每句都接；不学习或模仿群友性格。"
)


def context_limits(settings: dict) -> tuple[int, int]:
    def bounded(key, default, lower, upper):
        try:
            return min(upper, max(lower, int(settings.get(key, default))))
        except (TypeError, ValueError, OverflowError):
            return default
    return (bounded('natural_context_max_messages', 50, 1, 200),
            bounded('natural_context_max_chars', 12000, 1000, 64000))


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
