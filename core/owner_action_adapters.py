"""Code-owned, fail-closed parameter adapters for trusted-owner actions.

The model-facing :class:`OwnerActionProposal` only selects one closed operation.
This module derives every executable parameter from the *current* canonical message,
checks an exact candidate-runtime attestation, and returns an opaque canonical draft.
It deliberately exposes no generic argument mapping or public materializer.

P3-08D does not access the filesystem and does not execute a tool.  Path, scope and
runtime facts must arrive as typed controller/runtime attestations.  All adapters are
disabled by default, and the shell adapter remains code-level hard-disabled even when
its configuration flag is true.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import threading
import weakref
from dataclasses import InitVar, dataclass, field
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any

from .capability_policy import CapabilityClass
from .contracts import (
    ActionConfirmationPolicy,
    ActionSideEffect,
    DecisionBinding,
    OwnerActionOperation,
    OwnerActionProposal,
)


ARTIFACT_READ_DEFAULT_OFFSET = 0
ARTIFACT_READ_DEFAULT_LIMIT = 200
ARTIFACT_READ_MAX_OFFSET = 10_000
ARTIFACT_READ_MAX_LIMIT = 400
ARTIFACT_READ_MAX_TARGET_BYTES = 131_072

ARTIFACT_GREP_CONTEXT_LINES = 2
ARTIFACT_GREP_RESULT_LIMIT = 50
ARTIFACT_GREP_MAX_FILES = 2_000
ARTIFACT_GREP_MAX_TREE_BYTES = 64 * 1024 * 1024
ARTIFACT_GREP_OUTPUT_BYTES = 32 * 1024
ARTIFACT_GREP_MAX_PATTERN_BYTES = 256
ARTIFACT_GREP_FIXED_GLOB = (
    "*.{txt,md,markdown,rst,py,pyi,js,jsx,ts,tsx,json,jsonl,yaml,yml,"
    "toml,ini,cfg,conf,log,csv,tsv,xml,html,css,scss,sql,sh,bash,zsh,"
    "ps1,bat,cmd,c,h,cc,cpp,hpp,java,kt,kts,go,rs,rb,php,lua,swift,"
    "cs,fs,fsx,vue,svelte}"
)

MEMORY_MAX_LITERAL_BYTES = 1_024
MEMORY_FIXED_IMPORTANCE = 0.6
SHELL_MAX_COMMAND_BYTES = 4_096

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SHELL_ROLLOUT_HARD_DISABLED = True

_TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".log",
        ".csv",
        ".tsv",
        ".xml",
        ".html",
        ".css",
        ".scss",
        ".sql",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".bat",
        ".cmd",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".java",
        ".kt",
        ".kts",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".lua",
        ".swift",
        ".cs",
        ".fs",
        ".fsx",
        ".vue",
        ".svelte",
    }
)
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)

_NON_EXPLICIT_MARKERS = (
    "不要",
    "别执行",
    "不用",
    "不必",
    "无需",
    "假设",
    "如果",
    "如何",
    "怎么使用",
    "教程",
    "示例",
    "比如",
    "引用",
    "他说",
    "她说",
    "讨论",
)
_NON_EXPLICIT_ENGLISH_RE = re.compile(
    r"\b(?:how\s+to|what\s+if|example|tutorial|do\s+not|don't|never)\b",
    re.IGNORECASE,
)


class AdapterDraftRejected(ValueError):
    """Safe, closed rejection from a code-owned adapter."""

    def __init__(self, reason_code: str) -> None:
        normalized = str(reason_code or "").strip().lower()
        if not _SAFE_NAME_RE.fullmatch(normalized):
            normalized = "adapter_rejected"
        self.reason_code = normalized
        super().__init__(normalized)


class ArtifactPathFlavor(str, Enum):
    WINDOWS = "windows"
    POSIX = "posix"
    WSL_POSIX = "wsl_posix"


class ArtifactTargetKind(str, Enum):
    PLAIN_TEXT_FILE = "plain_text_file"
    PLAIN_TEXT_TREE = "plain_text_tree"


class MemoryScopeMode(str, Enum):
    PRIVATE_SESSION = "private_session"
    PRIVATE_USER = "private_user"
    GLOBAL = "global"


class ShellFamily(str, Enum):
    POSIX_SH = "posix_sh"
    POWERSHELL = "powershell"


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest_payload(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name}_invalid")
    return value


def _require_nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name}_invalid")
    return value


def _require_safe_name(value: Any, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name}_invalid")
    normalized = value.strip().lower()
    if not _SAFE_NAME_RE.fullmatch(normalized):
        raise ValueError(f"{name}_invalid")
    return normalized


def _require_digest(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if type(value) is not str or not _HEX64_RE.fullmatch(value):
        raise ValueError(f"{name}_invalid")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class AdapterDescriptor:
    adapter_id: str
    adapter_version: str
    capability: CapabilityClass
    operation: OwnerActionOperation
    side_effect: ActionSideEffect
    confirmation_policy: ActionConfirmationPolicy
    exact_tool_name: str
    plugin_id: str
    implementation_version: str
    source_qualname: str
    interface_version: str
    schema_digest: str
    call_budget: int = 1
    private_owner_only: bool = True
    hard_disabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter_id", _require_safe_name(self.adapter_id, "adapter_id"))
        object.__setattr__(
            self,
            "adapter_version",
            _require_safe_name(self.adapter_version, "adapter_version"),
        )
        if not isinstance(self.capability, CapabilityClass):
            raise TypeError("adapter_capability_invalid")
        if type(self.operation) is not OwnerActionOperation:
            raise TypeError("adapter_operation_invalid")
        if not isinstance(self.side_effect, ActionSideEffect):
            raise TypeError("adapter_side_effect_invalid")
        if not isinstance(self.confirmation_policy, ActionConfirmationPolicy):
            raise TypeError("adapter_confirmation_policy_invalid")
        for name in (
            "exact_tool_name",
            "plugin_id",
            "implementation_version",
            "source_qualname",
            "interface_version",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"adapter_{name}_invalid")
        object.__setattr__(
            self,
            "schema_digest",
            _require_digest(self.schema_digest, "schema_digest"),
        )
        if type(self.call_budget) is not int or self.call_budget != 1:
            raise ValueError("adapter_call_budget_invalid")
        if self.private_owner_only is not True:
            raise ValueError("adapter_private_owner_only_required")
        _require_bool(self.hard_disabled, "adapter_hard_disabled")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "capability": self.capability.value,
            "operation": self.operation.value,
            "side_effect": self.side_effect.value,
            "confirmation_policy": self.confirmation_policy.value,
            "exact_tool_name": self.exact_tool_name,
            "call_budget": self.call_budget,
            "private_owner_only": True,
            "hard_disabled": self.hard_disabled,
        }

    def __repr__(self) -> str:
        return (
            "AdapterDescriptor("
            f"adapter_id={self.adapter_id!r}, operation={self.operation.value!r}, "
            f"exact_tool_name={self.exact_tool_name!r}, call_budget=1, "
            f"private_owner_only=True, hard_disabled={self.hard_disabled})"
        )


_ADAPTERS = {
    OwnerActionOperation.ARTIFACT_READ_EXACT: AdapterDescriptor(
        adapter_id="artifact-read-exact",
        adapter_version="1.0.0",
        capability=CapabilityClass.ARTIFACT_READ,
        operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
        side_effect=ActionSideEffect.SCOPED_READ,
        confirmation_policy=ActionConfirmationPolicy.CURRENT_EXPLICIT_REQUEST,
        exact_tool_name="astrbot_file_read_tool",
        plugin_id="astrbot.builtin",
        implementation_version="astrbot-4.27.2",
        source_qualname="astrbot.core.tools.computer_tools.fs.FileReadTool",
        interface_version="shio-owner-artifact-read-v1",
        schema_digest="c2699988e29253645d9f26e93197def1675b1022bd0b1109f016f637d3ffe22e",
    ),
    OwnerActionOperation.ARTIFACT_GREP: AdapterDescriptor(
        adapter_id="artifact-grep",
        adapter_version="1.0.0",
        capability=CapabilityClass.ARTIFACT_READ,
        operation=OwnerActionOperation.ARTIFACT_GREP,
        side_effect=ActionSideEffect.SCOPED_READ,
        confirmation_policy=ActionConfirmationPolicy.CURRENT_EXPLICIT_REQUEST,
        exact_tool_name="astrbot_grep_tool",
        plugin_id="astrbot.builtin",
        implementation_version="astrbot-4.27.2",
        source_qualname="astrbot.core.tools.computer_tools.fs.GrepTool",
        interface_version="shio-owner-artifact-grep-v1",
        schema_digest="b7e65c466697b0e807ef7ae85d871f1bc82baa5f9235d980a9e2e3612d6e745c",
    ),
    OwnerActionOperation.MEMORY_WRITE_LITERAL: AdapterDescriptor(
        adapter_id="memory-write-literal",
        adapter_version="1.0.0",
        capability=CapabilityClass.MEMORY_WRITE,
        operation=OwnerActionOperation.MEMORY_WRITE_LITERAL,
        side_effect=ActionSideEffect.STATE_WRITE,
        confirmation_policy=ActionConfirmationPolicy.CURRENT_EXPLICIT_REQUEST,
        exact_tool_name="memorize_long_term_memory",
        plugin_id="astrbot_plugin_livingmemory",
        implementation_version="livingmemory-2.5.7",
        source_qualname=(
            "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool."
            "MemoryMemorizeTool"
        ),
        interface_version="shio-owner-memory-write-v1",
        schema_digest="d9443ee69a71449877f2afc49b8f9a5a25972f14dd66677977583ba6d9f7ef8b",
    ),
    OwnerActionOperation.SANDBOX_SHELL_ONCE: AdapterDescriptor(
        adapter_id="sandbox-shell-once",
        adapter_version="1.0.0",
        capability=CapabilityClass.SHELL_EXEC,
        operation=OwnerActionOperation.SANDBOX_SHELL_ONCE,
        side_effect=ActionSideEffect.CODE_EXECUTION,
        confirmation_policy=ActionConfirmationPolicy.SECOND_TURN_REQUIRED,
        exact_tool_name="astrbot_execute_shell",
        plugin_id="astrbot.builtin",
        implementation_version="astrbot-4.27.2",
        source_qualname="astrbot.core.tools.computer_tools.shell.ExecuteShellTool",
        interface_version="shio-owner-sandbox-shell-v2",
        schema_digest="596fbad7c4464e5900a17e8682d9e90a2ea703540afa7cd9fa0e6a95e0487397",
        hard_disabled=True,
    ),
}
OWNER_ACTION_ADAPTER_REGISTRY = MappingProxyType(_ADAPTERS)


@dataclass(frozen=True, slots=True, repr=False)
class AdapterConfig:
    """Typed rollout gates.  Constructor defaults are deliberately all-off."""

    artifact_read_exact_enabled: bool = False
    artifact_grep_enabled: bool = False
    memory_write_literal_enabled: bool = False
    sandbox_shell_once_enabled: bool = False
    artifact_root: str = field(default="", repr=False)
    path_flavor: ArtifactPathFlavor | None = None
    shell_family: ShellFamily | None = None

    def __post_init__(self) -> None:
        for name in (
            "artifact_read_exact_enabled",
            "artifact_grep_enabled",
            "memory_write_literal_enabled",
            "sandbox_shell_once_enabled",
        ):
            _require_bool(getattr(self, name), name)
        if type(self.artifact_root) is not str:
            raise TypeError("artifact_root_invalid")
        if self.path_flavor is not None and not isinstance(
            self.path_flavor,
            ArtifactPathFlavor,
        ):
            raise TypeError("path_flavor_invalid")
        if self.shell_family is not None and not isinstance(
            self.shell_family,
            ShellFamily,
        ):
            raise TypeError("shell_family_invalid")

    @property
    def enabled_count(self) -> int:
        return sum(
            (
                self.artifact_read_exact_enabled,
                self.artifact_grep_enabled,
                self.memory_write_literal_enabled,
                self.sandbox_shell_once_enabled,
            )
        )

    def trace_metadata(self) -> dict[str, int | bool | str]:
        return {
            "schema_version": 1,
            "enabled_count": self.enabled_count,
            "artifact_read_enabled": self.artifact_read_exact_enabled,
            "artifact_grep_enabled": self.artifact_grep_enabled,
            "memory_write_enabled": self.memory_write_literal_enabled,
            "shell_flag_enabled": self.sandbox_shell_once_enabled,
            "artifact_root_configured": bool(self.artifact_root),
            "path_flavor": self.path_flavor.value if self.path_flavor else "none",
            "shell_family": self.shell_family.value if self.shell_family else "none",
            "private_owner_only": True,
        }

    def __repr__(self) -> str:
        return (
            "AdapterConfig("
            f"enabled_count={self.enabled_count}, "
            f"artifact_read={self.artifact_read_exact_enabled}, "
            f"artifact_grep={self.artifact_grep_enabled}, "
            f"memory_write={self.memory_write_literal_enabled}, "
            f"shell_flag={self.sandbox_shell_once_enabled}, "
            f"path_flavor={(self.path_flavor.value if self.path_flavor else 'none')!r}, "
            "private_owner_only=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ArtifactPathConformance:
    root_digest: str = field(repr=False)
    path_digest: str = field(repr=False)
    preflight_token_digest: str = field(repr=False)
    binding_revision: int
    target_kind: ArtifactTargetKind
    target_size_bytes: int
    tree_file_count: int
    tree_total_bytes: int
    exists: bool
    realpath_within_root: bool
    components_lstat_verified: bool
    symlink_free: bool
    reparse_free: bool
    mount_escape_free: bool
    hardlink_count: int
    root_exclusive_trusted_writers: bool
    replacement_protected: bool
    plain_text_magic: bool
    allowed_extensions_only: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "root_digest", _require_digest(self.root_digest, "root_digest"))
        object.__setattr__(self, "path_digest", _require_digest(self.path_digest, "path_digest"))
        object.__setattr__(
            self,
            "preflight_token_digest",
            _require_digest(
                self.preflight_token_digest,
                "preflight_token_digest",
                allow_empty=True,
            ),
        )
        if type(self.binding_revision) is not int or self.binding_revision < 1:
            raise ValueError("path_binding_revision_invalid")
        if not isinstance(self.target_kind, ArtifactTargetKind):
            raise TypeError("path_target_kind_invalid")
        for name in (
            "target_size_bytes",
            "tree_file_count",
            "tree_total_bytes",
            "hardlink_count",
        ):
            _require_nonnegative_int(getattr(self, name), name)
        for name in (
            "exists",
            "realpath_within_root",
            "components_lstat_verified",
            "symlink_free",
            "reparse_free",
            "mount_escape_free",
            "root_exclusive_trusted_writers",
            "replacement_protected",
            "plain_text_magic",
            "allowed_extensions_only",
        ):
            _require_bool(getattr(self, name), name)

    def trace_metadata(self) -> dict[str, int | str | bool]:
        return {
            "schema_version": 1,
            "target_kind": self.target_kind.value,
            "target_size_bytes": self.target_size_bytes,
            "tree_file_count": self.tree_file_count,
            "tree_total_bytes": self.tree_total_bytes,
            "preflight_bound": bool(self.preflight_token_digest),
            "link_checks_complete": (
                self.components_lstat_verified
                and self.symlink_free
                and self.reparse_free
                and self.mount_escape_free
            ),
            "toctou_controls_complete": (
                self.root_exclusive_trusted_writers and self.replacement_protected
            ),
        }

    def __repr__(self) -> str:
        return (
            "ArtifactPathConformance("
            f"target_kind={self.target_kind.value!r}, "
            f"target_size_bytes={self.target_size_bytes}, "
            f"tree_file_count={self.tree_file_count}, "
            f"tree_total_bytes={self.tree_total_bytes}, "
            f"preflight_bound={bool(self.preflight_token_digest)}, "
            "path_hidden=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GrepRuntimeConformance:
    engine: str
    literal_escape_verified: bool
    argv_or_posix_shell_verified: bool
    fallback_disabled: bool
    preflight_budget_enforced: bool
    hard_deadline_enforced: bool
    source_output_cap_bytes: int

    def __post_init__(self) -> None:
        if type(self.engine) is not str or not self.engine.strip():
            raise ValueError("grep_engine_invalid")
        for name in (
            "literal_escape_verified",
            "argv_or_posix_shell_verified",
            "fallback_disabled",
            "preflight_budget_enforced",
            "hard_deadline_enforced",
        ):
            _require_bool(getattr(self, name), name)
        _require_nonnegative_int(self.source_output_cap_bytes, "source_output_cap_bytes")

    def trace_metadata(self) -> dict[str, int | str | bool]:
        return {
            "schema_version": 1,
            "engine": self.engine,
            "literal_only": self.literal_escape_verified,
            "fallback_disabled": self.fallback_disabled,
            "preflight_budget_enforced": self.preflight_budget_enforced,
            "hard_deadline_enforced": self.hard_deadline_enforced,
            "source_output_cap_bytes": self.source_output_cap_bytes,
        }

    def __repr__(self) -> str:
        return (
            "GrepRuntimeConformance("
            f"engine={self.engine!r}, literal_only={self.literal_escape_verified}, "
            f"fallback_disabled={self.fallback_disabled}, "
            f"source_output_cap_bytes={self.source_output_cap_bytes})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class MemoryScopeConformance:
    mode: MemoryScopeMode
    expected_scope_token: str = field(repr=False)
    resolved_scope_token: str = field(repr=False)
    private_chat: bool
    current_owner_subject: bool
    identity_degraded: bool
    global_or_shared_alias_disabled: bool
    dashboard_permission_admin: bool
    owner_is_astrbot_admin: bool
    permission_wrapper_preserved: bool

    def __post_init__(self) -> None:
        if not isinstance(self.mode, MemoryScopeMode):
            raise TypeError("memory_scope_mode_invalid")
        for name in ("expected_scope_token", "resolved_scope_token"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip() or len(value) > 512:
                raise ValueError(f"{name}_invalid")
        for name in (
            "private_chat",
            "current_owner_subject",
            "identity_degraded",
            "global_or_shared_alias_disabled",
            "dashboard_permission_admin",
            "owner_is_astrbot_admin",
            "permission_wrapper_preserved",
        ):
            _require_bool(getattr(self, name), name)

    @property
    def expected_scope_digest(self) -> str:
        return _digest_text(self.expected_scope_token)

    def trace_metadata(self) -> dict[str, str | bool | int]:
        return {
            "schema_version": 1,
            "scope_mode": self.mode.value,
            "private_chat": self.private_chat,
            "current_owner_subject": self.current_owner_subject,
            "identity_degraded": self.identity_degraded,
            "scope_tokens_match": self.expected_scope_token == self.resolved_scope_token,
            "permission_wrapper_preserved": self.permission_wrapper_preserved,
        }

    def __repr__(self) -> str:
        return (
            "MemoryScopeConformance("
            f"mode={self.mode.value!r}, private_chat={self.private_chat}, "
            f"current_owner_subject={self.current_owner_subject}, "
            f"scope_tokens_match={self.expected_scope_token == self.resolved_scope_token}, "
            "scope_hidden=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ShellSandboxConformance:
    runtime: str
    booter_profile: str
    shell_family: ShellFamily
    ephemeral_per_action: bool
    principal_isolated: bool
    fixed_cwd: bool
    process_tree_kill: bool
    network_disabled: bool
    source_output_bounded: bool
    cpu_bounded: bool
    memory_bounded: bool
    process_count_bounded: bool
    disk_bounded: bool
    no_host_mounts: bool
    no_credentials: bool
    no_docker_socket: bool
    no_devices: bool
    guest_unreachable: bool

    def __post_init__(self) -> None:
        for name in ("runtime", "booter_profile"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"shell_{name}_invalid")
        if not isinstance(self.shell_family, ShellFamily):
            raise TypeError("shell_family_invalid")
        for name in (
            "ephemeral_per_action",
            "principal_isolated",
            "fixed_cwd",
            "process_tree_kill",
            "network_disabled",
            "source_output_bounded",
            "cpu_bounded",
            "memory_bounded",
            "process_count_bounded",
            "disk_bounded",
            "no_host_mounts",
            "no_credentials",
            "no_docker_socket",
            "no_devices",
            "guest_unreachable",
        ):
            _require_bool(getattr(self, name), name)

    @property
    def complete(self) -> bool:
        return (
            self.runtime == "sandbox"
            and self.booter_profile == "ephemeral-owner-action-v1"
            and all(
                (
                    self.ephemeral_per_action,
                    self.principal_isolated,
                    self.fixed_cwd,
                    self.process_tree_kill,
                    self.network_disabled,
                    self.source_output_bounded,
                    self.cpu_bounded,
                    self.memory_bounded,
                    self.process_count_bounded,
                    self.disk_bounded,
                    self.no_host_mounts,
                    self.no_credentials,
                    self.no_docker_socket,
                    self.no_devices,
                    self.guest_unreachable,
                )
            )
        )

    def trace_metadata(self) -> dict[str, str | bool | int]:
        return {
            "schema_version": 1,
            "runtime": self.runtime,
            "booter_profile": self.booter_profile,
            "shell_family": self.shell_family.value,
            "ephemeral": self.ephemeral_per_action,
            "process_tree_kill": self.process_tree_kill,
            "network_disabled": self.network_disabled,
            "resource_limits_complete": all(
                (
                    self.cpu_bounded,
                    self.memory_bounded,
                    self.process_count_bounded,
                    self.disk_bounded,
                    self.source_output_bounded,
                )
            ),
            "complete": self.complete,
        }

    def __repr__(self) -> str:
        return (
            "ShellSandboxConformance("
            f"runtime={self.runtime!r}, booter_profile={self.booter_profile!r}, "
            f"shell_family={self.shell_family.value!r}, complete={self.complete})"
        )


_RUNTIME_EVIDENCE_LOCK = threading.RLock()
_PENDING_RUNTIME_EVIDENCE_SEALS: set[object] = set()
_CANONICAL_RUNTIME_EVIDENCE: weakref.WeakValueDictionary[
    int, "RuntimeConformanceEvidence"
] = weakref.WeakValueDictionary()
_RUNTIME_EVIDENCE_SNAPSHOTS: weakref.WeakKeyDictionary[
    "RuntimeConformanceEvidence", object
] = weakref.WeakKeyDictionary()
_CLAIMED_RUNTIME_EVIDENCE: weakref.WeakSet["RuntimeConformanceEvidence"] = (
    weakref.WeakSet()
)
_RUNTIME_EVIDENCE_MARKER = object()


@dataclass(frozen=True, slots=True, repr=False, eq=False, weakref_slot=True)
class RuntimeConformanceEvidence:
    """Canonical collector output; never a caller-authored boolean attestation."""

    binding: DecisionBinding = field(repr=False)
    operation: OwnerActionOperation
    adapter_id: str
    adapter_version: str
    exact_tool_name: str
    plugin_id: str
    implementation_version: str
    source_qualname: str
    interface_version: str
    schema_digest: str = field(repr=False)
    source_file_digest: str = field(repr=False)
    source_fingerprint: str = field(repr=False)
    collector_epoch: int
    candidate_digest: str = field(repr=False)
    active: bool
    canonical_object_unique: bool
    local_function_tool: bool
    handoff_or_mcp: bool
    background_disabled: bool
    direct_send_blocked: bool
    narrow_context_verified: bool
    return_contract_verified: bool
    permission_wrapper_preserved: bool
    artifact_path: ArtifactPathConformance | None = field(default=None, repr=False)
    grep_runtime: GrepRuntimeConformance | None = field(default=None, repr=False)
    memory_scope: MemoryScopeConformance | None = field(default=None, repr=False)
    shell_sandbox: ShellSandboxConformance | None = field(default=None, repr=False)
    _issuer_seal: InitVar[object | None] = None
    _canonical_marker: object = field(init=False, repr=False, compare=False)

    def __post_init__(self, _issuer_seal: object | None) -> None:
        with _RUNTIME_EVIDENCE_LOCK:
            if (
                _issuer_seal is None
                or _issuer_seal not in _PENDING_RUNTIME_EVIDENCE_SEALS
            ):
                raise AdapterDraftRejected("runtime_issuer_seal_invalid")
            _PENDING_RUNTIME_EVIDENCE_SEALS.remove(_issuer_seal)
        object.__setattr__(self, "_canonical_marker", _RUNTIME_EVIDENCE_MARKER)
        if type(self.binding) is not DecisionBinding:
            raise TypeError("runtime_binding_invalid")
        if type(self.operation) is not OwnerActionOperation:
            raise TypeError("runtime_operation_invalid")
        for name in (
            "adapter_id",
            "adapter_version",
            "exact_tool_name",
            "plugin_id",
            "implementation_version",
            "source_qualname",
            "interface_version",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"runtime_{name}_invalid")
        object.__setattr__(
            self,
            "schema_digest",
            _require_digest(self.schema_digest, "runtime_schema_digest"),
        )
        object.__setattr__(
            self,
            "source_file_digest",
            _require_digest(self.source_file_digest, "runtime_source_file_digest"),
        )
        object.__setattr__(
            self,
            "source_fingerprint",
            _require_digest(self.source_fingerprint, "runtime_source_fingerprint"),
        )
        if type(self.collector_epoch) is not int or self.collector_epoch < 1:
            raise ValueError("runtime_collector_epoch_invalid")
        object.__setattr__(
            self,
            "candidate_digest",
            _require_digest(self.candidate_digest, "runtime_candidate_digest"),
        )
        for name in (
            "active",
            "canonical_object_unique",
            "local_function_tool",
            "handoff_or_mcp",
            "background_disabled",
            "direct_send_blocked",
            "narrow_context_verified",
            "return_contract_verified",
            "permission_wrapper_preserved",
        ):
            _require_bool(getattr(self, name), name)
        optional_shapes = (
            (self.artifact_path, ArtifactPathConformance, "artifact_path"),
            (self.grep_runtime, GrepRuntimeConformance, "grep_runtime"),
            (self.memory_scope, MemoryScopeConformance, "memory_scope"),
            (self.shell_sandbox, ShellSandboxConformance, "shell_sandbox"),
        )
        for value, expected, name in optional_shapes:
            if value is not None and not isinstance(value, expected):
                raise TypeError(f"runtime_{name}_invalid")

    @property
    def is_canonical(self) -> bool:
        with _RUNTIME_EVIDENCE_LOCK:
            expected = _RUNTIME_EVIDENCE_SNAPSHOTS.get(self)
            return (
                self._canonical_marker is _RUNTIME_EVIDENCE_MARKER
                and _CANONICAL_RUNTIME_EVIDENCE.get(id(self)) is self
                and expected is not None
                and expected == _runtime_evidence_state(self)
            )

    def trace_metadata(self) -> dict[str, str | int | bool]:
        if not self.is_canonical:
            raise AdapterDraftRejected("runtime_evidence_not_canonical")
        return {
            "schema_version": 1,
            "operation": self.operation.value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "exact_tool_name": self.exact_tool_name,
            "collector_epoch": self.collector_epoch,
            "active": self.active,
            "canonical_object_unique": self.canonical_object_unique,
            "local_function_tool": self.local_function_tool,
            "handoff_or_mcp": self.handoff_or_mcp,
            "background_disabled": self.background_disabled,
            "direct_send_blocked": self.direct_send_blocked,
            "narrow_context_verified": self.narrow_context_verified,
            "return_contract_verified": self.return_contract_verified,
            "permission_wrapper_preserved": self.permission_wrapper_preserved,
            "has_artifact_path": self.artifact_path is not None,
            "has_grep_runtime": self.grep_runtime is not None,
            "has_memory_scope": self.memory_scope is not None,
            "has_shell_sandbox": self.shell_sandbox is not None,
            "has_source_file_digest": True,
            "has_source_fingerprint": True,
            "has_candidate_digest": True,
            "canonical": self.is_canonical,
        }

    def __repr__(self) -> str:
        if not self.is_canonical:
            return "RuntimeConformanceEvidence(canonical=False, details_hidden=True)"
        return (
            "RuntimeConformanceEvidence("
            f"operation={self.operation.value!r}, adapter_id={self.adapter_id!r}, "
            f"exact_tool_name={self.exact_tool_name!r}, active={self.active}, "
            f"collector_epoch={self.collector_epoch}, "
            f"canonical_object_unique={self.canonical_object_unique}, "
            f"has_path={self.artifact_path is not None}, "
            f"has_scope={self.memory_scope is not None}, "
            f"has_sandbox={self.shell_sandbox is not None}, "
            f"canonical={self.is_canonical}, details_hidden=True)"
        )


def _freeze_runtime_state(value: Any) -> object:
    if dataclasses.is_dataclass(value):
        return (
            type(value),
            tuple(
                (item.name, _freeze_runtime_state(getattr(value, item.name)))
                for item in dataclasses.fields(value)
                if item.name not in {"_canonical_marker"}
            ),
        )
    if isinstance(value, Enum):
        return (type(value), value.value)
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_runtime_state(item) for item in value)
    if isinstance(value, dict):
        return tuple(
            sorted(
                (
                    _freeze_runtime_state(key),
                    _freeze_runtime_state(item),
                )
                for key, item in value.items()
            )
        )
    return value


def _runtime_evidence_state(evidence: RuntimeConformanceEvidence) -> object:
    return _freeze_runtime_state(evidence)


def _issue_runtime_conformance_evidence(
    collector: object,
    candidate: object,
    proof: object,
) -> RuntimeConformanceEvidence:
    """Private bridge that consumes exact collector-owned candidate and proof.

    It intentionally accepts no evidence fields or boolean mapping.  The late
    import avoids a module cycle while requiring the collector's canonical
    identity/one-shot claim before construction.
    """

    from .owner_action_runtime_collector import _claim_runtime_evidence_material

    material = _claim_runtime_evidence_material(collector, candidate, proof)
    issuer_seal = object()
    with _RUNTIME_EVIDENCE_LOCK:
        _PENDING_RUNTIME_EVIDENCE_SEALS.add(issuer_seal)
    try:
        evidence = RuntimeConformanceEvidence(
            binding=material.binding,
            operation=material.operation,
            adapter_id=material.adapter_id,
            adapter_version=material.adapter_version,
            exact_tool_name=material.exact_tool_name,
            plugin_id=material.plugin_id,
            implementation_version=material.implementation_version,
            source_qualname=material.source_qualname,
            interface_version=material.interface_version,
            schema_digest=material.schema_digest,
            source_file_digest=material.source_file_digest,
            source_fingerprint=material.source_fingerprint,
            collector_epoch=material.collector_epoch,
            candidate_digest=material.candidate_digest,
            active=True,
            canonical_object_unique=True,
            local_function_tool=material.local_function_tool,
            handoff_or_mcp=False,
            background_disabled=material.background_disabled,
            direct_send_blocked=material.direct_send_blocked,
            narrow_context_verified=material.narrow_context_verified,
            return_contract_verified=material.return_contract_verified,
            permission_wrapper_preserved=material.permission_wrapper_preserved,
            artifact_path=material.artifact_path,
            grep_runtime=material.grep_runtime,
            memory_scope=material.memory_scope,
            shell_sandbox=None,
            _issuer_seal=issuer_seal,
        )
    finally:
        with _RUNTIME_EVIDENCE_LOCK:
            _PENDING_RUNTIME_EVIDENCE_SEALS.discard(issuer_seal)
    with _RUNTIME_EVIDENCE_LOCK:
        _CANONICAL_RUNTIME_EVIDENCE[id(evidence)] = evidence
        _RUNTIME_EVIDENCE_SNAPSHOTS[evidence] = _runtime_evidence_state(evidence)
    from .owner_action_runtime_collector import _bind_runtime_evidence_material

    _bind_runtime_evidence_material(evidence, material)
    return evidence


def _assert_runtime_evidence_canonical(
    evidence: RuntimeConformanceEvidence,
) -> None:
    if type(evidence) is not RuntimeConformanceEvidence or not evidence.is_canonical:
        raise AdapterDraftRejected("runtime_evidence_not_canonical")
    with _RUNTIME_EVIDENCE_LOCK:
        if evidence in _CLAIMED_RUNTIME_EVIDENCE:
            raise AdapterDraftRejected("runtime_evidence_replayed")


def _claim_runtime_conformance_evidence(
    evidence: RuntimeConformanceEvidence,
    *,
    binding: DecisionBinding,
    operation: OwnerActionOperation,
) -> None:
    with _RUNTIME_EVIDENCE_LOCK:
        _assert_runtime_evidence_canonical(evidence)
        if type(binding) is not DecisionBinding or evidence.binding != binding:
            raise AdapterDraftRejected("runtime_binding_mismatch")
        if type(operation) is not OwnerActionOperation or evidence.operation is not operation:
            raise AdapterDraftRejected("runtime_operation_drift")
        _CLAIMED_RUNTIME_EVIDENCE.add(evidence)


@dataclass(frozen=True, slots=True, repr=False)
class _ArtifactReadExactParameters:
    path: str = field(repr=False)
    offset: int
    limit: int
    path_flavor: ArtifactPathFlavor
    root_digest: str = field(repr=False)
    path_digest: str = field(repr=False)
    preflight_token_digest: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            "_ArtifactReadExactParameters("
            f"offset={self.offset}, limit={self.limit}, "
            f"path_flavor={self.path_flavor.value!r}, path_hidden=True, "
            "has_root_path_preflight_digests=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _ArtifactGrepParameters:
    path: str = field(repr=False)
    literal_pattern: str = field(repr=False)
    escaped_pattern: str = field(repr=False)
    path_flavor: ArtifactPathFlavor
    root_digest: str = field(repr=False)
    path_digest: str = field(repr=False)
    preflight_token_digest: str = field(repr=False)
    literal_pattern_digest: str = field(repr=False)
    glob: str
    context_before: int
    context_after: int
    result_limit: int
    max_files: int
    max_tree_bytes: int
    max_output_bytes: int

    def __repr__(self) -> str:
        return (
            "_ArtifactGrepParameters("
            f"glob={self.glob!r}, context_before={self.context_before}, "
            f"context_after={self.context_after}, result_limit={self.result_limit}, "
            f"max_files={self.max_files}, max_tree_bytes={self.max_tree_bytes}, "
            f"max_output_bytes={self.max_output_bytes}, path_hidden=True, "
            f"path_flavor={self.path_flavor.value!r}, pattern_hidden=True, "
            "has_root_path_preflight_pattern_digests=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _MemoryWriteLiteralParameters:
    memory: str = field(repr=False)
    memory_digest: str = field(repr=False)
    scope_token_digest: str = field(repr=False)
    topics: tuple[str, ...]
    key_facts: tuple[str, ...]
    sentiment: str
    importance: float
    reason: str

    def __repr__(self) -> str:
        return (
            "_MemoryWriteLiteralParameters("
            f"memory_bytes={len(self.memory.encode('utf-8'))}, topics_count=0, "
            f"key_facts_count=0, sentiment={self.sentiment!r}, "
            f"importance={self.importance:.1f}, reason={self.reason!r}, "
            "memory_hidden=True, scope_hidden=True, has_digests=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _SandboxShellOnceParameters:
    command: str = field(repr=False)
    command_digest: str = field(repr=False)
    shell_family: ShellFamily
    background: bool
    timeout_seconds: int
    environment_count: int

    def __repr__(self) -> str:
        return (
            "_SandboxShellOnceParameters("
            f"shell_family={self.shell_family.value!r}, background=False, "
            f"timeout_seconds={self.timeout_seconds}, environment_count=0, "
            "command_hidden=True, has_command_digest=True)"
        )


_ParameterRecord = (
    _ArtifactReadExactParameters
    | _ArtifactGrepParameters
    | _MemoryWriteLiteralParameters
    | _SandboxShellOnceParameters
)


@dataclass(frozen=True, slots=True, repr=False)
class _AdapterMaterial:
    descriptor: AdapterDescriptor
    parameter_record: _ParameterRecord = field(repr=False)
    runtime_evidence: RuntimeConformanceEvidence = field(repr=False)
    source_interface_digest: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            "_AdapterMaterial("
            f"adapter_id={self.descriptor.adapter_id!r}, "
            f"operation={self.descriptor.operation.value!r}, "
            f"parameter_record_type={type(self.parameter_record).__name__!r}, "
            "parameters_hidden=True, evidence_hidden=True, "
            "has_source_interface_digest=True)"
        )


_DRAFT_ISSUANCE_LOCK = threading.RLock()
_PENDING_DRAFT_SEALS: set[object] = set()
_CANONICAL_DRAFTS: weakref.WeakValueDictionary[int, "AdapterDraft"] = (
    weakref.WeakValueDictionary()
)
_DRAFT_MATERIALS: weakref.WeakKeyDictionary["AdapterDraft", _AdapterMaterial] = (
    weakref.WeakKeyDictionary()
)
_CANONICAL_DRAFT_MARKER = object()


@dataclass(frozen=True, slots=True, repr=False, eq=False, weakref_slot=True)
class AdapterDraft:
    """Canonical opaque compiler output consumed by the owner-action controller."""

    binding: DecisionBinding = field(repr=False)
    adapter_id: str
    adapter_version: str
    capability: CapabilityClass
    operation: OwnerActionOperation
    side_effect: ActionSideEffect
    confirmation_policy: ActionConfirmationPolicy
    exact_tool_name: str
    plugin_id: str
    source_qualname: str
    interface_version: str
    schema_digest: str = field(repr=False)
    parameter_digest: str = field(repr=False)
    source_interface_digest: str = field(repr=False)
    call_budget: int = 1
    private_owner_only: bool = True
    _issuer_seal: InitVar[object | None] = None
    _canonical_marker: object = field(init=False, repr=False, compare=False)

    def __post_init__(self, _issuer_seal: object | None) -> None:
        with _DRAFT_ISSUANCE_LOCK:
            if _issuer_seal is None or _issuer_seal not in _PENDING_DRAFT_SEALS:
                raise AdapterDraftRejected("issuer_seal_invalid")
            _PENDING_DRAFT_SEALS.remove(_issuer_seal)
        object.__setattr__(self, "_canonical_marker", _CANONICAL_DRAFT_MARKER)
        if type(self.binding) is not DecisionBinding:
            raise AdapterDraftRejected("draft_binding_invalid")
        descriptor = OWNER_ACTION_ADAPTER_REGISTRY.get(self.operation)
        if descriptor is None:
            raise AdapterDraftRejected("draft_operation_unknown")
        expected = (
            descriptor.adapter_id,
            descriptor.adapter_version,
            descriptor.capability,
            descriptor.side_effect,
            descriptor.confirmation_policy,
            descriptor.exact_tool_name,
            descriptor.plugin_id,
            descriptor.source_qualname,
            descriptor.interface_version,
            descriptor.schema_digest,
            descriptor.call_budget,
            descriptor.private_owner_only,
        )
        actual = (
            self.adapter_id,
            self.adapter_version,
            self.capability,
            self.side_effect,
            self.confirmation_policy,
            self.exact_tool_name,
            self.plugin_id,
            self.source_qualname,
            self.interface_version,
            self.schema_digest,
            self.call_budget,
            self.private_owner_only,
        )
        if actual != expected:
            raise AdapterDraftRejected("draft_descriptor_mismatch")
        _require_digest(self.parameter_digest, "parameter_digest")
        _require_digest(self.source_interface_digest, "source_interface_digest")

    @property
    def is_canonical(self) -> bool:
        with _DRAFT_ISSUANCE_LOCK:
            return (
                self._canonical_marker is _CANONICAL_DRAFT_MARKER
                and _CANONICAL_DRAFTS.get(id(self)) is self
                and self in _DRAFT_MATERIALS
            )

    def assert_current_binding(self, current: DecisionBinding) -> None:
        if type(current) is not DecisionBinding or current != self.binding:
            raise AdapterDraftRejected("decision_binding_mismatch")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": 1,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "capability": self.capability.value,
            "operation": self.operation.value,
            "side_effect": self.side_effect.value,
            "confirmation_policy": self.confirmation_policy.value,
            "exact_tool_name": self.exact_tool_name,
            "call_budget": self.call_budget,
            "private_owner_only": True,
            "has_parameter_digest": True,
            "has_source_interface_digest": True,
            "canonical": self.is_canonical,
        }

    def __repr__(self) -> str:
        return (
            "AdapterDraft("
            f"adapter_id={self.adapter_id!r}, operation={self.operation.value!r}, "
            f"exact_tool_name={self.exact_tool_name!r}, "
            f"confirmation_policy={self.confirmation_policy.value!r}, "
            "call_budget=1, private_owner_only=True, parameters_hidden=True, "
            f"canonical={self.is_canonical})"
        )


def _source_interface_digest(
    descriptor: AdapterDescriptor,
    evidence: RuntimeConformanceEvidence,
) -> str:
    runtime_payload: dict[str, Any] = dict(evidence.trace_metadata())
    if evidence.artifact_path is not None:
        runtime_payload["artifact_path"] = {
            **evidence.artifact_path.trace_metadata(),
            "root_digest": evidence.artifact_path.root_digest,
            "path_digest": evidence.artifact_path.path_digest,
            "preflight_token_digest": evidence.artifact_path.preflight_token_digest,
        }
    if evidence.grep_runtime is not None:
        runtime_payload["grep_runtime"] = evidence.grep_runtime.trace_metadata()
    if evidence.memory_scope is not None:
        runtime_payload["memory_scope"] = {
            **evidence.memory_scope.trace_metadata(),
            "expected_scope_digest": evidence.memory_scope.expected_scope_digest,
            "resolved_scope_digest": _digest_text(
                evidence.memory_scope.resolved_scope_token
            ),
        }
    if evidence.shell_sandbox is not None:
        runtime_payload["shell_sandbox"] = evidence.shell_sandbox.trace_metadata()
    return _digest_payload(
        {
            "adapter_id": descriptor.adapter_id,
            "adapter_version": descriptor.adapter_version,
            "operation": descriptor.operation.value,
            "tool": descriptor.exact_tool_name,
            "plugin": descriptor.plugin_id,
            "implementation": descriptor.implementation_version,
            "source": descriptor.source_qualname,
            "interface": descriptor.interface_version,
            "schema": descriptor.schema_digest,
            "runtime": runtime_payload,
        }
    )


def _parameter_payload(record: _ParameterRecord) -> dict[str, Any]:
    if isinstance(record, _ArtifactReadExactParameters):
        return {
            "kind": "artifact_read_exact",
            "path": record.path,
            "offset": record.offset,
            "limit": record.limit,
            "path_flavor": record.path_flavor.value,
            "root_digest": record.root_digest,
            "preflight_token_digest": record.preflight_token_digest,
        }
    if isinstance(record, _ArtifactGrepParameters):
        return {
            "kind": "artifact_grep",
            "path": record.path,
            "literal": record.literal_pattern,
            "escaped": record.escaped_pattern,
            "path_flavor": record.path_flavor.value,
            "root_digest": record.root_digest,
            "preflight_token_digest": record.preflight_token_digest,
            "glob": record.glob,
            "before": record.context_before,
            "after": record.context_after,
            "result_limit": record.result_limit,
            "max_files": record.max_files,
            "max_tree_bytes": record.max_tree_bytes,
            "max_output_bytes": record.max_output_bytes,
        }
    if isinstance(record, _MemoryWriteLiteralParameters):
        return {
            "kind": "memory_write_literal",
            "memory": record.memory,
            "scope_token_digest": record.scope_token_digest,
            "topics": record.topics,
            "key_facts": record.key_facts,
            "sentiment": record.sentiment,
            "importance": record.importance,
            "reason": record.reason,
        }
    if isinstance(record, _SandboxShellOnceParameters):
        return {
            "kind": "sandbox_shell_once",
            "command": record.command,
            "shell_family": record.shell_family.value,
            "background": record.background,
            "timeout": record.timeout_seconds,
            "environment_count": record.environment_count,
        }
    raise AdapterDraftRejected("parameter_record_unknown")


def _issue_adapter_draft(
    *,
    descriptor: AdapterDescriptor,
    binding: DecisionBinding,
    parameter_record: _ParameterRecord,
    runtime_evidence: RuntimeConformanceEvidence,
) -> AdapterDraft:
    parameter_digest = _digest_payload(_parameter_payload(parameter_record))
    interface_digest = _source_interface_digest(descriptor, runtime_evidence)
    material = _AdapterMaterial(
        descriptor=descriptor,
        parameter_record=parameter_record,
        runtime_evidence=runtime_evidence,
        source_interface_digest=interface_digest,
    )
    issuer_seal = object()
    with _DRAFT_ISSUANCE_LOCK:
        _PENDING_DRAFT_SEALS.add(issuer_seal)
    try:
        draft = AdapterDraft(
            binding=binding,
            adapter_id=descriptor.adapter_id,
            adapter_version=descriptor.adapter_version,
            capability=descriptor.capability,
            operation=descriptor.operation,
            side_effect=descriptor.side_effect,
            confirmation_policy=descriptor.confirmation_policy,
            exact_tool_name=descriptor.exact_tool_name,
            plugin_id=descriptor.plugin_id,
            source_qualname=descriptor.source_qualname,
            interface_version=descriptor.interface_version,
            schema_digest=descriptor.schema_digest,
            parameter_digest=parameter_digest,
            source_interface_digest=interface_digest,
            call_budget=descriptor.call_budget,
            private_owner_only=descriptor.private_owner_only,
            _issuer_seal=issuer_seal,
        )
    finally:
        with _DRAFT_ISSUANCE_LOCK:
            _PENDING_DRAFT_SEALS.discard(issuer_seal)
    with _DRAFT_ISSUANCE_LOCK:
        _CANONICAL_DRAFTS[id(draft)] = draft
        _DRAFT_MATERIALS[draft] = material
    return draft


def _open_adapter_draft(draft: AdapterDraft) -> _AdapterMaterial:
    """Private friend bridge for the canonical controller/executor.

    It returns the module-owned typed record identity, never a caller-supplied or
    generic argument mapping.  It is intentionally omitted from ``__all__``.
    """

    if not isinstance(draft, AdapterDraft) or not draft.is_canonical:
        raise AdapterDraftRejected("adapter_draft_not_canonical")
    with _DRAFT_ISSUANCE_LOCK:
        material = _DRAFT_MATERIALS.get(draft)
    if material is None:
        raise AdapterDraftRejected("adapter_material_missing")
    return material


def _enabled(config: AdapterConfig, operation: OwnerActionOperation) -> bool:
    return {
        OwnerActionOperation.ARTIFACT_READ_EXACT: config.artifact_read_exact_enabled,
        OwnerActionOperation.ARTIFACT_GREP: config.artifact_grep_enabled,
        OwnerActionOperation.MEMORY_WRITE_LITERAL: config.memory_write_literal_enabled,
        OwnerActionOperation.SANDBOX_SHELL_ONCE: config.sandbox_shell_once_enabled,
    }[operation]


def _assert_explicit(message: str, *, verbs: tuple[str, ...]) -> None:
    lowered = message.casefold()
    if (
        any(marker.casefold() in lowered for marker in _NON_EXPLICIT_MARKERS)
        or _NON_EXPLICIT_ENGLISH_RE.search(message)
    ):
        raise AdapterDraftRejected("request_not_explicit")
    matched = False
    for verb in verbs:
        if verb.isascii():
            if re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(verb)}(?![A-Za-z0-9_])",
                message,
                flags=re.IGNORECASE,
            ):
                matched = True
                break
        elif verb.casefold() in lowered:
            matched = True
            break
    if not matched:
        raise AdapterDraftRejected("request_not_explicit")


_LABELED_STRING_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<name>[A-Za-z_][A-Za-z0-9_-]*)\s*=\s*"
    r"(?P<quote>[\"'`])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)


def _extract_string_slot(message: str, name: str) -> tuple[str, tuple[tuple[int, int], ...]]:
    matches = tuple(
        match
        for match in _LABELED_STRING_RE.finditer(message)
        if match.group("name").casefold() == name.casefold()
    )
    if len(matches) != 1:
        raise AdapterDraftRejected(f"{name}_slot_not_unique")
    value = matches[0].group("value")
    if not value:
        raise AdapterDraftRejected(f"{name}_slot_empty")
    return value, tuple((match.start(), match.end()) for match in matches)


def _extract_int_slot(message: str, name: str, *, default: int, maximum: int) -> int:
    marker = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}\s*=", re.IGNORECASE)
    markers = tuple(marker.finditer(message))
    if not markers:
        return default
    if len(markers) != 1:
        raise AdapterDraftRejected(f"{name}_slot_not_unique")
    tail = message[markers[0].end() :]
    match = re.match(r"(?P<value>-?\d+)(?=$|[\s,，;；])", tail)
    if match is None:
        raise AdapterDraftRejected(f"{name}_slot_invalid")
    value = int(match.group("value"))
    if value < 0 or value > maximum:
        raise AdapterDraftRejected(f"{name}_slot_out_of_range")
    return value


def _assignment_names(message: str) -> tuple[str, ...]:
    masked = list(message)
    for match in _LABELED_STRING_RE.finditer(message):
        for index in range(match.start("value"), match.end("value")):
            masked[index] = " "
    return tuple(
        match.group("name").casefold()
        for match in re.finditer(
            r"(?<![A-Za-z0-9_])(?P<name>[A-Za-z_][A-Za-z0-9_-]*)\s*=",
            "".join(masked),
        )
    )


def _reject_unknown_assignments(message: str, allowed: frozenset[str]) -> None:
    if any(name not in allowed for name in _assignment_names(message)):
        raise AdapterDraftRejected("parameter_slot_unknown")


def _validate_path_text(raw: str) -> str:
    if type(raw) is not str:
        raise AdapterDraftRejected("artifact_path_invalid")
    value = raw.strip()
    if value != raw:
        raise AdapterDraftRejected("artifact_path_whitespace_forbidden")
    if not value or _CONTROL_RE.search(value):
        raise AdapterDraftRejected("artifact_path_invalid")
    if "$" in value or "%" in value or "~" in value:
        raise AdapterDraftRejected("artifact_path_expansion_forbidden")
    return value


def _normalize_artifact_path(
    raw: str,
    flavor: ArtifactPathFlavor,
    *,
    is_root: bool = False,
) -> str:
    value = _validate_path_text(raw)
    if flavor is ArtifactPathFlavor.WINDOWS:
        lowered = value.casefold()
        if (
            value.startswith(("\\\\", "//", "\\??\\"))
            or lowered.startswith(("\\\\?\\", "\\\\.\\", "\\device\\"))
            or "/" in value
            or not re.match(r"^[A-Za-z]:\\", value)
        ):
            raise AdapterDraftRejected("artifact_path_flavor_mismatch")
        if value.count(":") != 1 or value[1] != ":":
            raise AdapterDraftRejected("artifact_path_ads_forbidden")
        path = PureWindowsPath(value)
        parts = path.parts[1:]
        for part in parts:
            if part in {".", ".."}:
                raise AdapterDraftRejected("artifact_path_traversal_forbidden")
            if part.endswith((" ", ".")):
                raise AdapterDraftRejected("artifact_path_component_invalid")
            stem = part.split(".", 1)[0].casefold()
            if stem in _WINDOWS_DEVICE_NAMES:
                raise AdapterDraftRejected("artifact_path_device_forbidden")
        normalized = str(path)
    else:
        if (
            not value.startswith("/")
            or value.startswith("//")
            or "\\" in value
            or re.match(r"^[A-Za-z]:", value)
        ):
            raise AdapterDraftRejected("artifact_path_flavor_mismatch")
        if flavor is ArtifactPathFlavor.WSL_POSIX and re.match(
            r"^/mnt/[A-Za-z](?:/|$)",
            value,
            flags=re.IGNORECASE,
        ):
            raise AdapterDraftRejected("artifact_path_cross_namespace_forbidden")
        path = PurePosixPath(value)
        for part in path.parts[1:]:
            if part in {".", ".."}:
                raise AdapterDraftRejected("artifact_path_traversal_forbidden")
        normalized = str(path)
    if not is_root and normalized in {"/", "\\"}:
        raise AdapterDraftRejected("artifact_path_target_invalid")
    return normalized


def _assert_under_root(
    path: str,
    root: str,
    flavor: ArtifactPathFlavor,
) -> None:
    if flavor is ArtifactPathFlavor.WINDOWS:
        path_parts = tuple(part.casefold() for part in PureWindowsPath(path).parts)
        root_parts = tuple(part.casefold() for part in PureWindowsPath(root).parts)
    else:
        path_parts = PurePosixPath(path).parts
        root_parts = PurePosixPath(root).parts
    if len(path_parts) < len(root_parts) or path_parts[: len(root_parts)] != root_parts:
        raise AdapterDraftRejected("artifact_path_outside_root")


def _assert_text_extension(path: str, flavor: ArtifactPathFlavor) -> None:
    suffix = (
        PureWindowsPath(path).suffix
        if flavor is ArtifactPathFlavor.WINDOWS
        else PurePosixPath(path).suffix
    ).casefold()
    if suffix not in _TEXT_EXTENSIONS:
        raise AdapterDraftRejected("artifact_extension_forbidden")


def _validate_runtime_common(
    *,
    descriptor: AdapterDescriptor,
    proposal: OwnerActionProposal,
    evidence: RuntimeConformanceEvidence | None,
) -> RuntimeConformanceEvidence:
    if evidence is None:
        raise AdapterDraftRejected("runtime_evidence_required")
    if type(evidence) is not RuntimeConformanceEvidence:
        raise AdapterDraftRejected("runtime_evidence_invalid")
    _assert_runtime_evidence_canonical(evidence)
    if evidence.binding != proposal.binding:
        raise AdapterDraftRejected("runtime_binding_mismatch")
    if evidence.operation is not descriptor.operation:
        raise AdapterDraftRejected("runtime_operation_drift")
    drift_fields = (
        ("adapter_id", descriptor.adapter_id, "runtime_adapter_id_drift"),
        ("adapter_version", descriptor.adapter_version, "runtime_adapter_version_drift"),
        ("exact_tool_name", descriptor.exact_tool_name, "runtime_tool_name_drift"),
        ("plugin_id", descriptor.plugin_id, "runtime_plugin_id_drift"),
        (
            "implementation_version",
            descriptor.implementation_version,
            "runtime_implementation_version_drift",
        ),
        ("source_qualname", descriptor.source_qualname, "runtime_source_drift"),
        (
            "interface_version",
            descriptor.interface_version,
            "runtime_interface_version_drift",
        ),
        ("schema_digest", descriptor.schema_digest, "runtime_schema_drift"),
    )
    for field_name, expected, reason in drift_fields:
        if getattr(evidence, field_name) != expected:
            raise AdapterDraftRejected(reason)
    if not (
        evidence.active
        and evidence.canonical_object_unique
        and evidence.local_function_tool
        and not evidence.handoff_or_mcp
        and evidence.background_disabled
        and evidence.direct_send_blocked
        and evidence.narrow_context_verified
        and evidence.return_contract_verified
    ):
        raise AdapterDraftRejected("runtime_object_conformance_incomplete")
    return evidence


def _validate_artifact_attestation(
    *,
    attestation: ArtifactPathConformance | None,
    binding: DecisionBinding,
    root: str,
    path: str,
    expected_kinds: frozenset[ArtifactTargetKind],
) -> ArtifactPathConformance:
    if not isinstance(attestation, ArtifactPathConformance):
        raise AdapterDraftRejected("artifact_path_conformance_required")
    if attestation.root_digest != _digest_text(root):
        raise AdapterDraftRejected("artifact_root_attestation_mismatch")
    if attestation.path_digest != _digest_text(path):
        raise AdapterDraftRejected("artifact_path_attestation_mismatch")
    if attestation.binding_revision != binding.conversation_revision:
        raise AdapterDraftRejected("artifact_preflight_revision_mismatch")
    if attestation.target_kind not in expected_kinds:
        raise AdapterDraftRejected("artifact_target_kind_mismatch")
    if not attestation.preflight_token_digest:
        raise AdapterDraftRejected("artifact_toctou_evidence_missing")
    if not (
        attestation.exists
        and attestation.realpath_within_root
        and attestation.components_lstat_verified
        and attestation.symlink_free
        and attestation.reparse_free
        and attestation.mount_escape_free
        and attestation.root_exclusive_trusted_writers
        and attestation.replacement_protected
        and attestation.plain_text_magic
        and attestation.allowed_extensions_only
    ):
        raise AdapterDraftRejected("artifact_path_conformance_incomplete")
    if (
        attestation.target_kind is ArtifactTargetKind.PLAIN_TEXT_FILE
        and attestation.hardlink_count != 1
    ):
        raise AdapterDraftRejected("artifact_hardlink_forbidden")
    return attestation


def _artifact_root(config: AdapterConfig) -> tuple[str, ArtifactPathFlavor]:
    if not config.artifact_root or config.path_flavor is None:
        raise AdapterDraftRejected("artifact_root_config_required")
    root = _normalize_artifact_path(
        config.artifact_root,
        config.path_flavor,
        is_root=True,
    )
    return root, config.path_flavor


def _compile_artifact_read(
    *,
    proposal: OwnerActionProposal,
    message: str,
    config: AdapterConfig,
    descriptor: AdapterDescriptor,
    runtime_evidence: RuntimeConformanceEvidence | None,
) -> tuple[_ArtifactReadExactParameters, RuntimeConformanceEvidence]:
    _assert_explicit(message, verbs=("读取", "读一下", "查看文件", "打开文件", "read file"))
    if "```" in message:
        raise AdapterDraftRejected("request_not_explicit")
    path_raw, _ = _extract_string_slot(message, "path")
    _reject_unknown_assignments(message, frozenset({"path", "offset", "limit"}))
    offset = _extract_int_slot(
        message,
        "offset",
        default=ARTIFACT_READ_DEFAULT_OFFSET,
        maximum=ARTIFACT_READ_MAX_OFFSET,
    )
    limit = _extract_int_slot(
        message,
        "limit",
        default=ARTIFACT_READ_DEFAULT_LIMIT,
        maximum=ARTIFACT_READ_MAX_LIMIT,
    )
    if limit < 1:
        raise AdapterDraftRejected("limit_slot_out_of_range")
    root, flavor = _artifact_root(config)
    path = _normalize_artifact_path(path_raw, flavor)
    _assert_under_root(path, root, flavor)
    _assert_text_extension(path, flavor)
    evidence = _validate_runtime_common(
        descriptor=descriptor,
        proposal=proposal,
        evidence=runtime_evidence,
    )
    attestation = _validate_artifact_attestation(
        attestation=evidence.artifact_path,
        binding=proposal.binding,
        root=root,
        path=path,
        expected_kinds=frozenset({ArtifactTargetKind.PLAIN_TEXT_FILE}),
    )
    if attestation.target_size_bytes > ARTIFACT_READ_MAX_TARGET_BYTES:
        raise AdapterDraftRejected("artifact_target_too_large")
    return (
        _ArtifactReadExactParameters(
            path=path,
            offset=offset,
            limit=limit,
            path_flavor=flavor,
            root_digest=attestation.root_digest,
            path_digest=_digest_text(path),
            preflight_token_digest=attestation.preflight_token_digest,
        ),
        evidence,
    )


def _compile_artifact_grep(
    *,
    proposal: OwnerActionProposal,
    message: str,
    config: AdapterConfig,
    descriptor: AdapterDescriptor,
    runtime_evidence: RuntimeConformanceEvidence | None,
) -> tuple[_ArtifactGrepParameters, RuntimeConformanceEvidence]:
    _assert_explicit(message, verbs=("搜索", "查找", "检索", "grep"))
    if "```" in message:
        raise AdapterDraftRejected("request_not_explicit")
    path_raw, _ = _extract_string_slot(message, "path")
    literal, _ = _extract_string_slot(message, "literal")
    _reject_unknown_assignments(message, frozenset({"path", "literal"}))
    if _CONTROL_RE.search(literal) or len(literal.encode("utf-8")) > ARTIFACT_GREP_MAX_PATTERN_BYTES:
        raise AdapterDraftRejected("literal_pattern_invalid")
    root, flavor = _artifact_root(config)
    path = _normalize_artifact_path(path_raw, flavor)
    _assert_under_root(path, root, flavor)
    evidence = _validate_runtime_common(
        descriptor=descriptor,
        proposal=proposal,
        evidence=runtime_evidence,
    )
    attestation = _validate_artifact_attestation(
        attestation=evidence.artifact_path,
        binding=proposal.binding,
        root=root,
        path=path,
        expected_kinds=frozenset(
            {
                ArtifactTargetKind.PLAIN_TEXT_FILE,
                ArtifactTargetKind.PLAIN_TEXT_TREE,
            }
        ),
    )
    if attestation.target_kind is ArtifactTargetKind.PLAIN_TEXT_FILE:
        _assert_text_extension(path, flavor)
    if (
        attestation.tree_file_count > ARTIFACT_GREP_MAX_FILES
        or attestation.tree_total_bytes > ARTIFACT_GREP_MAX_TREE_BYTES
    ):
        raise AdapterDraftRejected("grep_corpus_budget_exceeded")
    grep_runtime = evidence.grep_runtime
    if not isinstance(grep_runtime, GrepRuntimeConformance):
        raise AdapterDraftRejected("grep_runtime_conformance_required")
    if not (
        grep_runtime.engine == "rg"
        and grep_runtime.literal_escape_verified
        and grep_runtime.argv_or_posix_shell_verified
        and grep_runtime.fallback_disabled
        and grep_runtime.preflight_budget_enforced
        and grep_runtime.hard_deadline_enforced
        and grep_runtime.source_output_cap_bytes == ARTIFACT_GREP_OUTPUT_BYTES
    ):
        raise AdapterDraftRejected("grep_runtime_conformance_incomplete")
    escaped = re.escape(literal)
    return (
        _ArtifactGrepParameters(
            path=path,
            literal_pattern=literal,
            escaped_pattern=escaped,
            path_flavor=flavor,
            root_digest=attestation.root_digest,
            path_digest=_digest_text(path),
            preflight_token_digest=attestation.preflight_token_digest,
            literal_pattern_digest=_digest_text(literal),
            glob=ARTIFACT_GREP_FIXED_GLOB,
            context_before=ARTIFACT_GREP_CONTEXT_LINES,
            context_after=ARTIFACT_GREP_CONTEXT_LINES,
            result_limit=ARTIFACT_GREP_RESULT_LIMIT,
            max_files=ARTIFACT_GREP_MAX_FILES,
            max_tree_bytes=ARTIFACT_GREP_MAX_TREE_BYTES,
            max_output_bytes=ARTIFACT_GREP_OUTPUT_BYTES,
        ),
        evidence,
    )


_REMEMBER_RE = re.compile(
    r"^\s*(?:(?:请(?:你)?|麻烦(?:你)?|帮我)\s*)?记住\s*[:：]\s*(?P<literal>.+?)\s*$",
    re.DOTALL,
)
_MEMORY_AUTHORITY_TEXT_RE = re.compile(
    r"(?:\bscope\s*[:=]|作用域\s*[:=]|(?:全局|群|共享)\s*(?:记忆|scope)|"
    r"(?:存|写|保存|记录).{0,6}(?:全局|群内|群组|群聊|共享)|"
    r"替\s*\S{1,32}\s*(?:记住|保存|记录|写入)|"
    r"给\s*\S{1,32}\s*(?:记住|保存|记录|写入)|"
    r"(?:subject|sender|owner|user(?:_id)?)\s*[:=])",
    re.IGNORECASE,
)


def _compile_memory_write(
    *,
    proposal: OwnerActionProposal,
    message: str,
    descriptor: AdapterDescriptor,
    runtime_evidence: RuntimeConformanceEvidence | None,
) -> tuple[_MemoryWriteLiteralParameters, RuntimeConformanceEvidence]:
    if any(marker in message for marker in ("不要", "别记", "不用", "无需", "引用", "教程", "示例")):
        raise AdapterDraftRejected("request_not_explicit")
    match = _REMEMBER_RE.fullmatch(message)
    if match is None:
        raise AdapterDraftRejected("memory_literal_not_explicit")
    literal = match.group("literal").strip()
    if (
        not literal
        or _CONTROL_RE.search(literal)
        or len(literal.encode("utf-8")) > MEMORY_MAX_LITERAL_BYTES
    ):
        raise AdapterDraftRejected("memory_literal_invalid")
    if _MEMORY_AUTHORITY_TEXT_RE.search(message):
        raise AdapterDraftRejected("memory_authority_text_forbidden")
    evidence = _validate_runtime_common(
        descriptor=descriptor,
        proposal=proposal,
        evidence=runtime_evidence,
    )
    if not evidence.permission_wrapper_preserved:
        raise AdapterDraftRejected("memory_permission_wrapper_required")
    scope = evidence.memory_scope
    if not isinstance(scope, MemoryScopeConformance):
        raise AdapterDraftRejected("memory_scope_conformance_required")
    if scope.mode not in {
        MemoryScopeMode.PRIVATE_SESSION,
        MemoryScopeMode.PRIVATE_USER,
    }:
        raise AdapterDraftRejected("memory_scope_not_private")
    if not (
        scope.private_chat
        and scope.current_owner_subject
        and not scope.identity_degraded
        and scope.global_or_shared_alias_disabled
        and scope.dashboard_permission_admin
        and scope.owner_is_astrbot_admin
        and scope.permission_wrapper_preserved
    ):
        raise AdapterDraftRejected("memory_scope_conformance_incomplete")
    if scope.expected_scope_token != scope.resolved_scope_token:
        raise AdapterDraftRejected("memory_scope_token_mismatch")
    return (
        _MemoryWriteLiteralParameters(
            memory=literal,
            memory_digest=_digest_text(literal),
            scope_token_digest=scope.expected_scope_digest,
            topics=(),
            key_facts=(),
            sentiment="neutral",
            importance=MEMORY_FIXED_IMPORTANCE,
            reason="owner_explicit_literal",
        ),
        evidence,
    )


_FENCED_SHELL_RE = re.compile(
    r"```(?P<label>[A-Za-z0-9_-]+)[ \t]*\r?\n(?P<command>.*?)\r?\n```",
    re.DOTALL,
)
_DETACH_RE = re.compile(
    r"(?:\b(?:nohup|setsid|disown|start-process)\b|"
    r"(?:^|[;\n])\s*start\s+|(?<!&)&(?!&)|\bwsl(?:\.exe)?\b)",
    re.IGNORECASE,
)


def _compile_shell(
    *,
    proposal: OwnerActionProposal,
    message: str,
    config: AdapterConfig,
    descriptor: AdapterDescriptor,
    runtime_evidence: RuntimeConformanceEvidence | None,
) -> tuple[_SandboxShellOnceParameters, RuntimeConformanceEvidence]:
    matches = tuple(_FENCED_SHELL_RE.finditer(message))
    if len(matches) != 1 or message.count("```") != 2:
        raise AdapterDraftRejected("shell_block_not_unique")
    match = matches[0]
    outside = (message[: match.start()] + message[match.end() :]).strip()
    _assert_explicit(outside, verbs=("执行", "运行", "execute", "run"))
    if config.shell_family is None:
        raise AdapterDraftRejected("shell_family_config_required")
    label = match.group("label").casefold()
    allowed_labels = {
        ShellFamily.POSIX_SH: frozenset({"sh", "bash", "shell"}),
        ShellFamily.POWERSHELL: frozenset({"powershell", "pwsh"}),
    }[config.shell_family]
    if label not in allowed_labels:
        raise AdapterDraftRejected("shell_family_mismatch")
    command = match.group("command")
    if (
        not command.strip()
        or "\x00" in command
        or len(command.encode("utf-8")) > SHELL_MAX_COMMAND_BYTES
    ):
        raise AdapterDraftRejected("shell_command_invalid")
    if _DETACH_RE.search(command):
        raise AdapterDraftRejected("shell_detach_forbidden")
    lowered = command.casefold()
    if config.shell_family is ShellFamily.POSIX_SH and re.search(
        r"\b(?:powershell|pwsh|cmd(?:\.exe)?\s+/c)\b",
        lowered,
    ):
        raise AdapterDraftRejected("shell_cross_family_forbidden")
    if config.shell_family is ShellFamily.POWERSHELL and re.search(
        r"\b(?:bash|zsh|sh)\s+-c\b",
        lowered,
    ):
        raise AdapterDraftRejected("shell_cross_family_forbidden")
    evidence = _validate_runtime_common(
        descriptor=descriptor,
        proposal=proposal,
        evidence=runtime_evidence,
    )
    sandbox = evidence.shell_sandbox
    if not isinstance(sandbox, ShellSandboxConformance):
        raise AdapterDraftRejected("shell_sandbox_conformance_required")
    if sandbox.shell_family is not config.shell_family or not sandbox.complete:
        raise AdapterDraftRejected("shell_sandbox_conformance_incomplete")
    record = _SandboxShellOnceParameters(
        command=command,
        command_digest=_digest_text(command),
        shell_family=config.shell_family,
        background=False,
        timeout_seconds=30,
        environment_count=0,
    )
    if descriptor.hard_disabled or _SHELL_ROLLOUT_HARD_DISABLED:
        raise AdapterDraftRejected("adapter_hard_disabled")
    return record, evidence


def compile_owner_action_draft(
    proposal: OwnerActionProposal,
    current_message: str,
    config: AdapterConfig,
    runtime_evidence: RuntimeConformanceEvidence | None = None,
) -> AdapterDraft:
    """Compile one current-message proposal into a canonical opaque draft.

    No history, reference body, memory, nickname, model arguments or generic mapping
    can be supplied to this function.  Any missing config/evidence fails closed.
    """

    if not isinstance(proposal, OwnerActionProposal):
        raise AdapterDraftRejected("owner_action_proposal_required")
    if type(current_message) is not str or not current_message:
        raise AdapterDraftRejected("current_message_required")
    if _digest_text(current_message) != proposal.binding.current_content_digest:
        raise AdapterDraftRejected("current_message_binding_mismatch")
    if not isinstance(config, AdapterConfig):
        raise AdapterDraftRejected("adapter_config_required")
    descriptor = OWNER_ACTION_ADAPTER_REGISTRY.get(proposal.operation)
    if descriptor is None or proposal.capability is not descriptor.capability:
        raise AdapterDraftRejected("adapter_operation_unavailable")
    if proposal.confidence < 0.9:
        raise AdapterDraftRejected("proposal_confidence_insufficient")
    if not _enabled(config, proposal.operation):
        raise AdapterDraftRejected("adapter_disabled")
    if descriptor.hard_disabled or proposal.operation is OwnerActionOperation.SANDBOX_SHELL_ONCE:
        raise AdapterDraftRejected("adapter_hard_disabled")

    if proposal.operation is OwnerActionOperation.ARTIFACT_READ_EXACT:
        record, evidence = _compile_artifact_read(
            proposal=proposal,
            message=current_message,
            config=config,
            descriptor=descriptor,
            runtime_evidence=runtime_evidence,
        )
    elif proposal.operation is OwnerActionOperation.ARTIFACT_GREP:
        record, evidence = _compile_artifact_grep(
            proposal=proposal,
            message=current_message,
            config=config,
            descriptor=descriptor,
            runtime_evidence=runtime_evidence,
        )
    elif proposal.operation is OwnerActionOperation.MEMORY_WRITE_LITERAL:
        record, evidence = _compile_memory_write(
            proposal=proposal,
            message=current_message,
            descriptor=descriptor,
            runtime_evidence=runtime_evidence,
        )
    elif proposal.operation is OwnerActionOperation.SANDBOX_SHELL_ONCE:
        record, evidence = _compile_shell(
            proposal=proposal,
            message=current_message,
            config=config,
            descriptor=descriptor,
            runtime_evidence=runtime_evidence,
        )
    else:  # pragma: no cover - enum and registry are closed above.
        raise AdapterDraftRejected("adapter_operation_unavailable")

    _claim_runtime_conformance_evidence(
        evidence,
        binding=proposal.binding,
        operation=proposal.operation,
    )
    return _issue_adapter_draft(
        descriptor=descriptor,
        binding=proposal.binding,
        parameter_record=record,
        runtime_evidence=evidence,
    )


__all__ = [
    "ARTIFACT_GREP_CONTEXT_LINES",
    "ARTIFACT_GREP_FIXED_GLOB",
    "ARTIFACT_GREP_MAX_FILES",
    "ARTIFACT_GREP_MAX_TREE_BYTES",
    "ARTIFACT_GREP_OUTPUT_BYTES",
    "AdapterConfig",
    "AdapterDescriptor",
    "AdapterDraft",
    "AdapterDraftRejected",
    "ArtifactPathConformance",
    "ArtifactPathFlavor",
    "ArtifactTargetKind",
    "GrepRuntimeConformance",
    "MEMORY_FIXED_IMPORTANCE",
    "MemoryScopeConformance",
    "MemoryScopeMode",
    "OWNER_ACTION_ADAPTER_REGISTRY",
    "RuntimeConformanceEvidence",
    "ShellFamily",
    "ShellSandboxConformance",
    "compile_owner_action_draft",
]
