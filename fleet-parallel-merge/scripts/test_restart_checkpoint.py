#!/usr/bin/env python3
"""Regression tests for pure JSON parallel-wave restart checkpoints."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("restart_checkpoint.py")
SPEC = importlib.util.spec_from_file_location("restart_checkpoint", SCRIPT)
assert SPEC and SPEC.loader
CHECKPOINT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKPOINT)

BASE_SHA = "1" * 40
CORE_SHA = "2" * 40
MANIFEST_SHA256 = "a" * 64
STATE_PATH = "/durable/controller/wave-1"
CHECKPOINT_PATH = f"{STATE_PATH}/restart-checkpoint.json"
MANIFEST_PATH = f"{STATE_PATH}/resource-manifest.json"
CORE_ROOT = "/managed/session/wave-1/core"
DOCS_ROOT = "/managed/session/wave-1/docs"
TMP_ROOT = "/managed/session/wave-1/tmp"


def parallel_plan() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "parallel-change-plan",
        "base_sha": BASE_SHA,
        "tasks": [
            {
                "id": "core",
                "summary": "Implement core",
                "depends_on": [],
                "write_set": ["src/core/**"],
                "acceptance": [
                    {
                        "id": "unit",
                        "kind": "command",
                        "value": "python3 -m unittest tests.test_core",
                    }
                ],
            },
            {
                "id": "docs",
                "summary": "Document core",
                "depends_on": ["core"],
                "write_set": ["docs/core.md"],
                "acceptance": [
                    {
                        "id": "content",
                        "kind": "criterion",
                        "value": "Document the accepted behavior.",
                    }
                ],
            },
        ],
        "parallel_stages": [["core"], ["docs"]],
        "integration_order": ["core", "docs"],
        "invariants": {
            "shared_base_sha": True,
            "disjoint_write_sets": True,
        },
    }


def checkpoint_input() -> dict[str, object]:
    return {
        "schema_version": 1,
        "plan": parallel_plan(),
        "base_sha": BASE_SHA,
        "current": {
            "phase": "wave-1",
            "gate": "worker-validation",
        },
        "worker_ownership": [
            {
                "task_id": "docs",
                "root": DOCS_ROOT,
                "state": "running",
                "unique_commit_sha": None,
            },
            {
                "task_id": "core",
                "root": CORE_ROOT,
                "state": "succeeded",
                "unique_commit_sha": CORE_SHA,
            },
        ],
        "durable_archive_refs": [
            {
                "id": "core-archive",
                "task_id": "core",
                "commit_sha": CORE_SHA,
                "locator": "refs/archive/session/wave-1/core",
            }
        ],
        "resource_manifest": {
            "locator": MANIFEST_PATH,
            "sha256": MANIFEST_SHA256,
        },
        "non_durable_paths": [TMP_ROOT, DOCS_ROOT, CORE_ROOT],
        "checkpoint_path": CHECKPOINT_PATH,
        "state_path": STATE_PATH,
        "next_action": {
            "kind": "validate_workers",
            "args": {
                "task_ids": ["core", "docs"],
                "gate": "worker-validation",
            },
        },
    }


def built_checkpoint() -> dict[str, object]:
    return CHECKPOINT.build_checkpoint(checkpoint_input())


def archive_evidence(archive: dict[str, object]) -> dict[str, object]:
    claim = {
        "kind": "archive-ref-resolution",
        "locator": archive["locator"],
        "resolved_commit_sha": archive["commit_sha"],
    }
    return {
        **claim,
        "sha256": CHECKPOINT._canonical_sha256(claim, "archive evidence"),
    }


def observed_snapshot(
    checkpoint: dict[str, object],
    *,
    manifest_state: str = "live",
    root_state: str = "live",
) -> dict[str, object]:
    plan = parallel_plan()
    observed_workers: list[dict[str, object]]
    if root_state == "live":
        observed_workers = copy.deepcopy(checkpoint["worker_ownership"])
    else:
        observed_workers = []
    return {
        "schema_version": 1,
        "base_sha": BASE_SHA,
        "plan": plan,
        "plan_sha256": checkpoint["plan_sha256"],
        "checkpoint_sha256": CHECKPOINT._rendered_sha256(checkpoint),
        "checkpoint_path": CHECKPOINT_PATH,
        "state_path": STATE_PATH,
        "resource_manifest": {
            "locator": MANIFEST_PATH,
            "state": manifest_state,
            "sha256": MANIFEST_SHA256 if manifest_state == "live" else None,
        },
        "roots": [
            {"path": path, "state": root_state}
            for path in checkpoint["non_durable_paths"]
        ],
        "workers": observed_workers,
        "durable_archive_refs": [
            {
                **copy.deepcopy(archive),
                "state": "live",
                "evidence": archive_evidence(archive),
            }
            for archive in checkpoint["durable_archive_refs"]
        ],
    }


def resume_input(
    checkpoint: dict[str, object],
    observed: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "checkpoint": checkpoint,
        "observed": observed,
    }


def flattened_keys(value: object) -> list[str]:
    if isinstance(value, dict):
        keys = list(value)
        for item in value.values():
            keys.extend(flattened_keys(item))
        return keys
    if isinstance(value, list):
        keys: list[str] = []
        for item in value:
            keys.extend(flattened_keys(item))
        return keys
    return []


def flattened_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        strings: list[str] = []
        for item in value.values():
            strings.extend(flattened_strings(item))
        return strings
    if isinstance(value, list):
        strings = []
        for item in value:
            strings.extend(flattened_strings(item))
        return strings
    return []


class CheckpointTests(unittest.TestCase):
    def test_checkpoint_is_deterministic_minimal_and_command_free(self) -> None:
        source = checkpoint_input()
        first = CHECKPOINT.build_checkpoint(source)
        second = CHECKPOINT.build_checkpoint(copy.deepcopy(source))

        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["kind"], "parallel-wave-restart-checkpoint")
        self.assertEqual(first["base_sha"], BASE_SHA)
        self.assertEqual(
            first["plan_sha256"],
            CHECKPOINT._canonical_sha256(source["plan"], "plan"),
        )
        self.assertNotIn("plan", first)
        self.assertNotIn("python3 -m unittest", json.dumps(first))
        self.assertEqual(
            [worker["task_id"] for worker in first["worker_ownership"]],
            ["core", "docs"],
        )
        self.assertEqual(
            first["non_durable_paths"],
            sorted([CORE_ROOT, DOCS_ROOT, TMP_ROOT]),
        )
        forbidden_keys = {
            "api_key",
            "command",
            "credential",
            "password",
            "secret",
            "shell",
            "token",
        }
        self.assertFalse(forbidden_keys & set(flattened_keys(first)))

    def test_checkpoint_accepts_explicit_current_phase_and_gate(self) -> None:
        source = checkpoint_input()
        source.pop("current")
        source["current_phase"] = "wave-1"
        source["current_gate"] = "archive"

        receipt = CHECKPOINT.build_checkpoint(source)

        self.assertEqual(
            receipt["current"],
            {"phase": "wave-1", "gate": "archive"},
        )

    def test_checkpoint_rejects_plan_base_mismatch(self) -> None:
        source = checkpoint_input()
        source["base_sha"] = "9" * 40
        with self.assertRaisesRegex(
            CHECKPOINT.ContractError,
            "base_sha does not match plan.base_sha",
        ):
            CHECKPOINT.build_checkpoint(source)

    def test_checkpoint_requires_archive_for_every_unique_commit(self) -> None:
        source = checkpoint_input()
        source["durable_archive_refs"] = []
        with self.assertRaisesRegex(
            CHECKPOINT.ContractError,
            "unique commit for task 'core' lacks a durable archive ref",
        ):
            CHECKPOINT.build_checkpoint(source)

    def test_checkpoint_rejects_archive_commit_mismatch(self) -> None:
        source = checkpoint_input()
        source["durable_archive_refs"][0]["commit_sha"] = "8" * 40
        with self.assertRaisesRegex(
            CHECKPOINT.ContractError,
            "does not match task 'core' unique commit",
        ):
            CHECKPOINT.build_checkpoint(source)

    def test_checkpoint_rejects_state_or_receipt_inside_disposable_root(self) -> None:
        for field in ("state_path", "checkpoint_path"):
            with self.subTest(field=field):
                source = checkpoint_input()
                if field == "state_path":
                    value = f"{CORE_ROOT}/state"
                    source[field] = value
                    source["checkpoint_path"] = f"{value}/checkpoint.json"
                    source["resource_manifest"]["locator"] = f"{value}/manifest.json"
                else:
                    disposable_state_child = f"{STATE_PATH}/disposable"
                    source["non_durable_paths"].append(disposable_state_child)
                    source[field] = f"{disposable_state_child}/checkpoint.json"
                with self.assertRaisesRegex(
                    CHECKPOINT.ContractError,
                    "must live outside non_durable_paths",
                ):
                    CHECKPOINT.build_checkpoint(source)

    def test_checkpoint_requires_every_worker_root_in_non_durable_paths(self) -> None:
        source = checkpoint_input()
        source["non_durable_paths"].remove(DOCS_ROOT)
        with self.assertRaisesRegex(
            CHECKPOINT.ContractError,
            "worker 'docs' root must be listed",
        ):
            CHECKPOINT.build_checkpoint(source)

    def test_checkpoint_rejects_sensitive_or_command_action_arguments(self) -> None:
        unsafe_args = (
            {"command": "git status"},
            {"access_token": "sensitive"},
            {"note": "run one; run two"},
            {"note": "git status"},
            {"authorization": "Bearer abcdefghijklmnop"},
        )
        for args in unsafe_args:
            with self.subTest(args=args):
                source = checkpoint_input()
                source["next_action"] = {
                    "kind": "validate_workers",
                    "args": args,
                }
                with self.assertRaises(CHECKPOINT.ContractError):
                    CHECKPOINT.build_checkpoint(source)

    def test_checkpoint_rejects_unknown_action_kind_and_shape(self) -> None:
        source = checkpoint_input()
        source["next_action"] = {
            "kind": "run_shell",
            "args": {},
        }
        with self.assertRaisesRegex(
            CHECKPOINT.ContractError,
            "next_action.kind must be one of",
        ):
            CHECKPOINT.build_checkpoint(source)

        source = checkpoint_input()
        source["next_action"]["description"] = "validate"
        with self.assertRaisesRegex(
            CHECKPOINT.ContractError,
            "must contain exactly 'kind' and 'args'",
        ):
            CHECKPOINT.build_checkpoint(source)


class ResumeTests(unittest.TestCase):
    def test_resume_existing_requires_matching_live_manifest_and_roots(self) -> None:
        checkpoint = built_checkpoint()
        result = CHECKPOINT.validate_resume(
            resume_input(checkpoint, observed_snapshot(checkpoint))
        )

        self.assertEqual(result["status"], "resume_existing")
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["next_action"], checkpoint["next_action"])
        self.assertIsNone(result["fresh_reconstruction_plan"])
        rendered = json.dumps(result)
        self.assertNotIn("python3 -m unittest", rendered)
        self.assertNotIn("command", flattened_keys(result))

    def test_reconstruct_fresh_requires_manifest_and_all_roots_lost(self) -> None:
        checkpoint = built_checkpoint()
        observed = observed_snapshot(
            checkpoint,
            manifest_state="lost",
            root_state="lost",
        )

        result = CHECKPOINT.validate_resume(resume_input(checkpoint, observed))

        self.assertEqual(result["status"], "reconstruct_fresh")
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["next_action"]["kind"], "allocate_fresh_resources")
        plan = result["fresh_reconstruction_plan"]
        self.assertEqual(
            plan["kind"],
            "parallel-wave-fresh-reconstruction-plan",
        )
        self.assertEqual(plan["base_sha"], BASE_SHA)
        self.assertEqual(
            plan["do_not_reuse_paths"],
            checkpoint["non_durable_paths"],
        )
        self.assertEqual(
            [action["kind"] for action in plan["worker_actions"]],
            ["restore_archived_result", "recreate_worker"],
        )
        self.assertEqual(
            plan["worker_actions"][0]["args"]["archive_ref_id"],
            "core-archive",
        )
        self.assertEqual(
            plan["worker_actions"][1]["args"],
            {"task_id": "docs", "base_sha": BASE_SHA},
        )

    def test_base_and_plan_hash_mismatch_block_resume(self) -> None:
        checkpoint = built_checkpoint()
        observed = observed_snapshot(checkpoint)
        observed["base_sha"] = "9" * 40
        observed["plan_sha256"] = "f" * 64

        result = CHECKPOINT.validate_resume(resume_input(checkpoint, observed))

        self.assertEqual(result["status"], "blocked")
        codes = {blocker["code"] for blocker in result["blockers"]}
        self.assertTrue(
            {
                "base_sha_mismatch",
                "observed_plan_base_mismatch",
                "observed_plan_hash_invalid",
                "plan_hash_mismatch",
            }.issubset(codes)
        )

    def test_manifest_hash_mismatch_blocks_live_resume(self) -> None:
        checkpoint = built_checkpoint()
        observed = observed_snapshot(checkpoint)
        observed["resource_manifest"]["sha256"] = "b" * 64

        result = CHECKPOINT.validate_resume(resume_input(checkpoint, observed))

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "manifest_hash_mismatch",
            {blocker["code"] for blocker in result["blockers"]},
        )

    def test_checkpoint_bytes_hash_tamper_blocks_resume(self) -> None:
        checkpoint = built_checkpoint()
        observed = observed_snapshot(checkpoint)
        checkpoint["current"]["gate"] = "integration"

        result = CHECKPOINT.validate_resume(resume_input(checkpoint, observed))

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "checkpoint_hash_mismatch",
            {blocker["code"] for blocker in result["blockers"]},
        )

    def test_resume_requires_observed_plan_for_hash_recomputation(self) -> None:
        checkpoint = built_checkpoint()
        observed = observed_snapshot(checkpoint)
        observed.pop("plan")

        with self.assertRaisesRegex(
            CHECKPOINT.ContractError,
            "observed.plan must be an object",
        ):
            CHECKPOINT.validate_resume(resume_input(checkpoint, observed))

    def test_manifest_lost_with_old_roots_blocks(self) -> None:
        checkpoint = built_checkpoint()
        observed = observed_snapshot(checkpoint)
        observed["resource_manifest"] = {
            "locator": MANIFEST_PATH,
            "state": "lost",
            "sha256": None,
        }

        result = CHECKPOINT.validate_resume(resume_input(checkpoint, observed))

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "manifest_lost_with_live_roots",
            {blocker["code"] for blocker in result["blockers"]},
        )

    def test_mixed_root_liveness_blocks(self) -> None:
        checkpoint = built_checkpoint()
        observed = observed_snapshot(checkpoint)
        observed["roots"][0]["state"] = "lost"
        observed["workers"] = [
            worker
            for worker in observed["workers"]
            if worker["root"] != observed["roots"][0]["path"]
        ]

        result = CHECKPOINT.validate_resume(resume_input(checkpoint, observed))

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "mixed_root_liveness",
            {blocker["code"] for blocker in result["blockers"]},
        )

    def test_tampered_worker_root_outside_non_durable_paths_blocks(self) -> None:
        checkpoint = built_checkpoint()
        core = next(
            worker
            for worker in checkpoint["worker_ownership"]
            if worker["task_id"] == "core"
        )
        core["root"] = "/managed/session/wave-1/unowned-core"
        observed = observed_snapshot(checkpoint)

        result = CHECKPOINT.validate_resume(resume_input(checkpoint, observed))

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "worker_root_not_non_durable",
            {blocker["code"] for blocker in result["blockers"]},
        )

    def test_live_archive_without_resolution_evidence_blocks(self) -> None:
        checkpoint = built_checkpoint()
        observed = observed_snapshot(checkpoint)
        observed["durable_archive_refs"][0]["evidence"] = None

        result = CHECKPOINT.validate_resume(resume_input(checkpoint, observed))

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "archive_evidence_missing",
            {blocker["code"] for blocker in result["blockers"]},
        )

    def test_archive_evidence_hash_tamper_blocks(self) -> None:
        checkpoint = built_checkpoint()
        observed = observed_snapshot(checkpoint)
        observed["durable_archive_refs"][0]["evidence"]["sha256"] = "f" * 64

        result = CHECKPOINT.validate_resume(resume_input(checkpoint, observed))

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "archive_evidence_hash_mismatch",
            {blocker["code"] for blocker in result["blockers"]},
        )

    def test_archive_evidence_commit_mismatch_blocks(self) -> None:
        checkpoint = built_checkpoint()
        observed = observed_snapshot(checkpoint)
        evidence = observed["durable_archive_refs"][0]["evidence"]
        evidence["resolved_commit_sha"] = "9" * 40
        claim = {
            "kind": evidence["kind"],
            "locator": evidence["locator"],
            "resolved_commit_sha": evidence["resolved_commit_sha"],
        }
        evidence["sha256"] = CHECKPOINT._canonical_sha256(
            claim, "archive evidence"
        )

        result = CHECKPOINT.validate_resume(resume_input(checkpoint, observed))

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "archive_evidence_mismatch",
            {blocker["code"] for blocker in result["blockers"]},
        )

    def test_missing_archive_blocks_reconstruction(self) -> None:
        checkpoint = built_checkpoint()
        observed = observed_snapshot(
            checkpoint,
            manifest_state="lost",
            root_state="lost",
        )
        observed["durable_archive_refs"][0]["state"] = "lost"
        observed["durable_archive_refs"][0]["evidence"] = None

        result = CHECKPOINT.validate_resume(resume_input(checkpoint, observed))

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "archive_missing",
            {blocker["code"] for blocker in result["blockers"]},
        )
        self.assertIsNone(result["fresh_reconstruction_plan"])

    def test_stale_uncheckpointed_worker_success_blocks(self) -> None:
        checkpoint = built_checkpoint()
        observed = observed_snapshot(checkpoint)
        docs = next(
            worker for worker in observed["workers"] if worker["task_id"] == "docs"
        )
        docs["state"] = "succeeded"
        docs["unique_commit_sha"] = "3" * 40

        result = CHECKPOINT.validate_resume(resume_input(checkpoint, observed))

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "stale_worker_success",
            {blocker["code"] for blocker in result["blockers"]},
        )

    def test_success_report_from_lost_root_is_stale(self) -> None:
        checkpoint = built_checkpoint()
        observed = observed_snapshot(
            checkpoint,
            manifest_state="lost",
            root_state="lost",
        )
        observed["workers"] = [
            {
                "task_id": "core",
                "root": CORE_ROOT,
                "state": "succeeded",
                "unique_commit_sha": CORE_SHA,
            }
        ]

        result = CHECKPOINT.validate_resume(resume_input(checkpoint, observed))

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "stale_worker_success",
            {blocker["code"] for blocker in result["blockers"]},
        )

    def test_missing_or_unowned_root_observations_block(self) -> None:
        checkpoint = built_checkpoint()
        observed = observed_snapshot(checkpoint)
        observed["roots"].pop()
        observed["roots"].append(
            {"path": "/managed/session/wave-1/foreign", "state": "live"}
        )

        result = CHECKPOINT.validate_resume(resume_input(checkpoint, observed))

        codes = {blocker["code"] for blocker in result["blockers"]}
        self.assertEqual(result["status"], "blocked")
        self.assertIn("missing_root_observation", codes)
        self.assertIn("unexpected_root_observation", codes)

    def test_tampered_checkpoint_or_state_path_inside_disposable_root_blocks(self) -> None:
        checkpoint = built_checkpoint()
        checkpoint["state_path"] = f"{CORE_ROOT}/state"
        checkpoint["checkpoint_path"] = f"{CORE_ROOT}/state/checkpoint.json"
        checkpoint["resource_manifest"]["locator"] = (
            f"{CORE_ROOT}/state/resource-manifest.json"
        )
        observed = observed_snapshot(checkpoint)
        observed["state_path"] = checkpoint["state_path"]
        observed["checkpoint_path"] = checkpoint["checkpoint_path"]
        observed["resource_manifest"]["locator"] = checkpoint["resource_manifest"][
            "locator"
        ]

        result = CHECKPOINT.validate_resume(resume_input(checkpoint, observed))

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "state_inside_disposable_root",
            {blocker["code"] for blocker in result["blockers"]},
        )


class CliTests(unittest.TestCase):
    def test_cli_contract_error_is_structured_and_nonzero(self) -> None:
        source = checkpoint_input()
        source["base_sha"] = "bad"
        completed = subprocess.run(
            [str(SCRIPT), "checkpoint", "--input", "-"],
            input=json.dumps(source),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "")
        result = json.loads(completed.stdout)
        self.assertEqual(result["kind"], "parallel-wave-checkpoint-error")
        self.assertFalse(result["valid"])

    def test_cli_blocked_resume_is_structured_and_returns_one(self) -> None:
        checkpoint = built_checkpoint()
        observed = observed_snapshot(checkpoint)
        observed["resource_manifest"]["sha256"] = "b" * 64
        completed = subprocess.run(
            [str(SCRIPT), "resume-validate", "--input", "-"],
            input=json.dumps(resume_input(checkpoint, observed)),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, "")
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["kind"], "parallel-wave-resume-validation")

    def test_cli_writes_only_requested_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state"
            state_path.mkdir()
            input_path = root / "input.json"
            output_path = state_path / "checkpoint.json"
            source = checkpoint_input()
            source["state_path"] = str(state_path)
            source["checkpoint_path"] = str(output_path)
            source["resource_manifest"]["locator"] = str(
                state_path / "resource-manifest.json"
            )
            input_path.write_text(json.dumps(source), encoding="utf-8")

            before = sorted(path.name for path in root.iterdir())
            completed = subprocess.run(
                [
                    str(SCRIPT),
                    "checkpoint",
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
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
            self.assertEqual(before, ["input.json", "state"])
            self.assertEqual(after, ["input.json", "state"])
            result = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(result["kind"], "parallel-wave-restart-checkpoint")
            self.assertEqual(output_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                hashlib.sha256(output_path.read_bytes()).hexdigest(),
                CHECKPOINT._rendered_sha256(result),
            )

    def test_cli_requires_checkpoint_output(self) -> None:
        completed = subprocess.run(
            [str(SCRIPT), "checkpoint", "--input", "-"],
            input=json.dumps(checkpoint_input()),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "")
        self.assertIn(
            "checkpoint --output is required",
            json.loads(completed.stdout)["errors"][0],
        )

    def test_cli_refuses_existing_checkpoint_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory)
            output_path = state_path / "checkpoint.json"
            original = b"existing checkpoint\n"
            output_path.write_bytes(original)
            source = checkpoint_input()
            source["state_path"] = str(state_path)
            source["checkpoint_path"] = str(output_path)
            source["resource_manifest"]["locator"] = str(
                state_path / "resource-manifest.json"
            )

            completed = subprocess.run(
                [
                    str(SCRIPT),
                    "checkpoint",
                    "--input",
                    "-",
                    "--output",
                    str(output_path),
                ],
                input=json.dumps(source),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stderr, "")
            self.assertEqual(output_path.read_bytes(), original)
            result = json.loads(completed.stdout)
            self.assertIn("File exists", result["errors"][0])

    def test_cli_refuses_output_that_differs_from_checkpoint_path(self) -> None:
        source = checkpoint_input()
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "different.json"
            completed = subprocess.run(
                [
                    str(SCRIPT),
                    "checkpoint",
                    "--input",
                    "-",
                    "--output",
                    str(output_path),
                ],
                input=json.dumps(source),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertFalse(output_path.exists())
            self.assertIn(
                "must equal the receipt checkpoint_path",
                json.loads(completed.stdout)["errors"][0],
            )

    def test_runtime_has_no_process_shell_git_or_os_imports(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in (
            "import subprocess",
            "from subprocess",
            "import os",
            "from os",
            "import git",
            "from git",
            "os.system",
            "Popen(",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
