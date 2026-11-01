#!/usr/bin/env python3
"""Tests for non-performance validation provenance."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validation_provenance


SCRIPT = Path(__file__).with_name("validation_provenance.py")
BASELINE = "1" * 64
CANDIDATE = "2" * 64
COMPARISON_SURFACE = {
    "validator_sha256": "3" * 64,
    "fixture_sha256": "4" * 64,
    "command_sha256": "5" * 64,
    "parser_sha256": "6" * 64,
    "environment_sha256": "7" * 64,
}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def failure(raw: str, normalized: str, normalizer: str = "pytest-nodeid-v1") -> dict[str, str]:
    raw_digest = digest(raw)
    normalized_digest = digest(normalized)
    return {
        "normalizer_id": normalizer,
        "raw": raw,
        "raw_sha256": raw_digest,
        "normalized": normalized,
        "normalized_sha256": normalized_digest,
        "binding_sha256": validation_provenance.signature_binding(
            normalizer, raw_digest, normalized_digest
        ),
    }


def epoch(role: str) -> dict[str, object]:
    if role == "baseline":
        return {
            "runtime_id": "runtime-test-1",
            "epoch_id": "epoch-baseline",
            "predecessor_epoch_id": None,
            "activated_artifact_sha256": BASELINE,
            "fresh": True,
            "reset_before_run": False,
        }
    return {
        "runtime_id": "runtime-test-1",
        "epoch_id": "epoch-candidate",
        "predecessor_epoch_id": "epoch-baseline",
        "activated_artifact_sha256": CANDIDATE,
        "fresh": True,
        "reset_before_run": True,
    }


def run(role: str, outcome: str, failures: list[dict[str, str]] | None = None) -> dict[str, object]:
    return {
        "executed": True,
        "artifact_sha256": BASELINE if role == "baseline" else CANDIDATE,
        "epoch_id": f"epoch-{role}",
        "profile": "default_release",
        "active_suppressors": [],
        "comparison_surface": dict(COMPARISON_SURFACE),
        "outcome": outcome,
        "failures": [] if failures is None else failures,
    }


def packet_fixture(
    baseline_outcome: str = "pass",
    candidate_outcome: str = "fail",
    baseline_failures: list[dict[str, str]] | None = None,
    candidate_failures: list[dict[str, str]] | None = None,
    classification: str = "regression",
) -> dict[str, object]:
    candidate_failures = (
        [failure("FAILED test_api.py::test_create at 0x1234", "test_api.py::test_create")]
        if candidate_failures is None and candidate_outcome == "fail"
        else candidate_failures
    )
    baseline_failures = (
        [failure("FAILED test_api.py::test_create at 0xabcd", "test_api.py::test_create")]
        if baseline_failures is None and baseline_outcome == "fail"
        else baseline_failures
    )
    return {
        "schema_version": 1,
        "non_performance_work": True,
        "artifacts": {
            "baseline_sha256": BASELINE,
            "candidate_sha256": CANDIDATE,
        },
        "runtime": {
            "test_owned": True,
            "epochs": {"baseline": epoch("baseline"), "candidate": epoch("candidate")},
        },
        "suppressors": {
            "inventory": [
                {
                    "id": "flaky-retry",
                    "kind": "retry",
                    "enabled_by_default_or_release": False,
                }
            ],
            "default_release_active": [],
        },
        "runs": {
            "baseline": run("baseline", baseline_outcome, baseline_failures),
            "candidate": run("candidate", candidate_outcome, candidate_failures),
        },
        "claim": {
            "classification": classification,
            "regression_claim": classification == "regression",
        },
    }


class ValidationProvenanceTest(unittest.TestCase):
    def assert_invalid(self, packet: dict[str, object], expected: str) -> None:
        with self.assertRaisesRegex(ValueError, expected):
            validation_provenance.validate_packet(packet)

    def test_baseline_pass_candidate_fail_is_regression(self):
        result = validation_provenance.validate_packet(packet_fixture())

        self.assertEqual("regression", result["classification"])
        self.assertTrue(result["regression_claim_allowed"])

    def test_failure_classifications_are_derived_from_bound_signatures(self):
        cases = (
            ("fail", "fail", None, None, "pre_existing"),
            ("fail", "pass", None, None, "fixed"),
            (
                "fail",
                "fail",
                [failure("baseline trace", "test_api.py::old")],
                [failure("candidate trace", "test_api.py::new")],
                "changed",
            ),
            ("pass", "pass", None, None, "unknown"),
        )
        for baseline_outcome, candidate_outcome, baseline_failures, candidate_failures, expected in cases:
            with self.subTest(expected=expected):
                packet = packet_fixture(
                    baseline_outcome,
                    candidate_outcome,
                    baseline_failures,
                    candidate_failures,
                    expected,
                )
                result = validation_provenance.validate_packet(packet)
                self.assertEqual(expected, result["classification"])

    def test_different_normalizer_namespaces_are_unknown(self):
        packet = packet_fixture(
            "fail",
            "fail",
            [failure("same", "same", "normalizer-v1")],
            [failure("same", "same", "normalizer-v2")],
            "unknown",
        )

        result = validation_provenance.validate_packet(packet)

        self.assertEqual("unknown", result["classification"])

    def test_candidate_only_evidence_cannot_claim_regression(self):
        packet = packet_fixture(classification="unknown")
        packet["runtime"]["epochs"]["baseline"] = None
        packet["runs"]["baseline"].update(
            executed=False,
            epoch_id=None,
            comparison_surface=None,
            outcome="not_run",
            failures=[],
        )
        packet["claim"] = {
            "classification": "regression",
            "regression_claim": True,
        }

        self.assert_invalid(packet, "classification must be unknown")

    def test_candidate_only_evidence_is_unknown_without_comparison(self):
        packet = packet_fixture(classification="unknown")
        packet["runtime"]["epochs"]["baseline"] = None
        packet["runs"]["baseline"].update(
            executed=False,
            epoch_id=None,
            comparison_surface=None,
            outcome="not_run",
            failures=[],
        )

        result = validation_provenance.validate_packet(packet)

        self.assertEqual("unknown", result["classification"])
        self.assertFalse(result["comparison_complete"])

    def test_ab_requires_identical_comparison_surface(self):
        for field in sorted(COMPARISON_SURFACE):
            with self.subTest(field=field):
                packet = packet_fixture(classification="unknown")
                packet["runs"]["candidate"]["comparison_surface"][field] = (
                    "f" * 64
                )
                packet["claim"] = {
                    "classification": "unknown",
                    "regression_claim": False,
                }

                result = validation_provenance.validate_packet(packet)

                self.assertEqual("unknown", result["classification"])
                self.assertFalse(result["comparison_surface_identical"])
                self.assertEqual(
                    [field], result["comparison_surface_differences"]
                )

    def test_surface_mismatch_rejects_regression_claim(self):
        packet = packet_fixture()
        packet["runs"]["candidate"]["comparison_surface"][
            "environment_sha256"
        ] = "f" * 64

        self.assert_invalid(packet, "classification must be unknown")

    def test_runtime_epochs_must_be_fresh_and_resets_test_owned(self):
        packet = packet_fixture()
        packet["runtime"]["epochs"]["candidate"]["fresh"] = False
        self.assert_invalid(packet, "fresh runtime epoch")

        packet = packet_fixture()
        packet["runtime"]["test_owned"] = False
        self.assert_invalid(packet, "reset is allowed only for a test-owned runtime")

    def test_same_runtime_candidate_epoch_must_follow_baseline(self):
        packet = packet_fixture()
        packet["runtime"]["epochs"]["candidate"]["predecessor_epoch_id"] = "some-other-epoch"

        self.assert_invalid(packet, "reset directly from the baseline epoch")

    def test_default_release_rejects_any_suppressor(self):
        packet = packet_fixture()
        packet["suppressors"]["default_release_active"] = ["flaky-retry"]
        self.assert_invalid(packet, "must have no active suppressors")

        packet = packet_fixture()
        packet["runs"]["candidate"]["active_suppressors"] = ["flaky-retry"]
        self.assert_invalid(packet, "must not activate suppressors")

    def test_raw_and_normalized_signature_binding_is_recomputed(self):
        packet = packet_fixture()
        packet["runs"]["candidate"]["failures"][0]["normalized"] = "changed"
        self.assert_invalid(packet, "normalized_sha256 does not match")

        packet = packet_fixture()
        packet["runs"]["candidate"]["failures"][0]["binding_sha256"] = "f" * 64
        self.assert_invalid(packet, "does not bind raw and normalized")

    def test_cli_returns_stable_failure_json(self):
        packet = packet_fixture()
        packet["non_performance_work"] = False
        process = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(packet),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, process.returncode)
        result = json.loads(process.stdout)
        self.assertFalse(result["valid"])
        self.assertIn("only to non-performance work", result["error"])


if __name__ == "__main__":
    unittest.main()
