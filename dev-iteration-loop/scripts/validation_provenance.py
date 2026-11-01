#!/usr/bin/env python3
"""Validate non-performance baseline/candidate failure provenance."""

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
    "non_performance_work",
    "artifacts",
    "runtime",
    "suppressors",
    "runs",
    "claim",
}
ARTIFACT_FIELDS = {"baseline_sha256", "candidate_sha256"}
RUNTIME_FIELDS = {"test_owned", "epochs"}
EPOCH_FIELDS = {
    "runtime_id",
    "epoch_id",
    "predecessor_epoch_id",
    "activated_artifact_sha256",
    "fresh",
    "reset_before_run",
}
SUPPRESSOR_FIELDS = {"inventory", "default_release_active"}
SUPPRESSOR_ENTRY_FIELDS = {"id", "kind", "enabled_by_default_or_release"}
SUPPRESSOR_KINDS = {
    "skip",
    "retry",
    "filter",
    "exclusion",
    "feature_override",
    "other",
}
RUN_FIELDS = {
    "executed",
    "artifact_sha256",
    "epoch_id",
    "profile",
    "active_suppressors",
    "comparison_surface",
    "outcome",
    "failures",
}
COMPARISON_SURFACE_FIELDS = {
    "validator_sha256",
    "fixture_sha256",
    "command_sha256",
    "parser_sha256",
    "environment_sha256",
}
FAILURE_FIELDS = {
    "normalizer_id",
    "raw",
    "raw_sha256",
    "normalized",
    "normalized_sha256",
    "binding_sha256",
}
CLAIM_FIELDS = {"classification", "regression_claim"}
CLASSIFICATIONS = {
    "regression",
    "pre_existing",
    "fixed",
    "changed",
    "unknown",
}


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


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def signature_binding(
    normalizer_id: str, raw_sha256: str, normalized_sha256: str
) -> str:
    canonical = json.dumps(
        {
            "normalizer_id": normalizer_id,
            "normalized_sha256": normalized_sha256,
            "raw_sha256": raw_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _artifacts(value: Any) -> dict[str, str]:
    artifacts = _object(value, "artifacts")
    _exact_fields(artifacts, ARTIFACT_FIELDS, "artifacts")
    baseline = _sha256(artifacts["baseline_sha256"], "artifacts.baseline_sha256")
    candidate = _sha256(
        artifacts["candidate_sha256"], "artifacts.candidate_sha256"
    )
    if baseline == candidate:
        raise ValueError("baseline and candidate artifacts must be distinct")
    return {"baseline": baseline, "candidate": candidate}


def _epoch(
    value: Any, role: str, artifact: str, test_owned: bool
) -> dict[str, Any] | None:
    if value is None:
        return None
    label = f"runtime.epochs.{role}"
    epoch = _object(value, label)
    _exact_fields(epoch, EPOCH_FIELDS, label)
    _nonempty(epoch["runtime_id"], f"{label}.runtime_id")
    _nonempty(epoch["epoch_id"], f"{label}.epoch_id")
    predecessor = epoch["predecessor_epoch_id"]
    if predecessor is not None:
        _nonempty(predecessor, f"{label}.predecessor_epoch_id")
    if _sha256(
        epoch["activated_artifact_sha256"],
        f"{label}.activated_artifact_sha256",
    ) != artifact:
        raise ValueError(f"{label} is not bound to the {role} artifact")
    if epoch["fresh"] is not True:
        raise ValueError(f"{label} must attest a fresh runtime epoch")
    if not isinstance(epoch["reset_before_run"], bool):
        raise ValueError(f"{label}.reset_before_run must be boolean")
    if epoch["reset_before_run"] and not test_owned:
        raise ValueError("runtime reset is allowed only for a test-owned runtime")
    if epoch["reset_before_run"] != (predecessor is not None):
        raise ValueError(
            f"{label} reset and predecessor evidence must be declared together"
        )
    return epoch


def _runtime(value: Any, artifacts: dict[str, str]) -> dict[str, Any]:
    runtime = _object(value, "runtime")
    _exact_fields(runtime, RUNTIME_FIELDS, "runtime")
    if not isinstance(runtime["test_owned"], bool):
        raise ValueError("runtime.test_owned must be boolean")
    epochs = _object(runtime["epochs"], "runtime.epochs")
    _exact_fields(epochs, {"baseline", "candidate"}, "runtime.epochs")
    result = {
        role: _epoch(
            epochs[role], role, artifacts[role], runtime["test_owned"]
        )
        for role in ("baseline", "candidate")
    }
    present = [epoch for epoch in result.values() if epoch is not None]
    if len({epoch["epoch_id"] for epoch in present}) != len(present):
        raise ValueError("each run must use a distinct fresh runtime epoch")
    baseline = result["baseline"]
    candidate = result["candidate"]
    if (
        baseline is not None
        and candidate is not None
        and baseline["runtime_id"] == candidate["runtime_id"]
    ):
        if candidate["predecessor_epoch_id"] != baseline["epoch_id"]:
            raise ValueError(
                "same-runtime candidate must reset directly from the baseline epoch"
            )
        if candidate["reset_before_run"] is not True:
            raise ValueError("same-runtime candidate requires a test-owned reset")
    return result


def _suppressors(value: Any) -> set[str]:
    suppressors = _object(value, "suppressors")
    _exact_fields(suppressors, SUPPRESSOR_FIELDS, "suppressors")
    inventory = suppressors["inventory"]
    if not isinstance(inventory, list):
        raise ValueError("suppressors.inventory must be a list")
    identifiers: set[str] = set()
    for index, raw_entry in enumerate(inventory):
        label = f"suppressors.inventory[{index}]"
        entry = _object(raw_entry, label)
        _exact_fields(entry, SUPPRESSOR_ENTRY_FIELDS, label)
        identifier = _nonempty(entry["id"], f"{label}.id")
        if identifier in identifiers:
            raise ValueError("suppressor inventory IDs must be unique")
        identifiers.add(identifier)
        if entry["kind"] not in SUPPRESSOR_KINDS:
            raise ValueError(f"{label}.kind must be one of {sorted(SUPPRESSOR_KINDS)}")
        if entry["enabled_by_default_or_release"] is not False:
            raise ValueError("default/release validation must not enable suppressors")
    active = suppressors["default_release_active"]
    if not isinstance(active, list) or any(not isinstance(item, str) for item in active):
        raise ValueError("suppressors.default_release_active must be a string list")
    unknown = sorted(set(active) - identifiers)
    if unknown:
        raise ValueError(f"default/release active suppressors are not inventoried: {unknown}")
    if active:
        raise ValueError("default/release validation must have no active suppressors")
    return identifiers


def _failure(value: Any, label: str) -> tuple[str, str]:
    failure = _object(value, label)
    _exact_fields(failure, FAILURE_FIELDS, label)
    normalizer_id = _nonempty(failure["normalizer_id"], f"{label}.normalizer_id")
    raw = failure["raw"]
    normalized = failure["normalized"]
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{label}.raw must be a non-empty string")
    if not isinstance(normalized, str) or not normalized:
        raise ValueError(f"{label}.normalized must be a non-empty string")
    raw_digest = _sha256(failure["raw_sha256"], f"{label}.raw_sha256")
    normalized_digest = _sha256(
        failure["normalized_sha256"], f"{label}.normalized_sha256"
    )
    if raw_digest != _text_sha256(raw):
        raise ValueError(f"{label}.raw_sha256 does not match raw failure bytes")
    if normalized_digest != _text_sha256(normalized):
        raise ValueError(
            f"{label}.normalized_sha256 does not match normalized failure bytes"
        )
    binding = _sha256(failure["binding_sha256"], f"{label}.binding_sha256")
    if binding != signature_binding(normalizer_id, raw_digest, normalized_digest):
        raise ValueError(f"{label}.binding_sha256 does not bind raw and normalized signatures")
    return normalizer_id, normalized_digest


def _comparison_surface(value: Any, label: str) -> dict[str, str]:
    surface = _object(value, label)
    _exact_fields(surface, COMPARISON_SURFACE_FIELDS, label)
    return {
        field: _sha256(surface[field], f"{label}.{field}")
        for field in sorted(COMPARISON_SURFACE_FIELDS)
    }


def _run(
    value: Any,
    role: str,
    artifact: str,
    epoch: dict[str, Any] | None,
    inventory: set[str],
) -> dict[str, Any]:
    label = f"runs.{role}"
    run = _object(value, label)
    _exact_fields(run, RUN_FIELDS, label)
    if not isinstance(run["executed"], bool):
        raise ValueError(f"{label}.executed must be boolean")
    if _sha256(run["artifact_sha256"], f"{label}.artifact_sha256") != artifact:
        raise ValueError(f"{label} artifact does not match the pinned {role} artifact")
    if run["profile"] != "default_release":
        raise ValueError(f"{label}.profile must be default_release")
    active = run["active_suppressors"]
    if not isinstance(active, list) or any(not isinstance(item, str) for item in active):
        raise ValueError(f"{label}.active_suppressors must be a string list")
    unknown = sorted(set(active) - inventory)
    if unknown:
        raise ValueError(f"{label} active suppressors are not inventoried: {unknown}")
    if active:
        raise ValueError(f"{label} default/release run must not activate suppressors")
    outcome = run["outcome"]
    if outcome not in {"not_run", "pass", "fail"}:
        raise ValueError(f"{label}.outcome must be not_run, pass, or fail")
    failures = run["failures"]
    if not isinstance(failures, list):
        raise ValueError(f"{label}.failures must be a list")
    if run["executed"]:
        if outcome == "not_run":
            raise ValueError(f"{label} executed run cannot have outcome not_run")
        if epoch is None or run["epoch_id"] != epoch["epoch_id"]:
            raise ValueError(f"{label} must bind its matching fresh runtime epoch")
        surface = _comparison_surface(
            run["comparison_surface"], f"{label}.comparison_surface"
        )
    else:
        if outcome != "not_run" or run["epoch_id"] is not None:
            raise ValueError(f"{label} unexecuted run must use not_run and null epoch_id")
        if epoch is not None:
            raise ValueError(f"{label} unexecuted run must not carry an epoch receipt")
        if run["comparison_surface"] is not None:
            raise ValueError(
                f"{label} unexecuted run must use null comparison_surface"
            )
        surface = None
    if outcome == "fail" and not failures:
        raise ValueError(f"{label} failed run must include at least one failure signature")
    if outcome != "fail" and failures:
        raise ValueError(f"{label} non-failing run must not include failure signatures")
    signatures = [_failure(item, f"{label}.failures[{index}]") for index, item in enumerate(failures)]
    if len(set(signatures)) != len(signatures):
        raise ValueError(f"{label} failure signatures must be unique")
    return {
        "executed": run["executed"],
        "outcome": outcome,
        "signatures": set(signatures),
        "comparison_surface": surface,
    }


def _classification(
    baseline: dict[str, Any], candidate: dict[str, Any], surface_differences: list[str]
) -> str:
    if not baseline["executed"] or not candidate["executed"]:
        return "unknown"
    if surface_differences:
        return "unknown"
    pair = (baseline["outcome"], candidate["outcome"])
    if pair == ("pass", "fail"):
        return "regression"
    if pair == ("fail", "pass"):
        return "fixed"
    if pair != ("fail", "fail"):
        return "unknown"
    baseline_namespaces = {item[0] for item in baseline["signatures"]}
    candidate_namespaces = {item[0] for item in candidate["signatures"]}
    if baseline_namespaces != candidate_namespaces:
        return "unknown"
    if baseline["signatures"] == candidate["signatures"]:
        return "pre_existing"
    return "changed"


def _claim(value: Any, classification: str) -> None:
    claim = _object(value, "claim")
    _exact_fields(claim, CLAIM_FIELDS, "claim")
    if claim["classification"] not in CLASSIFICATIONS:
        raise ValueError(
            f"claim.classification must be one of {sorted(CLASSIFICATIONS)}"
        )
    if claim["classification"] != classification:
        raise ValueError(
            f"claim.classification must be {classification} for this evidence"
        )
    if not isinstance(claim["regression_claim"], bool):
        raise ValueError("claim.regression_claim must be boolean")
    if claim["regression_claim"] != (classification == "regression"):
        raise ValueError(
            "regression_claim is allowed only for a baseline-pass/candidate-fail A/B"
        )


def validate_packet(packet: Any) -> dict[str, Any]:
    packet = _object(packet, "input")
    _exact_fields(packet, ROOT_FIELDS, "input")
    if packet["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    if packet["non_performance_work"] is not True:
        raise ValueError("this contract applies only to non-performance work")
    artifacts = _artifacts(packet["artifacts"])
    epochs = _runtime(packet["runtime"], artifacts)
    inventory = _suppressors(packet["suppressors"])
    runs = _object(packet["runs"], "runs")
    _exact_fields(runs, {"baseline", "candidate"}, "runs")
    baseline = _run(
        runs["baseline"],
        "baseline",
        artifacts["baseline"],
        epochs["baseline"],
        inventory,
    )
    candidate = _run(
        runs["candidate"],
        "candidate",
        artifacts["candidate"],
        epochs["candidate"],
        inventory,
    )
    if not candidate["executed"]:
        raise ValueError("candidate run must be executed")
    surface_differences = (
        []
        if not baseline["executed"]
        else sorted(
            field
            for field in COMPARISON_SURFACE_FIELDS
            if baseline["comparison_surface"][field]
            != candidate["comparison_surface"][field]
        )
    )
    classification = _classification(baseline, candidate, surface_differences)
    _claim(packet["claim"], classification)
    return {
        "schema_version": 1,
        "valid": True,
        "classification": classification,
        "comparison_complete": baseline["executed"],
        "comparison_surface_identical": (
            baseline["executed"] and not surface_differences
        ),
        "comparison_surface_differences": surface_differences,
        "regression_claim_allowed": classification == "regression",
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
