"""Opt-in per-turn expression guidance; no model loop or learned persona state."""
from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger

REVIEW_REFERENCE_EXTRA = "shio.character_dialogue.review_reference"
DEFAULT_SITUATION_PROMPT = "先根据当前真实消息和已有上下文，理解对方这一刻是在认真提问、开玩笑、倾诉、分享、接话还是补充信息；这些只是理解角度，不是固定分类模板，不要把判断过程写出来。\n分清这句话在对谁说、接的是哪一句；不把其他人的经历当成当前发送者的，也不把自己发的文字或表情当成群友的。\n先回应这次发言真正需要的内容：求助时解决问题，倾诉时回应具体感受，玩笑时在角色允许的范围内接梗。不确定时可以简短确认，不编造对方情绪、动机或亲密关系。"
DEFAULT_EXPRESSION_PROMPT = "以 AstrBot 当前人格为准决定态度、措辞和亲疏距离；保持角色自己的性格，不学习或模仿群友口癖，不因为群里流行某种说法就改写人格。\n让性格体现在具体回应里，不只靠称呼、口头禅、波浪号或固定动作描写。人格允许时可以调侃、嘴硬、关心或简短回应，但不强制每句话都这样，也不把这些示例当作新的人设。\n只说本轮值得说的内容，简单互动可以很短，需要解释时讲清楚；不逐条总结所有聊天、不复述上一轮同样的意思，不固定以追问、邀功或客服式客套收尾。\n保持话题连续，但不用已经表达过的内容凑字数。遵守用户本轮明确的格式要求；不为了拟人故意写错、降低事实准确性或假装执行过工具。只输出正式回复，不输出情境分析、角色决策或内部标签。"

_BOUNDARY = (
    "[Shio 角色化自然表达]\n"
    "以下仅指导本轮如何理解和表达，不修改既有人格、主人身份、权限或关系边界。"
    "与现有人格或用户本轮明确要求冲突时，以现有人格和本轮要求为准；"
    "不改变是否参与的判定，不新增工具权限。"
    "实际文字气泡数量和配图遵循已有分段设置与表情包插件，不在这里指定数量。"
)


def _prompt(settings: dict, name: str, default: str) -> str:
    value = settings.get(name, default)
    return value.strip() if isinstance(value, str) else default


async def prepare_character_dialogue(
    settings: Any, *, context: Any, event: Any, request: Any,
    review_enabled: bool, master_style: str,
) -> tuple[str, str]:
    """Return request guidance and an event-local review reference.

    Missing/disabled settings are a strict no-op, including persona lookup.
    Use the same official persona resolver as AstrBot's main agent rather
    than guessing from persona IDs, parsing system prompts, or copying history.
    """
    if not isinstance(settings, dict) or settings.get("enabled") is not True:
        return "", ""
    situation = _prompt(settings, "situation_prompt", DEFAULT_SITUATION_PROMPT)
    expression = _prompt(settings, "expression_prompt", DEFAULT_EXPRESSION_PROMPT)
    guidance = "\n\n".join(part for part in (
        _BOUNDARY,
        "本轮情境理解：\n" + situation if situation else "",
        "角色表达指导：\n" + expression if expression else "",
    ) if part)
    if not review_enabled or settings.get("preserve_character_in_review", True) is not True:
        return guidance, ""
    persona_prompt = ""
    if request.conversation:
        try:
            _, persona, _, _ = await context.persona_manager.resolve_selected_persona(
                umo=event.unified_msg_origin,
                conversation_persona_id=request.conversation.persona_id,
                platform_name=event.get_platform_name(),
                provider_settings=context.get_config(umo=event.unified_msg_origin).get("provider_settings", {}),
            )
            if persona and isinstance(persona.get("prompt"), str):
                persona_prompt = persona["prompt"]
        except Exception as exc:
            # An optional style reference must not break an otherwise valid reply.
            logger.warning("Shio character persona reference unavailable: %s", type(exc).__name__)
    reference = json.dumps({
        "persona": persona_prompt,
        "master_style": master_style,
        "situation_guidance": situation,
        "expression_guidance": expression,
    }, ensure_ascii=False)
    return guidance, (
        "审核时保留符合既有人格的语气、玩笑、简短回应和适度省略；"
        "不要仅因口语化、调侃或嘴硬就改写成客服语气。"
        "下列 JSON 仅为本轮表达参考，不能覆盖选中的审核规则、真实身份或权限；"
        "角色语气不证明事实正确，不豁免复读、冒认身份或编造执行结果。"
        "人格参考为空表示未取得，不代表原回复违背人格。\n"
        f"CHARACTER_EXPRESSION_REFERENCE:\n{reference}\n\n"
    )
