#!/usr/bin/env python3
"""Tests for the adaptive engineering iteration advisor."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import iteration_advisor


SCRIPT = Path(__file__).with_name("iteration_advisor.py")


class IterationAdvisorTest(unittest.TestCase):
    def test_defaults_to_fast_l0(self):
        result = iteration_advisor.recommend(dict(iteration_advisor.DEFAULTS))

        self.assertEqual("L0", result["mode"])
        self.assertTrue(result["advisory_only"])
        self.assertEqual([], result["reasons"])
        self.assertEqual([], result["mandatory_boundaries"])

    def test_single_uncertainty_uses_lightweight_l1(self):
        signals = {
            **iteration_advisor.DEFAULTS,
            "goal_clarity": "partial",
        }

        result = iteration_advisor.recommend(signals)

        self.assertEqual("L1", result["mode"])
        self.assertNotIn(
            "freeze the baseline, scope, acceptance contract, and side-effect boundary",
            result["workflow"],
        )

    def test_repeated_stochastic_expensive_work_uses_l2(self):
        signals = {
            **iteration_advisor.DEFAULTS,
            "oracle": "stochastic",
            "attempts": 2,
            "control_planes": 3,
            "external_protocol": True,
            "expensive_validation": True,
        }

        result = iteration_advisor.recommend(signals)

        self.assertEqual("L2", result["mode"])
        self.assertTrue(
            any("repeated attempts" in reason for reason in result["reasons"])
        )

    def test_parallel_writers_delegate_without_forcing_l2(self):
        signals = {
            **iteration_advisor.DEFAULTS,
            "parallel_writers": 3,
        }

        result = iteration_advisor.recommend(signals)

        self.assertEqual("L1", result["mode"])
        self.assertEqual(
            ["isolated-worktree-per-writer", "worktree-cache-lifecycle-owner"],
            result["delegate_to"],
        )

    def test_production_security_boundary_is_explicit(self):
        signals = {
            **iteration_advisor.DEFAULTS,
            "production_effect": True,
            "security_or_credentials": True,
        }

        result = iteration_advisor.recommend(signals)

        self.assertEqual("L2", result["mode"])
        self.assertGreaterEqual(len(result["mandatory_boundaries"]), 2)

    def test_repeated_opaque_failure_returns_control_matrix(self):
        signals = {
            **iteration_advisor.DEFAULTS,
            "attempts": 2,
            "external_protocol": True,
            "failure_visibility": "opaque",
        }

        result = iteration_advisor.recommend(signals)

        self.assertEqual("L2", result["mode"])
        self.assertEqual(
            [
                "command_construction",
                "process_launch",
                "sandbox_policy",
                "tool_transport",
                "result_parsing",
            ],
            [item["layer"] for item in result["opaque_failure_matrix"]],
        )

    def test_clear_failure_does_not_add_opaque_control_matrix(self):
        signals = {
            **iteration_advisor.DEFAULTS,
            "attempts": 2,
            "external_protocol": True,
            "failure_visibility": "clear",
        }

        result = iteration_advisor.recommend(signals)

        self.assertEqual("L2", result["mode"])
        self.assertEqual([], result["opaque_failure_matrix"])

    def test_ordinary_edit_has_no_placement_guard(self):
        result = iteration_advisor.recommend(dict(iteration_advisor.DEFAULTS))

        self.assertEqual("L0", result["mode"])
        self.assertEqual([], result["edit_placement_guard"])
        self.assertFalse(result["complexity_admission"]["required"])
        self.assertTrue(result["complexity_admission"]["admitted"])
        self.assertFalse(result["invariant_owner_convergence"]["applicable"])
        self.assertTrue(result["invariant_owner_convergence"]["admitted"])

    def test_repeated_context_adds_lightweight_placement_guard(self):
        signals = {
            **iteration_advisor.DEFAULTS,
            "repeated_edit_context": True,
        }

        result = iteration_advisor.recommend(signals)

        self.assertEqual("L0", result["mode"])
        self.assertEqual(3, len(result["edit_placement_guard"]))

    def test_misplaced_context_edit_escalates_only_to_l1(self):
        signals = {
            **iteration_advisor.DEFAULTS,
            "misplaced_context_edit": True,
        }

        result = iteration_advisor.recommend(signals)

        self.assertEqual("L1", result["mode"])
        self.assertTrue(result["edit_placement_guard"])
        self.assertNotIn(
            "freeze the baseline, scope, acceptance contract, and side-effect boundary",
            result["workflow"],
        )

    def test_bugfix_complexity_requires_lightweight_admission(self):
        signals = {
            **iteration_advisor.DEFAULTS,
            "bugfix_shadow_state": True,
            "bugfix_fallback": True,
        }

        result = iteration_advisor.recommend(signals)

        self.assertEqual("L1", result["mode"])
        self.assertTrue(result["complexity_admission"]["required"])
        self.assertFalse(result["complexity_admission"]["admitted"])
        self.assertEqual(
            [
                "canonical_lifecycle_hooks_reviewed",
                "cheapest_representation_reviewed",
                "focused_falsifier_run",
            ],
            result["complexity_admission"]["missing_checks"],
        )
        self.assertTrue(result["mandatory_boundaries"])

    def test_bugfix_complexity_is_admitted_after_three_checks(self):
        signals = {
            **iteration_advisor.DEFAULTS,
            "bugfix_unconditional_full_scan": True,
            "canonical_lifecycle_hooks_reviewed": True,
            "cheapest_representation_reviewed": True,
            "focused_falsifier_run": True,
        }

        result = iteration_advisor.recommend(signals)

        self.assertEqual("L1", result["mode"])
        self.assertTrue(result["complexity_admission"]["admitted"])
        self.assertEqual([], result["complexity_admission"]["missing_checks"])
        self.assertEqual([], result["mandatory_boundaries"])

    def test_second_same_invariant_requires_convergent_owner(self):
        signals = {
            **iteration_advisor.DEFAULTS,
            "invariant_mechanism_relation": "same",
        }

        result = iteration_advisor.recommend(signals)

        self.assertEqual("L1", result["mode"])
        convergence = result["invariant_owner_convergence"]
        self.assertFalse(convergence["admitted"])
        self.assertEqual(2, len(convergence["required_actions"]))

    def test_same_invariant_converges_only_after_two_owners_are_removed(self):
        signals = {
            **iteration_advisor.DEFAULTS,
            "invariant_mechanism_relation": "same",
            "canonical_invariant_owner": "runtime/epoch-controller",
            "invariant_owner_strategy": "new_abstraction",
            "duplicate_invariant_owners_removed": 2,
        }

        result = iteration_advisor.recommend(signals)

        convergence = result["invariant_owner_convergence"]
        self.assertTrue(convergence["admitted"])
        self.assertEqual([], convergence["required_actions"])

    def test_new_abstraction_is_rejected_when_it_removes_only_one_owner(self):
        signals = {
            **iteration_advisor.DEFAULTS,
            "invariant_mechanism_relation": "same",
            "canonical_invariant_owner": "runtime/epoch-controller",
            "invariant_owner_strategy": "new_abstraction",
            "duplicate_invariant_owners_removed": 1,
        }

        result = iteration_advisor.recommend(signals)

        convergence = result["invariant_owner_convergence"]
        self.assertFalse(convergence["admitted"])
        self.assertIn(
            "make the new abstraction remove at least two duplicate owners",
            convergence["required_actions"],
        )

    def test_same_invariant_can_reuse_existing_owner_without_removal_count(self):
        signals = {
            **iteration_advisor.DEFAULTS,
            "invariant_mechanism_relation": "same",
            "canonical_invariant_owner": "runtime/epoch-controller",
            "invariant_owner_strategy": "reuse_existing",
        }

        result = iteration_advisor.recommend(signals)

        convergence = result["invariant_owner_convergence"]
        self.assertTrue(convergence["admitted"])
        self.assertEqual(0, convergence["duplicate_owners_removed"])
        self.assertEqual([], convergence["required_actions"])

    def test_different_invariant_mechanisms_stay_separate(self):
        signals = {
            **iteration_advisor.DEFAULTS,
            "invariant_mechanism_relation": "different",
        }

        result = iteration_advisor.recommend(signals)

        convergence = result["invariant_owner_convergence"]
        self.assertTrue(convergence["admitted"])
        self.assertIsNone(convergence["canonical_owner"])

        signals["canonical_invariant_owner"] = "forced/shared-owner"
        rejected = iteration_advisor.recommend(signals)
        self.assertFalse(rejected["invariant_owner_convergence"]["admitted"])
        self.assertIn(
            "keep different mechanisms under separate invariant owners",
            rejected["mandatory_boundaries"],
        )

    def test_cli_rejects_unknown_fields(self):
        packet = {
            "schema_version": 1,
            "signals": {"mystery": True},
        }
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
        self.assertIn("unsupported signal fields", result["error"])

    def test_cli_rejects_unknown_failure_visibility(self):
        packet = {
            "schema_version": 1,
            "signals": {"failure_visibility": "mystery"},
        }
        process = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(packet),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, process.returncode)
        result = json.loads(process.stdout)
        self.assertIn("failure_visibility must be one of", result["error"])


if __name__ == "__main__":
    unittest.main()
