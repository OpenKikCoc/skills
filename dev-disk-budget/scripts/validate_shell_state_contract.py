#!/usr/bin/env python3
"""Validate dev-disk-budget shell/state workflow snippets."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


STATE_FLAGS = {
    "--manifest",
    "--output",
    "--receipt",
    "--session-manifest",
    "--wave-manifest",
}
PATH_ASSIGNMENT = re.compile(
    r"^(?:(?:local|readonly|typeset|export)\s+)?path(?:\+)?="
)
PATH_LOOP = re.compile(r"^for\s+path\s+in(?:\s|$)")
STATE_ASSIGNMENT = re.compile(r"^(?:export\s+)?(STATE|STATE_ROOT)=(\S.*)$")
STATE_REFERENCE = re.compile(r"^['\"]?\$\{?(STATE|STATE_ROOT)\}?/")
SCALAR_LIST_LOOP = re.compile(
    r"^for\s+[A-Za-z_][A-Za-z0-9_]*\s+in\s+"
    r"['\"]?\$\{?[A-Za-z_][A-Za-z0-9_]*\}?['\"]?;"
)


def issue(line: int, code: str, message: str) -> dict[str, Any]:
    return {"line": line, "code": code, "message": message}


def option_value(line: str, flag: str) -> str | None:
    match = re.search(rf"(?:^|\s){re.escape(flag)}(?:=|\s+)(\S+)", line)
    return match.group(1) if match else None


def optional_bare_zsh_glob(line: str) -> bool:
    if "(N)" in line:
        return False
    without_parameters = re.sub(
        r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*", "", line
    )
    return ("*" in without_parameters or "?" in without_parameters) and "=(" in line


def validate_snippet(text: str) -> list[dict[str, Any]]:
    """Return stable violations for one zsh workflow command sequence."""
    violations: list[dict[str, Any]] = []
    established: set[str] = set()
    continued = ""
    continued_line = 0

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if continued:
            stripped = f"{continued} {stripped}"
            line_number = continued_line
            continued = ""
        if stripped.endswith("\\"):
            continued = stripped[:-1].rstrip()
            continued_line = line_number
            continue

        if PATH_ASSIGNMENT.match(stripped) or PATH_LOOP.match(stripped):
            violations.append(
                issue(
                    line_number,
                    "ZSH_SPECIAL_PATH",
                    "zsh variable 'path' mutates PATH; use a descriptive path name",
                )
            )

        assignment = STATE_ASSIGNMENT.match(stripped)
        if assignment:
            name, value = assignment.groups()
            if re.search(r"\s(?:python3|git|jq|go|bash|zsh|sh)(?:\s|$)", value):
                violations.append(
                    issue(
                        line_number,
                        "ONE_COMMAND_STATE_PREFIX",
                        "establish the state root before the whole command sequence",
                    )
                )
            else:
                established.add(name)
            if "MANAGED_ROOT" in value:
                violations.append(
                    issue(
                        line_number,
                        "STATE_ROOT_UNDER_MANAGED_ROOT",
                        "state must remain outside disposable managed roots",
                    )
                )

        for flag in STATE_FLAGS:
            value = option_value(stripped, flag)
            if value is None:
                continue
            reference = STATE_REFERENCE.match(value)
            if reference is None:
                violations.append(
                    issue(
                        line_number,
                        "STATE_ARTIFACT_OUTSIDE_ROOT",
                        f"{flag} must stay beneath $STATE_ROOT or $STATE",
                    )
                )
            elif reference.group(1) not in established:
                violations.append(
                    issue(
                        line_number,
                        "STATE_ROOT_NOT_ESTABLISHED",
                        f"establish {reference.group(1)} before using {flag}",
                    )
                )

        if (
            " go-topology " in f" {stripped} "
            or " go-env " in f" {stripped} "
        ) and option_value(stripped, "--output") is None:
            violations.append(
                issue(
                    line_number,
                    "STATE_RECEIPT_NOT_PERSISTED",
                    "persist topology and environment receipts beneath state",
                )
            )

        if SCALAR_LIST_LOOP.match(stripped) and "[@]" not in stripped:
            violations.append(
                issue(
                    line_number,
                    "SCALAR_PSEUDO_LIST",
                    "use an array and quoted array expansion for repeated values",
                )
            )

        if optional_bare_zsh_glob(stripped):
            violations.append(
                issue(
                    line_number,
                    "OPTIONAL_BARE_ZSH_GLOB",
                    "use the zsh (N) qualifier when an optional glob may not match",
                )
            )

    return violations


def zsh_blocks(text: str) -> list[str]:
    return re.findall(r"^\x60\x60\x60zsh\s*$\n(.*?)^\x60\x60\x60$", text, re.MULTILINE | re.DOTALL)


def validate_file(path: Path) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for block_number, block in enumerate(
        zsh_blocks(path.read_text(encoding="utf-8")), start=1
    ):
        for entry in validate_snippet(block):
            violations.append({"block": block_number, **entry})
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Markdown paths; defaults to the canonical shell-state reference.",
    )
    arguments = parser.parse_args()
    skill_root = Path(__file__).resolve().parent.parent
    paths = arguments.paths or [skill_root / "references/shell-state-contract.md"]
    results: list[dict[str, Any]] = []
    try:
        for path in paths:
            violations = validate_file(path)
            if violations:
                results.append({"path": str(path), "violations": violations})
    except OSError as error:
        print(
            json.dumps(
                {"status": "error", "error": str(error)},
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "kind": "dev-disk-budget-shell-state-validation",
                "status": "ready" if not results else "blocked",
                "paths_checked": [str(path) for path in paths],
                "results": results,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not results else 1


if __name__ == "__main__":
    raise SystemExit(main())
