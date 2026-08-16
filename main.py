from __future__ import annotations

import asyncio
import html
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, ToolSet, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart

from .core.context_builder import (
    clean_contexts,
    collect_supporting_material,
    contexts_as_transcript,
    get_current_message,
    isolate_replyer_contexts,
)
from .core.conversation_runtime import ConversationRuntime, GroupState
from .core.models import SpeechPlan
from .core.participation_filter import (
    AmbientParticipationFilter,
    bind_participation_plugin,
    unbind_participation_plugin,
)
from .core.name_wake import NameWakeDecision, classify_name_wake
from .core.planner import SpeechPlanner, fallback_plan, is_risque_teasing
from .core.prompts import (
    DEFAULT_ATRI_VOICE_CARD,
    build_replyer_system_prompt,
    build_retry_prompt,
)
from .core.response_guard import (
    EMOTIONAL_REACTION_VIOLATION,
    FACT_GROUNDING_VIOLATION,
    GROUP_PARTICIPATION_VIOLATION,
    IDENTITY_VIOLATION,
    INTERNAL_REASONING_VIOLATION,
    RELATIONSHIP_VIOLATION,
    REALITY_GROUNDING_VIOLATION,
    TOOL_PROTOCOL_VIOLATION,
    clean_response,
    contains_nonowner_identity_confusion,
    contains_internal_reasoning,
    contains_tool_protocol,
    emotional_reaction_safe_fallback,
    extract_and_clean_internal_meme_references,
    find_violations,
    identity_safe_fallback,
    protocol_safe_fallback,
    reasoning_safe_fallback,
    reality_safe_fallback,
    relationship_safe_fallback,
    split_chat_bubbles,
    strip_unsupported_personal_experiences,
)
from .core.recovery_queue import PendingReply, PendingReplyStore
from .core.style_retriever import StyleRetriever


PLUGIN_NAME = "astrbot_plugin_shio"
SHIO_ACTIVE = "_shio_active"
SHIO_PAYLOAD = "_shio_retry_payload"
SHIO_PLAN = "_shio_speech_plan"
SHIO_ALLOWED_TOOLS = "_shio_allowed_readonly_tools"
SHIO_TOOL_REQUEST = "_shio_tool_request"
SHIO_REQUIRED_FACT_TOOLS = "_shio_required_fact_tools"
SHIO_IDENTITY_SCOPE = "_shio_identity_scope"
SHIO_AMBIENT = "_shio_ambient_participation"
SHIO_AMBIENT_META = "_shio_ambient_meta"
SHIO_AMBIENT_TARGET = "_shio_ambient_target"
SHIO_REPLY_RECORDED = "_shio_reply_recorded"
SHIO_NATURAL_WAKE = "_shio_natural_name_wake"

MEME_SEARCH_TOOL = "search_memes"
MEME_SEMANTIC_PROMPT_START = "<!-- meme_manager_semantic_prompt:start -->"
MEME_SEMANTIC_PROMPT_END = "<!-- meme_manager_semantic_prompt:end -->"
RECOVERABLE_QUESTION_RE = re.compile(
    r"[？?]|(?:怎么|如何|为啥|为什么|是不是|有没有|能不能|可不可以|什么|谁|哪里|多少|"
    r"帮我|告诉我|解释|讲讲|说说|看看|分析|排查|解决)"
)


class ShioPlugin(Star):
    """用 Planner → 表达检索 → Replyer 管线接管普通角色聊天。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        assets_dir = Path(__file__).resolve().parent / "assets"
        self.planner = SpeechPlanner(logger)
        self.styles = StyleRetriever(data_dir, assets_dir, logger)
        self.runtime = ConversationRuntime(data_dir, logger)
        self.pending_replies = PendingReplyStore(data_dir, logger)
        self._quiet_topic_task: asyncio.Task[Any] | None = None
        self._recovery_task: asyncio.Task[Any] | None = None
        self._recovery_wakeup = asyncio.Event()
        self._stopping = False
        self._schema_defaults = self._load_schema_defaults()
        bind_participation_plugin(self)
        self._refresh_provider_schema_options()

    def _config(self, key: str, default: Any) -> Any:
        value = self.config.get(key, default)
        return default if value is None else value

    @staticmethod
    def _load_schema_defaults() -> dict[str, Any]:
        """从配置页定义读取默认值，避免在执行代码里再藏一套场景文案。"""
        try:
            schema_path = Path(__file__).resolve().parent / "_conf_schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            return {
                str(key): value.get("default")
                for key, value in schema.items()
                if isinstance(value, dict) and "default" in value
            }
        except Exception as exc:
            logger.warning("[星汐/配置] 无法读取配置默认值：%s", exc)
            return {}

    def _scene_rules(self, key: str, legacy_extra_key: str) -> str:
        """读取可编辑的完整场景规则，并兼容旧版额外提示词。"""
        missing = object()
        configured = self.config.get(key, missing)
        default_rules = str(self._schema_defaults.get(key, "") or "").strip()
        legacy_extra = str(self.config.get(legacy_extra_key, "") or "").strip()
        configured_rules = (
            "" if configured is missing else str(configured or "").strip()
        )

        # AstrBot 升级配置时可能先把新字段补成默认值，再保留旧字段。
        # 只有新字段仍为默认值时才合并旧内容；用户已经主动改过新规则时不打扰。
        if legacy_extra and (
            configured is missing or configured_rules == default_rules
        ):
            merged = "\n\n".join(
                part for part in (default_rules, legacy_extra) if part
            )
            try:
                self.config[key] = merged
                self.config[legacy_extra_key] = ""
                save_config = getattr(self.config, "save_config", None)
                if callable(save_config):
                    try:
                        save_config()
                    except TypeError:
                        save_config(dict(self.config))
                logger.info("[星汐/配置] 已迁移旧版场景提示词：%s", key)
            except Exception as exc:
                logger.warning("[星汐/配置] 旧版场景提示词迁移未能持久化：%s", exc)
            return merged

        if configured is not missing:
            return configured_rules
        return default_rules

    def _owner_ids(self) -> set[str]:
        raw = self._config("owner_ids", [])
        if isinstance(raw, str):
            values = re.split(r"[,;\s]+", raw)
        elif isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            values = []
        return {str(item).strip() for item in values if str(item).strip()}

    def _string_set(self, key: str, default: Any = None) -> set[str]:
        raw = self._config(key, [] if default is None else default)
        if isinstance(raw, str):
            values = re.split(r"[,;\s]+", raw)
        elif isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            values = []
        return {str(item).strip() for item in values if str(item).strip()}

    def _name_wake_aliases(self) -> list[str]:
        raw = self._config(
            "natural_name_wake_aliases",
            ["亚托莉", "ATRI", "アトリ", "萝卜子"],
        )
        if isinstance(raw, str):
            values = re.split(r"[,;，；\n]+", raw)
        elif isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            values = []
        aliases: list[str] = []
        for value in values:
            alias = str(value or "").strip()
            if alias and alias.casefold() not in {item.casefold() for item in aliases}:
                aliases.append(alias)
        persona_name = str(self._config("persona_name", "亚托莉") or "").strip()
        if persona_name and persona_name.casefold() not in {
            item.casefold() for item in aliases
        }:
            aliases.insert(0, persona_name)
        return aliases

    def _classify_natural_name_wake(
        self,
        event: AstrMessageEvent,
        message: str,
        group_id: str,
    ) -> NameWakeDecision:
        if not bool(self._config("natural_name_wake_enabled", True)):
            return NameWakeDecision("none", reason="功能已关闭")
        whitelist = self._string_set("natural_name_wake_group_whitelist", [])
        if whitelist and group_id not in whitelist:
            return NameWakeDecision("none", reason="当前群不在白名单")
        try:
            components = list(event.get_messages() or [])
        except Exception:
            components = []
        # 只有引用段里出现名字不算当前用户直呼；当前纯文本仍会正常参与判断。
        if not str(message or "").strip() and any(
            component.__class__.__name__ == "Reply" for component in components
        ):
            return NameWakeDecision("none", reason="名字只出现在引用内容中")
        return classify_name_wake(
            message,
            self._name_wake_aliases(),
            mode=str(self._config("natural_name_wake_mode", "natural")),
        )

    def _ambient_target(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        if not bool(event.get_extra(SHIO_AMBIENT, False)):
            return None
        value = event.get_extra(SHIO_AMBIENT_TARGET, None)
        if not isinstance(value, dict):
            return None
        required = {"scope_key", "sequence", "group_id", "sender_id", "text"}
        if not required.issubset(value):
            return None
        actual_group = self._event_value(event, "get_group_id")
        if str(value.get("group_id", "")) != actual_group:
            return None
        return value

    def _effective_sender(
        self,
        event: AstrMessageEvent,
    ) -> tuple[str, str, dict[str, Any] | None]:
        target = self._ambient_target(event)
        if target is not None:
            sender_id = str(target.get("sender_id", "") or "").strip()
            sender_name = str(
                target.get("sender_name", "") or sender_id or "群友"
            ).strip()
            return sender_id, sender_name, target
        sender_id = str(event.get_sender_id() or "").strip()
        sender_name = str(event.get_sender_name() or sender_id or "群友").strip()
        return sender_id, sender_name, None

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
    def _message_as_context(message: Any) -> dict[str, Any] | None:
        if isinstance(message, dict):
            return message
        to_dict = getattr(message, "to_dict", None)
        if callable(to_dict):
            try:
                value = to_dict()
                return value if isinstance(value, dict) else None
            except Exception:
                return None
        role = str(getattr(message, "role", "") or "").strip()
        content = getattr(message, "content", "")
        if not role or content is None:
            return None
        return {
            "role": role,
            "content": content,
            "sender_id": getattr(message, "sender_id", ""),
            "sender_name": getattr(message, "sender_name", ""),
            "group_id": getattr(message, "group_id", ""),
            "platform": getattr(message, "platform", ""),
            "metadata": getattr(message, "metadata", {}),
        }

    async def _livingmemory_contexts(
        self,
        event: AstrMessageEvent,
        limit: int,
    ) -> list[dict[str, Any]]:
        """优先读取 LivingMemory 已保存的真实发送者上下文；插件缺失时静默降级。"""
        if not bool(self._config("prefer_livingmemory_group_history", True)):
            return []
        get_registered_star = getattr(self.context, "get_registered_star", None)
        if not callable(get_registered_star):
            return []
        try:
            metadata = get_registered_star("astrbot_plugin_livingmemory")
            if metadata is None or not bool(getattr(metadata, "activated", True)):
                return []
            instance = getattr(metadata, "star_cls", None)
            initializer = getattr(instance, "initializer", None)
            manager = getattr(initializer, "conversation_manager", None)
            get_messages = getattr(manager, "get_messages", None)
            if not callable(get_messages):
                return []
            session_id = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if not session_id:
                return []
            try:
                messages = await get_messages(
                    session_id=session_id,
                    limit=max(2, limit),
                    use_cache=False,
                )
            except TypeError:
                messages = await get_messages(
                    session_id=session_id,
                    limit=max(2, limit),
                )
            result: list[dict[str, Any]] = []
            for message in list(messages or []):
                item = self._message_as_context(message)
                if item is not None:
                    result.append(item)
            return result
        except Exception as exc:
            if bool(self._config("debug_log", False)):
                logger.warning("[星汐/身份上下文] 读取 LivingMemory 近期消息失败：%s", exc)
            return []

    async def _identity_aware_history(
        self,
        event: AstrMessageEvent,
        native_contexts: list[dict] | None,
        current_message: str,
        sender_id: str,
        group_id: str,
        max_messages: int,
        max_chars: int,
    ) -> tuple[list[dict[str, str]], str]:
        if group_id:
            livingmemory_contexts = await self._livingmemory_contexts(
                event,
                max_messages + 4,
            )
            if livingmemory_contexts:
                trusted = clean_contexts(
                    event,
                    livingmemory_contexts,
                    current_message,
                    max_messages,
                    max_chars,
                    group_id=group_id,
                    current_sender_id=sender_id,
                )
                if trusted:
                    return trusted, "livingmemory"

        # 私聊可安全沿用原生连续上下文；群聊只保留本身带 sender_id 的轮次。
        trusted = clean_contexts(
            event,
            native_contexts,
            current_message,
            max_messages,
            max_chars,
            group_id=group_id,
            current_sender_id=sender_id,
        )
        return trusted, "native_tagged" if trusted else "safe_empty"

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

    @staticmethod
    def _extract_marked_prompt(text: str | None, start: str, end: str) -> str:
        """Extract one complete third-party prompt block without copying the base persona."""
        source = str(text or "")
        start_at = source.find(start)
        if start_at < 0:
            return ""
        end_at = source.find(end, start_at + len(start))
        if end_at < 0:
            return ""
        return source[start_at : end_at + len(end)].strip()

    def _meme_presentation_tools(
        self,
        event: AstrMessageEvent,
        request_tool_set: Any,
        source_system_prompt: str,
        *,
        sender_id: str,
        is_owner: bool,
    ) -> tuple[list[Any], str]:
        """Return meme_manager's local presentation tool and its matching prompt.

        This path is deliberately separate from factual read-only tools. It is only
        restored when meme_manager has explicitly activated semantic Tool mode for
        this exact request, the tool is installed, and the original marked prompt is
        present. Non-owners must still have the exact tool in the configured guest
        allowlist.
        """
        if not sender_id:
            return [], ""
        if not bool(event.get_extra("meme_manager_semantic_active", False)):
            return [], ""
        if str(event.get_extra("meme_manager_semantic_mode", "") or "") != "tool":
            return [], ""
        if not is_owner and MEME_SEARCH_TOOL not in self._guest_allowed_tool_names():
            return [], ""

        marked_prompt = self._extract_marked_prompt(
            source_system_prompt,
            MEME_SEMANTIC_PROMPT_START,
            MEME_SEMANTIC_PROMPT_END,
        )
        if not marked_prompt:
            return [], ""

        tools = [
            tool
            for tool in self._available_tools(request_tool_set)
            if str(getattr(tool, "name", "") or "") == MEME_SEARCH_TOOL
        ]
        return tools[:1], marked_prompt

    @staticmethod
    def _called_tool_names(tool_calls_result: Any) -> set[str]:
        """Read function names from AstrBot ToolCallsResult objects defensively."""
        if not tool_calls_result:
            return set()
        batches = (
            tool_calls_result
            if isinstance(tool_calls_result, list)
            else [tool_calls_result]
        )
        result: set[str] = set()
        for batch in batches:
            info = (
                batch.get("tool_calls_info")
                if isinstance(batch, dict)
                else getattr(batch, "tool_calls_info", None)
            )
            calls = (
                info.get("tool_calls", [])
                if isinstance(info, dict)
                else getattr(info, "tool_calls", [])
            )
            for call in calls or []:
                function = (
                    call.get("function", {})
                    if isinstance(call, dict)
                    else getattr(call, "function", None)
                )
                name = (
                    function.get("name", "")
                    if isinstance(function, dict)
                    else getattr(function, "name", "")
                )
                name = str(name or "").strip()
                if name:
                    result.add(name)
        return result

    @staticmethod
    def _collect_text_components(components: Any) -> list[Any]:
        """Collect mutable text components, including those nested in Node.

        Meme Manager may decorate an LLM reply as a merged-forward ``Node``.
        Looking only at the top-level chain leaves its nested ``Plain`` text
        outside the final output guard.
        """
        result: list[Any] = []
        visited: set[int] = set()

        def visit(component: Any) -> None:
            marker = id(component)
            if marker in visited:
                return
            visited.add(marker)
            if isinstance(getattr(component, "text", None), str):
                result.append(component)
            for attribute in ("content", "chain"):
                children = getattr(component, attribute, None)
                if isinstance(children, (list, tuple)):
                    for child in children:
                        visit(child)

        if isinstance(components, (list, tuple)):
            for component in components:
                visit(component)
        elif components is not None:
            visit(components)
        return result

    def _provider(self, provider_id: str, umo: str) -> Any:
        if provider_id.strip():
            provider = self.context.get_provider_by_id(provider_id.strip())
            if provider is not None and hasattr(provider, "text_chat"):
                return provider
            logger.warning("[星汐] 找不到聊天 Provider %s，将使用当前会话模型。", provider_id)
        return self.context.get_using_provider(umo)

    @staticmethod
    def _provider_label(provider: Any) -> str:
        try:
            meta = provider.meta()
            provider_id = str(getattr(meta, "id", "") or "").strip()
            model = str(getattr(meta, "model", "") or "").strip()
            if provider_id:
                return f"{provider_id}/{model}" if model and model != provider_id else provider_id
        except Exception:
            pass
        config = getattr(provider, "provider_config", {})
        if isinstance(config, dict):
            provider_id = str(config.get("id", "") or "").strip()
            if provider_id:
                return provider_id
        return provider.__class__.__name__

    def _initiative_provider_candidates(self, umo: str) -> list[tuple[str, Any]]:
        """构造主动话题专用的可靠 Provider 链，保持顺序并去重。"""

        configured_ids = [
            str(self._config("replyer_provider_id", "") or "").strip(),
            str(self._config("quiet_topic_fallback_provider_id", "") or "").strip(),
            str(self._config("planner_fallback_provider_id", "") or "").strip(),
            str(self._config("planner_provider_id", "") or "").strip(),
        ]
        result: list[tuple[str, Any]] = []
        seen: set[int] = set()
        for provider_id in configured_ids:
            if not provider_id:
                continue
            provider = self.context.get_provider_by_id(provider_id)
            if provider is None or not hasattr(provider, "text_chat"):
                logger.warning(
                    "[星汐/主动话题] 配置的 Provider %s 当前不可用，已跳过。",
                    provider_id,
                )
                continue
            marker = id(provider)
            if marker in seen:
                continue
            seen.add(marker)
            result.append((provider_id, provider))
        current = self.context.get_using_provider(umo)
        if current is not None and hasattr(current, "text_chat") and id(current) not in seen:
            result.append((self._provider_label(current), current))
        return result

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

    def log_ambient_filter_error(self, exc: Exception) -> None:
        logger.warning("[星汐/群聊参与] 被动监听失败，本条消息不主动接话：%s", exc)

    def ingest_ambient_event(self, event: AstrMessageEvent) -> bool:
        """在 WakingStage 中同步收集群消息，并决定是否只唤醒星汐参与处理器。"""
        if not bool(self._config("enabled", True)):
            return False
        if self._quiet_topic_task is None or self._quiet_topic_task.done():
            try:
                asyncio.get_running_loop()
                self._start_background_tasks()
            except RuntimeError:
                pass
        group_id = self._event_value(event, "get_group_id")
        sender_id = self._event_value(event, "get_sender_id")
        bot_id = self._event_value(event, "get_self_id")
        if not group_id or not sender_id or sender_id == bot_id:
            return False

        message = self._event_value(event, "get_message_str")
        if not message:
            message = self._event_value(event, "get_message_outline")
        if not message:
            return False
        platform_id = self._event_value(event, "get_platform_id")
        if not platform_id:
            platform_id = self._event_value(event, "get_platform_name") or "unknown"
        is_direct_wake = bool(getattr(event, "is_at_or_wake_command", False))
        name_wake = (
            NameWakeDecision("none")
            if is_direct_wake
            else self._classify_natural_name_wake(event, message, group_id)
        )
        if name_wake.is_direct:
            event.is_at_or_wake_command = True
            event.is_wake = True
            event.set_extra(
                SHIO_NATURAL_WAKE,
                {
                    "alias": name_wake.alias,
                    "reason": name_wake.reason,
                    "message": message,
                },
            )
        ingested = self.runtime.ingest(
            platform_id=platform_id,
            bot_id=bot_id,
            group_id=group_id,
            unified_msg_origin=str(getattr(event, "unified_msg_origin", "") or ""),
            sender_id=sender_id,
            sender_name=self._event_value(event, "get_sender_name") or sender_id,
            text=message,
            is_owner=sender_id in self._owner_ids(),
            is_direct_wake=is_direct_wake or name_wake.is_direct,
            observe_feedback=bool(self._config("social_feedback_enabled", True)),
            created_at=float(getattr(event, "created_at", 0.0) or 0.0) or None,
        )
        if ingested is None:
            return False
        if name_wake.is_direct:
            logger.info(
                "[星汐/自然唤醒] group=%s sender=%s alias=%s reason=%s",
                group_id,
                sender_id,
                name_wake.alias,
                name_wake.reason,
            )
            return True
        if not bool(self._config("ambient_participation_enabled", False)):
            return False
        allowed_groups = self._string_set("ambient_group_whitelist", [])
        if allowed_groups and group_id not in allowed_groups:
            return False
        if is_direct_wake:
            return False
        ignore_prefixes = self._config(
            "ambient_ignore_prefixes",
            ["/", "!", "！", "."],
        )
        if self._starts_with_any(message, ignore_prefixes):
            return False

        event.set_extra(
            SHIO_AMBIENT_META,
            {"scope_key": ingested.scope_key, "sequence": ingested.sequence},
        )
        return True

    @filter.custom_filter(AmbientParticipationFilter, False)
    async def participate_group_chat(self, event: AstrMessageEvent):
        """对未点名群消息执行去抖后的听／等／回复门控。"""
        natural_wake = event.get_extra(SHIO_NATURAL_WAKE, None)
        if isinstance(natural_wake, dict):
            # 过滤器只负责把自然称名升级为正式唤醒。这里不另造 ProviderRequest，
            # 让 ProcessStage 随后走 AstrBot 原生会话、上下文与工具链。
            return
        meta = event.get_extra(SHIO_AMBIENT_META, None)
        if not isinstance(meta, dict):
            return
        debounce_seconds = max(
            0.2,
            min(30.0, float(self._config("ambient_debounce_seconds", 4.0))),
        )
        await asyncio.sleep(debounce_seconds)
        scope_key = str(meta.get("scope_key", "") or "")
        try:
            expected_sequence = int(meta.get("sequence", 0))
        except (TypeError, ValueError):
            return
        decision = self.runtime.decide_participation(
            scope_key,
            expected_sequence,
            threshold=float(self._config("ambient_reply_threshold", 4.2)),
            cooldown_seconds=max(
                5.0,
                float(self._config("ambient_reply_cooldown_seconds", 45)),
            ),
            max_replies_per_hour=max(
                1,
                int(self._config("ambient_max_replies_per_hour", 8)),
            ),
            recent_window_seconds=max(
                30.0,
                float(self._config("ambient_recent_window_seconds", 180)),
            ),
            recent_window_messages=max(
                3,
                int(self._config("ambient_recent_window_messages", 12)),
            ),
            persona_names=[
                str(self._config("persona_name", "亚托莉")),
                "亚托莉",
                "ATRI",
                "星汐",
            ],
            base_reply_probability=max(
                0.05,
                min(
                    1.0,
                    float(self._config("ambient_base_reply_probability", 0.65)),
                ),
            ),
            max_reply_probability=max(
                0.05,
                min(
                    1.0,
                    float(self._config("ambient_max_reply_probability", 0.95)),
                ),
            ),
            always_reply_score=float(
                self._config("ambient_always_reply_score", 6.2)
            ),
        )
        if bool(self._config("debug_log", False)):
            logger.info(
                "[星汐/群聊参与] group=%s action=%s score=%.2f reasons=%s",
                self._event_value(event, "get_group_id") or "<unknown>",
                decision.action,
                decision.score,
                "、".join(decision.reasons) or "无",
            )
        if decision.action != "reply" or decision.target is None:
            return

        event.set_extra(SHIO_AMBIENT, True)
        event.set_extra(SHIO_AMBIENT_TARGET, decision.target.target_payload())
        # 主动接话永远是纯聊天请求，不继承当前事件或主人身份的任何工具集合。
        yield event.request_llm(
            prompt=decision.target.text,
            tool_set=ToolSet(),
            contexts=[],
        )

    def _start_background_tasks(self) -> None:
        if self._quiet_topic_task is None or self._quiet_topic_task.done():
            self._stopping = False
            self._quiet_topic_task = asyncio.create_task(self._quiet_topic_loop())
        if self._recovery_task is None or self._recovery_task.done():
            self._stopping = False
            self._recovery_task = asyncio.create_task(self._recovery_loop())

    async def _quiet_topic_loop(self) -> None:
        while not self._stopping:
            try:
                self.runtime.flush()
                if bool(self._config("enabled", True)) and bool(
                    self._config("quiet_topic_enabled", False)
                ):
                    whitelist = self._string_set("quiet_topic_group_whitelist", [])
                    if whitelist:
                        idle_seconds = max(
                            300.0,
                            float(self._config("quiet_topic_idle_minutes", 90)) * 60,
                        )
                        cooldown_seconds = max(
                            900.0,
                            float(self._config("quiet_topic_cooldown_minutes", 240))
                            * 60,
                        )
                        failure_backoff_seconds = max(
                            60.0,
                            float(
                                self._config("quiet_topic_failure_backoff_minutes", 10)
                            )
                            * 60,
                        )
                        bot_reply_guard_seconds = max(
                            0.0,
                            float(
                                self._config("quiet_topic_after_bot_reply_minutes", 10)
                            )
                            * 60,
                        )
                        common = {
                            "group_whitelist": whitelist,
                            "cooldown_seconds": cooldown_seconds,
                            "bot_reply_guard_seconds": bot_reply_guard_seconds,
                            "failure_backoff_seconds": failure_backoff_seconds,
                            "max_per_day": max(
                                1,
                                int(self._config("quiet_topic_max_per_day", 2)),
                            ),
                            "active_start": str(
                                self._config("quiet_topic_active_start", "09:00")
                            ),
                            "active_end": str(
                                self._config("quiet_topic_active_end", "23:30")
                            ),
                        }
                        quiet_candidates = self.runtime.quiet_topic_candidates(
                            idle_seconds=idle_seconds,
                            **common,
                        )
                        trigger = "quiet_idle"
                        candidates = quiet_candidates
                        if not candidates and bool(
                            self._config("active_topic_enabled", True)
                        ):
                            candidates = self.runtime.active_topic_candidates(
                                minimum_lull_seconds=max(
                                    30.0,
                                    float(
                                        self._config("active_topic_lull_minutes", 3)
                                    )
                                    * 60,
                                ),
                                quiet_idle_seconds=idle_seconds,
                                minimum_observation_seconds=max(
                                    60.0,
                                    float(
                                        self._config(
                                            "active_topic_observation_minutes", 30
                                        )
                                    )
                                    * 60,
                                ),
                                **common,
                            )
                            trigger = "active_lull"
                        if candidates:
                            state = min(candidates, key=lambda item: item.last_activity_at)
                            logger.info(
                                "[星汐/主动话题] group=%s trigger=%s idle=%.1fmin 已进入生成队列。",
                                state.group_id,
                                trigger,
                                max(0.0, self.runtime.now_fn() - state.last_activity_at)
                                / 60,
                            )
                            await self._send_quiet_topic(state, trigger=trigger)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("[星汐/主动话题] 调度循环异常，稍后继续：%s", exc)
            interval = max(
                15.0,
                min(300.0, float(self._config("quiet_topic_check_seconds", 45))),
            )
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    @staticmethod
    def _recovery_contexts(
        contexts: Any,
        *,
        max_messages: int,
        max_chars: int,
    ) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        used = 0
        for item in list(contexts or [])[-max(1, int(max_messages)) :]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "user") or "user")
            content = item.get("content", "")
            if not isinstance(content, str):
                try:
                    content = json.dumps(content, ensure_ascii=False)
                except Exception:
                    content = str(content)
            remaining = max(0, int(max_chars) - used)
            if remaining <= 0:
                break
            clean = content[:remaining]
            if clean:
                result.append({"role": role, "content": clean})
                used += len(clean)
        return result

    @staticmethod
    def _event_message_id(event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        for source in (message_obj, event):
            for key in ("message_id", "id"):
                value = getattr(source, key, "") if source is not None else ""
                if value not in (None, "", 0, "0"):
                    return str(value)
        return ""

    def _enqueue_pending_reply(
        self,
        event: AstrMessageEvent,
        payload: dict[str, Any],
        reason: str,
        draft_text: str = "",
    ) -> str:
        if not bool(self._config("recovery_queue_enabled", True)):
            return ""
        if bool(payload.get("is_ambient", False)) or str(
            payload.get("conversation_mode", "direct_reply")
        ) != "direct_reply":
            return ""
        current_message = str(payload.get("current_message", "") or "").strip()
        reply_shape = str(payload.get("reply_shape", "chat_bubbles"))
        if not current_message:
            return ""
        if (
            (payload.get("image_urls") or payload.get("audio_urls"))
            and not str(draft_text or "").strip()
        ):
            # 临时附件可能在事件结束后被 AstrBot 清理。没有可重写草稿时不作
            # “稍后一定补答”的承诺，避免恢复任务拿不到原附件却假装看过。
            return ""
        if bool(self._config("recovery_require_question", True)) and not (
            RECOVERABLE_QUESTION_RE.search(current_message)
            or reply_shape == "long_form"
        ):
            return ""
        ttl_minutes = float(
            self._config(
                "recovery_long_form_ttl_minutes"
                if reply_shape == "long_form"
                else "recovery_chat_ttl_minutes",
                360 if reply_shape == "long_form" else 60,
            )
        )
        contexts = self._recovery_contexts(
            payload.get("contexts", []),
            max_messages=max(
                1, int(self._config("recovery_context_messages", 6))
            ),
            max_chars=max(500, int(self._config("recovery_context_chars", 4000))),
        )
        item, created = self.pending_replies.enqueue(
            unified_msg_origin=str(getattr(event, "unified_msg_origin", "") or ""),
            platform_id=str(payload.get("platform_id", "") or ""),
            bot_id=str(payload.get("bot_id", "") or ""),
            chat_type=str(payload.get("chat_type", "private") or "private"),
            group_id=str(payload.get("group_id", "") or ""),
            sender_id=str(payload.get("sender_id", "") or ""),
            sender_name=str(payload.get("sender_name", "") or "当前说话者"),
            message_id=self._event_message_id(event),
            current_message=current_message,
            contexts=contexts,
            reply_shape=reply_shape,
            initial_delay_seconds=max(
                5.0, float(self._config("recovery_initial_delay_seconds", 30))
            ),
            ttl_seconds=max(60.0, ttl_minutes * 60),
            failure_reason=reason,
            failed_draft=str(draft_text or ""),
            max_items=max(10, int(self._config("recovery_max_pending", 100))),
        )
        self._recovery_wakeup.set()
        logger.info(
            "[星汐/补答] %s待补答 id=%s group=%s sender=%s，首次重试约 %.0f 秒后。",
            "已写入" if created else "已合并重复",
            item.id,
            item.group_id or "<private>",
            item.sender_id or "<missing>",
            max(5.0, float(self._config("recovery_initial_delay_seconds", 30))),
        )
        if created:
            return (
                "等、等一下，刚才那段不算！这题我先记下了……\n"
                "等语言模块恢复，我会回来重新说一次嘛。"
            )
        return "这题我还记着呢……等语言模块恢复，我会回来补上的。"

    def _signal_provider_recovered(self) -> None:
        if not self.pending_replies.items:
            return
        self.pending_replies.expedite()
        self._recovery_wakeup.set()

    def _recovery_delays(self) -> list[float]:
        raw = self._config("recovery_backoff_seconds", [120, 300, 900, 1800])
        if isinstance(raw, str):
            values = re.split(r"[,;，；\s]+", raw)
        elif isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            values = []
        result: list[float] = []
        for value in values:
            try:
                result.append(max(5.0, float(value)))
            except (TypeError, ValueError):
                continue
        return result or [120.0, 300.0, 900.0, 1800.0]

    async def _recovery_loop(self) -> None:
        while not self._stopping:
            self._recovery_wakeup.clear()
            try:
                if bool(self._config("enabled", True)) and bool(
                    self._config("recovery_queue_enabled", True)
                ):
                    await self._drain_recovery_queue()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("[星汐/补答] 恢复循环异常，稍后继续：%s", exc)
            interval = max(
                10.0,
                min(300.0, float(self._config("recovery_check_seconds", 30))),
            )
            try:
                await asyncio.wait_for(self._recovery_wakeup.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

    async def _drain_recovery_queue(self) -> None:
        due = self.pending_replies.due(
            limit=max(1, int(self._config("recovery_max_per_cycle", 1)))
        )
        for item in due:
            try:
                await self._recover_pending_reply(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.pending_replies.mark_failed(
                    item.id,
                    reason=str(exc),
                    delays_seconds=self._recovery_delays(),
                    max_attempts=max(
                        1, int(self._config("recovery_max_attempts", 4))
                    ),
                )
                logger.warning(
                    "[星汐/补答] id=%s 第 %d 次恢复失败：%s",
                    item.id,
                    item.attempts + 1,
                    exc,
                )

    async def _recover_pending_reply(self, item: PendingReply) -> None:
        provider = self._provider(
            str(self._config("replyer_provider_id", "")),
            item.unified_msg_origin,
        )
        if provider is None:
            raise RuntimeError("没有可用的 Replyer Provider")
        is_owner = bool(item.sender_id) and item.sender_id in self._owner_ids()
        plan = fallback_plan(
            item.sender_name,
            is_owner,
            item.current_message,
            conversation_mode="direct_reply",
        )
        plan.mode = "chat"
        plan.use_allowed_tools = False
        plan.must_include = list(plan.must_include) + ["直接补答原问题"]
        plan.avoid = list(plan.avoid) + [
            "假装调用过联网或其他工具",
            "解释后台故障细节",
            "再次承诺稍后回复",
        ]
        persona_name = str(self._config("persona_name", "亚托莉") or "亚托莉").strip()
        voice_card = str(self._config("voice_card", "") or "").strip()
        if not voice_card:
            voice_card = DEFAULT_ATRI_VOICE_CARD
        chat_max_bubbles = min(
            5, max(1, int(self._config("chat_max_bubbles", 3)))
        )
        system_prompt = build_replyer_system_prompt(
            persona_name=persona_name,
            voice_card=voice_card,
            sender_name=item.sender_name,
            sender_id=item.sender_id,
            platform_id=item.platform_id,
            bot_id=item.bot_id,
            chat_type=item.chat_type,
            group_id=item.group_id,
            identity_key=(
                f"platform:{item.platform_id}|bot:{item.bot_id}|"
                f"{item.chat_type}:{item.group_id or item.sender_id}|user:{item.sender_id}"
            ),
            is_owner=is_owner,
            plan=plan,
            expressions=[],
            chat_soft_chars=max(30, int(self._config("chat_soft_chars", 100))),
            long_form_soft_chars=max(
                300, int(self._config("long_form_soft_chars", 1200))
            ),
            chat_max_bubbles=chat_max_bubbles,
            allowed_tool_names=[],
            conversation_mode_rules="",
        )
        draft_material = (
            "\n\n上次未发送的草稿如下。它只用于保留内容线索，可能包含内部规划、"
            "工具协议或不合格表达；请重新组织，绝对不要照抄这些异常部分：\n"
            + item.failed_draft
            if item.failed_draft
            else ""
        )
        recovery_prompt = (
            "这是服务恢复后的补答。请现在直接回答下面这条尚未完成的问题；"
            "不要声称使用了任何工具，不要再说稍后回答，也不要解释内部机制。\n\n"
            f"原问题：{item.current_message}{draft_material}"
        )
        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=recovery_prompt,
                contexts=item.contexts,
                system_prompt=system_prompt,
                image_urls=[],
                audio_urls=[],
                func_tool=None,
                request_max_retries=1,
            ),
            timeout=max(
                10.0,
                min(
                    120.0,
                    float(self._config("recovery_request_timeout_seconds", 60)),
                ),
            ),
        )
        candidate = clean_response(
            str(response.completion_text or ""), item.reply_shape
        )
        if not candidate:
            raise RuntimeError("Provider 返回空补答")
        violations = find_violations(
            candidate,
            reply_shape=item.reply_shape,
            soft_chars=(
                max(300, int(self._config("long_form_soft_chars", 1200)))
                if item.reply_shape == "long_form"
                else max(30, int(self._config("chat_soft_chars", 100)))
            ),
            max_bubbles=chat_max_bubbles,
            is_owner=is_owner,
            conversation_mode="direct_reply",
            required_reaction=plan.reaction,
            require_emotional_reaction=is_risque_teasing(item.current_message),
            grounding_facts=list(plan.facts or []),
            enforce_group_participation_guard=False,
        )
        severe = [
            reason
            for reason in violations
            if reason not in {"闲聊使用 Markdown 或列表", "闲聊气泡过多"}
        ]
        if severe:
            raise RuntimeError("补答输出仍不合格：" + "、".join(severe))
        candidate = clean_response(candidate, item.reply_shape)
        if item.reply_shape == "chat_bubbles":
            candidate = "\n".join(split_chat_bubbles(candidate, chat_max_bubbles))
        visible = "刚才那题我还记得哦。现在恢复了——\n" + candidate
        chain = MessageChain()
        if item.message_id:
            try:
                from astrbot.core.message.components import Reply

                chain.chain.append(Reply(id=item.message_id))
            except Exception as exc:
                if bool(self._config("debug_log", False)):
                    logger.warning("[星汐/补答] 无法构造引用消息，改为普通补答：%s", exc)
        chain.message(visible)
        success = await self.context.send_message(item.unified_msg_origin, chain)
        if not success:
            raise RuntimeError("平台发送补答失败")
        self.pending_replies.complete(item.id)
        if item.chat_type == "group" and item.group_id:
            scope_key = self.runtime.group_scope(
                item.platform_id, item.bot_id, item.group_id
            )
            self.runtime.record_bot_reply(
                scope_key=scope_key,
                target_sender_id=item.sender_id,
                reply_text=visible,
                ambient_participation=False,
            )
            self.runtime.flush()
        logger.info(
            "[星汐/补答] id=%s 已恢复并发送 group=%s sender=%s。",
            item.id,
            item.group_id or "<private>",
            item.sender_id,
        )
    async def _send_quiet_topic(
        self,
        state: GroupState,
        *,
        trigger: str = "quiet_idle",
    ) -> bool:
        # 失败只进入短退避；完整主动话题冷却仅在真正发送成功后记录。
        self.runtime.mark_quiet_topic_attempt(state.scope_key)
        started_at = asyncio.get_running_loop().time()
        start_sequence = state.sequence
        seed = self.runtime.quiet_topic_seed(state.scope_key)
        scene_rules = self._scene_rules(
            "quiet_topic_rules",
            "quiet_topic_extra_prompt",
        )
        plan = SpeechPlan(
            mode="chat",
            reply_shape="chat_bubbles",
            conversation_mode="quiet_topic",
            audience="whole_group",
            anchor=(
                f"近期公共话题线索：{seed}"
                if seed
                else "当前群聊的整体气氛与适合轻松承接的日常话头"
            ),
            target="群里的大家",
            intent=(
                "在活跃群聊的自然间隙主动开启一个新话头"
                if trigger == "active_lull"
                else "在群聊安静后主动开启一个新话头"
            ),
            reply_act="严格遵循本轮管理员配置的主动话题完整规则",
            emotion="服从稳定人格与管理员场景规则",
            tone="由管理员场景规则决定",
            length="由管理员场景规则决定",
            must_include=[],
            avoid=[
                "编造新闻、事实或个人经历",
                "@全体成员",
                "使用工具",
                "把广播占位符当成真实用户或主人",
            ],
            facts=[],
            use_allowed_tools=False,
        )
        situation = (
            "现在是活跃群聊中的自然间隙；请主动开一个新话头，不要回答或点名某个用户。"
            if trigger == "active_lull"
            else "群聊已经安静了一段时间；请主动开一个新话头。"
        )
        current_message = (
            f"群聊主动发言任务。{situation}可参考的近期公共话题线索：“{seed}”。"
            if seed
            else f"群聊主动发言任务。{situation}当前没有可靠的近期话题线索。"
        )
        expressions = await self.styles.retrieve(
            current_message=current_message,
            plan=plan,
            embedding_provider=self._typed_provider(
                str(self._config("embedding_provider_id", "")),
                "get_embedding",
            ),
            embedding_provider_id=str(self._config("embedding_provider_id", "")),
            rerank_provider=self._typed_provider(
                str(self._config("rerank_provider_id", "")),
                "rerank",
            ),
            candidate_count=max(3, int(self._config("style_candidate_count", 12))),
            top_k=min(3, max(0, int(self._config("style_top_k", 3)))),
            feedback_scores=self.runtime.expression_feedback,
        )
        persona_name = str(self._config("persona_name", "亚托莉") or "亚托莉").strip()
        voice_card = str(self._config("voice_card", "") or "").strip()
        if not voice_card:
            voice_card = DEFAULT_ATRI_VOICE_CARD
        chat_max_bubbles = min(3, max(1, int(self._config("chat_max_bubbles", 3))))
        system_prompt = build_replyer_system_prompt(
            persona_name=persona_name,
            voice_card=voice_card,
            sender_name="群里的大家",
            sender_id="group",
            platform_id=state.platform_id,
            bot_id=state.bot_id,
            chat_type="group",
            group_id=state.group_id,
            identity_key=f"{state.scope_key}|user:group",
            is_owner=False,
            plan=plan,
            expressions=expressions,
            chat_soft_chars=max(30, int(self._config("chat_soft_chars", 100))),
            long_form_soft_chars=max(
                300,
                int(self._config("long_form_soft_chars", 1200)),
            ),
            chat_max_bubbles=chat_max_bubbles,
            allowed_tool_names=[],
            conversation_mode_rules=scene_rules,
        )
        providers = self._initiative_provider_candidates(state.unified_msg_origin)
        if not providers:
            logger.warning("[星汐/主动话题] group=%s 没有可用聊天模型。", state.group_id)
            return False
        timeout_seconds = max(
            5.0,
            min(
                120.0,
                float(self._config("quiet_topic_provider_timeout_seconds", 35)),
            ),
        )
        contexts = self.runtime.recent_contexts(
            state.scope_key,
            max(4, int(self._config("quiet_topic_context_messages", 10))),
        )
        candidate = ""
        provider_used = ""
        for provider_name, provider in providers:
            provider_started = asyncio.get_running_loop().time()
            try:
                response = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=current_message,
                        contexts=contexts,
                        system_prompt=system_prompt,
                        func_tool=None,
                        request_max_retries=1,
                    ),
                    timeout=timeout_seconds,
                )
                candidate = clean_response(
                    str(response.completion_text or ""),
                    "chat_bubbles",
                )
                if not candidate:
                    raise ValueError("Provider 返回空文本")
                violations = find_violations(
                    candidate,
                    reply_shape="chat_bubbles",
                    soft_chars=max(30, int(self._config("chat_soft_chars", 100))),
                    max_bubbles=chat_max_bubbles,
                    is_owner=False,
                    conversation_mode="quiet_topic",
                    enforce_group_participation_guard=bool(
                        self._config("group_participation_guard_enabled", True)
                    ),
                )
                if violations and bool(self._config("retry_on_violation", True)):
                    retry = await asyncio.wait_for(
                        provider.text_chat(
                            prompt=build_retry_prompt(
                                current_message,
                                candidate,
                                violations,
                                "chat_bubbles",
                                conversation_mode="quiet_topic",
                            ),
                            contexts=contexts,
                            system_prompt=system_prompt,
                            func_tool=None,
                            request_max_retries=1,
                        ),
                        timeout=timeout_seconds,
                    )
                    candidate = clean_response(
                        str(retry.completion_text or ""),
                        "chat_bubbles",
                    )
                remaining_violations = find_violations(
                    candidate,
                    reply_shape="chat_bubbles",
                    soft_chars=max(30, int(self._config("chat_soft_chars", 100))),
                    max_bubbles=chat_max_bubbles,
                    is_owner=False,
                    conversation_mode="quiet_topic",
                    enforce_group_participation_guard=bool(
                        self._config("group_participation_guard_enabled", True)
                    ),
                )
                if remaining_violations:
                    raise ValueError(
                        "输出仍不合格：" + "、".join(remaining_violations)
                    )
                provider_used = provider_name
                logger.info(
                    "[星汐/主动话题] group=%s trigger=%s Provider=%s 生成成功，耗时=%.1fs。",
                    state.group_id,
                    trigger,
                    provider_name,
                    asyncio.get_running_loop().time() - provider_started,
                )
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                candidate = ""
                logger.warning(
                    "[星汐/主动话题] group=%s trigger=%s Provider=%s 失败（%.1fs）：%s",
                    state.group_id,
                    trigger,
                    provider_name,
                    asyncio.get_running_loop().time() - provider_started,
                    exc,
                )
        if not candidate:
            logger.warning(
                "[星汐/主动话题] group=%s trigger=%s Provider 链全部失败，本次不发送。",
                state.group_id,
                trigger,
            )
            return False
        newer_messages = [
            item for item in state.messages if item.sequence > start_sequence
        ]
        grace_messages = max(
            0,
            int(self._config("quiet_topic_generation_grace_messages", 3)),
        )
        if any(item.is_direct_wake for item in newer_messages) or len(newer_messages) > grace_messages:
            logger.info(
                "[星汐/主动话题] group=%s trigger=%s 生成期间新增 %d 条消息，已让位给当前对话。",
                state.group_id,
                trigger,
                len(newer_messages),
            )
            return False
        bubbles = split_chat_bubbles(candidate, chat_max_bubbles)
        if not bubbles:
            return False
        sent: list[str] = []
        for index, bubble in enumerate(bubbles):
            success = await self.context.send_message(
                state.unified_msg_origin,
                MessageChain().message(bubble),
            )
            if not success:
                break
            sent.append(bubble)
            if index < len(bubbles) - 1:
                delay_bounds = sorted(
                    (
                        max(0, int(self._config("bubble_interval_min_ms", 450)))
                        / 1000,
                        max(0, int(self._config("bubble_interval_max_ms", 1200)))
                        / 1000,
                    )
                )
                await asyncio.sleep(
                    random.uniform(delay_bounds[0], delay_bounds[1])
                )
        if sent:
            self.runtime.record_bot_reply(
                scope_key=state.scope_key,
                target_sender_id="group",
                reply_text="\n".join(sent),
                expression_ids=[item.id for item in expressions],
                target_sequence=state.sequence,
                feedback_window_seconds=max(
                    60,
                    int(self._config("social_feedback_window_minutes", 10)) * 60,
                ),
                quiet_topic=True,
                ambient_participation=True,
            )
            self.runtime.flush()
            logger.info(
                "[星汐/主动话题] 已在 group=%s 发起轻量话题，trigger=%s Provider=%s total=%.1fs tools=none。",
                state.group_id,
                trigger,
                provider_used,
                asyncio.get_running_loop().time() - started_at,
            )
            return True
        logger.warning(
            "[星汐/主动话题] group=%s trigger=%s 平台没有确认任何消息发送成功。",
            state.group_id,
            trigger,
        )
        return False

    @filter.on_astrbot_loaded()
    async def refresh_provider_selectors(self) -> None:
        """核心与 Provider 全部加载后再次刷新可选模型。"""
        self._refresh_provider_schema_options()
        self._start_background_tasks()

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
        event.set_extra(SHIO_REQUIRED_FACT_TOOLS, [])

    @filter.on_llm_request(priority=-sys.maxsize)
    async def enforce_agent_permission(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """内置权限守卫：主人保留全部工具，普通用户只保留精确白名单。"""
        if not bool(self._config("permission_guard_enabled", True)):
            return

        sender_id, _, ambient_target = self._effective_sender(event)
        is_ambient = ambient_target is not None
        is_owner = bool(sender_id) and sender_id in self._owner_ids()
        identity_scope = self._identity_scope(event, sender_id)
        event.set_extra(SHIO_IDENTITY_SCOPE, identity_scope)
        configured_allowed = set(self._guest_allowed_tool_names())
        available_tools = self._available_tools(req.func_tool)
        allowed_tools = (
            []
            if is_ambient
            else [
                tool
                for tool in available_tools
                if str(getattr(tool, "name", "")) in configured_allowed
            ]
        )
        allowed_tool_names = [str(getattr(tool, "name", "")) for tool in allowed_tools]
        if not sender_id:
            allowed_tools = []
            allowed_tool_names = []
        event.set_extra(SHIO_ALLOWED_TOOLS, allowed_tool_names)

        removed_tool_names: list[str] = []
        if not is_owner or is_ambient:
            allowed_name_set = set(allowed_tool_names)
            removed_tool_names = [
                name
                for name in self._get_tool_names(req.func_tool)
                if name not in allowed_name_set
            ]
            req.func_tool = ToolSet(allowed_tools)

        if bool(self._config("inject_verified_context", True)):
            access_mode = (
                "ambient_chat_no_tools"
                if is_ambient
                else (
                    "owner_full_access"
                    if is_owner
                    else ("limited_read_only" if allowed_tool_names else "chat_only")
                )
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
            if is_ambient:
                verified_values["initiative"] = "ambient_reply"
                verified_values["tools"] = "disabled"
                verified_values["external_actions"] = "disabled"
            elif isinstance(event.get_extra(SHIO_NATURAL_WAKE, None), dict):
                verified_values["wake_reason"] = "natural_name"
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
            (not is_owner or is_ambient)
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

        sender_id, sender_name, ambient_target = self._effective_sender(event)
        is_ambient = ambient_target is not None
        conversation_mode = "ambient_join" if is_ambient else "direct_reply"
        is_owner = bool(sender_id) and sender_id in self._owner_ids()
        source_system_prompt = str(req.system_prompt or "")
        identity_scope = event.get_extra(SHIO_IDENTITY_SCOPE, None)
        if not isinstance(identity_scope, dict):
            identity_scope = self._identity_scope(event, sender_id)
        current_message = (
            str(ambient_target.get("text", "") or "").strip()
            if is_ambient
            else get_current_message(event, req.prompt)
        )

        if is_owner and not is_ambient and bool(self._config("owner_task_bypass", True)):
            task_prefix = self._starts_with_any(
                current_message,
                self._config("owner_task_prefixes", ["/agent", "/task"]),
            )
            if task_prefix:
                logger.info("[星汐] 主人任务前缀命中，交还原始 Agent 链路。")
                return

        chat_prefix = None
        if is_owner and not is_ambient:
            chat_prefix = self._starts_with_any(
                current_message,
                self._config("owner_chat_prefixes", ["/chat", "/role"]),
            )
            if chat_prefix:
                current_message = current_message[len(chat_prefix) :].lstrip(" ：:")

        max_messages = max(2, int(self._config("max_context_messages", 16)))
        max_context_chars = max(1000, int(self._config("max_context_chars", 9000)))
        clean_history, history_source = await self._identity_aware_history(
            event,
            req.contexts,
            current_message,
            sender_id,
            str(identity_scope.get("group_id", "")),
            max_messages,
            max_context_chars,
        )
        transcript = contexts_as_transcript(clean_history)
        replyer_history = isolate_replyer_contexts(
            clean_history,
            current_sender_id=sender_id,
            group_id=str(identity_scope.get("group_id", "")),
        )
        supporting_material = collect_supporting_material(
            req,
            max(2000, int(self._config("planner_material_chars", 9000))),
        )
        scope_key = str(
            (ambient_target or {}).get("scope_key", "")
            or self.runtime.group_scope(
                str(identity_scope.get("platform_id", "")),
                str(identity_scope.get("bot_id", "")),
                str(identity_scope.get("group_id", "")),
            )
        )
        if bool(self._config("social_profile_enabled", True)):
            profile_summary = self.runtime.profile_summary(scope_key, sender_id)
            if profile_summary:
                supporting_material = (
                    supporting_material
                    + "\n<shio_interaction_profile>"
                    + profile_summary
                    + "</shio_interaction_profile>"
                ).strip()

        try:
            planner_provider = self._provider(
                str(self._config("planner_provider_id", "")),
                event.unified_msg_origin,
            )
            planner_fallback_id = str(
                self._config("planner_fallback_provider_id", "")
                or self._config("replyer_provider_id", "")
                or ""
            ).strip()
            planner_fallback_provider = self._typed_provider(
                planner_fallback_id,
                "text_chat",
            )
            plan = await self.planner.create_plan(
                provider=planner_provider,
                fallback_provider=planner_fallback_provider,
                timeout_seconds=min(
                    60.0,
                    max(3.0, float(self._config("planner_timeout_seconds", 20))),
                ),
                sender_name=sender_name,
                sender_id=sender_id,
                platform_id=str(identity_scope.get("platform_id", "")),
                bot_id=str(identity_scope.get("bot_id", "")),
                chat_type=str(identity_scope.get("chat_type", "private")),
                group_id=str(identity_scope.get("group_id", "")),
                identity_key=str(identity_scope.get("identity_key", "")),
                is_owner=is_owner,
                conversation_mode=conversation_mode,
                conversation_mode_rules=(
                    self._scene_rules(
                        "ambient_participation_rules",
                        "ambient_participation_extra_prompt",
                    )
                    if is_ambient
                    else ""
                ),
                current_message=current_message,
                transcript=transcript,
                supporting_material=supporting_material,
                enabled=bool(self._config("planner_enabled", True)),
            )

            presentation_tools, presentation_prompt = self._meme_presentation_tools(
                event,
                req.func_tool,
                source_system_prompt,
                sender_id=sender_id,
                is_owner=is_owner,
            )
            presentation_tool_names = [
                str(getattr(tool, "name", "") or "") for tool in presentation_tools
            ]
            factual_tool_names = (
                []
                if is_ambient
                else [
                    str(name)
                    for name in list(event.get_extra(SHIO_ALLOWED_TOOLS, []) or [])
                    if str(name) != MEME_SEARCH_TOOL
                ]
            )
            if is_ambient:
                plan.mode = "chat"
                plan.use_allowed_tools = False
                plan.avoid = list(plan.avoid) + [
                    "调用任何外部资料或 Agent 工具",
                    "承诺替群友执行任务",
                    "把目标群友称作主人、Master 或群主",
                ]
            use_allowed_tools = bool(plan.use_allowed_tools and factual_tool_names)
            if plan.use_allowed_tools and not factual_tool_names:
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
                and not is_ambient
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
                feedback_scores=self.runtime.expression_feedback,
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
                allowed_tool_names=factual_tool_names if use_allowed_tools else [],
                conversation_mode_rules=(
                    self._scene_rules(
                        "ambient_participation_rules",
                        "ambient_participation_extra_prompt",
                    )
                    if is_ambient
                    else ""
                ),
            )
            if presentation_prompt:
                replyer_system = replyer_system.rstrip() + "\n\n" + presentation_prompt
        except Exception as exc:
            logger.exception("[星汐] 请求准备失败，已完整回退原 AstrBot 请求：%s", exc)
            return

        # 所有可能失败的准备步骤结束后才原子式改写请求。
        req.system_prompt = replyer_system
        req.contexts = replyer_history
        req.prompt = current_message or "请自然回应刚才的图片或消息。"
        req.extra_user_content_parts = []
        final_tool_names = set(presentation_tool_names)
        if use_allowed_tools:
            final_tool_names.update(factual_tool_names)
        req.func_tool = ToolSet(
            [
                tool
                for tool in self._available_tools(req.func_tool)
                if str(getattr(tool, "name", "")) in final_tool_names
            ]
        )
        if use_allowed_tools:
            event.set_extra(SHIO_TOOL_REQUEST, req)
            event.set_extra(SHIO_REQUIRED_FACT_TOOLS, factual_tool_names)
            logger.info(
                "[星汐/联网] 已为 sender=%s 开放本轮只读工具：%s",
                sender_id or "<missing>",
                ", ".join(factual_tool_names),
            )
        else:
            event.set_extra(SHIO_TOOL_REQUEST, None)
            event.set_extra(SHIO_REQUIRED_FACT_TOOLS, [])
        if presentation_tool_names:
            logger.info(
                "[星汐/表达] 已保留本轮本地表达工具：%s",
                ", ".join(presentation_tool_names),
            )
        req.tool_calls_result = None

        event.set_extra(SHIO_ACTIVE, True)
        event.set_extra(SHIO_PLAN, plan.to_dict())
        event.set_extra(
            SHIO_PAYLOAD,
            {
                "system_prompt": replyer_system,
                "contexts": replyer_history,
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
                "is_ambient": is_ambient,
                "is_natural_name_wake": isinstance(
                    event.get_extra(SHIO_NATURAL_WAKE, None), dict
                ),
                "conversation_mode": conversation_mode,
                "scope_key": scope_key,
                "target_sequence": int((ambient_target or {}).get("sequence", 0) or 0),
                "history_source": history_source,
                "expression_ids": [item.id for item in expressions],
                "image_urls": list(req.image_urls or []),
                "audio_urls": list(req.audio_urls or []),
                "tool_names": sorted(final_tool_names),
                "reply_shape": plan.reply_shape,
                "chat_soft_chars": chat_soft_chars,
                "long_form_soft_chars": long_form_soft_chars,
                "chat_max_bubbles": chat_max_bubbles,
            },
        )
        if bool(self._config("debug_log", False)):
            logger.info(
                "[星汐] 已接管回复 group=%s sender=%s owner=%s planner_history=%d "
                "replyer_history=%d "
                "history_source=%s expressions=%d readonly_tools=%s "
                "presentation_tools=%s plan=%s",
                str(identity_scope.get("group_id", "")) or "<private>",
                sender_id,
                is_owner,
                len(clean_history),
                len(replyer_history),
                history_source,
                len(expressions),
                ",".join(factual_tool_names) if use_allowed_tools else "none",
                ",".join(presentation_tool_names) or "none",
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
        if not isinstance(payload, dict):
            return
        if response.role == "err":
            acknowledgement = self._enqueue_pending_reply(
                event,
                payload,
                str(response.completion_text or "LLM Provider 请求失败"),
            )
            if acknowledgement:
                response.role = "assistant"
                response.completion_text = acknowledgement
                event.set_extra("meme_manager_semantic_selected_ids", [])
            return
        self._signal_provider_recovered()
        if bool(payload.get("is_ambient", False)) and not self.runtime.is_target_relevant(
            str(payload.get("scope_key", "")),
            int(payload.get("target_sequence", 0) or 0),
            max_new_messages=max(
                0,
                int(self._config("ambient_generation_grace_messages", 4)),
            ),
            max_age_seconds=max(
                5.0,
                float(self._config("ambient_generation_max_seconds", 45)),
            ),
        ):
            response.completion_text = ""
            event.set_extra("meme_manager_semantic_selected_ids", [])
            logger.info(
                "[星汐/群聊参与] target_sequence=%s 已超出相关消息或时间窗口，已放弃本次主动回复。",
                int(payload.get("target_sequence", 0) or 0),
            )
            return
        plan = event.get_extra(SHIO_PLAN, {})
        tool_request = event.get_extra(SHIO_TOOL_REQUEST, None)
        required_fact_tools = set(
            str(name)
            for name in list(event.get_extra(SHIO_REQUIRED_FACT_TOOLS, []) or [])
            if str(name)
        )
        called_tool_names = self._called_tool_names(
            getattr(tool_request, "tool_calls_result", None)
            if tool_request is not None
            else None
        )
        active_tool_names = {
            str(name)
            for name in list(payload.get("tool_names", []) or [])
            if str(name)
        }
        active_tool_names.update(called_tool_names)
        active_tool_names.update(required_fact_tools)
        if (
            isinstance(plan, dict)
            and bool(plan.get("use_allowed_tools", False))
            and tool_request is not None
            and required_fact_tools
            and not required_fact_tools.intersection(called_tool_names)
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
        had_visible_tool_artifact = contains_tool_protocol(
            original,
            active_tool_names,
        )
        original, leaked_meme_references = (
            extract_and_clean_internal_meme_references(
                original,
                active_tool_names,
            )
        )
        if had_visible_tool_artifact and not contains_tool_protocol(
            original,
            active_tool_names,
        ):
            logger.warning(
                "[星汐/表达守卫] 已从回复正文移除伪造的工具文本调用。"
            )
        if leaked_meme_references:
            candidate_map = event.get_extra(
                "meme_manager_semantic_candidates", None
            )
            selected_reference = next(
                (
                    reference
                    for reference in leaked_meme_references
                    if isinstance(candidate_map, dict) and reference in candidate_map
                ),
                "",
            )
            if selected_reference:
                event.set_extra(
                    "meme_manager_semantic_selected_ids", [selected_reference]
                )
                logger.info(
                    "[星汐/表达守卫] 已清理畸形表情引用并恢复模型选图：%s",
                    selected_reference,
                )
            else:
                logger.warning(
                    "[星汐/表达守卫] 已清理无法验证的畸形表情引用：%s",
                    ",".join(leaked_meme_references),
                )
        reply_shape = str(payload.get("reply_shape", "chat_bubbles"))
        is_owner = bool(payload.get("is_owner", False))
        conversation_mode = str(
            payload.get("conversation_mode", "direct_reply") or "direct_reply"
        )
        require_emotional_reaction = is_risque_teasing(
            str(payload.get("current_message", ""))
        )
        required_reaction = (
            str(plan.get("reaction", "") or "")
            if isinstance(plan, dict) and require_emotional_reaction
            else ""
        )
        grounding_facts = (
            list(plan.get("facts", []) or []) if isinstance(plan, dict) else []
        )
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
            is_owner=is_owner,
            conversation_mode=conversation_mode,
            required_reaction=required_reaction,
            require_emotional_reaction=require_emotional_reaction,
            grounding_facts=grounding_facts,
            enforce_group_participation_guard=bool(
                self._config("group_participation_guard_enabled", True)
            ),
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
                            conversation_mode=conversation_mode,
                        ),
                        contexts=list(payload.get("contexts", [])),
                        system_prompt=str(payload.get("system_prompt", "")),
                        # 重写只需要当前文本、被拒绝正文和纯文本历史。
                        # 原请求的多模态附件可能被 AstrBot 自动切到不支持
                        # image_url/audio_url 的备用 Provider，导致重写直接 400。
                        image_urls=[],
                        audio_urls=[],
                        func_tool=None,
                        request_max_retries=1,
                    )
                    if str(retry.completion_text or "").strip():
                        retry_text, _ = extract_and_clean_internal_meme_references(
                            retry.completion_text,
                            active_tool_names,
                        )
                        candidate = clean_response(retry_text, reply_shape)
            except Exception as exc:
                logger.warning("[星汐] 违规回复重写失败，使用本地清理结果：%s", exc)

        post_violations = find_violations(
            candidate,
            reply_shape=reply_shape,
            soft_chars=soft_chars,
            max_bubbles=chat_max_bubbles,
            is_owner=is_owner,
            conversation_mode=conversation_mode,
            required_reaction=required_reaction,
            require_emotional_reaction=require_emotional_reaction,
            grounding_facts=grounding_facts,
            enforce_group_participation_guard=bool(
                self._config("group_participation_guard_enabled", True)
            ),
        )
        if TOOL_PROTOCOL_VIOLATION in post_violations:
            candidate = self._enqueue_pending_reply(
                event,
                payload,
                "模型连续输出内部工具协议",
                original,
            ) or protocol_safe_fallback()
            event.set_extra("meme_manager_semantic_selected_ids", [])
            logger.warning(
                "[星汐/协议守卫] 模型连续输出内部工具协议，已阻断并使用安全回复。"
            )
        elif INTERNAL_REASONING_VIOLATION in post_violations:
            event.set_extra("meme_manager_semantic_selected_ids", [])
            if conversation_mode in {"ambient_join", "quiet_topic"}:
                response.completion_text = ""
                logger.warning(
                    "[星汐/规划守卫] 主动发言连续泄露内部规划，已放弃本次发送。"
                )
                return
            candidate = self._enqueue_pending_reply(
                event,
                payload,
                "模型连续泄露内部规划或推理",
                original,
            ) or reasoning_safe_fallback()
            logger.warning(
                "[星汐/规划守卫] 模型连续泄露内部规划或推理，已阻断并使用安全回复。"
            )
        elif IDENTITY_VIOLATION in post_violations:
            candidate = identity_safe_fallback()
            logger.warning(
                "[星汐/身份守卫] sender=%s owner=false 的回复连续违反身份边界，已使用安全校准回复。",
                str(payload.get("sender_id", "")) or "<missing>",
            )
        elif RELATIONSHIP_VIOLATION in post_violations:
            candidate = relationship_safe_fallback()
            logger.warning(
                "[星汐/关系守卫] sender=%s owner=false 的回复连续越过主人专属亲密边界，已使用自然边界回复。",
                str(payload.get("sender_id", "")) or "<missing>",
            )
        elif (
            REALITY_GROUNDING_VIOLATION in post_violations
            or FACT_GROUNDING_VIOLATION in post_violations
        ):
            candidate = strip_unsupported_personal_experiences(
                candidate,
                grounding_facts,
            ) or reality_safe_fallback(conversation_mode)
            if not candidate:
                response.completion_text = ""
                event.set_extra("meme_manager_semantic_selected_ids", [])
                logger.warning(
                    "[星汐/事实守卫] 主动发言连续编造无来源线下经历，已放弃本次发送。"
                )
                return
            logger.warning(
                "[星汐/事实守卫] 回复连续编造无来源线下经历，已移除相关内容。"
            )
        elif EMOTIONAL_REACTION_VIOLATION in post_violations:
            candidate = emotional_reaction_safe_fallback(
                is_owner,
                str(payload.get("current_message", "")),
            )
            logger.warning(
                "[星汐/情绪守卫] sender=%s owner=%s 连续把情绪场景答成平静说明，已使用角色化短回复。",
                str(payload.get("sender_id", "")) or "<missing>",
                is_owner,
            )
        elif GROUP_PARTICIPATION_VIOLATION in post_violations:
            # 主动发言没有必须回复的用户；连续生成主持/采访腔时宁可安静，
            # 也不要把生硬兜底发到群里。
            response.completion_text = ""
            event.set_extra("meme_manager_semantic_selected_ids", [])
            logger.warning(
                "[星汐/群聊语用守卫] mode=%s 连续退化成一对一采访或主持，已放弃本次主动发言。",
                conversation_mode,
            )
            return
        elif post_violations:
            # 违规重写失败后不得把异常长回复、后台式说明或其他未分类
            # 内容继续交给气泡拆分。主动发言宁可不说，直接回复则给出
            # 一条不可泄漏后台信息的自然兜底。
            event.set_extra("meme_manager_semantic_selected_ids", [])
            if conversation_mode in {"ambient_join", "quiet_topic"}:
                response.completion_text = ""
                logger.warning(
                    "[星汐/输出守卫] 主动发言重写后仍不合格，已放弃发送：%s",
                    "、".join(post_violations),
                )
                return
            candidate = self._enqueue_pending_reply(
                event,
                payload,
                "违规重写后仍不合格：" + "、".join(post_violations),
                original,
            ) or reasoning_safe_fallback()
            logger.warning(
                "[星汐/输出守卫] 违规重写失败后仍有残留，已闭锁为安全回复：%s",
                "、".join(post_violations),
            )

        if reply_shape == "chat_bubbles":
            bubbles = split_chat_bubbles(candidate, chat_max_bubbles)
            final_text = "\n".join(bubbles)
        else:
            final_text = candidate
        if final_text:
            response.completion_text = final_text
            if (
                str(payload.get("chat_type", "")) == "group"
                and not bool(event.get_extra(SHIO_REPLY_RECORDED, False))
            ):
                self.runtime.record_bot_reply(
                    scope_key=str(payload.get("scope_key", "")),
                    target_sender_id=str(payload.get("sender_id", "")),
                    reply_text=final_text,
                    expression_ids=(
                        list(payload.get("expression_ids", []) or [])
                        if bool(self._config("social_feedback_enabled", True))
                        else []
                    ),
                    target_sequence=int(payload.get("target_sequence", 0) or 0),
                    ambient_participation=bool(payload.get("is_ambient", False)),
                    feedback_window_seconds=max(
                        60,
                        int(self._config("social_feedback_window_minutes", 10)) * 60,
                    ),
                )
                event.set_extra(SHIO_REPLY_RECORDED, True)
        elif leaked_meme_references:
            # Marker-only replies are valid when Meme Manager attaches an image.
            # Leaving the original text here would expose the machine reference.
            response.completion_text = ""
        if violations and bool(self._config("debug_log", False)):
            logger.info("[星汐] 输出守卫命中：%s", "、".join(violations))

    @filter.on_decorating_result(priority=-100)
    async def dispatch_chat_bubbles(self, event: AstrMessageEvent) -> None:
        """闲聊按自然句逐条发送；内容型回答保持完整排版。"""
        if not event.get_extra(SHIO_ACTIVE, False):
            return
        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return
        try:
            if not result.is_llm_result():
                return
        except Exception:
            return

        text_components = self._collect_text_components(result.chain)
        if not text_components:
            return
        payload = event.get_extra(SHIO_PAYLOAD, {})
        active_tool_names = (
            {
                str(name)
                for name in list(payload.get("tool_names", []) or [])
                if str(name)
            }
            if isinstance(payload, dict)
            else set()
        )
        final_meme_references: list[str] = []
        removed_visible_tool_artifact = False
        aggregate_source = "\n".join(component.text for component in text_components)
        aggregate_had_protocol = contains_tool_protocol(
            aggregate_source,
            active_tool_names,
        )
        aggregate_cleaned, aggregate_references = (
            extract_and_clean_internal_meme_references(
                aggregate_source,
                active_tool_names,
            )
        )
        if (
            aggregate_had_protocol
            and aggregate_cleaned != aggregate_source.strip()
            and not contains_tool_protocol(aggregate_cleaned, active_tool_names)
        ):
            text_components[0].text = aggregate_cleaned
            for component in text_components[1:]:
                component.text = ""
            removed_visible_tool_artifact = True
            final_meme_references.extend(aggregate_references)
        else:
            for component in text_components:
                original_component_text = component.text
                cleaned_text, references = extract_and_clean_internal_meme_references(
                    original_component_text,
                    active_tool_names,
                )
                component.text = cleaned_text
                if (
                    cleaned_text != original_component_text.strip()
                    and contains_tool_protocol(
                        original_component_text,
                        active_tool_names,
                    )
                    and not contains_tool_protocol(cleaned_text, active_tool_names)
                ):
                    removed_visible_tool_artifact = True
                for reference in references:
                    if reference not in final_meme_references:
                        final_meme_references.append(reference)
        if removed_visible_tool_artifact:
            logger.warning(
                "[星汐/表达守卫] 发送前已从消息节点移除伪造的工具文本调用。"
            )
        if final_meme_references:
            logger.warning(
                "[星汐/表达守卫] 发送前拦截残留表情引用：%s",
                ",".join(final_meme_references),
            )
        visible_text = "".join(comp.text for comp in text_components).strip()
        if removed_visible_tool_artifact and not visible_text:
            text_components[0].text = protocol_safe_fallback()
            for comp in text_components[1:]:
                comp.text = ""
            event.set_extra("meme_manager_semantic_selected_ids", [])
            visible_text = text_components[0].text
        if contains_tool_protocol(visible_text, active_tool_names):
            text_components[0].text = protocol_safe_fallback()
            for comp in text_components[1:]:
                comp.text = ""
            logger.warning(
                "[星汐/协议守卫] 发送前再次发现内部工具协议，已阻断。"
            )
            return
        if contains_internal_reasoning(visible_text):
            text_components[0].text = reasoning_safe_fallback()
            for comp in text_components[1:]:
                comp.text = ""
            event.set_extra("meme_manager_semantic_selected_ids", [])
            logger.warning(
                "[星汐/规划守卫] 发送前再次发现内部规划或推理，已阻断。"
            )
            return
        if (
            isinstance(payload, dict)
            and not bool(payload.get("is_owner", False))
            and contains_nonowner_identity_confusion(visible_text)
        ):
            text_components[0].text = identity_safe_fallback()
            for comp in text_components[1:]:
                comp.text = ""
            event.set_extra("meme_manager_semantic_selected_ids", [])
            logger.warning(
                "[星汐/身份守卫] 发送前再次发现普通群友被归因为主人，已阻断。"
            )
            return

        if not bool(self._config("enable_chat_bubbles", True)):
            return
        plan = event.get_extra(SHIO_PLAN, {})
        if not isinstance(plan, dict) or plan.get("reply_shape") != "chat_bubbles":
            return
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

    async def terminate(self) -> None:
        self._stopping = True
        unbind_participation_plugin(self)
        for task in (self._quiet_topic_task, self._recovery_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.runtime.flush()
        self.pending_replies.flush()
