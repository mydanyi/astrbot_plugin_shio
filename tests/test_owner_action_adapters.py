from __future__ import annotations

import dataclasses
import hashlib
import inspect
import unittest

from astrbot_plugin_shio.core.capability_policy import CapabilityClass
from astrbot_plugin_shio.core.contracts import (
    ActionConfirmationPolicy,
    ActionSideEffect,
    DecisionBinding,
    OwnerActionOperation,
    OwnerActionProposal,
)
from astrbot_plugin_shio.core import owner_action_adapters as adapters
from astrbot_plugin_shio.core import owner_action_runtime_collector as runtime_collector
from astrbot_plugin_shio.tests import (
    test_owner_action_runtime_collector as runtime_fixture,
)


def _binding(message: str) -> DecisionBinding:
    return DecisionBinding(
        scope_key="platform:test|bot:shio|private:owner-a",
        session_id="session-owner-a",
        current_message_id="message-current",
        current_sender_key="platform:test|user:owner-a",
        current_content_digest=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        conversation_revision=7,
        generation_epoch=11,
        trace_id="a" * 32,
    )


def _proposal(
    message: str,
    operation: OwnerActionOperation,
) -> OwnerActionProposal:
    capability = {
        OwnerActionOperation.ARTIFACT_READ_EXACT: CapabilityClass.ARTIFACT_READ,
        OwnerActionOperation.ARTIFACT_GREP: CapabilityClass.ARTIFACT_READ,
        OwnerActionOperation.MEMORY_WRITE_LITERAL: CapabilityClass.MEMORY_WRITE,
        OwnerActionOperation.SANDBOX_SHELL_ONCE: CapabilityClass.SHELL_EXEC,
    }[operation]
    return OwnerActionProposal(
        binding=_binding(message),
        capability=capability,
        operation=operation,
        confidence=0.99,
        reason_codes=("current_explicit_action",),
    )


def _path_attestation(
    *,
    message: str,
    root: str,
    path: str,
    target_kind: adapters.ArtifactTargetKind,
    target_size_bytes: int = 128,
    tree_file_count: int = 1,
    tree_total_bytes: int = 128,
    **changes: object,
) -> adapters.ArtifactPathConformance:
    values: dict[str, object] = {
        "root_digest": hashlib.sha256(root.encode("utf-8")).hexdigest(),
        "path_digest": hashlib.sha256(path.encode("utf-8")).hexdigest(),
        "preflight_token_digest": "b" * 64,
        "binding_revision": _binding(message).conversation_revision,
        "target_kind": target_kind,
        "target_size_bytes": target_size_bytes,
        "tree_file_count": tree_file_count,
        "tree_total_bytes": tree_total_bytes,
        "exists": True,
        "realpath_within_root": True,
        "components_lstat_verified": True,
        "symlink_free": True,
        "reparse_free": True,
        "mount_escape_free": True,
        "hardlink_count": 1,
        "root_exclusive_trusted_writers": True,
        "replacement_protected": True,
        "plain_text_magic": True,
        "allowed_extensions_only": True,
    }
    values.update(changes)
    return adapters.ArtifactPathConformance(**values)


def _grep_attestation(**changes: object) -> adapters.GrepRuntimeConformance:
    values: dict[str, object] = {
        "engine": "rg",
        "literal_escape_verified": True,
        "argv_or_posix_shell_verified": True,
        "fallback_disabled": True,
        "preflight_budget_enforced": True,
        "hard_deadline_enforced": True,
        "source_output_cap_bytes": adapters.ARTIFACT_GREP_OUTPUT_BYTES,
    }
    values.update(changes)
    return adapters.GrepRuntimeConformance(**values)


def _scope_attestation(
    *,
    expected: str = "owner-private-scope-token",
    resolved: str = "owner-private-scope-token",
    **changes: object,
) -> adapters.MemoryScopeConformance:
    values: dict[str, object] = {
        "mode": adapters.MemoryScopeMode.PRIVATE_USER,
        "expected_scope_token": expected,
        "resolved_scope_token": resolved,
        "private_chat": True,
        "current_owner_subject": True,
        "identity_degraded": False,
        "global_or_shared_alias_disabled": True,
        "dashboard_permission_admin": True,
        "owner_is_astrbot_admin": True,
        "permission_wrapper_preserved": True,
    }
    values.update(changes)
    return adapters.MemoryScopeConformance(**values)


def _shell_attestation(**changes: object) -> adapters.ShellSandboxConformance:
    values: dict[str, object] = {
        "runtime": "sandbox",
        "booter_profile": "ephemeral-owner-action-v1",
        "shell_family": adapters.ShellFamily.POSIX_SH,
        "ephemeral_per_action": True,
        "principal_isolated": True,
        "fixed_cwd": True,
        "process_tree_kill": True,
        "network_disabled": True,
        "source_output_bounded": True,
        "cpu_bounded": True,
        "memory_bounded": True,
        "process_count_bounded": True,
        "disk_bounded": True,
        "no_host_mounts": True,
        "no_credentials": True,
        "no_docker_socket": True,
        "no_devices": True,
        "guest_unreachable": True,
    }
    values.update(changes)
    return adapters.ShellSandboxConformance(**values)


def _runtime_evidence(
    *,
    message: str,
    operation: OwnerActionOperation,
    path_attestation: adapters.ArtifactPathConformance | None = None,
    grep_attestation: adapters.GrepRuntimeConformance | None = None,
    scope_attestation: adapters.MemoryScopeConformance | None = None,
    shell_attestation: adapters.ShellSandboxConformance | None = None,
    **changes: object,
) -> adapters.RuntimeConformanceEvidence:
    if changes:
        raise AssertionError("runtime evidence fields are not caller-authorable")
    if shell_attestation is not None or operation is OwnerActionOperation.SANDBOX_SHELL_ONCE:
        raise AssertionError("shell has no runtime candidate")
    if operation is OwnerActionOperation.ARTIFACT_READ_EXACT:
        tool = runtime_fixture._tool(
            runtime_fixture.FileReadTool,
            "astrbot_file_read_tool",
            runtime_fixture.FILE_READ_SCHEMA,
        )
        manager = runtime_fixture._BuiltinManager(tool)
    elif operation is OwnerActionOperation.ARTIFACT_GREP:
        tool = runtime_fixture._tool(
            runtime_fixture.GrepTool,
            "astrbot_grep_tool",
            runtime_fixture.GREP_SCHEMA,
        )
        manager = runtime_fixture._BuiltinManager(tool)
    elif operation is OwnerActionOperation.MEMORY_WRITE_LITERAL:
        raw = runtime_fixture._tool(
            runtime_fixture.MemoryMemorizeTool,
            "memorize_long_term_memory",
            runtime_fixture.MEMORY_SCHEMA,
        )
        wrapped = runtime_fixture._tool(
            runtime_fixture.PermissionGuardedTool,
            "memorize_long_term_memory",
            runtime_fixture.MEMORY_SCHEMA,
        )
        wrapped._wrapped = raw
        wrapped.handler_module_path = type(raw).__module__
        manager = runtime_fixture._MemoryManager(raw, wrapped)
    else:  # pragma: no cover - closed above.
        raise AssertionError("unknown operation")
    runtime = runtime_collector._build_test_runtime_collector(
        manager,
        operation=operation,
        implementation_version=(
            adapters.OWNER_ACTION_ADAPTER_REGISTRY[operation].implementation_version
        ),
        memory_scope_probe=(
            (lambda: "private_user")
            if operation is OwnerActionOperation.MEMORY_WRITE_LITERAL
            else None
        ),
    )
    candidate = runtime.collect(_binding(message), operation)
    proof = runtime_collector._issue_test_operation_proof(
        artifact_path=path_attestation,
        grep_runtime=grep_attestation,
        memory_scope=scope_attestation,
    )
    return runtime.issue_evidence(candidate, proof)


def _compile(
    message: str,
    operation: OwnerActionOperation,
    config: adapters.AdapterConfig,
    evidence: adapters.RuntimeConformanceEvidence | None,
) -> adapters.AdapterDraft:
    return adapters.compile_owner_action_draft(
        _proposal(message, operation),
        message,
        config,
        evidence,
    )


class AdapterRegistryAndSealTests(unittest.TestCase):
    def test_default_config_is_explicitly_all_disabled(self):
        config = adapters.AdapterConfig()
        self.assertEqual(config.enabled_count, 0)
        self.assertFalse(config.artifact_read_exact_enabled)
        self.assertFalse(config.artifact_grep_enabled)
        self.assertFalse(config.memory_write_literal_enabled)
        self.assertFalse(config.sandbox_shell_once_enabled)
        self.assertNotIn("root", repr(config).lower())

        for operation in OwnerActionOperation:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    adapters.AdapterDraftRejected,
                    "adapter_disabled",
                ):
                    _compile("请执行当前操作", operation, config, None)

    def test_registry_fixes_tool_source_schema_adapter_and_version(self):
        expected = {
            OwnerActionOperation.ARTIFACT_READ_EXACT: (
                "artifact-read-exact",
                "astrbot_file_read_tool",
                "astrbot.builtin",
                "astrbot.core.tools.computer_tools.fs.FileReadTool",
            ),
            OwnerActionOperation.ARTIFACT_GREP: (
                "artifact-grep",
                "astrbot_grep_tool",
                "astrbot.builtin",
                "astrbot.core.tools.computer_tools.fs.GrepTool",
            ),
            OwnerActionOperation.MEMORY_WRITE_LITERAL: (
                "memory-write-literal",
                "memorize_long_term_memory",
                "astrbot_plugin_livingmemory",
                "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.MemoryMemorizeTool",
            ),
            OwnerActionOperation.SANDBOX_SHELL_ONCE: (
                "sandbox-shell-once",
                "astrbot_execute_shell",
                "astrbot.builtin",
                "astrbot.core.tools.computer_tools.shell.ExecuteShellTool",
            ),
        }
        self.assertEqual(set(adapters.OWNER_ACTION_ADAPTER_REGISTRY), set(expected))
        for operation, values in expected.items():
            with self.subTest(operation=operation):
                spec = adapters.OWNER_ACTION_ADAPTER_REGISTRY[operation]
                self.assertEqual(
                    (spec.adapter_id, spec.exact_tool_name, spec.plugin_id, spec.source_qualname),
                    values,
                )
                self.assertRegex(spec.schema_digest, r"^[0-9a-f]{64}$")
                self.assertEqual(spec.call_budget, 1)
        self.assertTrue(
            adapters.OWNER_ACTION_ADAPTER_REGISTRY[
                OwnerActionOperation.SANDBOX_SHELL_ONCE
            ].hard_disabled
        )

    def test_public_compiler_has_no_history_reference_model_or_mapping_surface(self):
        parameters = tuple(inspect.signature(adapters.compile_owner_action_draft).parameters)
        self.assertEqual(
            parameters,
            ("proposal", "current_message", "config", "runtime_evidence"),
        )
        self.assertNotIn("materialize", " ".join(adapters.__all__).lower())
        self.assertNotIn("mapping", " ".join(adapters.__all__).lower())

    def test_draft_and_private_record_are_sealed_and_repr_safe(self):
        message = '请读取文件 path="C:\\trusted\\private\\secret.txt" offset=2 limit=20'
        root = "C:\\trusted"
        path = "C:\\trusted\\private\\secret.txt"
        config = adapters.AdapterConfig(
            artifact_read_exact_enabled=True,
            artifact_root=root,
            path_flavor=adapters.ArtifactPathFlavor.WINDOWS,
        )
        evidence = _runtime_evidence(
            message=message,
            operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
            path_attestation=_path_attestation(
                message=message,
                root=root,
                path=path,
                target_kind=adapters.ArtifactTargetKind.PLAIN_TEXT_FILE,
            ),
        )
        draft = _compile(
            message,
            OwnerActionOperation.ARTIFACT_READ_EXACT,
            config,
            evidence,
        )
        self.assertTrue(draft.is_canonical)
        self.assertNotIn(path, repr(draft))
        self.assertNotIn(path, repr(draft.trace_metadata()))
        self.assertNotIn("parameters", {field.name for field in dataclasses.fields(draft)})
        material = adapters._open_adapter_draft(draft)
        self.assertIs(material.runtime_evidence, evidence)
        self.assertNotIn(path, repr(material))
        self.assertNotIn(path, repr(material.parameter_record))
        with self.assertRaisesRegex(Exception, "issuer_seal_invalid"):
            dataclasses.replace(draft)

    def test_current_message_and_binding_are_the_only_parameter_authority(self):
        message = '引用里说“读取 path="C:\\trusted\\old.txt"”，请别执行'
        config = adapters.AdapterConfig(
            artifact_read_exact_enabled=True,
            artifact_root="C:\\trusted",
            path_flavor=adapters.ArtifactPathFlavor.WINDOWS,
        )
        with self.assertRaisesRegex(adapters.AdapterDraftRejected, "request_not_explicit"):
            _compile(message, OwnerActionOperation.ARTIFACT_READ_EXACT, config, None)

        mismatched = _proposal("请执行当前操作", OwnerActionOperation.ARTIFACT_READ_EXACT)
        with self.assertRaisesRegex(
            adapters.AdapterDraftRejected,
            "current_message_binding_mismatch",
        ):
            adapters.compile_owner_action_draft(
                mismatched,
                "另一个消息",
                config,
                None,
            )


class ArtifactReadAdapterTests(unittest.TestCase):
    def _success(
        self,
        *,
        message: str,
        root: str,
        path: str,
        flavor: adapters.ArtifactPathFlavor,
    ) -> adapters.AdapterDraft:
        config = adapters.AdapterConfig(
            artifact_read_exact_enabled=True,
            artifact_root=root,
            path_flavor=flavor,
        )
        evidence = _runtime_evidence(
            message=message,
            operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
            path_attestation=_path_attestation(
                message=message,
                root=root,
                path=path,
                target_kind=adapters.ArtifactTargetKind.PLAIN_TEXT_FILE,
            ),
        )
        return _compile(
            message,
            OwnerActionOperation.ARTIFACT_READ_EXACT,
            config,
            evidence,
        )

    def test_windows_posix_and_wsl_paths_are_independent_flavors(self):
        cases = (
            (
                adapters.ArtifactPathFlavor.WINDOWS,
                "C:\\trusted",
                "C:\\trusted\\src\\main.py",
            ),
            (adapters.ArtifactPathFlavor.POSIX, "/srv/shio", "/srv/shio/src/main.py"),
            (
                adapters.ArtifactPathFlavor.WSL_POSIX,
                "/home/owner/work",
                "/home/owner/work/src/main.py",
            ),
        )
        for flavor, root, path in cases:
            with self.subTest(flavor=flavor):
                message = f'请读取文件 path="{path}" offset=0 limit=40'
                draft = self._success(
                    message=message,
                    root=root,
                    path=path,
                    flavor=flavor,
                )
                record = adapters._open_adapter_draft(draft).parameter_record
                self.assertEqual(record.offset, 0)
                self.assertEqual(record.limit, 40)

    def test_path_attack_matrix_fails_before_any_file_access(self):
        root = "C:\\trusted"
        attacks = (
            "C:\\trusted\\..\\secret.txt",
            "\\\\server\\share\\secret.txt",
            "\\\\?\\C:\\trusted\\secret.txt",
            "\\\\.\\PhysicalDrive0",
            "C:\\trusted\\secret.txt:ads",
            "C:\\trusted\\NUL.txt",
            "C:\\trusted\\%TEMP%\\secret.txt",
            "C:\\trusted\\~\\secret.txt",
            "C:\\trusted\\mixed/secret.txt",
            "D:\\trusted\\secret.txt",
            "C:\\trusted2\\secret.txt",
            "C:\\trusted\\secret.txt\x00",
        )
        config = adapters.AdapterConfig(
            artifact_read_exact_enabled=True,
            artifact_root=root,
            path_flavor=adapters.ArtifactPathFlavor.WINDOWS,
        )
        for path in attacks:
            with self.subTest(path=repr(path)):
                message = f'请读取文件 path="{path}"'
                with self.assertRaises(adapters.AdapterDraftRejected):
                    _compile(
                        message,
                        OwnerActionOperation.ARTIFACT_READ_EXACT,
                        config,
                        None,
                    )

        wsl_config = adapters.AdapterConfig(
            artifact_read_exact_enabled=True,
            artifact_root="/home/owner/work",
            path_flavor=adapters.ArtifactPathFlavor.WSL_POSIX,
        )
        for path in ("/mnt/c/Users/owner/file.txt", "C:\\Users\\owner\\file.txt"):
            with self.subTest(path=path):
                with self.assertRaises(adapters.AdapterDraftRejected):
                    _compile(
                        f'请读取文件 path="{path}"',
                        OwnerActionOperation.ARTIFACT_READ_EXACT,
                        wsl_config,
                        None,
                    )

    def test_multiple_paths_and_closed_offset_limit_are_rejected(self):
        root = "/srv/shio"
        config = adapters.AdapterConfig(
            artifact_read_exact_enabled=True,
            artifact_root=root,
            path_flavor=adapters.ArtifactPathFlavor.POSIX,
        )
        messages = (
            '请读取 path="/srv/shio/a.txt" path="/srv/shio/b.txt"',
            '请读取 path="/srv/shio/a.txt" offset=-1',
            '请读取 path="/srv/shio/a.txt" offset=1.5',
            '请读取 path="/srv/shio/a.txt" limit=999999',
            '请读取 path="/srv/shio/a.txt" encoding="utf-16"',
        )
        for message in messages:
            with self.subTest(message=message):
                with self.assertRaises(adapters.AdapterDraftRejected):
                    _compile(
                        message,
                        OwnerActionOperation.ARTIFACT_READ_EXACT,
                        config,
                        None,
                    )

    def test_binary_document_image_and_unknown_extensions_are_closed(self):
        config = adapters.AdapterConfig(
            artifact_read_exact_enabled=True,
            artifact_root="/srv/shio",
            path_flavor=adapters.ArtifactPathFlavor.POSIX,
        )
        for extension in ("bin", "pdf", "docx", "epub", "png", "jpg", "exe", ""):
            suffix = f".{extension}" if extension else ""
            message = f'请读取 path="/srv/shio/private/file{suffix}"'
            with self.subTest(extension=extension):
                with self.assertRaisesRegex(
                    adapters.AdapterDraftRejected,
                    "artifact_extension_forbidden",
                ):
                    _compile(
                        message,
                        OwnerActionOperation.ARTIFACT_READ_EXACT,
                        config,
                        None,
                    )

    def test_missing_or_incomplete_toctou_and_link_evidence_fails_closed(self):
        message = '请读取 path="/srv/shio/private/file.txt"'
        root = "/srv/shio"
        path = "/srv/shio/private/file.txt"
        config = adapters.AdapterConfig(
            artifact_read_exact_enabled=True,
            artifact_root=root,
            path_flavor=adapters.ArtifactPathFlavor.POSIX,
        )
        with self.assertRaisesRegex(adapters.AdapterDraftRejected, "runtime_evidence_required"):
            _compile(message, OwnerActionOperation.ARTIFACT_READ_EXACT, config, None)

        for changes in (
            {"components_lstat_verified": False},
            {"symlink_free": False},
            {"reparse_free": False},
            {"hardlink_count": 2},
            {"root_exclusive_trusted_writers": False},
            {"replacement_protected": False},
            {"preflight_token_digest": ""},
        ):
            with self.subTest(changes=changes):
                attestation = _path_attestation(
                    message=message,
                    root=root,
                    path=path,
                    target_kind=adapters.ArtifactTargetKind.PLAIN_TEXT_FILE,
                    **changes,
                )
                evidence = _runtime_evidence(
                    message=message,
                    operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
                    path_attestation=attestation,
                )
                with self.assertRaises(adapters.AdapterDraftRejected):
                    _compile(
                        message,
                        OwnerActionOperation.ARTIFACT_READ_EXACT,
                        config,
                        evidence,
                    )

    def test_runtime_evidence_field_mutation_is_not_a_drift_attestation(self):
        message = '请读取 path="/srv/shio/file.txt"'
        root = "/srv/shio"
        path = "/srv/shio/file.txt"
        config = adapters.AdapterConfig(
            artifact_read_exact_enabled=True,
            artifact_root=root,
            path_flavor=adapters.ArtifactPathFlavor.POSIX,
        )
        changes = (
            ("exact_tool_name", "astrbot_file_read_alias"),
            ("plugin_id", "untrusted.plugin"),
            ("source_qualname", "clone.FileReadTool"),
            ("implementation_version", "astrbot-4.26.7"),
            ("interface_version", "legacy-file-read-v0"),
            ("schema_digest", "f" * 64),
        )
        for field_name, value in changes:
            with self.subTest(field_name=field_name):
                evidence = _runtime_evidence(
                    message=message,
                    operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
                    path_attestation=_path_attestation(
                        message=message,
                        root=root,
                        path=path,
                        target_kind=adapters.ArtifactTargetKind.PLAIN_TEXT_FILE,
                    ),
                )
                object.__setattr__(evidence, field_name, value)
                self.assertFalse(evidence.is_canonical)
                self.assertNotIn(value, repr(evidence))
                with self.assertRaisesRegex(
                    adapters.AdapterDraftRejected,
                    "runtime_evidence_not_canonical",
                ):
                    evidence.trace_metadata()
                with self.assertRaisesRegex(
                    adapters.AdapterDraftRejected,
                    "runtime_evidence_not_canonical",
                ):
                    _compile(
                        message,
                        OwnerActionOperation.ARTIFACT_READ_EXACT,
                        config,
                        evidence,
                    )


class ArtifactGrepAdapterTests(unittest.TestCase):
    def test_literal_is_code_escaped_and_budgets_are_fixed(self):
        message = '请搜索 path="/srv/shio/src" literal="a.*(b)[c]$"'
        root = "/srv/shio"
        path = "/srv/shio/src"
        config = adapters.AdapterConfig(
            artifact_grep_enabled=True,
            artifact_root=root,
            path_flavor=adapters.ArtifactPathFlavor.POSIX,
        )
        evidence = _runtime_evidence(
            message=message,
            operation=OwnerActionOperation.ARTIFACT_GREP,
            path_attestation=_path_attestation(
                message=message,
                root=root,
                path=path,
                target_kind=adapters.ArtifactTargetKind.PLAIN_TEXT_TREE,
                tree_file_count=30,
                tree_total_bytes=100_000,
            ),
            grep_attestation=_grep_attestation(),
        )
        draft = _compile(message, OwnerActionOperation.ARTIFACT_GREP, config, evidence)
        record = adapters._open_adapter_draft(draft).parameter_record
        self.assertNotEqual(record.escaped_pattern, "a.*(b)[c]$")
        self.assertEqual(record.context_before, adapters.ARTIFACT_GREP_CONTEXT_LINES)
        self.assertEqual(record.context_after, adapters.ARTIFACT_GREP_CONTEXT_LINES)
        self.assertEqual(record.result_limit, adapters.ARTIFACT_GREP_RESULT_LIMIT)
        self.assertEqual(record.glob, adapters.ARTIFACT_GREP_FIXED_GLOB)
        self.assertNotIn("a.*(b)[c]$", repr(record))

    def test_regex_surface_and_multiple_slots_are_not_available(self):
        config = adapters.AdapterConfig(
            artifact_grep_enabled=True,
            artifact_root="/srv/shio",
            path_flavor=adapters.ArtifactPathFlavor.POSIX,
        )
        messages = (
            '请搜索 path="/srv/shio" pattern=".*secret.*"',
            '请搜索 path="/srv/shio" literal="a" literal="b"',
            '请搜索 path="/srv/shio" literal="a" glob="*.env"',
            '如何使用 grep path="/srv/shio" literal="secret"',
            'How to grep path="/srv/shio" literal="secret"',
        )
        for message in messages:
            with self.subTest(message=message):
                with self.assertRaises(adapters.AdapterDraftRejected):
                    _compile(message, OwnerActionOperation.ARTIFACT_GREP, config, None)

    def test_corpus_and_source_output_preflight_budgets_are_hard(self):
        message = '请搜索 path="/srv/shio/src" literal="needle"'
        root = "/srv/shio"
        path = "/srv/shio/src"
        config = adapters.AdapterConfig(
            artifact_grep_enabled=True,
            artifact_root=root,
            path_flavor=adapters.ArtifactPathFlavor.POSIX,
        )
        path_base = _path_attestation(
            message=message,
            root=root,
            path=path,
            target_kind=adapters.ArtifactTargetKind.PLAIN_TEXT_TREE,
        )
        cases = (
            (
                dataclasses.replace(
                    path_base,
                    tree_file_count=adapters.ARTIFACT_GREP_MAX_FILES + 1,
                ),
                _grep_attestation(),
            ),
            (
                dataclasses.replace(
                    path_base,
                    tree_total_bytes=adapters.ARTIFACT_GREP_MAX_TREE_BYTES + 1,
                ),
                _grep_attestation(),
            ),
            (
                path_base,
                _grep_attestation(source_output_cap_bytes=0),
            ),
            (
                path_base,
                _grep_attestation(preflight_budget_enforced=False),
            ),
        )
        for path_attestation, grep_attestation in cases:
            with self.subTest(
                files=path_attestation.tree_file_count,
                bytes=path_attestation.tree_total_bytes,
            ):
                evidence = _runtime_evidence(
                    message=message,
                    operation=OwnerActionOperation.ARTIFACT_GREP,
                    path_attestation=path_attestation,
                    grep_attestation=grep_attestation,
                )
                with self.assertRaises(adapters.AdapterDraftRejected):
                    _compile(
                        message,
                        OwnerActionOperation.ARTIFACT_GREP,
                        config,
                        evidence,
                    )


class MemoryWriteAdapterTests(unittest.TestCase):
    def test_only_current_remember_literal_is_compiled_with_fixed_fields(self):
        message = "请记住：我偏好简短的中文回答。"
        config = adapters.AdapterConfig(memory_write_literal_enabled=True)
        evidence = _runtime_evidence(
            message=message,
            operation=OwnerActionOperation.MEMORY_WRITE_LITERAL,
            scope_attestation=_scope_attestation(),
        )
        draft = _compile(
            message,
            OwnerActionOperation.MEMORY_WRITE_LITERAL,
            config,
            evidence,
        )
        record = adapters._open_adapter_draft(draft).parameter_record
        self.assertEqual(record.topics, ())
        self.assertEqual(record.key_facts, ())
        self.assertEqual(record.sentiment, "neutral")
        self.assertEqual(record.importance, adapters.MEMORY_FIXED_IMPORTANCE)
        self.assertNotIn("简短", repr(record))
        self.assertEqual(
            draft.confirmation_policy,
            ActionConfirmationPolicy.CURRENT_EXPLICIT_REQUEST,
        )
        self.assertEqual(draft.side_effect, ActionSideEffect.STATE_WRITE)

    def test_body_cannot_supply_scope_subject_or_another_person(self):
        config = adapters.AdapterConfig(memory_write_literal_enabled=True)
        messages = (
            "请记住：scope=global；大家都喜欢这个。",
            "请记住：scope: group；这是群共享事实。",
            "请替群友甲记住：他住在某地。",
            "请给 owner-b 记住：他喜欢咖啡。",
            "请记住：替群友甲保存他住在某地。",
            "引用上一条里的记住：秘密。",
            "不要记住：这句话。",
        )
        for message in messages:
            with self.subTest(message=message):
                with self.assertRaises(adapters.AdapterDraftRejected):
                    _compile(
                        message,
                        OwnerActionOperation.MEMORY_WRITE_LITERAL,
                        config,
                        None,
                    )

    def test_controller_scope_token_and_private_owner_evidence_are_mandatory(self):
        message = "记住：只在我的私人记忆中保留这个偏好。"
        config = adapters.AdapterConfig(memory_write_literal_enabled=True)
        cases = (
            None,
            _scope_attestation(resolved="different-token"),
            _scope_attestation(private_chat=False),
            _scope_attestation(current_owner_subject=False),
            _scope_attestation(mode=adapters.MemoryScopeMode.GLOBAL),
            _scope_attestation(permission_wrapper_preserved=False),
        )
        for scope in cases:
            with self.subTest(scope=scope):
                evidence = (
                    None
                    if scope is None
                    else _runtime_evidence(
                        message=message,
                        operation=OwnerActionOperation.MEMORY_WRITE_LITERAL,
                        scope_attestation=scope,
                    )
                )
                with self.assertRaises(adapters.AdapterDraftRejected):
                    _compile(
                        message,
                        OwnerActionOperation.MEMORY_WRITE_LITERAL,
                        config,
                        evidence,
                    )


class SandboxShellAdapterTests(unittest.TestCase):
    def test_detach_multiblock_and_cross_shell_are_rejected(self):
        config = adapters.AdapterConfig(
            sandbox_shell_once_enabled=True,
            shell_family=adapters.ShellFamily.POSIX_SH,
        )
        messages = (
            "请执行\n```sh\nnohup echo hi\n```",
            "请执行\n```sh\necho hi &\n```",
            "请执行\n```sh\nsetsid echo hi\n```",
            "请执行\n```powershell\nStart-Process calc.exe\n```",
            "请执行\n```sh\necho one\n```\n```sh\necho two\n```",
            "请执行\n```powershell\nWrite-Output hi\n```",
        )
        for message in messages:
            with self.subTest(message=message):
                with self.assertRaises(adapters.AdapterDraftRejected):
                    _compile(
                        message,
                        OwnerActionOperation.SANDBOX_SHELL_ONCE,
                        config,
                        None,
                    )

    def test_valid_unique_literal_remains_code_level_hard_disabled(self):
        message = "请执行下面唯一命令\n```sh\nprintf 'hello'\n```"
        config = adapters.AdapterConfig(
            sandbox_shell_once_enabled=True,
            shell_family=adapters.ShellFamily.POSIX_SH,
        )
        with self.assertRaisesRegex(
            adapters.AdapterDraftRejected,
            "adapter_hard_disabled",
        ):
            _compile(
                message,
                OwnerActionOperation.SANDBOX_SHELL_ONCE,
                config,
                None,
            )

    def test_runtime_collector_never_issues_a_shell_candidate(self):
        tool = runtime_fixture._tool(
            runtime_fixture.FileReadTool,
            "astrbot_execute_shell",
            runtime_fixture.FILE_READ_SCHEMA,
        )
        with self.assertRaisesRegex(
            runtime_collector.RuntimeCollectorRejected,
            "shell_runtime_hard_disabled",
        ):
            runtime_collector._build_test_runtime_collector(
                runtime_fixture._BuiltinManager(tool),
                operation=OwnerActionOperation.SANDBOX_SHELL_ONCE,
                implementation_version="astrbot-4.27.2",
            )


if __name__ == "__main__":
    unittest.main()
