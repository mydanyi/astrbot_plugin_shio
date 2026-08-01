from __future__ import annotations

import asyncio
import html
import random
import re
import sys
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, ToolSet, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart

from .core.context_builder import (
    clean_contexts,
    collect_supporting_material,
    contexts_as_transcript,
    get_current_message,
)
from .core.planner import SpeechPlanner
from .core.prompts import (
    DEFAULT_ATRI_VOICE_CARD,
    build_replyer_system_prompt,
    build_retry_prompt,
)
from .core.response_guard import clean_response, find_violations, split_chat_bubbles
from .core.style_retriever import StyleRetriever


PLUGIN_NAME = "astrbot_plugin_shio"
SHIO_ACTIVE = "_shio_active"
SHIO_PAYLOAD = "_shio_retry_payload"
SHIO_PLAN = "_shio_speech_plan"
SHIO_ALLOWED_TOOLS = "_shio_allowed_readonly_tools"
SHIO_TOOL_REQUEST = "_shio_tool_request"
SHIO_IDENTITY_SCOPE = "_shio_identity_scope"


class ShioPlugin(Star):
    """用 Planner → 表达检索 → Replyer 管线接管普通角色聊天。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        assets_dir = Path(__file__).resolve().parent / "assets"
        self.planner = SpeechPlanner(logger)
        self.styles = StyleRetriever(data_dir, assets_dir, logger)
        self._refresh_provider_schema_options()

    def _config(self, key: str, default: Any) -> Any:
        value = self.config.get(key, default)
        return default if value is None else value

    def _owner_ids(self) -> set[str]:
        raw = self._config("owner_ids", [])
        if isinstance(raw, str):
            values = re.split(r"[,;\s]+", raw)
        elif isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            values = []
        return {str(item).strip() for item in values if str(item).strip()}

    @staticmethod
    def _event_value(event: AstrMessageEvent, method_name: str) -> str:
        method = getattr(event, method_name, None)
        if not callable(method):
            return ""
        try:
            return str(method() or "").strip()
        except Exception:
            return ""

    def _identity_scope(
        self,
        event: AstrMessageEvent,
        sender_id: str,
    ) -> dict[str, str]:
        platform_id = self._event_value(event, "get_platform_id")
        platform_name = self._event_value(event, "get_platform_name")
        bot_id = self._event_value(event, "get_self_id")
        group_id = self._event_value(event, "get_group_id")
        session_id = self._event_value(event, "get_session_id")
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not platform_id and umo:
            platform_id = umo.split(":", 1)[0].strip()
        chat_type = "group" if group_id else "private"
        conversation_id = group_id or session_id or sender_id or "unknown"
        platform_scope = platform_id or platform_name or "unknown"
        identity_key = (
            f"platform:{platform_scope}|bot:{bot_id or 'unknown'}|"
            f"{chat_type}:{conversation_id}|user:{sender_id or 'unknown'}"
        )
        return {
            "platform_id": platform_scope,
            "platform_name": platform_name,
            "bot_id": bot_id,
            "chat_type": chat_type,
            "group_id": group_id,
            "session_id": session_id,
            "identity_key": identity_key,
        }

    @staticmethod
    def _xml_attrs(values: dict[str, str]) -> str:
        return " ".join(
            f'{key}="{html.escape(str(value), quote=True)}"'
            for key, value in values.items()
        )

    def _guest_allowed_tool_names(self) -> list[str]:
        raw = self._config(
            "guest_allowed_tools",
            ["anysearch_search", "anysearch_extract"],
        )
        if isinstance(raw, str):
            values = re.split(r"[,;\s]+", raw)
        elif isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            values = []
        result: list[str] = []
        for item in values:
            name = str(item or "").strip()
            if name and name not in result:
                result.append(name)
        return result

    @staticmethod
    def _get_tools(tool_set: Any) -> list[Any]:
        if tool_set is None:
            return []
        tools = getattr(tool_set, "tools", None)
        if tools is None:
            tools = getattr(tool_set, "func_list", [])
        return list(tools or [])

    def _available_tools(self, request_tool_set: Any) -> list[Any]:
        """合并当前请求与 AstrBot 全局插件工具，按名称去重。"""
        candidates = self._get_tools(request_tool_set)
        try:
            manager = self.context.get_llm_tool_manager()
            global_tool_set = (
                manager.get_full_tool_set()
                if hasattr(manager, "get_full_tool_set")
                else manager
            )
            candidates.extend(self._get_tools(global_tool_set))
        except Exception as exc:
            if bool(self._config("debug_log", False)):
                logger.warning("[星汐/联网] 读取 AstrBot 全局工具集失败：%s", exc)

        tools_by_name: dict[str, Any] = {}
        for tool in candidates:
            name = str(getattr(tool, "name", "") or "").strip()
            if not name or not bool(getattr(tool, "active", True)):
                continue
            if name not in tools_by_name:
                tools_by_name[name] = tool
        return list(tools_by_name.values())

    @staticmethod
    def _get_tool_names(tool_set: Any) -> list[str]:
        if tool_set is None:
            return []
        names = getattr(tool_set, "names", None)
        if callable(names):
            try:
                return [str(name) for name in names()]
            except Exception:
                pass
        return [
            str(getattr(tool, "name", "unknown"))
            for tool in ShioPlugin._get_tools(tool_set)
        ]

    def _provider(self, provider_id: str, umo: str) -> Any:
        if provider_id.strip():
            provider = self.context.get_provider_by_id(provider_id.strip())
            if provider is not None and hasattr(provider, "text_chat"):
                return provider
            logger.warning("[星汐] 找不到聊天 Provider %s，将使用当前会话模型。", provider_id)
        return self.context.get_using_provider(umo)

    def _typed_provider(self, provider_id: str, required_method: str) -> Any:
        if not provider_id.strip():
            return None
        provider = self.context.get_provider_by_id(provider_id.strip())
        if provider is None or not hasattr(provider, required_method):
            logger.warning("[星汐] Provider %s 不支持 %s，已跳过。", provider_id, required_method)
            return None
        return provider

    @staticmethod
    def _provider_choices(providers: Any) -> tuple[list[str], list[str]]:
        options: list[str] = []
        labels: list[str] = []
        for provider in providers or []:
            try:
                meta = provider.meta()
                provider_id = str(getattr(meta, "id", "") or "").strip()
                model = str(getattr(meta, "model", "") or "").strip()
            except Exception:
                provider_id = str(
                    getattr(provider, "provider_config", {}).get("id", "") or ""
                ).strip()
                model = str(getattr(provider, "model_name", "") or "").strip()
            if not provider_id or provider_id in options:
                continue
            options.append(provider_id)
            labels.append(
                f"{provider_id} · {model}"
                if model and model != provider_id
                else provider_id
            )
        return options, labels

    def _refresh_provider_schema_options(self) -> None:
        """把已加载的向量与精排模型写入插件配置下拉框。"""
        schema = getattr(self.config, "schema", None)
        if not isinstance(schema, dict):
            return
        try:
            embedding_providers = self.context.get_all_embedding_providers()
        except Exception:
            embedding_providers = getattr(
                getattr(self.context, "provider_manager", None),
                "embedding_provider_insts",
                [],
            )
        rerank_providers = getattr(
            getattr(self.context, "provider_manager", None),
            "rerank_provider_insts",
            [],
        )
        groups = (
            (
                "embedding_provider_id",
                embedding_providers,
                "不使用 Embedding（本地文字匹配）",
            ),
            (
                "rerank_provider_id",
                rerank_providers,
                "不使用 Reranker（沿用初筛顺序）",
            ),
        )
        for key, providers, empty_label in groups:
            field = schema.get(key)
            if not isinstance(field, dict):
                continue
            options, labels = self._provider_choices(providers)
            current = str(self.config.get(key, "") or "").strip()
            if current and current not in options:
                options.append(current)
                labels.append(f"{current}（当前配置，Provider 尚未加载）")
            field["options"] = ["", *options]
            field["labels"] = [empty_label, *labels]

    @filter.on_astrbot_loaded()
    async def refresh_provider_selectors(self) -> None:
        """核心与 Provider 全部加载后再次刷新可选模型。"""
        self._refresh_provider_schema_options()

    @staticmethod
    def _starts_with_any(message: str, prefixes: Any) -> str | None:
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        for item in prefixes or []:
            prefix = str(item or "").strip()
            if prefix and message.lower().startswith(prefix.lower()):
                return prefix
        return None

    def _set_inactive(self, event: AstrMessageEvent) -> None:
        event.set_extra(SHIO_ACTIVE, False)
        event.set_extra(SHIO_PAYLOAD, None)
        event.set_extra(SHIO_TOOL_REQUEST, None)

    @filter.on_llm_request(priority=-sys.maxsize)
    async def enforce_agent_permission(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """内置权限守卫：主人保留全部工具，普通用户只保留精确白名单。"""
        if not bool(self._config("permission_guard_enabled", True)):
            return

        sender_id = str(event.get_sender_id() or "").strip()
        is_owner = bool(sender_id) and sender_id in self._owner_ids()
        identity_scope = self._identity_scope(event, sender_id)
        event.set_extra(SHIO_IDENTITY_SCOPE, identity_scope)
        configured_allowed = set(self._guest_allowed_tool_names())
        available_tools = self._available_tools(req.func_tool)
        allowed_tools = [
            tool
            for tool in available_tools
            if str(getattr(tool, "name", "")) in configured_allowed
        ]
        allowed_tool_names = [str(getattr(tool, "name", "")) for tool in allowed_tools]
        if not sender_id:
            allowed_tools = []
            allowed_tool_names = []
        event.set_extra(SHIO_ALLOWED_TOOLS, allowed_tool_names)

        removed_tool_names: list[str] = []
        if not is_owner:
            allowed_name_set = set(allowed_tool_names)
            removed_tool_names = [
                name
                for name in self._get_tool_names(req.func_tool)
                if name not in allowed_name_set
            ]
            req.func_tool = ToolSet(allowed_tools)

        if bool(self._config("inject_verified_context", True)):
            access_mode = (
                "owner_full_access"
                if is_owner
                else ("limited_read_only" if allowed_tool_names else "chat_only")
            )
            verified_values = {
                "source": "shio",
                "platform_id": identity_scope["platform_id"],
                "bot_id": identity_scope["bot_id"] or "unknown",
                "chat_type": identity_scope["chat_type"],
                "group_id": identity_scope["group_id"] or "private",
                "sender_id": sender_id or "unknown",
                "identity_key": identity_scope["identity_key"],
                "owner": "true" if is_owner else "false",
                "mode": access_mode,
            }
            if allowed_tool_names and not is_owner:
                verified_values["allowed_tools"] = ",".join(allowed_tool_names)
                verified_values["external_writes"] = "disabled"
            elif not is_owner:
                verified_values["tools"] = "disabled"
                verified_values["external_actions"] = "disabled"
            context_text = (
                "<verified_access_control "
                + self._xml_attrs(verified_values)
                + " />"
            )
            parts = req.extra_user_content_parts
            if not isinstance(parts, list):
                parts = []
                req.extra_user_content_parts = parts
            parts.append(TextPart(text=context_text).mark_as_temp())

        if (
            not is_owner
            and removed_tool_names
            and bool(self._config("permission_audit_log", True))
        ):
            preview = ", ".join(removed_tool_names[:12])
            if len(removed_tool_names) > 12:
                preview += f", ... (+{len(removed_tool_names) - 12})"
            logger.info(
                "[星汐/权限守卫] 已阻止 group_id=%s sender_id=%s 使用 %d 个工具：%s",
                identity_scope["group_id"] or "<private>",
                sender_id or "<missing>",
                len(removed_tool_names),
                preview,
            )

    # 内置权限守卫先裁决，角色回复链随后清理后台注入并构造纯聊天请求。
    @filter.on_llm_request(priority=-sys.maxsize - 100)
    async def build_persona_reply(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """在最终调用前接管普通聊天；失败时保留原请求。"""
        self._set_inactive(event)
        if not bool(self._config("enabled", True)):
            return
        if req.tool_calls_result:
            return

        sender_id = str(event.get_sender_id() or "").strip()
        sender_name = str(event.get_sender_name() or sender_id or "群友").strip()
        is_owner = bool(sender_id) and sender_id in self._owner_ids()
        identity_scope = event.get_extra(SHIO_IDENTITY_SCOPE, None)
        if not isinstance(identity_scope, dict):
            identity_scope = self._identity_scope(event, sender_id)
        current_message = get_current_message(event, req.prompt)

        if is_owner and bool(self._config("owner_task_bypass", True)):
            task_prefix = self._starts_with_any(
                current_message,
                self._config("owner_task_prefixes", ["/agent", "/task"]),
            )
            if task_prefix:
                logger.info("[星汐] 主人任务前缀命中，交还原始 Agent 链路。")
                return

        chat_prefix = None
        if is_owner:
            chat_prefix = self._starts_with_any(
                current_message,
                self._config("owner_chat_prefixes", ["/chat", "/role"]),
            )
            if chat_prefix:
                current_message = current_message[len(chat_prefix) :].lstrip(" ：:")

        max_messages = max(2, int(self._config("max_context_messages", 16)))
        max_context_chars = max(1000, int(self._config("max_context_chars", 9000)))
        clean_history = clean_contexts(
            event,
            req.contexts,
            current_message,
            max_messages,
            max_context_chars,
            group_id=str(identity_scope.get("group_id", "")),
        )
        transcript = contexts_as_transcript(clean_history)
        supporting_material = collect_supporting_material(
            req,
            max(2000, int(self._config("planner_material_chars", 9000))),
        )

        try:
            planner_provider = self._provider(
                str(self._config("planner_provider_id", "")),
                event.unified_msg_origin,
            )
            plan = await self.planner.create_plan(
                provider=planner_provider,
                sender_name=sender_name,
                sender_id=sender_id,
                platform_id=str(identity_scope.get("platform_id", "")),
                bot_id=str(identity_scope.get("bot_id", "")),
                chat_type=str(identity_scope.get("chat_type", "private")),
                group_id=str(identity_scope.get("group_id", "")),
                identity_key=str(identity_scope.get("identity_key", "")),
                is_owner=is_owner,
                current_message=current_message,
                transcript=transcript,
                supporting_material=supporting_material,
                enabled=bool(self._config("planner_enabled", True)),
            )

            allowed_tool_names = list(event.get_extra(SHIO_ALLOWED_TOOLS, []) or [])
            use_allowed_tools = bool(plan.use_allowed_tools and allowed_tool_names)
            if plan.use_allowed_tools and not allowed_tool_names:
                plan.must_include = list(plan.must_include) + [
                    "明确说明本轮没有取得联网结果，不得声称已经搜索或查过"
                ]
                plan.avoid = list(plan.avoid) + ["编造搜索结果", "假装已经联网"]
                logger.warning(
                    "[星汐/联网] 本轮需要外部资料，但白名单工具未出现在 AstrBot 工具管理器中；"
                    "sender=%s configured=%s",
                    sender_id or "<missing>",
                    ",".join(self._guest_allowed_tool_names()) or "<empty>",
                )
            if not use_allowed_tools:
                plan.use_allowed_tools = False

            if (
                is_owner
                and bool(self._config("owner_task_bypass", True))
                and not chat_prefix
                and plan.mode == "task"
            ):
                logger.info("[星汐] Planner 判定为主人任务，交还原始 Agent 链路。")
                return

            embedding_id = str(self._config("embedding_provider_id", ""))
            rerank_id = str(self._config("rerank_provider_id", ""))
            expressions = await self.styles.retrieve(
                current_message=current_message,
                plan=plan,
                embedding_provider=self._typed_provider(embedding_id, "get_embedding"),
                embedding_provider_id=embedding_id,
                rerank_provider=self._typed_provider(rerank_id, "rerank"),
                candidate_count=max(3, int(self._config("style_candidate_count", 12))),
                top_k=min(3, max(0, int(self._config("style_top_k", 3)))),
            )

            persona_name = str(self._config("persona_name", "亚托莉") or "亚托莉").strip()
            voice_card = str(self._config("voice_card", "") or "").strip()
            if not voice_card:
                voice_card = DEFAULT_ATRI_VOICE_CARD
            chat_soft_chars = max(30, int(self._config("chat_soft_chars", 100)))
            long_form_soft_chars = max(
                300,
                int(self._config("long_form_soft_chars", 1200)),
            )
            chat_max_bubbles = min(
                5,
                max(1, int(self._config("chat_max_bubbles", 3))),
            )
            replyer_system = build_replyer_system_prompt(
                persona_name=persona_name,
                voice_card=voice_card,
                sender_name=sender_name,
                sender_id=sender_id,
                platform_id=str(identity_scope.get("platform_id", "")),
                bot_id=str(identity_scope.get("bot_id", "")),
                chat_type=str(identity_scope.get("chat_type", "private")),
                group_id=str(identity_scope.get("group_id", "")),
                identity_key=str(identity_scope.get("identity_key", "")),
                is_owner=is_owner,
                plan=plan,
                expressions=expressions,
                chat_soft_chars=chat_soft_chars,
                long_form_soft_chars=long_form_soft_chars,
                chat_max_bubbles=chat_max_bubbles,
                allowed_tool_names=allowed_tool_names if use_allowed_tools else [],
            )
        except Exception as exc:
            logger.exception("[星汐] 请求准备失败，已完整回退原 AstrBot 请求：%s", exc)
            return

        # 所有可能失败的准备步骤结束后才原子式改写请求。
        req.system_prompt = replyer_system
        req.contexts = clean_history
        req.prompt = current_message or "请自然回应刚才的图片或消息。"
        req.extra_user_content_parts = []
        if use_allowed_tools:
            allowed_name_set = set(allowed_tool_names)
            req.func_tool = ToolSet(
                [
                    tool
                    for tool in self._available_tools(req.func_tool)
                    if str(getattr(tool, "name", "")) in allowed_name_set
                ]
            )
            event.set_extra(SHIO_TOOL_REQUEST, req)
            logger.info(
                "[星汐/联网] 已为 sender=%s 开放本轮只读工具：%s",
                sender_id or "<missing>",
                ", ".join(allowed_tool_names),
            )
        else:
            req.func_tool = ToolSet()
        req.tool_calls_result = None

        event.set_extra(SHIO_ACTIVE, True)
        event.set_extra(SHIO_PLAN, plan.to_dict())
        event.set_extra(
            SHIO_PAYLOAD,
            {
                "system_prompt": replyer_system,
                "contexts": clean_history,
                "prompt": req.prompt,
                "current_message": current_message,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "platform_id": str(identity_scope.get("platform_id", "")),
                "bot_id": str(identity_scope.get("bot_id", "")),
                "chat_type": str(identity_scope.get("chat_type", "private")),
                "group_id": str(identity_scope.get("group_id", "")),
                "identity_key": str(identity_scope.get("identity_key", "")),
                "is_owner": is_owner,
                "image_urls": list(req.image_urls or []),
                "audio_urls": list(req.audio_urls or []),
                "reply_shape": plan.reply_shape,
                "chat_soft_chars": chat_soft_chars,
                "long_form_soft_chars": long_form_soft_chars,
                "chat_max_bubbles": chat_max_bubbles,
            },
        )
        if bool(self._config("debug_log", False)):
            logger.info(
                "[星汐] 已接管回复 group=%s sender=%s owner=%s history=%d expressions=%d readonly_tools=%s plan=%s",
                str(identity_scope.get("group_id", "")) or "<private>",
                sender_id,
                is_owner,
                len(clean_history),
                len(expressions),
                ",".join(allowed_tool_names) if use_allowed_tools else "none",
                plan.to_dict(),
            )

    @filter.on_llm_response(priority=100)
    async def guard_persona_reply(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> None:
        """清理格式；严重违规时用无工具直连最多重写一次。"""
        if not event.get_extra(SHIO_ACTIVE, False):
            return
        payload = event.get_extra(SHIO_PAYLOAD, None)
        if not isinstance(payload, dict) or response.role == "err":
            return
        plan = event.get_extra(SHIO_PLAN, {})
        tool_request = event.get_extra(SHIO_TOOL_REQUEST, None)
        if (
            isinstance(plan, dict)
            and bool(plan.get("use_allowed_tools", False))
            and tool_request is not None
            and not getattr(tool_request, "tool_calls_result", None)
        ):
            logger.warning(
                "[星汐/联网] 当前聊天模型没有实际调用已开放的只读工具，已阻止伪造联网结果。"
            )
            response.completion_text = (
                "唔，联网模块这次没有真的返回结果……我可不能装作已经查过了。"
                "你稍后再叫我试一次吧。"
            )
            return
        original = str(response.completion_text or "")
        reply_shape = str(payload.get("reply_shape", "chat_bubbles"))
        chat_max_bubbles = max(1, int(payload.get("chat_max_bubbles", 3)))
        soft_chars = int(
            payload.get(
                "long_form_soft_chars" if reply_shape == "long_form" else "chat_soft_chars",
                1200 if reply_shape == "long_form" else 100,
            )
        )
        violations = find_violations(
            original,
            reply_shape=reply_shape,
            soft_chars=soft_chars,
            max_bubbles=chat_max_bubbles,
        )
        candidate = clean_response(original, reply_shape)

        severe = any(
            reason not in {"闲聊使用 Markdown 或列表", "闲聊气泡过多"}
            for reason in violations
        )
        if severe and bool(self._config("retry_on_violation", True)):
            try:
                provider = self._provider(
                    str(self._config("replyer_provider_id", "")),
                    event.unified_msg_origin,
                )
                if provider is not None:
                    retry = await provider.text_chat(
                        prompt=build_retry_prompt(
                            str(payload.get("current_message", "")),
                            original,
                            violations,
                            reply_shape,
                        ),
                        contexts=list(payload.get("contexts", [])),
                        system_prompt=str(payload.get("system_prompt", "")),
                        image_urls=list(payload.get("image_urls", [])),
                        audio_urls=list(payload.get("audio_urls", [])),
                        func_tool=None,
                        request_max_retries=1,
                    )
                    if str(retry.completion_text or "").strip():
                        candidate = clean_response(retry.completion_text, reply_shape)
            except Exception as exc:
                logger.warning("[星汐] 违规回复重写失败，使用本地清理结果：%s", exc)

        if reply_shape == "chat_bubbles":
            bubbles = split_chat_bubbles(candidate, chat_max_bubbles)
            final_text = "\n".join(bubbles)
        else:
            final_text = candidate
        if final_text:
            response.completion_text = final_text
        if violations and bool(self._config("debug_log", False)):
            logger.info("[星汐] 输出守卫命中：%s", "、".join(violations))

    @filter.on_decorating_result(priority=-100)
    async def dispatch_chat_bubbles(self, event: AstrMessageEvent) -> None:
        """闲聊按自然句逐条发送；内容型回答保持完整排版。"""
        if not event.get_extra(SHIO_ACTIVE, False):
            return
        if not bool(self._config("enable_chat_bubbles", True)):
            return
        plan = event.get_extra(SHIO_PLAN, {})
        if not isinstance(plan, dict) or plan.get("reply_shape") != "chat_bubbles":
            return
        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return
        try:
            if not result.is_llm_result():
                return
        except Exception:
            return

        text_components = [
            comp
            for comp in result.chain
            if isinstance(getattr(comp, "text", None), str)
        ]
        if not text_components:
            return
        visible_text = "".join(comp.text for comp in text_components).strip()
        bubbles = split_chat_bubbles(
            visible_text,
            min(5, max(1, int(self._config("chat_max_bubbles", 3)))),
        )
        if len(bubbles) <= 1:
            return

        min_delay = max(0, int(self._config("bubble_interval_min_ms", 450))) / 1000
        max_delay = max(0, int(self._config("bubble_interval_max_ms", 1200))) / 1000
        if max_delay < min_delay:
            min_delay, max_delay = max_delay, min_delay

        sent_count = 0
        for bubble in bubbles[:-1]:
            try:
                await event.send(event.plain_result(bubble))
                sent_count += 1
                if max_delay > 0:
                    await asyncio.sleep(random.uniform(min_delay, max_delay))
            except Exception as exc:
                logger.warning("[星汐] 分气泡发送失败，剩余内容改为单条发送：%s", exc)
                break

        if sent_count == 0:
            return
        remaining = bubbles[sent_count:]
        text_components[0].text = "\n".join(remaining)
        for comp in text_components[1:]:
            comp.text = ""
