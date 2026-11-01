#!/usr/bin/env python3
"""Tests for exact-SHA benchmark evidence validation."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmark_evidence_validator


SCRIPT = Path(__file__).with_name("benchmark_evidence_validator.py")
BASELINE_SHA = "1" * 40
CANDIDATE_SHA = "2" * 40
STATUS_HASH = "3" * 64
SEMANTIC_HASHES = {
    "harness": "4" * 64,
    "parser": "5" * 64,
    "fixture": "6" * 64,
    "command": "7" * 64,
    "metric": "8" * 64,
}


def run_fixture(role: str, sha: str, value: str) -> dict[str, object]:
    worktree = f"/workspace/bench/{role}"
    return {
        "executed_sha": sha,
        "shell": "/bin/sh",
        "argv": ["bench-tool", "--target", "pkg/hotpath"],
        "executable_preflight": {
            "shell": "/bin/sh",
            "tool": "bench-tool",
            "resolved_path": "/usr/local/bin/bench-tool",
            "executable": True,
        },
        "discovery_roots": [f"{worktree}/pkg/hotpath"],
        "log_channel": {
            "name": f"{role}-stderr",
            "format": "text",
            "text": "warming fixture\niteration 1\niteration 2\n",
        },
        "result_channel": {
            "name": f"{role}-result.json",
            "format": "application/json",
            "records": [
                {"metric": "latency_p50", "unit": "ms", "value": value}
            ],
        },
        "semantic_fingerprints": dict(SEMANTIC_HASHES),
        "exit_code": 0,
    }


def portable_fixture() -> dict[str, object]:
    return {
        "schema_version": 1,
        "performance_work": True,
        "baseline_sha": BASELINE_SHA,
        "candidate_sha": CANDIDATE_SHA,
        "controller_checkout": {
            "path": "/workspace/controller",
            "dirty_before": True,
            "declared_read_only": True,
            "write_operations": 0,
            "status_before_sha256": STATUS_HASH,
            "status_after_sha256": STATUS_HASH,
        },
        "worktrees": {
            "baseline": {
                "path": "/workspace/bench/baseline",
                "head_sha": BASELINE_SHA,
                "isolated": True,
            },
            "candidate": {
                "path": "/workspace/bench/candidate",
                "head_sha": CANDIDATE_SHA,
                "isolated": True,
            },
        },
        "cache": {
            "applicable": True,
            "shared_cache_ids": ["benchmark-session-cache"],
        },
        "runs": {
            "baseline": run_fixture("baseline", BASELINE_SHA, "10.5"),
            "candidate": run_fixture("candidate", CANDIDATE_SHA, "8.25"),
        },
        "claim": {
            "attribution": "portable_cross_sha",
            "ab_delta": {
                "metric": "latency_p50",
                "unit": "ms",
                "baseline": "10.5",
                "candidate": "8.25",
                "candidate_minus_baseline": "-2.25",
            },
        },
    }


class BenchmarkEvidenceValidatorTest(unittest.TestCase):
    def assert_invalid(self, packet: dict[str, object], expected: str) -> None:
        with self.assertRaisesRegex(ValueError, expected):
            benchmark_evidence_validator.validate_packet(packet)

    def test_non_performance_work_is_not_burdened(self):
        result = benchmark_evidence_validator.validate_packet(
            {"schema_version": 1, "performance_work": False}
        )

        self.assertFalse(result["applicable"])
        self.assertEqual("not_applicable", result["attribution"])

    def test_portable_comparison_accepts_multiline_logs_and_machine_result(self):
        result = benchmark_evidence_validator.validate_packet(portable_fixture())

        self.assertTrue(result["comparison_allowed"])
        self.assertEqual("portable_cross_sha", result["attribution"])
        self.assertEqual([], result["semantic_differences"])

    def test_machine_result_rejects_multiline_scalar(self):
        packet = portable_fixture()
        packet["runs"]["candidate"]["result_channel"]["records"][0][
            "value"
        ] = "8.25\nbenchmark complete"

        self.assert_invalid(packet, "single-line decimal string")

    def test_missing_tool_on_actual_shell_path_is_rejected(self):
        packet = portable_fixture()
        packet["runs"]["candidate"]["executable_preflight"][
            "resolved_path"
        ] = None

        self.assert_invalid(packet, "tool was not resolved through the actual shell PATH")

    def test_unbounded_or_escaping_discovery_is_rejected(self):
        for root in ("/", "/workspace/bench/other-target", "../pkg", "/workspace/bench/candidate/**"):
            with self.subTest(root=root):
                packet = portable_fixture()
                packet["runs"]["candidate"]["discovery_roots"] = [root]
                self.assert_invalid(packet, "bounded|escapes|traversal|glob")

    def test_dirty_controller_must_remain_read_only_and_unchanged(self):
        for field, value, expected in (
            ("declared_read_only", False, "declared read-only"),
            ("write_operations", 1, "zero write operations"),
            ("status_after_sha256", "9" * 64, "status changed"),
        ):
            with self.subTest(field=field):
                packet = portable_fixture()
                packet["controller_checkout"][field] = value
                self.assert_invalid(packet, expected)

    def test_exact_shas_and_one_shared_cache_are_required(self):
        packet = portable_fixture()
        packet["baseline_sha"] = "1" * 12
        self.assert_invalid(packet, "full lowercase Git object ID")

        packet = portable_fixture()
        packet["cache"]["shared_cache_ids"] = ["one", "two"]
        self.assert_invalid(packet, "exactly 1 shared cache IDs")

    def test_semantic_difference_yields_candidate_only_without_delta(self):
        packet = portable_fixture()
        packet["runs"]["candidate"]["semantic_fingerprints"]["parser"] = "a" * 64
        packet["claim"] = {
            "attribution": "candidate_only",
            "ab_delta": None,
        }

        result = benchmark_evidence_validator.validate_packet(packet)

        self.assertFalse(result["comparison_allowed"])
        self.assertEqual("candidate_only", result["attribution"])
        self.assertEqual(["parser"], result["semantic_differences"])

    def test_portable_results_require_same_metric_and_unit(self):
        packet = portable_fixture()
        packet["runs"]["candidate"]["result_channel"]["records"][0][
            "metric"
        ] = "throughput"
        packet["claim"]["ab_delta"] = None

        self.assert_invalid(packet, "same metric and unit")

    def test_candidate_only_forbids_ab_delta_claim(self):
        packet = portable_fixture()
        packet["runs"]["candidate"]["semantic_fingerprints"]["fixture"] = "b" * 64
        packet["claim"]["attribution"] = "candidate_only"

        self.assert_invalid(packet, "A/B delta claims are forbidden")

    def test_cli_returns_stable_failure_json(self):
        packet = portable_fixture()
        packet["worktrees"]["candidate"]["path"] = "/workspace/controller"
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
        self.assertIn("must be distinct", result["error"])


if __name__ == "__main__":
    unittest.main()
