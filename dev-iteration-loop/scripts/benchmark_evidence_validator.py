#!/usr/bin/env python3
"""Validate pure, controller-collected exact-SHA benchmark evidence."""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any


FULL_GIT_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SEMANTIC_FIELDS = {
    "harness",
    "parser",
    "fixture",
    "command",
    "metric",
}
ROOT_FIELDS = {
    "schema_version",
    "performance_work",
    "baseline_sha",
    "candidate_sha",
    "controller_checkout",
    "worktrees",
    "cache",
    "runs",
    "claim",
}


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _exact_fields(
    value: dict[str, Any], expected: set[str], label: str
) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise ValueError(f"{label} missing fields: {missing}")
    if unknown:
        raise ValueError(f"{label} unsupported fields: {unknown}")


def _full_oid(value: Any, label: str) -> str:
    if not isinstance(value, str) or not FULL_GIT_OID.fullmatch(value):
        raise ValueError(f"{label} must be a full lowercase Git object ID")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _absolute_path(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty absolute POSIX path")
    path = PurePosixPath(value)
    if not path.is_absolute() or path == PurePosixPath("/"):
        raise ValueError(f"{label} must be a bounded absolute POSIX path")
    if ".." in path.parts or any(char in value for char in "*?[]{}"):
        raise ValueError(f"{label} must not contain traversal or glob syntax")
    return path


def _is_within(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or root in path.parents


def _decimal(value: Any, label: str) -> Decimal:
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be a single-line decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{label} must be a decimal string") from error
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def _controller(value: Any) -> PurePosixPath:
    controller = _object(value, "controller_checkout")
    _exact_fields(
        controller,
        {
            "path",
            "dirty_before",
            "declared_read_only",
            "write_operations",
            "status_before_sha256",
            "status_after_sha256",
        },
        "controller_checkout",
    )
    path = _absolute_path(controller["path"], "controller_checkout.path")
    if not isinstance(controller["dirty_before"], bool):
        raise ValueError("controller_checkout.dirty_before must be boolean")
    if controller["declared_read_only"] is not True:
        raise ValueError("controller checkout must be declared read-only")
    writes = controller["write_operations"]
    if not isinstance(writes, int) or isinstance(writes, bool) or writes != 0:
        raise ValueError("controller checkout must record zero write operations")
    before = _sha256(
        controller["status_before_sha256"],
        "controller_checkout.status_before_sha256",
    )
    after = _sha256(
        controller["status_after_sha256"],
        "controller_checkout.status_after_sha256",
    )
    if before != after:
        raise ValueError("controller checkout status changed during the benchmark")
    return path


def _worktrees(
    value: Any, baseline_sha: str, candidate_sha: str, controller: PurePosixPath
) -> dict[str, PurePosixPath]:
    worktrees = _object(value, "worktrees")
    _exact_fields(worktrees, {"baseline", "candidate"}, "worktrees")
    result: dict[str, PurePosixPath] = {}
    for role, expected_sha in (
        ("baseline", baseline_sha),
        ("candidate", candidate_sha),
    ):
        entry = _object(worktrees[role], f"worktrees.{role}")
        _exact_fields(
            entry, {"path", "head_sha", "isolated"}, f"worktrees.{role}"
        )
        path = _absolute_path(entry["path"], f"worktrees.{role}.path")
        if _full_oid(entry["head_sha"], f"worktrees.{role}.head_sha") != expected_sha:
            raise ValueError(f"worktrees.{role}.head_sha does not match pinned SHA")
        if entry["isolated"] is not True:
            raise ValueError(f"worktrees.{role} must be attested as isolated")
        result[role] = path
    if len({controller, result["baseline"], result["candidate"]}) != 3:
        raise ValueError("controller, baseline, and candidate paths must be distinct")
    return result


def _cache(value: Any) -> None:
    cache = _object(value, "cache")
    _exact_fields(cache, {"applicable", "shared_cache_ids"}, "cache")
    applicable = cache["applicable"]
    identifiers = cache["shared_cache_ids"]
    if not isinstance(applicable, bool):
        raise ValueError("cache.applicable must be boolean")
    if not isinstance(identifiers, list) or any(
        not isinstance(item, str) or not item for item in identifiers
    ):
        raise ValueError("cache.shared_cache_ids must be a list of non-empty strings")
    expected = 1 if applicable else 0
    if len(identifiers) != expected or len(set(identifiers)) != len(identifiers):
        raise ValueError(
            f"cache must declare exactly {expected} shared cache IDs when applicable={applicable}"
        )


def _result_record(value: Any, label: str) -> dict[str, str]:
    record = _object(value, label)
    _exact_fields(record, {"metric", "unit", "value"}, label)
    for field in ("metric", "unit"):
        item = record[field]
        if not isinstance(item, str) or not item or "\n" in item or "\r" in item:
            raise ValueError(f"{label}.{field} must be a non-empty single-line string")
    _decimal(record["value"], f"{label}.value")
    return record


def _run(
    value: Any, role: str, expected_sha: str, worktree: PurePosixPath
) -> tuple[dict[str, str], dict[str, str]]:
    label = f"runs.{role}"
    run = _object(value, label)
    _exact_fields(
        run,
        {
            "executed_sha",
            "shell",
            "argv",
            "executable_preflight",
            "discovery_roots",
            "log_channel",
            "result_channel",
            "semantic_fingerprints",
            "exit_code",
        },
        label,
    )
    if _full_oid(run["executed_sha"], f"{label}.executed_sha") != expected_sha:
        raise ValueError(f"{label}.executed_sha does not match pinned SHA")
    shell = _absolute_path(run["shell"], f"{label}.shell")
    argv = run["argv"]
    if not isinstance(argv, list) or not argv or any(
        not isinstance(item, str) or not item for item in argv
    ):
        raise ValueError(f"{label}.argv must be a non-empty string list")

    preflight = _object(run["executable_preflight"], f"{label}.executable_preflight")
    _exact_fields(
        preflight,
        {"shell", "tool", "resolved_path", "executable"},
        f"{label}.executable_preflight",
    )
    if _absolute_path(
        preflight["shell"], f"{label}.executable_preflight.shell"
    ) != shell:
        raise ValueError(f"{label} preflight did not use the actual run shell")
    if preflight["tool"] != argv[0]:
        raise ValueError(f"{label} preflight tool does not match argv[0]")
    try:
        _absolute_path(
            preflight["resolved_path"],
            f"{label}.executable_preflight.resolved_path",
        )
    except ValueError as error:
        raise ValueError(f"{label} tool was not resolved through the actual shell PATH") from error
    if preflight["executable"] is not True:
        raise ValueError(f"{label} resolved tool is not executable")

    roots = run["discovery_roots"]
    if not isinstance(roots, list) or not roots:
        raise ValueError(f"{label}.discovery_roots must be a non-empty bounded list")
    for index, raw_root in enumerate(roots):
        root = _absolute_path(raw_root, f"{label}.discovery_roots[{index}]")
        if not _is_within(root, worktree):
            raise ValueError(f"{label}.discovery_roots[{index}] escapes its worktree")

    log_channel = _object(run["log_channel"], f"{label}.log_channel")
    _exact_fields(log_channel, {"name", "format", "text"}, f"{label}.log_channel")
    if (
        not isinstance(log_channel["name"], str)
        or not log_channel["name"]
        or log_channel["format"] != "text"
        or not isinstance(log_channel["text"], str)
    ):
        raise ValueError(f"{label}.log_channel must be a named text channel")

    result_channel = _object(run["result_channel"], f"{label}.result_channel")
    _exact_fields(
        result_channel, {"name", "format", "records"}, f"{label}.result_channel"
    )
    if (
        not isinstance(result_channel["name"], str)
        or not result_channel["name"]
        or result_channel["format"] != "application/json"
    ):
        raise ValueError(f"{label}.result_channel must be a named JSON channel")
    if result_channel["name"] == log_channel["name"]:
        raise ValueError(f"{label} log and machine result channels must be distinct")
    records = result_channel["records"]
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError(f"{label}.result_channel.records must contain one JSON record")
    record = _result_record(records[0], f"{label}.result_channel.records[0]")

    semantics = _object(run["semantic_fingerprints"], f"{label}.semantic_fingerprints")
    _exact_fields(semantics, SEMANTIC_FIELDS, f"{label}.semantic_fingerprints")
    for field in sorted(SEMANTIC_FIELDS):
        _sha256(semantics[field], f"{label}.semantic_fingerprints.{field}")
    exit_code = run["exit_code"]
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code != 0:
        raise ValueError(f"{label}.exit_code must be zero")
    return record, semantics


def _claim(
    value: Any,
    attribution: str,
    baseline: dict[str, str],
    candidate: dict[str, str],
) -> None:
    claim = _object(value, "claim")
    _exact_fields(claim, {"attribution", "ab_delta"}, "claim")
    if claim["attribution"] not in {"portable_cross_sha", "candidate_only"}:
        raise ValueError("claim.attribution must be portable_cross_sha or candidate_only")
    if claim["attribution"] != attribution:
        raise ValueError(f"claim.attribution must be {attribution} for this evidence")
    delta = claim["ab_delta"]
    if attribution == "candidate_only":
        if delta is not None:
            raise ValueError("A/B delta claims are forbidden for candidate-only evidence")
        return
    if (
        baseline["metric"] != candidate["metric"]
        or baseline["unit"] != candidate["unit"]
    ):
        raise ValueError(
            "portable result records must use the same metric and unit"
        )
    if delta is None:
        return
    delta = _object(delta, "claim.ab_delta")
    _exact_fields(
        delta,
        {"metric", "unit", "baseline", "candidate", "candidate_minus_baseline"},
        "claim.ab_delta",
    )
    if delta["metric"] != baseline["metric"] or delta["unit"] != baseline["unit"]:
        raise ValueError("claim.ab_delta metric or unit does not match machine results")
    baseline_value = _decimal(delta["baseline"], "claim.ab_delta.baseline")
    candidate_value = _decimal(delta["candidate"], "claim.ab_delta.candidate")
    claimed_delta = _decimal(
        delta["candidate_minus_baseline"],
        "claim.ab_delta.candidate_minus_baseline",
    )
    if baseline_value != _decimal(baseline["value"], "baseline result value"):
        raise ValueError("claim.ab_delta baseline does not match machine result")
    if candidate_value != _decimal(candidate["value"], "candidate result value"):
        raise ValueError("claim.ab_delta candidate does not match machine result")
    if claimed_delta != candidate_value - baseline_value:
        raise ValueError("claim.ab_delta candidate_minus_baseline is incorrect")


def validate_packet(packet: Any) -> dict[str, Any]:
    packet = _object(packet, "input")
    unknown = sorted(set(packet) - ROOT_FIELDS)
    if unknown:
        raise ValueError(f"unsupported root fields: {unknown}")
    if packet.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    performance_work = packet.get("performance_work")
    if not isinstance(performance_work, bool):
        raise ValueError("performance_work must be boolean")
    if not performance_work:
        if set(packet) != {"schema_version", "performance_work"}:
            raise ValueError("non-performance packets must not carry benchmark evidence")
        return {
            "schema_version": 1,
            "valid": True,
            "applicable": False,
            "attribution": "not_applicable",
            "comparison_allowed": False,
            "semantic_differences": [],
        }

    _exact_fields(packet, ROOT_FIELDS, "input")
    baseline_sha = _full_oid(packet["baseline_sha"], "baseline_sha")
    candidate_sha = _full_oid(packet["candidate_sha"], "candidate_sha")
    if baseline_sha == candidate_sha:
        raise ValueError("baseline_sha and candidate_sha must be distinct")
    controller = _controller(packet["controller_checkout"])
    worktrees = _worktrees(
        packet["worktrees"], baseline_sha, candidate_sha, controller
    )
    _cache(packet["cache"])
    runs = _object(packet["runs"], "runs")
    _exact_fields(runs, {"baseline", "candidate"}, "runs")
    baseline_record, baseline_semantics = _run(
        runs["baseline"], "baseline", baseline_sha, worktrees["baseline"]
    )
    candidate_record, candidate_semantics = _run(
        runs["candidate"], "candidate", candidate_sha, worktrees["candidate"]
    )
    differences = sorted(
        field
        for field in SEMANTIC_FIELDS
        if baseline_semantics[field] != candidate_semantics[field]
    )
    attribution = "candidate_only" if differences else "portable_cross_sha"
    _claim(packet["claim"], attribution, baseline_record, candidate_record)
    return {
        "schema_version": 1,
        "valid": True,
        "applicable": True,
        "attribution": attribution,
        "comparison_allowed": not differences,
        "semantic_differences": differences,
    }


def load_packet(path: Path | None) -> Any:
    try:
        raw = sys.stdin.read() if path is None else path.read_text()
        return json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid input: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "packet", nargs="?", type=Path, help="JSON packet; read stdin when omitted"
    )
    args = parser.parse_args()
    try:
        result = validate_packet(load_packet(args.packet))
    except ValueError as error:
        print(json.dumps({"valid": False, "error": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
