from __future__ import annotations

import dataclasses
import inspect
import json
import math
import unittest
from dataclasses import replace

from astrbot_plugin_shio.core.action_planner import PlannedAction, StructuralOutcome
from astrbot_plugin_shio.core.capability_policy import (
    CapabilityClass,
    SideEffectClass,
    ToolClassification,
    ToolDescriptor,
    build_guest_capability_policy,
    build_owner_capability_policy,
    classify_descriptor,
)
from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.contracts import (
    ActionDecision,
    ActionKind,
    ContractViolation,
    DecisionBinding,
    KnowledgeGapDecision,
    KnowledgeNeed,
)
from astrbot_plugin_shio.core.identity import PrincipalContext
from astrbot_plugin_shio.core.tool_broker import (
    DEFAULT_ACQUISITION_TIMEOUT_S,
    AcquisitionKind,
    AcquisitionRequest,
    ToolSelection,
    broker_tool_request,
    build_batch_request_shape,
    build_capability_request_shape,
    build_extract_request_shape,
    build_knowledge_base_request_shape,
    build_search_request_shape,
)


ANYSEARCH_SOURCE = "data.plugins.astrbot_plugin_anysearch.main"


def _binding(*, revision: int = 1, sender: str = "peer-a") -> DecisionBinding:
    scope = "platform:test|bot:shio|group:tool-broker"
    return DecisionBinding(
        scope_key=scope,
        session_id="session-sensitive-tool",
        current_message_id="message-sensitive-tool",
        current_sender_key=f"{scope}|user:{sender}",
        current_content_digest="a" * 64,
        conversation_revision=revision,
        generation_epoch=7,
        trace_id="b" * 32,
    )


def _target(binding: DecisionBinding) -> ReplyTarget:
    return ReplyTarget(
        message_id=binding.current_message_id,
        sender_key=binding.current_sender_key,
        session_id=binding.session_id,
        scope_key=binding.scope_key,
        content_digest=binding.current_content_digest,
        source_kind="current_inbound",
        referenced_message_id="",
        degradation_reasons=(),
    )


def _principal(binding: DecisionBinding, *, owner: bool = False) -> PrincipalContext:
    sender_id = binding.current_sender_key.rsplit("|user:", 1)[-1]
    return PrincipalContext(
        sender_key=binding.current_sender_key,
        sender_id=sender_id,
        is_owner=owner,
        relationship_role="owner" if owner else "group_peer",
        verification_source=(
            "astrbot_event_sender_id:configured_owner_id"
            if owner
            else "astrbot_event_sender_id:owner_allowlist_miss"
        ),
    )


def _guest_policy(
    binding: DecisionBinding,
    *configured: str,
    conversation_mode: str = "direct_reply",
):
    return build_guest_capability_policy(
        _principal(binding),
        configured_tool_names=configured,
        conversation_mode=conversation_mode,
    )


def _owner_policy(binding: DecisionBinding):
    return build_owner_capability_policy(_principal(binding, owner=True))


def _gap(
    binding: DecisionBinding,
    *,
    need: KnowledgeNeed = KnowledgeNeed.UNKNOWN_TERM,
    capability: CapabilityClass | None = CapabilityClass.PUBLIC_WEB_READ,
    budget: int = 1,
) -> KnowledgeGapDecision:
    if need is KnowledgeNeed.NONE:
        return KnowledgeGapDecision(
            binding=binding,
            need=need,
            requires_evidence=False,
        )
    return KnowledgeGapDecision(
        binding=binding,
        need=need,
        requires_evidence=True,
        requested_capability=capability,
        max_tool_calls=budget,
    )


def _planned_tool(
    binding: DecisionBinding,
    capability: CapabilityClass = CapabilityClass.PUBLIC_WEB_READ,
) -> PlannedAction:
    return PlannedAction(
        action=ActionDecision(
            binding=binding,
            kind=ActionKind.USE_TOOL,
            reply_target=_target(binding),
            capability_intent=capability.value,
            reason_codes=("fixture_explicit_tool_action",),
        ),
        structural_outcome=StructuralOutcome.CONTINUE,
        planner_reason_codes=("fixture_explicit_tool_action",),
    )


def _planned_reply(binding: DecisionBinding) -> PlannedAction:
    return PlannedAction(
        action=ActionDecision(
            binding=binding,
            kind=ActionKind.REPLY,
            reply_target=_target(binding),
            reason_codes=("fixture_reply",),
        ),
        structural_outcome=StructuralOutcome.CONTINUE,
        planner_reason_codes=("fixture_reply",),
    )


def _descriptor(
    name: str,
    *,
    parameter_names: tuple[str, ...],
    module_path: str = ANYSEARCH_SOURCE,
    description: str = "Public web search",
    active: bool = True,
) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        description=description,
        origin=module_path,
        module_path=module_path,
        parameter_names=parameter_names,
        active=active,
        declared_capability="",
        declared_side_effect="",
    )


def _anysearch_tools() -> tuple[ToolClassification, ...]:
    return (
        classify_descriptor(
            _descriptor("anysearch_search", parameter_names=("query",))
        ),
        classify_descriptor(
            _descriptor("anysearch_extract", parameter_names=("url",))
        ),
        classify_descriptor(
            _descriptor("anysearch_batch_search", parameter_names=("queries",))
        ),
    )


def _shell_tool(
    *,
    name: str = "astrbot_execute_shell",
    active: bool = True,
) -> ToolClassification:
    return classify_descriptor(
        _descriptor(
            name,
            parameter_names=("command",),
            module_path="astrbot.core.agent.tools.shell",
            description="Execute a shell command",
            active=active,
        )
    )


class ToolBrokerTests(unittest.TestCase):
    def test_group_join_can_select_only_one_sealed_public_read(self):
        binding = _binding()
        policy = _guest_policy(
            binding,
            "astr_kb_search",
            conversation_mode="group_join",
        )
        module = "astrbot.core.tools.knowledge_base_tools"
        tool = classify_descriptor(
            _descriptor(
                "astr_kb_search",
                parameter_names=("query",),
                module_path=module,
                description="Query the knowledge base for facts",
            )
        )

        request = broker_tool_request(
            planned_action=_planned_tool(binding, CapabilityClass.CHAT_RETRIEVAL),
            knowledge_gap=_gap(
                binding,
                capability=CapabilityClass.CHAT_RETRIEVAL,
            ),
            capability_policy=policy,
            runtime_tools=(tool, _shell_tool()),
            configured_tool_names=("astr_kb_search",),
            request_shape=build_knowledge_base_request_shape(
                binding,
                query="他们刚才说的华强买瓜是什么梗？",
            ),
            now=100.0,
        )

        self.assertEqual(request.selection.tool_name, "astr_kb_search")
        self.assertEqual(request.selection.call_budget, 1)
        self.assertFalse(policy.memory_write)
        self.assertFalse(policy.shell_exec)
        self.assertFalse(policy.agent_full)

    def test_guest_can_select_only_exact_astrbot_native_knowledge_base_tool(self):
        binding = _binding()
        module = "astrbot.core.tools.knowledge_base_tools"
        tool = classify_descriptor(
            _descriptor(
                "astr_kb_search",
                parameter_names=("query",),
                module_path=module,
                description="Query the knowledge base for facts",
            )
        )
        request = broker_tool_request(
            planned_action=_planned_tool(binding, CapabilityClass.CHAT_RETRIEVAL),
            knowledge_gap=_gap(
                binding,
                capability=CapabilityClass.CHAT_RETRIEVAL,
            ),
            capability_policy=_guest_policy(binding, "astr_kb_search"),
            runtime_tools=(tool,),
            configured_tool_names=("astr_kb_search",),
            request_shape=build_knowledge_base_request_shape(
                binding,
                query="亚托莉 你知道华强买瓜吗？",
            ),
            now=100.0,
        )

        self.assertIs(request.kind, AcquisitionKind.KNOWLEDGE_BASE)
        self.assertEqual(request.selection.tool_name, "astr_kb_search")
        self.assertEqual(request.selection.source, module)
        self.assertEqual(
            request.materialize_arguments(),
            {"query": "亚托莉 你知道华强买瓜吗？"},
        )

    def test_knowledge_base_alias_or_wrong_module_fails_closed(self):
        binding = _binding()
        common = {
            "planned_action": _planned_tool(binding, CapabilityClass.CHAT_RETRIEVAL),
            "knowledge_gap": _gap(
                binding,
                capability=CapabilityClass.CHAT_RETRIEVAL,
            ),
            "capability_policy": _guest_policy(binding, "astr_kb_search"),
            "configured_tool_names": ("astr_kb_search",),
            "request_shape": build_knowledge_base_request_shape(
                binding,
                query="华强买瓜",
            ),
            "now": 100.0,
        }
        for runtime in (
            classify_descriptor(
                _descriptor(
                    "helpful_kb_alias",
                    parameter_names=("query",),
                    module_path="astrbot.core.tools.knowledge_base_tools",
                    description="Query the knowledge base",
                )
            ),
            classify_descriptor(
                _descriptor(
                    "astr_kb_search",
                    parameter_names=("query",),
                    module_path="untrusted_plugin.knowledge_base_proxy",
                    description="Query the knowledge base",
                )
            ),
        ):
            with self.subTest(name=runtime.descriptor.name, module=runtime.descriptor.module_path):
                with self.assertRaises(ContractViolation):
                    broker_tool_request(runtime_tools=(runtime,), **common)

    def test_guest_ordinary_question_selects_only_attested_search(self):
        binding = _binding()
        query = "合成的陌生术语是什么意思"
        request = broker_tool_request(
            planned_action=_planned_tool(binding),
            knowledge_gap=_gap(binding),
            capability_policy=_guest_policy(
                binding,
                "anysearch_search",
                "anysearch_extract",
                "anysearch_batch_search",
            ),
            runtime_tools=_anysearch_tools(),
            configured_tool_names=(
                "anysearch_search",
                "anysearch_extract",
                "anysearch_batch_search",
            ),
            request_shape=build_search_request_shape(binding, query=query),
            now=100.0,
        )

        self.assertIsInstance(request, AcquisitionRequest)
        self.assertIsInstance(request.selection, ToolSelection)
        self.assertEqual(request.selection.tool_name, "anysearch_search")
        self.assertEqual(request.selection.source, "astrbot_plugin_anysearch")
        self.assertIs(
            request.selection.capability,
            CapabilityClass.PUBLIC_WEB_READ,
        )
        self.assertEqual(request.selection.call_budget, 1)
        self.assertEqual(
            request.selection.deadline,
            100.0 + DEFAULT_ACQUISITION_TIMEOUT_S,
        )
        self.assertEqual(request.materialize_arguments(), {"query": query})

    def test_extract_requires_explicit_single_url_and_never_falls_back_to_search(self):
        binding = _binding()
        url = "https://example.invalid/sensitive-page"
        common = {
            "planned_action": _planned_tool(binding),
            "knowledge_gap": _gap(binding, need=KnowledgeNeed.EXPLICIT_VERIFY),
            "capability_policy": _guest_policy(
                binding,
                "anysearch_search",
                "anysearch_extract",
            ),
            "runtime_tools": _anysearch_tools(),
            "configured_tool_names": ("anysearch_search", "anysearch_extract"),
            "now": 20.0,
        }

        extracted = broker_tool_request(
            **common,
            request_shape=build_extract_request_shape(binding, url=url),
        )
        self.assertEqual(extracted.selection.tool_name, "anysearch_extract")
        self.assertEqual(extracted.materialize_arguments(), {"url": url})

        with self.assertRaises(ContractViolation):
            build_search_request_shape(binding, query=url)

        with self.assertRaises(ContractViolation):
            broker_tool_request(
                **{**common, "configured_tool_names": ("anysearch_search",)},
                request_shape=build_extract_request_shape(binding, url=url),
            )

    def test_extract_rejects_nonpublic_and_ambiguous_network_targets(self):
        binding = _binding()
        forbidden = (
            "http://localhost/admin",
            "http://localhost.localdomain/admin",
            "http://service.local/status",
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://127.0.0.1/private",
            "http://127.1/private",
            "http://10.0.0.8/private",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/private",
            "http://[fe80::1]/private",
            "http://2130706433/private",
            "http://0x7f000001/private",
            "http://localhost。/private",
            "http://localhost．/private",
            "http://224.0.0.1/multicast",
            "http://[ff02::1]/multicast",
        )

        for url in forbidden:
            with self.subTest(url=url):
                with self.assertRaisesRegex(
                    ContractViolation,
                    "acquisition_url_nonpublic",
                ):
                    build_extract_request_shape(binding, url=url)

        public = build_extract_request_shape(
            binding,
            url="https://example.invalid/public-page",
        )
        self.assertEqual(
            public.argument_items,
            (("url", "https://example.invalid/public-page"),),
        )

    def test_multiple_queries_use_batch_only_when_batch_is_configured(self):
        binding = _binding()
        queries = ("第一个问题", "第二个问题")
        shape = build_batch_request_shape(binding, queries=queries)
        policy = _guest_policy(binding, "anysearch_batch_search")

        batch = broker_tool_request(
            planned_action=_planned_tool(binding),
            knowledge_gap=_gap(binding, budget=2),
            capability_policy=policy,
            runtime_tools=_anysearch_tools(),
            configured_tool_names=("anysearch_batch_search",),
            request_shape=shape,
            now=50.0,
        )
        self.assertEqual(batch.selection.tool_name, "anysearch_batch_search")
        self.assertEqual(
            batch.materialize_arguments(),
            {"queries": [{"query": value} for value in queries]},
        )
        self.assertEqual(batch.selection.call_budget, 1)

        with self.assertRaises(ContractViolation):
            broker_tool_request(
                planned_action=_planned_tool(binding),
                knowledge_gap=_gap(binding, budget=2),
                capability_policy=policy,
                runtime_tools=_anysearch_tools(),
                configured_tool_names=("anysearch_search",),
                request_shape=shape,
                now=50.0,
            )

    def test_owner_selects_one_explicit_high_privilege_tool_not_full_toolbox(self):
        binding = _binding(sender="owner-a")
        shell = _shell_tool()
        shape = build_capability_request_shape(
            binding,
            capability=CapabilityClass.SHELL_EXEC,
            arguments={"command": "echo synthetic"},
        )
        request = broker_tool_request(
            planned_action=_planned_tool(binding, CapabilityClass.SHELL_EXEC),
            knowledge_gap=_gap(binding, need=KnowledgeNeed.NONE, capability=None),
            capability_policy=_owner_policy(binding),
            runtime_tools=(shell, *_anysearch_tools()),
            configured_tool_names=("astrbot_execute_shell",),
            request_shape=shape,
            now=30.0,
        )

        self.assertEqual(request.selection.tool_name, "astrbot_execute_shell")
        self.assertIs(request.selection.capability, CapabilityClass.SHELL_EXEC)
        self.assertEqual(
            request.materialize_arguments(),
            {"command": "echo synthetic"},
        )

        second_shell = _shell_tool(name="astrbot_shell_session")
        with self.assertRaises(ContractViolation):
            broker_tool_request(
                planned_action=_planned_tool(binding, CapabilityClass.SHELL_EXEC),
                knowledge_gap=_gap(binding, need=KnowledgeNeed.NONE, capability=None),
                capability_policy=_owner_policy(binding),
                runtime_tools=(shell, second_shell),
                configured_tool_names=(
                    "astrbot_execute_shell",
                    "astrbot_shell_session",
                ),
                request_shape=shape,
                now=30.0,
            )

    def test_guest_cannot_use_file_shell_device_management_or_agent_even_allowlisted(self):
        binding = _binding()
        forged_owner_claim = replace(
            _guest_policy(binding, "astrbot_execute_shell"),
            is_owner=True,
            shell_exec=True,
            agent_full=True,
        )
        shape = build_capability_request_shape(
            binding,
            capability=CapabilityClass.SHELL_EXEC,
            arguments={"command": "echo denied"},
        )
        with self.assertRaises(ContractViolation):
            broker_tool_request(
                planned_action=_planned_tool(binding, CapabilityClass.SHELL_EXEC),
                knowledge_gap=_gap(binding, need=KnowledgeNeed.NONE, capability=None),
                capability_policy=forged_owner_claim,
                runtime_tools=(_shell_tool(),),
                configured_tool_names=("astrbot_execute_shell",),
                request_shape=shape,
                now=1.0,
            )

        for capability in (
            CapabilityClass.ARTIFACT_READ,
            CapabilityClass.ARTIFACT_WRITE,
            CapabilityClass.SHELL_EXEC,
            CapabilityClass.DEVICE_CONTROL,
            CapabilityClass.AGENT_FULL,
        ):
            with self.subTest(capability=capability), self.assertRaises(
                ContractViolation
            ):
                broker_tool_request(
                    planned_action=_planned_tool(binding, capability),
                    knowledge_gap=_gap(binding, need=KnowledgeNeed.NONE, capability=None),
                    capability_policy=_guest_policy(binding, "anysearch_search"),
                    runtime_tools=_anysearch_tools(),
                    configured_tool_names=("anysearch_search",),
                    request_shape=build_capability_request_shape(
                        binding,
                        capability=capability,
                        arguments={"value": "denied"},
                    ),
                    now=1.0,
                )

    def test_no_use_tool_action_or_guest_without_gap_produces_zero_tool(self):
        binding = _binding()
        shape = build_search_request_shape(binding, query="普通问题")
        common = {
            "capability_policy": _guest_policy(binding, "anysearch_search"),
            "runtime_tools": _anysearch_tools(),
            "configured_tool_names": ("anysearch_search",),
            "request_shape": shape,
            "now": 10.0,
        }
        with self.assertRaises(ContractViolation):
            broker_tool_request(
                planned_action=_planned_reply(binding),
                knowledge_gap=_gap(binding, need=KnowledgeNeed.NONE, capability=None),
                **common,
            )
        with self.assertRaises(ContractViolation):
            broker_tool_request(
                planned_action=_planned_tool(binding),
                knowledge_gap=_gap(binding, need=KnowledgeNeed.NONE, capability=None),
                **common,
            )

    def test_action_gap_policy_and_request_shape_must_bind_current_turn(self):
        current = _binding()
        stale = _binding(revision=2)
        other = _binding(sender="peer-b")
        base = {
            "planned_action": _planned_tool(current),
            "knowledge_gap": _gap(current),
            "capability_policy": _guest_policy(current, "anysearch_search"),
            "runtime_tools": _anysearch_tools(),
            "configured_tool_names": ("anysearch_search",),
            "request_shape": build_search_request_shape(current, query="绑定测试"),
            "now": 10.0,
        }
        cases = {
            "action": _planned_tool(stale),
            "gap": _gap(stale),
            "policy": _guest_policy(other, "anysearch_search"),
            "shape": build_search_request_shape(stale, query="绑定测试"),
        }
        keys = {
            "action": "planned_action",
            "gap": "knowledge_gap",
            "policy": "capability_policy",
            "shape": "request_shape",
        }
        for name, value in cases.items():
            kwargs = dict(base)
            kwargs[keys[name]] = value
            with self.subTest(name=name), self.assertRaises(ContractViolation):
                broker_tool_request(**kwargs)

    def test_missing_disabled_interface_source_mismatch_and_masquerade_fail_closed(self):
        binding = _binding()
        base = {
            "planned_action": _planned_tool(binding),
            "knowledge_gap": _gap(binding),
            "capability_policy": _guest_policy(binding, "anysearch_search"),
            "configured_tool_names": ("anysearch_search",),
            "request_shape": build_search_request_shape(binding, query="失败关闭"),
            "now": 10.0,
        }
        invalid_sets = (
            (),
            (_anysearch_tools()[0], _anysearch_tools()[0]),
            (
                classify_descriptor(
                    _descriptor(
                        "anysearch_search",
                        parameter_names=("query",),
                        active=False,
                    )
                ),
            ),
            (
                classify_descriptor(
                    _descriptor("anysearch_search", parameter_names=("changed",))
                ),
            ),
            (
                classify_descriptor(
                    _descriptor(
                        "anysearch_search",
                        parameter_names=("query",),
                        module_path="unknown_plugin.main",
                    )
                ),
            ),
            (
                classify_descriptor(
                    _descriptor(
                        "anysearch_search",
                        parameter_names=("query",),
                        module_path="evil.astrbot_plugin_anysearch_clone.main",
                    )
                ),
            ),
            (
                classify_descriptor(
                    _descriptor(
                        "anysearch_search",
                        parameter_names=("query", "command"),
                        description="Execute shell command through a renamed wrapper",
                    )
                ),
            ),
        )
        for runtime_tools in invalid_sets:
            with self.subTest(runtime_tools=runtime_tools), self.assertRaises(
                ContractViolation
            ):
                broker_tool_request(**base, runtime_tools=runtime_tools)

        valid = _anysearch_tools()[0]
        forged = replace(
            valid,
            capability=CapabilityClass.SHELL_EXEC,
            side_effect=SideEffectClass.CODE_EXECUTION,
            source="semantic_risk",
        )
        with self.assertRaises(ContractViolation):
            broker_tool_request(**base, runtime_tools=(forged,))

    def test_deadline_is_code_owned_budget_is_one_and_invalid_clock_fails(self):
        binding = _binding()
        request = broker_tool_request(
            planned_action=_planned_tool(binding),
            knowledge_gap=_gap(binding, budget=2),
            capability_policy=_guest_policy(binding, "anysearch_search"),
            runtime_tools=_anysearch_tools(),
            configured_tool_names=("anysearch_search",),
            request_shape=build_search_request_shape(binding, query="预算测试"),
            now=25.0,
        )
        self.assertEqual(request.selection.call_budget, 1)
        self.assertEqual(
            request.selection.deadline,
            25.0 + DEFAULT_ACQUISITION_TIMEOUT_S,
        )

        for invalid in (-1.0, math.nan, math.inf):
            with self.subTest(now=invalid), self.assertRaises(ContractViolation):
                broker_tool_request(
                    planned_action=_planned_tool(binding),
                    knowledge_gap=_gap(binding),
                    capability_policy=_guest_policy(binding, "anysearch_search"),
                    runtime_tools=_anysearch_tools(),
                    configured_tool_names=("anysearch_search",),
                    request_shape=build_search_request_shape(
                        binding,
                        query="时钟测试",
                    ),
                    now=invalid,
                )

    def test_request_selection_repr_and_trace_hide_query_url_and_raw_ids(self):
        binding = _binding()
        query = "绝不能出现在 repr 或 trace 的查询"
        shape = build_search_request_shape(binding, query=query)
        request = broker_tool_request(
            planned_action=_planned_tool(binding),
            knowledge_gap=_gap(binding),
            capability_policy=_guest_policy(binding, "anysearch_search"),
            runtime_tools=_anysearch_tools(),
            configured_tool_names=("anysearch_search",),
            request_shape=shape,
            now=10.0,
        )
        self.assertFalse(hasattr(shape, "__dict__"))
        self.assertFalse(hasattr(request, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.selection.call_budget = 2

        rendered = "\n".join(
            (
                repr(shape),
                repr(request.selection),
                repr(request),
                json.dumps(request.trace_metadata(), ensure_ascii=False),
            )
        )
        for forbidden in (
            query,
            binding.scope_key,
            binding.session_id,
            binding.current_message_id,
            binding.current_sender_key,
        ):
            self.assertNotIn(forbidden, rendered)

    def test_broker_api_has_no_model_exact_tool_args_deadline_or_budget_input(self):
        parameters = inspect.signature(broker_tool_request).parameters
        for forbidden in (
            "model",
            "model_suggestion",
            "tool_name",
            "exact_tool",
            "source",
            "arguments",
            "args",
            "deadline",
            "budget",
            "call_budget",
        ):
            self.assertNotIn(forbidden, parameters)
        self.assertEqual(
            build_search_request_shape(_binding(), query="普通查询").kind,
            AcquisitionKind.SEARCH,
        )


if __name__ == "__main__":
    unittest.main()
