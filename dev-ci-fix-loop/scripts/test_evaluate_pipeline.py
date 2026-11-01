#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional


MODULE_PATH = Path(__file__).with_name("evaluate_pipeline.py")
SPEC = importlib.util.spec_from_file_location("evaluate_pipeline_under_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load evaluate_pipeline.py")
evaluate_pipeline = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = evaluate_pipeline
SPEC.loader.exec_module(evaluate_pipeline)


def check(
    name: str,
    *,
    required: bool = True,
    sha: Optional[str] = "sha-new",
    status: str = "completed",
    conclusion: Optional[str] = "success",
    **extra,
):
    value = {
        "id": f"id-{name}",
        "name": name,
        "required": required,
        "sha": sha,
        "status": status,
        "conclusion": conclusion,
    }
    value.update(extra)
    return value


def packet(
    commit_checks=None,
    mr_checks=None,
    expected_required_checks=None,
):
    commit_checks = commit_checks or []
    mr_checks = mr_checks or []
    if expected_required_checks is None:
        selected = commit_checks if commit_checks else mr_checks
        expected_required_checks = sorted(
            {
                value["name"]
                for value in selected
                if value.get("required") is True
            }
        )
    return {
        "expected_sha": "sha-new",
        "expected_required_checks": expected_required_checks,
        "commit_checks": commit_checks,
        "mr_checks": mr_checks,
    }


class EvaluatePipelineTest(unittest.TestCase):
    def test_fresh_required_green_allows_optional_failure(self) -> None:
        result = evaluate_pipeline.evaluate(
            packet(
                commit_checks=[
                    check("build"),
                    check("lint-advisory", required=False, conclusion="failure"),
                ]
            )
        )

        self.assertEqual(result["state"], "green")
        self.assertTrue(result["complete"])
        self.assertEqual(result["summary"]["optional_failed"], ["lint-advisory"])
        self.assertEqual(result["counts"]["fresh_required"], 1)
        self.assertEqual(result["counts"]["fresh_optional"], 1)

    def test_stale_required_green_is_not_completion(self) -> None:
        result = evaluate_pipeline.evaluate(
            packet(commit_checks=[check("build", sha="sha-old")])
        )

        self.assertEqual(result["state"], "stale")
        self.assertFalse(result["complete"])
        self.assertEqual(result["counts"]["stale_required"], 1)
        self.assertEqual(result["next_action"], "refresh_current_sha_checks")

    def test_empty_commit_query_falls_back_to_fresh_mr_aggregate(self) -> None:
        result = evaluate_pipeline.evaluate(
            packet(
                mr_checks=[
                    check("build", sha="sha-old"),
                    check("build"),
                ]
            )
        )

        self.assertEqual(result["query_source"], "mr_aggregate")
        self.assertEqual(result["state"], "green")
        self.assertEqual(
            [item["name"] for item in result["required"]],
            ["build"],
        )
        self.assertEqual([item["name"] for item in result["stale"]], ["build"])

    def test_mr_aggregate_uncovered_stale_required_check_blocks_green(self) -> None:
        result = evaluate_pipeline.evaluate(
            packet(
                mr_checks=[
                    check("build"),
                    check("unit", sha="sha-old"),
                ]
            )
        )

        self.assertEqual(result["state"], "stale")
        self.assertFalse(result["complete"])
        self.assertEqual(result["counts"]["blocking_stale_required"], 1)

    def test_non_empty_commit_query_never_uses_friendlier_mr_aggregate(self) -> None:
        result = evaluate_pipeline.evaluate(
            packet(
                commit_checks=[check("build", conclusion="failure")],
                mr_checks=[check("build")],
            )
        )

        self.assertEqual(result["query_source"], "commit")
        self.assertEqual(result["state"], "failed")

    def test_unavailable_artifact_falls_back_to_failed_step_log(self) -> None:
        result = evaluate_pipeline.evaluate(
            packet(
                commit_checks=[
                    check(
                        "e2e",
                        conclusion="failure",
                        artifacts=[
                            {
                                "name": "report",
                                "available": False,
                                "locator": "artifact://expired",
                            }
                        ],
                        steps=[
                            {
                                "id": "step-7",
                                "name": "run-e2e",
                                "status": "completed",
                                "conclusion": "failure",
                                "log_available": True,
                                "job_run_id": "job-9",
                                "run_seq": 2,
                            }
                        ],
                    )
                ]
            )
        )

        self.assertEqual(result["state"], "failed")
        self.assertEqual(
            result["failures"][0]["evidence"],
            {
                "kind": "step_log",
                "job_run_id": "job-9",
                "run_seq": 2,
                "step_id": "step-7",
                "step_name": "run-e2e",
            },
        )

    def test_available_artifact_precedes_step_log(self) -> None:
        result = evaluate_pipeline.evaluate(
            packet(
                commit_checks=[
                    check(
                        "e2e",
                        conclusion="failure",
                        artifacts=[
                            {
                                "name": "report",
                                "available": True,
                                "locator": "artifact://report",
                            }
                        ],
                        steps=[
                            {
                                "id": "step-7",
                                "name": "run-e2e",
                                "status": "completed",
                                "conclusion": "failure",
                                "log_available": True,
                            }
                        ],
                    )
                ]
            )
        )

        self.assertEqual(
            result["failures"][0]["evidence"],
            {
                "kind": "artifact",
                "name": "report",
                "locator": "artifact://report",
            },
        )

    def test_queued_required_state(self) -> None:
        result = evaluate_pipeline.evaluate(
            packet(
                commit_checks=[
                    check("build", status="queued", conclusion=None),
                ]
            )
        )

        self.assertEqual(result["state"], "queued")
        self.assertEqual(result["summary"]["required_queued"], ["build"])

    def test_running_required_state(self) -> None:
        result = evaluate_pipeline.evaluate(
            packet(
                commit_checks=[
                    check("build", status="in_progress", conclusion=None),
                ]
            )
        )

        self.assertEqual(result["state"], "running")
        self.assertEqual(result["summary"]["required_running"], ["build"])

    def test_active_status_overrides_stale_success_conclusion(self) -> None:
        result = evaluate_pipeline.evaluate(
            packet(
                commit_checks=[
                    check("build", status="running", conclusion="success"),
                ]
            )
        )

        self.assertEqual(result["state"], "running")
        self.assertFalse(result["complete"])

    def test_failed_takes_precedence_over_running(self) -> None:
        result = evaluate_pipeline.evaluate(
            packet(
                commit_checks=[
                    check("build", status="running", conclusion=None),
                    check("unit", conclusion="failure"),
                ]
            )
        )

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["summary"]["required_failed"], ["unit"])
        self.assertEqual(result["summary"]["required_running"], ["build"])

    def test_fresh_green_required_state(self) -> None:
        result = evaluate_pipeline.evaluate(
            packet(
                commit_checks=[
                    check("build", status="completed", conclusion="success"),
                    check("unit", status="success", conclusion=None),
                ]
            )
        )

        self.assertEqual(result["state"], "green")
        self.assertTrue(result["complete"])

    def test_missing_expected_required_check_is_unknown(self) -> None:
        result = evaluate_pipeline.evaluate(
            packet(
                commit_checks=[check("build")],
                expected_required_checks=["build", "unit"],
            )
        )

        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["complete"])
        self.assertEqual(
            result["summary"]["missing_expected_required"], ["unit"]
        )
        self.assertEqual(
            result["next_action"], "refresh_missing_required_checks"
        )

    def test_conflicting_terminal_status_and_conclusion_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            evaluate_pipeline.EvidenceError,
            "conflicting terminal status and conclusion",
        ):
            evaluate_pipeline.evaluate(
                packet(
                    commit_checks=[
                        check(
                            "build",
                            status="success",
                            conclusion="failure",
                        )
                    ]
                )
            )

    def test_missing_required_sha_is_unknown_not_green(self) -> None:
        result = evaluate_pipeline.evaluate(
            packet(commit_checks=[check("build", sha=None)])
        )

        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["complete"])
        self.assertEqual(result["counts"]["missing_required_sha"], 1)

    def test_no_required_checks_requires_policy_confirmation(self) -> None:
        result = evaluate_pipeline.evaluate(
            packet(commit_checks=[check("advisory", required=False)])
        )

        self.assertEqual(result["state"], "no_required_checks")
        self.assertFalse(result["complete"])

    def test_invalid_packet_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            evaluate_pipeline.EvidenceError,
            "required must be a boolean",
        ):
            evaluate_pipeline.evaluate(
                packet(commit_checks=[{"name": "build", "required": "yes"}])
            )

        with self.assertRaisesRegex(
            evaluate_pipeline.EvidenceError,
            "expected_required_checks must not contain duplicate names",
        ):
            evaluate_pipeline.evaluate(
                packet(
                    commit_checks=[check("build")],
                    expected_required_checks=["build", "build"],
                )
            )

    def test_cli_exit_codes_are_deterministic_and_offline(self) -> None:
        cases = [
            (packet(commit_checks=[check("build")]), 0, "green"),
            (
                packet(
                    commit_checks=[
                        check("build", status="running", conclusion=None),
                    ]
                ),
                2,
                "running",
            ),
        ]
        for evidence, expected_code, expected_state in cases:
            with self.subTest(expected_state), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "evidence.json"
                path.write_text(json.dumps(evidence))
                process = subprocess.run(
                    [sys.executable, str(MODULE_PATH), str(path), "--compact"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )

                self.assertEqual(process.returncode, expected_code, process.stderr)
                self.assertEqual(json.loads(process.stdout)["state"], expected_state)


if __name__ == "__main__":
    unittest.main(verbosity=2)
