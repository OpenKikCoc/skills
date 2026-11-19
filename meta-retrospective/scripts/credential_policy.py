#!/usr/bin/env python3
"""Shared credential-text detector for the draft inbox.

One implementation, used by every consumer (propose, decide, list/read paths).
It scans decoded strings and dict keys recursively, never a serialized JSON blob,
and returns stable labels only: a caller must never echo the matched text.
Standard library only.
"""

from __future__ import annotations

import re
from typing import Any, Tuple

MAX_DEPTH = 32

# A value that is obviously a placeholder is not a secret.
_PLACEHOLDER = r"(?!<|\$\{|\$[A-Z_]+\b|\*{3}|\[?redacted|x{4})"
_NAMES = (r"password|passwd|secret|token|api[_-]?key|access[_-]?token|"
          r"refresh[_-]?token|client[_-]?secret")

_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("assignment", re.compile(
        r"(?<![A-Za-z0-9])(?:" + _NAMES + r")(?![A-Za-z0-9])\s*[:=]\s*" + _PLACEHOLDER + r"\S+",
        re.IGNORECASE)),
    ("authorization", re.compile(
        r"\b(?:Bearer|Basic)\s+" + _PLACEHOLDER + r"[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE)),
    ("query-secret", re.compile(
        r"[?&](?:" + _NAMES + r"|key|ak|sig|signature)=" + _PLACEHOLDER + r"[^&\s]+", re.IGNORECASE)),
    ("token-prefix", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("uri-password", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)),
    ("home-path", re.compile(r"(?:/Users|/home)/(?!you/|user/|name/)[A-Za-z0-9._-]+/")),
)
_FIELD_NAME = re.compile(
    r"(?i)(?:^|[_.\-\s])(?:password|passwd|secret|token|api[_-]?key|apikey|authorization|"
    r"credentials?|private[_-]?key|cookie)(?:$|[_.\-\s])")


def _scan_text(text: str, kinds: set) -> None:
    for kind, pattern in _PATTERNS:
        if pattern.search(text):
            kinds.add(kind)


def _walk(value: Any, kinds: set, depth: int) -> None:
    if depth > MAX_DEPTH:
        kinds.add("depth-limit")
        return
    if isinstance(value, str):
        _scan_text(value, kinds)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                _scan_text(key, kinds)
                if _FIELD_NAME.search(key):
                    kinds.add("credential-field-name")
            else:
                kinds.add("non-string-key")
            _walk(item, kinds, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk(item, kinds, depth + 1)


def credential_findings(value: Any) -> Tuple[str, ...]:
    """Return sorted kind labels found in strings and keys of ``value``; never the text."""
    kinds: set = set()
    _walk(value, kinds, 0)
    return tuple(sorted(kinds))
