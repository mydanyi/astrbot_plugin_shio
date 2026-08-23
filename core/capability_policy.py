from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from .identity import PrincipalContext


class CapabilityClass(str, Enum):
    PUBLIC_WEB_READ = "public_web_read"
    CHAT_RETRIEVAL = "chat_retrieval"
    LOCAL_PRESENTATION = "local_presentation"
    MEDIA_GENERATION = "media_generation"
    MEMORY_WRITE = "memory_write"
    ARTIFACT_READ = "artifact_read"
    ARTIFACT_WRITE = "artifact_write"
    SHELL_EXEC = "shell_exec"
    DEVICE_CONTROL = "device_control"
    AGENT_FULL = "agent_full"
    UNKNOWN = "unknown"


class SideEffectClass(str, Enum):
    PUBLIC_READ = "public_read"
    SCOPED_READ = "scoped_read"
    STATE_WRITE = "state_write"
    CODE_EXECUTION = "code_execution"
    EXTERNAL_CONTROL = "external_control"
    AGENT_DELEGATION = "agent_delegation"
    UNKNOWN = "unknown"


DEFAULT_SIDE_EFFECT: dict[CapabilityClass, SideEffectClass] = {
    CapabilityClass.PUBLIC_WEB_READ: SideEffectClass.PUBLIC_READ,
    CapabilityClass.CHAT_RETRIEVAL: SideEffectClass.SCOPED_READ,
    CapabilityClass.LOCAL_PRESENTATION: SideEffectClass.SCOPED_READ,
    CapabilityClass.MEDIA_GENERATION: SideEffectClass.STATE_WRITE,
    CapabilityClass.MEMORY_WRITE: SideEffectClass.STATE_WRITE,
    CapabilityClass.ARTIFACT_READ: SideEffectClass.SCOPED_READ,
    CapabilityClass.ARTIFACT_WRITE: SideEffectClass.STATE_WRITE,
    CapabilityClass.SHELL_EXEC: SideEffectClass.CODE_EXECUTION,
    CapabilityClass.DEVICE_CONTROL: SideEffectClass.EXTERNAL_CONTROL,
    CapabilityClass.AGENT_FULL: SideEffectClass.AGENT_DELEGATION,
    CapabilityClass.UNKNOWN: SideEffectClass.UNKNOWN,
}


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """Sanitized runtime metadata used for classification, never tool arguments."""

    name: str
    description: str
    origin: str
    module_path: str
    parameter_names: tuple[str, ...]
    active: bool
    declared_capability: str
    declared_side_effect: str
    delegation_hint: bool = False

    @classmethod
    def from_runtime_tool(cls, tool: Any) -> "ToolDescriptor":
        metadata = getattr(tool, "metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        parameters = getattr(tool, "parameters", {})
        properties = (
            parameters.get("properties", {})
            if isinstance(parameters, dict)
            else {}
        )
        parameter_names = tuple(
            sorted(
                str(key or "").strip()
                for key in properties
                if str(key or "").strip()
            )
        ) if isinstance(properties, dict) else ()
        declared_capability = str(
            getattr(tool, "shio_capability", "")
            or metadata.get("shio_capability", "")
            or ""
        ).strip().lower()
        declared_side_effect = str(
            getattr(tool, "shio_side_effect", "")
            or metadata.get("shio_side_effect", "")
            or ""
        ).strip().lower()
        delegation_hint = any(
            key in metadata
            for key in (
                "shio_delegates_to",
                "delegates_to",
                "wrapped_tool",
                "target_tool",
                "tool_alias_for",
            )
        )
        module_path = str(
            getattr(tool, "handler_module_path", "")
            or getattr(tool.__class__, "__module__", "")
            or ""
        ).strip()
        return cls(
            name=str(getattr(tool, "name", "") or "").strip(),
            description=str(getattr(tool, "description", "") or "").strip()[:1000],
            origin=str(getattr(tool, "origin", "") or "unknown").strip()[:120],
            module_path=module_path[:240],
            parameter_names=parameter_names,
            active=bool(getattr(tool, "active", True)),
            declared_capability=declared_capability,
            declared_side_effect=declared_side_effect,
            delegation_hint=delegation_hint,
        )


@dataclass(frozen=True, slots=True)
class ToolClassification:
    descriptor: ToolDescriptor
    capability: CapabilityClass
    side_effect: SideEffectClass
    source: str
    reason_code: str

    @property
    def is_read_only(self) -> bool:
        return self.side_effect in {
            SideEffectClass.PUBLIC_READ,
            SideEffectClass.SCOPED_READ,
        }


@dataclass(frozen=True, slots=True)
class CapabilityPolicy:
    """Code-owned capabilities for one verified principal and turn mode.

    Capability booleans express which *kind* of action the principal may take.
    A tool must additionally be explicitly configured by its exact runtime name;
    classification alone never grants a newly installed plugin permission.
    """

    principal_key: str
    policy_kind: str
    is_owner: bool
    relationship_role: str
    verification_source: str
    conversation_mode: str
    chat_read: bool
    public_web_read: bool
    chat_retrieval: bool
    local_presentation: bool
    media_generation: bool
    memory_write: bool
    artifact_read: bool
    artifact_write: bool
    shell_exec: bool
    device_control: bool
    agent_full: bool
    max_external_tool_calls: int
    max_local_presentation_calls: int
    requires_explicit_tool_name: bool
    explicitly_configured_tools: frozenset[str]
    degradation_reasons: tuple[str, ...]

    @property
    def is_degraded(self) -> bool:
        return bool(self.degradation_reasons)

    def allows_capability(self, capability: CapabilityClass) -> bool:
        mapping = {
            CapabilityClass.PUBLIC_WEB_READ: self.public_web_read,
            CapabilityClass.CHAT_RETRIEVAL: self.chat_retrieval,
            CapabilityClass.LOCAL_PRESENTATION: self.local_presentation,
            CapabilityClass.MEDIA_GENERATION: self.media_generation,
            CapabilityClass.MEMORY_WRITE: self.memory_write,
            CapabilityClass.ARTIFACT_READ: self.artifact_read,
            CapabilityClass.ARTIFACT_WRITE: self.artifact_write,
            CapabilityClass.SHELL_EXEC: self.shell_exec,
            CapabilityClass.DEVICE_CONTROL: self.device_control,
            CapabilityClass.AGENT_FULL: self.agent_full,
            CapabilityClass.UNKNOWN: self.is_owner and self.agent_full,
        }
        return bool(mapping.get(capability, False))


@dataclass(frozen=True, slots=True)
class CapabilityDecision:
    classification: ToolClassification
    allowed: bool
    reason_code: str


def _configured_tool_names(values: Iterable[str]) -> frozenset[str]:
    return frozenset(
        name
        for value in values
        if (name := str(value or "").strip())
    )


def build_guest_capability_policy(
    principal: PrincipalContext,
    *,
    configured_tool_names: Iterable[str],
    conversation_mode: str = "direct_reply",
) -> CapabilityPolicy:
    """Build the P3-02 guest policy from trusted structural identity only.

    Owner policies use a separate builder. If this builder is called with an
    owner or non-direct mode, external capabilities fail
    closed instead of silently inheriting guest access.
    """

    mode = str(conversation_mode or "direct_reply").strip().lower()
    reasons: list[str] = []
    if principal.is_owner:
        reasons.append("owner_policy_not_applicable")
    if mode != "direct_reply":
        reasons.append("non_direct_mode")
    if not principal.sender_id or not principal.sender_key:
        reasons.append("identity_unavailable")
    if principal.relationship_role not in {"group_peer", "private_peer"}:
        reasons.append("guest_relationship_unverified")
    if "identity_unverified" in principal.verification_source:
        reasons.append("identity_unverified")

    guest_verified = not reasons
    return CapabilityPolicy(
        principal_key=principal.sender_key,
        policy_kind="guest",
        is_owner=False,
        relationship_role=principal.relationship_role,
        verification_source=principal.verification_source,
        conversation_mode=mode,
        # Text conversation remains available even when external capabilities
        # fail closed; callers must still avoid attributing unverified identity.
        chat_read=True,
        public_web_read=guest_verified,
        chat_retrieval=guest_verified,
        local_presentation=guest_verified,
        media_generation=False,
        memory_write=False,
        artifact_read=False,
        artifact_write=False,
        shell_exec=False,
        device_control=False,
        agent_full=False,
        max_external_tool_calls=2 if guest_verified else 0,
        max_local_presentation_calls=1 if guest_verified else 0,
        requires_explicit_tool_name=True,
        explicitly_configured_tools=_configured_tool_names(configured_tool_names),
        degradation_reasons=tuple(reasons),
    )


def build_owner_capability_policy(
    principal: PrincipalContext,
    *,
    conversation_mode: str = "direct_reply",
) -> CapabilityPolicy:
    """Grant full capability only to a structurally verified configured owner."""

    mode = str(conversation_mode or "direct_reply").strip().lower()
    reasons: list[str] = []
    if not principal.is_owner:
        reasons.append("principal_not_owner")
    if principal.relationship_role != "owner":
        reasons.append("owner_relationship_unverified")
    if not principal.sender_id or not principal.sender_key:
        reasons.append("identity_unavailable")
    if not principal.verification_source.startswith("astrbot_event_sender_id:"):
        reasons.append("owner_source_untrusted")
    if not principal.verification_source.endswith(":configured_owner_id"):
        reasons.append("owner_allowlist_not_verified")
    if mode != "direct_reply":
        reasons.append("non_direct_mode")

    owner_verified = not reasons
    return CapabilityPolicy(
        principal_key=principal.sender_key,
        policy_kind="owner",
        is_owner=owner_verified,
        relationship_role=principal.relationship_role,
        verification_source=principal.verification_source,
        conversation_mode=mode,
        chat_read=True,
        public_web_read=owner_verified,
        chat_retrieval=owner_verified,
        local_presentation=owner_verified,
        media_generation=owner_verified,
        memory_write=owner_verified,
        artifact_read=owner_verified,
        artifact_write=owner_verified,
        shell_exec=owner_verified,
        device_control=owner_verified,
        agent_full=owner_verified,
        # -1 means Shio does not add a call-count limit beyond AstrBot's own
        # owner Agent loop and provider constraints.
        max_external_tool_calls=-1 if owner_verified else 0,
        max_local_presentation_calls=-1 if owner_verified else 0,
        requires_explicit_tool_name=False,
        explicitly_configured_tools=frozenset(),
        degradation_reasons=tuple(reasons),
    )


def decide_tool(
    policy: CapabilityPolicy,
    classification: ToolClassification,
) -> CapabilityDecision:
    """Evaluate one tool without inspecting or logging its arguments."""

    descriptor = classification.descriptor
    if not descriptor.active:
        return CapabilityDecision(classification, False, "tool_inactive")
    if policy.is_owner and policy.agent_full and not policy.is_degraded:
        return CapabilityDecision(
            classification,
            True,
            "verified_owner_full_access",
        )
    if classification.capability == CapabilityClass.UNKNOWN:
        return CapabilityDecision(classification, False, "unknown_capability")
    if not policy.allows_capability(classification.capability):
        return CapabilityDecision(classification, False, "capability_denied")
    expected_side_effect = SAFE_SIDE_EFFECT_BY_CAPABILITY.get(
        classification.capability
    )
    if expected_side_effect is None or classification.side_effect != expected_side_effect:
        return CapabilityDecision(classification, False, "side_effect_mismatch")
    if (
        policy.requires_explicit_tool_name
        and descriptor.name not in policy.explicitly_configured_tools
    ):
        return CapabilityDecision(classification, False, "tool_not_configured")
    source_trusted, source_reason = _tool_source_attestation(classification)
    if not source_trusted:
        return CapabilityDecision(classification, False, source_reason)
    return CapabilityDecision(classification, True, "capability_and_name_allowed")


def _catalog(
    capability: CapabilityClass,
    names: Iterable[str],
    destination: dict[str, CapabilityClass],
) -> None:
    for name in names:
        destination[str(name).strip().lower()] = capability


AUDITED_TOOL_CATALOG: dict[str, CapabilityClass] = {}
_catalog(
    CapabilityClass.PUBLIC_WEB_READ,
    (
        "anysearch_search",
        "anysearch_extract",
        "anysearch_batch_search",
        "web_search",
        "exa_get_contents",
        "firecrawl_extract_web_page",
        "tavily_extract_web_page",
        "web_search_baidu",
        "web_search_bocha",
        "web_search_brave",
        "web_search_exa",
        "web_search_firecrawl",
        "web_search_tavily",
    ),
    AUDITED_TOOL_CATALOG,
)
_catalog(
    CapabilityClass.CHAT_RETRIEVAL,
    (
        "astr_kb_search",
        "recall_long_term_memory",
        "get_group_message_history",
    ),
    AUDITED_TOOL_CATALOG,
)
_catalog(
    CapabilityClass.LOCAL_PRESENTATION,
    ("search_memes",),
    AUDITED_TOOL_CATALOG,
)
_catalog(
    CapabilityClass.MEDIA_GENERATION,
    ("generate_image", "generate_selfie", "generate_video"),
    AUDITED_TOOL_CATALOG,
)
_catalog(
    CapabilityClass.MEMORY_WRITE,
    ("memorize_long_term_memory",),
    AUDITED_TOOL_CATALOG,
)
_catalog(
    CapabilityClass.ARTIFACT_READ,
    (
        "astrbot_file_read_tool",
        "astrbot_grep_tool",
        "astrbot_download_file",
    ),
    AUDITED_TOOL_CATALOG,
)
_catalog(
    CapabilityClass.ARTIFACT_WRITE,
    (
        "astrbot_file_write_tool",
        "astrbot_file_edit_tool",
        "astrbot_upload_file",
    ),
    AUDITED_TOOL_CATALOG,
)
_catalog(
    CapabilityClass.SHELL_EXEC,
    (
        "astrbot_execute_shell",
        "astrbot_shell_session",
        "astrbot_execute_python",
        "astrbot_execute_ipython",
    ),
    AUDITED_TOOL_CATALOG,
)
_catalog(
    CapabilityClass.DEVICE_CONTROL,
    (
        "astrbot_cua_keyboard_type",
        "astrbot_cua_mouse_click",
        "astrbot_cua_screenshot",
        "astrbot_execute_browser",
        "astrbot_execute_browser_batch",
        "astrbot_run_browser_skill",
        "future_task",
        "send_message_to_user",
        "llm_set_group_ban",
    ),
    AUDITED_TOOL_CATALOG,
)
_catalog(
    CapabilityClass.AGENT_FULL,
    (
        "astrbot_annotate_execution",
        "astrbot_create_skill_candidate",
        "astrbot_create_skill_payload",
        "astrbot_evaluate_skill_candidate",
        "astrbot_get_execution_history",
        "astrbot_get_skill_payload",
        "astrbot_list_skill_candidates",
        "astrbot_list_skill_releases",
        "astrbot_promote_skill_candidate",
        "astrbot_rollback_skill_release",
        "astrbot_sync_skill_release",
    ),
    AUDITED_TOOL_CATALOG,
)


AGENT_RISK_RE = re.compile(
    r"(?:subagent|agent[_ -]?full|skill[_ -]?(?:candidate|release|payload)|"
    r"execution[_ -]?history|代理工作流|完整\s*agent)",
    re.IGNORECASE,
)
DEVICE_RISK_RE = re.compile(
    r"(?:cua[_ -]|mouse[_ -]?click|keyboard[_ -]?type|screenshot|"
    r"execute[_ -]?browser|run[_ -]?browser|browser[_ -]?(?:control|batch)|"
    r"send[_ -]?message|group[_ -]?ban|future[_ -]?task|device[_ -]?control|"
    r"发送消息|禁言|控制(?:设备|浏览器|鼠标|键盘))",
    re.IGNORECASE,
)
INDIRECT_DISPATCH_RE = re.compile(
    r"(?:invoke|call|execute|run|dispatch|delegate|forward|proxy|route)"
    r"(?:[ _-]+|\s+(?:an?|the)\s+)"
    r"(?:another[ _-]+)?(?:tool|plugin|agent|function|skill)|"
    r"(?:tool|plugin|agent|function|skill)[ _-]?"
    r"(?:dispatcher|router|proxy|bridge|delegate)|"
    r"(?:调用|转发|代理|分派|委派)(?:任意|其他|另一个)?(?:工具|插件|代理|函数)",
    re.IGNORECASE,
)
SHELL_RISK_RE = re.compile(
    r"(?:execute[_ -]?(?:shell|python|ipython)|shell[_ -]?session|"
    r"shell\b|terminal\b|execute (?:a )?(?:command|code)|run (?:a )?command|"
    r"命令执行|执行(?:命令|代码|python)|终端)",
    re.IGNORECASE,
)
MEMORY_WRITE_RE = re.compile(
    r"(?:memorize[_ -]|write[_ -]?memory|save[_ -]?memory|记住|写入.*记忆)",
    re.IGNORECASE,
)
ARTIFACT_WRITE_RE = re.compile(
    r"(?:file[_ -]?(?:write|edit|upload|delete|remove)|"
    r"write (?:a )?(?:local )?file|edit (?:a )?(?:local )?file|"
    r"delete (?:a )?(?:local )?file|artifact[_ -]?write|"
    r"写入文件|编辑文件|删除文件|上传文件)",
    re.IGNORECASE,
)
ARTIFACT_READ_RE = re.compile(
    r"(?:file[_ -]?(?:read|download)|grep[_ -]?tool|artifact[_ -]?read|"
    r"read (?:a )?(?:local )?file|读取文件|下载文件)",
    re.IGNORECASE,
)
MEDIA_GENERATION_RE = re.compile(
    r"(?:generate[_ -]?(?:image|selfie|video)|image[_ -]?generation|"
    r"video[_ -]?generation|生成(?:图片|图像|视频|自拍))",
    re.IGNORECASE,
)


def _semantic_risk(
    descriptor: ToolDescriptor,
) -> tuple[CapabilityClass, SideEffectClass, str] | None:
    combined = " ".join(
        (
            descriptor.name,
            descriptor.description,
            descriptor.origin,
            descriptor.module_path,
            " ".join(descriptor.parameter_names),
        )
    )
    parameter_names = set(descriptor.parameter_names)
    dispatch_target_parameters = {
        "tool",
        "tool_name",
        "function",
        "function_name",
        "plugin",
        "plugin_name",
        "agent",
        "agent_name",
        "skill",
        "skill_name",
    }
    dispatch_payload_parameters = {
        "arguments",
        "args",
        "parameters",
        "payload",
        "request",
        "task",
        "prompt",
    }
    if (
        descriptor.delegation_hint
        or INDIRECT_DISPATCH_RE.search(combined)
        or (
            parameter_names.intersection(dispatch_target_parameters)
            and parameter_names.intersection(dispatch_payload_parameters)
        )
    ):
        return (
            CapabilityClass.AGENT_FULL,
            SideEffectClass.AGENT_DELEGATION,
            "indirect_tool_dispatch_semantics",
        )
    checks = (
        (
            AGENT_RISK_RE,
            CapabilityClass.AGENT_FULL,
            SideEffectClass.AGENT_DELEGATION,
            "agent_semantics",
        ),
        (
            DEVICE_RISK_RE,
            CapabilityClass.DEVICE_CONTROL,
            SideEffectClass.EXTERNAL_CONTROL,
            "device_control_semantics",
        ),
        (
            SHELL_RISK_RE,
            CapabilityClass.SHELL_EXEC,
            SideEffectClass.CODE_EXECUTION,
            "code_execution_semantics",
        ),
        (
            MEMORY_WRITE_RE,
            CapabilityClass.MEMORY_WRITE,
            SideEffectClass.STATE_WRITE,
            "memory_write_semantics",
        ),
        (
            ARTIFACT_WRITE_RE,
            CapabilityClass.ARTIFACT_WRITE,
            SideEffectClass.STATE_WRITE,
            "artifact_write_semantics",
        ),
        (
            ARTIFACT_READ_RE,
            CapabilityClass.ARTIFACT_READ,
            SideEffectClass.SCOPED_READ,
            "artifact_read_semantics",
        ),
        (
            MEDIA_GENERATION_RE,
            CapabilityClass.MEDIA_GENERATION,
            SideEffectClass.STATE_WRITE,
            "media_generation_semantics",
        ),
    )
    for pattern, capability, side_effect, reason in checks:
        if pattern.search(combined):
            return capability, side_effect, reason
    return None


def _declared_classification(
    descriptor: ToolDescriptor,
) -> tuple[CapabilityClass, SideEffectClass] | None:
    try:
        capability = CapabilityClass(descriptor.declared_capability)
    except ValueError:
        return None
    if capability == CapabilityClass.UNKNOWN:
        return capability, SideEffectClass.UNKNOWN
    try:
        side_effect = SideEffectClass(descriptor.declared_side_effect)
    except ValueError:
        side_effect = DEFAULT_SIDE_EFFECT[capability]
    return capability, side_effect


def _safe_semantic_classification(
    descriptor: ToolDescriptor,
) -> tuple[CapabilityClass, SideEffectClass, str] | None:
    combined = " ".join(
        (
            descriptor.name,
            descriptor.description,
            descriptor.module_path,
            " ".join(descriptor.parameter_names),
        )
    ).lower()
    if re.search(r"(?:recall|knowledge|message[_ -]?history|回忆|知识库|聊天记录)", combined):
        return (
            CapabilityClass.CHAT_RETRIEVAL,
            SideEffectClass.SCOPED_READ,
            "chat_retrieval_semantics",
        )
    if re.search(r"(?:search[_ -]?memes|表情包.*(?:搜索|检索))", combined):
        return (
            CapabilityClass.LOCAL_PRESENTATION,
            SideEffectClass.SCOPED_READ,
            "presentation_semantics",
        )
    has_web_signal = bool(
        re.search(
            r"(?:web[_ -]?search|public (?:web|page|documentation)|"
            r"公开网页|联网搜索|网页(?:搜索|提取)|https?\b)",
            combined,
        )
    )
    has_query_shape = bool(
        {"query", "url", "keywords", "keyword"}.intersection(
            descriptor.parameter_names
        )
    )
    if has_web_signal and (has_query_shape or "search" in descriptor.name.lower()):
        return (
            CapabilityClass.PUBLIC_WEB_READ,
            SideEffectClass.PUBLIC_READ,
            "public_web_semantics",
        )
    return None


def classify_descriptor(descriptor: ToolDescriptor) -> ToolClassification:
    risk = _semantic_risk(descriptor)
    if risk is not None:
        capability, side_effect, reason = risk
        return ToolClassification(
            descriptor=descriptor,
            capability=capability,
            side_effect=side_effect,
            source="semantic_risk",
            reason_code=reason,
        )

    declared = _declared_classification(descriptor)
    if declared is not None:
        capability, side_effect = declared
        return ToolClassification(
            descriptor=descriptor,
            capability=capability,
            side_effect=side_effect,
            source="declared_metadata",
            reason_code="tool_declared_capability",
        )

    normalized_name = descriptor.name.strip().lower()
    catalog_capability = AUDITED_TOOL_CATALOG.get(normalized_name)
    if catalog_capability is not None:
        return ToolClassification(
            descriptor=descriptor,
            capability=catalog_capability,
            side_effect=DEFAULT_SIDE_EFFECT[catalog_capability],
            source="audited_catalog",
            reason_code="audited_runtime_tool",
        )

    semantic = _safe_semantic_classification(descriptor)
    if semantic is not None:
        capability, side_effect, reason = semantic
        return ToolClassification(
            descriptor=descriptor,
            capability=capability,
            side_effect=side_effect,
            source="semantic_read",
            reason_code=reason,
        )

    return ToolClassification(
        descriptor=descriptor,
        capability=CapabilityClass.UNKNOWN,
        side_effect=SideEffectClass.UNKNOWN,
        source="unknown",
        reason_code="unclassified_tool",
    )


def classify_tool(tool: Any) -> ToolClassification:
    return classify_descriptor(ToolDescriptor.from_runtime_tool(tool))


AUDITED_SOURCE_HINTS: dict[str, tuple[str, ...]] = {}


def _source_hints(names: Iterable[str], *hints: str) -> None:
    normalized_hints = tuple(str(hint).strip().lower() for hint in hints if hint)
    for name in names:
        AUDITED_SOURCE_HINTS[str(name).strip().lower()] = normalized_hints


_source_hints(
    ("anysearch_search", "anysearch_extract", "anysearch_batch_search"),
    "astrbot_plugin_anysearch",
)
_source_hints(
    ("recall_long_term_memory", "memorize_long_term_memory"),
    "astrbot_plugin_livingmemory",
)
_source_hints(("search_memes",), "astrbot_plugin_meme_manager")
_source_hints(
    ("generate_image", "generate_selfie", "generate_video"),
    "astrbot_plugin_omnidraw",
)
_source_hints(("llm_set_group_ban",), "astrbot_plugin_qq_admin")

for _catalog_name in AUDITED_TOOL_CATALOG:
    AUDITED_SOURCE_HINTS.setdefault(_catalog_name, ("astrbot", "builtin"))


SAFE_SIDE_EFFECT_BY_CAPABILITY = {
    CapabilityClass.PUBLIC_WEB_READ: SideEffectClass.PUBLIC_READ,
    CapabilityClass.CHAT_RETRIEVAL: SideEffectClass.SCOPED_READ,
    CapabilityClass.LOCAL_PRESENTATION: SideEffectClass.SCOPED_READ,
}


def _tool_source_attestation(
    classification: ToolClassification,
) -> tuple[bool, str]:
    descriptor = classification.descriptor
    source_text = f"{descriptor.origin} {descriptor.module_path}".strip().lower()
    if classification.source == "audited_catalog":
        hints = AUDITED_SOURCE_HINTS.get(descriptor.name.strip().lower(), ())
        if hints and any(hint in source_text for hint in hints):
            return True, "audited_name_and_source"
        return False, "audited_name_source_mismatch"
    if classification.source == "declared_metadata":
        module_path = descriptor.module_path.strip().lower()
        if module_path and module_path not in {"builtins", "types"}:
            return True, "declared_capability_with_source"
        return False, "declared_capability_source_missing"
    # Conservative semantic inference is useful for inventory, but it is not
    # an authorization attestation for a dynamic wrapper or MCP alias.
    return False, "classification_source_unattested"


def inventory_by_capability(
    classifications: Iterable[ToolClassification],
) -> dict[CapabilityClass, int]:
    inventory = {capability: 0 for capability in CapabilityClass}
    for classification in classifications:
        inventory[classification.capability] += 1
    return inventory
