from __future__ import annotations

import json
import re
from typing import Any

from .response_guard import extract_and_clean_internal_meme_references


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return str(content or "").strip()

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type", ""))
        if kind == "text":
            parts.append(str(item.get("text", "") or ""))
        elif kind == "image_url":
            parts.append("[图片]")
        elif kind == "audio_url":
            parts.append("[语音]")
    return " ".join(part.strip() for part in parts if part.strip()).strip()


def get_current_message(event: Any, request_prompt: str | None) -> str:
    current = ""
    try:
        current = str(event.get_message_str() or "").strip()
    except Exception:
        pass
    if not current:
        current = str(request_prompt or "").strip()
    return current[:4000]


def _raw_contexts(request_contexts: list[dict] | None) -> list[Any]:
    return list(request_contexts or [])


def normalize_group_id(value: Any) -> str:
    """把 LivingMemory 的完整 UMO 或普通群号统一成群 ID。"""
    text = str(value or "").strip()
    if not text:
        return ""
    parts = text.split(":")
    if len(parts) >= 3 and "group" in parts[-2].lower():
        return parts[-1].strip()
    return text


def _sender_fields(item: dict[str, Any]) -> tuple[str, str, str]:
    metadata = item.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    sender = item.get("sender", {})
    if not isinstance(sender, dict):
        sender = {}
    sender_id = str(
        item.get("sender_id")
        or item.get("user_id")
        or sender.get("user_id")
        or sender.get("id")
        or metadata.get("sender_id")
        or ""
    ).strip()
    sender_name = str(
        item.get("sender_name")
        or item.get("username")
        or item.get("nickname")
        or item.get("name")
        or sender.get("nickname")
        or sender.get("name")
        or metadata.get("sender_name")
        or ""
    ).strip()
    source_group_id = normalize_group_id(
        item.get("group_id") or metadata.get("group_id") or ""
    )
    return sender_id, sender_name, source_group_id


def _is_prelabelled_user(text: str) -> bool:
    return text.startswith(("[群ID:", "[发送者："))


def _contains_sender_id(text: str, sender_id: str) -> bool:
    if not sender_id:
        return True
    return bool(
        re.search(
            rf"(?:ID|id)\s*[:：]\s*{re.escape(sender_id)}(?:\D|$)",
            text,
        )
    )


def clean_contexts(
    event: Any,
    request_contexts: list[dict] | None,
    current_message: str,
    max_messages: int,
    max_chars: int,
    group_id: str = "",
    current_sender_id: str = "",
) -> list[dict[str, str]]:
    """保留可信对话并隔离群成员；群聊中丢弃无法确认发送者的完整旧轮次。"""
    cleaned: list[dict[str, str]] = []
    raw_contexts = _raw_contexts(request_contexts)
    normalized_group_id = normalize_group_id(group_id)
    trusted_group_turn = not bool(normalized_group_id)
    for item in raw_contexts:
        if not isinstance(item, dict):
            model_dump = getattr(item, "model_dump", None)
            if callable(model_dump):
                item = model_dump()
            else:
                continue
        role = str(item.get("role", "")).lower()
        if role not in {"user", "assistant"}:
            continue
        text = content_to_text(item.get("content", ""))
        if not text:
            continue
        if role == "assistant":
            # Never feed a previously leaked Meme Manager marker or pseudo tool
            # call back to the model as an in-context speaking example.
            text, _ = extract_and_clean_internal_meme_references(text)
            if not text:
                continue
        if role == "user" and not _is_prelabelled_user(text):
            sender_id, sender_name, source_group_id = _sender_fields(item)
            if sender_id or sender_name:
                source_group_id = source_group_id or normalized_group_id
                scope = f"群ID:{source_group_id}｜" if source_group_id else ""
                text = (
                    f"[{scope}发送者：{sender_name or '未知用户'}｜ID:{sender_id or 'unknown'}] "
                    + text
                )
                trusted_group_turn = True
            elif normalized_group_id:
                # AstrBot 原生群聊 contexts 常把全群成员压成同一个 user，且不带
                # sender_id。保留这种轮次会把前一个人的“我”继承给当前群友。
                trusted_group_turn = False
                continue
        elif role == "user" and text.startswith("[发送者："):
            # 兼容已经标注发送者、但尚未标注来源群的上游上下文。
            _, _, source_group_id = _sender_fields(item)
            source_group_id = source_group_id or normalized_group_id
            if source_group_id:
                text = f"[群ID:{source_group_id}｜{text[1:]}"
            trusted_group_turn = True
        elif role == "user":
            trusted_group_turn = True
        elif normalized_group_id and not trusted_group_turn:
            # 与上一条无身份 user 配对的 assistant 回复同样可能含有“你/主人”等
            # 人物关系，必须整轮丢弃，不能留下半截污染后续身份判断。
            continue
        cleaned.append({"role": role, "content": text[:2000]})

    current = current_message.strip()
    if current:
        # LivingMemory 可能已在 on_llm_request 前记录当前消息。按发送者 ID 从尾部
        # 精确移除本轮，避免当前问题既在 history 又在 prompt 中出现。
        for index in range(len(cleaned) - 1, -1, -1):
            item = cleaned[index]
            if item["role"] != "user":
                continue
            tail = item["content"].strip()
            if not _contains_sender_id(tail, current_sender_id):
                continue
            if tail == current or current in tail[-len(current) - 80 :]:
                cleaned.pop(index)
                break

    cleaned = cleaned[-max(1, max_messages) :]
    total = 0
    result: list[dict[str, str]] = []
    for item in reversed(cleaned):
        size = len(item["content"])
        if result and total + size > max_chars:
            break
        if not result and size > max_chars:
            item = {"role": item["role"], "content": item["content"][-max_chars:]}
            size = len(item["content"])
        result.append(item)
        total += size
    return list(reversed(result))


def contexts_as_transcript(contexts: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for item in contexts:
        content = item["content"]
        if item["role"] == "user" and content.startswith(("[群ID:", "[发送者：")):
            lines.append(content)
        else:
            label = "未标注身份的历史用户" if item["role"] == "user" else "角色"
            lines.append(f"{label}：{content}")
    return "\n".join(lines) or "（没有可用的历史聊天）"


SENDER_LABEL_PATTERN = re.compile(
    r"^\[(?:群ID:[^｜\]]+｜)?发送者：.*?｜ID:([^\]]+)\]\s*"
)


def isolate_replyer_contexts(
    contexts: list[dict[str, str]],
    *,
    current_sender_id: str,
    group_id: str,
) -> list[dict[str, str]]:
    """群聊 Replyer 只保留当前发送者自己的历史轮次。

    Planner 仍然读取完整、带身份标签的群聊历史来理解多人话题；最终
    Replyer 不再直接看到其他成员的第一人称经历，避免把上一人的事实
    套给当前用户。紧随当前发送者消息的机器人回复视为同一轮保留。
    """
    if not normalize_group_id(group_id):
        return list(contexts)

    sender_id = str(current_sender_id or "").strip()
    if not sender_id:
        return []

    result: list[dict[str, str]] = []
    keep_assistant_turn = False
    for item in contexts:
        role = str(item.get("role", "") or "").lower()
        content = str(item.get("content", "") or "")
        if role == "user":
            match = SENDER_LABEL_PATTERN.match(content)
            keep_assistant_turn = bool(
                match and match.group(1).strip() == sender_id
            )
            if keep_assistant_turn:
                result.append({"role": "user", "content": content})
        elif role == "assistant" and keep_assistant_turn:
            result.append({"role": "assistant", "content": content})
    return result


def collect_supporting_material(request: Any, limit: int = 7000) -> str:
    """收集 LivingMemory 与原始人格资料，只提供给 Planner。"""
    parts: list[str] = []
    for part in list(getattr(request, "extra_user_content_parts", None) or []):
        text = getattr(part, "text", None)
        if text is None and isinstance(part, dict):
            text = part.get("text", "")
        text = str(text or "").strip()
        if text:
            parts.append(text)

    # LivingMemory 推荐使用 extra_user_content；同时兼容其可选的伪工具调用注入。
    for item in list(getattr(request, "contexts", None) or []):
        if not isinstance(item, dict) or item.get("role") != "tool":
            continue
        tool_call_id = str(item.get("tool_call_id", "") or "")
        tool_name = str(item.get("name", "") or "")
        if tool_call_id.startswith("fake_recall_") or tool_name == "recall_long_term_memory":
            text = content_to_text(item.get("content", ""))
            if text:
                parts.append("[LivingMemory 召回资料]\n" + text)

    # 兼容 LivingMemory 的 user_message_before / user_message_after 注入方式，
    # 只提取记忆标签，不把整个当前问题重复塞给 Planner。
    request_prompt = str(getattr(request, "prompt", "") or "")
    for memory_block in re.findall(
        r"<RAG-Faiss-Memory>.*?</RAG-Faiss-Memory>",
        request_prompt,
        flags=re.DOTALL,
    ):
        parts.append(memory_block.strip())

    system_prompt = str(getattr(request, "system_prompt", "") or "").strip()
    if system_prompt:
        parts.append("[原始人格与系统资料]\n" + system_prompt)

    merged = "\n\n".join(parts)
    if len(merged) <= limit:
        return merged
    head = merged[: limit // 2]
    tail = merged[-limit // 2 :]
    return head + "\n\n[中间资料已截断]\n\n" + tail


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
