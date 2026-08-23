from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from astrbot_plugin_shio.scripts.build_p10_candidate import (
    RELEASE_ROOT_FILES,
    build_candidate,
    collect_release_files,
    verify_candidate,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.5.10"
FORBIDDEN_PARTS = {
    ".git",
    ".github",
    ".pytest_cache",
    "__pycache__",
    "docs",
    "scripts",
    "tests",
}


class P10CandidatePackageTests(unittest.TestCase):
    def test_release_file_set_is_closed_and_runtime_only(self):
        files = collect_release_files(PLUGIN_ROOT)
        relative = tuple(path.relative_to(PLUGIN_ROOT).as_posix() for path in files)

        self.assertEqual(tuple(relative[: len(RELEASE_ROOT_FILES)]), RELEASE_ROOT_FILES)
        self.assertEqual(len(relative), len(set(relative)))
        self.assertTrue(all(not (set(Path(item).parts) & FORBIDDEN_PARTS) for item in relative))
        self.assertTrue(all(item in RELEASE_ROOT_FILES or item.startswith(("core/", "assets/personas/")) for item in relative))
        self.assertTrue(all(item.endswith((".py", ".json", ".yaml")) for item in relative))
        self.assertIn("core/contracts/behavior.py", relative)
        self.assertEqual(
            tuple(item for item in relative if item.startswith("assets/personas/")),
            (
                "assets/personas/atri.json",
                "assets/personas/neutral_minimal.json",
                "assets/personas/su_cheng.json",
                "assets/personas/warm_companion.json",
            ),
        )

    def test_two_builds_are_byte_identical_and_manifest_is_content_only(self):
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first_zip = Path(first_dir) / f"astrbot_plugin_shio_v{EXPECTED_VERSION}_upload.zip"
            second_zip = Path(second_dir) / first_zip.name
            first = build_candidate(PLUGIN_ROOT, first_zip)
            second = build_candidate(PLUGIN_ROOT, second_zip)

            self.assertEqual(first_zip.read_bytes(), second_zip.read_bytes())
            self.assertEqual(first, second)
            self.assertEqual(first["package_sha256"], hashlib.sha256(first_zip.read_bytes()).hexdigest())
            self.assertEqual(first["release_version"], EXPECTED_VERSION)
            self.assertNotIn(str(PLUGIN_ROOT), json.dumps(first, ensure_ascii=False))
            self.assertNotIn(first_dir, json.dumps(first, ensure_ascii=False))
            self.assertNotIn(second_dir, json.dumps(second, ensure_ascii=False))

    def test_zip_has_one_top_level_and_manifest_matches_every_entry(self):
        with tempfile.TemporaryDirectory() as output_dir:
            archive = Path(output_dir) / f"astrbot_plugin_shio_v{EXPECTED_VERSION}_upload.zip"
            manifest = build_candidate(PLUGIN_ROOT, archive)
            manifest_path = archive.with_suffix(".manifest.json")

            self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8")), manifest)
            self.assertTrue(verify_candidate(archive, manifest_path)["verified"])
            with zipfile.ZipFile(archive) as package:
                names = tuple(info.filename for info in package.infolist())
                self.assertTrue(names)
                self.assertTrue(all(name.startswith("astrbot_plugin_shio/") for name in names))
                self.assertTrue(all(not name.endswith("/") for name in names))
                self.assertEqual(names, tuple(sorted(names)))
                self.assertEqual(len(names), manifest["file_count"])
                self.assertEqual(sum(len(package.read(name)) for name in names), manifest["uncompressed_bytes"])
                by_path = {entry["path"]: entry for entry in manifest["files"]}
                self.assertEqual(set(by_path), set(names))
                for name in names:
                    payload = package.read(name)
                    self.assertEqual(by_path[name]["bytes"], len(payload))
                    self.assertEqual(by_path[name]["sha256"], hashlib.sha256(payload).hexdigest())

    def test_metadata_version_controls_archive_name(self):
        with tempfile.TemporaryDirectory() as output_dir:
            wrong = Path(output_dir) / "astrbot_plugin_shio_v0.4.6_upload.zip"
            with self.assertRaisesRegex(ValueError, "candidate_archive_name_invalid"):
                build_candidate(PLUGIN_ROOT, wrong)

    def test_verifier_rejects_manifest_or_package_tampering(self):
        with tempfile.TemporaryDirectory() as output_dir:
            archive = Path(output_dir) / f"astrbot_plugin_shio_v{EXPECTED_VERSION}_upload.zip"
            manifest = build_candidate(PLUGIN_ROOT, archive)
            manifest_path = archive.with_suffix(".manifest.json")
            tampered = dict(manifest)
            tampered["package_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "candidate_package_digest_mismatch"):
                verify_candidate(archive, manifest_path)

            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            archive.write_bytes(archive.read_bytes() + b"tampered")
            with self.assertRaisesRegex(ValueError, "candidate_package_size_mismatch"):
                verify_candidate(archive, manifest_path)


if __name__ == "__main__":
    unittest.main()
