"""Standard-library checks for the candidate documentation boundary."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
D089_BEGIN = "<!-- SYS001_D089_OWNER_CONTRACT_BEGIN -->"
D089_END = "<!-- SYS001_D089_OWNER_CONTRACT_END -->"
D089_RENDER_BEGIN = "<!-- SYS001_D089_OWNER_RENDER_BEGIN -->"
D089_RENDER_END = "<!-- SYS001_D089_OWNER_RENDER_END -->"
D089_OWNER = {
    "contract": "SYS001-D089-owner-v1",
    "astrbot_owner": ["adapter", "standard_first_send", "official_segmented"],
    "shio_when": {"fixed_safe": True, "official_segmented": "disabled"},
    "shio_action": ["public_event_send", "orchestrate_remaining_bubbles", "await_remaining_bubbles"],
    "shio_not_owner": ["network_receipt", "retry", "compensation", "second_adapter"],
}
D089_RENDER = (
    "D-089 owner 合同：AstrBot 拥有 adapter、标准首发和官方 segmented；"
    "只有 fixed-safe 且官方 segmented 关闭时，Shio 才通过公开 `event.send()` "
    "编排并等待剩余气泡。Shio 不拥有网络 receipt、retry、compensation 或第二 adapter。"
)
# This is deliberately the one frozen Reviewer counterexample, not a natural
# language classifier.  It is compared after Unicode whitespace is removed so
# formatting cannot turn that exact conflicting ownership claim into prose the
# bounded D-089 authority units fail to reject.
_D089_PLAIN_CONFLICT_COMPACT = "所有文本气泡的分段、间隔和发送都只由AstrBot执行；Shio在任何情况下都不会发送文字"


def _d089_contract_block(contract: dict) -> str:
    return f"{D089_BEGIN}\n```json\n{json.dumps(contract, ensure_ascii=False)}\n```\n{D089_END}"


def _d089_render_block(render: str = D089_RENDER) -> str:
    return f"{D089_RENDER_BEGIN}\n{render}\n{D089_RENDER_END}"


def _remove_unicode_whitespace(value: str) -> str:
    return "".join(character for character in value if not character.isspace())


def _parse_d089_owner_contract(document: str) -> dict:
    """Parse bounded D-089 authority units; this intentionally does no NLP."""
    contract_pattern = re.escape(D089_BEGIN) + r"\s*```json\s*(.*?)\s*```\s*" + re.escape(D089_END)
    render_pattern = re.escape(D089_RENDER_BEGIN) + r"\s*(.*?)\s*" + re.escape(D089_RENDER_END)
    blocks = re.findall(contract_pattern, document, flags=re.DOTALL)
    if len(blocks) != 1:
        raise ValueError("D-089 owner contract must occur exactly once")
    try:
        contract = json.loads(blocks[0])
    except json.JSONDecodeError as error:
        raise ValueError("D-089 owner contract must be JSON") from error
    if contract != D089_OWNER:
        raise ValueError("D-089 owner contract fields do not match the approved owner boundary")
    render_blocks = re.findall(render_pattern, document, flags=re.DOTALL)
    if len(render_blocks) != 1 or render_blocks[0].strip() != D089_RENDER:
        raise ValueError("D-089 owner render must occur exactly once and match the authority data")
    outside_authority = re.sub(contract_pattern, "", document, flags=re.DOTALL)
    outside_authority = re.sub(render_pattern, "", outside_authority, flags=re.DOTALL)
    if _D089_PLAIN_CONFLICT_COMPACT in _remove_unicode_whitespace(outside_authority):
        raise ValueError("unmarked D-089 AstrBot-only/Shio-never-send claim conflicts with the authority unit")
    return contract


def _replace_d089_owner_contract(document: str, contract: dict) -> str:
    pattern = re.escape(D089_BEGIN) + r"\s*```json\s*.*?\s*```\s*" + re.escape(D089_END)
    replaced, count = re.subn(pattern, _d089_contract_block(contract), document, count=1, flags=re.DOTALL)
    if count != 1:
        raise ValueError("test fixture did not contain the approved D-089 owner unit")
    return replaced


class DocumentationContractTests(unittest.TestCase):
    def test_required_documents_and_honest_readme_contract_exist(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for path in (
            ROOT / "docs" / "INSTALLATION_AND_ROLLBACK.md",
            ROOT / "docs" / "COMPATIBILITY.md",
            ROOT / "docs" / "PITFALL_LEDGER.md",
            ROOT / "docs" / "REVIEW_PLAYBOOK.md",
        ):
            self.assertTrue(path.is_file(), path)
        for phrase in (
            "本地候选",
            "不读、不迁移",
            "AstrBot",
            "segmented_reply",
            "REPLY",
            "前置判定辅助模型",
            "无网络送达成功回执",
            "RespondStage",
        ):
            self.assertIn(phrase, readme)
        for false_claim in ("标准发送回执", "已支持生产", "最终文本气泡"):
            self.assertNotIn(false_claim, readme)

    def test_installation_document_states_the_fixed_owner_and_rollback_boundaries(self) -> None:
        document = (ROOT / "docs" / "INSTALLATION_AND_ROLLBACK.md").read_text(encoding="utf-8")
        for phrase in (
            "不会读取、迁移或自动删除旧 0.5.x",
            "group_message_history_enable",
            "group_icl_enable",
            "LivingMemory",
            "Meme Manager 为 4.15.4",
            "(?s).+",
            "Master 告警默认关闭",
            "不是网络送达",
            "D-074",
            "Owner 单独明确批准",
        ):
            self.assertIn(phrase, document)

    def test_schema_and_metadata_remain_parseable_and_candidate_scoped(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIn("sys001", schema)
        group = schema["sys001"]["items"]["group"]["items"]
        self.assertEqual("", group["natural_decision_provider_id"]["default"])
        self.assertEqual([], group["natural_decision_fallback_provider_ids"]["default"])
        self.assertEqual(8, group["natural_decision_timeout_seconds"]["default"])
        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        fields = dict(
            line.split(":", 1) for line in metadata.splitlines() if ":" in line and not line.startswith(" ")
        )
        self.assertEqual('"==4.27.4"', fields["astrbot_version"].strip())
        self.assertIn("候选", fields["help"])
        self.assertNotIn("发送回执", fields["desc"])

    def test_auxiliary_timeout_descriptions_state_one_purpose_wide_budget(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        group = schema["sys001"]["items"]["group"]["items"]
        review = schema["sys001"]["items"]["final_review"]["items"]
        hints = (
            group["name_semantic_timeout_seconds"]["hint"],
            group["natural_decision_timeout_seconds"]["hint"],
            review["timeout_seconds"]["hint"],
        )
        for hint in hints:
            self.assertIn("共用总等待时间", hint)
            self.assertIn("当前模型", hint)
            self.assertIn("剩余时间", hint)
            self.assertIn("真正耗尽", hint)
            self.assertNotIn("每个", hint)

    def test_retired_delivery_and_private_meme_owners_are_absent(self) -> None:
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

    def test_d089_remaining_bubble_owner_contract_is_unique_and_semantic(self) -> None:
        pitfall = (ROOT / "docs" / "PITFALL_LEDGER.md").read_text(encoding="utf-8")
        review = (ROOT / "docs" / "REVIEW_PLAYBOOK.md").read_text(encoding="utf-8")
        for document in (pitfall, review):
            with self.subTest(document=document[:32], case="approved"):
                self.assertEqual(D089_OWNER, _parse_d089_owner_contract(document))
            with self.subTest(document=document[:32], case="missing"):
                with self.assertRaises(ValueError):
                    _parse_d089_owner_contract(document.replace(D089_BEGIN, "", 1))
                with self.assertRaises(ValueError):
                    _parse_d089_owner_contract(document.replace(D089_RENDER_BEGIN, "", 1))
            with self.subTest(document=document[:32], case="duplicate"):
                with self.assertRaises(ValueError):
                    _parse_d089_owner_contract(document + "\n" + _d089_contract_block(D089_OWNER))
                with self.assertRaises(ValueError):
                    _parse_d089_owner_contract(document + "\n" + _d089_render_block())

            reversed_owner = json.loads(json.dumps(D089_OWNER))
            reversed_owner["astrbot_owner"] = ["adapter", "remaining_bubbles", "official_segmented"]
            reversed_owner["shio_action"] = ["standard_first_send"]
            with self.subTest(document=document[:32], case="owner-reversed"):
                with self.assertRaises(ValueError):
                    _parse_d089_owner_contract(_replace_d089_owner_contract(document, reversed_owner))

            for case_name, plain_prose_conflict in {
                "exact": "所有文本气泡的分段、间隔和发送都只由 AstrBot 执行；Shio 在任何情况下都不会发送文字。",
                "space-after-send": "所有文本气泡的分段、间隔和发送 都只由 AstrBot 执行；Shio 在任何情况下都不会发送文字。",
                "newline-after-send": "所有文本气泡的分段、间隔和发送\n都只由 AstrBot 执行；Shio 在任何情况下都不会发送文字。",
                "tab-after-send": "所有文本气泡的分段、间隔和发送\t都只由 AstrBot 执行；Shio 在任何情况下都不会发送文字。",
                "crlf-after-send": "所有文本气泡的分段、间隔和发送\r\n都只由 AstrBot 执行；Shio 在任何情况下都不会发送文字。",
                "space-before-semicolon": "所有文本气泡的分段、间隔和发送都只由 AstrBot 执行 \uff1b Shio 在任何情况下都不会发送文字。",
                "brand-whitespace": "所有文本气泡的分段、间隔和发送都只由\u3000AstrBot\t执行；Shio 在任何情况下都不会发送文字。",
                "no-terminal": "所有文本气泡的分段、间隔和发送都只由 AstrBot 执行；Shio 在任何情况下都不会发送文字",
                "ascii-terminal": "所有文本气泡的分段、间隔和发送都只由 AstrBot 执行；Shio 在任何情况下都不会发送文字.",
                "fullwidth-terminal": "所有文本气泡的分段、间隔和发送都只由 AstrBot 执行；Shio 在任何情况下都不会发送文字。",
            }.items():
                with self.subTest(document=document[:32], case=f"unmarked-plain-prose-conflict-{case_name}"):
                    with self.assertRaisesRegex(ValueError, "unmarked D-089"):
                        _parse_d089_owner_contract(document + "\n" + plain_prose_conflict)

            for case_name, normal_neighbor in {
                "whitespace": "D-089 继续适用：AstrBot 标准首发后，Shio 在 fixed-safe 条件下等待余段。",
                "newline": "D-089\n正常记录：Shio 不拥有 retry，AstrBot 继续处理 official segmented。",
            }.items():
                with self.subTest(document=document[:32], case=f"ordinary-neighbor-{case_name}"):
                    self.assertEqual(D089_OWNER, _parse_d089_owner_contract(document + "\n" + normal_neighbor))


if __name__ == "__main__":
    unittest.main()
