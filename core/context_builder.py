from __future__ import annotations

import json
import re
from typing import Any

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


def clean_contexts(
    event: Any,
    request_contexts: list[dict] | None,
    current_message: str,
    max_messages: int,
    max_chars: int,
    group_id: str = "",
) -> list[dict[str, str]]:
    """保留原生真实对话与可信身份字段，清除 system/tool/隐藏参考消息。"""
    cleaned: list[dict[str, str]] = []
    raw_contexts = _raw_contexts(request_contexts)
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
        if role == "user" and not text.startswith(("[群ID:", "[发送者：")):
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
            source_group_id = str(
                item.get("group_id")
                or metadata.get("group_id")
                or group_id
                or ""
            ).strip()
            if sender_id or sender_name:
                scope = f"群ID:{source_group_id}｜" if source_group_id else ""
                text = (
                    f"[{scope}发送者：{sender_name or '未知用户'}｜ID:{sender_id or 'unknown'}] "
                    + text
                )
        elif role == "user" and text.startswith("[发送者："):
            # 兼容已经标注发送者、但尚未标注来源群的上游上下文。
            metadata = item.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            source_group_id = str(
                item.get("group_id")
                or metadata.get("group_id")
                or group_id
                or ""
            ).strip()
            if source_group_id:
                text = f"[群ID:{source_group_id}｜{text[1:]}"
        cleaned.append({"role": role, "content": text[:2000]})

    while current_message.strip() and cleaned and cleaned[-1]["role"] == "user":
        tail = cleaned[-1]["content"].strip()
        if tail == current_message.strip() or current_message.strip() in tail[-len(current_message) - 20 :]:
            cleaned.pop()
        else:
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
