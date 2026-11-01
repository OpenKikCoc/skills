#!/usr/bin/env python3
"""Advise bounded replacement and single-owner convergence for a stalled slice."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
ACCEPTED_FOLLOWUP_LIMIT = 2
UNCHANGED_INTENT_REPLACEMENT_LIMIT = 1
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
HOST_STATES = {"accepted", "completed", "idle", "running", "unknown"}
WRITER_STATES = {"running", "stopped", "unknown"}

PLAN_SCRIPT = Path(__file__).with_name("parallel_merge.py")
PLAN_SPEC = importlib.util.spec_from_file_location(
    "parallel_merge_contract", PLAN_SCRIPT
)
assert PLAN_SPEC and PLAN_SPEC.loader
PLAN_CONTRACT = importlib.util.module_from_spec(PLAN_SPEC)
PLAN_SPEC.loader.exec_module(PLAN_CONTRACT)


class ContractError(ValueError):
    """Raised when worker-liveness evidence violates the contract."""


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    return value


def _exact_keys(
    value: dict[str, Any],
    field: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required - (optional or set()))
    if missing:
        raise ContractError(f"{field} is missing fields: {', '.join(missing)}")
    if extra:
        raise ContractError(f"{field} has unsupported fields: {', '.join(extra)}")


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise ContractError(f"{field} must not have surrounding whitespace")
    return value


def _identifier(value: Any, field: str) -> str:
    candidate = _string(value, field)
    if not IDENTIFIER.fullmatch(candidate):
        raise ContractError(f"{field} must be a stable lowercase identifier")
    return candidate


def _sha(value: Any, field: str) -> str:
    candidate = _string(value, field)
    if not SHA.fullmatch(candidate):
        raise ContractError(f"{field} must be a lowercase full Git SHA")
    return candidate


def _sha256(value: Any, field: str) -> str:
    candidate = _string(value, field)
    if not SHA256.fullmatch(candidate):
        raise ContractError(f"{field} must be a lowercase SHA-256 digest")
    return candidate


def _optional_sha256(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, field)


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{field} must be a boolean")
    return value


def _absolute_root(value: Any, field: str) -> str:
    candidate = _string(value, field)
    if (
        not candidate.startswith("/")
        or candidate == "/"
        or candidate.endswith("/")
        or "//" in candidate
        or "\\" in candidate
        or "\x00" in candidate
        or "\n" in candidate
        or "\r" in candidate
    ):
        raise ContractError(f"{field} must be a normalized absolute POSIX path")
    if any(part in {"", ".", ".."} for part in candidate.split("/")[1:]):
        raise ContractError(f"{field} must not contain '.' or '..' segments")
    return PurePosixPath(candidate).as_posix()


def _roots_overlap(left: str, right: str) -> bool:
    return (
        left == right
        or left.startswith(f"{right}/")
        or right.startswith(f"{left}/")
    )


def _plan_digest(plan: dict[str, Any]) -> str:
    payload = json.dumps(plan, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _intent_digest(
    *,
    plan_sha256: str,
    task_id: str,
    base_sha: str,
    write_set: list[str],
    substrate: str,
) -> str:
    payload = json.dumps(
        {
            "base_sha": base_sha,
            "plan_sha256": plan_sha256,
            "substrate": substrate,
            "task_id": task_id,
            "write_set": write_set,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _replacement_count(value: Any) -> int:
    if type(value) is not int or not (
        0 <= value <= UNCHANGED_INTENT_REPLACEMENT_LIMIT
    ):
        raise ContractError(
            "unchanged_intent_replacement_count must be 0 or 1"
        )
    return value


def _single_owner_fallback(
    value: Any, intent_sha256: str
) -> dict[str, str] | None:
    if value is None:
        return None
    owner = _object(value, "single_owner_fallback")
    _exact_keys(
        owner,
        "single_owner_fallback",
        {"owner_id", "intent_sha256", "authority_receipt_sha256"},
    )
    owner_id = _identifier(owner["owner_id"], "single_owner_fallback.owner_id")
    owner_intent = _sha256(
        owner["intent_sha256"], "single_owner_fallback.intent_sha256"
    )
    if owner_intent != intent_sha256:
        raise ContractError(
            "single_owner_fallback must bind the unchanged assignment intent"
        )
    return {
        "owner_id": owner_id,
        "intent_sha256": owner_intent,
        "authority_receipt_sha256": _sha256(
            owner["authority_receipt_sha256"],
            "single_owner_fallback.authority_receipt_sha256",
        ),
    }


def _snapshot(
    value: Any, field: str, execution_id: str, worktree_root: str
) -> dict[str, Any]:
    snapshot = _object(value, field)
    _exact_keys(
        snapshot,
        field,
        {
            "execution_id",
            "worktree_root",
            "head_sha",
            "file_state_sha256",
            "receipt_state_sha256",
        },
    )
    observed_execution_id = _identifier(
        snapshot["execution_id"], f"{field}.execution_id"
    )
    observed_root = _absolute_root(
        snapshot["worktree_root"], f"{field}.worktree_root"
    )
    if observed_execution_id != execution_id:
        raise ContractError(f"{field} is not bound to the old execution_id")
    if observed_root != worktree_root:
        raise ContractError(f"{field} is not from the assigned old worktree")
    return {
        "execution_id": observed_execution_id,
        "worktree_root": observed_root,
        "head_sha": _sha(snapshot["head_sha"], f"{field}.head_sha"),
        "file_state_sha256": _sha256(
            snapshot["file_state_sha256"], f"{field}.file_state_sha256"
        ),
        "receipt_state_sha256": _optional_sha256(
            snapshot["receipt_state_sha256"],
            f"{field}.receipt_state_sha256",
        ),
    }


def _acknowledgement(
    value: Any, field: str, execution_id: str, task_id: str
) -> dict[str, str] | None:
    if value is None:
        return None
    acknowledgement = _object(value, field)
    _exact_keys(acknowledgement, field, {"ack_id", "execution_id", "task_id"})
    ack = {
        "ack_id": _identifier(acknowledgement["ack_id"], f"{field}.ack_id"),
        "execution_id": _identifier(
            acknowledgement["execution_id"], f"{field}.execution_id"
        ),
        "task_id": _identifier(acknowledgement["task_id"], f"{field}.task_id"),
    }
    if ack["execution_id"] != execution_id or ack["task_id"] != task_id:
        raise ContractError(
            f"{field} must acknowledge the old execution and assigned task"
        )
    return ack


def _bound_receipt(
    value: Any, field: str, execution_id: str, worktree_root: str
) -> dict[str, str] | None:
    if value is None:
        return None
    receipt = _object(value, field)
    _exact_keys(
        receipt, field, {"execution_id", "worktree_root", "receipt_sha256"}
    )
    observed_execution_id = _identifier(
        receipt["execution_id"], f"{field}.execution_id"
    )
    observed_root = _absolute_root(
        receipt["worktree_root"], f"{field}.worktree_root"
    )
    if observed_execution_id != execution_id or observed_root != worktree_root:
        raise ContractError(f"{field} must bind the old execution and worktree")
    return {
        "execution_id": observed_execution_id,
        "worktree_root": observed_root,
        "receipt_sha256": _sha256(
            receipt["receipt_sha256"], f"{field}.receipt_sha256"
        ),
    }


def advise(document: dict[str, Any]) -> dict[str, Any]:
    root = _object(document, "input")
    _exact_keys(
        root,
        "input",
        {
            "schema_version",
            "plan",
            "plan_sha256",
            "task_id",
            "old_execution",
            "review_boundary",
            "old_execution_recovery",
            "replacement_execution",
        },
        {
            "unchanged_intent_replacement_count",
            "single_owner_fallback",
        },
    )
    if root["schema_version"] != SCHEMA_VERSION:
        raise ContractError(f"schema_version must be {SCHEMA_VERSION}")

    try:
        plan = PLAN_CONTRACT.validate_plan(root["plan"])
    except PLAN_CONTRACT.ContractError as error:
        raise ContractError(f"plan is invalid: {error}") from error
    plan_sha256 = _sha256(root["plan_sha256"], "plan_sha256")
    if plan_sha256 != _plan_digest(plan):
        raise ContractError("plan_sha256 does not match the validated plan")
    task_id = _identifier(root["task_id"], "task_id")
    tasks = {task["id"]: task for task in plan["tasks"]}
    if task_id not in tasks:
        raise ContractError("task_id does not identify a task in the plan")
    substrate = plan.get("worker_substrate")
    if substrate is None:
        raise ContractError("plan.worker_substrate must be sealed")
    task = tasks[task_id]

    old = _object(root["old_execution"], "old_execution")
    _exact_keys(
        old,
        "old_execution",
        {
            "execution_id",
            "task_id",
            "plan_sha256",
            "base_sha",
            "write_set",
            "substrate",
            "worktree_root",
        },
    )
    execution_id = _identifier(old["execution_id"], "old_execution.execution_id")
    old_task_id = _identifier(old["task_id"], "old_execution.task_id")
    old_plan_sha256 = _sha256(old["plan_sha256"], "old_execution.plan_sha256")
    old_base_sha = _sha(old["base_sha"], "old_execution.base_sha")
    old_substrate = _identifier(old["substrate"], "old_execution.substrate")
    old_root = _absolute_root(old["worktree_root"], "old_execution.worktree_root")
    if not isinstance(old["write_set"], list) or not all(
        isinstance(item, str) for item in old["write_set"]
    ):
        raise ContractError("old_execution.write_set must be an array of strings")
    if (
        old_task_id != task_id
        or old_plan_sha256 != plan_sha256
        or old_base_sha != plan["base_sha"]
        or old["write_set"] != task["write_set"]
        or old_substrate != substrate
    ):
        raise ContractError(
            "old_execution must match the immutable task plan, base, write set, and substrate"
        )
    intent_sha256 = _intent_digest(
        plan_sha256=plan_sha256,
        task_id=task_id,
        base_sha=plan["base_sha"],
        write_set=task["write_set"],
        substrate=substrate,
    )
    replacement_count = _replacement_count(
        root.get("unchanged_intent_replacement_count", 0)
    )
    single_owner_fallback = _single_owner_fallback(
        root.get("single_owner_fallback"), intent_sha256
    )

    boundary = _object(root["review_boundary"], "review_boundary")
    _exact_keys(
        boundary,
        "review_boundary",
        {"accepted_followup_limit", "baseline", "accepted_followups"},
    )
    if boundary["accepted_followup_limit"] != ACCEPTED_FOLLOWUP_LIMIT:
        raise ContractError("review_boundary.accepted_followup_limit must be 2")
    baseline = _snapshot(
        boundary["baseline"], "review_boundary.baseline", execution_id, old_root
    )
    raw_followups = boundary["accepted_followups"]
    if not isinstance(raw_followups, list):
        raise ContractError("review_boundary.accepted_followups must be an array")
    if len(raw_followups) > ACCEPTED_FOLLOWUP_LIMIT:
        raise ContractError("accepted follow-ups exceed the bounded limit of 2")

    prior = baseline
    progress_signals: list[str] = []
    followup_ids: set[str] = set()
    for index, raw_followup in enumerate(raw_followups):
        field = f"review_boundary.accepted_followups[{index}]"
        followup = _object(raw_followup, field)
        _exact_keys(
            followup,
            field,
            {
                "followup_id",
                "host_accepted",
                "host_reported_state",
                "task_acknowledgement",
                "observation",
            },
        )
        followup_id = _identifier(followup["followup_id"], f"{field}.followup_id")
        if followup_id in followup_ids:
            raise ContractError("accepted follow-up IDs must be unique")
        followup_ids.add(followup_id)
        if not _boolean(followup["host_accepted"], f"{field}.host_accepted"):
            raise ContractError(
                "accepted_followups may contain only host-accepted follow-ups"
            )
        host_state = _string(
            followup["host_reported_state"], f"{field}.host_reported_state"
        )
        if host_state not in HOST_STATES:
            raise ContractError(f"{field}.host_reported_state is unsupported")
        acknowledgement = _acknowledgement(
            followup["task_acknowledgement"],
            f"{field}.task_acknowledgement",
            execution_id,
            task_id,
        )
        current = _snapshot(
            followup["observation"], f"{field}.observation", execution_id, old_root
        )
        if acknowledgement is not None:
            progress_signals.append(f"{followup_id}:task_acknowledgement")
        for key, label in (
            ("head_sha", "head_change"),
            ("file_state_sha256", "file_change"),
            ("receipt_state_sha256", "receipt_change"),
        ):
            if current[key] != prior[key]:
                progress_signals.append(f"{followup_id}:{label}")
        prior = current

    recovery = _object(root["old_execution_recovery"], "old_execution_recovery")
    _exact_keys(
        recovery,
        "old_execution_recovery",
        {"preservation_receipt", "inspection_receipt", "writer_observation"},
    )
    preservation_receipt = _bound_receipt(
        recovery["preservation_receipt"],
        "old_execution_recovery.preservation_receipt",
        execution_id,
        old_root,
    )
    inspection_receipt = _bound_receipt(
        recovery["inspection_receipt"],
        "old_execution_recovery.inspection_receipt",
        execution_id,
        old_root,
    )
    preserved = preservation_receipt is not None
    inspected = inspection_receipt is not None
    writer_observation = _object(
        recovery["writer_observation"],
        "old_execution_recovery.writer_observation",
    )
    _exact_keys(
        writer_observation,
        "old_execution_recovery.writer_observation",
        {"execution_id", "state", "observation_sha256"},
    )
    writer_execution_id = _identifier(
        writer_observation["execution_id"],
        "old_execution_recovery.writer_observation.execution_id",
    )
    if writer_execution_id != execution_id:
        raise ContractError(
            "old_execution_recovery.writer_observation must bind the old execution"
        )
    writer_state = _string(
        writer_observation["state"],
        "old_execution_recovery.writer_observation.state",
    )
    if writer_state not in WRITER_STATES:
        raise ContractError(
            "old_execution_recovery.writer_observation.state is unsupported"
        )
    _sha256(
        writer_observation["observation_sha256"],
        "old_execution_recovery.writer_observation.observation_sha256",
    )

    limit_reached = len(raw_followups) == ACCEPTED_FOLLOWUP_LIMIT
    lineage_lost = limit_reached and not progress_signals
    replacement = root["replacement_execution"]
    replacement_result: dict[str, Any] | None = None
    if replacement is not None:
        if replacement_count >= UNCHANGED_INTENT_REPLACEMENT_LIMIT:
            raise ContractError(
                "unchanged-intent replacement budget is exhausted; another replacement is forbidden"
            )
        if not lineage_lost:
            raise ContractError(
                "replacement is forbidden while the old lineage is not lost"
            )
        if not preserved or not inspected:
            raise ContractError(
                "replacement requires the old worktree to be preserved and inspected"
            )
        if writer_state != "stopped":
            raise ContractError("replacement requires the old writer to be stopped")
        candidate = _object(replacement, "replacement_execution")
        _exact_keys(
            candidate,
            "replacement_execution",
            {
                "execution_id",
                "task_id",
                "plan_sha256",
                "base_sha",
                "write_set",
                "substrate",
                "worktree_root",
                "writer_started",
            },
        )
        candidate_id = _identifier(
            candidate["execution_id"], "replacement_execution.execution_id"
        )
        candidate_task_id = _identifier(
            candidate["task_id"], "replacement_execution.task_id"
        )
        candidate_plan = _sha256(
            candidate["plan_sha256"], "replacement_execution.plan_sha256"
        )
        candidate_base = _sha(
            candidate["base_sha"], "replacement_execution.base_sha"
        )
        candidate_substrate = _identifier(
            candidate["substrate"], "replacement_execution.substrate"
        )
        candidate_root = _absolute_root(
            candidate["worktree_root"], "replacement_execution.worktree_root"
        )
        if not isinstance(candidate["write_set"], list) or not all(
            isinstance(item, str) for item in candidate["write_set"]
        ):
            raise ContractError("replacement_execution.write_set must be an array of strings")
        if candidate_id == execution_id:
            raise ContractError("replacement execution_id must be new")
        if _roots_overlap(old_root, candidate_root):
            raise ContractError("old and replacement worktree roots must not overlap")
        if (
            candidate_task_id != task_id
            or candidate_plan != plan_sha256
            or candidate_base != plan["base_sha"]
            or candidate["write_set"] != task["write_set"]
            or candidate_substrate != substrate
        ):
            raise ContractError(
                "replacement must keep the same task, plan, base, write set, and substrate"
            )
        if _boolean(
            candidate["writer_started"], "replacement_execution.writer_started"
        ):
            raise ContractError("replacement must be reviewed before its writer starts")
        replacement_result = {
            "execution_id": candidate_id,
            "worktree_root": candidate_root,
            "dispatch_allowed": True,
        }

    if progress_signals:
        action = "continue_existing_execution"
        reason_code = "old-execution-progress-observed"
    elif not limit_reached:
        action = "await_bounded_followup"
        reason_code = "review-boundary-not-reached"
    elif not preserved or not inspected:
        action = "preserve_and_inspect_old_worktree"
        reason_code = "old-execution-lineage-lost"
    elif writer_state != "stopped":
        action = "wait_for_old_writer_stop"
        reason_code = "lost-lineage-writer-not-stopped"
    elif replacement_count >= UNCHANGED_INTENT_REPLACEMENT_LIMIT:
        if single_owner_fallback is None:
            action = "block_no_authorized_owner"
            reason_code = "replacement-exhausted-no-authorized-owner"
        else:
            action = "collapse_slice_to_single_owner"
            reason_code = "replacement-exhausted-single-owner-convergence"
    elif replacement_result is None:
        action = "prepare_fresh_execution"
        reason_code = "fresh-execution-required"
    else:
        action = "dispatch_fresh_execution"
        reason_code = "fresh-execution-contract-ready"

    replacement_count_after_decision = replacement_count + int(
        action == "dispatch_fresh_execution"
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "worker-liveness-advice",
        "action": action,
        "reason_code": reason_code,
        "task_id": task_id,
        "plan_sha256": plan_sha256,
        "intent_sha256": intent_sha256,
        "base_sha": plan["base_sha"],
        "write_set": task["write_set"],
        "substrate": substrate,
        "old_execution": {
            "execution_id": execution_id,
            "worktree_root": old_root,
            "accepted_followups_observed": len(raw_followups),
            "accepted_followup_limit": ACCEPTED_FOLLOWUP_LIMIT,
            "progress_observed": bool(progress_signals),
            "progress_signals": progress_signals,
            "lineage_state": "lost" if lineage_lost else "live_or_bounded_review",
            "worktree_preserved": preserved,
            "worktree_inspected": inspected,
            "writer_state": writer_state,
            "preservation_receipt_sha256": (
                preservation_receipt["receipt_sha256"]
                if preservation_receipt is not None
                else None
            ),
            "inspection_receipt_sha256": (
                inspection_receipt["receipt_sha256"]
                if inspection_receipt is not None
                else None
            ),
        },
        "replacement_execution": replacement_result,
        "unchanged_intent_replacement_budget": {
            "limit": UNCHANGED_INTENT_REPLACEMENT_LIMIT,
            "consumed_before_decision": replacement_count,
            "consumed_after_decision": replacement_count_after_decision,
            "remaining_after_decision": (
                UNCHANGED_INTENT_REPLACEMENT_LIMIT
                - replacement_count_after_decision
            ),
        },
        "single_owner_fallback": single_owner_fallback,
        "replacement_evidence_counts_for_old_execution": False,
    }


def _load(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"failed to read input JSON: {error}") from error
    return _object(document, "input")


def _write(document: dict[str, Any], path: Path | None) -> None:
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if path is None:
        sys.stdout.write(payload)
    else:
        path.write_text(payload, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Advise bounded stale-worker replacement and single-owner convergence "
            "from supplied evidence."
        )
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    try:
        result = advise(_load(arguments.input))
    except ContractError as error:
        _write(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "worker-liveness-error",
                "error": str(error),
            },
            arguments.output,
        )
        return 2
    _write(result, arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
