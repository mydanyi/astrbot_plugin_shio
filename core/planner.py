from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from .models import SpeechPlan
from .prompts import PLANNER_SYSTEM_PROMPT, build_planner_prompt


NONOWNER_INTIMACY_SIGNAL_PATTERN = re.compile(
    r"(?:mua|么么|啵啵|亲昵|亲亲|亲吻|回亲|亲回|撒娇|求抱|抱抱|贴贴|暧昧|"
    r"恋爱|恋人|情侣|老婆|老公|女朋友|男朋友|宝贝|亲爱的|爱你|喜欢你|"
    r"吃醋|占有欲|专属(?:感情|承诺|依恋))",
    flags=re.IGNORECASE,
)

NONOWNER_DIRECT_INTIMACY_PATTERN = re.compile(
    r"(?:^|[，,。！？!?～~\s])(?:mua|么么|啵啵|亲亲|亲一个|抱抱|贴贴)(?:$|[，,。！？!?～~\s])|"
    r"(?:我爱你|我喜欢你|做我(?:老婆|女朋友|男朋友)|当我(?:老婆|女朋友|男朋友)|"
    r"你是我(?:老婆|女朋友|男朋友)|叫(?:你|我)(?:老婆|老公|宝贝|亲爱的))",
    flags=re.IGNORECASE,
)

OWNER_REFERENCE_PATTERN = re.compile(
    r"(?:主人|Master|群主)",
    flags=re.IGNORECASE,
)

# 普通群友提到主人时，Planner 偶尔会把问题本身改写成“主人这么说了”，
# 再通过 must_include 强迫 Replyer 复读。这里识别的是“把主人当成本轮说话者”
# 的归因句式，不拦截“让 Master 本人决定”等明确第三人称表达。
NONOWNER_OWNER_SPEAKER_ATTRIBUTION_PATTERN = re.compile(
    r"(?:^|[\s，,。！？!?；;：:])"
    r"(?:既然|因为|所以|听(?:到|见)?|按照|按|那(?:么)?|好吧)?\s*"
    r"(?:主人|Master|群主)(?:大人)?\s*"
    r"(?:这么|这样|都|已经)?\s*"
    r"(?:说|讲|问|要求|吩咐|同意|答应|愿意|想|要)(?:了|过)?",
    flags=re.IGNORECASE,
)

RISQUE_TEASING_PATTERN = re.compile(
    r"(?:黄色笑话|黄段子|涩涩|色色|瑟瑟|色图|"
    r"亲一个|亲亲你|mua|么么|啵啵|摸摸你|摸你|摸一下你|"
    r"睡你|陪睡|暖床|上床|脱衣服|脱掉衣服|看裙底|内衣照|胖次|调教)|"
    r"(?:看|摸|碰|舔|亲|抱|给我看看|让我看看).{0,6}(?:你的?)?(?:胸|屁股|腿|脚|白丝|黑丝|丝袜)|"
    r"(?:做我|当我|你是我|叫你|喊你).{0,3}(?:老婆|女朋友|小老婆)",
    flags=re.IGNORECASE,
)

SERIOUS_RISQUE_CONTEXT_PATTERN = re.compile(
    r"(?:科普|医学|健康|疾病|症状|治疗|法律|举报|骚扰|侵犯|受害|求助|"
    r"怎么处理|如何处理|怎么办|是否违法|正常吗|讨论|什么意思|是什么|为什么|原理|新闻|资料)",
    flags=re.IGNORECASE,
)


def is_risque_teasing(current_message: str) -> bool:
    """识别短促的调戏/擦边玩笑，排除医学、求助和资料讨论。"""
    message = str(current_message or "").strip()
    # 引用正文与昵称都不是当前发送者正在说的话。尤其不能因为群友昵称中
    # 带“变态”等词，就把后面的验证码、电影票或技术讨论误判成擦边调戏。
    message = re.sub(
        r"\[引用消息\([^\]\n]{0,600}\)\]",
        " ",
        message,
        flags=re.IGNORECASE,
    )
    # QQ 适配器可能把 @ 对象渲染成“@昵称(QQ号)”。昵称只是身份显示，
    # 不能因为里面恰好带“老婆/白丝”等词就把后面的正常消息判成调戏。
    message = re.sub(r"@[^\n()]{1,80}\(\d{4,20}\)", " ", message)
    message = re.sub(r"\[At:\d{4,20}\]", " ", message, flags=re.IGNORECASE)
    message = re.sub(r"\s+", " ", message).strip()
    if not message or len(message) > 160:
        return False
    if SERIOUS_RISQUE_CONTEXT_PATTERN.search(message):
        return False
    return bool(RISQUE_TEASING_PATTERN.search(message))


def enforce_emotional_reaction(
    plan: SpeechPlan,
    is_owner: bool,
    current_message: str = "",
) -> bool:
    """为调戏和擦边玩笑锁定角色反应，避免退化成正经答复。"""
    if not is_risque_teasing(current_message):
        return False

    plan.mode = "chat"
    plan.reply_shape = "chat_bubbles"
    if is_owner:
        plan.intent = "主人突然用暧昧或擦边玩笑逗弄，需要先表现真实的害羞或羞恼"
        plan.reaction = "第一拍先明显愣住、慌张或羞恼，可以嘴硬抗议主人太突然"
        plan.reply_act = "先短促地害羞抗议，再按人格接住或岔开；不要平静解释笑话"
        plan.emotion = "被主人逗得慌张、害羞又嘴硬"
        plan.tone = "短促、傲娇、有明显情绪起伏"
    else:
        plan.intent = "普通群友用暧昧或擦边玩笑调戏，需要可爱地羞恼抗议并保持边界"
        plan.reaction = (
            "第一拍先羞恼或被吓一跳，用短促抗议表现出来；不要要求固定词，"
            "只有对方直接说得很露骨时才可极偶尔叫一声“变态”"
        )
        plan.reply_act = "先短促羞恼地抗议，再嘴硬挡回或迅速岔开；不回亲、不解释规则，也不顺势暧昧"
        plan.emotion = "羞恼、慌张、鼓起脸抗议，但不是真的仇视"
        plan.tone = "像被逗到的傲娇受气包，短促、有情绪、不说教"
    plan.length = "1至2条短消息；第一条是情绪反应，第二条最多补一句嘴硬抗议或岔开"
    plan.must_include = [
        "先有角色本能反应，再表达态度",
        *[
            item
            for item in plan.must_include
            if not NONOWNER_INTIMACY_SIGNAL_PATTERN.search(str(item))
        ],
    ][:4]
    plan.avoid = list(
        dict.fromkeys(
            [
                "正经解释黄色笑话或性暗示",
                "客服式提醒、规则宣读或道德说教",
                "复述露骨内容或把玩笑升级得更露骨",
                "毫无情绪地直接回答字面问题",
                *plan.avoid,
            ]
        )
    )[:8]
    return True


def enforce_relationship_boundary(
    plan: SpeechPlan,
    is_owner: bool,
    current_message: str = "",
) -> bool:
    """把关系距离锁定到代码身份，避免 Planner 给群友分配主人式亲密。"""
    if is_owner:
        return False

    plan.mode = "chat"
    boundary_avoids = [
        "把主人专属亲密给普通群友",
        "回亲、回应mua、索吻、求抱或恋爱式撒娇",
        "情侣称呼、吃醋、占有欲或专属承诺",
    ]
    plan.avoid = list(dict.fromkeys([*boundary_avoids, *plan.avoid]))[:6]

    plan_material = " ".join(
        [
            plan.intent,
            plan.reply_act,
            plan.emotion,
            plan.tone,
            *plan.must_include,
        ]
    )
    direct_message = str(current_message or "").strip()
    plan_claims_intimacy = bool(
        NONOWNER_INTIMACY_SIGNAL_PATTERN.search(plan_material)
    )
    direct_message_is_intimate = bool(
        len(direct_message) <= 80
        and NONOWNER_DIRECT_INTIMACY_PATTERN.search(direct_message)
    ) or is_risque_teasing(direct_message)
    needs_correction = plan_claims_intimacy or direct_message_is_intimate
    if not needs_correction:
        return False

    if plan_claims_intimacy and not direct_message_is_intimate:
        plan.intent = "按字面理解普通群友的问题或提到的其他对象，不擅自升级成亲昵、示爱或调戏"
        plan.reaction = ""
        plan.reply_act = "自然回应当前问题；保持当前群友、主人和消息中的其他对象是不同的人"
        plan.emotion = "自然、稍带一点傲娇"
        plan.tone = "轻快、口语化、不暧昧"
        plan.length = "1至2条自然短消息"
        plan.must_include = [
            item
            for item in plan.must_include
            if not NONOWNER_INTIMACY_SIGNAL_PATTERN.search(str(item))
            and not NONOWNER_OWNER_SPEAKER_ATTRIBUTION_PATTERN.search(str(item))
        ][:4]
        return True

    plan.intent = "普通群友正在用亲昵表达调侃或示好，需要先有真实反应，再傲娇地守住边界"
    plan.reaction = "先有一瞬间慌张、害羞或羞恼的本能反应，不能平静宣读边界"
    plan.reply_act = "短促地嘴硬抗议或岔开，不回亲，也不升级成恋人关系"
    plan.emotion = "有点慌张和羞恼，但不是真的敌视"
    plan.tone = "傲娇、短促、有情绪、不暧昧"
    plan.length = "1至2条自然短消息；先反应，再挡回去"
    plan.must_include = [
        item
        for item in plan.must_include
        if not NONOWNER_INTIMACY_SIGNAL_PATTERN.search(str(item))
        and not NONOWNER_OWNER_SPEAKER_ATTRIBUTION_PATTERN.search(str(item))
    ][:4]
    enforce_emotional_reaction(plan, False, current_message)
    return True


def enforce_verified_identity(
    plan: SpeechPlan,
    *,
    sender_name: str,
    sender_id: str,
    is_owner: bool,
    current_message: str = "",
) -> bool:
    """把运行时身份重新写入模型计划，并清除与代码身份冲突的强制台词。"""
    identity_role = "主人" if is_owner else "普通群友，不是主人、Master或群主"
    identity_fact = (
        f"当前发送者是{sender_name or '未知昵称'}（ID:{sender_id or '缺失'}），"
        f"代码验证身份为{identity_role}。"
    )
    existing_facts = [
        str(item)
        for item in plan.facts
        if str(item).strip()
        and "代码验证身份为" not in str(item)
    ]
    facts = [identity_fact]
    owner_is_third_person = bool(
        not is_owner and OWNER_REFERENCE_PATTERN.search(str(current_message or ""))
    )
    if owner_is_third_person:
        facts.append(
            "当前消息中的“主人／Master／群主”是普通群友提到的另一个对象；"
            "当前这句话不是主人说的，也不能据此替主人补写发言。"
        )
    plan.facts = list(dict.fromkeys([*facts, *existing_facts]))[:5]

    identity_avoids = [
        "把当前发送者、主人和消息中的第三人称对象混为一人",
        "根据昵称、自称、代词或消息里出现“主人”就改变代码身份",
    ]
    if owner_is_third_person:
        identity_avoids.append(
            "把普通群友当前这句话改写成“主人这么说了”或主人提出的要求"
        )
    plan.avoid = list(dict.fromkeys([*identity_avoids, *plan.avoid]))[:6]

    if is_owner:
        return False
    original_must_include = list(plan.must_include)
    plan.must_include = [
        item
        for item in plan.must_include
        if not NONOWNER_OWNER_SPEAKER_ATTRIBUTION_PATTERN.search(str(item))
    ][:4]
    return owner_is_third_person or plan.must_include != original_must_include


def enforce_conversation_mode(
    plan: SpeechPlan,
    conversation_mode: str,
    current_message: str = "",
) -> None:
    """锁定本轮语用场景，避免主动群聊退化成一对一问答。"""
    normalized = str(conversation_mode or "direct_reply").strip().lower()
    if normalized not in {"direct_reply", "ambient_join", "quiet_topic"}:
        normalized = "direct_reply"
    plan.conversation_mode = normalized

    if normalized == "ambient_join":
        plan.mode = "chat"
        plan.reply_shape = "chat_bubbles"
        plan.audience = "current_thread"
        plan.anchor = plan.anchor if plan.anchor not in {"", "当前消息"} else (
            f"当前多人话题中的这句：{str(current_message or '').strip()[:120]}"
        )
        plan.use_allowed_tools = False
    elif normalized == "quiet_topic":
        plan.mode = "chat"
        plan.reply_shape = "chat_bubbles"
        plan.audience = "whole_group"
        plan.anchor = plan.anchor or "当前群聊的公共话题与整体气氛"
        plan.use_allowed_tools = False
    else:
        plan.audience = "current_sender"
        if not plan.anchor:
            plan.anchor = "当前消息"


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


def fallback_plan(
    sender_name: str,
    is_owner: bool,
    current_message: str,
    conversation_mode: str = "direct_reply",
) -> SpeechPlan:
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
        plan.reaction = "第一拍先明显不服或有点委屈，像被戳到自尊一样短促反驳"
        plan.emotion = "有点委屈但不生气"
        plan.tone = "嘴硬、可爱"
        plan.reply_act = "轻轻反驳，用一个校准借口维护尊严"
        plan.must_include = ["高性能或校准式回应，二选一"]
        plan.avoid = ["攻击对方", "真的生气"]
    elif any(word in message for word in ("可爱", "厉害", "真棒", "高性能")):
        plan.reaction = "第一拍先愣一下或下意识嘴硬，不能平静接受夸奖"
        plan.emotion = "得意又有点害羞"
        plan.tone = "傲娇、轻快"
        plan.reply_act = "嘴上淡化夸奖，实际明显受用"
        plan.avoid = ["长篇自夸", "逐项介绍性格"]
    elif any(word in message for word in ("错了", "不对", "搞错", "错误")):
        plan.reaction = "第一拍先心虚地停一下或嘴硬半句，再立刻改正"
        plan.emotion = "心虚但嘴硬"
        plan.tone = "简短、带一点逞强"
        plan.reply_act = "先用半句校准借口，再直接改正"
        plan.avoid = ["强行狡辩", "拒绝承认事实"]
    if not is_owner:
        plan.mode = "chat"
    enforce_emotional_reaction(plan, is_owner, current_message)
    enforce_conversation_mode(plan, conversation_mode, current_message)
    return plan


class SpeechPlanner:
    def __init__(self, logger: Any) -> None:
        self.logger = logger

    async def create_plan(
        self,
        *,
        provider: Any,
        fallback_provider: Any = None,
        timeout_seconds: float = 20.0,
        sender_name: str,
        sender_id: str,
        platform_id: str,
        bot_id: str,
        chat_type: str,
        group_id: str,
        identity_key: str,
        is_owner: bool,
        conversation_mode: str = "direct_reply",
        conversation_mode_rules: str = "",
        current_message: str,
        transcript: str,
        supporting_material: str,
        enabled: bool,
    ) -> SpeechPlan:
        fallback = fallback_plan(
            sender_name,
            is_owner,
            current_message,
            conversation_mode,
        )
        verified_target = sender_name or "当前说话者"
        if sender_id:
            verified_target += f"（ID:{sender_id}）"
        if group_id:
            verified_target += f"（群ID:{group_id}）"
        fallback.target = verified_target
        enforce_verified_identity(
            fallback,
            sender_name=sender_name,
            sender_id=sender_id,
            is_owner=is_owner,
            current_message=current_message,
        )
        if re.search(r"(?:他|她|它|TA|ta|那个人|刚刚那人|刚才那人)", current_message):
            fallback.facts = [
                *fallback.facts,
                "当前消息中的第三人称对象身份未由代码验证；保持原有指代，不得默认当成当前发送者或主人。",
            ][:5]
        enforce_relationship_boundary(fallback, is_owner, current_message)
        enforce_emotional_reaction(fallback, is_owner, current_message)
        enforce_conversation_mode(fallback, conversation_mode, current_message)
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
            conversation_mode=conversation_mode,
            conversation_mode_rules=conversation_mode_rules,
            current_message=current_message,
            transcript=transcript,
            supporting_material=supporting_material,
        )
        attempts: list[tuple[str, Any]] = [("主", provider)]
        if fallback_provider is not None and fallback_provider is not provider:
            attempts.append(("备用", fallback_provider))
        # 生产配置在调用方限制为 3～60 秒；这里保留更小值便于精确单元测试。
        timeout_seconds = min(60.0, max(0.01, float(timeout_seconds)))

        for attempt_name, candidate in attempts:
            provider_name = self._provider_name(candidate)
            started = time.monotonic()
            try:
                completion_text = await asyncio.wait_for(
                    self._request_completion(candidate, prompt),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                elapsed = time.monotonic() - started
                self.logger.warning(
                    "[星汐/Planner] %s Provider %s 达到插件硬超时 %.1f 秒，已取消 SDK 内部等待。",
                    attempt_name,
                    provider_name,
                    elapsed,
                )
                continue
            except Exception as exc:
                elapsed = time.monotonic() - started
                self.logger.warning(
                    "[星汐/Planner] %s Provider %s 调用失败（%s，%.1f 秒）。",
                    attempt_name,
                    provider_name,
                    type(exc).__name__,
                    elapsed,
                )
                continue

            parsed = parse_json_object(completion_text)
            if parsed is None:
                self.logger.warning(
                    "[星汐/Planner] %s Provider %s 未返回合法 JSON，继续降级。",
                    attempt_name,
                    provider_name,
                )
                continue
            plan = SpeechPlan.from_mapping(parsed)
            plan.target = verified_target
            identity_corrected = enforce_verified_identity(
                plan,
                sender_name=sender_name,
                sender_id=sender_id,
                is_owner=is_owner,
                current_message=current_message,
            )
            if identity_corrected:
                self.logger.warning(
                    "[星汐/身份锚] 已按代码身份清理 Planner 中的伪主人归因或强制台词。"
                )
            enforce_conversation_mode(plan, conversation_mode, current_message)
            # 明确要求搜索或回答强时效信息时，代码规则拥有最终决定权。
            # Planner 可以主动申请只读工具，但不能否决用户明确提出的联网请求。
            if fallback.use_allowed_tools:
                plan.use_allowed_tools = True
            elif plan.use_allowed_tools:
                # Planner 只能在代码检测到外部资料信号时申请工具，避免把
                # “刚刚那个人”“当前这句话”等群聊指代误判成实时搜索。
                plan.use_allowed_tools = False
            if not is_owner:
                corrected = enforce_relationship_boundary(
                    plan,
                    is_owner,
                    current_message,
                )
                if corrected:
                    self.logger.warning(
                        "[星汐/关系边界] Planner 为普通群友生成了主人式亲密计划，已按代码身份修正。"
                    )
            enforce_emotional_reaction(plan, is_owner, current_message)
            if attempt_name == "备用":
                self.logger.info(
                    "[星汐/Planner] 备用 Provider %s 已接管规划，耗时 %.1f 秒。",
                    provider_name,
                    time.monotonic() - started,
                )
            return plan

        self.logger.warning("[星汐/Planner] 所有 Provider 均失败，已使用身份安全的本地降级计划。")
        return fallback

    async def _request_completion(self, provider: Any, prompt: str) -> str:
        """调用 Planner Provider，且不改动共享的主对话配置。

        Planner 需要机器可解析的 JSON，而普通 ``text_chat`` 会继承 Provider
        的采样参数。llama.cpp 等 OpenAI 兼容端点可能因此输出解释、代码块或
        不完整 JSON。这里仅为 Planner 的单次请求启用 JSON 模式和确定性采样；
        若端点明确不支持这些结构化参数，再兼容回退到 AstrBot 的公开接口。

        官方 DeepSeek V4 还会在该单次请求中显式关闭思考，主对话配置不变。
        """
        if self._supports_openai_json_query(provider):
            query = getattr(provider, "_query")
            model = str(provider.get_model() or "").strip()
            payloads = {
                "messages": [
                    {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "model": model,
                "max_tokens": 512,
                "response_format": {"type": "json_object"},
            }
            if self._supports_deepseek_nonthinking_query(provider):
                payloads["thinking"] = {"type": "disabled"}
            else:
                payloads["temperature"] = 0

            try:
                response = await query(payloads, None, request_max_retries=1)
            except Exception as exc:
                if not self._structured_output_not_supported(exc):
                    raise
                self.logger.warning(
                    "[星汐/Planner] Provider %s 不支持 JSON 结构化参数，"
                    "本轮已兼容回退到普通请求。",
                    self._provider_name(provider),
                )
                response = await self._request_plain_completion(provider, prompt)
        else:
            response = await self._request_plain_completion(provider, prompt)

        completion_text = str(getattr(response, "completion_text", "") or "").strip()
        if not completion_text:
            raise ValueError("planner provider returned empty completion text")
        return completion_text

    @staticmethod
    async def _request_plain_completion(provider: Any, prompt: str) -> Any:
        return await provider.text_chat(
            prompt=prompt,
            contexts=[],
            system_prompt=PLANNER_SYSTEM_PROMPT,
            func_tool=None,
            request_max_retries=1,
        )

    @classmethod
    def _supports_openai_json_query(cls, provider: Any) -> bool:
        query = getattr(provider, "_query", None)
        get_model = getattr(provider, "get_model", None)
        if not callable(query) or not callable(get_model):
            return False

        config = getattr(provider, "provider_config", {})
        provider_type = str(
            config.get("type", "") if isinstance(config, dict) else ""
        ).strip()
        return (
            provider_type == "openai_chat_completion"
            or cls._supports_deepseek_nonthinking_query(provider)
        )

    @staticmethod
    def _structured_output_not_supported(exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)

        text_candidates = [str(exc)]
        body = getattr(exc, "body", None)
        if body is not None:
            try:
                text_candidates.append(
                    json.dumps(body, ensure_ascii=False, default=str)
                )
            except Exception:
                text_candidates.append(str(body))
        text = " ".join(text_candidates).lower()
        parameter_markers = (
            "response_format",
            "json_object",
            "json mode",
            "structured output",
            "unknown field",
            "unknown parameter",
            "unsupported parameter",
            "unrecognized request argument",
        )
        return status_code in {400, 404, 422} and any(
            marker in text for marker in parameter_markers
        )

    @staticmethod
    def _supports_deepseek_nonthinking_query(provider: Any) -> bool:
        query = getattr(provider, "_query", None)
        get_model = getattr(provider, "get_model", None)
        if not callable(query) or not callable(get_model):
            return False

        model = str(get_model() or "").strip().lower()
        deepseek_models = ("deepseek-v4", "deepseek-chat", "deepseek-reasoner")
        if not any(marker in model for marker in deepseek_models):
            return False

        config = getattr(provider, "provider_config", {})
        api_base = str(config.get("api_base", "") if isinstance(config, dict) else "")
        client_base = str(getattr(getattr(provider, "client", None), "base_url", ""))
        return "api.deepseek.com" in f"{api_base} {client_base}".lower()

    @staticmethod
    def _provider_name(provider: Any) -> str:
        try:
            provider_id = str(getattr(provider.meta(), "id", "") or "").strip()
            if provider_id:
                return provider_id
        except Exception:
            pass
        provider_id = str(
            getattr(provider, "provider_config", {}).get("id", "") or ""
        ).strip()
        return provider_id or type(provider).__name__
