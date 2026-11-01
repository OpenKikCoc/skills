#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


QUEUED_VALUES = {
    "created",
    "pending",
    "queued",
    "requested",
    "scheduled",
    "waiting",
}
RUNNING_VALUES = {"in-progress", "in_progress", "processing", "running"}
SUCCESS_VALUES = {"pass", "passed", "success", "successful", "succeeded"}
FAILURE_VALUES = {
    "action_required",
    "cancelled",
    "canceled",
    "error",
    "errored",
    "fail",
    "failed",
    "failure",
    "startup_failure",
    "timed_out",
    "timeout",
}
NON_SUCCESS_VALUES = {"neutral", "skipped"}


class EvidenceError(ValueError):
    pass


def normalized_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{label} must be an object")
    return value


def require_check_list(packet: Mapping[str, Any], key: str) -> List[Mapping[str, Any]]:
    value = packet.get(key, [])
    if not isinstance(value, list):
        raise EvidenceError(f"{key} must be an array")
    checks: List[Mapping[str, Any]] = []
    for index, check in enumerate(value):
        item = require_mapping(check, f"{key}[{index}]")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise EvidenceError(f"{key}[{index}].name must be a non-empty string")
        if not isinstance(item.get("required"), bool):
            raise EvidenceError(f"{key}[{index}].required must be a boolean")
        for collection_key in ("artifacts", "steps"):
            collection = item.get(collection_key, [])
            if not isinstance(collection, list):
                raise EvidenceError(
                    f"{key}[{index}].{collection_key} must be an array"
                )
            for child_index, child in enumerate(collection):
                require_mapping(
                    child,
                    f"{key}[{index}].{collection_key}[{child_index}]",
                )
        checks.append(item)
    return checks


def require_name_list(packet: Mapping[str, Any], key: str) -> List[str]:
    value = packet.get(key)
    if not isinstance(value, list):
        raise EvidenceError(f"{key} must be an array")
    names: List[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise EvidenceError(f"{key}[{index}] must be a non-empty string")
        names.append(item.strip())
    if len(set(names)) != len(names):
        raise EvidenceError(f"{key} must not contain duplicate names")
    return sorted(names)


def check_state(check: Mapping[str, Any]) -> str:
    status = normalized_text(check.get("status"))
    conclusion = normalized_text(check.get("conclusion"))
    status_terminal = (
        "failed"
        if status in FAILURE_VALUES
        else "green"
        if status in SUCCESS_VALUES
        else None
    )
    conclusion_terminal = (
        "failed"
        if conclusion in FAILURE_VALUES
        else "green"
        if conclusion in SUCCESS_VALUES
        else None
    )
    if (
        status_terminal is not None
        and conclusion_terminal is not None
        and status_terminal != conclusion_terminal
    ):
        raise EvidenceError(
            f"check {check.get('name', '<unknown>')!r} has conflicting "
            "terminal status and conclusion"
        )
    if status in FAILURE_VALUES:
        return "failed"
    if status in SUCCESS_VALUES:
        return "green"
    if status in RUNNING_VALUES:
        return "running"
    if status in QUEUED_VALUES:
        return "queued"
    if conclusion in FAILURE_VALUES:
        return "failed"
    if conclusion in SUCCESS_VALUES:
        return "green"
    if conclusion in NON_SUCCESS_VALUES:
        return "unknown"
    return "unknown"


def first_available_artifact(
    check: Mapping[str, Any],
) -> Optional[Mapping[str, Any]]:
    for artifact in check.get("artifacts", []):
        if artifact.get("available") is True:
            return artifact
    return None


def first_failed_step_with_log(
    check: Mapping[str, Any],
) -> Optional[Mapping[str, Any]]:
    for step in check.get("steps", []):
        if check_state(step) == "failed" and step.get("log_available") is True:
            return step
    return None


def selected_evidence(check: Mapping[str, Any]) -> Dict[str, Any]:
    artifact = first_available_artifact(check)
    if artifact is not None:
        return {
            "kind": "artifact",
            "name": artifact.get("name"),
            "locator": artifact.get("locator"),
        }

    step = first_failed_step_with_log(check)
    if step is not None:
        return {
            "kind": "step_log",
            "job_run_id": step.get("job_run_id", check.get("job_run_id")),
            "run_seq": step.get("run_seq", check.get("run_seq")),
            "step_id": step.get("id"),
            "step_name": step.get("name"),
        }

    if check.get("check_log_available") is True:
        return {
            "kind": "check_log",
            "check_run_id": check.get("id"),
        }
    return {"kind": "none"}


def check_summary(check: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "id": check.get("id"),
        "name": check.get("name"),
        "required": check.get("required"),
        "sha": check.get("sha"),
        "status": check.get("status"),
        "conclusion": check.get("conclusion"),
        "state": check_state(check),
    }


def names(checks: Iterable[Mapping[str, Any]]) -> List[str]:
    return sorted(str(check["name"]) for check in checks)


def evaluate(packet_value: Any) -> Dict[str, Any]:
    packet = require_mapping(packet_value, "input")
    expected_sha = packet.get("expected_sha")
    if not isinstance(expected_sha, str) or not expected_sha.strip():
        raise EvidenceError("expected_sha must be a non-empty string")

    expected_required_checks = require_name_list(
        packet, "expected_required_checks"
    )
    commit_checks = require_check_list(packet, "commit_checks")
    mr_checks = require_check_list(packet, "mr_checks")
    if commit_checks:
        query_source = "commit"
        selected = commit_checks
    else:
        query_source = "mr_aggregate"
        selected = mr_checks

    fresh = [check for check in selected if check.get("sha") == expected_sha]
    stale = [
        check
        for check in selected
        if isinstance(check.get("sha"), str)
        and check.get("sha")
        and check.get("sha") != expected_sha
    ]
    missing_sha = [check for check in selected if not check.get("sha")]
    required = [check for check in fresh if check["required"]]
    optional = [check for check in fresh if not check["required"]]
    stale_required = [check for check in stale if check["required"]]
    missing_required_sha = [check for check in missing_sha if check["required"]]
    fresh_required_names = {str(check["name"]) for check in required}
    missing_expected_required = sorted(
        set(expected_required_checks) - fresh_required_names
    )
    blocking_stale_required = (
        stale_required
        if query_source == "commit"
        else [
            check
            for check in stale_required
            if str(check["name"]) not in fresh_required_names
        ]
    )

    required_by_state = {
        state: [check for check in required if check_state(check) == state]
        for state in ("failed", "running", "queued", "unknown", "green")
    }

    if required_by_state["failed"]:
        state = "failed"
        next_action = "collect_failure_evidence"
    elif required_by_state["running"]:
        state = "running"
        next_action = "poll_current_sha"
    elif required_by_state["queued"]:
        state = "queued"
        next_action = "poll_current_sha"
    elif missing_required_sha:
        state = "unknown"
        next_action = "refresh_checks_with_sha"
    elif required_by_state["unknown"]:
        state = "unknown"
        next_action = "inspect_unknown_required_checks"
    elif blocking_stale_required:
        state = "stale"
        next_action = "refresh_current_sha_checks"
    elif missing_expected_required:
        state = "unknown"
        next_action = "refresh_missing_required_checks"
    elif required and len(required_by_state["green"]) == len(required):
        state = "green"
        next_action = "complete"
    elif selected:
        state = "no_required_checks"
        next_action = "confirm_required_check_policy"
    else:
        state = "unknown"
        next_action = "refresh_checks"

    failures = []
    for check in required_by_state["failed"]:
        failure = check_summary(check)
        failure["evidence"] = selected_evidence(check)
        failures.append(failure)

    return {
        "expected_sha": expected_sha,
        "query_source": query_source,
        "state": state,
        "complete": state == "green",
        "next_action": next_action,
        "counts": {
            "selected": len(selected),
            "fresh_required": len(required),
            "fresh_optional": len(optional),
            "stale_required": len(stale_required),
            "blocking_stale_required": len(blocking_stale_required),
            "missing_required_sha": len(missing_required_sha),
            "missing_expected_required": len(missing_expected_required),
        },
        "expected_required_checks": expected_required_checks,
        "required": [check_summary(check) for check in required],
        "optional": [check_summary(check) for check in optional],
        "stale": [check_summary(check) for check in stale],
        "missing_sha": [check_summary(check) for check in missing_sha],
        "failures": failures,
        "summary": {
            "required_failed": names(required_by_state["failed"]),
            "required_running": names(required_by_state["running"]),
            "required_queued": names(required_by_state["queued"]),
            "required_unknown": names(required_by_state["unknown"]),
            "missing_expected_required": missing_expected_required,
            "optional_failed": names(
                check for check in optional if check_state(check) == "failed"
            ),
        },
    }


def read_packet(path: str) -> Any:
    try:
        if path == "-":
            return json.load(sys.stdin)
        return json.loads(Path(path).read_text())
    except OSError as error:
        raise EvidenceError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise EvidenceError(
            f"invalid JSON in {path}: line {error.lineno} column {error.colno}"
        ) from error


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate normalized MR check evidence without network access."
    )
    parser.add_argument("evidence", help="JSON evidence file, or - for stdin")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact JSON instead of indented JSON",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        result = evaluate(read_packet(args.evidence))
    except EvidenceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.compact:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
