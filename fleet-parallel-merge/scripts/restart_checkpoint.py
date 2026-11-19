#!/usr/bin/env python3
"""Pure JSON checkpoint and resume validator for parallel change waves."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
CHECKPOINT_KIND = "parallel-wave-restart-checkpoint"
RESUME_KIND = "parallel-wave-resume-validation"
RECONSTRUCTION_KIND = "parallel-wave-fresh-reconstruction-plan"
SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]*$")
WORKER_STATES = {"pending", "running", "succeeded", "failed", "interrupted"}
LIVENESS_STATES = {"live", "lost"}
ARCHIVE_EVIDENCE_KIND = "archive-ref-resolution"
NEXT_ACTION_KINDS = {
    "archive_worker_commits",
    "authorize_cleanup",
    "await_workers",
    "cleanup_wave",
    "collect_worker_snapshots",
    "complete",
    "complete_wave",
    "continue_current_gate",
    "dispatch_next_wave",
    "finish_wave",
    "integrate_workers",
    "plan_cleanup",
    "run_acceptance_gate",
    "start_next_wave",
    "validate_workers",
}
GENERATED_ACTION_KINDS = {
    "allocate_fresh_resources",
    "recreate_worker",
    "resolve_blockers",
    "restore_archived_result",
}
FORBIDDEN_FIELD_PARTS = {
    "api_key",
    "apikey",
    "authorization",
    "cmd",
    "command",
    "cookie",
    "credential",
    "password",
    "secret",
    "shell",
    "token",
}
SHELL_FRAGMENTS = ("&&", "||", "$(", "`", "\n", "\r", "\x00")
TOKEN_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."),
)


class ContractError(ValueError):
    """Raised when a checkpoint document violates its JSON contract."""


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{field} must be an array")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    return value.strip()


def _schema_version(document: dict[str, Any], field: str) -> None:
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"{field}.schema_version must be {SCHEMA_VERSION}")


def _sha(value: Any, field: str) -> str:
    candidate = _string(value, field).lower()
    if not SHA_PATTERN.fullmatch(candidate):
        raise ContractError(f"{field} must be a full 40-character hexadecimal SHA")
    return candidate


def _sha256(value: Any, field: str) -> str:
    candidate = _string(value, field)
    if not SHA256_PATTERN.fullmatch(candidate):
        raise ContractError(f"{field} must be a lowercase SHA-256 digest")
    return candidate


def _identifier(value: Any, field: str) -> str:
    candidate = _string(value, field)
    if not ID_PATTERN.fullmatch(candidate):
        raise ContractError(
            f"{field} must use lowercase letters, digits, '.', '_', or '-'"
        )
    return candidate


def _absolute_path(value: Any, field: str) -> str:
    candidate = _string(value, field)
    if (
        not candidate.startswith("/")
        or candidate == "/"
        or candidate.endswith("/")
        or "//" in candidate
        or "\\" in candidate
    ):
        raise ContractError(f"{field} must be a normalized absolute POSIX path")
    if any(part in {"", ".", ".."} for part in candidate.split("/")[1:]):
        raise ContractError(f"{field} must not contain '.' or '..' segments")
    if any(fragment in candidate for fragment in SHELL_FRAGMENTS):
        raise ContractError(f"{field} contains unsafe control or shell syntax")
    return PurePosixPath(candidate).as_posix()


def _locator(value: Any, field: str) -> str:
    candidate = _string(value, field)
    if candidate.startswith("/"):
        return _absolute_path(candidate, field)
    if not REF_PATTERN.fullmatch(candidate):
        raise ContractError(f"{field} must be a safe durable locator")
    return candidate


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}/")


def _canonical_sha256(value: Any, field: str) -> str:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ContractError(f"{field} must contain only JSON values") from error
    return hashlib.sha256(rendered).hexdigest()


def _render_json(document: Any) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _rendered_sha256(document: Any) -> str:
    return hashlib.sha256(_render_json(document).encode("utf-8")).hexdigest()


def _safe_payload(value: Any, field: str) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        raise ContractError(f"{field} must not contain floating-point values")
    if isinstance(value, str):
        if any(fragment in value for fragment in SHELL_FRAGMENTS):
            raise ContractError(f"{field} contains raw shell syntax")
        if ";" in value or re.search(r"\s[|<>]\s", value):
            raise ContractError(f"{field} contains raw shell syntax")
        if any(pattern.search(value) for pattern in TOKEN_PATTERNS):
            raise ContractError(f"{field} contains token-like credential material")
        return value
    if isinstance(value, list):
        return [
            _safe_payload(item, f"{field}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str) or not raw_key:
                raise ContractError(f"{field} keys must be non-empty strings")
            key = raw_key.lower().replace("-", "_")
            parts = set(part for part in key.split("_") if part)
            if key in FORBIDDEN_FIELD_PARTS or parts & FORBIDDEN_FIELD_PARTS:
                raise ContractError(f"{field}.{raw_key} is a forbidden sensitive field")
            normalized[raw_key] = _safe_payload(item, f"{field}.{raw_key}")
        return normalized
    raise ContractError(f"{field} must contain only JSON values")


def _safe_action_payload(value: Any, field: str) -> Any:
    normalized = _safe_payload(value, field)
    if isinstance(normalized, str) and re.search(r"\s", normalized):
        raise ContractError(
            f"{field} must be a structured atom, not free-form command text"
        )
    if isinstance(normalized, list):
        return [
            _safe_action_payload(item, f"{field}[{index}]")
            for index, item in enumerate(normalized)
        ]
    if isinstance(normalized, dict):
        return {
            key: _safe_action_payload(item, f"{field}.{key}")
            for key, item in normalized.items()
        }
    return normalized


def _action(
    value: Any,
    field: str,
    *,
    allow_generated: bool = False,
) -> dict[str, Any]:
    source = _mapping(value, field)
    if set(source) != {"kind", "args"}:
        raise ContractError(f"{field} must contain exactly 'kind' and 'args'")
    kind = _identifier(source.get("kind"), f"{field}.kind")
    allowed = NEXT_ACTION_KINDS | (GENERATED_ACTION_KINDS if allow_generated else set())
    if kind not in allowed:
        raise ContractError(f"{field}.kind must be one of {sorted(allowed)}")
    args = _mapping(source.get("args"), f"{field}.args")
    return {"kind": kind, "args": _safe_action_payload(args, f"{field}.args")}


def _normalized_paths(value: Any, field: str) -> list[str]:
    paths = [
        _absolute_path(item, f"{field}[{index}]")
        for index, item in enumerate(_list(value, field))
    ]
    if not paths:
        raise ContractError(f"{field} must contain at least one path")
    if len(paths) != len(set(paths)):
        raise ContractError(f"{field} contains duplicate paths")
    paths.sort()
    for index, left in enumerate(paths):
        for right in paths[index + 1 :]:
            if _is_within(right, left):
                raise ContractError(
                    f"{field} contains overlapping paths '{left}' and '{right}'"
                )
    return paths


def _current(value: Any, field: str) -> dict[str, str]:
    source = _mapping(value, field)
    return {
        "phase": _identifier(source.get("phase"), f"{field}.phase"),
        "gate": _identifier(source.get("gate"), f"{field}.gate"),
    }


def _worker_ownership(value: Any, field: str) -> list[dict[str, Any]]:
    entries = _list(value, field)
    if not entries:
        raise ContractError(f"{field} must contain at least one worker")
    workers: list[dict[str, Any]] = []
    task_ids: set[str] = set()
    roots: set[str] = set()
    for index, raw_entry in enumerate(entries):
        item_field = f"{field}[{index}]"
        source = _mapping(raw_entry, item_field)
        task_id = _identifier(source.get("task_id"), f"{item_field}.task_id")
        if task_id in task_ids:
            raise ContractError(f"{field} contains duplicate task_id '{task_id}'")
        task_ids.add(task_id)
        root = _absolute_path(source.get("root"), f"{item_field}.root")
        if root in roots:
            raise ContractError(f"{field} contains duplicate root '{root}'")
        roots.add(root)
        state = _string(source.get("state"), f"{item_field}.state")
        if state not in WORKER_STATES:
            raise ContractError(
                f"{item_field}.state must be one of {sorted(WORKER_STATES)}"
            )
        raw_commit = source.get("unique_commit_sha")
        unique_commit_sha = (
            None
            if raw_commit is None
            else _sha(raw_commit, f"{item_field}.unique_commit_sha")
        )
        if state == "succeeded" and unique_commit_sha is None:
            raise ContractError(
                f"{item_field}.unique_commit_sha is required for succeeded workers"
            )
        workers.append(
            {
                "task_id": task_id,
                "root": root,
                "state": state,
                "unique_commit_sha": unique_commit_sha,
            }
        )
    return sorted(workers, key=lambda item: item["task_id"])


def _archive_refs(
    value: Any,
    field: str,
    *,
    observed: bool = False,
) -> list[dict[str, Any]]:
    archives: list[dict[str, Any]] = []
    archive_ids: set[str] = set()
    for index, raw_entry in enumerate(_list(value, field)):
        item_field = f"{field}[{index}]"
        source = _mapping(raw_entry, item_field)
        archive_id = _identifier(source.get("id"), f"{item_field}.id")
        if archive_id in archive_ids:
            raise ContractError(f"{field} contains duplicate id '{archive_id}'")
        archive_ids.add(archive_id)
        archive = {
            "id": archive_id,
            "task_id": _identifier(
                source.get("task_id"), f"{item_field}.task_id"
            ),
            "commit_sha": _sha(
                source.get("commit_sha"), f"{item_field}.commit_sha"
            ),
            "locator": _locator(source.get("locator"), f"{item_field}.locator"),
        }
        if observed:
            state = _string(source.get("state"), f"{item_field}.state")
            if state not in LIVENESS_STATES:
                raise ContractError(
                    f"{item_field}.state must be one of {sorted(LIVENESS_STATES)}"
                )
            archive["state"] = state
            raw_evidence = source.get("evidence")
            if state == "lost":
                if raw_evidence is not None:
                    raise ContractError(
                        f"{item_field}.evidence must be null when the archive is lost"
                    )
                archive["evidence"] = None
            elif raw_evidence is None:
                archive["evidence"] = None
            else:
                evidence = _mapping(raw_evidence, f"{item_field}.evidence")
                kind = _string(
                    evidence.get("kind"), f"{item_field}.evidence.kind"
                )
                if kind != ARCHIVE_EVIDENCE_KIND:
                    raise ContractError(
                        f"{item_field}.evidence.kind must be "
                        f"'{ARCHIVE_EVIDENCE_KIND}'"
                    )
                archive["evidence"] = {
                    "kind": kind,
                    "locator": _locator(
                        evidence.get("locator"),
                        f"{item_field}.evidence.locator",
                    ),
                    "resolved_commit_sha": _sha(
                        evidence.get("resolved_commit_sha"),
                        f"{item_field}.evidence.resolved_commit_sha",
                    ),
                    "sha256": _sha256(
                        evidence.get("sha256"),
                        f"{item_field}.evidence.sha256",
                    ),
                }
        archives.append(archive)
    return sorted(archives, key=lambda item: item["id"])


def _resource_manifest(
    value: Any,
    field: str,
    *,
    observed: bool = False,
) -> dict[str, Any]:
    source = _mapping(value, field)
    result: dict[str, Any] = {
        "locator": _absolute_path(source.get("locator"), f"{field}.locator"),
    }
    if observed:
        state = _string(source.get("state"), f"{field}.state")
        if state not in LIVENESS_STATES:
            raise ContractError(
                f"{field}.state must be one of {sorted(LIVENESS_STATES)}"
            )
        result["state"] = state
        raw_digest = source.get("sha256")
        result["sha256"] = (
            None if raw_digest is None else _sha256(raw_digest, f"{field}.sha256")
        )
        if state == "live" and result["sha256"] is None:
            raise ContractError(f"{field}.sha256 is required when the manifest is live")
    else:
        result["sha256"] = _sha256(source.get("sha256"), f"{field}.sha256")
    return result


def _assert_outside_disposable(
    path: str,
    field: str,
    non_durable_paths: list[str],
) -> None:
    for disposable in non_durable_paths:
        if _is_within(path, disposable):
            raise ContractError(f"{field} must live outside non_durable_paths")


def _validate_archive_coverage(
    workers: list[dict[str, Any]],
    archives: list[dict[str, Any]],
) -> None:
    workers_by_task = {worker["task_id"]: worker for worker in workers}
    covered: set[tuple[str, str]] = set()
    for archive in archives:
        worker = workers_by_task.get(archive["task_id"])
        if worker is None:
            raise ContractError(
                "durable_archive_refs contains an archive for unknown task "
                f"'{archive['task_id']}'"
            )
        if worker["unique_commit_sha"] != archive["commit_sha"]:
            raise ContractError(
                f"durable archive '{archive['id']}' does not match task "
                f"'{archive['task_id']}' unique commit"
            )
        key = (archive["task_id"], archive["commit_sha"])
        if key in covered:
            raise ContractError(
                f"unique commit for task '{archive['task_id']}' has multiple archives"
            )
        covered.add(key)
    for worker in workers:
        commit_sha = worker["unique_commit_sha"]
        if commit_sha is not None and (worker["task_id"], commit_sha) not in covered:
            raise ContractError(
                f"unique commit for task '{worker['task_id']}' lacks a durable archive ref"
            )


def _plan(value: Any, field: str) -> tuple[dict[str, Any], str, str]:
    plan = _mapping(value, field)
    _schema_version(plan, field)
    if plan.get("kind") != "parallel-change-plan":
        raise ContractError(f"{field}.kind must be 'parallel-change-plan'")
    base_sha = _sha(plan.get("base_sha"), f"{field}.base_sha")
    return plan, base_sha, _canonical_sha256(plan, field)


def build_checkpoint(document: Any) -> dict[str, Any]:
    """Build a deterministic, command-free restart checkpoint receipt."""
    root = _mapping(document, "checkpoint input")
    _schema_version(root, "checkpoint input")
    plan, plan_base_sha, plan_sha256 = _plan(root.get("plan"), "plan")
    base_sha = _sha(root.get("base_sha"), "base_sha")
    if base_sha != plan_base_sha:
        raise ContractError("base_sha does not match plan.base_sha")

    current_value = root.get("current")
    if current_value is None and (
        "current_phase" in root or "current_gate" in root
    ):
        current_value = {
            "phase": root.get("current_phase"),
            "gate": root.get("current_gate"),
        }
    current = _current(current_value, "current")
    workers = _worker_ownership(root.get("worker_ownership"), "worker_ownership")
    archives = _archive_refs(
        root.get("durable_archive_refs"), "durable_archive_refs"
    )
    _validate_archive_coverage(workers, archives)
    manifest = _resource_manifest(root.get("resource_manifest"), "resource_manifest")
    non_durable_paths = _normalized_paths(
        root.get("non_durable_paths"), "non_durable_paths"
    )
    checkpoint_path = _absolute_path(root.get("checkpoint_path"), "checkpoint_path")
    state_path = _absolute_path(root.get("state_path"), "state_path")
    next_action = _action(root.get("next_action"), "next_action")

    if not _is_within(checkpoint_path, state_path):
        raise ContractError("checkpoint_path must be within state_path")
    if not _is_within(manifest["locator"], state_path):
        raise ContractError("resource_manifest.locator must be within state_path")
    _assert_outside_disposable(
        checkpoint_path, "checkpoint_path", non_durable_paths
    )
    _assert_outside_disposable(state_path, "state_path", non_durable_paths)
    _assert_outside_disposable(
        manifest["locator"], "resource_manifest.locator", non_durable_paths
    )
    for archive in archives:
        if archive["locator"].startswith("/"):
            _assert_outside_disposable(
                archive["locator"],
                f"durable archive '{archive['id']}' locator",
                non_durable_paths,
            )
    for worker in workers:
        if worker["root"] not in non_durable_paths:
            raise ContractError(
                f"worker '{worker['task_id']}' root must be listed in non_durable_paths"
            )

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "kind": CHECKPOINT_KIND,
        "cursor_owner": "iteration-root-packet",
        "recovery_entry": {"kind": "resume_validate", "checkpoint_path": checkpoint_path},
        "base_sha": base_sha,
        "plan_sha256": plan_sha256,
        "current": current,
        "worker_ownership": workers,
        "durable_archive_refs": archives,
        "resource_manifest": manifest,
        "non_durable_paths": non_durable_paths,
        "checkpoint_path": checkpoint_path,
        "state_path": state_path,
        "next_action": next_action,
    }
    if "predecessor_checkpoint_sha256" in root:
        receipt["predecessor_checkpoint_sha256"] = _sha256(
            root["predecessor_checkpoint_sha256"], "predecessor_checkpoint_sha256"
        )
    _safe_payload(receipt, "checkpoint receipt")
    return receipt


def _checkpoint_receipt(value: Any) -> dict[str, Any]:
    source = _mapping(value, "checkpoint")
    _schema_version(source, "checkpoint")
    if source.get("kind") != CHECKPOINT_KIND:
        raise ContractError(f"checkpoint.kind must be '{CHECKPOINT_KIND}'")
    base_sha = _sha(source.get("base_sha"), "checkpoint.base_sha")
    plan_sha256 = _sha256(
        source.get("plan_sha256"), "checkpoint.plan_sha256"
    )
    current = _current(source.get("current"), "checkpoint.current")
    workers = _worker_ownership(
        source.get("worker_ownership"), "checkpoint.worker_ownership"
    )
    archives = _archive_refs(
        source.get("durable_archive_refs"),
        "checkpoint.durable_archive_refs",
    )
    _validate_archive_coverage(workers, archives)
    manifest = _resource_manifest(
        source.get("resource_manifest"), "checkpoint.resource_manifest"
    )
    non_durable_paths = _normalized_paths(
        source.get("non_durable_paths"), "checkpoint.non_durable_paths"
    )
    checkpoint_path = _absolute_path(
        source.get("checkpoint_path"), "checkpoint.checkpoint_path"
    )
    state_path = _absolute_path(source.get("state_path"), "checkpoint.state_path")
    if source.get("cursor_owner") != "iteration-root-packet":
        raise ContractError("checkpoint.cursor_owner must be iteration-root-packet")
    if source.get("recovery_entry") != {"kind": "resume_validate", "checkpoint_path": checkpoint_path}:
        raise ContractError("checkpoint.recovery_entry must name this checkpoint's resume_validate entry")
    next_action = _action(source.get("next_action"), "checkpoint.next_action")
    predecessor = {}
    if "predecessor_checkpoint_sha256" in source:
        predecessor["predecessor_checkpoint_sha256"] = _sha256(
            source["predecessor_checkpoint_sha256"], "checkpoint.predecessor_checkpoint_sha256"
        )
    return {
        **predecessor,
        "schema_version": SCHEMA_VERSION,
        "kind": CHECKPOINT_KIND,
        "cursor_owner": "iteration-root-packet",
        "recovery_entry": {"kind": "resume_validate", "checkpoint_path": checkpoint_path},
        "base_sha": base_sha,
        "plan_sha256": plan_sha256,
        "current": current,
        "worker_ownership": workers,
        "durable_archive_refs": archives,
        "resource_manifest": manifest,
        "non_durable_paths": non_durable_paths,
        "checkpoint_path": checkpoint_path,
        "state_path": state_path,
        "next_action": next_action,
    }


def _observed_roots(value: Any, field: str) -> list[dict[str, str]]:
    roots: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw_entry in enumerate(_list(value, field)):
        item_field = f"{field}[{index}]"
        source = _mapping(raw_entry, item_field)
        path = _absolute_path(source.get("path"), f"{item_field}.path")
        if path in seen:
            raise ContractError(f"{field} contains duplicate path '{path}'")
        seen.add(path)
        state = _string(source.get("state"), f"{item_field}.state")
        if state not in LIVENESS_STATES:
            raise ContractError(
                f"{item_field}.state must be one of {sorted(LIVENESS_STATES)}"
            )
        roots.append({"path": path, "state": state})
    return sorted(roots, key=lambda item: item["path"])


def _observed_workers(value: Any, field: str) -> list[dict[str, Any]]:
    workers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_entry in enumerate(_list(value, field)):
        item_field = f"{field}[{index}]"
        source = _mapping(raw_entry, item_field)
        task_id = _identifier(source.get("task_id"), f"{item_field}.task_id")
        if task_id in seen:
            raise ContractError(f"{field} contains duplicate task_id '{task_id}'")
        seen.add(task_id)
        state = _string(source.get("state"), f"{item_field}.state")
        if state not in WORKER_STATES:
            raise ContractError(
                f"{item_field}.state must be one of {sorted(WORKER_STATES)}"
            )
        raw_commit = source.get("unique_commit_sha")
        workers.append(
            {
                "task_id": task_id,
                "root": _absolute_path(source.get("root"), f"{item_field}.root"),
                "state": state,
                "unique_commit_sha": (
                    None
                    if raw_commit is None
                    else _sha(raw_commit, f"{item_field}.unique_commit_sha")
                ),
            }
        )
    return sorted(workers, key=lambda item: item["task_id"])


def _blocker(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def _append_blocker(
    blockers: list[dict[str, str]],
    code: str,
    detail: str,
) -> None:
    entry = _blocker(code, detail)
    if entry not in blockers:
        blockers.append(entry)


def _observed_plan(
    observed: dict[str, Any],
    blockers: list[dict[str, str]],
) -> tuple[str, str]:
    observed_base_sha = _sha(observed.get("base_sha"), "observed.base_sha")
    claimed_digest = _sha256(
        observed.get("plan_sha256"), "observed.plan_sha256"
    )
    _, plan_base_sha, derived_digest = _plan(observed.get("plan"), "observed.plan")
    if plan_base_sha != observed_base_sha:
        _append_blocker(
            blockers,
            "observed_plan_base_mismatch",
            "observed plan base SHA does not match observed base SHA",
        )
    if derived_digest != claimed_digest:
        _append_blocker(
            blockers,
            "observed_plan_hash_invalid",
            "observed plan bytes do not match observed plan_sha256",
        )
    return observed_base_sha, claimed_digest


def _fresh_reconstruction_plan(
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    archives_by_task = {
        archive["task_id"]: archive
        for archive in checkpoint["durable_archive_refs"]
    }
    worker_actions: list[dict[str, Any]] = []
    for worker in checkpoint["worker_ownership"]:
        archive = archives_by_task.get(worker["task_id"])
        if archive is not None:
            action = {
                "kind": "restore_archived_result",
                "args": {
                    "task_id": worker["task_id"],
                    "commit_sha": archive["commit_sha"],
                    "archive_ref_id": archive["id"],
                    "archive_locator": archive["locator"],
                },
            }
        else:
            action = {
                "kind": "recreate_worker",
                "args": {
                    "task_id": worker["task_id"],
                    "base_sha": checkpoint["base_sha"],
                },
            }
        worker_actions.append(_action(action, "worker action", allow_generated=True))

    plan = {
        "schema_version": SCHEMA_VERSION,
        "kind": RECONSTRUCTION_KIND,
        "base_sha": checkpoint["base_sha"],
        "plan_sha256": checkpoint["plan_sha256"],
        "resume_phase": checkpoint["current"]["phase"],
        "resume_gate": checkpoint["current"]["gate"],
        "previous_manifest": checkpoint["resource_manifest"],
        "do_not_reuse_paths": checkpoint["non_durable_paths"],
        "worker_actions": worker_actions,
        "checkpoint_next_action": checkpoint["next_action"],
    }
    _safe_payload(plan, "fresh reconstruction plan")
    return plan


def validate_resume(document: Any) -> dict[str, Any]:
    """Classify a supplied observed snapshot without inspecting the host."""
    root = _mapping(document, "resume input")
    _schema_version(root, "resume input")
    raw_checkpoint = root.get("checkpoint")
    checkpoint = _checkpoint_receipt(raw_checkpoint)
    observed = _mapping(root.get("observed"), "observed")
    _schema_version(observed, "observed")
    blockers: list[dict[str, str]] = []

    observed_checkpoint_sha256 = _sha256(
        observed.get("checkpoint_sha256"), "observed.checkpoint_sha256"
    )
    if observed_checkpoint_sha256 != _rendered_sha256(raw_checkpoint):
        _append_blocker(
            blockers,
            "checkpoint_hash_mismatch",
            "observed checkpoint bytes hash does not match the checkpoint document",
        )

    observed_base_sha, observed_plan_sha256 = _observed_plan(observed, blockers)
    if observed_base_sha != checkpoint["base_sha"]:
        _append_blocker(
            blockers,
            "base_sha_mismatch",
            "observed base SHA does not match checkpoint base SHA",
        )
    if observed_plan_sha256 != checkpoint["plan_sha256"]:
        _append_blocker(
            blockers,
            "plan_hash_mismatch",
            "observed plan hash does not match checkpoint plan hash",
        )

    observed_checkpoint_path = _absolute_path(
        observed.get("checkpoint_path"), "observed.checkpoint_path"
    )
    observed_state_path = _absolute_path(
        observed.get("state_path"), "observed.state_path"
    )
    if observed_checkpoint_path != checkpoint["checkpoint_path"]:
        _append_blocker(
            blockers,
            "checkpoint_path_mismatch",
            "observed checkpoint path does not match checkpoint receipt",
        )
    if observed_state_path != checkpoint["state_path"]:
        _append_blocker(
            blockers,
            "state_path_mismatch",
            "observed state path does not match checkpoint receipt",
        )
    if not _is_within(checkpoint["checkpoint_path"], checkpoint["state_path"]):
        _append_blocker(
            blockers,
            "checkpoint_path_outside_state",
            "checkpoint path is outside the durable state path",
        )
    if not _is_within(
        checkpoint["resource_manifest"]["locator"], checkpoint["state_path"]
    ):
        _append_blocker(
            blockers,
            "manifest_path_outside_state",
            "resource manifest locator is outside the durable state path",
        )
    for field, path in (
        ("checkpoint_path", observed_checkpoint_path),
        ("state_path", observed_state_path),
        ("checkpoint_path", checkpoint["checkpoint_path"]),
        ("state_path", checkpoint["state_path"]),
        (
            "resource_manifest.locator",
            checkpoint["resource_manifest"]["locator"],
        ),
    ):
        for disposable in checkpoint["non_durable_paths"]:
            if _is_within(path, disposable):
                _append_blocker(
                    blockers,
                    "state_inside_disposable_root",
                    f"{field} is within non-durable path '{disposable}'",
                )

    non_durable_paths = set(checkpoint["non_durable_paths"])
    for worker in checkpoint["worker_ownership"]:
        if worker["root"] not in non_durable_paths:
            _append_blocker(
                blockers,
                "worker_root_not_non_durable",
                f"worker '{worker['task_id']}' root is not declared non-durable",
            )
    for archive in checkpoint["durable_archive_refs"]:
        locator = archive["locator"]
        if not locator.startswith("/"):
            continue
        for disposable in checkpoint["non_durable_paths"]:
            if _is_within(locator, disposable):
                _append_blocker(
                    blockers,
                    "archive_inside_non_durable_root",
                    f"durable archive '{archive['id']}' is within '{disposable}'",
                )

    observed_manifest = _resource_manifest(
        observed.get("resource_manifest"),
        "observed.resource_manifest",
        observed=True,
    )
    if observed_manifest["locator"] != checkpoint["resource_manifest"]["locator"]:
        _append_blocker(
            blockers,
            "manifest_locator_mismatch",
            "observed manifest locator does not match checkpoint",
        )
    if (
        observed_manifest["state"] == "live"
        and observed_manifest["sha256"] != checkpoint["resource_manifest"]["sha256"]
    ):
        _append_blocker(
            blockers,
            "manifest_hash_mismatch",
            "live resource manifest hash does not match checkpoint",
        )
    if (
        observed_manifest["state"] == "lost"
        and observed_manifest["sha256"] is not None
        and observed_manifest["sha256"]
        != checkpoint["resource_manifest"]["sha256"]
    ):
        _append_blocker(
            blockers,
            "lost_manifest_hash_mismatch",
            "lost manifest observation carries a conflicting hash",
        )

    observed_roots = _observed_roots(observed.get("roots"), "observed.roots")
    observed_root_paths = {item["path"] for item in observed_roots}
    expected_root_paths = set(checkpoint["non_durable_paths"])
    for missing in sorted(expected_root_paths - observed_root_paths):
        _append_blocker(
            blockers,
            "missing_root_observation",
            f"non-durable path lacks an observed root state: {missing}",
        )
    for unexpected in sorted(observed_root_paths - expected_root_paths):
        _append_blocker(
            blockers,
            "unexpected_root_observation",
            f"observed root is not checkpoint-owned: {unexpected}",
        )

    expected_archives = {
        archive["id"]: archive for archive in checkpoint["durable_archive_refs"]
    }
    observed_archives = {
        archive["id"]: archive
        for archive in _archive_refs(
            observed.get("durable_archive_refs"),
            "observed.durable_archive_refs",
            observed=True,
        )
    }
    for archive_id, expected in expected_archives.items():
        actual = observed_archives.get(archive_id)
        if actual is None:
            _append_blocker(
                blockers,
                "archive_missing",
                f"durable archive '{archive_id}' was not observed",
            )
            continue
        if any(
            actual[field] != expected[field]
            for field in ("task_id", "commit_sha", "locator")
        ):
            _append_blocker(
                blockers,
                "archive_identity_mismatch",
                f"durable archive '{archive_id}' identity changed",
            )
        if actual["state"] != "live":
            _append_blocker(
                blockers,
                "archive_missing",
                f"durable archive '{archive_id}' is lost",
            )
            continue
        evidence = actual["evidence"]
        if evidence is None:
            _append_blocker(
                blockers,
                "archive_evidence_missing",
                f"durable archive '{archive_id}' lacks resolution evidence",
            )
            continue
        evidence_claim = {
            "kind": evidence["kind"],
            "locator": evidence["locator"],
            "resolved_commit_sha": evidence["resolved_commit_sha"],
        }
        if evidence["sha256"] != _canonical_sha256(
            evidence_claim, f"archive evidence '{archive_id}'"
        ):
            _append_blocker(
                blockers,
                "archive_evidence_hash_mismatch",
                f"durable archive '{archive_id}' evidence hash is invalid",
            )
        if (
            evidence["locator"] != expected["locator"]
            or evidence["resolved_commit_sha"] != expected["commit_sha"]
        ):
            _append_blocker(
                blockers,
                "archive_evidence_mismatch",
                f"durable archive '{archive_id}' evidence does not prove its commit",
            )

    observed_workers = _observed_workers(
        observed.get("workers"), "observed.workers"
    )
    observed_workers_by_task = {
        worker["task_id"]: worker for worker in observed_workers
    }
    checkpoint_workers_by_task = {
        worker["task_id"]: worker for worker in checkpoint["worker_ownership"]
    }
    roots_by_path = {root["path"]: root["state"] for root in observed_roots}
    for task_id, worker in checkpoint_workers_by_task.items():
        root_state = roots_by_path.get(worker["root"])
        actual = observed_workers_by_task.get(task_id)
        if root_state == "live" and actual is None:
            _append_blocker(
                blockers,
                "missing_live_worker_observation",
                f"live worker root for task '{task_id}' lacks worker state",
            )
            continue
        if root_state == "lost" and actual is not None:
            code = (
                "stale_worker_success"
                if actual["state"] == "succeeded"
                else "worker_observed_at_lost_root"
            )
            _append_blocker(
                blockers,
                code,
                f"task '{task_id}' reports state from a lost root",
            )
            continue
        if actual is None:
            continue
        if actual["root"] != worker["root"]:
            _append_blocker(
                blockers,
                "worker_root_mismatch",
                f"task '{task_id}' root does not match checkpoint ownership",
            )
        if (
            actual["state"] == "succeeded"
            and (
                worker["state"] != "succeeded"
                or actual["unique_commit_sha"] != worker["unique_commit_sha"]
            )
        ):
            _append_blocker(
                blockers,
                "stale_worker_success",
                f"task '{task_id}' has uncheckpointed success state",
            )
        elif (
            actual["state"] != worker["state"]
            or actual["unique_commit_sha"] != worker["unique_commit_sha"]
        ):
            _append_blocker(
                blockers,
                "worker_state_mismatch",
                f"task '{task_id}' state does not match checkpoint",
            )
    for task_id in sorted(
        set(observed_workers_by_task) - set(checkpoint_workers_by_task)
    ):
        actual = observed_workers_by_task[task_id]
        code = (
            "stale_worker_success"
            if actual["state"] == "succeeded"
            else "unexpected_worker_observation"
        )
        _append_blocker(
            blockers,
            code,
            f"observed worker '{task_id}' is not checkpoint-owned",
        )

    root_states = {
        item["state"]
        for item in observed_roots
        if item["path"] in expected_root_paths
    }
    manifest_state = observed_manifest["state"]
    if root_states == {"live"} and manifest_state == "lost":
        _append_blocker(
            blockers,
            "manifest_lost_with_live_roots",
            "resource manifest is lost while old non-durable roots remain",
        )
    elif root_states == {"lost"} and manifest_state == "live":
        _append_blocker(
            blockers,
            "manifest_live_with_lost_roots",
            "resource manifest is live while all non-durable roots are lost",
        )
    elif len(root_states) > 1:
        _append_blocker(
            blockers,
            "mixed_root_liveness",
            "non-durable roots are only partially present",
        )

    blockers.sort(key=lambda item: (item["code"], item["detail"]))
    reconstruction_plan: dict[str, Any] | None = None
    if blockers:
        status = "blocked"
        next_action = {
            "kind": "resolve_blockers",
            "args": {
                "blocker_codes": sorted({item["code"] for item in blockers}),
            },
        }
    elif manifest_state == "live" and root_states == {"live"}:
        status = "resume_existing"
        next_action = checkpoint["next_action"]
    elif manifest_state == "lost" and root_states == {"lost"}:
        status = "reconstruct_fresh"
        reconstruction_plan = _fresh_reconstruction_plan(checkpoint)
        next_action = {
            "kind": "allocate_fresh_resources",
            "args": {
                "task_ids": [
                    worker["task_id"] for worker in checkpoint["worker_ownership"]
                ],
                "excluded_paths": checkpoint["non_durable_paths"],
                "previous_manifest_locator": checkpoint["resource_manifest"][
                    "locator"
                ],
            },
        }
    else:
        status = "blocked"
        blockers = [
            _blocker(
                "incomplete_liveness_evidence",
                "manifest and root liveness do not prove a resumable state",
            )
        ]
        next_action = {
            "kind": "resolve_blockers",
            "args": {"blocker_codes": ["incomplete_liveness_evidence"]},
        }

    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESUME_KIND,
        "cursor_owner": "iteration-root-packet",
        "checkpoint_sha256": _rendered_sha256(raw_checkpoint),
        "status": status,
        "base_sha": checkpoint["base_sha"],
        "plan_sha256": checkpoint["plan_sha256"],
        "current": checkpoint["current"],
        "blockers": blockers,
        "next_action": _action(
            next_action, "resume next_action", allow_generated=True
        ),
        "fresh_reconstruction_plan": reconstruction_plan,
    }
    _safe_payload(result, "resume validation")
    return result


def _load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _write_json(document: Any, path: str | None, *, exclusive: bool = False) -> None:
    rendered = _render_json(document)
    if path is None or path == "-":
        if exclusive:
            raise ContractError("checkpoint --output must be an absolute file path")
        sys.stdout.write(rendered)
        return
    output_path = Path(path)
    mode = "x" if exclusive else "w"
    with output_path.open(mode, encoding="utf-8") as stream:
        if exclusive:
            output_path.chmod(0o600)
        stream.write(rendered)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build or validate pure JSON parallel-wave restart checkpoints."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    checkpoint = subparsers.add_parser("checkpoint")
    checkpoint.add_argument("--input", required=True, help="JSON file or '-'")
    checkpoint.add_argument(
        "--output",
        help="Required new receipt path; must equal checkpoint_path in the input",
    )
    resume = subparsers.add_parser("resume-validate")
    resume.add_argument("--input", required=True, help="JSON file or '-'")
    resume.add_argument("--output", help="JSON file or '-' for stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        source = _load_json(arguments.input)
        if arguments.command == "checkpoint":
            result = build_checkpoint(source)
            if arguments.output is None:
                raise ContractError("checkpoint --output is required")
            output_path = _absolute_path(arguments.output, "checkpoint --output")
            if output_path != result["checkpoint_path"]:
                raise ContractError(
                    "checkpoint --output must equal the receipt checkpoint_path"
                )
            _write_json(result, output_path, exclusive=True)
            exit_code = 0
        else:
            result = validate_resume(source)
            exit_code = 0 if result["status"] != "blocked" else 1
    except (ContractError, json.JSONDecodeError, OSError) as error:
        result = {
            "schema_version": SCHEMA_VERSION,
            "kind": f"parallel-wave-{arguments.command}-error",
            "valid": False,
            "errors": [str(error)],
        }
        exit_code = 2
        _write_json(result, None)
        return exit_code
    if arguments.command != "checkpoint":
        _write_json(result, arguments.output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
