from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SpeechPlan:
    """Planner 交给 Replyer 的最小化说话计划。"""

    mode: str = "chat"
    reply_shape: str = "chat_bubbles"
    conversation_mode: str = "direct_reply"
    audience: str = "current_sender"
    anchor: str = "当前消息"
    target: str = "当前说话者"
    intent: str = "自然回应当前消息"
    reply_act: str = "直接接话"
    reaction: str = ""
    emotion: str = "轻松"
    tone: str = "自然、口语化"
    length: str = "1至2句"
    must_include: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    use_allowed_tools: bool = False

    @classmethod
    def from_mapping(cls, value: Any) -> "SpeechPlan":
        if not isinstance(value, dict):
            return cls()

        def text(key: str, default: str, limit: int = 160) -> str:
            raw = str(value.get(key, default) or default).strip()
            return raw[:limit]

        def strings(key: str, limit: int = 5, item_limit: int = 180) -> list[str]:
            raw = value.get(key, [])
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, list):
                return []
            result: list[str] = []
            for item in raw:
                item_text = str(item or "").strip()
                if item_text and item_text not in result:
                    result.append(item_text[:item_limit])
                if len(result) >= limit:
                    break
            return result

        mode = text("mode", "chat", 16).lower()
        if mode not in {"chat", "task"}:
            mode = "chat"
        reply_shape = text("reply_shape", "chat_bubbles", 24).lower()
        if reply_shape not in {"chat_bubbles", "long_form"}:
            reply_shape = "chat_bubbles"
        conversation_mode = text("conversation_mode", "direct_reply", 24).lower()
        if conversation_mode not in {"direct_reply", "ambient_join", "quiet_topic"}:
            conversation_mode = "direct_reply"
        audience = text("audience", "current_sender", 24).lower()
        if audience not in {"current_sender", "current_thread", "whole_group"}:
            audience = "current_sender"
        raw_use_allowed_tools = value.get("use_allowed_tools", False)
        if isinstance(raw_use_allowed_tools, bool):
            use_allowed_tools = raw_use_allowed_tools
        else:
            use_allowed_tools = str(raw_use_allowed_tools).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        return cls(
            mode=mode,
            reply_shape=reply_shape,
            conversation_mode=conversation_mode,
            audience=audience,
            anchor=text("anchor", "当前消息", 180),
            target=text("target", "当前说话者", 80),
            intent=text("intent", "自然回应当前消息"),
            reply_act=text("reply_act", "直接接话"),
            reaction=text("reaction", "", 120) if value.get("reaction") else "",
            emotion=text("emotion", "轻松", 80),
            tone=text("tone", "自然、口语化", 120),
            length=text("length", "1至2句", 40),
            must_include=strings("must_include", 4, 100),
            avoid=strings("avoid", 6, 100),
            facts=strings("facts", 5, 220),
            use_allowed_tools=use_allowed_tools,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Expression:
    id: str
    situation: str
    style: str
    examples: list[str] = field(default_factory=list)
    enabled: bool = True

    @classmethod
    def from_mapping(cls, value: Any, index: int = 0) -> "Expression | None":
        if not isinstance(value, dict):
            return None
        situation = str(value.get("situation", "") or "").strip()
        style = str(value.get("style", "") or "").strip()
        if not situation or not style:
            return None
        raw_examples = value.get("examples", [])
        if isinstance(raw_examples, str):
            raw_examples = [raw_examples]
        examples = [
            str(item).strip()[:240]
            for item in raw_examples
            if str(item or "").strip()
        ][:3]
        return cls(
            id=str(value.get("id", f"expression-{index}") or f"expression-{index}"),
            situation=situation[:240],
            style=style[:240],
            examples=examples,
            enabled=bool(value.get("enabled", True)),
        )

    def document(self) -> str:
        sample = f" 示例：{' / '.join(self.examples)}" if self.examples else ""
        return f"情景：{self.situation} 表达方式：{self.style}{sample}"
