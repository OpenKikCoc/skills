#!/usr/bin/env python3
"""Regression tests for receipt-backed wave-growth calibration."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path


SCRIPT = Path(__file__).with_name("growth_calibration.py")
SPEC = importlib.util.spec_from_file_location("growth_calibration", SCRIPT)
assert SPEC and SPEC.loader
CALIBRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CALIBRATION)

GIB = 1024**3


def cache_identity() -> dict[str, object]:
    return {
        "source": "operator_managed_host_shared",
        "path": "/managed/go-build",
        "device": 7,
        "inode": 101,
        "uid": 501,
    }


def receipt(
    wave_id: str,
    *,
    owned_bytes: int,
    cache_before: int,
    cache_after: int,
    explanation: str | None = "cache misses compiled by the completed wave",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": CALIBRATION.RECEIPT_KIND,
        "status": "completed",
        "wave_id": wave_id,
        "command_class": "go-test-focused",
        "target_class": "darwin-arm64-example",
        "cache_identity": cache_identity(),
        "owned_output": {
            "baseline_allocated_bytes": 0,
            "peak_allocated_bytes": owned_bytes,
            "newly_allocated_bytes": owned_bytes,
            "ownership_validated": True,
        },
        "shared_cache": {
            "before_allocated_bytes": cache_before,
            "after_allocated_bytes": cache_after,
            "delta_allocated_bytes": cache_after - cache_before,
            "growth_explanation": explanation,
        },
    }


def request() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": CALIBRATION.REQUEST_KIND,
        "requested_expected_growth_bytes": 30 * GIB,
        "completed_wave_receipts": [
            receipt(
                "wave-1",
                owned_bytes=5 * GIB,
                cache_before=10 * GIB,
                cache_after=12 * GIB,
            ),
            receipt(
                "wave-2",
                owned_bytes=6 * GIB,
                cache_before=12 * GIB,
                cache_after=12 * GIB,
                explanation=None,
            ),
        ],
        "command_class": "go-test-focused",
        "target_class": "darwin-arm64-example",
        "cache_identity": cache_identity(),
        "current_shared_cache": {
            "previous_allocated_bytes": 12 * GIB,
            "current_allocated_bytes": 12 * GIB,
            "delta_allocated_bytes": 0,
            "growth_explanation": None,
            "active": True,
        },
        "safety_margin_bytes": 2 * GIB,
        "disk": {
            "total_bytes": 500 * GIB,
            "free_bytes": 60 * GIB,
            "min_free_bytes": 20 * GIB,
            "min_free_percent": 5.0,
        },
    }


class GrowthCalibrationTests(unittest.TestCase):
    def test_matching_receipts_with_warm_cache_permit_lower_estimate(self) -> None:
        result = CALIBRATION.advise(request())

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["decision"], "calibrated")
        self.assertEqual(result["candidate_expected_growth_bytes"], 10 * GIB)
        self.assertEqual(result["effective_expected_growth_bytes"], 10 * GIB)
        self.assertEqual(
            result["evidence"]["peak_shared_cache_growth_bytes"], 2 * GIB
        )
        self.assertEqual(result["shared_cache_headroom_credit_bytes"], 0)
        self.assertTrue(result["disk"]["candidate_gate"]["passed"])
        self.assertIn("CALIBRATION_APPLIED", result["reason_codes"])
        self.assertIn(
            "ACTIVE_SHARED_CACHE_EXCLUDED_FROM_HEADROOM",
            result["reason_codes"],
        )

    def test_target_drift_retains_conservative_estimate(self) -> None:
        value = request()
        value["completed_wave_receipts"][1]["target_class"] = "linux-amd64-example"

        result = CALIBRATION.advise(value)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["decision"], "conservative_retained")
        self.assertEqual(result["effective_expected_growth_bytes"], 30 * GIB)
        self.assertFalse(result["calibration_permitted"])
        self.assertIn("TARGET_CLASS_DRIFT", result["reason_codes"])

    def test_unexplained_cache_growth_retains_conservative_estimate(self) -> None:
        value = request()
        value["current_shared_cache"].update(
            {
                "current_allocated_bytes": 13 * GIB,
                "delta_allocated_bytes": GIB,
                "growth_explanation": None,
            }
        )

        result = CALIBRATION.advise(value)

        self.assertEqual(result["decision"], "conservative_retained")
        self.assertEqual(result["effective_expected_growth_bytes"], 30 * GIB)
        self.assertIn(
            "SHARED_CACHE_GROWTH_UNEXPLAINED", result["reason_codes"]
        )

    def test_safety_margin_is_added_after_owned_and_cache_peaks(self) -> None:
        value = request()
        value["safety_margin_bytes"] = 7 * GIB

        result = CALIBRATION.advise(value)

        self.assertEqual(result["candidate_expected_growth_bytes"], 15 * GIB)
        self.assertEqual(result["effective_expected_growth_bytes"], 15 * GIB)
        self.assertEqual(result["evidence"]["safety_margin_bytes"], 7 * GIB)

    def test_zero_safety_margin_retains_conservative_estimate(self) -> None:
        value = request()
        value["safety_margin_bytes"] = 0

        result = CALIBRATION.advise(value)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["decision"], "conservative_retained")
        self.assertEqual(result["effective_expected_growth_bytes"], 30 * GIB)
        self.assertFalse(result["calibration_permitted"])
        self.assertIn("SAFETY_MARGIN_NOT_POSITIVE", result["reason_codes"])
        self.assertIn("CONSERVATIVE_ESTIMATE_RETAINED", result["reason_codes"])

    def test_calibrated_estimate_that_still_fails_floors_blocks(self) -> None:
        value = request()
        value["disk"]["free_bytes"] = 32 * GIB

        result = CALIBRATION.advise(value)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["decision"], "blocked_disk_floor")
        self.assertFalse(result["launch_allowed"])
        self.assertEqual(result["effective_expected_growth_bytes"], 30 * GIB)
        self.assertIn(
            "CALIBRATED_PERCENTAGE_FLOOR_FAILED", result["reason_codes"]
        )
        self.assertEqual(result["disk"]["min_free_bytes"], 20 * GIB)
        self.assertEqual(result["disk"]["min_free_percent"], 5.0)

    def test_malformed_owned_output_blocks_instead_of_falling_back(self) -> None:
        value = request()
        value["completed_wave_receipts"][0]["owned_output"][
            "newly_allocated_bytes"
        ] = 4 * GIB

        result = CALIBRATION.advise(value)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["decision"], "blocked_malformed_evidence")
        self.assertEqual(result["reason_codes"], ["MALFORMED_EVIDENCE"])
        self.assertFalse(result["launch_allowed"])

    def test_unvalidated_owned_output_retains_conservative_estimate(self) -> None:
        value = request()
        value["completed_wave_receipts"][0]["owned_output"][
            "ownership_validated"
        ] = False

        result = CALIBRATION.advise(value)

        self.assertEqual(result["decision"], "conservative_retained")
        self.assertEqual(result["effective_expected_growth_bytes"], 30 * GIB)
        self.assertIn("OWNED_OUTPUT_NOT_VALIDATED", result["reason_codes"])

    def test_negative_active_cache_delta_never_becomes_headroom(self) -> None:
        value = request()
        value["current_shared_cache"].update(
            {
                "current_allocated_bytes": 11 * GIB,
                "delta_allocated_bytes": -GIB,
            }
        )

        result = CALIBRATION.advise(value)

        self.assertEqual(result["decision"], "calibrated")
        self.assertEqual(result["candidate_expected_growth_bytes"], 10 * GIB)
        self.assertEqual(result["shared_cache_headroom_credit_bytes"], 0)

    def test_cli_emits_reason_codes_and_status_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            input_path = Path(temporary) / "request.json"
            input_path.write_text(json.dumps(request()), encoding="utf-8")
            completed = subprocess.run(
                [str(SCRIPT), "--input", str(input_path)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(json.loads(completed.stdout)["decision"], "calibrated")
            self.assertEqual(completed.stderr, "")

            malformed = deepcopy(request())
            malformed["disk"]["total_bytes"] = 0
            input_path.write_text(json.dumps(malformed), encoding="utf-8")
            completed = subprocess.run(
                [str(SCRIPT), "--input", str(input_path)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(
                json.loads(completed.stdout)["decision"],
                "blocked_malformed_evidence",
            )
            self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
