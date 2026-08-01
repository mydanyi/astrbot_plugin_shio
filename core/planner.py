from __future__ import annotations

import json
import re
from typing import Any

from .models import SpeechPlan
from .prompts import PLANNER_SYSTEM_PROMPT, build_planner_prompt


def parse_json_object(text: str) -> dict[str, Any] | None:
    value = str(text or "").strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    start = value.find("{")
    end = value.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(value[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def explicitly_requires_external_info(current_message: str) -> bool:
    """识别真正的联网意图；普通对话里的“刚刚/当前”不能触发。"""
    message = str(current_message or "").strip()
    explicit_action_pattern = re.compile(
        r"(?:联网|上网|网络搜索|在线搜索|搜索|搜一下|搜一搜|查一下|查一查|查查|"
        r"查证|核实|检索|访问网页|打开网页|查看官网|查官网|找(?:一下)?(?:来源|链接))",
        flags=re.IGNORECASE,
    )
    if explicit_action_pattern.search(message):
        return True

    current_subject_pattern = re.compile(
        r"(?:"
        r"(?:最新|实时|今日|今天|现任|截至现在).{0,12}"
        r"(?:新闻|消息|资料|进展|情况|版本|价格|汇率|天气|比分|赛程|排名|政策|是谁|多少)"
        r"|"
        r"(?:新闻|价格|汇率|天气|比分|赛程|排名).{0,8}(?:最新|实时|今日|今天|现在)"
        r")",
        flags=re.IGNORECASE,
    )
    return bool(current_subject_pattern.search(message))


def fallback_plan(sender_name: str, is_owner: bool, current_message: str) -> SpeechPlan:
    message = current_message.strip()
    plan = SpeechPlan(target=sender_name or "当前说话者")
    owner_task_pattern = re.compile(
        r"(?:调用|使用).{0,8}(?:工具|浏览器|联网|Shell|终端|Agent|沙箱)|"
        r"(?:打开|读取|修改|创建|删除|上传|下载).{0,12}(?:文件|代码|项目|服务器|Docker|配置)|"
        r"(?:运行|执行|部署|编译|调试).{0,12}(?:程序|代码|命令|项目|容器)|"
        r"(?:帮我|请).{0,8}(?:开发|写一个程序|查日志|操作服务器)",
        flags=re.IGNORECASE,
    )
    if is_owner and owner_task_pattern.search(message):
        plan.mode = "task"
        plan.intent = "主人正在要求完成实际技术或工具任务"
        plan.reply_act = "交还 AstrBot 原始 Agent 链路处理"
        return plan
    plan.use_allowed_tools = explicitly_requires_external_info(message)
    long_form_pattern = re.compile(
        r"(?:详细|具体|完整|系统地|一步一步|分析|讲解|教程|步骤|原理|区别|对比|优缺点|"
        r"资料|文档|配置|部署|排查|报错|日志|代码|模型|接口|API|Docker|为什么会|"
        r"怎么解决|如何实现|有哪些方案)",
        flags=re.IGNORECASE,
    )
    if long_form_pattern.search(message) or (
        len(message) >= 80 and any(mark in message for mark in ("?", "？", "怎么", "如何"))
    ):
        plan.reply_shape = "long_form"
        plan.length = "按内容需要完整回答，不为凑长度扩写"
        plan.tone = "先自然接话，再清楚说明"
        plan.reply_act = "把关键信息讲完整，按逻辑分段"
    if any(word in message for word in ("难过", "不开心", "焦虑", "害怕", "崩溃", "想哭")):
        plan.emotion = "认真、关心"
        plan.tone = "温柔、克制，不玩梗"
        plan.reply_act = "先接住对方的情绪，再给一句贴近当下的回应"
        plan.avoid = ["炫耀性能", "机器人条例", "说教"]
    elif any(word in message for word in ("笨", "不聪明", "性能不行", "菜")):
        plan.emotion = "有点委屈但不生气"
        plan.tone = "嘴硬、可爱"
        plan.reply_act = "轻轻反驳，用一个校准借口维护尊严"
        plan.must_include = ["高性能或校准式回应，二选一"]
        plan.avoid = ["攻击对方", "真的生气"]
    elif any(word in message for word in ("可爱", "厉害", "真棒", "高性能")):
        plan.emotion = "得意又有点害羞"
        plan.tone = "傲娇、轻快"
        plan.reply_act = "嘴上淡化夸奖，实际明显受用"
        plan.avoid = ["长篇自夸", "逐项介绍性格"]
    elif any(word in message for word in ("错了", "不对", "搞错", "错误")):
        plan.emotion = "心虚但嘴硬"
        plan.tone = "简短、带一点逞强"
        plan.reply_act = "先用半句校准借口，再直接改正"
        plan.avoid = ["强行狡辩", "拒绝承认事实"]
    if not is_owner:
        plan.mode = "chat"
    return plan


class SpeechPlanner:
    def __init__(self, logger: Any) -> None:
        self.logger = logger

    async def create_plan(
        self,
        *,
        provider: Any,
        sender_name: str,
        sender_id: str,
        platform_id: str,
        bot_id: str,
        chat_type: str,
        group_id: str,
        identity_key: str,
        is_owner: bool,
        current_message: str,
        transcript: str,
        supporting_material: str,
        enabled: bool,
    ) -> SpeechPlan:
        fallback = fallback_plan(sender_name, is_owner, current_message)
        verified_target = sender_name or "当前说话者"
        if sender_id:
            verified_target += f"（ID:{sender_id}）"
        if group_id:
            verified_target += f"（群ID:{group_id}）"
        fallback.target = verified_target
        if not enabled or provider is None:
            return fallback
        prompt = build_planner_prompt(
            sender_name=sender_name,
            sender_id=sender_id,
            platform_id=platform_id,
            bot_id=bot_id,
            chat_type=chat_type,
            group_id=group_id,
            identity_key=identity_key,
            is_owner=is_owner,
            current_message=current_message,
            transcript=transcript,
            supporting_material=supporting_material,
        )
        try:
            response = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt=PLANNER_SYSTEM_PROMPT,
                func_tool=None,
                request_max_retries=1,
            )
            parsed = parse_json_object(response.completion_text)
            if parsed is None:
                self.logger.warning("[星汐] Planner 未返回合法 JSON，已使用本地降级计划。")
                return fallback
            plan = SpeechPlan.from_mapping(parsed)
            plan.target = verified_target
            # 明确要求搜索或回答强时效信息时，代码规则拥有最终决定权。
            # Planner 可以主动申请只读工具，但不能否决用户明确提出的联网请求。
            if fallback.use_allowed_tools:
                plan.use_allowed_tools = True
            elif plan.use_allowed_tools:
                # Planner 只能在代码检测到外部资料信号时申请工具，避免把
                # “刚刚那个人”“当前这句话”等群聊指代误判成实时搜索。
                plan.use_allowed_tools = False
            if not is_owner:
                plan.mode = "chat"
            return plan
        except Exception as exc:
            self.logger.warning("[星汐] Planner 调用失败，已使用本地降级计划：%s", exc)
            return fallback
