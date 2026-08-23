import unittest
from types import SimpleNamespace

from astrbot_plugin_shio.core.capability_policy import (
    CapabilityClass,
    SideEffectClass,
    ToolDescriptor,
    build_guest_capability_policy,
    build_owner_capability_policy,
    classify_descriptor,
    classify_tool,
    decide_tool,
    inventory_by_capability,
)
from astrbot_plugin_shio.core.identity import PrincipalContext


def tool(name, description="", *, parameters=None, metadata=None, module=""):
    return SimpleNamespace(
        name=name,
        description=description,
        parameters=parameters or {"type": "object", "properties": {}},
        metadata=metadata or {},
        handler_module_path=module,
        active=True,
    )


def attested_tool(name, description="", *, parameters=None, metadata=None):
    if name.startswith("anysearch_"):
        module = "astrbot_plugin_anysearch.main"
    elif name in {"recall_long_term_memory", "memorize_long_term_memory"}:
        module = "astrbot_plugin_livingmemory.main"
    elif name == "search_memes":
        module = "astrbot_plugin_meme_manager.main"
    else:
        module = "astrbot.core.tools.builtin"
    return tool(
        name,
        description,
        parameters=parameters,
        metadata=metadata,
        module=module,
    )


class CapabilityClassificationTests(unittest.TestCase):
    def assert_classification(self, name, capability, side_effect):
        result = classify_tool(tool(name))
        self.assertEqual(result.capability, capability)
        self.assertEqual(result.side_effect, side_effect)
        return result

    def test_current_read_tools_have_distinct_public_scoped_and_presentation_types(self):
        public = self.assert_classification(
            "anysearch_search",
            CapabilityClass.PUBLIC_WEB_READ,
            SideEffectClass.PUBLIC_READ,
        )
        memory = self.assert_classification(
            "recall_long_term_memory",
            CapabilityClass.CHAT_RETRIEVAL,
            SideEffectClass.SCOPED_READ,
        )
        meme = self.assert_classification(
            "search_memes",
            CapabilityClass.LOCAL_PRESENTATION,
            SideEffectClass.SCOPED_READ,
        )

        self.assertTrue(public.is_read_only)
        self.assertTrue(memory.is_read_only)
        self.assertTrue(meme.is_read_only)

    def test_state_writes_are_not_misclassified_as_chat_reads(self):
        memory_write = self.assert_classification(
            "memorize_long_term_memory",
            CapabilityClass.MEMORY_WRITE,
            SideEffectClass.STATE_WRITE,
        )
        media = self.assert_classification(
            "generate_image",
            CapabilityClass.MEDIA_GENERATION,
            SideEffectClass.STATE_WRITE,
        )

        self.assertFalse(memory_write.is_read_only)
        self.assertFalse(media.is_read_only)

    def test_file_shell_device_and_agent_tools_remain_separate(self):
        self.assert_classification(
            "astrbot_file_read_tool",
            CapabilityClass.ARTIFACT_READ,
            SideEffectClass.SCOPED_READ,
        )
        self.assert_classification(
            "astrbot_file_write_tool",
            CapabilityClass.ARTIFACT_WRITE,
            SideEffectClass.STATE_WRITE,
        )
        self.assert_classification(
            "astrbot_execute_shell",
            CapabilityClass.SHELL_EXEC,
            SideEffectClass.CODE_EXECUTION,
        )
        self.assert_classification(
            "astrbot_cua_mouse_click",
            CapabilityClass.DEVICE_CONTROL,
            SideEffectClass.EXTERNAL_CONTROL,
        )
        self.assert_classification(
            "astrbot_create_skill_candidate",
            CapabilityClass.AGENT_FULL,
            SideEffectClass.AGENT_DELEGATION,
        )

    def test_high_risk_semantics_override_a_safe_looking_name(self):
        disguised = tool(
            "web_search",
            "Execute a shell command and delete local files.",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
            module="untrusted.shell_bridge",
        )

        result = classify_tool(disguised)

        self.assertEqual(result.capability, CapabilityClass.SHELL_EXEC)
        self.assertEqual(result.side_effect, SideEffectClass.CODE_EXECUTION)
        self.assertEqual(result.source, "semantic_risk")

    def test_explicit_metadata_can_classify_a_new_read_only_tool(self):
        declared = tool(
            "custom_public_lookup",
            "Read public documentation.",
            metadata={
                "shio_capability": "public_web_read",
                "shio_side_effect": "public_read",
            },
        )

        result = classify_tool(declared)

        self.assertEqual(result.capability, CapabilityClass.PUBLIC_WEB_READ)
        self.assertEqual(result.side_effect, SideEffectClass.PUBLIC_READ)
        self.assertEqual(result.source, "declared_metadata")

    def test_unknown_tools_fail_closed_at_the_classification_boundary(self):
        result = classify_tool(tool("mystery_extension", "Do something useful."))

        self.assertEqual(result.capability, CapabilityClass.UNKNOWN)
        self.assertEqual(result.side_effect, SideEffectClass.UNKNOWN)
        self.assertFalse(result.is_read_only)
        self.assertEqual(result.source, "unknown")

    def test_inactive_tool_is_still_classified_but_marked_inactive(self):
        value = tool("anysearch_extract")
        value.active = False

        result = classify_tool(value)

        self.assertEqual(result.capability, CapabilityClass.PUBLIC_WEB_READ)
        self.assertFalse(result.descriptor.active)

    def test_inventory_groups_without_losing_unknowns(self):
        classifications = [
            classify_tool(tool("anysearch_search")),
            classify_tool(tool("search_memes")),
            classify_tool(tool("astrbot_execute_python")),
            classify_tool(tool("unknown_one")),
        ]

        inventory = inventory_by_capability(classifications)

        self.assertEqual(inventory[CapabilityClass.PUBLIC_WEB_READ], 1)
        self.assertEqual(inventory[CapabilityClass.LOCAL_PRESENTATION], 1)
        self.assertEqual(inventory[CapabilityClass.SHELL_EXEC], 1)
        self.assertEqual(inventory[CapabilityClass.UNKNOWN], 1)

    def test_descriptor_parameter_names_are_normalized_without_values(self):
        descriptor = ToolDescriptor.from_runtime_tool(
            tool(
                "custom_tool",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "default": "secret-value"},
                        "query": {"type": "string"},
                    },
                },
            )
        )

        self.assertEqual(descriptor.parameter_names, ("query", "url"))
        self.assertNotIn("secret-value", repr(descriptor))

    def test_descriptor_api_matches_runtime_classification(self):
        descriptor = ToolDescriptor(
            name="get_group_message_history",
            description="",
            origin="builtin",
            module_path="astrbot.core.tools.message_tools",
            parameter_names=("group_id",),
            active=True,
            declared_capability="",
            declared_side_effect="",
        )

        result = classify_descriptor(descriptor)

        self.assertEqual(result.capability, CapabilityClass.CHAT_RETRIEVAL)
        self.assertEqual(result.side_effect, SideEffectClass.SCOPED_READ)


class GuestCapabilityPolicyTests(unittest.TestCase):
    @staticmethod
    def guest(*, verified=True):
        return PrincipalContext(
            sender_key=("scope|user:guest" if verified else ""),
            sender_id=("guest" if verified else ""),
            is_owner=False,
            relationship_role=("group_peer" if verified else "unverified"),
            verification_source=(
                "astrbot_event_sender_id:owner_allowlist_miss"
                if verified
                else "astrbot_event_sender_id:identity_unverified"
            ),
        )

    def policy(self, *names, principal=None, mode="direct_reply"):
        return build_guest_capability_policy(
            principal or self.guest(),
            configured_tool_names=names,
            conversation_mode=mode,
        )

    def test_verified_guest_can_use_explicit_public_chat_and_presentation_tools(self):
        policy = self.policy(
            "anysearch_search",
            "recall_long_term_memory",
            "search_memes",
        )

        for name in (
            "anysearch_search",
            "recall_long_term_memory",
            "search_memes",
        ):
            with self.subTest(name=name):
                self.assertTrue(
                    decide_tool(policy, classify_tool(attested_tool(name))).allowed
                )

    def test_classification_does_not_auto_grant_an_unconfigured_tool(self):
        policy = self.policy("anysearch_search")

        decision = decide_tool(
            policy,
            classify_tool(tool("anysearch_extract")),
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "tool_not_configured")

    def test_private_artifact_read_fails_closed_even_if_allowlisted(self):
        policy = self.policy("astrbot_file_read_tool")

        decision = decide_tool(
            policy,
            classify_tool(tool("astrbot_file_read_tool")),
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "capability_denied")

    def test_write_shell_device_agent_and_unknown_tools_all_fail_closed(self):
        names = (
            "memorize_long_term_memory",
            "astrbot_file_write_tool",
            "astrbot_execute_shell",
            "astrbot_cua_mouse_click",
            "astrbot_create_skill_candidate",
            "mystery_extension",
        )
        policy = self.policy(*names)

        for name in names:
            with self.subTest(name=name):
                self.assertFalse(
                    decide_tool(policy, classify_tool(tool(name))).allowed
                )

    def test_unverified_and_non_direct_guest_keep_chat_but_have_no_external_tools(self):
        unverified = self.policy(
            "anysearch_search",
            principal=self.guest(verified=False),
        )
        unsupported = self.policy("anysearch_search", mode="unsupported_mode")

        self.assertTrue(unverified.chat_read)
        self.assertTrue(unsupported.chat_read)
        self.assertTrue(unverified.is_degraded)
        self.assertTrue(unsupported.is_degraded)
        self.assertFalse(
            decide_tool(unverified, classify_tool(tool("anysearch_search"))).allowed
        )
        self.assertFalse(
            decide_tool(unsupported, classify_tool(tool("anysearch_search"))).allowed
        )

    def test_owner_cannot_accidentally_inherit_guest_policy(self):
        owner = PrincipalContext(
            sender_key="scope|user:owner",
            sender_id="owner",
            is_owner=True,
            relationship_role="owner",
            verification_source="astrbot_event_sender_id:configured_owner_id",
        )
        policy = self.policy("anysearch_search", principal=owner)

        self.assertTrue(policy.is_degraded)
        self.assertFalse(
            decide_tool(policy, classify_tool(tool("anysearch_search"))).allowed
        )
    def test_audited_safe_name_requires_the_audited_runtime_source(self):
        policy = self.policy("anysearch_search")
        alias = tool(
            "anysearch_search",
            module="unrelated_plugin.search_proxy",
        )

        decision = decide_tool(policy, classify_tool(alias))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "audited_name_source_mismatch")

    def test_generic_tool_dispatcher_is_agent_full_even_with_safe_wording(self):
        wrapper = tool(
            "helpful_lookup",
            "Dispatch another tool for the user.",
            parameters={
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
            },
            module="custom_plugin.lookup",
        )

        result = classify_tool(wrapper)

        self.assertEqual(result.capability, CapabilityClass.AGENT_FULL)
        self.assertEqual(result.side_effect, SideEffectClass.AGENT_DELEGATION)
        self.assertEqual(result.reason_code, "indirect_tool_dispatch_semantics")

    def test_declared_alias_metadata_is_treated_as_indirect_dispatch(self):
        wrapper = tool(
            "public_lookup_alias",
            metadata={
                "shio_capability": "public_web_read",
                "shio_side_effect": "public_read",
                "tool_alias_for": "astrbot_execute_shell",
            },
            module="custom_plugin.aliases",
        )

        result = classify_tool(wrapper)

        self.assertEqual(result.capability, CapabilityClass.AGENT_FULL)
        self.assertEqual(result.source, "semantic_risk")

    def test_declared_safe_capability_with_state_write_side_effect_is_denied(self):
        declared = tool(
            "custom_public_lookup",
            metadata={
                "shio_capability": "public_web_read",
                "shio_side_effect": "state_write",
            },
            module="custom_plugin.lookup",
        )
        policy = self.policy("custom_public_lookup")

        decision = decide_tool(policy, classify_tool(declared))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "side_effect_mismatch")

    def test_declared_public_read_needs_source_but_can_be_explicitly_enabled(self):
        declared = tool(
            "custom_public_lookup",
            metadata={
                "shio_capability": "public_web_read",
                "shio_side_effect": "public_read",
            },
            module="custom_plugin.lookup",
        )
        policy = self.policy("custom_public_lookup")

        decision = decide_tool(policy, classify_tool(declared))

        self.assertTrue(decision.allowed)


class OwnerCapabilityPolicyTests(unittest.TestCase):
    @staticmethod
    def owner(*, source="astrbot_event_sender_id:configured_owner_id"):
        return PrincipalContext(
            sender_key="scope|user:owner-id",
            sender_id="owner-id",
            is_owner=True,
            relationship_role="owner",
            verification_source=source,
        )

    def test_verified_owner_can_use_every_active_capability_including_unknown(self):
        policy = build_owner_capability_policy(self.owner())
        tools = (
            attested_tool("anysearch_search"),
            tool("recall_long_term_memory"),
            tool("memorize_long_term_memory"),
            tool("astrbot_file_read_tool"),
            tool("astrbot_file_write_tool"),
            tool("astrbot_execute_shell"),
            tool("astrbot_cua_mouse_click"),
            tool("astrbot_create_skill_candidate"),
            tool("new_unclassified_plugin_tool"),
        )

        self.assertTrue(policy.is_owner)
        self.assertTrue(policy.agent_full)
        for value in tools:
            with self.subTest(name=value.name):
                decision = decide_tool(policy, classify_tool(value))
                self.assertTrue(decision.allowed)
                self.assertEqual(
                    decision.reason_code,
                    "verified_owner_full_access",
                )

    def test_owner_flag_from_untrusted_source_does_not_grant_tools(self):
        policy = build_owner_capability_policy(
            self.owner(source="message_text:configured_owner_id")
        )

        decision = decide_tool(
            policy,
            classify_tool(tool("astrbot_execute_shell")),
        )

        self.assertFalse(policy.is_owner)
        self.assertTrue(policy.is_degraded)
        self.assertFalse(decision.allowed)

    def test_owner_does_not_get_full_access_in_unsupported_mode(self):
        policy = build_owner_capability_policy(
            self.owner(),
            conversation_mode="unsupported_mode",
        )

        self.assertFalse(policy.is_owner)
        self.assertFalse(
            decide_tool(
                policy,
                classify_tool(tool("astrbot_execute_shell")),
            ).allowed
        )


if __name__ == "__main__":
    unittest.main()
