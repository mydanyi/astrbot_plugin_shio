from __future__ import annotations

import copy
import concurrent.futures
import dataclasses
import hashlib
import json
import os
import tempfile
import types
import unittest

from astrbot_plugin_shio.core import owner_action_adapters as adapters
from astrbot_plugin_shio.core import owner_action_runtime_collector as collector_mod
from astrbot_plugin_shio.core.contracts import DecisionBinding, OwnerActionOperation


def _binding(message: str = "请读取当前文件") -> DecisionBinding:
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


FILE_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path of the file to read. If relative, will be in workspace root.",
        },
        "offset": {
            "type": "integer",
            "description": "Optional line offset to start reading from. 0-based index.",
            "minimum": 0,
        },
        "limit": {
            "type": "integer",
            "description": "Optional maximum number of lines to read.",
            "minimum": 1,
        },
    },
    "required": ["path"],
}

GREP_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "The expression pattern to search for in file contents.",
        },
        "path": {
            "type": "string",
            "description": "File or directory to search in (rg PATH). If relative, will be in workspace root.",
        },
        "glob": {
            "type": "string",
            "description": "Optional glob filter such as `*.py`, `*.{ts,tsx}`.",
        },
        "-A": {
            "type": "integer",
            "description": "Number of trailing context lines to include after each match.",
            "minimum": 0,
        },
        "-B": {
            "type": "integer",
            "description": "Number of leading context lines to include before each match.",
            "minimum": 0,
        },
        "-C": {
            "type": "integer",
            "description": "Number of leading and trailing context lines to include around each match.",
            "minimum": 0,
        },
        "result_limit": {
            "type": "integer",
            "description": "Maximum number of result groups returned by the tool. Defaults to 100.",
            "minimum": 1,
        },
    },
    "required": ["pattern"],
}

MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "memory": {
            "type": "string",
            "description": "Concise factual long-term memory to save. Do not copy the full conversation.",
        },
        "topics": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional short topic tags for this memory, up to 5.",
            "default": [],
        },
        "key_facts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional key facts supporting the memory, up to 5.",
            "default": [],
        },
        "sentiment": {
            "type": "string",
            "description": "Sentiment of the memory: positive, neutral, or negative.",
            "default": "neutral",
        },
        "importance": {
            "type": "number",
            "description": "Importance from 0.0 to 1.0. Use higher values for durable preferences, commitments, or identity facts.",
            "default": 0.7,
        },
        "reason": {
            "type": "string",
            "description": "Optional short reason why this information should be remembered.",
            "default": "",
        },
    },
    "required": ["memory"],
}


def _install_fake_type(module_name: str, class_name: str, attributes: dict):
    return type(class_name, (), {"__module__": module_name, **attributes})


FileReadTool = _install_fake_type(
    "astrbot.core.tools.computer_tools.fs",
    "FileReadTool",
    {},
)
GrepTool = _install_fake_type(
    "astrbot.core.tools.computer_tools.fs",
    "GrepTool",
    {},
)
MemoryMemorizeTool = _install_fake_type(
    "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool",
    "MemoryMemorizeTool",
    {},
)
PermissionGuardedTool = _install_fake_type(
    "astrbot.core.provider.func_tool_manager",
    "_PermissionGuardedTool",
    {},
)


def _tool(tool_type, name: str, schema: dict):
    value = tool_type()
    value.name = name
    value.parameters = copy.deepcopy(schema)
    value.active = True
    value.handler = None
    value.handler_module_path = type(value).__module__
    value.is_background_task = False
    return value


class _ToolSet:
    def __init__(self, tools):
        self.tools = list(tools)

    def get_tool(self, name: str):
        return next((tool for tool in self.tools if tool.name == name), None)


class _AdminEvent:
    def __init__(self, admin: bool) -> None:
        self._admin = admin

    def is_admin(self) -> bool:
        return self._admin

    def get_sender_id(self) -> str:
        return "redacted"


class _Context:
    def __init__(self, admin: bool) -> None:
        self.context = types.SimpleNamespace(event=_AdminEvent(admin))


class _BuiltinManager:
    def __init__(self, tool, *, conflicts=()) -> None:
        self.tool = tool
        self.func_list = list(conflicts)
        self.calls = 0

    def get_builtin_tool(self, name: str):
        self.calls += 1
        if name != self.tool.name:
            raise KeyError(name)
        return self.tool


class _MemoryManager:
    def __init__(
        self,
        raw,
        wrapper,
        *,
        permission="admin",
        scope="private_user",
    ) -> None:
        self.func_list = [raw]
        self.raw = raw
        self.wrapper = wrapper
        self.permission = permission
        self.scope = scope

    def get_full_tool_set(self):
        return _ToolSet([self.wrapper])

    def _check_tool_permission(self, name: str, context: _Context):
        if name != self.raw.name:
            return "error"
        if self.permission != "admin":
            return None
        return None if context.context.event.is_admin() else "error: permission denied"


def _test_collector(manager, operation, *, scope_probe=None):
    return collector_mod._build_test_runtime_collector(
        manager,
        operation=operation,
        implementation_version=(
            adapters.OWNER_ACTION_ADAPTER_REGISTRY[operation].implementation_version
        ),
        memory_scope_probe=scope_probe,
    )


class ProductionFailClosedTests(unittest.TestCase):
    def test_production_allowlist_is_empty_and_manager_is_not_trusted(self):
        tool = _tool(FileReadTool, "astrbot_file_read_tool", FILE_READ_SCHEMA)
        manager = _BuiltinManager(tool)
        runtime = collector_mod.RuntimeConformanceCollector(manager)
        with self.assertRaisesRegex(
            collector_mod.RuntimeCollectorRejected,
            "runtime_operation_not_audited",
        ):
            runtime.collect(_binding(), OwnerActionOperation.ARTIFACT_READ_EXACT)
        self.assertEqual(manager.calls, 0)
        self.assertEqual(collector_mod.PRODUCTION_AUDITED_OPERATION_COUNT, 0)
        self.assertFalse(hasattr(collector_mod, "_PRODUCTION_AUDITED_SPECS"))
        with self.assertRaises(TypeError):
            collector_mod.PRODUCTION_AUDITED_OPERATIONS[
                OwnerActionOperation.ARTIFACT_READ_EXACT
            ] = object()

        # Caller-authored underscore attributes are not the module registry used by
        # collect(), so they cannot turn the empty production manifest green.
        runtime._specs = {OwnerActionOperation.ARTIFACT_READ_EXACT: object()}
        runtime._implementation_version = lambda _operation: "forged"
        runtime._source_file = lambda _tool: __file__
        with self.assertRaisesRegex(
            collector_mod.RuntimeCollectorRejected,
            "runtime_operation_not_audited",
        ):
            runtime.collect(_binding(), OwnerActionOperation.ARTIFACT_READ_EXACT)
        self.assertEqual(manager.calls, 0)

    def test_shell_never_has_a_candidate_even_in_test_manifest(self):
        manager = _BuiltinManager(
            _tool(FileReadTool, "astrbot_execute_shell", FILE_READ_SCHEMA)
        )
        with self.assertRaisesRegex(
            collector_mod.RuntimeCollectorRejected,
            "shell_runtime_hard_disabled",
        ):
            _test_collector(manager, OwnerActionOperation.SANDBOX_SHELL_ONCE)

    def test_public_mapping_or_boolean_attestation_surface_does_not_exist(self):
        signature = collector_mod.inspect_public_collector_signature()
        self.assertEqual(signature, ("binding", "operation"))
        for forbidden in ("mapping", "active", "verified", "allowlist", "evidence"):
            self.assertNotIn(forbidden, " ".join(signature).lower())
        self.assertFalse(hasattr(collector_mod, "_issue_test_runtime_evidence"))
        self.assertEqual(
            tuple(
                __import__("inspect").signature(
                    adapters._issue_runtime_conformance_evidence
                ).parameters
            ),
            ("collector", "candidate", "proof"),
        )

        proof = collector_mod._issue_test_operation_proof()
        object.__setattr__(proof, "artifact_path", "secret-proof-text")
        self.assertEqual(
            repr(proof),
            "RuntimeOperationProof(canonical=False, details_hidden=True)",
        )
        with self.assertRaisesRegex(
            collector_mod.RuntimeCollectorRejected,
            "operation_proof_not_canonical",
        ):
            proof.trace_metadata()

        runtime = collector_mod.RuntimeConformanceCollector(
            _BuiltinManager(
                _tool(FileReadTool, "astrbot_file_read_tool", FILE_READ_SCHEMA)
            )
        )
        with self.assertRaisesRegex(
            collector_mod.RuntimeCollectorRejected,
            "runtime_operation_invalid",
        ):
            runtime.collect(_binding(), "artifact_read_exact")  # type: ignore[arg-type]

        class _DerivedBinding(DecisionBinding):
            pass

        derived = _DerivedBinding(**dataclasses.asdict(_binding()))
        with self.assertRaisesRegex(
            collector_mod.RuntimeCollectorRejected,
            "runtime_binding_invalid",
        ):
            runtime.collect(derived, OwnerActionOperation.ARTIFACT_READ_EXACT)


class BuiltinCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tool = _tool(FileReadTool, "astrbot_file_read_tool", FILE_READ_SCHEMA)
        self.manager = _BuiltinManager(self.tool)
        self.runtime = _test_collector(
            self.manager,
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        )

    def test_exact_builtin_object_is_sealed_and_epoch_bound(self):
        binding = _binding()
        candidate = self.runtime.collect(
            binding,
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        self.assertTrue(candidate.is_canonical)
        self.assertEqual(candidate.binding, binding)
        self.assertNotIn("tool", {field.name for field in dataclasses.fields(candidate)})
        self.assertNotIn(hex(id(self.tool)), repr(candidate))
        self.assertEqual(self.manager.calls, 2)

        with self.assertRaisesRegex(Exception, "candidate_issuer_seal_invalid"):
            dataclasses.replace(candidate)
        copied = copy.copy(candidate)
        self.assertIsNot(copied, candidate)
        self.assertFalse(copied.is_canonical)
        object.__setattr__(candidate, "collector_epoch", candidate.collector_epoch + 1)
        self.assertFalse(candidate.is_canonical)
        self.assertEqual(
            repr(candidate),
            "RuntimeToolCandidate(canonical=False, details_hidden=True)",
        )
        with self.assertRaisesRegex(
            collector_mod.RuntimeCollectorRejected,
            "runtime_candidate_not_canonical",
        ):
            candidate.trace_metadata()

    def test_same_name_plugin_or_mcp_conflict_fails_closed(self):
        conflict = _tool(FileReadTool, "astrbot_file_read_tool", FILE_READ_SCHEMA)
        runtime = _test_collector(
            _BuiltinManager(self.tool, conflicts=(conflict,)),
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        with self.assertRaisesRegex(
            collector_mod.RuntimeCollectorRejected,
            "runtime_tool_name_conflict",
        ):
            runtime.collect(_binding(), OwnerActionOperation.ARTIFACT_READ_EXACT)

    def test_schema_active_and_exact_object_drift_fail_closed(self):
        cases = ("schema", "active", "identity")
        for case in cases:
            tool = _tool(FileReadTool, "astrbot_file_read_tool", FILE_READ_SCHEMA)
            manager = _BuiltinManager(tool)
            runtime = _test_collector(
                manager,
                OwnerActionOperation.ARTIFACT_READ_EXACT,
            )
            if case == "schema":
                tool.parameters["required"] = []
            elif case == "active":
                tool.active = False
            else:
                replacement = _tool(
                    FileReadTool,
                    "astrbot_file_read_tool",
                    FILE_READ_SCHEMA,
                )
                original = manager.get_builtin_tool
                manager.get_builtin_tool = lambda name: (
                    original(name) if manager.calls % 2 == 0 else replacement
                )
            with self.subTest(case=case):
                with self.assertRaises(collector_mod.RuntimeCollectorRejected):
                    runtime.collect(
                        _binding(),
                        OwnerActionOperation.ARTIFACT_READ_EXACT,
                    )

    def test_version_qualname_source_file_and_fingerprint_are_all_exact(self):
        mutations = ("version", "qualname", "source_file")
        for mutation in mutations:
            tool = _tool(FileReadTool, "astrbot_file_read_tool", FILE_READ_SCHEMA)
            manager = _BuiltinManager(tool)
            runtime = _test_collector(
                manager,
                OwnerActionOperation.ARTIFACT_READ_EXACT,
            )
            profile = runtime._profile()
            cleanup = lambda: None
            if mutation == "version":
                replacement = dataclasses.replace(
                    profile,
                    implementation_version=lambda _operation: "astrbot-drift",
                )
            elif mutation == "qualname":
                original_module = FileReadTool.__module__
                original_handler_module = tool.handler_module_path
                FileReadTool.__module__ = "untrusted.clone"
                tool.handler_module_path = "untrusted.clone"
                cleanup = lambda: (
                    setattr(FileReadTool, "__module__", original_module),
                    setattr(tool, "handler_module_path", original_handler_module),
                )
                replacement = profile
            else:
                temp = tempfile.NamedTemporaryFile(delete=False)
                temp.write(b"different reviewed source")
                temp.close()
                cleanup = lambda: os.unlink(temp.name)
                replacement = dataclasses.replace(
                    profile,
                    source_file=lambda _tool, path=temp.name: path,
                )
            if replacement is not profile:
                with collector_mod._COLLECTOR_PROFILE_LOCK:
                    collector_mod._COLLECTOR_PROFILES[runtime] = replacement
            try:
                with self.subTest(mutation=mutation):
                    with self.assertRaisesRegex(
                        collector_mod.RuntimeCollectorRejected,
                        "runtime_source_contract_drift",
                    ):
                        runtime.collect(
                            _binding(),
                            OwnerActionOperation.ARTIFACT_READ_EXACT,
                        )
            finally:
                cleanup()

    def test_candidate_is_collector_local_operation_bound_and_one_shot(self):
        candidate = self.runtime.collect(
            _binding(),
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        other = _test_collector(
            _BuiltinManager(self.tool),
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        proof = collector_mod._issue_test_operation_proof()
        with self.assertRaisesRegex(
            collector_mod.RuntimeCollectorRejected,
            "candidate_collector_mismatch",
        ):
            other.issue_evidence(candidate, proof)

        evidence = self.runtime.issue_evidence(candidate, proof)
        self.assertTrue(evidence.is_canonical)
        with self.assertRaisesRegex(
            collector_mod.RuntimeCollectorRejected,
            "runtime_candidate_replayed",
        ):
            self.runtime.issue_evidence(
                candidate,
                collector_mod._issue_test_operation_proof(),
            )

    def test_evidence_retains_exact_object_across_manager_replacement(self):
        binding = _binding()
        candidate = self.runtime.collect(
            binding,
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        evidence = self.runtime.issue_evidence(
            candidate,
            collector_mod._issue_test_operation_proof(),
        )
        retained = self.tool
        self.manager.tool = _tool(
            FileReadTool,
            "astrbot_file_read_tool",
            FILE_READ_SCHEMA,
        )
        other = _test_collector(
            _BuiltinManager(self.manager.tool),
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        with self.assertRaisesRegex(
            collector_mod.RuntimeCollectorRejected,
            "runtime_evidence_collector_mismatch",
        ):
            collector_mod._open_runtime_evidence(
                evidence,
                collector=other,
                binding=binding,
                operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
            )
        with self.assertRaisesRegex(
            collector_mod.RuntimeCollectorRejected,
            "runtime_open_binding_invalid",
        ):
            collector_mod._open_runtime_evidence(
                evidence,
                collector=self.runtime,
                binding=binding,
                operation="artifact_read_exact",  # type: ignore[arg-type]
            )
        material = collector_mod._open_runtime_evidence(
            evidence,
            collector=self.runtime,
            binding=binding,
            operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        self.assertIs(material.tool, retained)
        self.assertIsNot(material.tool, self.manager.tool)
        forged = copy.copy(evidence)
        with self.assertRaisesRegex(
            collector_mod.RuntimeCollectorRejected,
            "runtime_evidence_not_canonical",
        ):
            collector_mod._open_runtime_evidence(
                forged,
                collector=self.runtime,
                binding=binding,
                operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
            )
        with self.assertRaisesRegex(
            collector_mod.RuntimeCollectorRejected,
            "runtime_evidence_open_replayed",
        ):
            collector_mod._open_runtime_evidence(
                evidence,
                collector=self.runtime,
                binding=binding,
                operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
            )

    def test_retained_exact_object_mutation_fails_open(self):
        binding = _binding()
        candidate = self.runtime.collect(
            binding,
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        evidence = self.runtime.issue_evidence(
            candidate,
            collector_mod._issue_test_operation_proof(),
        )
        self.tool.active = False
        with self.assertRaisesRegex(
            collector_mod.RuntimeCollectorRejected,
            "runtime_exact_object_drift",
        ):
            collector_mod._open_runtime_evidence(
                evidence,
                collector=self.runtime,
                binding=binding,
                operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
            )

    def test_shared_proof_race_does_not_half_consume_losing_candidate(self):
        first = self.runtime.collect(
            _binding(),
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        second = self.runtime.collect(
            _binding(),
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        proof = collector_mod._issue_test_operation_proof()

        def attempt(candidate):
            try:
                return (candidate, self.runtime.issue_evidence(candidate, proof), None)
            except Exception as exc:  # noqa: BLE001 - exact result asserted below.
                return (candidate, None, exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(attempt, (first, second)))
        winners = tuple(item for item in results if item[1] is not None)
        losers = tuple(item for item in results if item[2] is not None)
        self.assertEqual((len(winners), len(losers)), (1, 1))
        self.assertIsInstance(losers[0][2], collector_mod.RuntimeCollectorRejected)

        # The losing candidate was not marked before the shared proof replay was
        # discovered, so it remains usable with a fresh sealed proof.
        recovered = self.runtime.issue_evidence(
            losers[0][0],
            collector_mod._issue_test_operation_proof(),
        )
        self.assertTrue(recovered.is_canonical)


class LivingMemoryCandidateTests(unittest.TestCase):
    def _case(self, *, permission="admin", scope="private_user", wrapped=True):
        raw = _tool(
            MemoryMemorizeTool,
            "memorize_long_term_memory",
            MEMORY_SCHEMA,
        )
        wrapper = _tool(
            PermissionGuardedTool if wrapped else MemoryMemorizeTool,
            "memorize_long_term_memory",
            MEMORY_SCHEMA,
        )
        wrapper._wrapped = raw
        wrapper.handler_module_path = type(raw).__module__
        manager = _MemoryManager(
            raw,
            wrapper,
            permission=permission,
            scope=scope,
        )
        runtime = _test_collector(
            manager,
            OwnerActionOperation.MEMORY_WRITE_LITERAL,
            scope_probe=lambda: manager.scope,
        )
        return runtime

    def test_exact_full_tool_set_permission_wrapper_admin_private_scope(self):
        runtime = self._case()
        candidate = runtime.collect(
            _binding("请记住这个偏好"),
            OwnerActionOperation.MEMORY_WRITE_LITERAL,
        )
        self.assertTrue(candidate.is_canonical)
        self.assertTrue(candidate.permission_wrapper_preserved)

    def test_unwrapped_member_or_legacy_scope_each_fail_closed(self):
        for kwargs in (
            {"wrapped": False},
            {"permission": "member"},
            {"scope": "legacy"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(collector_mod.RuntimeCollectorRejected):
                    self._case(**kwargs).collect(
                        _binding("请记住这个偏好"),
                        OwnerActionOperation.MEMORY_WRITE_LITERAL,
                    )


class EvidenceAuthorityTests(unittest.TestCase):
    def test_public_evidence_and_replace_cannot_sign_an_adapter_draft(self):
        spec = adapters.OWNER_ACTION_ADAPTER_REGISTRY[
            OwnerActionOperation.ARTIFACT_READ_EXACT
        ]
        values = dict(
            binding=_binding(),
            operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
            adapter_id=spec.adapter_id,
            adapter_version=spec.adapter_version,
            exact_tool_name=spec.exact_tool_name,
            plugin_id=spec.plugin_id,
            implementation_version=spec.implementation_version,
            source_qualname=spec.source_qualname,
            interface_version=spec.interface_version,
            schema_digest=spec.schema_digest,
            source_file_digest="b" * 64,
            source_fingerprint="c" * 64,
            collector_epoch=1,
            candidate_digest="d" * 64,
            active=True,
            canonical_object_unique=True,
            local_function_tool=True,
            handoff_or_mcp=False,
            background_disabled=True,
            direct_send_blocked=True,
            narrow_context_verified=True,
            return_contract_verified=True,
            permission_wrapper_preserved=False,
        )
        with self.assertRaisesRegex(Exception, "runtime_issuer_seal_invalid"):
            adapters.RuntimeConformanceEvidence(**values)

        tool = _tool(FileReadTool, "astrbot_file_read_tool", FILE_READ_SCHEMA)
        runtime = _test_collector(
            _BuiltinManager(tool),
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        candidate = runtime.collect(
            _binding(),
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        evidence = runtime.issue_evidence(
            candidate,
            collector_mod._issue_test_operation_proof(),
        )
        self.assertTrue(evidence.is_canonical)
        with self.assertRaisesRegex(Exception, "runtime_issuer_seal_invalid"):
            dataclasses.replace(evidence)
        object.__setattr__(evidence, "active", False)
        self.assertFalse(evidence.is_canonical)
        self.assertEqual(
            repr(evidence),
            "RuntimeConformanceEvidence(canonical=False, details_hidden=True)",
        )
        with self.assertRaisesRegex(
            adapters.AdapterDraftRejected,
            "runtime_evidence_not_canonical",
        ):
            evidence.trace_metadata()

    def test_canonical_evidence_is_one_compile_only(self):
        message = '请读取文件 path="/srv/shio/file.txt"'
        binding = _binding(message)
        spec = adapters.OWNER_ACTION_ADAPTER_REGISTRY[
            OwnerActionOperation.ARTIFACT_READ_EXACT
        ]
        path = adapters.ArtifactPathConformance(
            root_digest=hashlib.sha256(b"/srv/shio").hexdigest(),
            path_digest=hashlib.sha256(b"/srv/shio/file.txt").hexdigest(),
            preflight_token_digest="b" * 64,
            binding_revision=binding.conversation_revision,
            target_kind=adapters.ArtifactTargetKind.PLAIN_TEXT_FILE,
            target_size_bytes=100,
            tree_file_count=1,
            tree_total_bytes=100,
            exists=True,
            realpath_within_root=True,
            components_lstat_verified=True,
            symlink_free=True,
            reparse_free=True,
            mount_escape_free=True,
            hardlink_count=1,
            root_exclusive_trusted_writers=True,
            replacement_protected=True,
            plain_text_magic=True,
            allowed_extensions_only=True,
        )
        tool = _tool(FileReadTool, "astrbot_file_read_tool", FILE_READ_SCHEMA)
        runtime = _test_collector(
            _BuiltinManager(tool),
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        candidate = runtime.collect(
            binding,
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        )
        evidence = runtime.issue_evidence(
            candidate,
            collector_mod._issue_test_operation_proof(artifact_path=path),
        )
        from astrbot_plugin_shio.core.capability_policy import CapabilityClass
        from astrbot_plugin_shio.core.contracts import OwnerActionProposal

        proposal = OwnerActionProposal(
            binding=binding,
            capability=CapabilityClass.ARTIFACT_READ,
            operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
            confidence=0.99,
            reason_codes=("current_explicit_action",),
        )
        config = adapters.AdapterConfig(
            artifact_read_exact_enabled=True,
            artifact_root="/srv/shio",
            path_flavor=adapters.ArtifactPathFlavor.POSIX,
        )
        draft = adapters.compile_owner_action_draft(
            proposal,
            message,
            config,
            evidence,
        )
        self.assertTrue(draft.is_canonical)
        with self.assertRaisesRegex(
            adapters.AdapterDraftRejected,
            "runtime_evidence_replayed",
        ):
            adapters.compile_owner_action_draft(proposal, message, config, evidence)


class PrivacyAndSourceBindingTests(unittest.TestCase):
    def test_candidate_trace_contains_only_closed_metadata_and_digests(self):
        tool = _tool(FileReadTool, "astrbot_file_read_tool", FILE_READ_SCHEMA)
        candidate = _test_collector(
            _BuiltinManager(tool),
            OwnerActionOperation.ARTIFACT_READ_EXACT,
        ).collect(_binding(), OwnerActionOperation.ARTIFACT_READ_EXACT)
        trace = candidate.trace_metadata()
        rendered = json.dumps(trace, ensure_ascii=False)
        self.assertNotIn(__file__, rendered)
        self.assertNotIn("session-owner-a", rendered)
        self.assertNotIn("owner-a", rendered)
        self.assertTrue(trace["has_source_file_digest"])
        self.assertTrue(trace["has_source_fingerprint"])


if __name__ == "__main__":
    unittest.main()
