#!/usr/bin/env python3
"""Validate supplied worker worktree context evidence without inspecting Git."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Callable


SCHEMA_VERSION = 1
KIND = "worker-context-attestation"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
GIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
EVIDENCE_FIELDS = (
    "task_id",
    "plan_sha256",
    "assigned_worktree_root",
    "controller_worktree_root",
    "other_worker_roots",
    "observed_cwd",
    "observed_git_top_level",
    "expected_head_sha",
    "observed_head_sha",
    "write_set_sha256",
    "observed_write_set_sha256",
    "command_root",
    "path_operands",
)


class ContractError(ValueError):
    """Raised when one attestation field violates the JSON contract."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise ContractError(f"{field} must not have leading or trailing whitespace")
    if "\x00" in value:
        raise ContractError(f"{field} must not contain NUL")
    return value


def _task_id(value: Any, field: str) -> str:
    candidate = _text(value, field)
    if not ID_PATTERN.fullmatch(candidate):
        raise ContractError(
            f"{field} must use lowercase letters, digits, '.', '_', or '-'"
        )
    return candidate


def _hex_digest(
    value: Any,
    field: str,
    pattern: re.Pattern[str],
    description: str,
) -> str:
    candidate = _text(value, field)
    if not pattern.fullmatch(candidate):
        raise ContractError(f"{field} must be {description}")
    return candidate.lower()


def _git_sha(value: Any, field: str) -> str:
    return _hex_digest(
        value,
        field,
        GIT_SHA_PATTERN,
        "a full 40-character hexadecimal Git SHA",
    )


def _sha256(value: Any, field: str) -> str:
    return _hex_digest(
        value,
        field,
        SHA256_PATTERN,
        "a 64-character hexadecimal SHA-256 digest",
    )


def _absolute_path(value: Any, field: str) -> str:
    candidate = _text(value, field)
    if "\\" in candidate:
        raise ContractError(f"{field} must use POSIX path separators")
    if not candidate.startswith("/") or candidate.startswith("//"):
        raise ContractError(f"{field} must be an absolute POSIX path")
    raw_parts = candidate.split("/")
    if ".." in raw_parts:
        raise ContractError(f"{field} must not contain '..' segments")
    parts = [part for part in raw_parts if part not in {"", "."}]
    return f"/{'/'.join(parts)}" if parts else "/"


def _absolute_path_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ContractError(f"{field} must be an array")
    return sorted(
        _absolute_path(item, f"{field}[{index}]")
        for index, item in enumerate(value)
    )


def _relative_operand(value: Any, field: str) -> str:
    candidate = _text(value, field)
    if "\\" in candidate:
        raise ContractError(f"{field} must use POSIX path separators")
    if (
        candidate.startswith("/")
        or candidate.startswith("~")
        or re.match(r"^[A-Za-z]:", candidate)
    ):
        raise ContractError(f"{field} must be relative to command_root")
    raw_parts = candidate.split("/")
    if ".." in raw_parts:
        raise ContractError(f"{field} must not contain '..' segments")
    parts = [part for part in raw_parts if part not in {"", "."}]
    return "/".join(parts) if parts else "."


def _relative_operand_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ContractError(f"{field} must be an array")
    return [
        _relative_operand(item, f"{field}[{index}]")
        for index, item in enumerate(value)
    ]


def _is_within(path: str, root: str) -> bool:
    candidate = PurePosixPath(path)
    boundary = PurePosixPath(root)
    return candidate == boundary or boundary in candidate.parents


def _paths_overlap(left: str, right: str) -> bool:
    return _is_within(left, right) or _is_within(right, left)


def _relative_prefixes(
    path: str,
    assigned_root: str,
    include_basename: bool,
) -> list[tuple[str, ...]]:
    candidate_path = PurePosixPath(path)
    assigned_path = PurePosixPath(assigned_root)
    relative_parts = candidate_path.relative_to(assigned_path).parts
    prefixes: set[tuple[str, ...]] = set()
    if relative_parts:
        prefixes.add(relative_parts)
    if include_basename and candidate_path.name:
        prefixes.add((candidate_path.name,))
    return sorted(prefixes, key=lambda parts: (-len(parts), parts))


def _empty_evidence() -> dict[str, Any]:
    return {field: None for field in EVIDENCE_FIELDS}


def _result(blockers: list[str], evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "status": "ready" if not blockers else "blocked",
        "blockers": blockers,
        "normalized_evidence": evidence,
    }


def validate_attestation(document: Any) -> dict[str, Any]:
    """Return a deterministic attestation decision over supplied JSON evidence."""

    blockers: list[str] = []
    evidence = _empty_evidence()
    if not isinstance(document, dict):
        return _result(["attestation input must be an object"], evidence)

    allowed_fields = {"schema_version", *EVIDENCE_FIELDS}
    unknown_fields = sorted(set(document) - allowed_fields)
    if unknown_fields:
        blockers.append(
            f"attestation input contains unsupported top-level fields: {unknown_fields}"
        )

    if (
        not isinstance(document.get("schema_version"), int)
        or isinstance(document.get("schema_version"), bool)
        or document.get("schema_version") != SCHEMA_VERSION
    ):
        blockers.append(f"schema_version must be {SCHEMA_VERSION}")

    normalizers: dict[str, Callable[[Any, str], Any]] = {
        "task_id": _task_id,
        "plan_sha256": _sha256,
        "assigned_worktree_root": _absolute_path,
        "controller_worktree_root": _absolute_path,
        "other_worker_roots": _absolute_path_list,
        "observed_cwd": _absolute_path,
        "observed_git_top_level": _absolute_path,
        "expected_head_sha": _git_sha,
        "observed_head_sha": _git_sha,
        "write_set_sha256": _sha256,
        "observed_write_set_sha256": _sha256,
        "command_root": _absolute_path,
        "path_operands": _relative_operand_list,
    }
    for field in EVIDENCE_FIELDS:
        try:
            evidence[field] = normalizers[field](document.get(field), field)
        except ContractError as error:
            blockers.append(str(error))

    assigned_root = evidence["assigned_worktree_root"]
    controller_root = evidence["controller_worktree_root"]
    other_roots = evidence["other_worker_roots"]
    observed_cwd = evidence["observed_cwd"]
    observed_git_top_level = evidence["observed_git_top_level"]
    command_root = evidence["command_root"]

    if assigned_root is not None and controller_root is not None:
        if _paths_overlap(assigned_root, controller_root):
            blockers.append(
                "assigned_worktree_root must not overlap controller_worktree_root"
            )

    if other_roots is not None:
        seen_roots: set[str] = set()
        for index, other_root in enumerate(other_roots):
            if other_root in seen_roots:
                blockers.append(
                    f"other_worker_roots contains duplicate root '{other_root}'"
                )
            seen_roots.add(other_root)
            if assigned_root is not None and _paths_overlap(
                assigned_root, other_root
            ):
                blockers.append(
                    "assigned_worktree_root must not overlap "
                    f"other_worker_roots[{index}]"
                )

    if assigned_root is not None and observed_cwd is not None:
        if not _is_within(observed_cwd, assigned_root):
            blockers.append("observed_cwd must be within assigned_worktree_root")

    if assigned_root is not None and observed_git_top_level is not None:
        if observed_git_top_level != assigned_root:
            blockers.append(
                "observed_git_top_level must equal assigned_worktree_root"
            )

    forbidden_roots: list[tuple[str, str]] = []
    if controller_root is not None:
        forbidden_roots.append(("controller_worktree_root", controller_root))
    if other_roots is not None:
        forbidden_roots.extend(
            (f"other_worker_roots[{index}]", root)
            for index, root in enumerate(other_roots)
        )
    for field, path in (
        ("observed_cwd", observed_cwd),
        ("observed_git_top_level", observed_git_top_level),
        ("command_root", command_root),
    ):
        if path is None:
            continue
        for root_field, root in forbidden_roots:
            if _is_within(path, root):
                blockers.append(f"{field} must not be within {root_field}")

    if evidence["expected_head_sha"] is not None and evidence[
        "observed_head_sha"
    ] is not None:
        if evidence["expected_head_sha"] != evidence["observed_head_sha"]:
            blockers.append("observed_head_sha does not match expected_head_sha")

    if evidence["write_set_sha256"] is not None and evidence[
        "observed_write_set_sha256"
    ] is not None:
        if (
            evidence["write_set_sha256"]
            != evidence["observed_write_set_sha256"]
        ):
            blockers.append(
                "observed_write_set_sha256 does not match write_set_sha256"
            )

    command_is_assigned = False
    if assigned_root is not None and command_root is not None:
        command_is_assigned = _is_within(command_root, assigned_root)
        if not command_is_assigned:
            blockers.append("command_root must be within assigned_worktree_root")

    path_operands = evidence["path_operands"]
    if (
        assigned_root is not None
        and command_root is not None
        and command_is_assigned
        and path_operands is not None
    ):
        represented_prefixes: list[tuple[tuple[str, ...], str]] = [
            (prefix, "command_root")
            for prefix in _relative_prefixes(command_root, assigned_root, True)
        ]
        if (
            observed_cwd is not None
            and _is_within(observed_cwd, assigned_root)
        ):
            represented_prefixes.extend(
                (prefix, "observed_cwd")
                for prefix in _relative_prefixes(
                    observed_cwd,
                    assigned_root,
                    False,
                )
            )
        represented_prefixes = sorted(
            set(represented_prefixes),
            key=lambda item: (-len(item[0]), item[0], item[1]),
        )
        for index, operand in enumerate(path_operands):
            if operand == ".":
                continue
            operand_parts = PurePosixPath(operand).parts
            for prefix, represented_by in represented_prefixes:
                if operand_parts[: len(prefix)] == prefix:
                    rendered_prefix = "/".join(prefix)
                    blockers.append(
                        f"path_operands[{index}] repeats subtree prefix "
                        f"'{rendered_prefix}' already represented by {represented_by}"
                    )
                    break

    return _result(blockers, evidence)


def _load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _write_json(document: Any, path: str) -> None:
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if path == "-":
        sys.stdout.write(rendered)
        return
    Path(path).write_text(rendered, encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate supplied worker worktree context evidence."
    )
    parser.add_argument(
        "--input",
        default="-",
        help="input JSON file; defaults to stdin",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="output JSON file; defaults to stdout",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    input_error = False
    try:
        source = _load_json(arguments.input)
        result = validate_attestation(source)
    except (json.JSONDecodeError, OSError, UnicodeError) as error:
        input_error = True
        result = _result([f"unable to read input JSON: {error}"], _empty_evidence())
    _write_json(result, arguments.output)
    if input_error:
        return 2
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
