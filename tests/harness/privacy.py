from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_FORBIDDEN_KEY_PARTS = (
    "api_key",
    "authorization",
    "base64",
    "cookie",
    "credential",
    "password",
    "prompt",
    "raw_chat",
    "real_id",
    "request_body",
    "response_body",
    "secret",
    "token",
    "url",
)
_FORBIDDEN_TEXT = (
    ("external_url", re.compile(r"https?://", re.IGNORECASE)),
    ("data_url", re.compile(r"data:[a-z0-9.+-]+/[a-z0-9.+-]+;base64,", re.IGNORECASE)),
    ("bearer_secret", re.compile(r"\bbearer\s+[a-z0-9._~-]{8,}", re.IGNORECASE)),
    ("api_key_value", re.compile(r"\bsk-[a-z0-9_-]{8,}", re.IGNORECASE)),
    ("windows_path", re.compile(r"\b[a-z]:\\", re.IGNORECASE)),
    ("runtime_path", re.compile(r"/(?:astrbot|home|users|vol\d+)/", re.IGNORECASE)),
    ("long_numeric_id", re.compile(r"(?<![a-z0-9])\d{7,}(?![a-z0-9])", re.IGNORECASE)),
    ("umo_identifier", re.compile(r"\b[a-z0-9_.-]+:[a-z0-9_.-]+:[a-z0-9_.-]+\b", re.IGNORECASE)),
)


@dataclass(frozen=True, slots=True)
class FixturePrivacyReport:
    is_safe: bool
    issue_codes: tuple[str, ...]
    file_count: int
    scalar_count: int


def _scan(value: Any, issues: set[str], *, key: str = "") -> int:
    count = 0
    lowered_key = str(key or "").strip().lower()
    if any(part in lowered_key for part in _FORBIDDEN_KEY_PARTS):
        issues.add("forbidden_key")
    if isinstance(value, dict):
        for child_key, child in value.items():
            count += _scan(child, issues, key=str(child_key))
        return count
    if isinstance(value, list):
        for child in value:
            count += _scan(child, issues, key=key)
        return count
    count += 1
    if isinstance(value, str):
        for code, pattern in _FORBIDDEN_TEXT:
            if pattern.search(value):
                issues.add(code)
    return count


def scan_fixture_tree(root: Path) -> FixturePrivacyReport:
    issues: set[str] = set()
    files = sorted(Path(root).glob("*.json"))
    scalar_count = 0
    for path in files:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            issues.add("invalid_json")
            continue
        scalar_count += _scan(value, issues)
    return FixturePrivacyReport(
        is_safe=not issues,
        issue_codes=tuple(sorted(issues)),
        file_count=len(files),
        scalar_count=scalar_count,
    )

