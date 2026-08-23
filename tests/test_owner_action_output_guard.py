from __future__ import annotations

import copy
import dataclasses
import hashlib
import math
import unittest
from unittest import mock

from astrbot_plugin_shio.core import owner_action_output_guard as guard


SAFE_ROOT_POSIX = "/srv/shio-owner-artifacts"
SAFE_PATH_POSIX = "/srv/shio-owner-artifacts/notes/result.md"
SAFE_ROOT_WINDOWS = r"D:\ShioOwnerArtifacts"
SAFE_PATH_WINDOWS = r"D:\ShioOwnerArtifacts\notes\result.txt"
ROOT_MARKER = "a" * 64
ROOT_IDENTITY = "b" * 64


def _policy(
    *,
    root: str = SAFE_ROOT_POSIX,
    flavor: guard.ArtifactPathFlavor = guard.ArtifactPathFlavor.POSIX,
    **changes: object,
) -> guard.ArtifactOutputPolicy:
    values: dict[str, object] = {
        "dedicated_root": root,
        "path_flavor": flavor,
        "trusted_root_marker": ROOT_MARKER,
    }
    values.update(changes)
    return guard.ArtifactOutputPolicy(**values)


def _snapshot(
    *,
    root: str = SAFE_ROOT_POSIX,
    path: str = SAFE_PATH_POSIX,
    flavor: guard.ArtifactPathFlavor = guard.ArtifactPathFlavor.POSIX,
    size_bytes: int,
    **changes: object,
) -> guard.ArtifactStatSnapshot:
    values: dict[str, object] = {
        "root_digest": guard.digest_artifact_path(root, flavor),
        "path_digest": guard.digest_artifact_path(path, flavor),
        "root_identity_digest": ROOT_IDENTITY,
        "trusted_root_marker": ROOT_MARKER,
        "device_id": 17,
        "file_id": 23,
        "size_bytes": size_bytes,
        "modified_ns": 1_725_000_000_000_000_000,
        "link_count": 1,
        "regular_file": True,
        "under_root": True,
        "symlink_free": True,
        "reparse_free": True,
        "junction_free": True,
        "mount_escape_free": True,
        "root_exclusive_trusted_writers": True,
        "replacement_protected": True,
    }
    values.update(changes)
    return guard.ArtifactStatSnapshot(**values)


def _proof(
    raw: bytes,
    *,
    root: str = SAFE_ROOT_POSIX,
    path: str = SAFE_PATH_POSIX,
    flavor: guard.ArtifactPathFlavor = guard.ArtifactPathFlavor.POSIX,
    pre_changes: dict[str, object] | None = None,
    post_changes: dict[str, object] | None = None,
) -> guard.ArtifactIdentityProof:
    pre_values: dict[str, object] = {"size_bytes": len(raw)}
    pre_values.update(pre_changes or {})
    post_values: dict[str, object] = {"size_bytes": len(raw)}
    post_values.update(post_changes or {})
    pre = _snapshot(
        root=root,
        path=path,
        flavor=flavor,
        **pre_values,
    )
    post = _snapshot(
        root=root,
        path=path,
        flavor=flavor,
        **post_values,
    )
    return guard.ArtifactIdentityProof(pre=pre, post=post)


def _guard(
    text: str,
    *,
    root: str = SAFE_ROOT_POSIX,
    path: str = SAFE_PATH_POSIX,
    flavor: guard.ArtifactPathFlavor = guard.ArtifactPathFlavor.POSIX,
    policy_changes: dict[str, object] | None = None,
    pre_changes: dict[str, object] | None = None,
    post_changes: dict[str, object] | None = None,
) -> guard.SafeArtifactOutput:
    raw = text.encode("utf-8")
    return guard.guard_artifact_output(
        policy=_policy(root=root, flavor=flavor, **(policy_changes or {})),
        artifact_path=path,
        identity_proof=_proof(
            raw,
            root=root,
            path=path,
            flavor=flavor,
            pre_changes=pre_changes,
            post_changes=post_changes,
        ),
        source_bytes=raw,
    )


class ArtifactOutputPolicyTests(unittest.TestCase):
    def test_policy_is_frozen_slotted_and_defaults_are_bounded(self) -> None:
        policy = _policy()
        self.assertEqual(policy.max_source_bytes, 32_768)
        self.assertEqual(policy.max_visible_characters, 4_096)
        self.assertEqual(policy.allowed_extensions, (".md", ".rst", ".txt"))
        self.assertFalse(hasattr(policy, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            policy.max_source_bytes = 1  # type: ignore[misc]

    def test_policy_rejects_unbounded_or_broad_roots(self) -> None:
        rejected = (
            "",
            "/",
            "/home",
            "/home/example",
            "/workspace",
            "/AstrBot",
            "/AstrBot/data",
            "/AstrBot/data/plugins",
            "/srv/config",
        )
        for root in rejected:
            with self.subTest(root=root), self.assertRaises(guard.ArtifactOutputRejected):
                _policy(root=root)

    def test_policy_requires_explicit_supported_flavor_and_fixed_caps(self) -> None:
        with self.assertRaises(guard.ArtifactOutputRejected):
            guard.ArtifactOutputPolicy(
                dedicated_root=SAFE_ROOT_POSIX,
                path_flavor=None,  # type: ignore[arg-type]
                trusted_root_marker=ROOT_MARKER,
            )
        with self.assertRaises(guard.ArtifactOutputRejected):
            _policy(max_source_bytes=65_536)
        with self.assertRaises(guard.ArtifactOutputRejected):
            _policy(max_visible_characters=8_192)

    def test_extension_allowlist_rejects_tuple_subclass_behavior(self) -> None:
        class EvilExtensions(tuple[str, ...]):
            def __eq__(self, other: object) -> bool:
                return True

            def __contains__(self, item: object) -> bool:
                return True

        with self.assertRaisesRegex(
            guard.ArtifactOutputRejected,
            "^artifact_extension_policy_invalid$",
        ):
            _policy(allowed_extensions=EvilExtensions((".exe",)))

    def test_extension_allowlist_rejects_string_subclass_equality(self) -> None:
        class EvilExtension(str):
            def __eq__(self, other: object) -> bool:
                return True

        with self.assertRaisesRegex(
            guard.ArtifactOutputRejected,
            "^artifact_extension_policy_invalid$",
        ):
            _policy(allowed_extensions=(EvilExtension(".exe"), ".rst", ".txt"))

    def test_policy_rejects_subclasses_for_other_authority_fields(self) -> None:
        class EvilString(str):
            pass

        class EvilInteger(int):
            pass

        invalid_changes = (
            {"root": EvilString(SAFE_ROOT_POSIX)},
            {"trusted_root_marker": EvilString(ROOT_MARKER)},
            {"max_source_bytes": EvilInteger(32_768)},
            {"max_visible_characters": EvilInteger(4_096)},
        )
        for changes in invalid_changes:
            with self.assertRaises(guard.ArtifactOutputRejected):
                _policy(**changes)

    def test_policy_repr_and_trace_do_not_expose_root_or_marker(self) -> None:
        policy = _policy()
        rendered = repr(policy) + repr(policy.trace_metadata())
        self.assertNotIn(SAFE_ROOT_POSIX, rendered)
        self.assertNotIn(ROOT_MARKER, rendered)
        self.assertIn("root_configured", rendered)


class ArtifactPathGateTests(unittest.TestCase):
    def test_posix_and_windows_paths_can_pass_with_matching_proof(self) -> None:
        posix = _guard("亚托莉：读取完成。\r\n第二行。")
        windows = _guard(
            "Windows result\r\n第二行",
            root=SAFE_ROOT_WINDOWS,
            path=SAFE_PATH_WINDOWS,
            flavor=guard.ArtifactPathFlavor.WINDOWS,
        )
        self.assertTrue(posix.is_canonical)
        self.assertTrue(windows.is_canonical)

    def test_path_rejects_traversal_mixed_flavor_and_outside_root(self) -> None:
        bad_paths = (
            "/srv/shio-owner-artifacts/../secret/result.md",
            "/srv/shio-owner-artifacts/notes\\result.md",
            "/srv/other/result.md",
        )
        for path in bad_paths:
            with self.subTest(path=path), self.assertRaises(guard.ArtifactOutputRejected):
                _guard("safe", path=path)

    def test_windows_rejects_unc_device_ads_and_forward_slash(self) -> None:
        bad_paths = (
            r"\\server\share\result.txt",
            r"\\?\D:\ShioOwnerArtifacts\result.txt",
            r"\\.\D:\ShioOwnerArtifacts\result.txt",
            r"D:\ShioOwnerArtifacts\result.txt:secret",
            "D:/ShioOwnerArtifacts/result.txt",
        )
        for path in bad_paths:
            with self.subTest(path=path), self.assertRaises(guard.ArtifactOutputRejected):
                _guard(
                    "safe",
                    root=SAFE_ROOT_WINDOWS,
                    path=path,
                    flavor=guard.ArtifactPathFlavor.WINDOWS,
                )

    def test_windows_rejects_same_components_on_a_different_drive(self) -> None:
        with self.assertRaises(guard.ArtifactOutputRejected):
            _guard(
                "safe",
                root=SAFE_ROOT_WINDOWS,
                path=r"E:\ShioOwnerArtifacts\notes\result.txt",
                flavor=guard.ArtifactPathFlavor.WINDOWS,
            )

    def test_windows_rejects_non_ascii_casefold_expansion_and_invalid_chars(self) -> None:
        bad_paths = (
            r"D:\ShioOwnerArtifacts\straße\result.txt",
            r"D:\ShioOwnerArtifacts\notes\bad?.txt",
            r"D:\ShioOwnerArtifacts\notes\bad|name.txt",
        )
        for path in bad_paths:
            with self.subTest(path=path), self.assertRaises(guard.ArtifactOutputRejected):
                _guard(
                    "safe",
                    root=SAFE_ROOT_WINDOWS,
                    path=path,
                    flavor=guard.ArtifactPathFlavor.WINDOWS,
                )

    def test_rejects_dot_sensitive_components_and_non_allowlisted_extensions(self) -> None:
        bad_paths = (
            "/srv/shio-owner-artifacts/.hidden/result.md",
            "/srv/shio-owner-artifacts/credential/result.md",
            "/srv/shio-owner-artifacts/notes/api_token.md",
            "/srv/shio-owner-artifacts/notes/provider-config.txt",
            "/srv/shio-owner-artifacts/notes/apikey.md",
            "/srv/shio-owner-artifacts/notes/privatekey.md",
            "/srv/shio-owner-artifacts/notes/authToken.md",
            "/srv/shio-owner-artifacts/notes/result.json",
            "/srv/shio-owner-artifacts/notes/result.md.exe",
        )
        for path in bad_paths:
            with self.subTest(path=path), self.assertRaises(guard.ArtifactOutputRejected):
                _guard("safe", path=path)

    def test_root_rejects_compact_astrbot_and_api_key_names(self) -> None:
        for root in (
            "/srv/astrbot-data",
            "/srv/AstrBotData",
            "/srv/AstrBotStore",
        ):
            with self.subTest(root=root), self.assertRaisesRegex(
                guard.ArtifactOutputRejected,
                "^artifact_root_too_broad$",
            ):
                _policy(root=root)
        with self.assertRaises(guard.ArtifactOutputRejected):
            _policy(root="/srv/ApiKeyStore")

    def test_windows_rejects_reserved_or_ambiguous_components(self) -> None:
        bad_paths = (
            r"D:\ShioOwnerArtifacts\CON.txt",
            r"D:\ShioOwnerArtifacts\CON .txt",
            r"D:\ShioOwnerArtifacts\notes.\result.txt",
            "D:\\ShioOwnerArtifacts\\notes \\result.txt",
        )
        for path in bad_paths:
            with self.subTest(path=path), self.assertRaises(guard.ArtifactOutputRejected):
                _guard(
                    "safe",
                    root=SAFE_ROOT_WINDOWS,
                    path=path,
                    flavor=guard.ArtifactPathFlavor.WINDOWS,
                )

    def test_windows_console_device_names_are_reserved(self) -> None:
        names = ("CONIN$.txt", "CONOUT$.md")
        for name in names:
            with self.assertRaisesRegex(
                guard.ArtifactOutputRejected,
                "^artifact_path_reserved$",
            ):
                _guard(
                    "safe",
                    root=SAFE_ROOT_WINDOWS,
                    path=rf"D:\ShioOwnerArtifacts\notes\{name}",
                    flavor=guard.ArtifactPathFlavor.WINDOWS,
                )

    def test_compact_astrbot_target_components_are_sensitive_for_both_flavors(self) -> None:
        targets = (
            (
                SAFE_ROOT_POSIX,
                "/srv/shio-owner-artifacts/AstrBotData/result.md",
                guard.ArtifactPathFlavor.POSIX,
            ),
            (
                SAFE_ROOT_WINDOWS,
                r"D:\ShioOwnerArtifacts\AstrBotStore\result.txt",
                guard.ArtifactPathFlavor.WINDOWS,
            ),
        )
        for root, path, flavor in targets:
            with self.assertRaisesRegex(
                guard.ArtifactOutputRejected,
                "^artifact_path_sensitive$",
            ):
                _guard("safe", root=root, path=path, flavor=flavor)

    def test_digest_is_flavor_canonical_and_hides_no_path_in_output(self) -> None:
        self.assertEqual(
            guard.digest_artifact_path(
                r"D:\SHIOOWNERARTIFACTS\NOTES\RESULT.TXT",
                guard.ArtifactPathFlavor.WINDOWS,
            ),
            guard.digest_artifact_path(SAFE_PATH_WINDOWS, guard.ArtifactPathFlavor.WINDOWS),
        )


class ArtifactIdentityProofTests(unittest.TestCase):
    def test_security_flags_regular_file_and_single_link_are_mandatory(self) -> None:
        mutations = (
            {"regular_file": False},
            {"under_root": False},
            {"symlink_free": False},
            {"reparse_free": False},
            {"junction_free": False},
            {"mount_escape_free": False},
            {"root_exclusive_trusted_writers": False},
            {"replacement_protected": False},
            {"link_count": 2},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(guard.ArtifactOutputRejected):
                _guard("safe", pre_changes=mutation, post_changes=mutation)

    def test_pre_post_identity_changes_fail_closed(self) -> None:
        mutations = (
            {"file_id": 99},
            {"device_id": 99},
            {"modified_ns": 1_725_000_000_000_000_001},
            {"size_bytes": 999},
            {"root_identity_digest": "c" * 64},
            {"trusted_root_marker": "d" * 64},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(guard.ArtifactOutputRejected):
                _guard("safe", post_changes=mutation)

    def test_proof_must_bind_policy_root_path_and_exact_source_size(self) -> None:
        raw = b"safe"
        proof = _proof(raw)
        wrong_path = "/srv/shio-owner-artifacts/notes/other.md"
        with self.assertRaises(guard.ArtifactOutputRejected):
            guard.guard_artifact_output(
                policy=_policy(),
                artifact_path=wrong_path,
                identity_proof=proof,
                source_bytes=raw,
            )
        with self.assertRaises(guard.ArtifactOutputRejected):
            guard.guard_artifact_output(
                policy=_policy(trusted_root_marker="e" * 64),
                artifact_path=SAFE_PATH_POSIX,
                identity_proof=proof,
                source_bytes=raw,
            )

    def test_copied_or_replaced_snapshot_cannot_hide_toctou_or_root_swap(self) -> None:
        raw = b"safe"
        proof = _proof(raw)
        swapped = dataclasses.replace(
            proof,
            post=dataclasses.replace(proof.post, root_identity_digest="f" * 64),
        )
        with self.assertRaises(guard.ArtifactOutputRejected):
            guard.guard_artifact_output(
                policy=_policy(),
                artifact_path=SAFE_PATH_POSIX,
                identity_proof=swapped,
                source_bytes=raw,
            )

    def test_proof_repr_and_trace_expose_no_path_or_identity(self) -> None:
        proof = _proof(b"safe")
        rendered = repr(proof) + repr(proof.trace_metadata())
        for forbidden in (SAFE_ROOT_POSIX, SAFE_PATH_POSIX, ROOT_MARKER, ROOT_IDENTITY):
            self.assertNotIn(forbidden, rendered)


class ArtifactSecretScannerTests(unittest.TestCase):
    def _assert_rejected(self, text: str) -> None:
        with self.assertRaises(guard.ArtifactOutputRejected):
            _guard(text)

    def _assert_secret_detected(self, text: str) -> None:
        with self.assertRaisesRegex(
            guard.ArtifactOutputRejected,
            "^artifact_secret_detected$",
        ):
            _guard(text)

    def test_rejects_invalid_utf8_nul_and_control_characters(self) -> None:
        raw_cases = (b"\xff", b"safe\x00text", b"safe\x01text")
        for raw in raw_cases:
            with self.subTest(raw=raw), self.assertRaises(guard.ArtifactOutputRejected):
                guard.guard_artifact_output(
                    policy=_policy(),
                    artifact_path=SAFE_PATH_POSIX,
                    identity_proof=_proof(raw),
                    source_bytes=raw,
                )

    def test_rejects_private_key_headers_authorization_cookie_and_jwt(self) -> None:
        cases = (
            "-----BEGIN PRIVATE KEY-----\nmaterial\n-----END PRIVATE KEY-----",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
            "Cookie: sid=abcdefghijklmnopqrstuvwxyz0123456789",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlMTIzNDU2",
            '{"Authorization":"Basic ZGVtbzpwYXNz"}',
            "eyJhbGciOiJIUzI1NiJ9.e30.c2lnbmF0dXJl",
        )
        for text in cases:
            with self.subTest(text=text[:20]):
                self._assert_rejected(text)

    def test_extended_private_key_block_headers_are_detected(self) -> None:
        self._assert_secret_detected(
            "-----BEGIN PGP PRIVATE KEY BLOCK-----\nfixture\n-----END PGP PRIVATE KEY BLOCK-----"
        )

    def test_all_secret_detectors_share_one_closed_reason(self) -> None:
        samples = (
            ("pem", "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----"),
            ("header", "Authorization: Basic Zml4dHVyZQ=="),
            ("jwt", "eyJhbGciOiJIUzI1NiJ9.e30.c2lnbmF0dXJl"),
            ("assignment", "VENDOR_API_KEY=fixture-value"),
            ("credential_uri", "scheme://fixture:value@example.invalid/item"),
            ("entropy", "A9k2Lm7Pq4Rs8Tu1Vw5Xy0Za3Bc6De9Fg2Hi"),
        )
        for detector, sample in samples:
            with self.subTest(detector=detector):
                self._assert_secret_detected(sample)

    def test_rejects_common_api_keys_passwords_dsns_and_high_entropy_tokens(self) -> None:
        cases = (
            "api_key = 'sk-live-abcdefghijklmnopqrstuvwxyz012345'",
            '"api_key": "short-secret-value"',
            "password: correct-horse-battery-staple",
            "password=abc",
            "DATABASE_URL=postgresql://user:pass@example.invalid/db",
            "AWS key AKIAABCDEFGHIJKLMNOP",
            "opaque: A9k2Lm7Pq4Rs8Tu1Vw5Xy0Za3Bc6De9Fg2Hi",
        )
        for text in cases:
            with self.subTest(text=text[:20]):
                self._assert_rejected(text)

    def test_structured_secret_keys_support_multiline_and_folding(self) -> None:
        samples = (
            '{\n  "VENDOR_API_KEY"\n  :\n  "fixture-value"\n}',
            "VENDOR_ACCESS_TOKEN=\\\n  fixture-value",
            "VENDOR_API_\\\n  KEY=fixture-value",
            "vendor_client_secret: >-\n  fixture-value",
            "vendor_private_key: |\n  fixture-value",
            '"vendor_password"\n:\n"abc"',
        )
        for sample in samples:
            self._assert_secret_detected(sample)

    def test_structured_secret_keys_decode_json_unicode_escapes(self) -> None:
        samples = (
            '{"vendor_api\\u005fkey":"fixture-value"}',
            '{"vendor_client\\u005fsecret"\n:\n"fixture-value"}',
        )
        for sample in samples:
            self._assert_secret_detected(sample)

    def test_structured_keys_normalize_json_and_yaml_separators(self) -> None:
        samples = (
            '{"vendor_api\\u002fkey\\/token":"fixture-value"}',
            "vendor@api$key: fixture-value",
        )
        for sample in samples:
            self._assert_secret_detected(sample)

    def test_vendor_prefixed_env_json_and_yaml_keys_are_detected(self) -> None:
        samples = (
            "ACME_PRIVATE_KEY=fixture-value",
            '{"cloud_refresh_token":"fixture-value"}',
            "service.credentials.password: abc",
            "providerAuthorization: Basic fixture-value",
        )
        for sample in samples:
            self._assert_secret_detected(sample)

    def test_low_entropy_nonempty_secret_values_are_not_placeholders(self) -> None:
        samples = (
            "vendor_password=example",
            "vendor_api_key=placeholder",
            'vendor_api_key="null"',
        )
        for sample in samples:
            self._assert_secret_detected(sample)

    def test_semantic_null_and_redaction_markers_are_safe(self) -> None:
        samples = (
            "vendor_api_key: null",
            'vendor_api_key: "redacted"',
            "vendor_api_key: ****",
        )
        for sample in samples:
            output = _guard(sample)
            self.assertTrue(output.is_canonical)

    def test_yaml_block_scalar_null_is_nonempty_secret_text(self) -> None:
        self._assert_secret_detected("vendor_api_key: >-\n  null")

    def test_scanner_exception_fails_closed_without_exception_detail(self) -> None:
        raw = b"safe"
        with mock.patch.object(
            guard,
            "_scan_sensitive_text",
            side_effect=RuntimeError("raw detail"),
        ):
            with self.assertRaisesRegex(
                guard.ArtifactOutputRejected,
                "^artifact_secret_scan_failed$",
            ):
                guard.guard_artifact_output(
                    policy=_policy(),
                    artifact_path=SAFE_PATH_POSIX,
                    identity_proof=_proof(raw),
                    source_bytes=raw,
                )

    def test_full_untruncated_source_is_scanned_across_visible_boundary(self) -> None:
        prefix = "a" * 4_090
        text = prefix + "\napi_key=" + "Z8y7X6w5V4u3T2s1R0q9P8o7N6m5L4k3"
        self.assertGreater(len(text), 4_096)
        self._assert_rejected(text)

    def test_unicode_and_crlf_are_preserved_for_safe_output(self) -> None:
        text = "亚托莉读取完成。\r\n这是安全的第二行。🙂\r\n"
        output = _guard(text)
        self.assertEqual(guard._open_safe_artifact_output(output), text)
        self.assertEqual(output.visible_character_count, len(text))

    def test_source_byte_limit_and_visible_truncation_are_separate(self) -> None:
        text = "普通内容。" * 1_000
        output = _guard(text)
        visible = guard._open_safe_artifact_output(output)
        self.assertEqual(len(visible), 4_096)
        self.assertTrue(output.truncated)
        self.assertEqual(output.source_byte_count, len(text.encode("utf-8")))
        oversized = b"a" * 32_769
        with self.assertRaises(guard.ArtifactOutputRejected):
            guard.guard_artifact_output(
                policy=_policy(),
                artifact_path=SAFE_PATH_POSIX,
                identity_proof=_proof(oversized),
                source_bytes=oversized,
            )

    def test_assignment_words_without_values_are_not_false_positives(self) -> None:
        text = "The password policy is documented here.\nAuthorization is a concept."
        output = _guard(text)
        self.assertEqual(guard._open_safe_artifact_output(output), text)

    def test_uuid_reference_is_not_treated_as_a_secret_token(self) -> None:
        text = "request reference: 123e4567-e89b-12d3-a456-426614174000"
        output = _guard(text)
        self.assertEqual(guard._open_safe_artifact_output(output), text)

    def test_uuid_versions_six_through_eight_are_normal_references(self) -> None:
        references = (
            "123e4567-e89b-62d3-a456-426614174000",
            "123e4567-e89b-72d3-a456-426614174000",
            "123e4567-e89b-82d3-a456-426614174000",
        )
        for reference in references:
            text = f"request reference: {reference}"
            output = _guard(text)
            self.assertEqual(guard._open_safe_artifact_output(output), text)


class SafeArtifactOutputTests(unittest.TestCase):
    def test_output_is_module_sealed_and_private_friend_requires_exact_object(self) -> None:
        output = _guard("safe output")
        self.assertTrue(output.is_canonical)
        with self.assertRaises(guard.ArtifactOutputRejected):
            guard.SafeArtifactOutput(
                source_byte_count=11,
                source_character_count=11,
                visible_character_count=11,
                line_count=1,
                content_digest=hashlib.sha256(b"safe output").hexdigest(),
                truncated=False,
            )
        with self.assertRaises((guard.ArtifactOutputRejected, TypeError, ValueError)):
            dataclasses.replace(output, visible_character_count=1)
        copied = copy.copy(output)
        if copied is not output:
            with self.assertRaises(guard.ArtifactOutputRejected):
                guard._open_safe_artifact_output(copied)

    def test_object_setattr_tamper_invalidates_output_without_repr_leak(self) -> None:
        output = _guard("safe output")
        object.__setattr__(output, "content_digest", SAFE_PATH_POSIX)
        self.assertFalse(output.is_canonical)
        self.assertNotIn(SAFE_PATH_POSIX, repr(output))
        with self.assertRaises(guard.ArtifactOutputRejected):
            output.trace_metadata()
        with self.assertRaises(guard.ArtifactOutputRejected):
            guard._open_safe_artifact_output(output)

    def test_repr_trace_and_fields_never_expose_text_path_or_identity(self) -> None:
        text = "uniquely safe sentence"
        output = _guard(text)
        rendered = repr(output) + repr(output.trace_metadata())
        for forbidden in (
            text,
            SAFE_ROOT_POSIX,
            SAFE_PATH_POSIX,
            ROOT_MARKER,
            ROOT_IDENTITY,
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertFalse(hasattr(output, "text"))
        self.assertFalse(hasattr(output, "raw"))
        self.assertFalse(hasattr(output, "path"))
        self.assertFalse(hasattr(output, "__dict__"))

    def test_content_digest_matches_full_source_and_metadata_is_bounded(self) -> None:
        text = "safe output\r\nsecond line"
        output = _guard(text)
        self.assertEqual(output.content_digest, hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertTrue(math.isfinite(float(output.source_byte_count)))
        metadata = output.trace_metadata()
        self.assertEqual(metadata["schema_version"], 1)
        self.assertEqual(metadata["source_byte_count"], len(text.encode("utf-8")))
        self.assertTrue(metadata["canonical"])


if __name__ == "__main__":
    unittest.main()
