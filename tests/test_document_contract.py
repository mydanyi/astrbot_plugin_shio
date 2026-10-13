"""Checks that public documentation matches the current Shio release."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


class DocumentationContractTests(unittest.TestCase):
    def test_public_document_set_exists(self) -> None:
        for path in (
            ROOT / "README.md",
            DOCS / "SETTINGS_GUIDE.md",
            DOCS / "ARCHITECTURE.md",
            DOCS / "COMPATIBILITY.md",
            DOCS / "INSTALLATION_AND_ROLLBACK.md",
            DOCS / "TROUBLESHOOTING.md",
        ):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), path)
                self.assertTrue(path.read_text(encoding="utf-8").strip(), path)

    def test_readme_is_user_facing_without_internal_pipeline_leak(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        # 面向使用者的产品介绍不暴露内部实现符号，也不宣传已退休的能力。
        for internal in (
            "event.request_llm()",
            "ProviderRequest.contexts",
            "REPLY",
            "WAIT",
            "NO_ACTION",
        ):
            with self.subTest(internal=internal):
                self.assertNotIn(internal, readme)

        for retired_claim in (
            "AstrBot：>=4.26.7,<5",
            "启用主动发起话题",
            "待补答队列容量",
            "主人 ID 白名单",
            "普通用户只读工具白名单",
        ):
            with self.subTest(retired_claim=retired_claim):
                self.assertNotIn(retired_claim, readme)

    def test_settings_guide_covers_every_visible_schema_field(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        guide = (DOCS / "SETTINGS_GUIDE.md").read_text(encoding="utf-8")

        def visible_descriptions(node: dict) -> list[str]:
            if node.get("type") != "object":
                return [node["description"]]
            result: list[str] = []
            for child in node["items"].values():
                result.extend(visible_descriptions(child))
            return result

        descriptions = visible_descriptions(schema["sys001"])
        self.assertEqual(70, len(descriptions))
        for description in descriptions:
            with self.subTest(description=description):
                self.assertIn(description, guide)

        self.assertIn("不要手写 Provider ID", guide)
        self.assertIn("群聊消息记录注入上下文", guide)
        self.assertIn("Shio 不自动改变这两项设置", guide)

    def test_provider_fields_use_astrbot_selectors(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        groups = schema["sys001"]["items"]
        provider_fields = (
            groups["group"]["items"]["name_semantic_provider_id"],
            groups["group"]["items"]["natural_decision_provider_id"],
            groups["final_review"]["items"]["provider_id"],
            groups["presentation"]["items"]["model_segment_provider_id"],
            groups["character_dialogue"]["items"]["preparation"]["items"]["provider_id"],
        )
        fallback_fields = (
            groups["group"]["items"]["name_semantic_fallback_provider_ids"],
            groups["group"]["items"]["natural_decision_fallback_provider_ids"],
            groups["final_review"]["items"]["fallback_provider_ids"],
            groups["presentation"]["items"]["model_segment_fallback_provider_ids"],
        )
        for field in provider_fields:
            self.assertEqual("select_provider", field.get("_special"))
        for field in fallback_fields:
            self.assertEqual("select_providers", field.get("_special"))

    def test_schema_and_metadata_match_documented_version(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIn("sys001", schema)
        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        version = re.search(r'^version: "([^"]+)"$', metadata, re.MULTILINE)
        self.assertIsNotNone(version)
        self.assertIn(version.group(1), (ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn('astrbot_version: ">=4.27.4,<=4.28.2"', metadata)
        self.assertIn("- aiocqhttp", metadata)

        compatibility = (DOCS / "COMPATIBILITY.md").read_text(encoding="utf-8")
        self.assertIn(version.group(1), compatibility)
        self.assertIn("4.27.4", compatibility)
        self.assertIn("aiocqhttp", compatibility)

    def test_release_version_is_stable_semver(self) -> None:
        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        version = re.search(r'^version: "([^"]+)"$', metadata, re.MULTILINE)
        self.assertIsNotNone(version)

        # 商店消费的发布版本必须是三段稳定 SemVer：每段为非负整数且无前导零。
        stable = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
        self.assertRegex(version.group(1), stable)

    def test_auxiliary_timeout_descriptions_state_one_shared_budget(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        groups = schema["sys001"]["items"]
        hints = (
            groups["group"]["items"]["name_semantic_timeout_seconds"]["hint"],
            groups["group"]["items"]["natural_decision_timeout_seconds"]["hint"],
            groups["final_review"]["items"]["timeout_seconds"]["hint"],
        )
        for hint in hints:
            self.assertIn("共用", hint)
            self.assertIn("秒", hint)

    def test_architecture_keeps_astrbot_owner_boundary(self) -> None:
        architecture = (DOCS / "ARCHITECTURE.md").read_text(encoding="utf-8")

        for phrase in (
            "正式回复始终通过",
            "不复制管理员、Persona、conversation、Provider fallback",
            "AstrBot 继续执行结果装饰和第一条标准发送",
            "不是 QQ 网络送达回执",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, architecture)

    def test_retired_delivery_owners_are_absent_from_runtime(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        schema = (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        for retired in (
            "compat_send_message",
            "compat_send_prepared_message",
            "text_component_interval",
            "SendReceipt",
            "sent_segments",
        ):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, source)
                self.assertNotIn(retired, schema)


if __name__ == "__main__":
    unittest.main()
