#!/usr/bin/env python3
"""Validate a pure external-write transaction event ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ROOT_FIELDS = {
    "schema_version",
    "provider_contract",
    "operation",
    "events",
    "claim",
}
PROVIDER_FIELDS = {
    "idempotency_key_support",
    "same_key_replay_guaranteed",
    "retry_after_authoritative_absence",
    "authoritative_read_support",
    "authority_locator",
    "authoritative_read_schema_revision",
}
OPERATION_FIELDS = {
    "operation_id",
    "intent",
    "intent_sha256",
    "authoritative_query",
    "authoritative_query_sha256",
    "retry_key",
    "prior_operations",
}
PRIOR_OPERATION_FIELDS = {"operation_id", "intent_sha256", "retry_key"}
READ_FIELDS = {
    "type",
    "phase",
    "authority_locator",
    "query_sha256",
    "provider_schema_revision",
    "observed_after_attempt",
    "matches",
}
MATCH_FIELDS = {"operation_id", "intent_sha256", "record_id"}
WRITE_FIELDS = {
    "type",
    "attempt",
    "operation_id",
    "intent_sha256",
    "retry_key",
    "outcome",
}
CLAIM_FIELDS = {"state", "next_action", "outcome_unknown_seen", "late_success"}
WRITE_OUTCOMES = {
    "success",
    "definitive_failure",
    "transport_ambiguous",
    "ui_ambiguous",
}
NEXT_ACTIONS = {
    "ready_to_write": "write_once",
    "already_applied": "stop",
    "applied": "stop",
    "failed": "stop",
    "outcome_unknown": "reconcile_authoritative_read",
    "retry_allowed": "retry_same_operation",
    "blocked": "stop_and_escalate",
}
TERMINAL_STATES = {"already_applied", "applied", "failed", "blocked"}


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _exact_fields(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise ValueError(f"{label} missing fields: {missing}")
    if unknown:
        raise ValueError(f"{label} unsupported fields: {unknown}")


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be a non-empty single-line string")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def intent_digest(intent: Any) -> str:
    """Return the contract's canonical immutable-intent digest."""
    try:
        canonical = json.dumps(
            intent,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("operation.intent must be canonical JSON data") from error
    return hashlib.sha256(canonical).hexdigest()


def _provider(value: Any) -> dict[str, Any]:
    provider = _object(value, "provider_contract")
    _exact_fields(provider, PROVIDER_FIELDS, "provider_contract")
    for field in (
        "idempotency_key_support",
        "same_key_replay_guaranteed",
        "retry_after_authoritative_absence",
        "authoritative_read_support",
    ):
        if not isinstance(provider[field], bool):
            raise ValueError(f"provider_contract.{field} must be boolean")
    _nonempty(
        provider["authority_locator"], "provider_contract.authority_locator"
    )
    _nonempty(
        provider["authoritative_read_schema_revision"],
        "provider_contract.authoritative_read_schema_revision",
    )
    if (
        provider["same_key_replay_guaranteed"]
        and not provider["idempotency_key_support"]
    ):
        raise ValueError(
            "same-key replay cannot be guaranteed without idempotency-key support"
        )
    if (
        provider["retry_after_authoritative_absence"]
        and not provider["same_key_replay_guaranteed"]
    ):
        raise ValueError(
            "retry safety requires a same-key same-operation replay guarantee"
        )
    if provider["authoritative_read_support"] is not True:
        raise ValueError(
            "provider must declare authoritative read support for preflight and reconciliation"
        )
    return provider


def _retry_key(value: Any, supported: bool, label: str) -> str | None:
    if supported:
        return _nonempty(value, label)
    if value is not None:
        raise ValueError(f"{label} must be null when idempotency keys are unsupported")
    return None


def _operation(value: Any, key_supported: bool) -> dict[str, Any]:
    operation = _object(value, "operation")
    _exact_fields(operation, OPERATION_FIELDS, "operation")
    operation_id = _nonempty(operation["operation_id"], "operation.operation_id")
    supplied_digest = _sha256(
        operation["intent_sha256"], "operation.intent_sha256"
    )
    if supplied_digest != intent_digest(operation["intent"]):
        raise ValueError("operation.intent_sha256 does not match canonical intent")
    query_digest = _sha256(
        operation["authoritative_query_sha256"],
        "operation.authoritative_query_sha256",
    )
    if query_digest != intent_digest(operation["authoritative_query"]):
        raise ValueError(
            "operation.authoritative_query_sha256 does not match canonical query"
        )
    retry_key = _retry_key(
        operation["retry_key"], key_supported, "operation.retry_key"
    )
    prior = operation["prior_operations"]
    if not isinstance(prior, list):
        raise ValueError("operation.prior_operations must be a list")
    seen_ids = {operation_id}
    seen_keys = {retry_key} if retry_key is not None else set()
    for index, raw_entry in enumerate(prior):
        label = f"operation.prior_operations[{index}]"
        entry = _object(raw_entry, label)
        _exact_fields(entry, PRIOR_OPERATION_FIELDS, label)
        prior_id = _nonempty(entry["operation_id"], f"{label}.operation_id")
        _sha256(entry["intent_sha256"], f"{label}.intent_sha256")
        prior_key = _retry_key(entry["retry_key"], key_supported, f"{label}.retry_key")
        if prior_id in seen_ids:
            raise ValueError("new operations must rotate operation identity")
        seen_ids.add(prior_id)
        if prior_key is not None:
            if prior_key in seen_keys:
                raise ValueError("new operations must rotate the idempotency retry key")
            seen_keys.add(prior_key)
    return operation


def _matches(value: Any, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    result = []
    record_ids: set[str] = set()
    for index, raw_match in enumerate(value):
        match_label = f"{label}[{index}]"
        match = _object(raw_match, match_label)
        _exact_fields(match, MATCH_FIELDS, match_label)
        _nonempty(match["operation_id"], f"{match_label}.operation_id")
        _sha256(match["intent_sha256"], f"{match_label}.intent_sha256")
        record_id = _nonempty(match["record_id"], f"{match_label}.record_id")
        if record_id in record_ids:
            raise ValueError(f"{label} contains duplicate provider record IDs")
        record_ids.add(record_id)
        result.append(match)
    return result


def _read_result(
    matches: list[dict[str, str]], operation: dict[str, Any]
) -> str:
    if not matches:
        return "absent"
    if len(matches) != 1:
        return "blocked"
    match = matches[0]
    if (
        match["operation_id"] == operation["operation_id"]
        and match["intent_sha256"] == operation["intent_sha256"]
    ):
        return "success"
    return "blocked"


def _validate_read(
    raw_event: Any,
    index: int,
    expected_phase: str,
    expected_attempt: int,
    operation: dict[str, Any],
    provider: dict[str, Any],
) -> str:
    label = f"events[{index}]"
    event = _object(raw_event, label)
    if event.get("type") != "authoritative_read":
        raise ValueError(f"{label}.type must be authoritative_read")
    _exact_fields(event, READ_FIELDS, label)
    if event["phase"] != expected_phase:
        raise ValueError(f"{label}.phase must be {expected_phase}")
    if event["authority_locator"] != provider["authority_locator"]:
        raise ValueError(f"{label} authority locator is not authoritative")
    if event["query_sha256"] != operation["authoritative_query_sha256"]:
        raise ValueError(f"{label} query digest does not match the operation query")
    if (
        event["provider_schema_revision"]
        != provider["authoritative_read_schema_revision"]
    ):
        raise ValueError(f"{label} provider schema revision is stale or mismatched")
    observed_after = event["observed_after_attempt"]
    if (
        not isinstance(observed_after, int)
        or isinstance(observed_after, bool)
        or observed_after != expected_attempt
    ):
        raise ValueError(
            f"{label}.observed_after_attempt must be {expected_attempt} for freshness"
        )
    matches = _matches(event["matches"], f"{label}.matches")
    return _read_result(matches, operation)


def _validate_write(
    raw_event: Any,
    index: int,
    expected_attempt: int,
    operation: dict[str, Any],
    provider: dict[str, bool],
) -> str:
    label = f"events[{index}]"
    event = _object(raw_event, label)
    if event.get("type") != "write_attempt":
        raise ValueError(f"{label}.type must be write_attempt")
    _exact_fields(event, WRITE_FIELDS, label)
    if (
        not isinstance(event["attempt"], int)
        or isinstance(event["attempt"], bool)
        or event["attempt"] != expected_attempt
    ):
        raise ValueError(f"{label}.attempt must be {expected_attempt}")
    if event["operation_id"] != operation["operation_id"]:
        raise ValueError("all attempts must reuse the stable operation identity")
    if event["intent_sha256"] != operation["intent_sha256"]:
        raise ValueError("operation intent is immutable across attempts")
    if event["retry_key"] != operation["retry_key"]:
        raise ValueError("same-operation attempts must reuse the same retry key")
    if expected_attempt > 1 and not provider["same_key_replay_guaranteed"]:
        raise ValueError("provider contract does not support same-operation retries")
    outcome = event["outcome"]
    if outcome not in WRITE_OUTCOMES:
        raise ValueError(f"{label}.outcome must be one of {sorted(WRITE_OUTCOMES)}")
    return outcome


def _claim(value: Any, state: str, ambiguity_seen: bool, late_success: bool) -> None:
    claim = _object(value, "claim")
    _exact_fields(claim, CLAIM_FIELDS, "claim")
    if claim["state"] != state:
        raise ValueError(f"claim.state must be {state} for this event ledger")
    expected_action = NEXT_ACTIONS[state]
    if claim["next_action"] != expected_action:
        raise ValueError(f"claim.next_action must be {expected_action}")
    if claim["outcome_unknown_seen"] is not ambiguity_seen:
        raise ValueError("claim.outcome_unknown_seen does not match the event ledger")
    if claim["late_success"] is not late_success:
        raise ValueError("claim.late_success does not match reconciliation evidence")


def validate_packet(packet: Any) -> dict[str, Any]:
    packet = _object(packet, "input")
    _exact_fields(packet, ROOT_FIELDS, "input")
    if packet["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    provider = _provider(packet["provider_contract"])
    operation = _operation(packet["operation"], provider["idempotency_key_support"])
    events = packet["events"]
    if not isinstance(events, list) or not events:
        raise ValueError("events must be a non-empty ordered list")

    preflight = _validate_read(
        events[0], 0, "preflight", 0, operation, provider
    )
    state = {
        "absent": "ready_to_write",
        "success": "already_applied",
        "blocked": "blocked",
    }[preflight]
    ambiguity_seen = False
    late_success = False
    attempts = 0

    for index, raw_event in enumerate(events[1:], start=1):
        if state in TERMINAL_STATES:
            raise ValueError(f"events[{index}] appears after terminal state {state}")
        if state == "outcome_unknown":
            read_result = _validate_read(
                raw_event,
                index,
                "reconciliation",
                attempts,
                operation,
                provider,
            )
            if read_result == "success":
                state = "applied"
                late_success = True
            elif read_result == "blocked":
                state = "blocked"
            elif (
                provider["retry_after_authoritative_absence"]
                and provider["same_key_replay_guaranteed"]
            ):
                state = "retry_allowed"
            else:
                state = "blocked"
            continue
        if state not in {"ready_to_write", "retry_allowed"}:
            raise ValueError(f"events[{index}] is invalid while state is {state}")
        attempts += 1
        outcome = _validate_write(
            raw_event, index, attempts, operation, provider
        )
        if outcome == "success":
            state = "applied"
        elif outcome == "definitive_failure":
            state = "failed"
        else:
            state = "outcome_unknown"
            ambiguity_seen = True

    _claim(packet["claim"], state, ambiguity_seen, late_success)
    return {
        "schema_version": 1,
        "valid": True,
        "state": state,
        "next_action": NEXT_ACTIONS[state],
        "attempts": attempts,
        "outcome_unknown_seen": ambiguity_seen,
        "late_success": late_success,
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
