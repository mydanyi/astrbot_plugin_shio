from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
RELEASE_VERSION = "0.5.14"


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _fields(schema: dict) -> dict:
    return {
        key: field
        for section in schema.values()
        for key, field in section["items"].items()
    }


class P10ReleaseSurfaceTests(unittest.TestCase):
    def test_name_wake_mode_is_visible_inside_the_name_wake_group(self):
        schema = json.loads(_text("_conf_schema.json"))
        wake = schema["wake_settings"]["items"]
        keys = tuple(wake)

        enabled_index = keys.index("natural_name_wake_enabled")
        self.assertEqual(
            keys[enabled_index : enabled_index + 4],
            (
                "natural_name_wake_enabled",
                "natural_name_wake_mode",
                "natural_name_wake_aliases",
                "natural_name_wake_group_whitelist",
            ),
        )
        mode = wake["natural_name_wake_mode"]
        self.assertEqual(mode["description"], "称名唤醒方式")
        self.assertEqual(mode["options"], ["natural", "contains"])
        self.assertEqual(
            mode["labels"],
            ["自然语言判断（推荐）", "关键词出现即唤醒"],
        )
        self.assertEqual(mode["default"], "natural")

    def test_metadata_readme_and_changelog_share_one_release_version(self):
        metadata = _text("metadata.yaml")
        readme = _text("README.md")
        changelog = _text("CHANGELOG.md")
        self.assertRegex(metadata, rf"(?m)^version: {re.escape(RELEASE_VERSION)}$")
        self.assertIn(f"astrbot_plugin_shio_v{RELEASE_VERSION}_upload.zip", readme)
        self.assertRegex(
            changelog,
            rf"(?m)^## {re.escape(RELEASE_VERSION)} - 2026-08-23$",
        )
        self.assertIn('astrbot_version: ">=4.26.7,<5"', metadata)

    def test_schema_is_current_closed_and_high_risk_defaults_are_all_off(self):
        schema = json.loads(_text("_conf_schema.json"))
        fields = _fields(schema)
        self.assertEqual(len(schema), 8)
        self.assertEqual(len(fields), 57)
        self.assertIs(fields["meme_complement_enabled"]["default"], True)
        self.assertEqual(fields["meme_complement_cadence_turns"]["default"], 4)
        self.assertEqual(fields["meme_complement_cooldown_turns"]["default"], 4)
        self.assertIn(
            "Meme Manager",
            fields["meme_complement_cadence_turns"]["hint"],
        )
        for key in (
            "natural_group_participation_enabled",
            "proactive_initiation_enabled",
            "owner_action_enabled",
            "owner_action_artifact_read_exact_enabled",
            "owner_action_artifact_grep_enabled",
            "owner_action_memory_write_literal_enabled",
            "owner_action_sandbox_shell_once_enabled",
        ):
            self.assertIs(fields[key]["default"], False, key)
        self.assertEqual(fields["natural_group_participation_allowlist"]["default"], [])
        self.assertEqual(fields["proactive_group_allowlist"]["default"], [])
        self.assertEqual(fields["owner_ids"]["default"], [])
        self.assertIn("代码级永久关闭", fields["owner_action_sandbox_shell_once_enabled"]["hint"])
        self.assertNotIn("部分名称", fields["permission_audit_log"]["hint"])

    def test_every_schema_field_has_public_shape_runtime_read_and_audit_entry(self):
        schema = json.loads(_text("_conf_schema.json"))
        fields = _fields(schema)
        source = _text("main.py")
        audit = _text("docs/CONFIG_AUDIT.md")
        for key, field in fields.items():
            with self.subTest(key=key):
                self.assertEqual(
                    set(field).intersection({"description", "type", "default"}),
                    {"description", "type", "default"},
                )
                self.assertIn(field["type"], {"bool", "int", "string", "list"})
                self.assertIn(f'"{key}"', source)
                self.assertIn(f"`{key}`", audit)

    def test_public_docs_describe_current_capabilities_and_external_boundaries(self):
        public = "\n".join(
            _text(path)
            for path in (
                "README.md",
                "docs/CONFIG_AUDIT.md",
                "docs/ARCHITECTURE.md",
                "docs/COMPATIBILITY.md",
                "docs/PRIVACY_AND_SECURITY.md",
                "docs/TESTING.md",
            )
        )
        for required in (
            "1,200+",
            "57",
            "X-01",
            "O1",
            "仅图片",
            "默认关闭",
            "proactive_initiation_enabled",
        ):
            self.assertIn(required, public)
        for stale in (
            "363 项",
            "158 个核心",
            "23 个顶层字段",
            "配置页现在只保留 23 个",
            "未点名自然接话、主动开题和延迟补答在当前版本中不存在",
            "旧版未点名主动接话、主动开题和延迟补答已从运行代码中删除",
            "主人执行 `/agent`、`/task`、代码、文件和设备调试，确认原始工具集保留",
        ):
            self.assertNotIn(stale, public)

    def test_readme_does_not_claim_deployment_or_live_owner_execution(self):
        readme = _text("README.md")
        self.assertIn("四个主人动作适配器保持关闭", readme)
        self.assertIn("Shell 永久硬关闭", readme)
        self.assertIn("O1 尚未执行", readme)
        self.assertNotIn("主人动作已启用", readme)
        self.assertNotIn("保证 LivingMemory 零存储", readme)

    def test_public_document_local_links_resolve(self):
        paths = (
            "README.md",
            "docs/CONFIG_AUDIT.md",
            "docs/ARCHITECTURE.md",
            "docs/COMPATIBILITY.md",
            "docs/PRIVACY_AND_SECURITY.md",
            "docs/TESTING.md",
        )
        link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
        for path in paths:
            document = ROOT / path
            for raw_target in link_pattern.findall(document.read_text(encoding="utf-8")):
                target = raw_target.split("#", 1)[0]
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                resolved = (document.parent / target).resolve()
                with self.subTest(document=path, target=target):
                    self.assertTrue(resolved.is_file(), resolved)


if __name__ == "__main__":
    unittest.main()
