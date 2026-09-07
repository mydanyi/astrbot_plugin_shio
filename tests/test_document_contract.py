"""Checks that public documentation matches the current Shio candidate."""

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
            DOCS / "PITFALL_LEDGER.md",
            DOCS / "REVIEW_PLAYBOOK.md",
        ):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), path)
                self.assertTrue(path.read_text(encoding="utf-8").strip(), path)

    def test_readme_describes_current_product_and_known_limit(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for phrase in (
            "修复候选",
            "4.27.4",
            "aiocqhttp",
            "event.request_llm()",
            "REPLY",
            "WAIT",
            "NO_ACTION",
            "ProviderRequest.contexts",
            "群聊消息记录注入上下文",
            "尚未作为稳定版发布",
            "从 AstrBot 已有模型中下拉选择",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, readme)

        for retired_claim in (
            "当前版本：",
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
        self.assertEqual(53, len(descriptions))
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
        self.assertIn('astrbot_version: "==4.27.4"', metadata)
        self.assertIn("- aiocqhttp", metadata)

        compatibility = (DOCS / "COMPATIBILITY.md").read_text(encoding="utf-8")
        self.assertIn(version.group(1), compatibility)
        self.assertIn("修复候选", compatibility)
        self.assertIn("4.27.4", compatibility)
        self.assertIn("aiocqhttp", compatibility)

    def test_auxiliary_timeout_descriptions_state_one_shared_budget(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        groups = schema["sys001"]["items"]
        hints = (
            groups["group"]["items"]["name_semantic_timeout_seconds"]["hint"],
            groups["group"]["items"]["natural_decision_timeout_seconds"]["hint"],
            groups["final_review"]["items"]["timeout_seconds"]["hint"],
        )
        for hint in hints:
            self.assertIn("共用总等待时间", hint)
            self.assertIn("当前模型", hint)
            self.assertIn("剩余时间", hint)
            self.assertIn("真正耗尽", hint)

    def test_documents_keep_astrbot_owner_boundary(self) -> None:
        architecture = (DOCS / "ARCHITECTURE.md").read_text(encoding="utf-8")
        pitfall = (DOCS / "PITFALL_LEDGER.md").read_text(encoding="utf-8")
        review = (DOCS / "REVIEW_PLAYBOOK.md").read_text(encoding="utf-8")

        for phrase in (
            "正式回复始终通过",
            "不复制管理员、Persona、conversation、Provider fallback",
            "AstrBot 继续执行结果装饰和第一条标准发送",
            "不是 QQ 网络送达回执",
        ):
            self.assertIn(phrase, architecture)

        self.assertIn("不要用错误的官方功能补另一个功能", pitfall)
        self.assertIn("工具可见性不是工具权限", pitfall)
        self.assertIn("临时群聊上下文不会保存进长期 conversation", review)
        self.assertIn("没有整批复制持久化群聊历史到", review)

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
