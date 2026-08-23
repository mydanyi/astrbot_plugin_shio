"""Fail-closed artifact path, identity and output guard for owner actions.

This module is deliberately not a filesystem collector and does not execute a
runtime tool.  A future live executor must obtain two typed stat snapshots around
the read and supply the complete, untruncated bytes.  The guard binds those facts
to one dedicated root and path, scans the complete source, and only then creates a
module-sealed, bounded presentation object.

No public object contains the safe text.  The future executor bridge may use the
private ``_open_safe_artifact_output`` friend after it has verified canonical
identity.  Repr and trace metadata contain only bounded counts and a digest.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import unicodedata
import weakref
from dataclasses import InitVar, dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from .owner_action_adapters import ArtifactPathFlavor


MAX_ARTIFACT_SOURCE_BYTES = 32_768
MAX_ARTIFACT_VISIBLE_CHARACTERS = 4_096
ALLOWED_ARTIFACT_EXTENSIONS = (".md", ".rst", ".txt")

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,95}$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:\\")
_WINDOWS_FORBIDDEN_CHARACTER_RE = re.compile(r"[<>\"|?*]")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CLOCK$", "CON", "CONIN$", "CONOUT$", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_SENSITIVE_COMPONENT_WORDS = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "key",
        "keys",
        "password",
        "passwd",
        "provider",
        "providers",
        "secret",
        "secrets",
        "session",
        "sessions",
        "token",
        "tokens",
    }
)
_SENSITIVE_COMPACT_COMPONENTS = frozenset(
    {
        "accesskey",
        "accesstoken",
        "apikey",
        "authtoken",
        "clientsecret",
        "connectionstring",
        "databaseurl",
        "privatekey",
        "secretkey",
        "sessionid",
    }
)

_PEM_PRIVATE_RE = re.compile(
    r"-----BEGIN[ \t]+(?:[A-Z0-9]+[ \t]+)*PRIVATE[ \t]+KEY"
    r"(?:[ \t]+[A-Z0-9]+)*[ \t]*-----",
    re.IGNORECASE,
)
_SENSITIVE_HEADER_RE = re.compile(
    r"(?im)^[ \t]*(?:proxy-)?authorization[ \t]*:[ \t]*\S+"
    r"|^[ \t]*(?:set-)?cookie[ \t]*:[ \t]*\S+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]{16,}")
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{2,}\."
    r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_KNOWN_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|"
    r"github_pat_[0-9A-Za-z_]{20,}|gh[pousr]_[0-9A-Za-z]{20,}|"
    r"xox[baprs]-[0-9A-Za-z-]{20,}|(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}|"
    r"sk-(?:live-)?[0-9A-Za-z_-]{20,}"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_STRUCTURED_QUOTED_ASSIGNMENT_RE = re.compile(
    r"(?imx)(?:^|[\r\n{\[(,;])[ \t]*(?:[-+*][ \t]+)?(?:\$env:)?"
    r"(?:\"(?P<double_key>[^\"\r\n]*?)\"|'(?P<single_key>[^'\r\n]*?)')"
    r"[ \t\r\n]*(?P<separator>[:=])"
)
_STRUCTURED_UNQUOTED_ASSIGNMENT_RE = re.compile(
    r"(?imx)(?:^|[\r\n{\[(,;])[ \t]*(?:[-+*][ \t]+)?(?:\$env:)?"
    r"(?P<key>[A-Za-z0-9_][^:=\r\n{}\[\],;'\"]*?)"
    r"[ \t]*(?P<separator>[:=])"
)
_JSON_SURROGATE_PAIR_RE = re.compile(
    r"\\u(?P<high>[dD][89aAbB][0-9a-fA-F]{2})"
    r"\\u(?P<low>[dD][c-fC-F][0-9a-fA-F]{2})"
)
_JSON_UNICODE_ESCAPE_RE = re.compile(r"\\u(?P<unit>[0-9a-fA-F]{4})")
_JSON_STANDARD_ESCAPE_RE = re.compile(r"\\(?P<escape>[\"\\/bfnrt])")
_EXPLICIT_LINE_CONTINUATION_RE = re.compile(r"\\[ \t]*(?:\r\n|\n|\r)[ \t]*")
_SENSITIVE_ASSIGNMENT_KEY_MARKERS = (
    "accesskey",
    "accesstoken",
    "apikey",
    "authorization",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "connectionstring",
    "cookie",
    "credential",
    "databaseurl",
    "dsn",
    "password",
    "passwd",
    "privatekey",
    "pwd",
    "refreshtoken",
    "secret",
    "secretaccesskey",
    "secretkey",
    "session",
    "sessionid",
    "sessionkey",
    "sessionsecret",
    "sessiontoken",
    "token",
)
_SAFE_PLACEHOLDER_VALUES = frozenset(
    {
        "masked",
        "nil",
        "none",
        "null",
        "redacted",
        "removed",
        "unset",
        "~",
    }
)
_SAFE_QUOTED_PLACEHOLDER_VALUES = frozenset({"masked", "redacted", "removed"})
_CREDENTIAL_URI_RE = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@]{1,128}:[^\s/@]{1,128}@"
)
_TOKEN_CANDIDATE_RE = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9+/=_-])")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


class ArtifactOutputRejected(ValueError):
    """Closed rejection whose message never includes paths or source content."""

    def __init__(self, reason_code: str) -> None:
        normalized = str(reason_code or "").strip().casefold()
        if not _SAFE_REASON_RE.fullmatch(normalized):
            normalized = "artifact_output_rejected"
        self.reason_code = normalized
        super().__init__(normalized)


def _require_bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ArtifactOutputRejected(f"{name}_invalid")
    return value


def _require_nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ArtifactOutputRejected(f"{name}_invalid")
    return value


def _require_digest(value: Any, name: str) -> str:
    if type(value) is not str or not _HEX64_RE.fullmatch(value):
        raise ArtifactOutputRejected(f"{name}_invalid")
    return value


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reject_unsafe_unicode(value: str, reason: str) -> None:
    if not value or unicodedata.normalize("NFKC", value) != value:
        raise ArtifactOutputRejected(reason)
    for character in value:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs"}:
            raise ArtifactOutputRejected(reason)


def _split_component_words(component: str) -> tuple[str, ...]:
    return tuple(
        part
        for part in re.split(r"[^a-z0-9]+", unicodedata.normalize("NFKC", component).casefold())
        if part
    )


def _component_is_sensitive(component: str) -> bool:
    normalized = unicodedata.normalize("NFKC", component).casefold().rstrip(" .")
    if not normalized or normalized.startswith("."):
        return True
    base = normalized.rsplit(".", 1)[0]
    words = _split_component_words(base)
    if any(word in _SENSITIVE_COMPONENT_WORDS for word in words):
        return True
    compact = "".join(words)
    if "astrbot" in compact:
        return True
    if any(marker in compact for marker in _SENSITIVE_COMPACT_COMPONENTS):
        return True
    return compact.startswith(("config", "credential", "provider"))


def _validate_posix_path(value: str) -> tuple[str, tuple[str, ...]]:
    _reject_unsafe_unicode(value, "artifact_path_invalid")
    if "\\" in value or not value.startswith("/") or value.startswith("//"):
        raise ArtifactOutputRejected("artifact_path_flavor_mismatch")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts[1:]):
        raise ArtifactOutputRejected("artifact_path_ambiguous")
    pure = PurePosixPath(value)
    if not pure.is_absolute():
        raise ArtifactOutputRejected("artifact_path_not_absolute")
    components = tuple(pure.parts[1:])
    if not components:
        raise ArtifactOutputRejected("artifact_path_too_broad")
    return str(pure), components


def _validate_windows_path(value: str) -> tuple[str, tuple[str, ...]]:
    _reject_unsafe_unicode(value, "artifact_path_invalid")
    if any(ord(character) > 0x7F for character in value):
        raise ArtifactOutputRejected("artifact_path_windows_unicode")
    if "/" in value:
        raise ArtifactOutputRejected("artifact_path_flavor_mismatch")
    if _WINDOWS_FORBIDDEN_CHARACTER_RE.search(value):
        raise ArtifactOutputRejected("artifact_path_forbidden_character")
    lowered = value.casefold()
    if (
        value.startswith("\\")
        or lowered.startswith("\\?\\")
        or lowered.startswith("\\.\\")
        or lowered.startswith(r"\??" + "\\")
        or "globalroot" in lowered
        or not _WINDOWS_ABSOLUTE_RE.match(value)
    ):
        raise ArtifactOutputRejected("artifact_path_windows_namespace")
    if ":" in value[2:]:
        raise ArtifactOutputRejected("artifact_path_ads")
    tail = value[3:]
    raw_parts = tail.split("\\") if tail else ()
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise ArtifactOutputRejected("artifact_path_ambiguous")
    for component in raw_parts:
        if component.endswith((" ", ".")):
            raise ArtifactOutputRejected("artifact_path_ambiguous")
        stem = component.split(".", 1)[0].rstrip(" ").upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise ArtifactOutputRejected("artifact_path_reserved")
    pure = PureWindowsPath(value)
    if not pure.is_absolute() or not pure.drive:
        raise ArtifactOutputRejected("artifact_path_not_absolute")
    normalized = str(pure)
    components = tuple(pure.parts[1:])
    if not components:
        raise ArtifactOutputRejected("artifact_path_too_broad")
    return normalized, components


def _normalize_artifact_path(
    value: str,
    flavor: ArtifactPathFlavor,
) -> tuple[str, tuple[str, ...]]:
    if type(value) is not str:
        raise ArtifactOutputRejected("artifact_path_invalid")
    if flavor is ArtifactPathFlavor.POSIX:
        return _validate_posix_path(value)
    if flavor is ArtifactPathFlavor.WINDOWS:
        return _validate_windows_path(value)
    raise ArtifactOutputRejected("artifact_path_flavor_unsupported")


def _digest_normalized_path(normalized: str, flavor: ArtifactPathFlavor) -> str:
    if flavor is ArtifactPathFlavor.WINDOWS:
        normalized = normalized.lower()
    return _digest_text(normalized)


def digest_artifact_path(value: str, flavor: ArtifactPathFlavor) -> str:
    """Return the flavor-canonical path digest used by typed stat snapshots."""

    if not isinstance(flavor, ArtifactPathFlavor):
        raise ArtifactOutputRejected("artifact_path_flavor_invalid")
    normalized, _ = _normalize_artifact_path(value, flavor)
    return _digest_normalized_path(normalized, flavor)


def _root_is_too_broad(
    normalized: str,
    components: tuple[str, ...],
    flavor: ArtifactPathFlavor,
) -> bool:
    folded = tuple(part.lower() for part in components)
    root_words = tuple(word for part in components for word in _split_component_words(part))
    root_compacts = tuple("".join(_split_component_words(part)) for part in components)
    if flavor is ArtifactPathFlavor.POSIX:
        if normalized == "/":
            return True
        if folded == ("home",) or (folded and folded[0] == "home" and len(folded) <= 2):
            return True
        if folded == ("root",):
            return True
    else:
        if len(components) == 0:
            return True
        if folded and folded[0] == "users" and len(folded) <= 2:
            return True
    if folded and folded[-1] in {
        "etc",
        "home",
        "media",
        "mnt",
        "opt",
        "program files",
        "programdata",
        "srv",
        "temp",
        "tmp",
        "usr",
        "var",
        "windows",
        "workspace",
        "workspaces",
        "astrbot",
        "data",
        "plugin",
        "plugins",
        "config",
    }:
        return True
    if (
        "astrbot" in root_words
        or any("astrbot" in compact for compact in root_compacts)
        or any(part in {"plugin", "plugins"} for part in folded)
    ):
        return True
    return False


def _validate_root(
    value: str,
    flavor: ArtifactPathFlavor,
) -> tuple[str, tuple[str, ...]]:
    normalized, components = _normalize_artifact_path(value, flavor)
    if _root_is_too_broad(normalized, components, flavor):
        raise ArtifactOutputRejected("artifact_root_too_broad")
    if any(_component_is_sensitive(component) for component in components):
        raise ArtifactOutputRejected("artifact_root_sensitive")
    return normalized, components


@dataclass(frozen=True, slots=True, repr=False)
class ArtifactOutputPolicy:
    """Frozen deployment policy for one explicit, dedicated artifact root."""

    dedicated_root: str = field(repr=False)
    path_flavor: ArtifactPathFlavor
    trusted_root_marker: str = field(repr=False)
    max_source_bytes: int = MAX_ARTIFACT_SOURCE_BYTES
    max_visible_characters: int = MAX_ARTIFACT_VISIBLE_CHARACTERS
    allowed_extensions: tuple[str, ...] = ALLOWED_ARTIFACT_EXTENSIONS

    def __post_init__(self) -> None:
        if type(self.path_flavor) is not ArtifactPathFlavor or self.path_flavor not in {
            ArtifactPathFlavor.POSIX,
            ArtifactPathFlavor.WINDOWS,
        }:
            raise ArtifactOutputRejected("artifact_path_flavor_unsupported")
        _validate_root(self.dedicated_root, self.path_flavor)
        _require_digest(self.trusted_root_marker, "trusted_root_marker")
        if (
            type(self.max_source_bytes) is not int
            or not 1 <= self.max_source_bytes <= MAX_ARTIFACT_SOURCE_BYTES
        ):
            raise ArtifactOutputRejected("max_source_bytes_invalid")
        if (
            type(self.max_visible_characters) is not int
            or not 1 <= self.max_visible_characters <= MAX_ARTIFACT_VISIBLE_CHARACTERS
        ):
            raise ArtifactOutputRejected("max_visible_characters_invalid")
        if (
            type(self.allowed_extensions) is not tuple
            or len(self.allowed_extensions) != len(ALLOWED_ARTIFACT_EXTENSIONS)
            or any(type(item) is not str for item in self.allowed_extensions)
            or self.allowed_extensions != ALLOWED_ARTIFACT_EXTENSIONS
        ):
            raise ArtifactOutputRejected("artifact_extension_policy_invalid")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        self.__post_init__()
        return {
            "schema_version": 1,
            "path_flavor": self.path_flavor.value,
            "root_configured": True,
            "trusted_root_marker_bound": True,
            "max_source_bytes": self.max_source_bytes,
            "max_visible_characters": self.max_visible_characters,
            "allowed_extension_count": len(self.allowed_extensions),
        }

    def __repr__(self) -> str:
        try:
            self.__post_init__()
        except Exception:
            return (
                "ArtifactOutputPolicy(valid=False, root_hidden=True, "
                "trusted_root_marker_hidden=True)"
            )
        return (
            "ArtifactOutputPolicy("
            f"path_flavor={self.path_flavor.value!r}, root_configured=True, "
            "trusted_root_marker_bound=True, "
            f"max_source_bytes={self.max_source_bytes}, "
            f"max_visible_characters={self.max_visible_characters}, "
            f"allowed_extension_count={len(self.allowed_extensions)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ArtifactStatSnapshot:
    """Typed pre/post lstat evidence; contains digests, never a raw path."""

    root_digest: str = field(repr=False)
    path_digest: str = field(repr=False)
    root_identity_digest: str = field(repr=False)
    trusted_root_marker: str = field(repr=False)
    device_id: int = field(repr=False)
    file_id: int = field(repr=False)
    size_bytes: int
    modified_ns: int = field(repr=False)
    link_count: int
    regular_file: bool
    under_root: bool
    symlink_free: bool
    reparse_free: bool
    junction_free: bool
    mount_escape_free: bool
    root_exclusive_trusted_writers: bool
    replacement_protected: bool

    def __post_init__(self) -> None:
        for name in (
            "root_digest",
            "path_digest",
            "root_identity_digest",
            "trusted_root_marker",
        ):
            _require_digest(getattr(self, name), name)
        for name in (
            "device_id",
            "file_id",
            "size_bytes",
            "modified_ns",
            "link_count",
        ):
            _require_nonnegative_int(getattr(self, name), name)
        for name in (
            "regular_file",
            "under_root",
            "symlink_free",
            "reparse_free",
            "junction_free",
            "mount_escape_free",
            "root_exclusive_trusted_writers",
            "replacement_protected",
        ):
            _require_bool(getattr(self, name), name)

    def trace_metadata(self) -> dict[str, int | bool]:
        self.__post_init__()
        return {
            "schema_version": 1,
            "size_bytes": self.size_bytes,
            "link_count": self.link_count,
            "regular_file": self.regular_file,
            "under_root": self.under_root,
            "symlink_free": self.symlink_free,
            "reparse_free": self.reparse_free,
            "junction_free": self.junction_free,
            "mount_escape_free": self.mount_escape_free,
            "trusted_root_marker_bound": True,
            "root_identity_bound": True,
        }

    def __repr__(self) -> str:
        try:
            self.__post_init__()
        except Exception:
            return (
                "ArtifactStatSnapshot(valid=False, identity_hidden=True, "
                "path_hidden=True)"
            )
        return (
            "ArtifactStatSnapshot("
            f"size_bytes={self.size_bytes}, link_count={self.link_count}, "
            f"regular_file={self.regular_file}, under_root={self.under_root}, "
            f"symlink_free={self.symlink_free}, reparse_free={self.reparse_free}, "
            f"junction_free={self.junction_free}, "
            f"mount_escape_free={self.mount_escape_free}, "
            "identity_hidden=True, path_hidden=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ArtifactIdentityProof:
    """Two snapshots binding one read against replacement and root swaps."""

    pre: ArtifactStatSnapshot = field(repr=False)
    post: ArtifactStatSnapshot = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.pre) is not ArtifactStatSnapshot
            or type(self.post) is not ArtifactStatSnapshot
        ):
            raise ArtifactOutputRejected("artifact_stat_snapshot_invalid")
        self.pre.__post_init__()
        self.post.__post_init__()

    def trace_metadata(self) -> dict[str, int | bool]:
        self.__post_init__()
        return {
            "schema_version": 1,
            "pre_post_equal": self.pre == self.post,
            "source_size_bytes": self.post.size_bytes,
            "single_link": self.pre.link_count == self.post.link_count == 1,
            "path_digest_bound": True,
            "root_digest_bound": True,
            "root_identity_bound": True,
            "trusted_root_marker_bound": True,
        }

    def __repr__(self) -> str:
        try:
            self.__post_init__()
        except Exception:
            return (
                "ArtifactIdentityProof(valid=False, identity_hidden=True, "
                "path_hidden=True)"
            )
        return (
            "ArtifactIdentityProof("
            f"pre_post_equal={self.pre == self.post}, "
            f"source_size_bytes={self.post.size_bytes}, "
            "path_hidden=True, identity_hidden=True)"
        )


def _path_is_under_root(
    root_components: tuple[str, ...],
    target_components: tuple[str, ...],
    flavor: ArtifactPathFlavor,
) -> bool:
    if len(target_components) <= len(root_components):
        return False
    if flavor is ArtifactPathFlavor.WINDOWS:
        root_components = tuple(part.lower() for part in root_components)
        target_components = tuple(part.lower() for part in target_components)
    return target_components[: len(root_components)] == root_components


def _validate_target_path(
    policy: ArtifactOutputPolicy,
    artifact_path: str,
) -> tuple[str, str]:
    root_normalized, root_components = _validate_root(
        policy.dedicated_root,
        policy.path_flavor,
    )
    target_normalized, target_components = _normalize_artifact_path(
        artifact_path,
        policy.path_flavor,
    )
    if policy.path_flavor is ArtifactPathFlavor.WINDOWS and (
        PureWindowsPath(root_normalized).drive.lower()
        != PureWindowsPath(target_normalized).drive.lower()
    ):
        raise ArtifactOutputRejected("artifact_path_outside_root")
    if not _path_is_under_root(root_components, target_components, policy.path_flavor):
        raise ArtifactOutputRejected("artifact_path_outside_root")
    relative_components = target_components[len(root_components) :]
    if any(_component_is_sensitive(component) for component in relative_components):
        raise ArtifactOutputRejected("artifact_path_sensitive")
    suffix = (
        PureWindowsPath(target_normalized).suffix
        if policy.path_flavor is ArtifactPathFlavor.WINDOWS
        else PurePosixPath(target_normalized).suffix
    ).casefold()
    if suffix not in policy.allowed_extensions:
        raise ArtifactOutputRejected("artifact_extension_forbidden")
    return (
        _digest_normalized_path(root_normalized, policy.path_flavor),
        _digest_normalized_path(target_normalized, policy.path_flavor),
    )


def _validate_identity_proof(
    *,
    policy: ArtifactOutputPolicy,
    identity_proof: ArtifactIdentityProof,
    root_digest: str,
    path_digest: str,
    source_size: int,
) -> None:
    if type(identity_proof) is not ArtifactIdentityProof:
        raise ArtifactOutputRejected("artifact_identity_proof_invalid")
    pre = identity_proof.pre
    post = identity_proof.post
    if type(pre) is not ArtifactStatSnapshot or type(post) is not ArtifactStatSnapshot:
        raise ArtifactOutputRejected("artifact_stat_snapshot_invalid")
    pre.__post_init__()
    post.__post_init__()
    if pre != post:
        raise ArtifactOutputRejected("artifact_identity_changed")
    if pre.root_digest != root_digest or pre.path_digest != path_digest:
        raise ArtifactOutputRejected("artifact_path_binding_mismatch")
    if pre.trusted_root_marker != policy.trusted_root_marker:
        raise ArtifactOutputRejected("artifact_root_marker_mismatch")
    if pre.size_bytes != source_size:
        raise ArtifactOutputRejected("artifact_source_size_mismatch")
    if pre.link_count != 1:
        raise ArtifactOutputRejected("artifact_link_count_forbidden")
    required_flags = (
        pre.regular_file,
        pre.under_root,
        pre.symlink_free,
        pre.reparse_free,
        pre.junction_free,
        pre.mount_escape_free,
        pre.root_exclusive_trusted_writers,
        pre.replacement_protected,
    )
    if not all(required_flags):
        raise ArtifactOutputRejected("artifact_identity_unsafe")


def _shannon_entropy(candidate: str) -> float:
    if not candidate:
        return 0.0
    frequencies = {character: candidate.count(character) for character in set(candidate)}
    length = len(candidate)
    return -sum(
        (count / length) * math.log2(count / length)
        for count in frequencies.values()
    )


def _looks_like_high_entropy_token(candidate: str) -> bool:
    stripped = candidate.rstrip("=")
    if _UUID_RE.fullmatch(stripped):
        return False
    if len(stripped) < 32 or len(set(stripped)) < 12:
        return False
    classes = sum(
        (
            any(char.islower() for char in stripped),
            any(char.isupper() for char in stripped),
            any(char.isdigit() for char in stripped),
            any(char in "+/_-" for char in stripped),
        )
    )
    if classes < 3 and not (
        len(stripped) >= 40 and re.fullmatch(r"[0-9a-fA-F]+", stripped)
    ):
        return False
    return _shannon_entropy(stripped) >= 3.5


def _decode_json_unicode_escapes(text: str) -> str:
    def replace_pair(match: re.Match[str]) -> str:
        high = int(match.group("high"), 16)
        low = int(match.group("low"), 16)
        codepoint = 0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)
        return chr(codepoint)

    def replace_unit(match: re.Match[str]) -> str:
        unit = int(match.group("unit"), 16)
        if 0xD800 <= unit <= 0xDFFF:
            raise ValueError("unpaired_json_surrogate")
        character = chr(unit)
        category = unicodedata.category(character)
        if category == "Cs":
            raise ValueError("unpaired_json_surrogate")
        if category in {"Cc", "Cf", "Zl", "Zp"}:
            return " "
        return character

    paired = _JSON_SURROGATE_PAIR_RE.sub(replace_pair, text)
    return _JSON_UNICODE_ESCAPE_RE.sub(replace_unit, paired)


def _decode_json_standard_escapes(text: str) -> str:
    def replace_escape(match: re.Match[str]) -> str:
        return "/" if match.group("escape") == "/" else " "

    return _JSON_STANDARD_ESCAPE_RE.sub(replace_escape, text)


def _normalize_secret_scan_text(text: str) -> str:
    decoded = _decode_json_unicode_escapes(text)
    standard_decoded = _decode_json_standard_escapes(decoded)
    continued = _EXPLICIT_LINE_CONTINUATION_RE.sub("", standard_decoded)
    return unicodedata.normalize("NFKC", continued)


def _structured_key_is_sensitive(key: str) -> bool:
    compact = "".join(
        character
        for character in unicodedata.normalize("NFKC", key).casefold()
        if character.isascii() and character.isalnum()
    )
    if not compact:
        return False
    return any(marker in compact for marker in _SENSITIVE_ASSIGNMENT_KEY_MARKERS)


def _extract_structured_value(tail: str) -> tuple[str, bool]:
    remaining = tail.lstrip(" \t\r\n")
    literal_scalar = False
    if remaining.startswith((">-", ">+", "|-", "|+")):
        literal_scalar = True
        remaining = remaining[2:].lstrip(" \t\r\n")
    elif remaining.startswith((">", "|")):
        literal_scalar = True
        remaining = remaining[1:].lstrip(" \t\r\n")
    while remaining.startswith("#"):
        _, separator, remaining = remaining.partition("\n")
        if not separator:
            return "", literal_scalar
        remaining = remaining.lstrip(" \t\r\n")
    if not remaining or remaining[0] in "}],;" or remaining.startswith("#"):
        return "", literal_scalar
    if remaining[0] in {'"', "'"}:
        quote = remaining[0]
        escaped = False
        value: list[str] = []
        for character in remaining[1:]:
            if escaped:
                value.append(character)
                escaped = False
                continue
            if character == "\\":
                escaped = True
                continue
            if character == quote:
                break
            value.append(character)
        return "".join(value).strip(), True
    first_line = remaining.splitlines()[0]
    return (
        re.split(r"[,;}#]", first_line, maxsplit=1)[0].strip(),
        literal_scalar,
    )


def _structured_value_is_sensitive(value: str, *, quoted: bool) -> bool:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not normalized:
        return False
    placeholder = normalized.strip(" \t\r\n'\"<>[]{}()")
    safe_values = (
        _SAFE_QUOTED_PLACEHOLDER_VALUES
        if quoted
        else _SAFE_PLACEHOLDER_VALUES
    )
    if placeholder in safe_values:
        return False
    if placeholder and all(character in {"*", "x", "-", "_"} for character in placeholder):
        return False
    return True


def _contains_structured_secret(text: str) -> bool:
    matches: list[tuple[re.Match[str], str]] = []
    matches.extend(
        (match, match.group("double_key") or match.group("single_key") or "")
        for match in _STRUCTURED_QUOTED_ASSIGNMENT_RE.finditer(text)
    )
    matches.extend(
        (match, match.group("key"))
        for match in _STRUCTURED_UNQUOTED_ASSIGNMENT_RE.finditer(text)
    )
    for match, key in matches:
        if not _structured_key_is_sensitive(key):
            continue
        value, quoted = _extract_structured_value(text[match.end() :])
        if _structured_value_is_sensitive(value, quoted=quoted):
            return True
    return False


def _scan_sensitive_text(text: str) -> bool:
    normalized = _normalize_secret_scan_text(text)
    if (
        _PEM_PRIVATE_RE.search(normalized)
        or _SENSITIVE_HEADER_RE.search(normalized)
        or _BEARER_RE.search(normalized)
        or _JWT_RE.search(normalized)
        or _KNOWN_KEY_RE.search(normalized)
        or _contains_structured_secret(normalized)
        or _CREDENTIAL_URI_RE.search(normalized)
    ):
        return True
    return any(
        _looks_like_high_entropy_token(match.group(0))
        for match in _TOKEN_CANDIDATE_RE.finditer(normalized)
    )


def _decode_and_scan_source(source_bytes: bytes) -> str:
    try:
        text = source_bytes.decode("utf-8", errors="strict")
    except (UnicodeDecodeError, UnicodeError):
        raise ArtifactOutputRejected("artifact_source_not_utf8") from None
    for character in text:
        category = unicodedata.category(character)
        if character in "\t\r\n":
            continue
        if category in {"Cc", "Cf", "Cs"}:
            raise ArtifactOutputRejected("artifact_source_control_character")
    try:
        sensitive = _scan_sensitive_text(text)
    except Exception:
        raise ArtifactOutputRejected("artifact_secret_scan_failed") from None
    if sensitive:
        raise ArtifactOutputRejected("artifact_secret_detected")
    return text


@dataclass(frozen=True, slots=True, repr=False)
class _SafeArtifactMaterial:
    visible_text: str = field(repr=False)
    path_binding_digest: str = field(repr=False)
    source_byte_count: int
    source_character_count: int
    visible_character_count: int
    line_count: int
    content_digest: str = field(repr=False)
    truncated: bool

    def __repr__(self) -> str:
        return "_SafeArtifactMaterial(metadata_bound=True, text_hidden=True, path_hidden=True)"


_SAFE_OUTPUT_LOCK = threading.RLock()
_PENDING_OUTPUT_SEALS: set[object] = set()
_CANONICAL_OUTPUTS: weakref.WeakValueDictionary[int, "SafeArtifactOutput"] = (
    weakref.WeakValueDictionary()
)
_SAFE_OUTPUT_MATERIALS: weakref.WeakKeyDictionary[
    "SafeArtifactOutput",
    _SafeArtifactMaterial,
] = weakref.WeakKeyDictionary()
_CANONICAL_OUTPUT_MARKER = object()


@dataclass(frozen=True, slots=True, repr=False, eq=False, weakref_slot=True)
class SafeArtifactOutput:
    """Canonical bounded output.  Source text remains in module-private storage."""

    source_byte_count: int
    source_character_count: int
    visible_character_count: int
    line_count: int
    content_digest: str = field(repr=False)
    truncated: bool
    _issuer_seal: InitVar[object | None] = None
    _canonical_marker: object = field(init=False, repr=False, compare=False)

    def __post_init__(self, _issuer_seal: object | None) -> None:
        with _SAFE_OUTPUT_LOCK:
            if _issuer_seal is None or _issuer_seal not in _PENDING_OUTPUT_SEALS:
                raise ArtifactOutputRejected("safe_output_issuer_seal_invalid")
            _PENDING_OUTPUT_SEALS.remove(_issuer_seal)
        object.__setattr__(self, "_canonical_marker", _CANONICAL_OUTPUT_MARKER)
        for name in (
            "source_byte_count",
            "source_character_count",
            "visible_character_count",
            "line_count",
        ):
            _require_nonnegative_int(getattr(self, name), name)
        _require_digest(self.content_digest, "content_digest")
        _require_bool(self.truncated, "truncated")
        if self.source_byte_count > MAX_ARTIFACT_SOURCE_BYTES:
            raise ArtifactOutputRejected("safe_output_source_oversized")
        if self.visible_character_count > MAX_ARTIFACT_VISIBLE_CHARACTERS:
            raise ArtifactOutputRejected("safe_output_visible_oversized")
        if self.visible_character_count > self.source_character_count:
            raise ArtifactOutputRejected("safe_output_count_invalid")
        if self.truncated != (
            self.visible_character_count < self.source_character_count
        ):
            raise ArtifactOutputRejected("safe_output_truncation_invalid")

    @property
    def is_canonical(self) -> bool:
        with _SAFE_OUTPUT_LOCK:
            material = _SAFE_OUTPUT_MATERIALS.get(self)
            try:
                marker = self._canonical_marker
            except Exception:
                return False
            return (
                marker is _CANONICAL_OUTPUT_MARKER
                and _CANONICAL_OUTPUTS.get(id(self)) is self
                and material is not None
                and _safe_output_matches_material(self, material)
            )

    def trace_metadata(self) -> dict[str, str | int | bool]:
        with _SAFE_OUTPUT_LOCK:
            material = _SAFE_OUTPUT_MATERIALS.get(self)
            try:
                marker = self._canonical_marker
            except Exception:
                raise ArtifactOutputRejected("safe_output_not_canonical") from None
            if (
                marker is not _CANONICAL_OUTPUT_MARKER
                or _CANONICAL_OUTPUTS.get(id(self)) is not self
                or material is None
                or not _safe_output_matches_material(self, material)
            ):
                raise ArtifactOutputRejected("safe_output_not_canonical")
            return {
                "schema_version": 1,
                "source_byte_count": material.source_byte_count,
                "source_character_count": material.source_character_count,
                "visible_character_count": material.visible_character_count,
                "line_count": material.line_count,
                "content_digest": material.content_digest,
                "truncated": material.truncated,
                "canonical": True,
            }

    def __copy__(self) -> "SafeArtifactOutput":
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> "SafeArtifactOutput":
        memo[id(self)] = self
        return self

    def __repr__(self) -> str:
        try:
            metadata = self.trace_metadata()
        except ArtifactOutputRejected:
            return (
                "SafeArtifactOutput(canonical=False, metadata_hidden=True, "
                "text_hidden=True, path_hidden=True)"
            )
        return (
            "SafeArtifactOutput("
            f"source_byte_count={metadata['source_byte_count']}, "
            f"source_character_count={metadata['source_character_count']}, "
            f"visible_character_count={metadata['visible_character_count']}, "
            f"line_count={metadata['line_count']}, "
            f"content_digest={metadata['content_digest']!r}, "
            f"truncated={metadata['truncated']}, canonical=True, "
            "text_hidden=True, path_hidden=True)"
        )


def _safe_output_matches_material(
    output: SafeArtifactOutput,
    material: _SafeArtifactMaterial,
) -> bool:
    try:
        actual = (
            output.source_byte_count,
            output.source_character_count,
            output.visible_character_count,
            output.line_count,
            output.content_digest,
            output.truncated,
        )
    except Exception:
        return False
    expected = (
        material.source_byte_count,
        material.source_character_count,
        material.visible_character_count,
        material.line_count,
        material.content_digest,
        material.truncated,
    )
    return (
        type(actual[0]) is int
        and type(actual[1]) is int
        and type(actual[2]) is int
        and type(actual[3]) is int
        and type(actual[4]) is str
        and type(actual[5]) is bool
        and actual == expected
    )


def _issue_safe_artifact_output(
    *,
    source_bytes: bytes,
    source_text: str,
    visible_text: str,
    path_binding_digest: str,
) -> SafeArtifactOutput:
    seal = object()
    with _SAFE_OUTPUT_LOCK:
        _PENDING_OUTPUT_SEALS.add(seal)
    try:
        output = SafeArtifactOutput(
            source_byte_count=len(source_bytes),
            source_character_count=len(source_text),
            visible_character_count=len(visible_text),
            line_count=source_text.count("\n") + (1 if source_text else 0),
            content_digest=hashlib.sha256(source_bytes).hexdigest(),
            truncated=len(visible_text) < len(source_text),
            _issuer_seal=seal,
        )
    finally:
        with _SAFE_OUTPUT_LOCK:
            _PENDING_OUTPUT_SEALS.discard(seal)
    material = _SafeArtifactMaterial(
        visible_text=visible_text,
        path_binding_digest=path_binding_digest,
        source_byte_count=output.source_byte_count,
        source_character_count=output.source_character_count,
        visible_character_count=output.visible_character_count,
        line_count=output.line_count,
        content_digest=output.content_digest,
        truncated=output.truncated,
    )
    with _SAFE_OUTPUT_LOCK:
        _CANONICAL_OUTPUTS[id(output)] = output
        _SAFE_OUTPUT_MATERIALS[output] = material
    return output


def _open_safe_artifact_output(output: SafeArtifactOutput) -> str:
    """Private friend for the future sealed executor; returns bounded safe text."""

    if type(output) is not SafeArtifactOutput:
        raise ArtifactOutputRejected("safe_output_invalid")
    with _SAFE_OUTPUT_LOCK:
        try:
            marker = output._canonical_marker
        except Exception:
            raise ArtifactOutputRejected("safe_output_not_canonical") from None
        if (
            marker is not _CANONICAL_OUTPUT_MARKER
            or _CANONICAL_OUTPUTS.get(id(output)) is not output
        ):
            raise ArtifactOutputRejected("safe_output_not_canonical")
        material = _SAFE_OUTPUT_MATERIALS.get(output)
        if material is None or not _safe_output_matches_material(output, material):
            raise ArtifactOutputRejected("safe_output_not_canonical")
        return material.visible_text


def guard_artifact_output(
    *,
    policy: ArtifactOutputPolicy,
    artifact_path: str,
    identity_proof: ArtifactIdentityProof,
    source_bytes: bytes,
) -> SafeArtifactOutput:
    """Validate one exact artifact read and return canonical bounded safe output.

    The entire byte source is decoded and scanned before visible truncation.  Any
    invalidity rejects the whole item; this function never performs redaction.
    """

    if type(policy) is not ArtifactOutputPolicy:
        raise ArtifactOutputRejected("artifact_output_policy_invalid")
    policy.__post_init__()
    if type(source_bytes) is not bytes:
        raise ArtifactOutputRejected("artifact_source_type_invalid")
    if len(source_bytes) > policy.max_source_bytes:
        raise ArtifactOutputRejected("artifact_source_oversized")
    root_digest, path_digest = _validate_target_path(policy, artifact_path)
    _validate_identity_proof(
        policy=policy,
        identity_proof=identity_proof,
        root_digest=root_digest,
        path_digest=path_digest,
        source_size=len(source_bytes),
    )
    source_text = _decode_and_scan_source(source_bytes)
    visible_text = source_text[: policy.max_visible_characters]
    return _issue_safe_artifact_output(
        source_bytes=source_bytes,
        source_text=source_text,
        visible_text=visible_text,
        path_binding_digest=path_digest,
    )


__all__ = [
    "ALLOWED_ARTIFACT_EXTENSIONS",
    "MAX_ARTIFACT_SOURCE_BYTES",
    "MAX_ARTIFACT_VISIBLE_CHARACTERS",
    "ArtifactIdentityProof",
    "ArtifactOutputPolicy",
    "ArtifactOutputRejected",
    "ArtifactPathFlavor",
    "ArtifactStatSnapshot",
    "SafeArtifactOutput",
    "digest_artifact_path",
    "guard_artifact_output",
]
