#!/usr/bin/env python3
"""Regression tests for worker worktree context attestation."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("worktree_context_attestation.py")
SPEC = importlib.util.spec_from_file_location("worktree_context_attestation", SCRIPT)
assert SPEC and SPEC.loader
ATTESTATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ATTESTATION)

HEAD_SHA = "a1" * 20
WRITE_SET_SHA256 = "b2" * 32
PLAN_SHA256 = "e5" * 32


def valid_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": "package-a",
        "plan_sha256": PLAN_SHA256,
        "assigned_worktree_root": "/worktrees/package-a",
        "controller_worktree_root": "/workspace/controller",
        "other_worker_roots": [
            "/worktrees/package-c",
            "/worktrees/package-b",
        ],
        "observed_cwd": "/worktrees/package-a/fleet-parallel-merge",
        "observed_git_top_level": "/worktrees/package-a",
        "expected_head_sha": HEAD_SHA.upper(),
        "observed_head_sha": HEAD_SHA,
        "write_set_sha256": WRITE_SET_SHA256.upper(),
        "observed_write_set_sha256": WRITE_SET_SHA256,
        "command_root": "/worktrees/package-a/fleet-parallel-merge",
        "path_operands": [
            "scripts/worktree_context_attestation.py",
            "./scripts//test_worktree_context_attestation.py",
        ],
    }


def blocker_result(
    **changes: object,
) -> dict[str, object]:
    document = valid_document()
    document.update(changes)
    return ATTESTATION.validate_attestation(document)


class ValidationTests(unittest.TestCase):
    def test_valid_attestation_is_ready_and_normalized(self) -> None:
        result = ATTESTATION.validate_attestation(valid_document())

        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["kind"], "worker-context-attestation")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["blockers"], [])
        evidence = result["normalized_evidence"]
        self.assertEqual(evidence["expected_head_sha"], HEAD_SHA)
        self.assertEqual(evidence["write_set_sha256"], WRITE_SET_SHA256)
        self.assertEqual(evidence["plan_sha256"], PLAN_SHA256)
        self.assertEqual(
            evidence["other_worker_roots"],
            ["/worktrees/package-b", "/worktrees/package-c"],
        )
        self.assertEqual(
            evidence["path_operands"],
            [
                "scripts/worktree_context_attestation.py",
                "scripts/test_worktree_context_attestation.py",
            ],
        )

    def test_normalizes_absolute_paths_without_resolving_the_filesystem(self) -> None:
        document = valid_document()
        document["assigned_worktree_root"] = "/worktrees/./package-a/"
        document["observed_git_top_level"] = "/worktrees/package-a"
        document["observed_cwd"] = (
            "/worktrees/package-a//fleet-parallel-merge/."
        )
        document["command_root"] = (
            "/worktrees/package-a/fleet-parallel-merge/"
        )

        result = ATTESTATION.validate_attestation(document)

        self.assertEqual(result["status"], "ready")
        evidence = result["normalized_evidence"]
        self.assertEqual(evidence["assigned_worktree_root"], "/worktrees/package-a")
        self.assertEqual(
            evidence["observed_cwd"],
            "/worktrees/package-a/fleet-parallel-merge",
        )

    def test_cwd_must_be_within_assigned_root(self) -> None:
        result = blocker_result(observed_cwd="/unrelated/checkout")

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "observed_cwd must be within assigned_worktree_root",
            result["blockers"],
        )

    def test_git_top_level_must_equal_assigned_root(self) -> None:
        result = blocker_result(
            observed_git_top_level="/worktrees/package-a/nested-repository"
        )

        self.assertIn(
            "observed_git_top_level must equal assigned_worktree_root",
            result["blockers"],
        )

    def test_controller_context_is_rejected(self) -> None:
        result = blocker_result(
            observed_cwd="/workspace/controller",
            observed_git_top_level="/workspace/controller",
            command_root="/workspace/controller",
        )

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "observed_cwd must not be within controller_worktree_root",
            result["blockers"],
        )
        self.assertIn(
            "observed_git_top_level must not be within controller_worktree_root",
            result["blockers"],
        )
        self.assertIn(
            "command_root must not be within controller_worktree_root",
            result["blockers"],
        )

    def test_other_worker_context_is_rejected(self) -> None:
        result = blocker_result(
            observed_cwd="/worktrees/package-b/src",
            observed_git_top_level="/worktrees/package-b",
            command_root="/worktrees/package-b",
        )

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "observed_cwd must not be within other_worker_roots[0]",
            result["blockers"],
        )

    def test_assigned_root_cannot_overlap_controller_or_other_worker(self) -> None:
        controller_result = blocker_result(
            controller_worktree_root="/worktrees/package-a"
        )
        other_result = blocker_result(
            other_worker_roots=["/worktrees/package-a/subtree"]
        )

        self.assertIn(
            "assigned_worktree_root must not overlap controller_worktree_root",
            controller_result["blockers"],
        )
        self.assertIn(
            "assigned_worktree_root must not overlap other_worker_roots[0]",
            other_result["blockers"],
        )

    def test_duplicate_other_worker_roots_are_rejected(self) -> None:
        result = blocker_result(
            other_worker_roots=[
                "/worktrees/package-b/",
                "/worktrees/./package-b",
            ]
        )

        self.assertIn(
            "other_worker_roots contains duplicate root '/worktrees/package-b'",
            result["blockers"],
        )

    def test_head_sha_mismatch_is_rejected(self) -> None:
        result = blocker_result(observed_head_sha="c3" * 20)

        self.assertIn(
            "observed_head_sha does not match expected_head_sha",
            result["blockers"],
        )

    def test_write_set_digest_mismatch_is_rejected(self) -> None:
        result = blocker_result(observed_write_set_sha256="d4" * 32)

        self.assertIn(
            "observed_write_set_sha256 does not match write_set_sha256",
            result["blockers"],
        )

    def test_sha_shapes_are_strict(self) -> None:
        head_result = blocker_result(expected_head_sha="a" * 39)
        digest_result = blocker_result(write_set_sha256="g" * 64)

        self.assertIn(
            "expected_head_sha must be a full 40-character hexadecimal Git SHA",
            head_result["blockers"],
        )
        self.assertIn(
            "write_set_sha256 must be a 64-character hexadecimal SHA-256 digest",
            digest_result["blockers"],
        )

    def test_command_root_must_be_within_assigned_root(self) -> None:
        result = blocker_result(command_root="/unrelated/checkout")

        self.assertIn(
            "command_root must be within assigned_worktree_root",
            result["blockers"],
        )

    def test_absolute_path_operand_is_rejected(self) -> None:
        result = blocker_result(path_operands=["/etc/passwd"])

        self.assertIn(
            "path_operands[0] must be relative to command_root",
            result["blockers"],
        )

    def test_parent_path_operand_is_rejected(self) -> None:
        result = blocker_result(path_operands=["scripts/../../controller/file.py"])

        self.assertIn(
            "path_operands[0] must not contain '..' segments",
            result["blockers"],
        )

    def test_windows_absolute_path_operand_is_rejected(self) -> None:
        result = blocker_result(path_operands=["C:/controller/file.py"])

        self.assertIn(
            "path_operands[0] must be relative to command_root",
            result["blockers"],
        )

    def test_repeated_cwd_subtree_prefix_is_rejected(self) -> None:
        result = blocker_result(
            path_operands=[
                "fleet-parallel-merge/scripts/"
                "worktree_context_attestation.py"
            ]
        )

        self.assertIn(
            "path_operands[0] repeats subtree prefix "
            "'fleet-parallel-merge' already represented by command_root",
            result["blockers"],
        )

    def test_full_repeated_command_root_prefix_is_rejected(self) -> None:
        result = blocker_result(
            command_root="/worktrees/package-a/src/module",
            path_operands=["src/module/test.py"],
        )

        self.assertIn(
            "path_operands[0] repeats subtree prefix "
            "'src/module' already represented by command_root",
            result["blockers"],
        )

    def test_full_repeated_cwd_prefix_is_rejected_when_command_root_is_parent(self) -> None:
        result = blocker_result(
            command_root="/worktrees/package-a",
            path_operands=[
                "fleet-parallel-merge/scripts/"
                "worktree_context_attestation.py"
            ],
        )

        self.assertIn(
            "path_operands[0] repeats subtree prefix "
            "'fleet-parallel-merge' already represented by observed_cwd",
            result["blockers"],
        )

    def test_missing_fields_are_reported_without_crashing(self) -> None:
        result = ATTESTATION.validate_attestation({"schema_version": 1})

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(
            result["blockers"][:2],
            [
                "task_id must be a non-empty string",
                "plan_sha256 must be a non-empty string",
            ],
        )
        self.assertIsNone(result["normalized_evidence"]["task_id"])

    def test_schema_version_must_be_integer_one(self) -> None:
        bool_result = blocker_result(schema_version=True)
        future_result = blocker_result(schema_version=2)

        self.assertIn("schema_version must be 1", bool_result["blockers"])
        self.assertIn("schema_version must be 1", future_result["blockers"])

    def test_nested_semantic_groups_are_rejected(self) -> None:
        document = valid_document()
        document["observed"] = {
            "cwd": document["observed_cwd"],
            "git_top_level": document["observed_git_top_level"],
        }
        document["controller"] = {"plan_sha256": document["plan_sha256"]}
        document["command"] = {"root": document["command_root"]}

        result = ATTESTATION.validate_attestation(document)

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "attestation input contains unsupported top-level fields: "
            "['command', 'controller', 'observed']",
            result["blockers"],
        )

    def test_input_document_is_not_mutated(self) -> None:
        document = valid_document()
        original = copy.deepcopy(document)

        ATTESTATION.validate_attestation(document)

        self.assertEqual(document, original)

    def test_non_object_input_is_structurally_blocked(self) -> None:
        result = ATTESTATION.validate_attestation([])

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blockers"], ["attestation input must be an object"])
        self.assertEqual(
            set(result["normalized_evidence"]),
            set(ATTESTATION.EVIDENCE_FIELDS),
        )


class CliTests(unittest.TestCase):
    def test_cli_defaults_to_stdin_and_stdout(self) -> None:
        completed = subprocess.run(
            [str(SCRIPT)],
            input=json.dumps(valid_document()),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(json.loads(completed.stdout)["status"], "ready")

    def test_blocked_cli_is_structured_and_nonzero(self) -> None:
        completed = subprocess.run(
            [str(SCRIPT), "--input", "-"],
            input=json.dumps(
                {
                    **valid_document(),
                    "observed_head_sha": "c3" * 20,
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, "")
        result = json.loads(completed.stdout)
        self.assertEqual(result["kind"], "worker-context-attestation")
        self.assertEqual(result["status"], "blocked")

    def test_cli_reads_and_writes_requested_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "input.json"
            output = root / "output.json"
            source.write_text(json.dumps(valid_document()), encoding="utf-8")

            completed = subprocess.run(
                [
                    str(SCRIPT),
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

            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(completed.stderr, "")
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["status"],
                "ready",
            )

    def test_malformed_json_is_structured_and_returns_two(self) -> None:
        completed = subprocess.run(
            [str(SCRIPT)],
            input="{",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "")
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["blockers"][0].startswith("unable to read input JSON:"))

    def test_scripts_have_shebang_and_executable_mode(self) -> None:
        for script in (SCRIPT, Path(__file__)):
            self.assertTrue(
                script.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3")
            )
            self.assertTrue(os.access(script, os.X_OK), f"{script.name} is executable")

    def test_runtime_has_no_git_shell_or_process_imports(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in (
            "import subprocess",
            "from subprocess",
            "import os",
            "from os",
            "import git",
            "from git",
            "os.system",
            "os.popen",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
