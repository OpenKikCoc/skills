#!/usr/bin/env python3
"""Regression tests for bounded stale-worker replacement advice."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("worker_liveness_advisor.py")
SPEC = importlib.util.spec_from_file_location("worker_liveness_advisor", SCRIPT)
assert SPEC and SPEC.loader
ADVISOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADVISOR)

BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40
OLD_ROOT = "/managed/worktrees/docs-old"
NEW_ROOT = "/managed/worktrees/docs-replacement"


def plan() -> dict[str, object]:
    return ADVISOR.PLAN_CONTRACT.build_plan(
        {
            "schema_version": 1,
            "base_sha": BASE_SHA,
            "worker_substrate": "native-subagent",
            "tasks": [
                {
                    "id": "docs",
                    "summary": "Implement docs",
                    "depends_on": [],
                    "write_set": ["docs/**"],
                    "acceptance": [
                        {
                            "id": "test",
                            "kind": "command",
                            "value": "python3 test_docs.py",
                        }
                    ],
                }
            ],
        }
    )


def snapshot(
    *,
    execution_id: str = "docs-exec-1",
    root: str = OLD_ROOT,
    head_sha: str = BASE_SHA,
    file_digest: str = "a" * 64,
    receipt_digest: str | None = None,
) -> dict[str, object]:
    return {
        "execution_id": execution_id,
        "worktree_root": root,
        "head_sha": head_sha,
        "file_state_sha256": file_digest,
        "receipt_state_sha256": receipt_digest,
    }


def followup(
    followup_id: str,
    *,
    state: str = "running",
    acknowledgement: dict[str, str] | None = None,
    observation: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "followup_id": followup_id,
        "host_accepted": True,
        "host_reported_state": state,
        "task_acknowledgement": acknowledgement,
        "observation": observation or snapshot(),
    }


def replacement(
    *,
    execution_id: str = "docs-exec-2",
    root: str = NEW_ROOT,
    substrate: str = "native-subagent",
    writer_started: bool = False,
) -> dict[str, object]:
    current_plan = plan()
    return {
        "execution_id": execution_id,
        "task_id": "docs",
        "plan_sha256": ADVISOR._plan_digest(current_plan),
        "base_sha": BASE_SHA,
        "write_set": ["docs/**"],
        "substrate": substrate,
        "worktree_root": root,
        "writer_started": writer_started,
    }


def single_owner(*, intent_sha256: str | None = None) -> dict[str, str]:
    current_plan = plan()
    plan_digest = ADVISOR._plan_digest(current_plan)
    return {
        "owner_id": "controller",
        "intent_sha256": intent_sha256
        or ADVISOR._intent_digest(
            plan_sha256=plan_digest,
            task_id="docs",
            base_sha=BASE_SHA,
            write_set=["docs/**"],
            substrate="native-subagent",
        ),
        "authority_receipt_sha256": "9" * 64,
    }


def document(
    *,
    followups: list[dict[str, object]] | None = None,
    preserved: bool = False,
    inspected: bool = False,
    writer_state: str = "running",
    candidate: dict[str, object] | None = None,
    replacement_count: int | None = None,
    fallback_owner: dict[str, str] | None = None,
) -> dict[str, object]:
    current_plan = plan()
    plan_digest = ADVISOR._plan_digest(current_plan)
    preservation_receipt = (
        {
            "execution_id": "docs-exec-1",
            "worktree_root": OLD_ROOT,
            "receipt_sha256": "d" * 64,
        }
        if preserved
        else None
    )
    inspection_receipt = (
        {
            "execution_id": "docs-exec-1",
            "worktree_root": OLD_ROOT,
            "receipt_sha256": "e" * 64,
        }
        if inspected
        else None
    )
    result = {
        "schema_version": 1,
        "plan": current_plan,
        "plan_sha256": plan_digest,
        "task_id": "docs",
        "old_execution": {
            "execution_id": "docs-exec-1",
            "task_id": "docs",
            "plan_sha256": plan_digest,
            "base_sha": BASE_SHA,
            "write_set": ["docs/**"],
            "substrate": "native-subagent",
            "worktree_root": OLD_ROOT,
        },
        "review_boundary": {
            "accepted_followup_limit": 2,
            "baseline": snapshot(),
            "accepted_followups": followups or [],
        },
        "old_execution_recovery": {
            "preservation_receipt": preservation_receipt,
            "inspection_receipt": inspection_receipt,
            "writer_observation": {
                "execution_id": "docs-exec-1",
                "state": writer_state,
                "observation_sha256": "f" * 64,
            },
        },
        "replacement_execution": candidate,
    }
    if replacement_count is not None:
        result["unchanged_intent_replacement_count"] = replacement_count
    if fallback_owner is not None:
        result["single_owner_fallback"] = fallback_owner
    return result


class AdviceTests(unittest.TestCase):
    def test_host_running_and_completed_are_not_progress(self) -> None:
        result = ADVISOR.advise(
            document(
                followups=[
                    followup("followup-1", state="running"),
                    followup("followup-2", state="completed"),
                ]
            )
        )

        self.assertEqual("preserve_and_inspect_old_worktree", result["action"])
        self.assertEqual("lost", result["old_execution"]["lineage_state"])
        self.assertFalse(result["old_execution"]["progress_observed"])

    def test_bound_acknowledgement_counts_as_progress(self) -> None:
        acknowledgement = {
            "ack_id": "docs-ack-1",
            "execution_id": "docs-exec-1",
            "task_id": "docs",
        }
        result = ADVISOR.advise(
            document(
                followups=[
                    followup("followup-1", acknowledgement=acknowledgement)
                ]
            )
        )

        self.assertEqual("continue_existing_execution", result["action"])
        self.assertEqual(
            ["followup-1:task_acknowledgement"],
            result["old_execution"]["progress_signals"],
        )

    def test_each_independently_observed_worktree_change_counts(self) -> None:
        cases = (
            ("head_change", snapshot(head_sha=HEAD_SHA)),
            ("file_change", snapshot(file_digest="b" * 64)),
            ("receipt_change", snapshot(receipt_digest="c" * 64)),
        )
        for label, observation in cases:
            with self.subTest(label=label):
                result = ADVISOR.advise(
                    document(
                        followups=[
                            followup("followup-1", observation=observation)
                        ]
                    )
                )
                self.assertEqual("continue_existing_execution", result["action"])
                self.assertEqual(
                    [f"followup-1:{label}"],
                    result["old_execution"]["progress_signals"],
                )

    def test_one_unchanged_followup_stays_within_boundary(self) -> None:
        result = ADVISOR.advise(
            document(followups=[followup("followup-1", state="accepted")])
        )

        self.assertEqual("await_bounded_followup", result["action"])
        self.assertEqual(
            "live_or_bounded_review",
            result["old_execution"]["lineage_state"],
        )

    def test_lost_lineage_requires_old_writer_stop_before_replacement(self) -> None:
        stalled = [followup("followup-1"), followup("followup-2")]
        result = ADVISOR.advise(
            document(
                followups=stalled,
                preserved=True,
                inspected=True,
                writer_state="unknown",
            )
        )

        self.assertEqual("wait_for_old_writer_stop", result["action"])
        with self.assertRaisesRegex(ADVISOR.ContractError, "old writer to be stopped"):
            ADVISOR.advise(
                document(
                    followups=stalled,
                    preserved=True,
                    inspected=True,
                    writer_state="running",
                    candidate=replacement(),
                )
            )

    def test_fresh_execution_preserves_immutable_assignment(self) -> None:
        stalled = [followup("followup-1"), followup("followup-2")]
        result = ADVISOR.advise(
            document(
                followups=stalled,
                preserved=True,
                inspected=True,
                writer_state="stopped",
                candidate=replacement(),
            )
        )

        self.assertEqual("dispatch_fresh_execution", result["action"])
        self.assertEqual(
            "docs-exec-2", result["replacement_execution"]["execution_id"]
        )
        self.assertTrue(result["replacement_execution"]["dispatch_allowed"])
        self.assertFalse(result["replacement_evidence_counts_for_old_execution"])
        self.assertEqual(
            {
                "limit": 1,
                "consumed_before_decision": 0,
                "consumed_after_decision": 1,
                "remaining_after_decision": 0,
            },
            result["unchanged_intent_replacement_budget"],
        )

    def test_stalled_replacement_cannot_spawn_again(self) -> None:
        stalled = [followup("followup-1"), followup("followup-2")]
        with self.assertRaisesRegex(
            ADVISOR.ContractError, "replacement budget is exhausted"
        ):
            ADVISOR.advise(
                document(
                    followups=stalled,
                    preserved=True,
                    inspected=True,
                    writer_state="stopped",
                    candidate=replacement(),
                    replacement_count=1,
                )
            )

    def test_stalled_replacement_collapses_to_bound_single_owner(self) -> None:
        stalled = [followup("followup-1"), followup("followup-2")]
        result = ADVISOR.advise(
            document(
                followups=stalled,
                preserved=True,
                inspected=True,
                writer_state="stopped",
                replacement_count=1,
                fallback_owner=single_owner(),
            )
        )

        self.assertEqual("collapse_slice_to_single_owner", result["action"])
        self.assertEqual("controller", result["single_owner_fallback"]["owner_id"])
        self.assertEqual(result["intent_sha256"], single_owner()["intent_sha256"])
        self.assertEqual(
            {
                "limit": 1,
                "consumed_before_decision": 1,
                "consumed_after_decision": 1,
                "remaining_after_decision": 0,
            },
            result["unchanged_intent_replacement_budget"],
        )

    def test_stalled_replacement_blocks_without_authorized_owner(self) -> None:
        stalled = [followup("followup-1"), followup("followup-2")]
        result = ADVISOR.advise(
            document(
                followups=stalled,
                preserved=True,
                inspected=True,
                writer_state="stopped",
                replacement_count=1,
            )
        )

        self.assertEqual("block_no_authorized_owner", result["action"])
        self.assertEqual(
            "replacement-exhausted-no-authorized-owner", result["reason_code"]
        )

    def test_single_owner_must_bind_unchanged_intent(self) -> None:
        with self.assertRaisesRegex(
            ADVISOR.ContractError, "bind the unchanged assignment intent"
        ):
            ADVISOR.advise(
                document(
                    replacement_count=1,
                    fallback_owner=single_owner(intent_sha256="8" * 64),
                )
            )

    def test_replacement_count_is_bounded(self) -> None:
        with self.assertRaisesRegex(
            ADVISOR.ContractError, "replacement_count must be 0 or 1"
        ):
            ADVISOR.advise(document(replacement_count=2))

    def test_replacement_cannot_reuse_id_or_worktree(self) -> None:
        stalled = [followup("followup-1"), followup("followup-2")]
        for candidate, message in (
            (replacement(execution_id="docs-exec-1"), "execution_id must be new"),
            (replacement(root=OLD_ROOT), "worktree roots must not overlap"),
            (replacement(root=f"{OLD_ROOT}/child"), "worktree roots must not overlap"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                ADVISOR.ContractError, message
            ):
                ADVISOR.advise(
                    document(
                        followups=stalled,
                        preserved=True,
                        inspected=True,
                        writer_state="stopped",
                        candidate=candidate,
                    )
                )

    def test_replacement_cannot_change_substrate_or_start_early(self) -> None:
        stalled = [followup("followup-1"), followup("followup-2")]
        with self.assertRaisesRegex(ADVISOR.ContractError, "same task, plan, base"):
            ADVISOR.advise(
                document(
                    followups=stalled,
                    preserved=True,
                    inspected=True,
                    writer_state="stopped",
                    candidate=replacement(substrate="alternate-runtime"),
                )
            )
        with self.assertRaisesRegex(ADVISOR.ContractError, "before its writer starts"):
            ADVISOR.advise(
                document(
                    followups=stalled,
                    preserved=True,
                    inspected=True,
                    writer_state="stopped",
                    candidate=replacement(writer_started=True),
                )
            )

    def test_replacement_progress_cannot_prove_old_execution_progress(self) -> None:
        stale = [followup("followup-1"), followup("followup-2")]
        invalid = document(followups=stale)
        invalid["review_boundary"]["accepted_followups"][1]["observation"] = snapshot(
            execution_id="docs-exec-2", root=NEW_ROOT, head_sha=HEAD_SHA
        )
        with self.assertRaisesRegex(ADVISOR.ContractError, "old execution_id"):
            ADVISOR.advise(invalid)

    def test_recovery_receipts_must_bind_the_old_execution_and_worktree(self) -> None:
        stalled = [followup("followup-1"), followup("followup-2")]
        invalid = document(
            followups=stalled,
            preserved=True,
            inspected=True,
            writer_state="stopped",
            candidate=replacement(),
        )
        invalid["old_execution_recovery"]["inspection_receipt"][
            "worktree_root"
        ] = NEW_ROOT
        with self.assertRaisesRegex(ADVISOR.ContractError, "old execution and worktree"):
            ADVISOR.advise(invalid)

    def test_replacement_is_forbidden_before_lost_lineage(self) -> None:
        with self.assertRaisesRegex(ADVISOR.ContractError, "old lineage is not lost"):
            ADVISOR.advise(
                document(
                    followups=[followup("followup-1")],
                    preserved=True,
                    inspected=True,
                    writer_state="stopped",
                    candidate=replacement(),
                )
            )

    def test_bound_acknowledgement_must_match_old_execution_and_task(self) -> None:
        bad = {
            "ack_id": "ack-1",
            "execution_id": "docs-exec-2",
            "task_id": "docs",
        }
        with self.assertRaisesRegex(ADVISOR.ContractError, "assigned task"):
            ADVISOR.advise(
                document(followups=[followup("followup-1", acknowledgement=bad)])
            )

    def test_cli_returns_structured_error(self) -> None:
        invalid = document()
        invalid["schema_version"] = 2
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "input.json"
            path.write_text(json.dumps(invalid), encoding="utf-8")
            completed = subprocess.run(
                [str(SCRIPT), "--input", str(path)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        self.assertEqual(2, completed.returncode)
        self.assertEqual("worker-liveness-error", json.loads(completed.stdout)["kind"])

    def test_runtime_has_no_agent_process_git_or_shell_execution(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for token in (
            "spawn_agent",
            "subprocess",
            "os.system",
            "import git",
            "from git",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
