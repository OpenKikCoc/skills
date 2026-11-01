#!/usr/bin/env python3
"""Pure planner and validator for isolated parallel change orchestration."""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
ACCEPTANCE_KINDS = {"command", "criterion"}
WORKER_STATES = {"completed", "interrupted", "failed"}
PRESERVATION_KINDS = {"integrated", "archived"}


class ContractError(ValueError):
    """Raised when an orchestration document violates its contract."""


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{field} must be a boolean")
    return value


def _schema_version(document: dict[str, Any], field: str) -> None:
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"{field}.schema_version must be {SCHEMA_VERSION}")


def _sha(value: Any, field: str) -> str:
    candidate = _string(value, field).lower()
    if not SHA_PATTERN.fullmatch(candidate):
        raise ContractError(f"{field} must be a full 40-character hexadecimal SHA")
    return candidate


def _identifier(value: Any, field: str) -> str:
    candidate = _string(value, field)
    if not ID_PATTERN.fullmatch(candidate):
        raise ContractError(
            f"{field} must use lowercase letters, digits, '.', '_', or '-'"
        )
    return candidate


def _relative_path(value: Any, field: str, allow_subtree: bool) -> str:
    candidate = _string(value, field)
    subtree = candidate.endswith("/**")
    raw_path = candidate[:-3] if subtree else candidate
    if subtree and not allow_subtree:
        raise ContractError(f"{field} must be an exact file path")
    if any(character in raw_path for character in ("*", "?", "[", "]", "\\")):
        raise ContractError(f"{field} contains unsupported path syntax")
    if (
        not raw_path
        or raw_path.startswith("/")
        or raw_path.startswith("./")
        or raw_path.endswith("/")
        or "//" in raw_path
    ):
        raise ContractError(f"{field} must be a normalized repository-relative path")
    if any(part in {"", ".", ".."} for part in raw_path.split("/")):
        raise ContractError(f"{field} must not contain '.' or '..' segments")
    path = PurePosixPath(raw_path)
    return f"{path.as_posix()}/**" if subtree else path.as_posix()


def _pattern_parts(pattern: str) -> tuple[str, bool]:
    if pattern.endswith("/**"):
        return pattern[:-3], True
    return pattern, False


def _patterns_overlap(left: str, right: str) -> bool:
    left_path, left_tree = _pattern_parts(left)
    right_path, right_tree = _pattern_parts(right)
    if not left_tree and not right_tree:
        return left_path == right_path
    if left_tree and right_tree:
        return (
            left_path == right_path
            or left_path.startswith(f"{right_path}/")
            or right_path.startswith(f"{left_path}/")
        )
    tree_path, exact_path = (
        (left_path, right_path) if left_tree else (right_path, left_path)
    )
    return exact_path == tree_path or exact_path.startswith(f"{tree_path}/")


def _path_allowed(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        pattern_path, subtree = _pattern_parts(pattern)
        if path == pattern_path or (subtree and path.startswith(f"{pattern_path}/")):
            return True
    return False


def _normalize_acceptance(value: Any, field: str) -> list[dict[str, str]]:
    entries = _list(value, field)
    if not entries:
        raise ContractError(f"{field} must contain at least one acceptance item")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw_entry in enumerate(entries):
        item_field = f"{field}[{index}]"
        entry = _mapping(raw_entry, item_field)
        acceptance_id = _identifier(entry.get("id"), f"{item_field}.id")
        if acceptance_id in seen:
            raise ContractError(f"{field} contains duplicate id '{acceptance_id}'")
        seen.add(acceptance_id)
        kind = _string(entry.get("kind"), f"{item_field}.kind")
        if kind not in ACCEPTANCE_KINDS:
            raise ContractError(
                f"{item_field}.kind must be one of {sorted(ACCEPTANCE_KINDS)}"
            )
        normalized.append(
            {
                "id": acceptance_id,
                "kind": kind,
                "value": _string(entry.get("value"), f"{item_field}.value"),
            }
        )
    return sorted(normalized, key=lambda item: item["id"])


def _stable_graph(
    tasks: list[dict[str, Any]],
) -> tuple[list[list[str]], list[str]]:
    task_ids = {task["id"] for task in tasks}
    dependents = {task_id: [] for task_id in task_ids}
    indegree = {task["id"]: len(task["depends_on"]) for task in tasks}
    for task in tasks:
        for dependency in task["depends_on"]:
            dependents[dependency].append(task["id"])
    for task_dependents in dependents.values():
        task_dependents.sort()

    stage_indegree = dict(indegree)
    current = sorted(
        task_id for task_id, dependency_count in stage_indegree.items()
        if dependency_count == 0
    )
    stages: list[list[str]] = []
    visited = 0
    while current:
        stages.append(current)
        visited += len(current)
        next_stage: list[str] = []
        for task_id in current:
            for dependent in dependents[task_id]:
                stage_indegree[dependent] -= 1
                if stage_indegree[dependent] == 0:
                    next_stage.append(dependent)
        current = sorted(next_stage)
    if visited != len(tasks):
        cyclic = sorted(
            task_id for task_id, dependency_count in stage_indegree.items()
            if dependency_count > 0
        )
        raise ContractError(f"task dependency cycle detected among: {', '.join(cyclic)}")

    heap = [
        task_id for task_id, dependency_count in indegree.items()
        if dependency_count == 0
    ]
    heapq.heapify(heap)
    integration_order: list[str] = []
    while heap:
        task_id = heapq.heappop(heap)
        integration_order.append(task_id)
        for dependent in dependents[task_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(heap, dependent)
    return stages, integration_order


def build_plan(document: Any) -> dict[str, Any]:
    root = _mapping(document, "plan input")
    _schema_version(root, "plan input")
    base_sha = _sha(root.get("base_sha"), "base_sha")
    worker_substrate = (
        None
        if root.get("worker_substrate") is None
        else _identifier(root.get("worker_substrate"), "worker_substrate")
    )
    raw_tasks = _list(root.get("tasks"), "tasks")
    if not raw_tasks:
        raise ContractError("tasks must contain at least one task")

    tasks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_task in enumerate(raw_tasks):
        field = f"tasks[{index}]"
        task = _mapping(raw_task, field)
        task_id = _identifier(task.get("id"), f"{field}.id")
        if task_id in seen_ids:
            raise ContractError(f"tasks contains duplicate id '{task_id}'")
        seen_ids.add(task_id)
        dependencies = [
            _identifier(dependency, f"{field}.depends_on[{dependency_index}]")
            for dependency_index, dependency in enumerate(
                _list(task.get("depends_on"), f"{field}.depends_on")
            )
        ]
        if len(set(dependencies)) != len(dependencies):
            raise ContractError(f"{field}.depends_on contains duplicates")
        if task_id in dependencies:
            raise ContractError(f"task '{task_id}' cannot depend on itself")

        write_set = [
            _relative_path(pattern, f"{field}.write_set[{pattern_index}]", True)
            for pattern_index, pattern in enumerate(
                _list(task.get("write_set"), f"{field}.write_set")
            )
        ]
        if not write_set:
            raise ContractError(f"{field}.write_set must contain at least one path")
        if len(set(write_set)) != len(write_set):
            raise ContractError(f"{field}.write_set contains duplicate paths")
        for left_index, left in enumerate(write_set):
            for right in write_set[left_index + 1 :]:
                if _patterns_overlap(left, right):
                    raise ContractError(
                        f"task '{task_id}' has overlapping write-set entries "
                        f"'{left}' and '{right}'"
                    )

        tasks.append(
            {
                "id": task_id,
                "summary": _string(task.get("summary"), f"{field}.summary"),
                "depends_on": sorted(dependencies),
                "write_set": sorted(write_set),
                "acceptance": _normalize_acceptance(
                    task.get("acceptance"), f"{field}.acceptance"
                ),
            }
        )

    for task in tasks:
        for dependency in task["depends_on"]:
            if dependency not in seen_ids:
                raise ContractError(
                    f"task '{task['id']}' depends on missing task '{dependency}'"
                )

    tasks.sort(key=lambda task: task["id"])
    for left_index, left_task in enumerate(tasks):
        for right_task in tasks[left_index + 1 :]:
            for left_pattern in left_task["write_set"]:
                for right_pattern in right_task["write_set"]:
                    if _patterns_overlap(left_pattern, right_pattern):
                        raise ContractError(
                            "write-set overlap between tasks "
                            f"'{left_task['id']}' and '{right_task['id']}': "
                            f"'{left_pattern}' overlaps '{right_pattern}'"
                        )

    parallel_stages, integration_order = _stable_graph(tasks)
    plan = {
        "schema_version": SCHEMA_VERSION,
        "kind": "parallel-change-plan",
        "base_sha": base_sha,
        "tasks": tasks,
        "parallel_stages": parallel_stages,
        "integration_order": integration_order,
        "invariants": {
            "shared_base_sha": True,
            "disjoint_write_sets": True,
            "controller_workspace_read_only_until_integration": True,
            "script_side_effects": "json-output-only",
        },
    }
    if worker_substrate is not None:
        plan["worker_substrate"] = worker_substrate
    return plan


def _validated_plan(value: Any) -> dict[str, Any]:
    supplied = _mapping(value, "plan")
    expected = build_plan(
        {
            "schema_version": supplied.get("schema_version"),
            "base_sha": supplied.get("base_sha"),
            "worker_substrate": supplied.get("worker_substrate"),
            "tasks": supplied.get("tasks"),
        }
    )
    if supplied.get("kind") != expected["kind"]:
        raise ContractError("plan.kind must be 'parallel-change-plan'")
    for field in ("parallel_stages", "integration_order"):
        if supplied.get(field) != expected[field]:
            raise ContractError(f"plan.{field} does not match the task DAG")
    return expected


def validate_plan(value: Any) -> dict[str, Any]:
    """Validate and normalize a supplied deterministic parallel plan."""
    return _validated_plan(value)


def plan_digest_receipt(value: Any) -> dict[str, Any]:
    plan = _validated_plan(value)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "parallel-change-plan-digest",
        "base_sha": plan["base_sha"],
        "plan_sha256": canonical_sha256(plan),
    }


def _changed_files(value: Any, field: str) -> list[str]:
    paths = [
        _relative_path(path, f"{field}[{index}]", False)
        for index, path in enumerate(_list(value, field))
    ]
    if len(set(paths)) != len(paths):
        raise ContractError(f"{field} contains duplicate paths")
    return sorted(paths)


def _main_snapshot(value: Any, field: str) -> dict[str, Any]:
    snapshot = _mapping(value, field)
    return {
        "head_sha": _sha(snapshot.get("head_sha"), f"{field}.head_sha"),
        "clean": _boolean(snapshot.get("clean"), f"{field}.clean"),
        "changed_files": _changed_files(
            snapshot.get("changed_files"), f"{field}.changed_files"
        ),
    }


def _main_validation(value: Any, base_sha: str) -> dict[str, Any]:
    errors: list[str] = []
    try:
        workspace = _mapping(value, "main_workspace")
        initial = _main_snapshot(workspace.get("initial"), "main_workspace.initial")
        current = _main_snapshot(workspace.get("current"), "main_workspace.current")
    except ContractError as error:
        return {
            "clean_at_start": False,
            "contaminated": True,
            "initial_head_sha": None,
            "current_head_sha": None,
            "errors": [str(error)],
        }

    if not initial["clean"] or initial["changed_files"]:
        errors.append("main workspace was dirty at start")
    if initial["head_sha"] != base_sha:
        errors.append("main workspace initial HEAD does not match plan base SHA")
    if current["head_sha"] != initial["head_sha"]:
        errors.append("main workspace HEAD changed before integration")
    if not current["clean"] or current["changed_files"]:
        errors.append("main workspace is contaminated before integration")
    return {
        "clean_at_start": initial["clean"] and not initial["changed_files"],
        "contaminated": bool(errors),
        "initial_head_sha": initial["head_sha"],
        "current_head_sha": current["head_sha"],
        "errors": errors,
    }


def _preservation_proof(
    value: Any, field: str, head_sha: str
) -> dict[str, Any] | None:
    if value is None:
        return None
    proof = _mapping(value, field)
    kind = _string(proof.get("kind"), f"{field}.kind")
    if kind not in PRESERVATION_KINDS:
        raise ContractError(
            f"{field}.kind must be one of {sorted(PRESERVATION_KINDS)}"
        )
    locator = _string(proof.get("locator"), f"{field}.locator")
    if kind == "integrated":
        locator = _sha(locator, f"{field}.locator")
    contains_head = _boolean(
        proof.get("contains_head"), f"{field}.contains_head"
    )
    observed_at = _string(proof.get("observed_at"), f"{field}.observed_at")
    if not contains_head:
        raise ContractError(
            f"{field} does not prove preservation of worker HEAD {head_sha}"
        )
    return {
        "kind": kind,
        "locator": locator,
        "contains_head": True,
        "observed_at": observed_at,
    }


def _worker_validation(
    task: dict[str, Any], raw_worker: Any, base_sha: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    errors: list[str] = []
    worker: dict[str, Any] | None = None
    try:
        source = _mapping(raw_worker, f"worker[{task['id']}]")
        worker_base_sha = _sha(
            source.get("base_sha"), f"worker[{task['id']}].base_sha"
        )
        head_sha = _sha(source.get("head_sha"), f"worker[{task['id']}].head_sha")
        parent_sha = _sha(
            source.get("parent_sha"), f"worker[{task['id']}].parent_sha"
        )
        merge_base_sha = _sha(
            source.get("merge_base_sha"),
            f"worker[{task['id']}].merge_base_sha",
        )
        commit_shas = [
            _sha(item, f"worker[{task['id']}].commit_shas[{index}]")
            for index, item in enumerate(
                _list(
                    source.get("commit_shas"),
                    f"worker[{task['id']}].commit_shas",
                )
            )
        ]
        if not commit_shas:
            raise ContractError(
                f"worker[{task['id']}].commit_shas must contain one result commit"
            )
        state = _string(source.get("state"), f"worker[{task['id']}].state")
        if state not in WORKER_STATES:
            raise ContractError(
                f"worker[{task['id']}].state must be one of {sorted(WORKER_STATES)}"
            )
        clean = _boolean(source.get("clean"), f"worker[{task['id']}].clean")
        changed_files = _changed_files(
            source.get("changed_files"), f"worker[{task['id']}].changed_files"
        )
        unique_artifacts = _boolean(
            source.get("unique_artifacts"),
            f"worker[{task['id']}].unique_artifacts",
        )
        preservation = _preservation_proof(
            source.get("preservation"),
            f"worker[{task['id']}].preservation",
            head_sha,
        )

        evidence_entries = _list(
            source.get("acceptance_evidence"),
            f"worker[{task['id']}].acceptance_evidence",
        )
        evidence: dict[str, dict[str, Any]] = {}
        for index, raw_evidence in enumerate(evidence_entries):
            field = f"worker[{task['id']}].acceptance_evidence[{index}]"
            item = _mapping(raw_evidence, field)
            evidence_id = _identifier(item.get("id"), f"{field}.id")
            if evidence_id in evidence:
                raise ContractError(
                    f"worker[{task['id']}] has duplicate evidence '{evidence_id}'"
                )
            evidence[evidence_id] = {
                "id": evidence_id,
                "passed": _boolean(item.get("passed"), f"{field}.passed"),
                "detail": _string(item.get("detail"), f"{field}.detail"),
            }
        worker = {
            "task_id": task["id"],
            "base_sha": worker_base_sha,
            "head_sha": head_sha,
            "parent_sha": parent_sha,
            "merge_base_sha": merge_base_sha,
            "commit_shas": commit_shas,
            "state": state,
            "clean": clean,
            "changed_files": changed_files,
            "acceptance_evidence": [
                evidence[evidence_id] for evidence_id in sorted(evidence)
            ],
            "preservation": preservation,
            "unique_artifacts": unique_artifacts,
        }
    except ContractError as error:
        errors.append(str(error))

    if worker is not None:
        if worker["base_sha"] != base_sha:
            errors.append("worker base SHA does not match plan base SHA")
        if worker["head_sha"] == base_sha:
            errors.append("worker has no distinct result commit")
        if worker["parent_sha"] != base_sha:
            errors.append("worker result parent SHA does not match plan base SHA")
        if worker["merge_base_sha"] != base_sha:
            errors.append("worker merge-base SHA does not match plan base SHA")
        if worker["commit_shas"] != [worker["head_sha"]]:
            errors.append(
                "worker result must contain exactly one commit equal to head SHA"
            )
        if worker["state"] != "completed":
            errors.append(f"worker state is {worker['state']}")
        if not worker["clean"]:
            errors.append("worker worktree is dirty")
        if not worker["changed_files"]:
            errors.append("worker result changed no files")
        for path in worker["changed_files"]:
            if not _path_allowed(path, task["write_set"]):
                errors.append(f"worker changed file outside write set: {path}")

        expected_acceptance = {
            acceptance["id"] for acceptance in task["acceptance"]
        }
        observed_acceptance = {
            evidence["id"] for evidence in worker["acceptance_evidence"]
        }
        for missing in sorted(expected_acceptance - observed_acceptance):
            errors.append(f"worker missing acceptance evidence: {missing}")
        for unexpected in sorted(observed_acceptance - expected_acceptance):
            errors.append(f"worker reported unexpected acceptance evidence: {unexpected}")
        for evidence in worker["acceptance_evidence"]:
            if evidence["id"] in expected_acceptance and not evidence["passed"]:
                errors.append(f"worker acceptance failed: {evidence['id']}")

    accepted = not errors
    cleanup_reasons: list[str] = []
    if worker is None:
        cleanup_reasons.append("worker snapshot is invalid")
    else:
        if worker["state"] != "completed":
            cleanup_reasons.append("worker did not complete")
        if not worker["clean"]:
            cleanup_reasons.append("worker worktree is dirty")
        if not accepted:
            cleanup_reasons.append("worker result is not accepted")
        if worker["preservation"] is None:
            cleanup_reasons.append(
                "worker commit lacks verified integration or archive preservation"
            )
        if worker["unique_artifacts"]:
            cleanup_reasons.append("worker has unique artifacts")
    cleanup_reasons = list(dict.fromkeys(cleanup_reasons))
    return (
        {
            "task_id": task["id"],
            "accepted": accepted,
            "errors": errors,
            "cleanup_allowed": not cleanup_reasons,
            "cleanup_refusal_reasons": cleanup_reasons,
        },
        worker,
    )


def _validate_execution_details(document: Any) -> dict[str, Any]:
    root = _mapping(document, "execution input")
    _schema_version(root, "execution input")
    plan = _validated_plan(root.get("plan"))
    main_result = _main_validation(root.get("main_workspace"), plan["base_sha"])

    top_errors: list[str] = []
    raw_workers = _list(root.get("workers"), "workers")
    workers_by_task: dict[str, Any] = {}
    expected_ids = {task["id"] for task in plan["tasks"]}
    for index, raw_worker in enumerate(raw_workers):
        try:
            worker = _mapping(raw_worker, f"workers[{index}]")
            task_id = _identifier(worker.get("task_id"), f"workers[{index}].task_id")
        except ContractError as error:
            top_errors.append(str(error))
            continue
        if task_id not in expected_ids:
            top_errors.append(f"unexpected worker for unknown task '{task_id}'")
            continue
        if task_id in workers_by_task:
            top_errors.append(f"duplicate worker for task '{task_id}'")
            continue
        workers_by_task[task_id] = worker

    worker_results: list[dict[str, Any]] = []
    normalized_workers: dict[str, dict[str, Any]] = {}
    for task in plan["tasks"]:
        raw_worker = workers_by_task.get(task["id"])
        if raw_worker is None:
            worker_results.append(
                {
                    "task_id": task["id"],
                    "accepted": False,
                    "errors": ["worker result is missing"],
                    "cleanup_allowed": False,
                    "cleanup_refusal_reasons": ["worker result is missing"],
                }
            )
            continue
        result, normalized = _worker_validation(task, raw_worker, plan["base_sha"])
        worker_results.append(result)
        if normalized is not None:
            normalized_workers[task["id"]] = normalized

    errors = list(top_errors)
    errors.extend(main_result["errors"])
    for worker_result in worker_results:
        errors.extend(
            f"{worker_result['task_id']}: {error}"
            for error in worker_result["errors"]
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "parallel-change-validation",
        "valid": not errors,
        "base_sha": plan["base_sha"],
        "integration_order": plan["integration_order"],
        "main_workspace": main_result,
        "workers": worker_results,
        "errors": errors,
        "_plan": plan,
        "_normalized_workers": normalized_workers,
    }


def validate_execution(document: Any) -> dict[str, Any]:
    result = _validate_execution_details(document)
    result.pop("_plan")
    result.pop("_normalized_workers")
    return result


def build_handoff(document: Any) -> dict[str, Any]:
    validation = _validate_execution_details(document)
    plan = validation.pop("_plan")
    normalized_workers = validation.pop("_normalized_workers")
    result_by_task = {
        worker["task_id"]: worker for worker in validation["workers"]
    }

    workers: list[dict[str, Any]] = []
    integration_steps: list[dict[str, Any]] = []
    for sequence, task_id in enumerate(plan["integration_order"], start=1):
        task = next(task for task in plan["tasks"] if task["id"] == task_id)
        result = result_by_task[task_id]
        worker = normalized_workers.get(task_id)
        workers.append(
            {
                "task_id": task_id,
                "commit_sha": worker["head_sha"] if worker else None,
                "parent_sha": worker["parent_sha"] if worker else None,
                "merge_base_sha": (
                    worker["merge_base_sha"] if worker else None
                ),
                "commit_shas": worker["commit_shas"] if worker else [],
                "depends_on": task["depends_on"],
                "write_set": task["write_set"],
                "acceptance_contract": task["acceptance"],
                "changed_files": worker["changed_files"] if worker else [],
                "acceptance_evidence": (
                    worker["acceptance_evidence"] if worker else []
                ),
                "accepted": result["accepted"],
                "preservation": worker["preservation"] if worker else None,
                "cleanup": {
                    "allowed": result["cleanup_allowed"],
                    "reasons": result["cleanup_refusal_reasons"],
                },
            }
        )
        if worker is not None and result["accepted"]:
            preservation = worker["preservation"]
            action = (
                "already-integrated"
                if preservation and preservation["kind"] == "integrated"
                else "archive-only"
                if preservation and preservation["kind"] == "archived"
                else "integrate"
            )
            integration_steps.append(
                {
                    "sequence": sequence,
                    "task_id": task_id,
                    "commit_sha": worker["head_sha"],
                    "parent_sha": worker["parent_sha"],
                    "merge_base_sha": worker["merge_base_sha"],
                    "commit_shas": worker["commit_shas"],
                    "depends_on": task["depends_on"],
                    "action": action,
                }
            )

    ready = validation["valid"]
    if ready:
        next_actions = [
            (
                f"Integrate task '{step['task_id']}' at {step['commit_sha']} "
                f"in sequence {step['sequence']} and verify its diff."
            )
            for step in integration_steps
            if step["action"] == "integrate"
        ]
        next_actions.append("Run the combined acceptance suite after integration.")
        next_actions.append(
            "Recheck every worktree before cleanup; honor cleanup refusals."
        )
        next_actions.append(
            "Finish, plan, and clean this wave's disk-budget manifest before "
            "dispatching another large wave; retain only session-scoped shared caches."
        )
    else:
        next_actions = [
            "Resolve all blockers and collect fresh snapshots.",
            "Do not integrate commits or clean worktrees while blocked.",
        ]

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "parallel-change-handoff",
        "status": "ready" if ready else "blocked",
        "base_sha": validation["base_sha"],
        "integration_order": validation["integration_order"],
        "integration_steps": integration_steps,
        "workers": workers,
        "main_workspace": validation["main_workspace"],
        "blockers": validation["errors"],
        "next_actions": next_actions,
    }


def _load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _write_json(document: Any, path: str | None) -> None:
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if path is None or path == "-":
        sys.stdout.write(rendered)
        return
    Path(path).write_text(rendered, encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan, digest, validate, or hand off parallel change snapshots."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "digest", "validate", "handoff"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--input", required=True, help="JSON file or '-'")
        subparser.add_argument("--output", help="JSON file or '-' for stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        source = _load_json(arguments.input)
        if arguments.command == "plan":
            result = build_plan(source)
            exit_code = 0
        elif arguments.command == "digest":
            result = plan_digest_receipt(source)
            exit_code = 0
        elif arguments.command == "validate":
            result = validate_execution(source)
            exit_code = 0 if result["valid"] else 1
        else:
            result = build_handoff(source)
            exit_code = 0 if result["status"] == "ready" else 1
    except (ContractError, json.JSONDecodeError, OSError) as error:
        result = {
            "schema_version": SCHEMA_VERSION,
            "kind": f"parallel-change-{arguments.command}-error",
            "valid": False,
            "errors": [str(error)],
        }
        exit_code = 2
    _write_json(result, arguments.output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
