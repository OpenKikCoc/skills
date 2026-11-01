#!/usr/bin/env python3
"""Maintain a small durable control packet for long-running agent sessions."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import fcntl
import json
import os
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
CONTROL_RELATIVE = Path(".claude/long-session")
SESSION_STATUSES = {"ACTIVE", "DISARMED", "RECOVERING", "WAITING_USER", "COMPLETE"}
MODES = {"DIRECT", "ORCHESTRATE", "REVIEW", "RECOVERY"}
MILESTONE_STATUSES = {"PENDING", "ACTIVE", "REVIEW", "PAUSED", "BLOCKED", "COMPLETE", "SUPERSEDED"}
FOREGROUND_STATUSES = {"ACTIVE", "REVIEW", "BLOCKED"}
INSTRUCTION_CLASSES = {"CLARIFY", "ADD", "PAUSE", "SUPERSEDE", "INDEPENDENT"}
WORKER_STATUSES = {"registered", "running", "awaiting_review", "reviewed", "lost", "conflicted"}
EFFECT_STATUSES = {"confirmed", "failed", "unknown"}
RECORDABLE_EVENTS = {"decision", "evidence", "evidence_invalidated", "worker"}
PATCH_FIELDS = {"mode", "foreground_milestone", "milestone_status", "next_action", "suspension_stack", "session_status"}
TRANSITIONS = {
    "PENDING": {"ACTIVE", "SUPERSEDED"},
    "ACTIVE": {"REVIEW", "PAUSED", "BLOCKED", "COMPLETE", "SUPERSEDED"},
    "REVIEW": {"ACTIVE", "BLOCKED", "COMPLETE", "SUPERSEDED"},
    "PAUSED": {"ACTIVE", "SUPERSEDED"},
    "BLOCKED": {"ACTIVE", "REVIEW", "SUPERSEDED"},
    "COMPLETE": set(),
    "SUPERSEDED": set(),
}


class ContractError(ValueError):
    """A deterministic control-packet contract violation."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def require_text(value: Any, field: str) -> str:
    require(isinstance(value, str) and bool(value.strip()), f"{field} must be a non-empty string")
    return value.strip()


def require_string_list(value: Any, field: str, *, nonempty: bool = False) -> list[str]:
    require(isinstance(value, list), f"{field} must be a list")
    if nonempty:
        require(bool(value), f"{field} must not be empty")
    result = []
    for index, item in enumerate(value):
        result.append(require_text(item, f"{field}[{index}]"))
    return result


def require_object(value: Any, field: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{field} must be an object")
    return value


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid JSON in {path}: {exc.msg}") from exc
    return require_object(value, str(path))


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_journal(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ContractError(f"missing required file: {path}") from exc
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ContractError(f"invalid JSON in {path}:{line_number}: {exc.msg}") from exc
        events.append(require_object(event, f"{path}:{line_number}"))
    return events


def authority(kind: str, value: str) -> dict[str, str]:
    return {"kind": require_text(kind, "authority kind"), "value": require_text(value, "authority value")}


def validate_authority(value: Any, field: str) -> dict[str, Any]:
    item = require_object(value, field)
    require_text(item.get("kind"), f"{field}.kind")
    require_text(item.get("value"), f"{field}.value")
    return item


def validate_contract(contract: dict[str, Any], workspace: Path | None = None) -> dict[str, Any]:
    require(contract.get("schema_version") == SCHEMA_VERSION, "unsupported contract schema_version")
    require_text(contract.get("session_id"), "contract.session_id")
    require(isinstance(contract.get("revision"), int) and contract["revision"] > 0, "contract.revision must be a positive integer")
    require_text(contract.get("objective"), "contract.objective")
    require_string_list(contract.get("success_outcomes"), "contract.success_outcomes", nonempty=True)
    constraints = contract.get("hard_constraints")
    require(isinstance(constraints, list), "contract.hard_constraints must be a list")
    constraint_ids: set[str] = set()
    for index, constraint in enumerate(constraints):
        item = require_object(constraint, f"contract.hard_constraints[{index}]")
        constraint_id = require_text(item.get("id"), f"contract.hard_constraints[{index}].id")
        require(constraint_id not in constraint_ids, f"duplicate constraint id: {constraint_id}")
        constraint_ids.add(constraint_id)
        require_text(item.get("text"), f"contract.hard_constraints[{index}].text")
    require_string_list(contract.get("non_goals"), "contract.non_goals")
    milestones = contract.get("milestones")
    require(isinstance(milestones, list) and bool(milestones), "contract.milestones must be a non-empty list")
    milestone_ids: set[str] = set()
    acceptance_ids: set[str] = set()
    for index, milestone in enumerate(milestones):
        item = require_object(milestone, f"contract.milestones[{index}]")
        milestone_id = require_text(item.get("id"), f"contract.milestones[{index}].id")
        require(milestone_id not in milestone_ids, f"duplicate milestone id: {milestone_id}")
        milestone_ids.add(milestone_id)
        require_text(item.get("title"), f"milestone {milestone_id}.title")
        require_string_list(item.get("deliverables"), f"milestone {milestone_id}.deliverables")
        checks = item.get("acceptance")
        require(isinstance(checks, list) and bool(checks), f"milestone {milestone_id}.acceptance must be a non-empty list")
        for check_index, check in enumerate(checks):
            check_item = require_object(check, f"milestone {milestone_id}.acceptance[{check_index}]")
            acceptance_id = require_text(check_item.get("id"), f"milestone {milestone_id}.acceptance[{check_index}].id")
            require(acceptance_id not in acceptance_ids, f"duplicate acceptance id: {acceptance_id}")
            acceptance_ids.add(acceptance_id)
            require_text(check_item.get("text"), f"acceptance {acceptance_id}.text")
            require(isinstance(check_item.get("required"), bool), f"acceptance {acceptance_id}.required must be Boolean")
    source = require_object(contract.get("authority"), "contract.authority")
    require_text(source.get("user_instruction"), "contract.authority.user_instruction")
    root = Path(require_text(source.get("workspace_root"), "contract.authority.workspace_root"))
    require(root.is_absolute(), "contract.authority.workspace_root must be absolute")
    if workspace is not None:
        require(root == workspace.resolve(), "contract workspace_root does not match --workspace")
    require_text(contract.get("updated_at"), "contract.updated_at")
    return {"milestone_ids": milestone_ids, "acceptance_ids": acceptance_ids, "constraint_ids": constraint_ids}


def acceptance_map(contract: dict[str, Any]) -> dict[str, tuple[str, bool]]:
    return {
        check["id"]: (milestone["id"], check["required"])
        for milestone in contract["milestones"]
        for check in milestone["acceptance"]
    }


def validate_state(state: dict[str, Any], contract: dict[str, Any]) -> None:
    require(state.get("schema_version") == SCHEMA_VERSION, "unsupported state schema_version")
    require(isinstance(state.get("state_revision"), int) and state["state_revision"] > 0, "state_revision must be a positive integer")
    require(isinstance(state.get("contract_revision"), int) and state["contract_revision"] > 0, "contract_revision must be a positive integer")
    status = state.get("session_status")
    require(status in SESSION_STATUSES, f"invalid session_status: {status}")
    require(isinstance(state.get("armed"), bool), "armed must be Boolean")
    root_id = require_text(state.get("root_session_id"), "root_session_id")
    armed_by = state.get("armed_by")
    require(armed_by is None or isinstance(armed_by, str), "armed_by must be null or a string")
    require(state.get("mode") in MODES, f"invalid mode: {state.get('mode')}")
    require(isinstance(state.get("epoch"), int) and state["epoch"] >= 0, "epoch must be a non-negative integer")
    statuses = require_object(state.get("milestone_status"), "milestone_status")
    milestone_ids = [item["id"] for item in contract["milestones"]]
    require(set(statuses) == set(milestone_ids), "milestone_status keys must exactly match contract milestones")
    for milestone_id, milestone_status in statuses.items():
        require(milestone_status in MILESTONE_STATUSES, f"invalid milestone status for {milestone_id}: {milestone_status}")
    foreground = state.get("foreground_milestone")
    active = [mid for mid, item_status in statuses.items() if item_status in FOREGROUND_STATUSES]
    if status == "COMPLETE":
        require(not state["armed"], "completed session cannot be armed")
        require(armed_by is None, "completed session cannot have armed_by")
        require(foreground is None, "completed session cannot have a foreground milestone")
        require(not active, "completed session cannot have an active/review/blocked milestone")
        require(state.get("next_action") is None, "completed session cannot have next_action")
        require(all(item in {"COMPLETE", "SUPERSEDED"} for item in statuses.values()), "completed session has unfinished milestones")
    else:
        require(foreground in statuses, "foreground_milestone must reference a contract milestone")
        require(len(active) == 1 and active[0] == foreground, "exactly one foreground milestone must be ACTIVE, REVIEW, or BLOCKED")
        require_text(state.get("next_action"), "next_action")
    if state["armed"]:
        require(status == "ACTIVE", "only ACTIVE session may be armed")
        require(armed_by == root_id, "armed_by must match root_session_id")
        require(state["mode"] != "RECOVERY", "armed session cannot remain in RECOVERY mode")
        require(state["contract_revision"] == contract["revision"], "armed state must match contract revision")
        require(state.get("recovery_reason") is None, "armed session cannot have recovery_reason")
    else:
        require(armed_by is None, "disarmed session must clear armed_by")
        require(status != "ACTIVE", "ACTIVE session must be armed")
    if status in {"DISARMED", "RECOVERING"}:
        require(state["mode"] == "RECOVERY", f"{status} session must use RECOVERY mode")
        require_text(state.get("recovery_reason"), "recovery_reason")
    stack = state.get("suspension_stack")
    require(isinstance(stack, list), "suspension_stack must be a list")
    stacked: set[str] = set()
    for index, entry in enumerate(stack):
        item = require_object(entry, f"suspension_stack[{index}]")
        milestone_id = require_text(item.get("milestone_id"), f"suspension_stack[{index}].milestone_id")
        require(milestone_id in statuses, f"unknown suspended milestone: {milestone_id}")
        require(milestone_id not in stacked, f"duplicate suspended milestone: {milestone_id}")
        stacked.add(milestone_id)
        require(statuses[milestone_id] == "PAUSED", f"suspended milestone {milestone_id} must be PAUSED")
        require_text(item.get("next_action"), f"suspension_stack[{index}].next_action")
        require_text(item.get("return_condition"), f"suspension_stack[{index}].return_condition")
    current_authority = state.get("workspace_authority")
    require(current_authority is None or isinstance(current_authority, dict), "workspace_authority must be null or an object")
    if isinstance(current_authority, dict):
        validate_authority(current_authority, "workspace_authority")
    require_text(state.get("updated_at"), "state.updated_at")


def validate_event(event: dict[str, Any], contract: dict[str, Any], *, stored: bool) -> None:
    event_type = event.get("type")
    allowed = RECORDABLE_EVENTS | {"checkpoint", "continuation", "recovery", "rearm", "effect_intent", "effect_outcome", "contract_update"}
    require(event_type in allowed, f"unsupported journal event type: {event_type}")
    if stored:
        require(isinstance(event.get("seq"), int) and event["seq"] > 0, "journal seq must be a positive integer")
        require_text(event.get("event_id"), "journal event_id")
        require_text(event.get("time"), "journal time")
    milestone_ids = {item["id"] for item in contract["milestones"]}
    checks = acceptance_map(contract)
    if event_type == "decision":
        require_text(event.get("id"), "decision.id")
        require_text(event.get("decision"), "decision.decision")
        require_text(event.get("authority"), "decision.authority")
        if "supersedes" in event:
            require_string_list(event["supersedes"], "decision.supersedes")
    elif event_type == "evidence":
        require_text(event.get("id"), "evidence.id")
        milestone_id = require_text(event.get("milestone_id"), "evidence.milestone_id")
        require(milestone_id in milestone_ids, f"evidence references unknown milestone: {milestone_id}")
        acceptance_ids = require_string_list(event.get("acceptance_ids"), "evidence.acceptance_ids", nonempty=True)
        for acceptance_id in acceptance_ids:
            require(acceptance_id in checks, f"evidence references unknown acceptance: {acceptance_id}")
            require(checks[acceptance_id][0] == milestone_id, f"acceptance {acceptance_id} does not belong to {milestone_id}")
        require_text(event.get("claim"), "evidence.claim")
        require_text(event.get("source"), "evidence.source")
        validate_authority(event.get("authority"), "evidence.authority")
        require(event.get("freshness") == "current", "new evidence freshness must be current")
    elif event_type == "evidence_invalidated":
        require_text(event.get("evidence_id"), "evidence_invalidated.evidence_id")
        require_text(event.get("reason"), "evidence_invalidated.reason")
    elif event_type == "worker":
        require_text(event.get("id"), "worker.id")
        milestone_id = require_text(event.get("milestone_id"), "worker.milestone_id")
        require(milestone_id in milestone_ids, f"worker references unknown milestone: {milestone_id}")
        require(event.get("status") in WORKER_STATUSES, f"invalid worker status: {event.get('status')}")
        if event["status"] == "registered":
            require_text(event.get("task"), "worker.task")
            require_text(event.get("base_authority"), "worker.base_authority")
            require_string_list(event.get("read_scope"), "worker.read_scope")
            require_string_list(event.get("write_scope"), "worker.write_scope")
        if event["status"] == "awaiting_review":
            require_text(event.get("receipt"), "worker.receipt")
    elif event_type == "continuation":
        require(event.get("boundary") == "compact", "continuation.boundary must be compact")
        require_text(event.get("actor"), "continuation.actor")
        require(event.get("goal_status") == "active", "continuation.goal_status must be active")
        validate_authority(event.get("authority"), "continuation.authority")
        milestone_id = require_text(event.get("foreground_milestone"), "continuation.foreground_milestone")
        require(milestone_id in milestone_ids, f"continuation references unknown milestone: {milestone_id}")
        require_text(event.get("next_action"), "continuation.next_action")
    elif event_type == "effect_intent":
        require_text(event.get("effect_id"), "effect_intent.effect_id")
        require_text(event.get("call_id"), "effect_intent.call_id")
        require_text(event.get("operation"), "effect_intent.operation")
        require_text(event.get("authority_before"), "effect_intent.authority_before")
    elif event_type == "effect_outcome":
        require_text(event.get("effect_id"), "effect_outcome.effect_id")
        require(event.get("status") in EFFECT_STATUSES, f"invalid effect outcome: {event.get('status')}")
        require_text(event.get("authority_after"), "effect_outcome.authority_after")


def validate_journal(events: list[dict[str, Any]], contract: dict[str, Any]) -> None:
    seen_event_ids: set[str] = set()
    seen_decision_ids: set[str] = set()
    seen_evidence_ids: set[str] = set()
    invalidated_evidence_ids: set[str] = set()
    workers: dict[str, str] = {}
    effects: dict[str, str | None] = {}
    expected_seq = 1
    for event in events:
        validate_event(event, contract, stored=True)
        require(event["seq"] == expected_seq, f"journal sequence must be contiguous at {expected_seq}")
        require(event["event_id"] not in seen_event_ids, f"duplicate journal event_id: {event['event_id']}")
        seen_event_ids.add(event["event_id"])
        if event["type"] == "decision":
            require(event["id"] not in seen_decision_ids, f"duplicate decision id: {event['id']}")
            seen_decision_ids.add(event["id"])
        elif event["type"] == "evidence":
            require(event["id"] not in seen_evidence_ids, f"duplicate evidence id: {event['id']}")
            seen_evidence_ids.add(event["id"])
        elif event["type"] == "evidence_invalidated":
            evidence_id = event["evidence_id"]
            require(evidence_id in seen_evidence_ids, f"cannot invalidate unknown evidence: {evidence_id}")
            require(evidence_id not in invalidated_evidence_ids, f"evidence already invalidated: {evidence_id}")
            invalidated_evidence_ids.add(evidence_id)
        elif event["type"] == "worker":
            worker_id = event["id"]
            prior = workers.get(worker_id)
            current = event["status"]
            if prior is None:
                require(current == "registered", f"worker {worker_id} must start as registered")
            else:
                allowed = {
                    "registered": {"running", "lost", "conflicted"},
                    "running": {"awaiting_review", "lost", "conflicted"},
                    "awaiting_review": {"reviewed", "conflicted"},
                    "lost": {"reviewed"},
                    "conflicted": {"reviewed"},
                    "reviewed": set(),
                }[prior]
                require(current in allowed, f"invalid worker transition {worker_id}: {prior} -> {current}")
            workers[worker_id] = current
        elif event["type"] == "effect_intent":
            effect_id = event["effect_id"]
            require(effect_id not in effects, f"duplicate effect intent: {effect_id}")
            effects[effect_id] = None
        elif event["type"] == "effect_outcome":
            effect_id = event["effect_id"]
            require(effect_id in effects, f"effect outcome references unknown intent: {effect_id}")
            prior = effects[effect_id]
            if prior is None:
                effects[effect_id] = event["status"]
            else:
                require(prior == "unknown", f"effect {effect_id} already has terminal outcome: {prior}")
                require(event["status"] in {"confirmed", "failed"}, f"unknown effect {effect_id} may only resolve to confirmed or failed")
                effects[effect_id] = event["status"]
        expected_seq += 1


def project(events: list[dict[str, Any]]) -> dict[str, Any]:
    evidence: dict[str, dict[str, Any]] = {}
    invalidated: set[str] = set()
    workers: dict[str, dict[str, Any]] = {}
    effects: dict[str, dict[str, Any]] = {}
    decisions: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["type"] == "evidence":
            evidence[event["id"]] = event
        elif event["type"] == "evidence_invalidated":
            invalidated.add(event["evidence_id"])
        elif event["type"] == "worker":
            workers[event["id"]] = event
        elif event["type"] == "effect_intent":
            effects[event["effect_id"]] = {"intent": event, "outcome": None}
        elif event["type"] == "effect_outcome":
            if event["effect_id"] in effects:
                effects[event["effect_id"]]["outcome"] = event
        elif event["type"] == "decision":
            decisions[event["id"]] = event
    valid_evidence = {key: value for key, value in evidence.items() if key not in invalidated}
    return {
        "evidence": evidence,
        "valid_evidence": valid_evidence,
        "invalidated_evidence": sorted(invalidated),
        "workers": workers,
        "effects": effects,
        "decisions": decisions,
    }


def completion_evidence_gaps(
    contract: dict[str, Any], state: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, list[str]]:
    view = project(events)
    covered: dict[str, set[str]] = {}
    for item in view["valid_evidence"].values():
        current_authority = state.get("workspace_authority")
        item_authority = item["authority"]
        if (
            current_authority is not None
            and item_authority["kind"] == current_authority["kind"]
            and item_authority != current_authority
        ):
            continue
        covered.setdefault(item["milestone_id"], set()).update(item["acceptance_ids"])
    gaps: dict[str, list[str]] = {}
    for milestone in contract["milestones"]:
        if state["milestone_status"][milestone["id"]] != "COMPLETE":
            continue
        required = {item["id"] for item in milestone["acceptance"] if item["required"]}
        missing = sorted(required - covered.get(milestone["id"], set()))
        if missing:
            gaps[milestone["id"]] = missing
    return gaps


def operational_blockers(contract: dict[str, Any], state: dict[str, Any], events: list[dict[str, Any]]) -> list[str]:
    view = project(events)
    blockers: list[str] = []
    if state["contract_revision"] != contract["revision"]:
        blockers.append("contract_revision_mismatch")
    for milestone_id, acceptance_ids in sorted(completion_evidence_gaps(contract, state, events).items()):
        blockers.extend(
            f"milestone:{milestone_id}:missing_evidence:{acceptance_id}"
            for acceptance_id in acceptance_ids
        )
    for worker_id, worker in sorted(view["workers"].items()):
        if worker["status"] in {"lost", "conflicted"}:
            blockers.append(f"worker:{worker_id}:{worker['status']}")
    for effect_id, effect in sorted(view["effects"].items()):
        outcome = effect["outcome"]
        if outcome is None:
            blockers.append(f"effect:{effect_id}:unclosed")
        elif outcome["status"] == "unknown":
            blockers.append(f"effect:{effect_id}:unknown")
    return blockers


def validate_completion(contract: dict[str, Any], state: dict[str, Any], events: list[dict[str, Any]]) -> None:
    for milestone_id, missing in completion_evidence_gaps(contract, state, events).items():
        raise ContractError(
            f"completed milestone {milestone_id} lacks fresh evidence for: {', '.join(missing)}"
        )


def packet_paths(workspace: Path) -> dict[str, Path]:
    control = workspace.resolve() / CONTROL_RELATIVE
    return {
        "control": control,
        "contract": control / "contract.json",
        "state": control / "state.json",
        "journal": control / "journal.jsonl",
        "lock": control / ".lock",
    }


@contextmanager
def locked(paths: dict[str, Path], *, create: bool) -> Iterator[None]:
    if create:
        paths["control"].mkdir(parents=True, exist_ok=True)
        lock_mode = "a+"
    else:
        require(paths["control"].is_dir(), f"missing control directory: {paths['control']}")
        require(paths["lock"].is_file(), f"missing required file: {paths['lock']}")
        lock_mode = "r+"
    with paths["lock"].open(lock_mode, encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def load_packet(paths: dict[str, Path], workspace: Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    contract = load_json(paths["contract"])
    state = load_json(paths["state"])
    events = read_journal(paths["journal"])
    validate_contract(contract, workspace)
    validate_state(state, contract)
    validate_journal(events, contract)
    return contract, state, events


def prepare_event(event: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    result = copy.deepcopy(event)
    for reserved in ("seq", "event_id", "time"):
        require(reserved not in result, f"event field is managed by the script: {reserved}")
    result["seq"] = len(events) + 1
    result["event_id"] = f"J-{uuid.uuid4()}"
    result["time"] = now()
    return result


def write_event(paths: dict[str, Path], event: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    stored = prepare_event(event, events)
    append_jsonl(paths["journal"], stored)
    events.append(stored)
    return stored


def bump_state(paths: dict[str, Path], state: dict[str, Any]) -> None:
    state["state_revision"] += 1
    state["updated_at"] = now()
    atomic_write_json(paths["state"], state)


def require_revision(state: dict[str, Any], expected: int) -> None:
    require(state["state_revision"] == expected, f"state revision conflict: expected {expected}, current {state['state_revision']}")


def result_summary(contract: dict[str, Any], state: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    view = project(events)
    workers: dict[str, list[str]] = {status: [] for status in sorted(WORKER_STATUSES)}
    for worker_id, worker in view["workers"].items():
        workers[worker["status"]].append(worker_id)
    effects = {"unclosed": [], "unknown": []}
    for effect_id, effect in view["effects"].items():
        outcome = effect["outcome"]
        if outcome is None:
            effects["unclosed"].append(effect_id)
        elif outcome["status"] == "unknown":
            effects["unknown"].append(effect_id)
    return {
        "valid": True,
        "session_id": contract["session_id"],
        "contract_revision": contract["revision"],
        "state_revision": state["state_revision"],
        "session_status": state["session_status"],
        "armed": state["armed"],
        "mode": state["mode"],
        "epoch": state["epoch"],
        "foreground_milestone": state["foreground_milestone"],
        "next_action": state["next_action"],
        "workspace_authority": state["workspace_authority"],
        "valid_evidence": sorted(view["valid_evidence"]),
        "invalidated_evidence": view["invalidated_evidence"],
        "workers": workers,
        "effects": effects,
        "blockers": operational_blockers(contract, state, events),
    }


def cmd_init(args: argparse.Namespace, paths: dict[str, Path], workspace: Path) -> dict[str, Any]:
    require(workspace.exists() and workspace.is_dir(), "--workspace must be an existing directory")
    require(not any(paths[key].exists() for key in ("contract", "state", "journal")), "control packet already exists")
    draft = load_json(Path(args.contract_file))
    contract = copy.deepcopy(draft)
    contract["schema_version"] = SCHEMA_VERSION
    contract["session_id"] = require_text(args.root_session_id, "root session id")
    contract["revision"] = 1
    source = require_object(contract.get("authority"), "contract.authority")
    source["workspace_root"] = str(workspace.resolve())
    contract["updated_at"] = now()
    validate_contract(contract, workspace)
    first = contract["milestones"][0]["id"]
    current_authority = authority(args.authority_kind, args.authority_value)
    state = {
        "schema_version": SCHEMA_VERSION,
        "state_revision": 1,
        "contract_revision": 1,
        "session_status": "ACTIVE",
        "armed": True,
        "root_session_id": contract["session_id"],
        "armed_by": contract["session_id"],
        "mode": "DIRECT",
        "epoch": 0,
        "foreground_milestone": first,
        "milestone_status": {item["id"]: ("ACTIVE" if item["id"] == first else "PENDING") for item in contract["milestones"]},
        "next_action": require_text(args.next_action, "next action"),
        "suspension_stack": [],
        "recovery_reason": None,
        "workspace_authority": current_authority,
        "updated_at": now(),
    }
    validate_state(state, contract)
    atomic_write_json(paths["contract"], contract)
    atomic_write_json(paths["state"], state)
    paths["journal"].touch(exist_ok=False)
    journal_fd = os.open(paths["journal"], os.O_RDONLY)
    try:
        os.fsync(journal_fd)
    finally:
        os.close(journal_fd)
    return {"valid": True, "initialized": True, "control_dir": str(paths["control"]), **result_summary(contract, state, [])}


def cmd_validate(args: argparse.Namespace, paths: dict[str, Path], workspace: Path) -> dict[str, Any]:
    contract, state, events = load_packet(paths, workspace)
    return {"valid": True, "control_dir": str(paths["control"]), "journal_events": len(events), "blockers": operational_blockers(contract, state, events)}


def cmd_status(args: argparse.Namespace, paths: dict[str, Path], workspace: Path) -> dict[str, Any]:
    contract, state, events = load_packet(paths, workspace)
    return {"control_dir": str(paths["control"]), **result_summary(contract, state, events)}


def cmd_record(args: argparse.Namespace, paths: dict[str, Path], workspace: Path) -> dict[str, Any]:
    contract, state, events = load_packet(paths, workspace)
    event = load_json(Path(args.event_file))
    require(event.get("type") in RECORDABLE_EVENTS, "record accepts decision, evidence, evidence_invalidated, or worker events only")
    if event.get("type") == "worker" and event.get("status") in {"registered", "running"}:
        require(state["session_status"] == "ACTIVE" and state["armed"], "cannot dispatch or start a worker while disarmed")
    if event.get("type") == "evidence":
        event.setdefault("freshness", "current")
        current_authority = state.get("workspace_authority")
        event_authority = event.get("authority")
        if isinstance(current_authority, dict) and isinstance(event_authority, dict):
            require(
                event_authority.get("kind") != current_authority.get("kind")
                or event_authority == current_authority,
                "evidence authority is stale relative to current workspace authority",
            )
    validate_event(event, contract, stored=False)
    stored = prepare_event(event, events)
    validate_journal(events + [stored], contract)
    append_jsonl(paths["journal"], stored)
    return {"valid": True, "recorded": stored}


def cmd_checkpoint(args: argparse.Namespace, paths: dict[str, Path], workspace: Path) -> dict[str, Any]:
    contract, state, events = load_packet(paths, workspace)
    require_revision(state, args.expected_revision)
    require(state["session_status"] == "ACTIVE" and state["armed"], "checkpoint requires an armed ACTIVE session")
    patch = load_json(Path(args.patch_file))
    unknown = sorted(set(patch) - PATCH_FIELDS)
    require(not unknown, f"unsupported checkpoint fields: {', '.join(unknown)}")
    candidate = copy.deepcopy(state)
    if "milestone_status" in patch:
        updates = require_object(patch["milestone_status"], "patch.milestone_status")
        for milestone_id, new_status in updates.items():
            require(milestone_id in candidate["milestone_status"], f"unknown milestone in patch: {milestone_id}")
            old_status = candidate["milestone_status"][milestone_id]
            require(new_status == old_status or new_status in TRANSITIONS[old_status], f"invalid milestone transition {milestone_id}: {old_status} -> {new_status}")
            candidate["milestone_status"][milestone_id] = new_status
    for key, value in patch.items():
        if key != "milestone_status":
            candidate[key] = value
    requested_status = candidate["session_status"]
    require(requested_status in {"ACTIVE", "WAITING_USER", "COMPLETE"}, "checkpoint cannot enter recovery states")
    if requested_status == "WAITING_USER":
        candidate["armed"] = False
        candidate["armed_by"] = None
    elif requested_status == "COMPLETE":
        candidate["armed"] = False
        candidate["armed_by"] = None
        candidate["foreground_milestone"] = None
        candidate["next_action"] = None
    validate_state(candidate, contract)
    validate_completion(contract, candidate, events)
    if requested_status == "COMPLETE":
        blockers = operational_blockers(contract, candidate, events)
        view = project(events)
        unfinished_workers = sorted(
            worker_id
            for worker_id, worker in view["workers"].items()
            if worker["status"] != "reviewed"
        )
        require(not blockers, f"cannot complete with blockers: {', '.join(blockers)}")
        require(not unfinished_workers, f"cannot complete with unfinished workers: {', '.join(unfinished_workers)}")
    write_event(paths, {"type": "checkpoint", "from_revision": state["state_revision"], "summary": args.summary or candidate.get("next_action") or "session complete"}, events)
    bump_state(paths, candidate)
    return {"valid": True, "state_revision": candidate["state_revision"], "session_status": candidate["session_status"], "foreground_milestone": candidate["foreground_milestone"]}


def cmd_disarm(args: argparse.Namespace, paths: dict[str, Path], workspace: Path) -> dict[str, Any]:
    contract, state, events = load_packet(paths, workspace)
    require_revision(state, args.expected_revision)
    require(state["session_status"] != "COMPLETE", "cannot disarm a completed session")
    state["session_status"] = "DISARMED"
    state["armed"] = False
    state["armed_by"] = None
    state["mode"] = "RECOVERY"
    state["recovery_reason"] = require_text(args.reason, "reason")
    bump_state(paths, state)
    write_event(paths, {"type": "checkpoint", "action": "disarm", "state_revision": state["state_revision"], "reason": state["recovery_reason"]}, events)
    return {"valid": True, "disarmed": True, "state_revision": state["state_revision"], "reason": state["recovery_reason"]}


def cmd_continue_after_compact(args: argparse.Namespace, paths: dict[str, Path], workspace: Path) -> dict[str, Any]:
    contract, state, events = load_packet(paths, workspace)
    require_revision(state, args.expected_revision)
    require(args.actor == state["root_session_id"], "only root_session_id may continue after compact")
    require(args.goal_status == "active", "host goal must be active to continue after compact")
    require(state["session_status"] == "ACTIVE" and state["armed"], "compact continuation requires an armed ACTIVE session")
    current_authority = authority(args.authority_kind, args.authority_value)
    require(current_authority == state["workspace_authority"], "authority changed across compact; disarm and recover")
    blockers = operational_blockers(contract, state, events)
    require(not blockers, f"compact continuation requires recovery: {', '.join(blockers)}")
    state["epoch"] += 1
    stored = write_event(
        paths,
        {
            "type": "continuation",
            "boundary": "compact",
            "actor": args.actor,
            "goal_status": args.goal_status,
            "authority": current_authority,
            "foreground_milestone": state["foreground_milestone"],
            "next_action": state["next_action"],
        },
        events,
    )
    bump_state(paths, state)
    milestone = next(
        item for item in contract["milestones"] if item["id"] == state["foreground_milestone"]
    )
    return {
        "valid": True,
        "continue": True,
        "instruction": "execute next_action now; do not stop at this receipt",
        "objective": contract["objective"],
        "success_outcomes": contract["success_outcomes"],
        "hard_constraints": contract["hard_constraints"],
        "foreground_milestone": state["foreground_milestone"],
        "milestone_title": milestone["title"],
        "required_acceptance": [
            item["id"] for item in milestone["acceptance"] if item["required"]
        ],
        "acceptance_contract": [
            item for item in milestone["acceptance"] if item["required"]
        ],
        "next_action": state["next_action"],
        "epoch": state["epoch"],
        "state_revision": state["state_revision"],
        "continuation_event_id": stored["event_id"],
    }


def cmd_recover(args: argparse.Namespace, paths: dict[str, Path], workspace: Path) -> dict[str, Any]:
    contract, state, events = load_packet(paths, workspace)
    require_revision(state, args.expected_revision)
    require(args.actor == state["root_session_id"], "only root_session_id may recover")
    require(state["session_status"] in {"DISARMED", "RECOVERING", "WAITING_USER"}, "recover requires a disarmed, recovering, or waiting session")
    current_authority = authority(args.authority_kind, args.authority_value)
    previous_authority = state.get("workspace_authority")
    invalidated: list[str] = []
    if previous_authority is not None and previous_authority != current_authority:
        view = project(events)
        for evidence_id, item in sorted(view["valid_evidence"].items()):
            if item.get("authority") == previous_authority:
                stored = write_event(paths, {"type": "evidence_invalidated", "evidence_id": evidence_id, "reason": "workspace authority changed during recovery"}, events)
                invalidated.append(stored["evidence_id"])
    state["workspace_authority"] = current_authority
    contract_revision_decisions = {event.get("contract_revision") for event in events if event["type"] in {"decision", "contract_update"}}
    if state["contract_revision"] != contract["revision"] and contract["revision"] in contract_revision_decisions:
        state["contract_revision"] = contract["revision"]
    state["session_status"] = "RECOVERING"
    state["armed"] = False
    state["armed_by"] = None
    state["mode"] = "RECOVERY"
    state["epoch"] += 1
    blockers = operational_blockers(contract, state, events)
    receipt = {
        "contract_revision": contract["revision"],
        "epoch": state["epoch"],
        "foreground_milestone": state["foreground_milestone"],
        "current_authority": current_authority,
        "invalidated_evidence": invalidated,
        "blockers": blockers,
        "next_action": state["next_action"],
        "rearm_allowed_after_semantic_review": not blockers,
    }
    stored = write_event(paths, {"type": "recovery", "receipt": receipt}, events)
    state["last_recovery_event_id"] = stored["event_id"]
    bump_state(paths, state)
    receipt["recovery_event_id"] = stored["event_id"]
    receipt["state_revision"] = state["state_revision"]
    return {"valid": True, "recovery": receipt}


def cmd_rearm(args: argparse.Namespace, paths: dict[str, Path], workspace: Path) -> dict[str, Any]:
    contract, state, events = load_packet(paths, workspace)
    require_revision(state, args.expected_revision)
    require(state["session_status"] == "RECOVERING", "rearm requires RECOVERING status")
    require(args.actor == state["root_session_id"], "only root_session_id may rearm")
    require(args.instruction_classification in INSTRUCTION_CLASSES, "invalid instruction classification")
    expected_constraints = {item["id"] for item in contract["hard_constraints"]}
    checked_constraints = set(args.checked_constraint)
    require(checked_constraints == expected_constraints, "checked constraints must exactly match all hard constraint IDs")
    current_authority = authority(args.authority_kind, args.authority_value)
    require(current_authority == state["workspace_authority"], "authority changed after recovery; recover again")
    blockers = operational_blockers(contract, state, events)
    require(not blockers, f"cannot rearm with blockers: {', '.join(blockers)}")
    state["session_status"] = "ACTIVE"
    state["armed"] = True
    state["armed_by"] = state["root_session_id"]
    state["mode"] = args.mode
    state["recovery_reason"] = None
    write_event(paths, {"type": "rearm", "actor": args.actor, "instruction_classification": args.instruction_classification, "checked_constraints": sorted(checked_constraints), "authority": current_authority}, events)
    bump_state(paths, state)
    return {"valid": True, "rearmed": True, "state_revision": state["state_revision"], "epoch": state["epoch"]}


def cmd_assert_ready(args: argparse.Namespace, paths: dict[str, Path], workspace: Path) -> dict[str, Any]:
    contract, state, events = load_packet(paths, workspace)
    require(args.actor == state["root_session_id"], "actor is not the root session")
    require(state["session_status"] == "ACTIVE" and state["armed"], "session is not armed for writes")
    require(state["armed_by"] == args.actor, "session is armed by another actor")
    blockers = operational_blockers(contract, state, events)
    if args.effect_id:
        expected = f"effect:{args.effect_id}:unclosed"
        require(expected in blockers, f"effect is not prepared and unclosed: {args.effect_id}")
        blockers = [item for item in blockers if item != expected]
    require(not blockers, f"write blockers remain: {', '.join(blockers)}")
    output = {"valid": True, "ready": True, "state_revision": state["state_revision"], "epoch": state["epoch"]}
    if args.effect_id:
        output["effect_id"] = args.effect_id
        output["call_id"] = project(events)["effects"][args.effect_id]["intent"]["call_id"]
    return output


def cmd_effect_prepare(args: argparse.Namespace, paths: dict[str, Path], workspace: Path) -> dict[str, Any]:
    contract, state, events = load_packet(paths, workspace)
    require(args.actor == state["root_session_id"], "only root_session_id may prepare an effect")
    require(state["session_status"] == "ACTIVE" and state["armed"], "effect prepare requires an armed ACTIVE session")
    require(not operational_blockers(contract, state, events), "cannot prepare an effect while blockers remain")
    view = project(events)
    require(args.effect_id not in view["effects"], f"effect already exists: {args.effect_id}")
    call_id = args.call_id or str(uuid.uuid4())
    stored = write_event(paths, {"type": "effect_intent", "effect_id": require_text(args.effect_id, "effect id"), "call_id": call_id, "operation": require_text(args.operation, "operation"), "authority_before": require_text(args.authority_before, "authority before")}, events)
    return {"valid": True, "prepared": True, "effect_id": stored["effect_id"], "call_id": call_id}


def cmd_effect_finish(args: argparse.Namespace, paths: dict[str, Path], workspace: Path) -> dict[str, Any]:
    contract, state, events = load_packet(paths, workspace)
    require(args.actor == state["root_session_id"], "only root_session_id may finish an effect")
    view = project(events)
    require(args.effect_id in view["effects"], f"unknown effect: {args.effect_id}")
    prior = view["effects"][args.effect_id]["outcome"]
    if prior is not None:
        require(prior["status"] == "unknown", f"effect already has terminal outcome: {prior['status']}")
    require(args.status in EFFECT_STATUSES, f"invalid effect outcome: {args.status}")
    if prior is not None:
        require(args.status in {"confirmed", "failed"}, "unknown effect may only be resolved to confirmed or failed")
    stored = write_event(paths, {"type": "effect_outcome", "effect_id": args.effect_id, "status": args.status, "authority_after": require_text(args.authority_after, "authority after")}, events)
    return {"valid": True, "finished": True, "effect_id": stored["effect_id"], "status": stored["status"]}


def cmd_contract_update(args: argparse.Namespace, paths: dict[str, Path], workspace: Path) -> dict[str, Any]:
    contract, state, events = load_packet(paths, workspace)
    require(state["session_status"] != "COMPLETE", "cannot update a completed contract")
    require(args.actor == state["root_session_id"], "only root_session_id may update the contract")
    require(contract["revision"] == args.expected_contract_revision, f"contract revision conflict: expected {args.expected_contract_revision}, current {contract['revision']}")
    require_revision(state, args.expected_state_revision)
    candidate = load_json(Path(args.contract_file))
    candidate["schema_version"] = SCHEMA_VERSION
    candidate["session_id"] = contract["session_id"]
    candidate["revision"] = contract["revision"] + 1
    source = require_object(candidate.get("authority"), "contract.authority")
    source["workspace_root"] = str(workspace.resolve())
    candidate["updated_at"] = now()
    validate_contract(candidate, workspace)
    old_acceptance_ids = set(acceptance_map(contract))
    new_acceptance_ids = set(acceptance_map(candidate))
    require(old_acceptance_ids <= new_acceptance_ids, "contract update cannot remove acceptance IDs; supersede their milestone instead")
    decision = load_json(Path(args.decision_file))
    require(decision.get("type") == "decision", "contract update requires a decision event")
    decision["contract_revision"] = candidate["revision"]
    validate_event(decision, candidate, stored=False)
    old_ids = set(state["milestone_status"])
    new_ids = {item["id"] for item in candidate["milestones"]}
    require(old_ids <= new_ids, "contract update cannot remove milestones; supersede them instead")
    state["session_status"] = "DISARMED"
    state["armed"] = False
    state["armed_by"] = None
    state["mode"] = "RECOVERY"
    state["recovery_reason"] = "contract_updated"
    validate_journal(events, candidate)
    bump_state(paths, state)
    atomic_write_json(paths["contract"], candidate)
    stored = write_event(paths, decision, events)
    state["milestone_status"].update({mid: "PENDING" for mid in sorted(new_ids - old_ids)})
    validate_state(state, candidate)
    state["updated_at"] = now()
    atomic_write_json(paths["state"], state)
    return {"valid": True, "contract_revision": candidate["revision"], "state_revision": state["state_revision"], "decision_event_id": stored["event_id"], "disarmed": True}


COMMANDS = {
    "init": cmd_init,
    "validate": cmd_validate,
    "status": cmd_status,
    "record": cmd_record,
    "checkpoint": cmd_checkpoint,
    "disarm": cmd_disarm,
    "continue-after-compact": cmd_continue_after_compact,
    "recover": cmd_recover,
    "rearm": cmd_rearm,
    "assert-ready": cmd_assert_ready,
    "contract-update": cmd_contract_update,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="Existing workspace root")
    parser.add_argument("--json-output", help="Optional path for an atomic JSON result copy")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init")
    init.add_argument("--contract-file", required=True)
    init.add_argument("--root-session-id", required=True)
    init.add_argument("--next-action", required=True)
    init.add_argument("--authority-kind", required=True)
    init.add_argument("--authority-value", required=True)

    subparsers.add_parser("validate")
    subparsers.add_parser("status")

    record = subparsers.add_parser("record")
    record.add_argument("--event-file", required=True)

    checkpoint = subparsers.add_parser("checkpoint")
    checkpoint.add_argument("--expected-revision", type=int, required=True)
    checkpoint.add_argument("--patch-file", required=True)
    checkpoint.add_argument("--summary")

    disarm = subparsers.add_parser("disarm")
    disarm.add_argument("--expected-revision", type=int, required=True)
    disarm.add_argument("--reason", required=True)

    continuation = subparsers.add_parser("continue-after-compact")
    continuation.add_argument("--expected-revision", type=int, required=True)
    continuation.add_argument("--actor", required=True)
    continuation.add_argument(
        "--goal-status", choices=["active", "paused", "complete"], required=True
    )
    continuation.add_argument("--authority-kind", required=True)
    continuation.add_argument("--authority-value", required=True)

    recover = subparsers.add_parser("recover")
    recover.add_argument("--expected-revision", type=int, required=True)
    recover.add_argument("--actor", required=True)
    recover.add_argument("--authority-kind", required=True)
    recover.add_argument("--authority-value", required=True)

    rearm = subparsers.add_parser("rearm")
    rearm.add_argument("--expected-revision", type=int, required=True)
    rearm.add_argument("--actor", required=True)
    rearm.add_argument("--authority-kind", required=True)
    rearm.add_argument("--authority-value", required=True)
    rearm.add_argument("--instruction-classification", choices=sorted(INSTRUCTION_CLASSES), required=True)
    rearm.add_argument("--checked-constraint", action="append", default=[])
    rearm.add_argument("--mode", choices=sorted(MODES - {"RECOVERY"}), default="DIRECT")

    ready = subparsers.add_parser("assert-ready")
    ready.add_argument("--actor", required=True)
    ready.add_argument("--effect-id")

    contract_update = subparsers.add_parser("contract-update")
    contract_update.add_argument("--expected-contract-revision", type=int, required=True)
    contract_update.add_argument("--expected-state-revision", type=int, required=True)
    contract_update.add_argument("--actor", required=True)
    contract_update.add_argument("--contract-file", required=True)
    contract_update.add_argument("--decision-file", required=True)

    effect = subparsers.add_parser("effect")
    effect_subparsers = effect.add_subparsers(dest="effect_command", required=True)
    prepare = effect_subparsers.add_parser("prepare")
    prepare.add_argument("--actor", required=True)
    prepare.add_argument("--effect-id", required=True)
    prepare.add_argument("--operation", required=True)
    prepare.add_argument("--authority-before", required=True)
    prepare.add_argument("--call-id")
    finish = effect_subparsers.add_parser("finish")
    finish.add_argument("--actor", required=True)
    finish.add_argument("--effect-id", required=True)
    finish.add_argument("--status", choices=sorted(EFFECT_STATUSES), required=True)
    finish.add_argument("--authority-after", required=True)
    return parser


def emit(value: dict[str, Any], output: str | None) -> None:
    if output:
        atomic_write_json(Path(output), value)
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    workspace = Path(args.workspace)
    paths = packet_paths(workspace)
    try:
        with locked(paths, create=args.command == "init"):
            if args.command == "effect":
                handler = cmd_effect_prepare if args.effect_command == "prepare" else cmd_effect_finish
            else:
                handler = COMMANDS[args.command]
            output = handler(args, paths, workspace)
        emit(output, args.json_output)
        return 0
    except ContractError as exc:
        emit({"valid": False, "error": str(exc)}, args.json_output)
        return 2
    except (OSError, UnicodeError) as exc:
        emit({"valid": False, "error": f"I/O failure: {exc}"}, args.json_output)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
