#!/usr/bin/env python3
"""Regression tests for the pure parallel merge planner."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("parallel_merge.py")
SPEC = importlib.util.spec_from_file_location("parallel_merge", SCRIPT)
assert SPEC and SPEC.loader
PARALLEL_MERGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PARALLEL_MERGE)

BASE_SHA = "1" * 40


def task(
    task_id: str,
    dependencies: list[str],
    write_set: list[str],
) -> dict[str, object]:
    return {
        "id": task_id,
        "summary": f"Implement {task_id}",
        "depends_on": dependencies,
        "write_set": write_set,
        "acceptance": [
            {
                "id": "test",
                "kind": "command",
                "value": f"test {task_id}",
            }
        ],
    }


def plan_input(
    tasks: list[dict[str, object]],
    *,
    worker_substrate: str | None = "native-subagent",
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "base_sha": BASE_SHA,
        "tasks": tasks,
    }
    if worker_substrate is not None:
        document["worker_substrate"] = worker_substrate
    return document


def worker(
    task_id: str,
    changed_files: list[str],
    *,
    state: str = "completed",
    clean: bool = True,
    preservation: dict[str, object] | None = None,
    unique_artifacts: bool = False,
) -> dict[str, object]:
    commit_digit = {
        "alpha": "2",
        "beta": "3",
        "zeta": "4",
        "core": "5",
    }.get(task_id, "9")
    return {
        "task_id": task_id,
        "base_sha": BASE_SHA,
        "head_sha": commit_digit * 40,
        "parent_sha": BASE_SHA,
        "merge_base_sha": BASE_SHA,
        "commit_shas": [commit_digit * 40],
        "state": state,
        "clean": clean,
        "changed_files": changed_files,
        "acceptance_evidence": [
            {
                "id": "test",
                "passed": True,
                "detail": "exit 0",
            }
        ],
        "preservation": preservation,
        "unique_artifacts": unique_artifacts,
    }


def execution(
    plan: dict[str, object],
    workers: list[dict[str, object]],
) -> dict[str, object]:
    snapshot = {
        "head_sha": BASE_SHA,
        "clean": True,
        "changed_files": [],
    }
    return {
        "schema_version": 1,
        "plan": plan,
        "main_workspace": {
            "initial": copy.deepcopy(snapshot),
            "current": copy.deepcopy(snapshot),
        },
        "workers": workers,
    }


class PlanTests(unittest.TestCase):
    def test_overlap_is_rejected(self) -> None:
        document = plan_input(
            [
                task("alpha", [], ["src/core/**"]),
                task("beta", [], ["src/core/engine.py"]),
            ]
        )
        with self.assertRaisesRegex(
            PARALLEL_MERGE.ContractError, "write-set overlap"
        ):
            PARALLEL_MERGE.build_plan(document)

    def test_cycle_is_rejected(self) -> None:
        document = plan_input(
            [
                task("alpha", ["beta"], ["src/alpha/**"]),
                task("beta", ["alpha"], ["src/beta/**"]),
            ]
        )
        with self.assertRaisesRegex(
            PARALLEL_MERGE.ContractError, "dependency cycle"
        ):
            PARALLEL_MERGE.build_plan(document)

    def test_missing_dependency_is_rejected(self) -> None:
        document = plan_input(
            [task("alpha", ["missing"], ["src/alpha/**"])]
        )
        with self.assertRaisesRegex(
            PARALLEL_MERGE.ContractError, "depends on missing task 'missing'"
        ):
            PARALLEL_MERGE.build_plan(document)

    def test_parent_segments_are_rejected(self) -> None:
        document = plan_input([task("alpha", [], ["src/../outside.py"])])
        with self.assertRaisesRegex(PARALLEL_MERGE.ContractError, r"'\.\.'"):
            PARALLEL_MERGE.build_plan(document)

    def test_integration_order_is_stable(self) -> None:
        tasks = [
            task("zeta", [], ["src/zeta/**"]),
            task("beta", ["alpha"], ["src/beta/**"]),
            task("alpha", [], ["src/alpha/**"]),
        ]
        first = PARALLEL_MERGE.build_plan(plan_input(tasks))
        second = PARALLEL_MERGE.build_plan(plan_input(list(reversed(tasks))))
        self.assertEqual(first, second)
        self.assertEqual(first["parallel_stages"], [["alpha", "zeta"], ["beta"]])
        self.assertEqual(first["integration_order"], ["alpha", "beta", "zeta"])
        self.assertEqual(first["worker_substrate"], "native-subagent")
        receipt = PARALLEL_MERGE.plan_digest_receipt(first)
        self.assertEqual(receipt["plan_sha256"], PARALLEL_MERGE.canonical_sha256(first))
        pretty_bytes = json.dumps(first, indent=2, sort_keys=True).encode()
        self.assertNotEqual(
            receipt["plan_sha256"], PARALLEL_MERGE.hashlib.sha256(pretty_bytes).hexdigest()
        )

    def test_worker_substrate_is_optional_for_legacy_plan_validation(self) -> None:
        result = PARALLEL_MERGE.build_plan(
            plan_input(
                [task("alpha", [], ["src/alpha/**"])],
                worker_substrate=None,
            )
        )

        self.assertNotIn("worker_substrate", result)

    def test_worker_substrate_must_be_a_stable_identifier(self) -> None:
        with self.assertRaisesRegex(
            PARALLEL_MERGE.ContractError, "worker_substrate must use"
        ):
            PARALLEL_MERGE.build_plan(
                plan_input(
                    [task("alpha", [], ["src/alpha/**"])],
                    worker_substrate="Native Agent",
                )
            )


class ValidationTests(unittest.TestCase):
    def base_plan(self) -> dict[str, object]:
        return PARALLEL_MERGE.build_plan(
            plan_input([task("core", [], ["src/core/**"])])
        )

    def test_worker_change_outside_allowlist_is_rejected(self) -> None:
        document = execution(
            self.base_plan(),
            [worker("core", ["src/core/engine.py", "README.md"])],
        )
        result = PARALLEL_MERGE.validate_execution(document)
        self.assertFalse(result["valid"])
        self.assertIn(
            "core: worker changed file outside write set: README.md",
            result["errors"],
        )

    def test_worker_base_sha_mismatch_is_rejected(self) -> None:
        raw_worker = worker("core", ["src/core/engine.py"])
        raw_worker["base_sha"] = "a" * 40
        result = PARALLEL_MERGE.validate_execution(
            execution(self.base_plan(), [raw_worker])
        )
        self.assertFalse(result["valid"])
        self.assertIn(
            "core: worker base SHA does not match plan base SHA",
            result["errors"],
        )

    def test_worker_topology_mismatch_is_rejected(self) -> None:
        raw_worker = worker("core", ["src/core/engine.py"])
        raw_worker["parent_sha"] = "a" * 40
        raw_worker["merge_base_sha"] = "b" * 40
        raw_worker["commit_shas"] = ["c" * 40, raw_worker["head_sha"]]
        result = PARALLEL_MERGE.validate_execution(
            execution(self.base_plan(), [raw_worker])
        )

        self.assertFalse(result["valid"])
        self.assertIn(
            "core: worker result parent SHA does not match plan base SHA",
            result["errors"],
        )
        self.assertIn(
            "core: worker merge-base SHA does not match plan base SHA",
            result["errors"],
        )
        self.assertIn(
            "core: worker result must contain exactly one commit equal to head SHA",
            result["errors"],
        )

    def test_missing_acceptance_evidence_is_rejected(self) -> None:
        raw_worker = worker("core", ["src/core/engine.py"])
        raw_worker["acceptance_evidence"] = []
        result = PARALLEL_MERGE.validate_execution(
            execution(self.base_plan(), [raw_worker])
        )
        self.assertFalse(result["valid"])
        self.assertIn(
            "core: worker missing acceptance evidence: test",
            result["errors"],
        )

    def test_main_workspace_contamination_is_rejected(self) -> None:
        document = execution(
            self.base_plan(),
            [worker("core", ["src/core/engine.py"])],
        )
        document["main_workspace"]["current"] = {
            "head_sha": BASE_SHA,
            "clean": False,
            "changed_files": ["src/leak.py"],
        }
        result = PARALLEL_MERGE.validate_execution(document)
        self.assertFalse(result["valid"])
        self.assertTrue(result["main_workspace"]["contaminated"])
        self.assertIn(
            "main workspace is contaminated before integration",
            result["errors"],
        )

    def test_interrupted_worker_is_rejected(self) -> None:
        document = execution(
            self.base_plan(),
            [worker("core", ["src/core/engine.py"], state="interrupted")],
        )
        result = PARALLEL_MERGE.validate_execution(document)
        worker_result = result["workers"][0]
        self.assertFalse(result["valid"])
        self.assertFalse(worker_result["accepted"])
        self.assertIn("worker state is interrupted", worker_result["errors"])
        self.assertIn(
            "worker did not complete",
            worker_result["cleanup_refusal_reasons"],
        )

    def test_dirty_cleanup_is_refused(self) -> None:
        document = execution(
            self.base_plan(),
            [
                worker(
                    "core",
                    ["src/core/engine.py"],
                    clean=False,
                    preservation={
                        "kind": "integrated",
                        "locator": "8" * 40,
                        "contains_head": True,
                        "observed_at": "2030-01-01T00:00:00Z",
                    },
                )
            ],
        )
        handoff = PARALLEL_MERGE.build_handoff(document)
        cleanup = handoff["workers"][0]["cleanup"]
        self.assertEqual(handoff["status"], "blocked")
        self.assertFalse(cleanup["allowed"])
        self.assertIn("worker worktree is dirty", cleanup["reasons"])

    def test_handoff_has_required_fields(self) -> None:
        plan = PARALLEL_MERGE.build_plan(
            plan_input(
                [
                    task("alpha", [], ["src/alpha/**"]),
                    task("beta", ["alpha"], ["src/beta/**"]),
                ]
            )
        )
        document = execution(
            plan,
            [
                worker("alpha", ["src/alpha/a.py"]),
                worker("beta", ["src/beta/b.py"]),
            ],
        )
        handoff = PARALLEL_MERGE.build_handoff(document)
        self.assertEqual(handoff["status"], "ready")
        self.assertEqual(
            set(handoff),
            {
                "schema_version",
                "kind",
                "status",
                "base_sha",
                "integration_order",
                "integration_steps",
                "workers",
                "main_workspace",
                "blockers",
                "next_actions",
            },
        )
        self.assertEqual(
            [step["task_id"] for step in handoff["integration_steps"]],
            ["alpha", "beta"],
        )
        self.assertEqual(handoff["integration_steps"][1]["depends_on"], ["alpha"])
        worker_handoff = handoff["workers"][0]
        self.assertTrue(
            {
                "task_id",
                "commit_sha",
                "parent_sha",
                "merge_base_sha",
                "commit_shas",
                "depends_on",
                "write_set",
                "acceptance_contract",
                "changed_files",
                "acceptance_evidence",
                "accepted",
                "preservation",
                "cleanup",
            }.issubset(worker_handoff)
        )
        self.assertFalse(worker_handoff["cleanup"]["allowed"])
        self.assertIn(
            "worker commit lacks verified integration or archive preservation",
            worker_handoff["cleanup"]["reasons"],
        )
        self.assertTrue(
            any(
                "clean this wave's disk-budget manifest" in action
                for action in handoff["next_actions"]
            )
        )

    def test_cleanup_is_allowed_only_after_integration(self) -> None:
        document = execution(
            self.base_plan(),
            [
                worker(
                    "core",
                    ["src/core/engine.py"],
                    preservation={
                        "kind": "integrated",
                        "locator": "8" * 40,
                        "contains_head": True,
                        "observed_at": "2030-01-01T00:00:00Z",
                    },
                )
            ],
        )
        handoff = PARALLEL_MERGE.build_handoff(document)
        self.assertEqual(handoff["status"], "ready")
        self.assertEqual(
            handoff["integration_steps"][0]["action"], "already-integrated"
        )
        self.assertTrue(handoff["workers"][0]["cleanup"]["allowed"])

    def test_cleanup_rejects_unverified_preservation_claim(self) -> None:
        raw_worker = worker(
            "core",
            ["src/core/engine.py"],
            preservation={
                "kind": "archived",
                "locator": "refs/archive/core",
                "contains_head": False,
                "observed_at": "2030-01-01T00:00:00Z",
            },
        )
        handoff = PARALLEL_MERGE.build_handoff(
            execution(self.base_plan(), [raw_worker])
        )

        self.assertEqual(handoff["status"], "blocked")
        self.assertFalse(handoff["workers"][0]["cleanup"]["allowed"])
        self.assertIn(
            "does not prove preservation",
            handoff["blockers"][0],
        )


class CliTests(unittest.TestCase):
    def test_cli_failure_is_structured_and_nonzero(self) -> None:
        document = plan_input(
            [
                task("alpha", [], ["src/shared/**"]),
                task("beta", [], ["src/shared/file.py"]),
            ]
        )
        completed = subprocess.run(
            [str(SCRIPT), "plan", "--input", "-"],
            input=json.dumps(document),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "")
        result = json.loads(completed.stdout)
        self.assertEqual(result["kind"], "parallel-change-plan-error")
        self.assertIn("write-set overlap", result["errors"][0])

    def test_cli_writes_only_requested_output(self) -> None:
        document = plan_input([task("core", [], ["src/core/**"])])
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "input.json"
            output = root / "output.json"
            source.write_text(json.dumps(document), encoding="utf-8")
            before = sorted(path.name for path in root.iterdir())
            completed = subprocess.run(
                [
                    str(SCRIPT),
                    "plan",
                    "--input",
                    str(source),
                    "--output",
                    str(output),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            after = sorted(path.name for path in root.iterdir())
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(completed.stderr, "")
            self.assertEqual(before, ["input.json"])
            self.assertEqual(after, ["input.json", "output.json"])
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["kind"],
                "parallel-change-plan",
            )

    def test_cli_digest_emits_machine_readable_receipt(self) -> None:
        plan = PARALLEL_MERGE.build_plan(
            plan_input([task("core", [], ["src/core/**"])])
        )
        completed = subprocess.run(
            [str(SCRIPT), "digest", "--input", "-"],
            input=json.dumps(plan, indent=2) + "\n", text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        receipt = json.loads(completed.stdout)
        self.assertEqual("parallel-change-plan-digest", receipt["kind"])
        self.assertEqual(PARALLEL_MERGE.canonical_sha256(plan), receipt["plan_sha256"])

    def test_runtime_has_no_process_or_git_imports(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in (
            "import subprocess",
            "from subprocess",
            "import os",
            "from os",
            "import git",
            "from git",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
