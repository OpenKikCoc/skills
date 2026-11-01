#!/usr/bin/env python3
"""Regression tests for disk-budget lifecycle recovery advice."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_ROOT = Path(__file__).parent
HYGIENE_SPEC = importlib.util.spec_from_file_location(
    "disk_budget",
    SCRIPT_ROOT / "disk_budget.py",
)
assert HYGIENE_SPEC and HYGIENE_SPEC.loader
HYGIENE = importlib.util.module_from_spec(HYGIENE_SPEC)
HYGIENE_SPEC.loader.exec_module(HYGIENE)

RECOVERY_SPEC = importlib.util.spec_from_file_location(
    "lifecycle_recovery",
    SCRIPT_ROOT / "lifecycle_recovery.py",
)
assert RECOVERY_SPEC and RECOVERY_SPEC.loader
RECOVERY = importlib.util.module_from_spec(RECOVERY_SPEC)
RECOVERY_SPEC.loader.exec_module(RECOVERY)

NOW = dt.datetime(2030, 1, 1, 0, 0, tzinfo=dt.timezone.utc)


def run(*arguments: str) -> str:
    completed = subprocess.run(
        arguments,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"command failed: {arguments}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed.stdout.strip()


def git(repo: Path, *arguments: str) -> str:
    return run("git", "-C", str(repo), *arguments)


def write_json(path: Path, document: object) -> None:
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class FinishReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test User")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "base")
        self.manifest = self.state / "wave.json"
        HYGIENE.init_manifest(self.manifest, "wave-1", NOW)
        self.worker = self.root / "worker"
        HYGIENE.reserve_worktree(
            self.manifest,
            "worker",
            "worker",
            str(self.repo),
            str(self.worker),
            "ordinary worker",
            "delete_when_released",
            NOW,
        )
        git(self.repo, "worktree", "add", str(self.worker), "HEAD")
        HYGIENE.bind_worktree(
            self.manifest,
            "worker",
            NOW + dt.timedelta(seconds=1),
        )
        HYGIENE.claim_directory(
            self.manifest,
            "fixture",
            str(self.root / "fixture"),
            "read-only migrated fixture",
            "report_only",
            NOW + dt.timedelta(seconds=1),
        )
        HYGIENE.start_run(
            self.manifest,
            NOW + dt.timedelta(seconds=2),
        )

    def test_reports_exact_blocker_and_lifecycle_cohorts(self) -> None:
        result = RECOVERY.finish_readiness(self.manifest)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(
            result["cohorts"],
            {
                "authorization_pending": ["worker"],
                "intentionally_retained": ["fixture"],
                "ordinary_disposable": [],
            },
        )
        self.assertEqual(
            [blocker["code"] for blocker in result["blockers"]],
            ["WORKTREE_CLEANUP_NOT_AUTHORIZED"],
        )
        self.assertFalse(result["mutation_authorized"])

    def test_non_active_manifest_is_not_finish_ready(self) -> None:
        HYGIENE.finish_run(
            self.manifest,
            "failed",
            NOW + dt.timedelta(seconds=3),
        )

        result = RECOVERY.finish_readiness(self.manifest)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(
            result["blockers"][0]["code"],
            "MANIFEST_NOT_ACTIVE",
        )


class RecoveryPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.manifest = self.state / "wave.json"
        HYGIENE.init_manifest(self.manifest, "wave-1", NOW)
        for resource_id, cleanup_policy in (
            ("alpha", "delete_when_released"),
            ("beta", "delete_when_released"),
            ("gamma", "delete_when_released"),
            ("retained", "report_only"),
        ):
            path = self.root / resource_id
            HYGIENE.claim_directory(
                self.manifest,
                resource_id,
                str(path),
                f"{resource_id} resource",
                cleanup_policy,
                NOW,
            )
        HYGIENE.start_run(
            self.manifest,
            NOW + dt.timedelta(seconds=1),
        )
        for resource_id in ("alpha", "beta", "gamma", "retained"):
            (self.root / resource_id / "payload").write_text(
                resource_id,
                encoding="utf-8",
            )
        HYGIENE.finish_run(
            self.manifest,
            "succeeded",
            NOW + dt.timedelta(seconds=2),
        )
        with mock.patch.object(
            HYGIENE,
            "active_handle_check",
            return_value=(False, "test probe found no activity"),
        ):
            self.plan = HYGIENE.build_plan(
                self.manifest,
                min_age_seconds=0,
                ttl_seconds=900,
                now=NOW + dt.timedelta(minutes=1),
            )
        self.plan_path = self.state / "plan.json"
        write_json(self.plan_path, self.plan)
        self.receipt_path = self.state / "receipt.json"
        eligible = [
            resource["id"]
            for resource in self.plan["resources"]
            if resource["eligible"]
        ]
        self.assertEqual(eligible, ["alpha", "beta", "gamma"])
        self.receipt = {
            "schema_version": 1,
            "kind": HYGIENE.RECEIPT_KIND,
            "executed_at": "2030-01-01T00:02:00Z",
            "plan_path": str(self.plan_path.resolve()),
            "plan_sha256": hashlib.sha256(
                self.plan_path.read_bytes()
            ).hexdigest(),
            "manifest_path": str(self.manifest.resolve()),
            "run_id": "wave-1",
            "status": "recovery_required",
            "current_resource": "beta",
            "current_quarantine": None,
            "results": [
                {
                    "id": "alpha",
                    "path": str(self.root / "alpha"),
                    "action": "remove_owned_directory",
                    "status": "removed",
                    "estimated_allocated_bytes": 1,
                },
                {
                    "id": "beta",
                    "path": str(self.root / "beta"),
                    "action": "remove_owned_directory",
                    "status": "failed",
                    "error": "synthetic failure",
                },
            ],
            "filesystem_before": {},
            "filesystem_after": {},
        }
        write_json(self.receipt_path, self.receipt)

    def render(self) -> dict[str, object]:
        return RECOVERY.recovery_plan(
            self.manifest,
            self.plan_path,
            self.receipt_path,
        )

    def test_classifies_partial_progress_without_authorizing_mutation(self) -> None:
        result = self.render()

        self.assertEqual(result["already_removed"], ["alpha"])
        self.assertEqual(result["failed_current"], "beta")
        self.assertEqual(result["eligible_remaining"], ["gamma"])
        self.assertEqual(result["intentionally_retained"], ["retained"])
        self.assertFalse(result["mutation_authorized"])
        self.assertTrue(
            any(
                "must never be rerun" in warning
                for warning in result["warnings"]
            )
        )

    def test_rejects_receipt_not_bound_to_exact_plan(self) -> None:
        self.receipt["plan_sha256"] = "0" * 64
        write_json(self.receipt_path, self.receipt)

        with self.assertRaisesRegex(
            RECOVERY.RecoveryError,
            "receipt does not bind",
        ):
            self.render()

    def test_rejects_non_prefix_or_replayed_result(self) -> None:
        self.receipt["results"][0]["id"] = "gamma"
        write_json(self.receipt_path, self.receipt)

        with self.assertRaisesRegex(
            RECOVERY.RecoveryError,
            "executed prefix",
        ):
            self.render()

    def test_rejects_result_for_blocked_resource(self) -> None:
        self.receipt["results"][1]["id"] = "retained"
        self.receipt["current_resource"] = "retained"
        write_json(self.receipt_path, self.receipt)

        with self.assertRaisesRegex(
            RECOVERY.RecoveryError,
            "was not eligible",
        ):
            self.render()


class CliTests(unittest.TestCase):
    def test_valid_recovery_plan_exits_successfully(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "manifest.json"
            plan_path = root / "plan.json"
            receipt_path = root / "receipt.json"
            output_path = root / "recovery.json"
            HYGIENE.init_manifest(manifest_path, "cli-run", NOW)
            resource_path = root / "resource"
            HYGIENE.claim_directory(
                manifest_path,
                "resource",
                str(resource_path),
                "CLI recovery fixture",
                "delete_when_released",
                NOW,
            )
            HYGIENE.start_run(
                manifest_path,
                NOW + dt.timedelta(seconds=1),
            )
            (resource_path / "payload").write_text("payload", encoding="utf-8")
            HYGIENE.finish_run(
                manifest_path,
                "succeeded",
                NOW + dt.timedelta(seconds=2),
            )
            with mock.patch.object(
                HYGIENE,
                "active_handle_check",
                return_value=(False, "test probe found no activity"),
            ):
                plan = HYGIENE.build_plan(
                    manifest_path,
                    min_age_seconds=0,
                    ttl_seconds=900,
                    now=NOW + dt.timedelta(minutes=1),
                )
            write_json(plan_path, plan)
            write_json(
                receipt_path,
                {
                    "schema_version": 1,
                    "kind": HYGIENE.RECEIPT_KIND,
                    "executed_at": "2030-01-01T00:02:00Z",
                    "plan_path": str(plan_path.resolve()),
                    "plan_sha256": hashlib.sha256(
                        plan_path.read_bytes()
                    ).hexdigest(),
                    "manifest_path": str(manifest_path.resolve()),
                    "run_id": "cli-run",
                    "status": "recovery_required",
                    "current_resource": "resource",
                    "current_quarantine": None,
                    "results": [
                        {
                            "id": "resource",
                            "path": str(resource_path),
                            "action": "remove_owned_directory",
                            "status": "failed",
                            "error": "synthetic failure",
                        }
                    ],
                    "filesystem_before": {},
                    "filesystem_after": {},
                },
            )

            completed = subprocess.run(
                [
                    str(SCRIPT_ROOT / "lifecycle_recovery.py"),
                    "recovery-plan",
                    "--manifest",
                    str(manifest_path),
                    "--plan",
                    str(plan_path),
                    "--receipt",
                    str(receipt_path),
                    "--output",
                    str(output_path),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stderr, "")
            self.assertEqual(
                json.loads(output_path.read_text(encoding="utf-8"))["status"],
                "review_required",
            )

    def test_invalid_input_is_structured_and_non_mutating(self) -> None:
        completed = subprocess.run(
            [
                str(SCRIPT_ROOT / "lifecycle_recovery.py"),
                "finish-readiness",
                "--manifest",
                str(SCRIPT_ROOT / "missing.json"),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "")
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["mutation_authorized"])


if __name__ == "__main__":
    unittest.main()
