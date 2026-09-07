"""Independent output-level round check; it does not invoke the candidate builder."""
from __future__ import annotations

import json
from pathlib import Path
import unittest


class R29CandidateRoundIdentityTests(unittest.TestCase):
    def test_r29_manifest_title_and_union_are_round_specific(self):
        root = Path(__file__).resolve().parents[2]
        manifest = root / "deploy" / "SYS-001" / "CANDIDATE_MANIFEST_R29.md"
        if not manifest.is_file():
            self.skipTest("R29 artifact is generated only after product self-tests freeze the candidate")
        text = manifest.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# SYS-001 R29 candidate\n"))
        data = json.loads(text.split("```json", 1)[1].split("```", 1)[0])
        self.assertEqual(12, len(data["runtime_members"]))
        self.assertEqual(26, len(data["test_paths"]))
