#!/usr/bin/env python3
"""Validate a source-bound delivery evidence epoch without side effects."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Sequence


SCHEMA_VERSION = 1
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MATRIX_AUTHORITIES = {
    "local_gates": "local_runner",
    "remote_checks": "remote_ci",
    "composition_generated_artifacts": "artifact_generator",
    "release_receipts": "release_authority",
}
RELATIONSHIPS = {"same_revision", "later_evidence_revision"}
SOURCE_DEPENDENCIES = {"source_dependent", "source_independent"}


class ContractError(ValueError):
    """Raised when a packet cannot be normalized safely."""


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _only_fields(value: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(f"{field} has unsupported fields: {unknown}")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{field} must be an array")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ContractError(f"{field} must be a non-empty string without NUL")
    return value.strip()


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{field} must be a boolean")
    return value


def _identifier(value: Any, field: str) -> str:
    candidate = _string(value, field)
    if not ID_PATTERN.fullmatch(candidate):
        raise ContractError(
            f"{field} must use lowercase letters, digits, '.', '_', or '-'"
        )
    return candidate


def _sha(value: Any, field: str) -> str:
    candidate = _string(value, field).lower()
    if not SHA_PATTERN.fullmatch(candidate):
        raise ContractError(f"{field} must be a lowercase full 40-character SHA")
    return candidate


def _sha256(value: Any, field: str) -> str:
    candidate = _string(value, field)
    if not SHA256_PATTERN.fullmatch(candidate):
        raise ContractError(f"{field} must be a lowercase SHA-256 digest")
    return candidate


def _relative_path(value: Any, field: str, *, allow_subtree: bool = False) -> str:
    candidate = _string(value, field)
    subtree = allow_subtree and candidate.endswith("/**")
    raw_path = candidate[:-3] if subtree else candidate
    if any(character in raw_path for character in ("*", "?", "[", "]", "\\")):
        raise ContractError(f"{field} contains unsupported path syntax")
    if (
        not raw_path
        or raw_path.startswith(("/", "./"))
        or raw_path.endswith("/")
        or "//" in raw_path
        or any(part in {"", ".", ".."} for part in raw_path.split("/"))
    ):
        raise ContractError(f"{field} must be a normalized repository-relative path")
    normalized = PurePosixPath(raw_path).as_posix()
    return f"{normalized}/**" if subtree else normalized


def _path_is_allowed(path: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("/**"):
            root = pattern[:-3]
            if path == root or path.startswith(f"{root}/"):
                return True
        elif path == pattern:
            return True
    return False


def _policy(value: Any, field: str) -> dict[str, str]:
    source = _mapping(value, field)
    _only_fields(source, {"id", "description"}, field)
    return {
        "id": _identifier(source.get("id"), f"{field}.id"),
        "description": _string(
            source.get("description"), f"{field}.description"
        ),
    }


def _named_hashes(value: Any, field: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for index, raw in enumerate(_list(value, field)):
        item_field = f"{field}[{index}]"
        source = _mapping(raw, item_field)
        _only_fields(source, {"name", "sha256"}, item_field)
        items.append(
            {
                "name": _identifier(source.get("name"), f"{item_field}.name"),
                "sha256": _sha256(
                    source.get("sha256"), f"{item_field}.sha256"
                ),
            }
        )
    if not items:
        raise ContractError(f"{field} must not be empty")
    names = [item["name"] for item in items]
    if len(names) != len(set(names)):
        raise ContractError(f"{field} contains duplicate names")
    return sorted(items, key=lambda item: item["name"])


def _authority(value: Any, field: str) -> dict[str, str]:
    source = _mapping(value, field)
    _only_fields(source, {"kind", "locator"}, field)
    return {
        "kind": _identifier(source.get("kind"), f"{field}.kind"),
        "locator": _string(source.get("locator"), f"{field}.locator"),
    }


def _repository_identity(value: Any, field: str) -> dict[str, str]:
    source = _mapping(value, field)
    _only_fields(source, {"repository_id", "checkout_sha256"}, field)
    return {
        "repository_id": _string(
            source.get("repository_id"), f"{field}.repository_id"
        ),
        "checkout_sha256": _sha256(
            source.get("checkout_sha256"), f"{field}.checkout_sha256"
        ),
    }


def _command(value: Any, field: str) -> dict[str, Any]:
    source = _mapping(value, field)
    _only_fields(
        source, {"id", "argv", "authority_kind", "target", "profile"}, field
    )
    raw_argv = _list(source.get("argv"), f"{field}.argv")
    argv = [_string(item, f"{field}.argv[{index}]") for index, item in enumerate(raw_argv)]
    if not argv:
        raise ContractError(f"{field}.argv must not be empty")
    return {
        "id": _identifier(source.get("id"), f"{field}.id"),
        "argv": argv,
        "authority_kind": _identifier(
            source.get("authority_kind"), f"{field}.authority_kind"
        ),
        "target": _string(source.get("target"), f"{field}.target"),
        "profile": _string(source.get("profile"), f"{field}.profile"),
    }


def _plan(value: Any) -> dict[str, Any]:
    source = _mapping(value, "revision_binding.immutable_plan")
    field = "revision_binding.immutable_plan"
    _only_fields(
        source,
        {
            "id",
            "established_at_revision",
            "repository_identity",
            "commands",
            "environment_policy",
            "sample_policy",
            "evidence_paths",
        },
        field,
    )
    commands = [
        _command(item, f"{field}.commands[{index}]")
        for index, item in enumerate(_list(source.get("commands"), f"{field}.commands"))
    ]
    if not commands:
        raise ContractError(f"{field}.commands must not be empty")
    command_ids = [item["id"] for item in commands]
    if len(command_ids) != len(set(command_ids)):
        raise ContractError(f"{field}.commands contains duplicate ids")
    evidence_paths = [
        _relative_path(item, f"{field}.evidence_paths[{index}]", allow_subtree=True)
        for index, item in enumerate(
            _list(source.get("evidence_paths"), f"{field}.evidence_paths")
        )
    ]
    if not evidence_paths:
        raise ContractError(f"{field}.evidence_paths must not be empty")
    if len(evidence_paths) != len(set(evidence_paths)):
        raise ContractError(f"{field}.evidence_paths contains duplicates")
    return {
        "id": _identifier(source.get("id"), f"{field}.id"),
        "established_at_revision": _sha(
            source.get("established_at_revision"),
            f"{field}.established_at_revision",
        ),
        "repository_identity": _repository_identity(
            source.get("repository_identity"), f"{field}.repository_identity"
        ),
        "commands": sorted(commands, key=lambda item: item["id"]),
        "environment_policy": _policy(
            source.get("environment_policy"), f"{field}.environment_policy"
        ),
        "sample_policy": _policy(
            source.get("sample_policy"), f"{field}.sample_policy"
        ),
        "evidence_paths": sorted(evidence_paths),
    }


def _receipt(value: Any, field: str) -> dict[str, Any]:
    source = _mapping(value, field)
    _only_fields(
        source,
        {
            "id",
            "source_dependency",
            "source_sha",
            "authoritative_inputs_sha256",
            "target",
            "profile",
            "bounded_claim",
            "reuse_class",
            "authority",
            "plan_command_id",
            "input_hashes",
            "output_hashes",
            "passed",
            "independence_basis",
        },
        field,
    )
    dependency = _string(source.get("source_dependency"), f"{field}.source_dependency")
    if dependency not in SOURCE_DEPENDENCIES:
        raise ContractError(
            f"{field}.source_dependency must be one of {sorted(SOURCE_DEPENDENCIES)}"
        )
    raw_source_sha = source.get("source_sha")
    source_sha = None if raw_source_sha is None else _sha(raw_source_sha, f"{field}.source_sha")
    raw_basis = source.get("independence_basis")
    independence_basis = (
        None if raw_basis is None else _string(raw_basis, f"{field}.independence_basis")
    )
    inputs = _named_hashes(source.get("input_hashes"), f"{field}.input_hashes")
    outputs = _named_hashes(source.get("output_hashes"), f"{field}.output_hashes")
    normalized = {
        "id": _identifier(source.get("id"), f"{field}.id"),
        "source_dependency": dependency,
        "source_sha": source_sha,
        "authoritative_inputs_sha256": _sha256(
            source.get("authoritative_inputs_sha256"),
            f"{field}.authoritative_inputs_sha256",
        ),
        "target": _string(source.get("target"), f"{field}.target"),
        "profile": _string(source.get("profile"), f"{field}.profile"),
        "bounded_claim": _string(
            source.get("bounded_claim"), f"{field}.bounded_claim"
        ),
        "reuse_class": _string(source.get("reuse_class"), f"{field}.reuse_class"),
        "authority": _authority(source.get("authority"), f"{field}.authority"),
        "plan_command_id": _identifier(
            source.get("plan_command_id"), f"{field}.plan_command_id"
        ),
        "input_hashes": inputs,
        "output_hashes": outputs,
        "passed": _boolean(source.get("passed"), f"{field}.passed"),
        "independence_basis": independence_basis,
    }
    return normalized


def _diff_proof(value: Any) -> dict[str, Any]:
    field = "revision_binding.evidence_only_diff"
    source = _mapping(value, field)
    _only_fields(
        source,
        {
            "implementation_revision",
            "evidence_revision",
            "implementation_is_ancestor",
            "source_paths_changed",
            "changed_paths",
            "source_tree_sha256_at_implementation",
            "source_tree_sha256_at_evidence",
            "diff_sha256",
            "repository_identity",
            "target",
            "profile",
            "authority",
            "plan_command_id",
            "input_hashes",
            "output_hashes",
        },
        field,
    )
    changed_paths = [
        _relative_path(item, f"{field}.changed_paths[{index}]")
        for index, item in enumerate(
            _list(source.get("changed_paths"), f"{field}.changed_paths")
        )
    ]
    if len(changed_paths) != len(set(changed_paths)):
        raise ContractError(f"{field}.changed_paths contains duplicates")
    return {
        "implementation_revision": _sha(
            source.get("implementation_revision"), f"{field}.implementation_revision"
        ),
        "evidence_revision": _sha(
            source.get("evidence_revision"), f"{field}.evidence_revision"
        ),
        "implementation_is_ancestor": _boolean(
            source.get("implementation_is_ancestor"),
            f"{field}.implementation_is_ancestor",
        ),
        "source_paths_changed": _boolean(
            source.get("source_paths_changed"), f"{field}.source_paths_changed"
        ),
        "changed_paths": sorted(changed_paths),
        "source_tree_sha256_at_implementation": _sha256(
            source.get("source_tree_sha256_at_implementation"),
            f"{field}.source_tree_sha256_at_implementation",
        ),
        "source_tree_sha256_at_evidence": _sha256(
            source.get("source_tree_sha256_at_evidence"),
            f"{field}.source_tree_sha256_at_evidence",
        ),
        "diff_sha256": _sha256(source.get("diff_sha256"), f"{field}.diff_sha256"),
        "repository_identity": _repository_identity(
            source.get("repository_identity"), f"{field}.repository_identity"
        ),
        "target": _string(source.get("target"), f"{field}.target"),
        "profile": _string(source.get("profile"), f"{field}.profile"),
        "authority": _authority(source.get("authority"), f"{field}.authority"),
        "plan_command_id": _identifier(
            source.get("plan_command_id"), f"{field}.plan_command_id"
        ),
        "input_hashes": _named_hashes(
            source.get("input_hashes"), f"{field}.input_hashes"
        ),
        "output_hashes": _named_hashes(
            source.get("output_hashes"), f"{field}.output_hashes"
        ),
    }


def _append(blockers: list[str], message: str) -> None:
    if message not in blockers:
        blockers.append(message)


def _hash_index(items: Sequence[Mapping[str, str]]) -> dict[str, str]:
    return {item["name"]: item["sha256"] for item in items}


def _ledger_entries(matrix: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for lane in sorted(matrix):
        for receipt in matrix[lane]:
            entries.append(
                {
                    "lane": lane,
                    "receipt_id": receipt["id"],
                    "authority": receipt["authority"],
                    "plan_command_id": receipt["plan_command_id"],
                    "source_dependency": receipt["source_dependency"],
                    "source_sha": receipt["source_sha"],
                    "authoritative_inputs_sha256": receipt[
                        "authoritative_inputs_sha256"
                    ],
                    "input_hashes": receipt["input_hashes"],
                    "output_hashes": receipt["output_hashes"],
                    "target": receipt["target"],
                    "profile": receipt["profile"],
                    "bounded_claim": receipt["bounded_claim"],
                    "reuse_class": receipt["reuse_class"],
                    "independence_basis": receipt["independence_basis"],
                    "passed": receipt["passed"],
                }
            )
    return sorted(entries, key=lambda item: (item["lane"], item["receipt_id"]))


def validate_packet(value: Any) -> dict[str, Any]:
    source = _mapping(value, "input")
    _only_fields(
        source,
        {
            "schema_version",
            "current_source_sha",
            "implementation_revision",
            "evidence_revision",
            "matrix",
            "revision_binding",
        },
        "input",
    )
    if source.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"schema_version must be {SCHEMA_VERSION}")
    current_source_sha = _sha(source.get("current_source_sha"), "current_source_sha")
    implementation_revision = _sha(
        source.get("implementation_revision"), "implementation_revision"
    )
    evidence_revision = _sha(source.get("evidence_revision"), "evidence_revision")

    matrix_source = _mapping(source.get("matrix"), "matrix")
    _only_fields(matrix_source, set(MATRIX_AUTHORITIES), "matrix")
    matrix: dict[str, list[dict[str, Any]]] = {}
    receipt_ids: set[str] = set()
    for lane in MATRIX_AUTHORITIES:
        raw_receipts = _list(matrix_source.get(lane), f"matrix.{lane}")
        if not raw_receipts:
            raise ContractError(f"matrix.{lane} must not be empty")
        receipts = [
            _receipt(item, f"matrix.{lane}[{index}]")
            for index, item in enumerate(raw_receipts)
        ]
        for receipt in receipts:
            if receipt["id"] in receipt_ids:
                raise ContractError(f"duplicate receipt id: {receipt['id']}")
            receipt_ids.add(receipt["id"])
        matrix[lane] = sorted(receipts, key=lambda item: item["id"])

    binding_field = "revision_binding"
    binding_source = _mapping(source.get("revision_binding"), binding_field)
    _only_fields(
        binding_source,
        {
            "relationship",
            "implementation_revision",
            "evidence_revision",
            "immutable_plan",
            "immutable_plan_sha256",
            "immutable_commands_sha256",
            "environment_policy_sha256",
            "sample_policy_sha256",
            "inputs_sha256",
            "outputs_sha256",
            "authority_ledger_sha256",
            "evidence_only_diff",
        },
        binding_field,
    )
    relationship = _string(binding_source.get("relationship"), f"{binding_field}.relationship")
    if relationship not in RELATIONSHIPS:
        raise ContractError(
            f"{binding_field}.relationship must be one of {sorted(RELATIONSHIPS)}"
        )
    binding_implementation = _sha(
        binding_source.get("implementation_revision"),
        f"{binding_field}.implementation_revision",
    )
    binding_evidence = _sha(
        binding_source.get("evidence_revision"), f"{binding_field}.evidence_revision"
    )
    plan = _plan(binding_source.get("immutable_plan"))
    proof = _diff_proof(binding_source.get("evidence_only_diff"))
    supplied_digests = {
        name: _sha256(binding_source.get(name), f"{binding_field}.{name}")
        for name in (
            "immutable_plan_sha256",
            "immutable_commands_sha256",
            "environment_policy_sha256",
            "sample_policy_sha256",
            "inputs_sha256",
            "outputs_sha256",
            "authority_ledger_sha256",
        )
    }

    blockers: list[str] = []
    if current_source_sha != evidence_revision:
        _append(blockers, "current_source_sha must equal evidence_revision")
    if binding_implementation != implementation_revision:
        _append(blockers, "revision binding names a different implementation revision")
    if binding_evidence != evidence_revision:
        _append(blockers, "revision binding names a different evidence revision")
    if relationship == "same_revision" and implementation_revision != evidence_revision:
        _append(blockers, "same_revision requires identical implementation and evidence revisions")
    if relationship == "later_evidence_revision" and implementation_revision == evidence_revision:
        _append(blockers, "later_evidence_revision requires distinct revisions")
    if plan["established_at_revision"] != implementation_revision:
        _append(blockers, "immutable plan must be established at implementation_revision")

    expected_digests = {
        "immutable_plan_sha256": canonical_sha256(plan),
        "immutable_commands_sha256": canonical_sha256(plan["commands"]),
        "environment_policy_sha256": canonical_sha256(plan["environment_policy"]),
        "sample_policy_sha256": canonical_sha256(plan["sample_policy"]),
    }
    for name, expected in expected_digests.items():
        if supplied_digests[name] != expected:
            _append(blockers, f"{name} does not match the immutable plan")

    commands = {command["id"]: command for command in plan["commands"]}
    used_command_ids: set[str] = set()
    all_inputs: list[dict[str, Any]] = []
    all_outputs: list[dict[str, Any]] = []
    source_dependent_count = 0
    source_independent_count = 0
    for lane in MATRIX_AUTHORITIES:
        lane_source_dependent = 0
        for receipt in matrix[lane]:
            receipt_label = f"{lane}/{receipt['id']}"
            command = commands.get(receipt["plan_command_id"])
            used_command_ids.add(receipt["plan_command_id"])
            if command is None:
                _append(blockers, f"receipt {receipt_label} names an unknown plan command")
            else:
                if command["authority_kind"] != receipt["authority"]["kind"]:
                    _append(
                        blockers,
                        f"receipt {receipt_label} authority does not match its plan command",
                    )
                if command["target"] != receipt["target"]:
                    _append(
                        blockers,
                        f"receipt {receipt_label} target does not match its plan command",
                    )
                if command["profile"] != receipt["profile"]:
                    _append(
                        blockers,
                        f"receipt {receipt_label} profile does not match its plan command",
                    )
            expected_authority = MATRIX_AUTHORITIES[lane]
            if receipt["authority"]["kind"] != expected_authority:
                _append(
                    blockers,
                    f"receipt {receipt_label} requires authority kind "
                    f"{expected_authority}",
                )
            if receipt["authoritative_inputs_sha256"] != canonical_sha256(
                receipt["input_hashes"]
            ):
                _append(
                    blockers,
                    f"receipt {receipt_label} authoritative inputs digest does not "
                    "match input_hashes",
                )
            if not receipt["passed"]:
                _append(blockers, f"receipt {receipt_label} did not pass")

            if receipt["source_dependency"] == "source_dependent":
                source_dependent_count += 1
                lane_source_dependent += 1
                if receipt["source_sha"] != implementation_revision:
                    _append(
                        blockers,
                        f"receipt {receipt_label} is stale or names a mixed "
                        "measured revision",
                    )
                expected_reuse = (
                    "implementation_bound"
                    if relationship == "later_evidence_revision"
                    else "current_source_only"
                )
                if receipt["reuse_class"] != expected_reuse:
                    _append(
                        blockers,
                        f"receipt {receipt_label} must use reuse_class {expected_reuse}",
                    )
                if receipt["independence_basis"] is not None:
                    _append(
                        blockers,
                        f"receipt {receipt_label} cannot claim an independence_basis",
                    )
            else:
                source_independent_count += 1
                if receipt["source_sha"] is not None:
                    _append(
                        blockers,
                        f"source-independent receipt {receipt_label} must not name "
                        "source_sha",
                    )
                if receipt["reuse_class"] != "source_independent":
                    _append(
                        blockers,
                        f"source-independent receipt {receipt_label} must explicitly "
                        "use reuse_class source_independent",
                    )
                if receipt["independence_basis"] is None:
                    _append(
                        blockers,
                        f"source-independent receipt {receipt_label} requires "
                        "independence_basis",
                    )
            for item in receipt["input_hashes"]:
                all_inputs.append({"receipt_id": receipt["id"], **item})
            for item in receipt["output_hashes"]:
                all_outputs.append({"receipt_id": receipt["id"], **item})
        if lane_source_dependent == 0:
            _append(blockers, f"matrix.{lane} requires at least one source-dependent receipt")

    if proof["implementation_revision"] != implementation_revision:
        _append(blockers, "evidence-only diff proof names a different implementation revision")
    if proof["evidence_revision"] != evidence_revision:
        _append(blockers, "evidence-only diff proof names a different evidence revision")
    if not proof["implementation_is_ancestor"]:
        _append(
            blockers,
            "evidence revision must prove the implementation revision is an ancestor",
        )
    if proof["source_paths_changed"]:
        _append(blockers, "evidence revision changes source paths")
    if proof["source_tree_sha256_at_implementation"] != proof["source_tree_sha256_at_evidence"]:
        _append(blockers, "evidence revision changes the source tree digest")
    if proof["repository_identity"] != plan["repository_identity"]:
        _append(
            blockers,
            "evidence-only diff repository identity does not match immutable plan",
        )
    disallowed_paths = [
        path
        for path in proof["changed_paths"]
        if not _path_is_allowed(path, plan["evidence_paths"])
    ]
    for path in disallowed_paths:
        _append(blockers, f"evidence revision changes a path outside evidence_paths: {path}")
    if relationship == "same_revision" and proof["changed_paths"]:
        _append(blockers, "same_revision cannot contain evidence-only changed paths")
    if relationship == "later_evidence_revision" and not proof["changed_paths"]:
        _append(blockers, "later_evidence_revision requires an evidence-only changed path")
    if proof["authority"]["kind"] != "git":
        _append(blockers, "evidence-only diff proof requires git authority")
    proof_command = commands.get(proof["plan_command_id"])
    used_command_ids.add(proof["plan_command_id"])
    if proof_command is None:
        _append(blockers, "evidence-only diff proof names an unknown plan command")
    else:
        if proof_command["authority_kind"] != proof["authority"]["kind"]:
            _append(
                blockers,
                "evidence-only diff proof authority does not match its plan command",
            )
        if proof_command["target"] != proof["target"]:
            _append(
                blockers,
                "evidence-only diff proof target does not match its plan command",
            )
        if proof_command["profile"] != proof["profile"]:
            _append(
                blockers,
                "evidence-only diff proof profile does not match its plan command",
            )
    proof_outputs = _hash_index(proof["output_hashes"])
    proof_inputs = _hash_index(proof["input_hashes"])
    if proof_inputs.get("repository-identity") != canonical_sha256(
        proof["repository_identity"]
    ):
        _append(
            blockers,
            "evidence-only diff repository identity does not match input hashes",
        )
    if (
        proof_inputs.get("implementation-source-tree")
        != proof["source_tree_sha256_at_implementation"]
    ):
        _append(
            blockers,
            "evidence-only diff implementation source tree does not match input hashes",
        )
    if (
        proof_inputs.get("evidence-source-tree")
        != proof["source_tree_sha256_at_evidence"]
    ):
        _append(
            blockers,
            "evidence-only diff evidence source tree does not match input hashes",
        )
    if proof_outputs.get("diff") != proof["diff_sha256"]:
        _append(blockers, "evidence-only diff digest does not match output_hashes.diff")

    declared_command_ids = set(commands)
    if used_command_ids != declared_command_ids:
        unused = sorted(declared_command_ids - used_command_ids)
        unknown = sorted(used_command_ids - declared_command_ids)
        if unused:
            _append(blockers, f"immutable plan has commands without receipts or proof: {unused}")
        if unknown:
            _append(blockers, f"receipts or proof name commands outside immutable plan: {unknown}")

    all_inputs.extend(
        {"receipt_id": "revision-proof", **item} for item in proof["input_hashes"]
    )
    all_outputs.extend(
        {"receipt_id": "revision-proof", **item} for item in proof["output_hashes"]
    )
    all_inputs.sort(key=lambda item: (item["receipt_id"], item["name"]))
    all_outputs.sort(key=lambda item: (item["receipt_id"], item["name"]))
    expected_inputs_sha256 = canonical_sha256(all_inputs)
    expected_outputs_sha256 = canonical_sha256(all_outputs)
    if supplied_digests["inputs_sha256"] != expected_inputs_sha256:
        _append(blockers, "inputs_sha256 does not match receipt and proof input hashes")
    if supplied_digests["outputs_sha256"] != expected_outputs_sha256:
        _append(blockers, "outputs_sha256 does not match receipt and proof output hashes")

    ledger = {
        "schema_version": SCHEMA_VERSION,
        "implementation_revision": implementation_revision,
        "evidence_revision": evidence_revision,
        "entries": _ledger_entries(matrix),
        "revision_proof": {
            "repository_identity": proof["repository_identity"],
            "target": proof["target"],
            "profile": proof["profile"],
            "authority": proof["authority"],
            "plan_command_id": proof["plan_command_id"],
            "input_hashes": proof["input_hashes"],
            "output_hashes": proof["output_hashes"],
        },
    }
    expected_ledger_sha256 = canonical_sha256(ledger)
    if supplied_digests["authority_ledger_sha256"] != expected_ledger_sha256:
        _append(blockers, "authority_ledger_sha256 is not reproducible from normalized receipts")

    binding = {
        "relationship": relationship,
        "implementation_revision": binding_implementation,
        "evidence_revision": binding_evidence,
        "immutable_plan": plan,
        **supplied_digests,
        "evidence_only_diff": proof,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready" if not blockers else "blocked",
        "ready": not blockers,
        "current_source_sha": current_source_sha,
        "implementation_revision": implementation_revision,
        "evidence_revision": evidence_revision,
        "measured_source_sha": implementation_revision,
        "matrix": matrix,
        "revision_binding": binding,
        "authority_ledger": ledger,
        "derived_digests": {
            **expected_digests,
            "inputs_sha256": expected_inputs_sha256,
            "outputs_sha256": expected_outputs_sha256,
            "authority_ledger_sha256": expected_ledger_sha256,
        },
        "counts": {
            "source_dependent_receipts": source_dependent_count,
            "source_independent_receipts": source_independent_count,
            "total_receipts": source_dependent_count + source_independent_count,
        },
        "blockers": blockers,
    }


def read_packet(path: str) -> Any:
    try:
        if path == "-":
            return json.load(sys.stdin)
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise ContractError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ContractError(
            f"invalid JSON in {path}: line {error.lineno} column {error.colno}"
        ) from error


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a normalized delivery evidence epoch offline."
    )
    parser.add_argument("packet", help="JSON packet, or - for stdin")
    parser.add_argument(
        "--compact", action="store_true", help="emit compact JSON"
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        result = validate_packet(read_packet(args.packet))
    except ContractError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            separators=(",", ":") if args.compact else None,
            sort_keys=True,
        )
    )
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
