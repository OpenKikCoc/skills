#!/usr/bin/env python3
"""Behavior tests for the long-session control packet."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).with_name("session_state.py")
ROOT = "root-session"
AUTHORITY_A = "HEAD:" + "a" * 40
AUTHORITY_B = "HEAD:" + "b" * 40


def contract(milestones: int = 2) -> dict[str, Any]:
    items = []
    for index in range(1, milestones + 1):
        items.append(
            {
                "id": f"M{index:03d}",
                "title": f"Milestone {index}",
                "deliverables": [f"artifact-{index}"],
                "acceptance": [
                    {
                        "id": f"A{index:03d}",
                        "text": f"Acceptance {index}",
                        "required": True,
                    }
                ],
            }
        )
    return {
        "objective": "Complete a multi-milestone task without losing intent",
        "success_outcomes": ["The root session preserves the requested behavior"],
        "hard_constraints": [
            {"id": "C001", "text": "Workers cannot redefine the goal"}
        ],
        "non_goals": ["Build a distributed scheduler"],
        "authority": {"user_instruction": "user:turn-1"},
        "milestones": items,
    }


class SessionStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.input_dir = self.root / "input"
        self.input_dir.mkdir()
        self.write_input("contract.json", contract())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_input(self, name: str, value: dict[str, Any]) -> Path:
        path = self.input_dir / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def invoke(self, *arguments: str, expected: int = 0) -> dict[str, Any]:
        process = subprocess.run(
            [sys.executable, str(SCRIPT), "--workspace", str(self.workspace), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(expected, process.returncode, process.stderr or process.stdout)
        return json.loads(process.stdout)

    def initialize(self, authority_value: str = AUTHORITY_A) -> dict[str, Any]:
        return self.invoke(
            "init",
            "--contract-file",
            str(self.input_dir / "contract.json"),
            "--root-session-id",
            ROOT,
            "--next-action",
            "Start M001",
            "--authority-kind",
            "git",
            "--authority-value",
            authority_value,
        )

    def status(self) -> dict[str, Any]:
        return self.invoke("status")

    def record(
        self, name: str, value: dict[str, Any], expected: int = 0
    ) -> dict[str, Any]:
        path = self.write_input(name, value)
        return self.invoke("record", "--event-file", str(path), expected=expected)

    def evidence(
        self,
        evidence_id: str,
        milestone: str,
        acceptance: str,
        authority_value: str = AUTHORITY_A,
    ) -> dict[str, Any]:
        return {
            "type": "evidence",
            "id": evidence_id,
            "milestone_id": milestone,
            "acceptance_ids": [acceptance],
            "claim": f"{acceptance} passed",
            "source": "command: focused test",
            "authority": {"kind": "git", "value": authority_value},
        }

    def recover(self, revision: int, authority_value: str = AUTHORITY_A) -> dict[str, Any]:
        return self.invoke(
            "recover",
            "--expected-revision",
            str(revision),
            "--actor",
            ROOT,
            "--authority-kind",
            "git",
            "--authority-value",
            authority_value,
        )

    def test_initialize_validate_and_status(self) -> None:
        result = self.initialize()
        self.assertTrue(result["initialized"])
        self.assertTrue(result["armed"])
        self.assertEqual("M001", result["foreground_milestone"])
        self.assertTrue(self.invoke("validate")["valid"])
        self.assertEqual([], self.status()["blockers"])

    def test_read_only_command_does_not_create_missing_control_directory(self) -> None:
        result = self.invoke("status", expected=2)
        self.assertIn("missing control directory", result["error"])
        self.assertFalse((self.workspace / ".claude").exists())

    def test_explicit_resume_anomaly_disarms_until_root_recovers_and_rearms(self) -> None:
        self.initialize()
        disarmed = self.invoke(
            "disarm", "--expected-revision", "1", "--reason", "resume"
        )
        self.assertEqual(2, disarmed["state_revision"])
        denied = self.invoke("assert-ready", "--actor", ROOT, expected=2)
        self.assertIn("not armed", denied["error"])

        recovered = self.recover(2)
        self.assertFalse(recovered["recovery"]["blockers"])
        self.assertEqual(1, recovered["recovery"]["epoch"])
        denied = self.invoke(
            "rearm",
            "--expected-revision",
            "3",
            "--actor",
            ROOT,
            "--authority-kind",
            "git",
            "--authority-value",
            AUTHORITY_A,
            "--instruction-classification",
            "CLARIFY",
            expected=2,
        )
        self.assertIn("hard constraint", denied["error"])
        rearmed = self.invoke(
            "rearm",
            "--expected-revision",
            "3",
            "--actor",
            ROOT,
            "--authority-kind",
            "git",
            "--authority-value",
            AUTHORITY_A,
            "--instruction-classification",
            "CLARIFY",
            "--checked-constraint",
            "C001",
        )
        self.assertTrue(rearmed["rearmed"])
        self.assertTrue(self.invoke("assert-ready", "--actor", ROOT)["ready"])

    def test_compact_with_active_goal_continues_next_action_without_disarm(self) -> None:
        self.initialize()
        continued = self.invoke(
            "continue-after-compact",
            "--expected-revision",
            "1",
            "--actor",
            ROOT,
            "--goal-status",
            "active",
            "--authority-kind",
            "git",
            "--authority-value",
            AUTHORITY_A,
        )
        self.assertTrue(continued["continue"])
        self.assertEqual("Start M001", continued["next_action"])
        self.assertEqual(1, continued["epoch"])
        self.assertEqual("Milestone 1", continued["milestone_title"])
        self.assertEqual(["A001"], continued["required_acceptance"])
        self.assertEqual("Acceptance 1", continued["acceptance_contract"][0]["text"])
        self.assertEqual("C001", continued["hard_constraints"][0]["id"])
        self.assertIn("execute next_action now", continued["instruction"])
        status = self.status()
        self.assertTrue(status["armed"])
        self.assertEqual("ACTIVE", status["session_status"])
        self.assertTrue(self.invoke("assert-ready", "--actor", ROOT)["ready"])

    def test_compact_fast_path_fails_closed_on_inactive_goal_or_authority_drift(self) -> None:
        self.initialize()
        state_path = self.workspace / ".claude" / "long-session" / "state.json"
        journal_path = self.workspace / ".claude" / "long-session" / "journal.jsonl"
        initial_state = state_path.read_bytes()
        initial_journal = journal_path.read_bytes()
        inactive = self.invoke(
            "continue-after-compact",
            "--expected-revision",
            "1",
            "--actor",
            ROOT,
            "--goal-status",
            "paused",
            "--authority-kind",
            "git",
            "--authority-value",
            AUTHORITY_A,
            expected=2,
        )
        self.assertIn("goal must be active", inactive["error"])
        non_root = self.invoke(
            "continue-after-compact",
            "--expected-revision",
            "1",
            "--actor",
            "fork-session",
            "--goal-status",
            "active",
            "--authority-kind",
            "git",
            "--authority-value",
            AUTHORITY_A,
            expected=2,
        )
        self.assertIn("only root_session_id", non_root["error"])
        drifted = self.invoke(
            "continue-after-compact",
            "--expected-revision",
            "1",
            "--actor",
            ROOT,
            "--goal-status",
            "active",
            "--authority-kind",
            "git",
            "--authority-value",
            AUTHORITY_B,
            expected=2,
        )
        self.assertIn("disarm and recover", drifted["error"])
        self.assertEqual(initial_state, state_path.read_bytes())
        self.assertEqual(initial_journal, journal_path.read_bytes())
        status = self.status()
        self.assertEqual(1, status["state_revision"])
        self.assertEqual(0, status["epoch"])

    def test_compact_fast_path_does_not_mutate_when_worker_blocks_continuation(self) -> None:
        self.initialize()
        self.record(
            "registered.json",
            {
                "type": "worker",
                "id": "W001",
                "milestone_id": "M001",
                "status": "registered",
                "task": "Inspect one boundary",
                "base_authority": AUTHORITY_A,
                "read_scope": ["src/**"],
                "write_scope": [],
            },
        )
        self.record(
            "conflict.json",
            {
                "type": "worker",
                "id": "W001",
                "milestone_id": "M001",
                "status": "conflicted",
            },
        )
        state_path = self.workspace / ".claude" / "long-session" / "state.json"
        journal_path = self.workspace / ".claude" / "long-session" / "journal.jsonl"
        state_before = state_path.read_bytes()
        journal_before = journal_path.read_bytes()

        denied = self.invoke(
            "continue-after-compact",
            "--expected-revision",
            "1",
            "--actor",
            ROOT,
            "--goal-status",
            "active",
            "--authority-kind",
            "git",
            "--authority-value",
            AUTHORITY_A,
            expected=2,
        )

        self.assertIn("worker:W001:conflicted", denied["error"])
        self.assertEqual(state_before, state_path.read_bytes())
        self.assertEqual(journal_before, journal_path.read_bytes())

    def test_only_root_can_recover_or_assert_ready(self) -> None:
        self.initialize()
        denied = self.invoke("assert-ready", "--actor", "fork-session", expected=2)
        self.assertIn("not the root", denied["error"])
        self.invoke("disarm", "--expected-revision", "1", "--reason", "resume")
        denied = self.invoke(
            "recover",
            "--expected-revision",
            "2",
            "--actor",
            "fork-session",
            "--authority-kind",
            "git",
            "--authority-value",
            AUTHORITY_A,
            expected=2,
        )
        self.assertIn("root_session_id", denied["error"])

    def test_checkpoint_uses_cas_and_requires_acceptance_evidence(self) -> None:
        self.initialize()
        patch = self.write_input(
            "complete-m1.json",
            {
                "foreground_milestone": "M002",
                "milestone_status": {"M001": "COMPLETE", "M002": "ACTIVE"},
                "next_action": "Start M002",
            },
        )
        denied = self.invoke(
            "checkpoint",
            "--expected-revision",
            "1",
            "--patch-file",
            str(patch),
            expected=2,
        )
        self.assertIn("lacks fresh evidence", denied["error"])
        self.record("e1.json", self.evidence("E001", "M001", "A001"))
        accepted = self.invoke(
            "checkpoint", "--expected-revision", "1", "--patch-file", str(patch)
        )
        self.assertEqual(2, accepted["state_revision"])
        stale = self.invoke(
            "checkpoint",
            "--expected-revision",
            "1",
            "--patch-file",
            str(patch),
            expected=2,
        )
        self.assertIn("revision conflict", stale["error"])

    def test_stale_authority_evidence_cannot_complete_milestone(self) -> None:
        self.initialize()
        denied = self.record(
            "stale.json",
            self.evidence("E001", "M001", "A001", AUTHORITY_B),
            expected=2,
        )
        self.assertIn("evidence authority is stale", denied["error"])

    def test_session_completion_requires_all_acceptance_and_reviewed_workers(self) -> None:
        self.initialize()
        self.record("e1.json", self.evidence("E001", "M001", "A001"))
        self.record("e2.json", self.evidence("E002", "M002", "A002"))
        activate_m2 = self.write_input(
            "activate-m2.json",
            {
                "foreground_milestone": "M002",
                "milestone_status": {"M001": "COMPLETE", "M002": "ACTIVE"},
                "next_action": "Review worker before completion",
            },
        )
        self.invoke(
            "checkpoint",
            "--expected-revision",
            "1",
            "--patch-file",
            str(activate_m2),
        )
        self.record(
            "registered.json",
            {
                "type": "worker",
                "id": "W001",
                "milestone_id": "M002",
                "status": "registered",
                "task": "Inspect one boundary",
                "base_authority": AUTHORITY_A,
                "read_scope": ["src/**"],
                "write_scope": [],
            },
        )
        self.record(
            "running.json",
            {
                "type": "worker",
                "id": "W001",
                "milestone_id": "M002",
                "status": "running",
            },
        )
        complete = self.write_input(
            "complete-session.json",
            {
                "session_status": "COMPLETE",
                "milestone_status": {"M001": "COMPLETE", "M002": "COMPLETE"},
            },
        )
        denied = self.invoke(
            "checkpoint",
            "--expected-revision",
            "2",
            "--patch-file",
            str(complete),
            expected=2,
        )
        self.assertIn("unfinished workers", denied["error"])
        self.record(
            "awaiting.json",
            {
                "type": "worker",
                "id": "W001",
                "milestone_id": "M002",
                "status": "awaiting_review",
                "receipt": "artifact:receipt.json",
            },
        )
        self.record(
            "reviewed.json",
            {
                "type": "worker",
                "id": "W001",
                "milestone_id": "M002",
                "status": "reviewed",
            },
        )
        result = self.invoke(
            "checkpoint", "--expected-revision", "2", "--patch-file", str(complete)
        )
        self.assertEqual("COMPLETE", result["session_status"])

    def test_pause_and_return_preserve_parent_next_action(self) -> None:
        self.initialize()
        pause = self.write_input(
            "pause.json",
            {
                "foreground_milestone": "M002",
                "milestone_status": {"M001": "PAUSED", "M002": "ACTIVE"},
                "suspension_stack": [
                    {
                        "milestone_id": "M001",
                        "next_action": "Start M001",
                        "return_condition": "M002 is complete",
                    }
                ],
                "next_action": "Handle bounded M002",
            },
        )
        self.invoke(
            "checkpoint", "--expected-revision", "1", "--patch-file", str(pause)
        )
        self.record("e2.json", self.evidence("E002", "M002", "A002"))
        resume = self.write_input(
            "return.json",
            {
                "foreground_milestone": "M001",
                "milestone_status": {"M001": "ACTIVE", "M002": "COMPLETE"},
                "suspension_stack": [],
                "next_action": "Start M001",
            },
        )
        self.invoke(
            "checkpoint", "--expected-revision", "2", "--patch-file", str(resume)
        )
        status = self.status()
        self.assertEqual("M001", status["foreground_milestone"])
        self.assertEqual("Start M001", status["next_action"])

    def test_authority_change_invalidates_matching_evidence(self) -> None:
        self.initialize()
        self.record("e1.json", self.evidence("E001", "M001", "A001"))
        self.invoke("disarm", "--expected-revision", "1", "--reason", "resume")
        result = self.recover(2, AUTHORITY_B)
        self.assertEqual(["E001"], result["recovery"]["invalidated_evidence"])
        self.assertEqual(["E001"], self.status()["invalidated_evidence"])

    def test_authority_change_allows_replacing_evidence_for_completed_milestone(self) -> None:
        self.initialize()
        self.record("e1.json", self.evidence("E001", "M001", "A001"))
        activate_m2 = self.write_input(
            "activate-m2.json",
            {
                "foreground_milestone": "M002",
                "milestone_status": {"M001": "COMPLETE", "M002": "ACTIVE"},
                "next_action": "Start M002",
            },
        )
        self.invoke(
            "checkpoint",
            "--expected-revision",
            "1",
            "--patch-file",
            str(activate_m2),
        )
        self.invoke("disarm", "--expected-revision", "2", "--reason", "resume")
        recovered = self.recover(3, AUTHORITY_B)
        blocker = "milestone:M001:missing_evidence:A001"
        self.assertIn(blocker, recovered["recovery"]["blockers"])
        self.assertIn(blocker, self.status()["blockers"])
        self.record(
            "e2.json",
            self.evidence("E002", "M001", "A001", AUTHORITY_B),
        )
        self.assertNotIn(blocker, self.status()["blockers"])
        rearmed = self.invoke(
            "rearm",
            "--expected-revision",
            "4",
            "--actor",
            ROOT,
            "--authority-kind",
            "git",
            "--authority-value",
            AUTHORITY_B,
            "--instruction-classification",
            "CLARIFY",
            "--checked-constraint",
            "C001",
        )
        self.assertTrue(rearmed["rearmed"])

    def test_worker_lifecycle_rejects_invalid_start_and_blocks_conflict(self) -> None:
        self.initialize()
        invalid = {
            "type": "worker",
            "id": "W001",
            "milestone_id": "M001",
            "status": "running",
        }
        denied = self.record("bad-worker.json", invalid, expected=2)
        self.assertIn("must start as registered", denied["error"])
        registered = {
            "type": "worker",
            "id": "W001",
            "milestone_id": "M001",
            "status": "registered",
            "task": "Inspect one boundary",
            "base_authority": AUTHORITY_A,
            "read_scope": ["src/**"],
            "write_scope": [],
        }
        self.record("registered.json", registered)
        self.record(
            "running.json",
            {
                "type": "worker",
                "id": "W001",
                "milestone_id": "M001",
                "status": "running",
            },
        )
        self.record(
            "conflict.json",
            {
                "type": "worker",
                "id": "W001",
                "milestone_id": "M001",
                "status": "conflicted",
            },
        )
        self.invoke(
            "disarm",
            "--expected-revision",
            "1",
            "--reason",
            "worker W001 reported a conflicting write",
        )
        result = self.recover(2)
        self.assertIn("worker:W001:conflicted", result["recovery"]["blockers"])

    def test_external_effect_unknown_blocks_unrelated_writes_and_rearm(self) -> None:
        self.initialize()
        prepared = self.invoke(
            "effect",
            "prepare",
            "--actor",
            ROOT,
            "--effect-id",
            "X001",
            "--operation",
            "deploy revision",
            "--authority-before",
            "revision:1",
        )
        denied = self.invoke("assert-ready", "--actor", ROOT, expected=2)
        self.assertIn("effect:X001:unclosed", denied["error"])
        allowed = self.invoke(
            "assert-ready", "--actor", ROOT, "--effect-id", "X001"
        )
        self.assertEqual(prepared["call_id"], allowed["call_id"])
        self.invoke(
            "effect",
            "finish",
            "--actor",
            ROOT,
            "--effect-id",
            "X001",
            "--status",
            "unknown",
            "--authority-after",
            "revision:unknown",
        )
        self.invoke("disarm", "--expected-revision", "1", "--reason", "resume")
        recovered = self.recover(2)
        self.assertIn("effect:X001:unknown", recovered["recovery"]["blockers"])
        denied = self.invoke(
            "rearm",
            "--expected-revision",
            "3",
            "--actor",
            ROOT,
            "--authority-kind",
            "git",
            "--authority-value",
            AUTHORITY_A,
            "--instruction-classification",
            "CLARIFY",
            "--checked-constraint",
            "C001",
            expected=2,
        )
        self.assertIn("effect:X001:unknown", denied["error"])
        self.invoke(
            "effect",
            "finish",
            "--actor",
            ROOT,
            "--effect-id",
            "X001",
            "--status",
            "confirmed",
            "--authority-after",
            "revision:2",
        )
        self.assertEqual([], self.status()["blockers"])

    def test_contract_update_is_root_only_and_disarms(self) -> None:
        self.initialize()
        revised = contract(3)
        revised["success_outcomes"].append("A third milestone is delivered")
        revised_path = self.write_input("revised.json", revised)
        decision_path = self.write_input(
            "decision.json",
            {
                "type": "decision",
                "id": "D001",
                "decision": "Add M003",
                "reason": "User added a deliverable",
                "authority": "user:turn-2",
                "supersedes": [],
            },
        )
        denied = self.invoke(
            "contract-update",
            "--expected-contract-revision",
            "1",
            "--expected-state-revision",
            "1",
            "--actor",
            "fork-session",
            "--contract-file",
            str(revised_path),
            "--decision-file",
            str(decision_path),
            expected=2,
        )
        self.assertIn("root_session_id", denied["error"])
        result = self.invoke(
            "contract-update",
            "--expected-contract-revision",
            "1",
            "--expected-state-revision",
            "1",
            "--actor",
            ROOT,
            "--contract-file",
            str(revised_path),
            "--decision-file",
            str(decision_path),
        )
        self.assertEqual(2, result["contract_revision"])
        status = self.status()
        self.assertEqual("DISARMED", status["session_status"])
        self.assertIn("contract_revision_mismatch", status["blockers"])

    def test_duplicate_evidence_is_rejected_without_partial_append(self) -> None:
        self.initialize()
        evidence = self.evidence("E001", "M001", "A001")
        self.record("e1.json", evidence)
        denied = self.record("duplicate.json", evidence, expected=2)
        self.assertIn("duplicate evidence id", denied["error"])
        journal = self.workspace / ".claude" / "long-session" / "journal.jsonl"
        self.assertEqual(1, len(journal.read_text(encoding="utf-8").splitlines()))

    def test_corrupt_state_fails_without_replacing_it(self) -> None:
        self.initialize()
        state = self.workspace / ".claude" / "long-session" / "state.json"
        state.write_text("{not-json\n", encoding="utf-8")
        before = state.read_bytes()
        denied = self.invoke("validate", expected=2)
        self.assertIn("invalid JSON", denied["error"])
        self.assertEqual(before, state.read_bytes())


if __name__ == "__main__":
    unittest.main()
