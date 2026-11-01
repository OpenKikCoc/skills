#!/usr/bin/env python3
"""Validate completed-wave evidence and advise a safe growth estimate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
REQUEST_KIND = "dev-disk-budget-growth-calibration-request"
RECEIPT_KIND = "dev-disk-budget-wave-growth-receipt"
ADVICE_KIND = "dev-disk-budget-growth-calibration-advice"
MIN_RECEIPTS = 2


class CalibrationError(ValueError):
    """Raised when calibration evidence is malformed."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CalibrationError(field, f"{field} must be an object")
    return value


def require_exact_keys(
    value: dict[str, Any], expected: set[str], field: str
) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise CalibrationError(
            field, f"{field} is missing fields: {', '.join(missing)}"
        )
    if unknown:
        raise CalibrationError(
            field, f"{field} has unknown fields: {', '.join(unknown)}"
        )


def require_nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CalibrationError(field, f"{field} must be a non-empty string")
    return value.strip()


def require_nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CalibrationError(field, f"{field} must be a non-negative integer")
    return value


def require_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CalibrationError(field, f"{field} must be an integer")
    return value


def validate_cache_identity(value: Any, field: str) -> dict[str, Any]:
    identity = require_object(value, field)
    require_exact_keys(
        identity,
        {"source", "path", "device", "inode", "uid"},
        field,
    )
    source = require_nonempty_string(identity["source"], f"{field}.source")
    path = require_nonempty_string(identity["path"], f"{field}.path")
    if not path.startswith("/"):
        raise CalibrationError(f"{field}.path", f"{field}.path must be absolute")
    return {
        "source": source,
        "path": path,
        "device": require_nonnegative_int(identity["device"], f"{field}.device"),
        "inode": require_nonnegative_int(identity["inode"], f"{field}.inode"),
        "uid": require_nonnegative_int(identity["uid"], f"{field}.uid"),
    }


def validate_owned_output(value: Any, field: str) -> dict[str, Any]:
    output = require_object(value, field)
    require_exact_keys(
        output,
        {
            "baseline_allocated_bytes",
            "peak_allocated_bytes",
            "newly_allocated_bytes",
            "ownership_validated",
        },
        field,
    )
    baseline = require_nonnegative_int(
        output["baseline_allocated_bytes"],
        f"{field}.baseline_allocated_bytes",
    )
    peak = require_nonnegative_int(
        output["peak_allocated_bytes"], f"{field}.peak_allocated_bytes"
    )
    newly_allocated = require_nonnegative_int(
        output["newly_allocated_bytes"], f"{field}.newly_allocated_bytes"
    )
    if not isinstance(output["ownership_validated"], bool):
        raise CalibrationError(
            f"{field}.ownership_validated",
            f"{field}.ownership_validated must be boolean",
        )
    if peak < baseline:
        raise CalibrationError(
            field, f"{field} peak_allocated_bytes must not be below its baseline"
        )
    if peak - baseline != newly_allocated:
        raise CalibrationError(
            field,
            f"{field} newly_allocated_bytes must equal peak minus baseline",
        )
    return {
        "baseline_allocated_bytes": baseline,
        "peak_allocated_bytes": peak,
        "newly_allocated_bytes": newly_allocated,
        "ownership_validated": output["ownership_validated"],
    }


def validate_cache_delta(
    value: Any,
    field: str,
    *,
    before_name: str,
    after_name: str,
) -> dict[str, Any]:
    observation = require_object(value, field)
    expected = {
        before_name,
        after_name,
        "delta_allocated_bytes",
        "growth_explanation",
    }
    if field == "current_shared_cache":
        expected.add("active")
    require_exact_keys(observation, expected, field)
    before = require_nonnegative_int(observation[before_name], f"{field}.{before_name}")
    after = require_nonnegative_int(observation[after_name], f"{field}.{after_name}")
    delta = require_int(
        observation["delta_allocated_bytes"],
        f"{field}.delta_allocated_bytes",
    )
    if after - before != delta:
        raise CalibrationError(
            field, f"{field} delta_allocated_bytes must equal after minus before"
        )
    explanation = observation["growth_explanation"]
    if explanation is not None and not isinstance(explanation, str):
        raise CalibrationError(
            f"{field}.growth_explanation",
            f"{field}.growth_explanation must be a string or null",
        )
    result: dict[str, Any] = {
        before_name: before,
        after_name: after,
        "delta_allocated_bytes": delta,
        "growth_explanation": (
            explanation.strip() if isinstance(explanation, str) else None
        ),
    }
    if field == "current_shared_cache":
        if not isinstance(observation["active"], bool):
            raise CalibrationError(
                f"{field}.active", f"{field}.active must be boolean"
            )
        result["active"] = observation["active"]
    return result


def validate_receipt(value: Any, index: int) -> dict[str, Any]:
    field = f"completed_wave_receipts[{index}]"
    receipt = require_object(value, field)
    require_exact_keys(
        receipt,
        {
            "schema_version",
            "kind",
            "status",
            "wave_id",
            "command_class",
            "target_class",
            "cache_identity",
            "owned_output",
            "shared_cache",
        },
        field,
    )
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise CalibrationError(field, f"{field}.schema_version must be 1")
    if receipt["kind"] != RECEIPT_KIND:
        raise CalibrationError(field, f"{field}.kind must be {RECEIPT_KIND}")
    status = require_nonempty_string(receipt["status"], f"{field}.status")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "status": status,
        "wave_id": require_nonempty_string(receipt["wave_id"], f"{field}.wave_id"),
        "command_class": require_nonempty_string(
            receipt["command_class"], f"{field}.command_class"
        ),
        "target_class": require_nonempty_string(
            receipt["target_class"], f"{field}.target_class"
        ),
        "cache_identity": validate_cache_identity(
            receipt["cache_identity"], f"{field}.cache_identity"
        ),
        "owned_output": validate_owned_output(
            receipt["owned_output"], f"{field}.owned_output"
        ),
        "shared_cache": validate_cache_delta(
            receipt["shared_cache"],
            f"{field}.shared_cache",
            before_name="before_allocated_bytes",
            after_name="after_allocated_bytes",
        ),
    }


def validate_disk(value: Any) -> dict[str, int | float]:
    disk = require_object(value, "disk")
    require_exact_keys(
        disk,
        {"total_bytes", "free_bytes", "min_free_bytes", "min_free_percent"},
        "disk",
    )
    total = require_nonnegative_int(disk["total_bytes"], "disk.total_bytes")
    free = require_nonnegative_int(disk["free_bytes"], "disk.free_bytes")
    floor_bytes = require_nonnegative_int(
        disk["min_free_bytes"], "disk.min_free_bytes"
    )
    percent = disk["min_free_percent"]
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        raise CalibrationError(
            "disk.min_free_percent", "disk.min_free_percent must be numeric"
        )
    percent = float(percent)
    if total <= 0:
        raise CalibrationError("disk.total_bytes", "disk.total_bytes must be positive")
    if free > total:
        raise CalibrationError(
            "disk.free_bytes", "disk.free_bytes must not exceed total_bytes"
        )
    if not 0 <= percent <= 100:
        raise CalibrationError(
            "disk.min_free_percent",
            "disk.min_free_percent must be between 0 and 100",
        )
    return {
        "total_bytes": total,
        "free_bytes": free,
        "min_free_bytes": floor_bytes,
        "min_free_percent": percent,
    }


def disk_gate(disk: dict[str, int | float], estimate: int) -> dict[str, Any]:
    total = int(disk["total_bytes"])
    free = int(disk["free_bytes"])
    projected = max(0, free - estimate)
    projected_percent = projected / total * 100
    absolute_ok = projected >= int(disk["min_free_bytes"])
    percentage_ok = projected_percent >= float(disk["min_free_percent"])
    return {
        "evaluated_expected_growth_bytes": estimate,
        "projected_free_bytes": projected,
        "projected_free_percent": projected_percent,
        "min_free_bytes": int(disk["min_free_bytes"]),
        "min_free_percent": float(disk["min_free_percent"]),
        "absolute_floor_passed": absolute_ok,
        "percentage_floor_passed": percentage_ok,
        "passed": absolute_ok and percentage_ok,
    }


def malformed_advice(error: CalibrationError) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": ADVICE_KIND,
        "status": "blocked",
        "decision": "blocked_malformed_evidence",
        "reason_codes": ["MALFORMED_EVIDENCE"],
        "errors": [
            {
                "code": "MALFORMED_EVIDENCE",
                "field": error.field,
                "message": str(error),
            }
        ],
        "calibration_permitted": False,
        "launch_allowed": False,
        "shared_cache_headroom_credit_bytes": 0,
        "mutation_authorized": False,
    }


def _advise(value: Any) -> dict[str, Any]:
    request = require_object(value, "request")
    require_exact_keys(
        request,
        {
            "schema_version",
            "kind",
            "requested_expected_growth_bytes",
            "completed_wave_receipts",
            "command_class",
            "target_class",
            "cache_identity",
            "current_shared_cache",
            "safety_margin_bytes",
            "disk",
        },
        "request",
    )
    if request["schema_version"] != SCHEMA_VERSION:
        raise CalibrationError("schema_version", "schema_version must be 1")
    if request["kind"] != REQUEST_KIND:
        raise CalibrationError("kind", f"kind must be {REQUEST_KIND}")

    requested = require_nonnegative_int(
        request["requested_expected_growth_bytes"],
        "requested_expected_growth_bytes",
    )
    safety_margin = require_nonnegative_int(
        request["safety_margin_bytes"], "safety_margin_bytes"
    )
    command_class = require_nonempty_string(request["command_class"], "command_class")
    target_class = require_nonempty_string(request["target_class"], "target_class")
    cache_identity = validate_cache_identity(
        request["cache_identity"], "cache_identity"
    )
    current_cache = validate_cache_delta(
        request["current_shared_cache"],
        "current_shared_cache",
        before_name="previous_allocated_bytes",
        after_name="current_allocated_bytes",
    )
    disk = validate_disk(request["disk"])
    raw_receipts = request["completed_wave_receipts"]
    if not isinstance(raw_receipts, list):
        raise CalibrationError(
            "completed_wave_receipts", "completed_wave_receipts must be an array"
        )
    receipts = [
        validate_receipt(item, index) for index, item in enumerate(raw_receipts)
    ]
    wave_ids = [receipt["wave_id"] for receipt in receipts]
    if len(set(wave_ids)) != len(wave_ids):
        raise CalibrationError(
            "completed_wave_receipts",
            "completed-wave receipt wave_id values must be unique",
        )

    reason_codes: list[str] = []
    if safety_margin == 0:
        reason_codes.append("SAFETY_MARGIN_NOT_POSITIVE")
    if len(receipts) < MIN_RECEIPTS:
        reason_codes.append("INSUFFICIENT_COMPLETED_RECEIPTS")
    if any(receipt["status"] != "completed" for receipt in receipts):
        reason_codes.append("RECEIPT_NOT_COMPLETED")
    if any(receipt["command_class"] != command_class for receipt in receipts):
        reason_codes.append("COMMAND_CLASS_DRIFT")
    if any(receipt["target_class"] != target_class for receipt in receipts):
        reason_codes.append("TARGET_CLASS_DRIFT")
    if any(receipt["cache_identity"] != cache_identity for receipt in receipts):
        reason_codes.append("CACHE_IDENTITY_DRIFT")
    if any(
        not receipt["owned_output"]["ownership_validated"]
        for receipt in receipts
    ):
        reason_codes.append("OWNED_OUTPUT_NOT_VALIDATED")
    if not current_cache["active"]:
        reason_codes.append("SHARED_CACHE_NOT_ACTIVE")

    cache_observations = [receipt["shared_cache"] for receipt in receipts]
    all_cache_observations = [*cache_observations, current_cache]
    if any(
        observation["delta_allocated_bytes"] > 0
        and not observation["growth_explanation"]
        for observation in all_cache_observations
    ):
        reason_codes.append("SHARED_CACHE_GROWTH_UNEXPLAINED")

    for previous, current in zip(cache_observations, cache_observations[1:]):
        if previous["after_allocated_bytes"] != current["before_allocated_bytes"]:
            reason_codes.append("SHARED_CACHE_LINEAGE_GAP")
            break
    if cache_observations and (
        cache_observations[-1]["after_allocated_bytes"]
        != current_cache["previous_allocated_bytes"]
    ):
        reason_codes.append("SHARED_CACHE_LINEAGE_GAP")

    peak_owned = max(
        (receipt["owned_output"]["newly_allocated_bytes"] for receipt in receipts),
        default=0,
    )
    peak_cache_growth = max(
        (
            max(0, observation["delta_allocated_bytes"])
            for observation in all_cache_observations
        ),
        default=0,
    )
    candidate = peak_owned + peak_cache_growth + safety_margin
    evidence_reason_codes = list(dict.fromkeys(reason_codes))
    candidate_gate = disk_gate(disk, candidate)
    conservative_gate = disk_gate(disk, requested)

    calibration_eligible = not evidence_reason_codes and candidate < requested
    if not evidence_reason_codes and candidate >= requested:
        reason_codes.append("CALIBRATION_NOT_LOWER")

    if calibration_eligible and not candidate_gate["passed"]:
        if not candidate_gate["absolute_floor_passed"]:
            reason_codes.append("CALIBRATED_ABSOLUTE_FLOOR_FAILED")
        if not candidate_gate["percentage_floor_passed"]:
            reason_codes.append("CALIBRATED_PERCENTAGE_FLOOR_FAILED")
        decision = "blocked_disk_floor"
        status = "blocked"
        effective = requested
        calibration_permitted = False
        launch_allowed = False
    elif calibration_eligible:
        reason_codes.append("CALIBRATION_APPLIED")
        decision = "calibrated"
        status = "ready"
        effective = candidate
        calibration_permitted = True
        launch_allowed = True
    else:
        reason_codes.append("CONSERVATIVE_ESTIMATE_RETAINED")
        if not conservative_gate["absolute_floor_passed"]:
            reason_codes.append("CONSERVATIVE_ABSOLUTE_FLOOR_FAILED")
        if not conservative_gate["percentage_floor_passed"]:
            reason_codes.append("CONSERVATIVE_PERCENTAGE_FLOOR_FAILED")
        launch_allowed = bool(conservative_gate["passed"])
        decision = "conservative_retained" if launch_allowed else "blocked_disk_floor"
        status = "ready" if launch_allowed else "blocked"
        effective = requested
        calibration_permitted = False

    reason_codes.append("ACTIVE_SHARED_CACHE_EXCLUDED_FROM_HEADROOM")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": ADVICE_KIND,
        "status": status,
        "decision": decision,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "requested_expected_growth_bytes": requested,
        "candidate_expected_growth_bytes": candidate,
        "effective_expected_growth_bytes": effective,
        "calibration_permitted": calibration_permitted,
        "launch_allowed": launch_allowed,
        "evidence": {
            "receipt_count": len(receipts),
            "minimum_receipt_count": MIN_RECEIPTS,
            "command_class": command_class,
            "target_class": target_class,
            "cache_identity": cache_identity,
            "peak_owned_output_newly_allocated_bytes": peak_owned,
            "peak_shared_cache_growth_bytes": peak_cache_growth,
            "safety_margin_bytes": safety_margin,
        },
        "disk": {
            **disk,
            "candidate_gate": candidate_gate,
            "effective_gate": disk_gate(disk, effective),
        },
        "shared_cache_headroom_credit_bytes": 0,
        "mutation_authorized": False,
    }


def advise(value: Any) -> dict[str, Any]:
    try:
        return _advise(value)
    except CalibrationError as error:
        return malformed_advice(error)


def load_input(path: str) -> Any:
    try:
        content = (
            sys.stdin.read()
            if path == "-"
            else Path(path).read_text(encoding="utf-8")
        )
    except OSError as error:
        raise CalibrationError("input", f"cannot read input {path}: {error}") from error
    try:
        return json.loads(content)
    except json.JSONDecodeError as error:
        raise CalibrationError(
            "input", f"cannot parse input {path}: {error}"
        ) from error


def write_output(document: dict[str, Any], path: str | None) -> None:
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if path in (None, "-"):
        sys.stdout.write(rendered)
    else:
        try:
            Path(path).write_text(rendered, encoding="utf-8")
        except OSError as error:
            raise CalibrationError(
                "output", f"cannot write output {path}: {error}"
            ) from error


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate homogeneous wave receipts and advise disk growth."
    )
    parser.add_argument(
        "--input", required=True, help="Request JSON path or - for stdin."
    )
    parser.add_argument(
        "--output", help="Write advice JSON to this path instead of stdout."
    )
    arguments = parser.parse_args()
    try:
        request = load_input(arguments.input)
        result = advise(request)
        write_output(result, arguments.output)
    except CalibrationError as error:
        result = malformed_advice(error)
        sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
        return 2
    if result["status"] == "ready":
        return 0
    return 2 if result["decision"] == "blocked_malformed_evidence" else 75


if __name__ == "__main__":
    raise SystemExit(main())
