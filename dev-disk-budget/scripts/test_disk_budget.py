#!/usr/bin/env python3
"""Regression tests for owned workspace disk budget."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import signal
import time
import subprocess
import tempfile
import unittest
import os
import sys

from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guarded_go  # noqa: E402


SCRIPT = Path(__file__).with_name("disk_budget.py")
SPEC = importlib.util.spec_from_file_location("disk_budget", SCRIPT)
assert SPEC and SPEC.loader
HYGIENE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HYGIENE)

NOW = dt.datetime(2030, 1, 1, 0, 0, tzinfo=dt.timezone.utc)


def run(*arguments: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        list(arguments),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {arguments}\n"
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


class DirectoryLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.manifest = self.state / "run.json"
        HYGIENE.init_manifest(self.manifest, "run-1", NOW)

    def claim(self, name: str = "cache") -> Path:
        path = self.root / name
        HYGIENE.claim_directory(
            self.manifest,
            name,
            str(path),
            "task-local Go build cache",
            "delete_when_released",
            NOW,
        )
        return path

    def plan(self) -> dict[str, object]:
        with mock.patch.object(
            HYGIENE,
            "active_handle_check",
            return_value=(False, "test probe found no activity"),
        ):
            return HYGIENE.build_plan(
                self.manifest,
                min_age_seconds=0,
                ttl_seconds=900,
                now=NOW + dt.timedelta(minutes=1),
            )

    def execute(self, plan: dict[str, object]) -> dict[str, object]:
        plan_path = self.state / "cleanup-plan.json"
        write_json(plan_path, plan)
        digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        with mock.patch.object(
            HYGIENE,
            "active_handle_check",
            return_value=(False, "test probe found no activity"),
        ):
            return HYGIENE.execute_cleanup(
                plan_path,
                expected_plan_sha256=digest,
                receipt_path=self.state / "cleanup-receipt.json",
                now=NOW + dt.timedelta(minutes=2),
            )

    def test_init_starts_in_planning_and_start_seals_resources(self) -> None:
        cache = self.claim()
        manifest = HYGIENE.start_run(
            self.manifest,
            NOW + dt.timedelta(seconds=1),
        )
        self.assertEqual(manifest["status"], "active")
        self.assertRegex(manifest["resource_set_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            manifest["resources"][0]["claimed_identity"],
            HYGIENE.path_identity(cache),
        )
        with self.assertRaisesRegex(HYGIENE.HygieneError, "only before"):
            HYGIENE.claim_directory(
                self.manifest,
                "late-cache",
                str(self.root / "late-cache"),
                "late undeclared output",
                "delete_when_released",
                NOW + dt.timedelta(seconds=2),
            )

    def test_nonempty_directory_cannot_be_adopted(self) -> None:
        foreign = self.root / "foreign-project"
        foreign.mkdir()
        (foreign / "valuable.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(HYGIENE.HygieneError, "existing directory"):
            HYGIENE.claim_directory(
                self.manifest,
                "foreign",
                str(foreign),
                "must not be adopted",
                "delete_when_released",
                NOW,
            )
        self.assertEqual(
            (foreign / "valuable.txt").read_text(encoding="utf-8"),
            "keep",
        )

    def test_overlapping_resource_roots_are_rejected(self) -> None:
        cache = self.claim()
        with self.assertRaisesRegex(HYGIENE.HygieneError, "another owned root"):
            HYGIENE.claim_directory(
                self.manifest,
                "nested",
                str(cache / "nested"),
                "nested accidental target",
                "delete_when_released",
                NOW,
            )

    def test_new_run_cannot_nest_under_existing_owned_root(self) -> None:
        cache = self.claim()
        other_manifest = self.state / "other-run.json"
        HYGIENE.init_manifest(other_manifest, "other-run", NOW)
        with self.assertRaisesRegex(HYGIENE.HygieneError, "another owned root"):
            HYGIENE.claim_directory(
                other_manifest,
                "nested-other-run",
                str(cache / "other-run-cache"),
                "must not nest under another run",
                "delete_when_released",
                NOW,
            )

    def test_failed_run_is_not_cleanup_eligible_until_explicit_release(self) -> None:
        self.claim()
        HYGIENE.start_run(self.manifest, NOW + dt.timedelta(seconds=1))
        HYGIENE.finish_run(
            self.manifest,
            "failed",
            NOW + dt.timedelta(seconds=2),
        )
        failed_plan = self.plan()
        self.assertFalse(failed_plan["resources"][0]["eligible"])
        self.assertIn(
            "run status 'failed' is not releasable",
            failed_plan["resources"][0]["reasons"],
        )
        HYGIENE.finish_run(
            self.manifest,
            "released",
            NOW + dt.timedelta(seconds=3),
        )
        released_plan = self.plan()
        self.assertTrue(released_plan["resources"][0]["eligible"])

    def test_active_run_cannot_skip_directly_to_released(self) -> None:
        self.claim()
        HYGIENE.start_run(self.manifest, NOW + dt.timedelta(seconds=1))
        with self.assertRaisesRegex(HYGIENE.HygieneError, "invalid run status"):
            HYGIENE.finish_run(
                self.manifest,
                "released",
                NOW + dt.timedelta(seconds=2),
            )
        self.assertEqual(HYGIENE.load_manifest(self.manifest)["status"], "active")

    def test_owned_directory_cleanup_requires_reviewed_plan_hash(self) -> None:
        cache = self.claim()
        HYGIENE.start_run(self.manifest, NOW + dt.timedelta(seconds=1))
        (cache / "go-build-entry").write_bytes(b"rebuildable")
        HYGIENE.finish_run(
            self.manifest,
            "succeeded",
            NOW + dt.timedelta(seconds=2),
        )
        plan = self.plan()
        self.assertTrue(plan["resources"][0]["eligible"])
        plan_path = self.state / "cleanup-plan.json"
        write_json(plan_path, plan)
        with self.assertRaisesRegex(HYGIENE.HygieneError, "does not match"):
            HYGIENE.execute_cleanup(
                plan_path,
                expected_plan_sha256="0" * 64,
                receipt_path=self.state / "wrong-hash-receipt.json",
                now=NOW + dt.timedelta(minutes=2),
            )
        self.assertTrue(cache.exists())
        receipt = self.execute(plan)
        self.assertEqual(receipt["status"], "completed")
        self.assertFalse(cache.exists())

    def test_plan_is_invalidated_when_owned_tree_changes(self) -> None:
        cache = self.claim()
        HYGIENE.start_run(self.manifest, NOW + dt.timedelta(seconds=1))
        (cache / "before").write_text("before", encoding="utf-8")
        HYGIENE.finish_run(
            self.manifest,
            "succeeded",
            NOW + dt.timedelta(seconds=2),
        )
        plan = self.plan()
        (cache / "after-plan").write_text("new", encoding="utf-8")
        with self.assertRaisesRegex(HYGIENE.HygieneError, "fingerprint changed"):
            self.execute(plan)
        self.assertTrue(cache.exists())
        self.assertTrue((cache / "after-plan").exists())

    def test_tampered_display_path_blocks_reviewed_plan(self) -> None:
        cache = self.claim()
        HYGIENE.start_run(self.manifest, NOW + dt.timedelta(seconds=1))
        HYGIENE.finish_run(
            self.manifest,
            "succeeded",
            NOW + dt.timedelta(seconds=2),
        )
        plan = self.plan()
        plan["resources"][0]["path"] = str(self.root / "misleading-display-path")
        plan_path = self.state / "tampered-plan.json"
        write_json(plan_path, plan)
        digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(HYGIENE.HygieneError, "plan path"):
            HYGIENE.execute_cleanup(
                plan_path,
                expected_plan_sha256=digest,
                receipt_path=self.state / "tampered-plan-receipt.json",
                now=NOW + dt.timedelta(minutes=2),
            )
        self.assertTrue(cache.exists())

    def test_marker_tampering_blocks_cleanup(self) -> None:
        cache = self.claim()
        HYGIENE.start_run(self.manifest, NOW + dt.timedelta(seconds=1))
        HYGIENE.finish_run(
            self.manifest,
            "succeeded",
            NOW + dt.timedelta(seconds=2),
        )
        marker = cache / HYGIENE.MARKER_NAME
        marker.write_text('{"foreign": true}\n', encoding="utf-8")
        plan = self.plan()
        self.assertFalse(plan["resources"][0]["eligible"])
        self.assertTrue(
            any(
                "ownership marker does not match" in reason
                for reason in plan["resources"][0]["reasons"]
            )
        )

    def test_symlink_inside_owned_directory_blocks_cleanup(self) -> None:
        cache = self.claim()
        HYGIENE.start_run(self.manifest, NOW + dt.timedelta(seconds=1))
        (cache / "outside-link").symlink_to(self.root / "state")
        HYGIENE.finish_run(
            self.manifest,
            "succeeded",
            NOW + dt.timedelta(seconds=2),
        )
        plan = self.plan()
        self.assertFalse(plan["resources"][0]["eligible"])
        self.assertTrue(
            any(
                "contains a symlink" in reason
                for reason in plan["resources"][0]["reasons"]
            )
        )
        self.assertTrue(self.state.exists())

    def test_nested_ownership_marker_blocks_parent_cleanup(self) -> None:
        cache = self.claim()
        HYGIENE.start_run(self.manifest, NOW + dt.timedelta(seconds=1))
        nested = cache / "another-run"
        nested.mkdir()
        (nested / HYGIENE.MARKER_NAME).write_text(
            '{"different": "owner"}\n',
            encoding="utf-8",
        )
        HYGIENE.finish_run(
            self.manifest,
            "succeeded",
            NOW + dt.timedelta(seconds=2),
        )
        plan = self.plan()
        self.assertFalse(plan["resources"][0]["eligible"])
        self.assertTrue(
            any(
                "nested ownership boundary" in reason
                for reason in plan["resources"][0]["reasons"]
            )
        )
        self.assertTrue(nested.exists())

    def test_existing_receipt_is_never_overwritten(self) -> None:
        cache = self.claim()
        HYGIENE.start_run(self.manifest, NOW + dt.timedelta(seconds=1))
        HYGIENE.finish_run(
            self.manifest,
            "succeeded",
            NOW + dt.timedelta(seconds=2),
        )
        plan = self.plan()
        plan_path = self.state / "cleanup-plan.json"
        write_json(plan_path, plan)
        digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        receipt_path = self.state / "existing-receipt.json"
        receipt_path.write_text("preserve\n", encoding="utf-8")
        with self.assertRaisesRegex(HYGIENE.HygieneError, "already exists"):
            HYGIENE.execute_cleanup(
                plan_path,
                expected_plan_sha256=digest,
                receipt_path=receipt_path,
                now=NOW + dt.timedelta(minutes=2),
            )
        self.assertEqual(receipt_path.read_text(encoding="utf-8"), "preserve\n")
        self.assertTrue(cache.exists())

    def test_sealed_resource_set_tampering_is_rejected(self) -> None:
        self.claim()
        HYGIENE.start_run(self.manifest, NOW + dt.timedelta(seconds=1))
        document = json.loads(self.manifest.read_text(encoding="utf-8"))
        document["resources"][0]["purpose"] = "silently repurposed target"
        write_json(self.manifest, document)
        with self.assertRaisesRegex(HYGIENE.HygieneError, "resource set changed"):
            HYGIENE.load_manifest(self.manifest)


class ReservationStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test User")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "base")
        self.worktree = self.root / "worker"
        self.state = self.root / "state"
        self.state.mkdir()
        self.manifest = self.state / "run.json"
        HYGIENE.init_manifest(self.manifest, "reservation-run", NOW)
        self.resource = HYGIENE.reserve_worktree(
            self.manifest,
            "worker-tree",
            "core",
            str(self.repo),
            str(self.worktree),
            "isolated core task",
            "delete_when_released",
            NOW,
        )
        self.worktree = Path(self.resource["path"])

    def status(self) -> dict[str, object]:
        return HYGIENE.reservation_status(
            self.manifest,
            "worker-tree",
            NOW + dt.timedelta(seconds=1),
        )

    def expected_identity_sha256(self) -> str:
        identity = HYGIENE.reservation_document(
            "reservation-run",
            "worker-tree",
            "core",
            Path(self.resource["repo"]),
            Path(self.resource["path"]),
        )
        return hashlib.sha256(
            json.dumps(
                identity,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    def test_ready_status_revalidates_identity_without_mutation(self) -> None:
        reservation_path = Path(self.resource["reservation_path"])
        lock_path = self.manifest.with_name(f".{self.manifest.name}.lock")
        lock_path.unlink(missing_ok=True)
        manifest_before = self.manifest.read_bytes()
        reservation_before = reservation_path.read_bytes()
        registry_before = git(self.repo, "worktree", "list", "--porcelain")

        result = self.status()

        self.assertEqual(
            result,
            {
                "schema_version": 1,
                "kind": "dev-disk-budget-reservation-status",
                "status": "ready",
                "run_id": "reservation-run",
                "resource_id": "worker-tree",
                "task_id": "core",
                "reservation_sha256": hashlib.sha256(
                    reservation_before
                ).hexdigest(),
                "reservation_identity_sha256": self.expected_identity_sha256(),
                "repo": str(self.repo.resolve()),
                "path": str(self.worktree.resolve()),
                "blockers": [],
                "observed_at": "2030-01-01T00:00:01Z",
            },
        )
        self.assertEqual(self.manifest.read_bytes(), manifest_before)
        self.assertEqual(reservation_path.read_bytes(), reservation_before)
        self.assertEqual(
            git(self.repo, "worktree", "list", "--porcelain"),
            registry_before,
        )
        self.assertFalse(lock_path.exists())
        self.assertFalse(self.worktree.exists())

    def test_reservation_marker_symlink_is_blocked_without_following(self) -> None:
        reservation_path = Path(self.resource["reservation_path"])
        marker_target = self.state / "foreign-reservation.json"
        marker_target.write_bytes(reservation_path.read_bytes())
        reservation_path.unlink()
        reservation_path.symlink_to(marker_target)

        result = self.status()

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reservation_sha256"], None)
        self.assertEqual(
            result["reservation_identity_sha256"],
            self.expected_identity_sha256(),
        )
        self.assertIn(
            f"worktree reservation marker is a symlink: {reservation_path}",
            result["blockers"],
        )

    def test_occupied_target_blocks_reservation(self) -> None:
        self.worktree.mkdir()

        result = self.status()

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            f"reserved worktree path is occupied: {self.worktree}",
            result["blockers"],
        )

    def test_symlink_target_blocks_reservation(self) -> None:
        foreign = self.root / "foreign"
        foreign.mkdir()
        self.worktree.symlink_to(foreign, target_is_directory=True)

        result = self.status()

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            f"reserved worktree path is a symlink: {self.worktree}",
            result["blockers"],
        )

    def test_reservation_marker_byte_tampering_blocks_reservation(self) -> None:
        reservation_path = Path(self.resource["reservation_path"])
        document = json.loads(reservation_path.read_bytes())
        reservation_path.write_text(
            json.dumps(document, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        result = self.status()

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(
            result["reservation_sha256"],
            hashlib.sha256(reservation_path.read_bytes()).hexdigest(),
        )
        self.assertIn(
            "worktree reservation marker bytes do not match canonical "
            "manifest identity",
            result["blockers"],
        )
        self.assertIn(
            "worktree reservation marker SHA-256 does not match canonical "
            "manifest identity",
            result["blockers"],
        )
        self.assertNotIn(
            "worktree reservation marker identity does not match manifest",
            result["blockers"],
        )

    def test_reservation_marker_identity_tampering_blocks_reservation(self) -> None:
        reservation_path = Path(self.resource["reservation_path"])
        original = json.loads(reservation_path.read_bytes())
        mutations = {
            "run_id": "other-run",
            "resource_id": "other-worker",
            "task_id": "other-task",
            "repo": str(self.root / "other-repo"),
            "canonical_path": str(self.root / "other-worker"),
        }

        for field, value in mutations.items():
            with self.subTest(field=field):
                tampered = dict(original)
                tampered[field] = value
                write_json(reservation_path, tampered)

                result = self.status()

                self.assertEqual(result["status"], "blocked")
                self.assertIn(
                    "worktree reservation marker identity does not match manifest",
                    result["blockers"],
                )
                self.assertIn(
                    "worktree reservation marker bytes do not match canonical "
                    "manifest identity",
                    result["blockers"],
                )
                self.assertIn(
                    "worktree reservation marker SHA-256 does not match canonical "
                    "manifest identity",
                    result["blockers"],
                )
        write_json(reservation_path, original)

    def test_already_registered_path_blocks_reservation(self) -> None:
        with mock.patch.object(
            HYGIENE,
            "git_worktrees",
            return_value={
                Path(self.resource["path"]): {
                    "worktree": self.resource["path"],
                }
            },
        ):
            result = self.status()

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            f"worktree path is already registered in repository: {self.worktree}",
            result["blockers"],
        )

    def test_wrong_resource_kind_is_blocked(self) -> None:
        HYGIENE.claim_directory(
            self.manifest,
            "worker-cache",
            str(self.root / "worker-cache"),
            "worker cache",
            "delete_when_released",
            NOW,
        )

        result = HYGIENE.reservation_status(
            self.manifest,
            "worker-cache",
            NOW + dt.timedelta(seconds=1),
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["repo"], None)
        self.assertIn(
            "resource kind must be git_worktree, observed 'owned_directory'",
            result["blockers"],
        )

    def test_bound_worktree_is_blocked(self) -> None:
        git(self.repo, "worktree", "add", "-b", "worker", str(self.worktree))
        HYGIENE.bind_worktree(
            self.manifest,
            "worker-tree",
            NOW + dt.timedelta(milliseconds=500),
        )

        result = self.status()

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "worktree registration_state must be reserved and unbound, "
            "observed 'bound'",
            result["blockers"],
        )

    def test_active_manifest_is_blocked(self) -> None:
        git(self.repo, "worktree", "add", "-b", "worker", str(self.worktree))
        HYGIENE.bind_worktree(
            self.manifest,
            "worker-tree",
            NOW + dt.timedelta(milliseconds=500),
        )
        HYGIENE.start_run(self.manifest, NOW + dt.timedelta(seconds=1))

        result = HYGIENE.reservation_status(
            self.manifest,
            "worker-tree",
            NOW + dt.timedelta(seconds=2),
        )

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "run status must be planning, observed 'active'",
            result["blockers"],
        )

    def test_cli_reports_ready_reservation(self) -> None:
        completed = subprocess.run(
            [
                str(SCRIPT),
                "reservation-status",
                "--manifest",
                str(self.manifest),
                "--id",
                "worker-tree",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        result = json.loads(completed.stdout)
        self.assertEqual(result["kind"], HYGIENE.RESERVATION_STATUS_KIND)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["run_id"], "reservation-run")
        self.assertEqual(result["resource_id"], "worker-tree")
        self.assertEqual(result["task_id"], "core")
        self.assertEqual(result["repo"], str(self.repo.resolve()))
        self.assertEqual(result["path"], str(self.worktree))
        self.assertRegex(result["reservation_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            result["reservation_identity_sha256"],
            self.expected_identity_sha256(),
        )
        self.assertEqual(result["blockers"], [])
        self.assertRegex(
            result["observed_at"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$",
        )
        self.assertEqual(completed.stderr, "")

    def test_cli_reports_blocked_reservation_as_structured_nonzero_json(
        self,
    ) -> None:
        self.worktree.mkdir()

        completed = subprocess.run(
            [
                str(SCRIPT),
                "reservation-status",
                "--manifest",
                str(self.manifest),
                "--id",
                "worker-tree",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, "")
        result = json.loads(completed.stdout)
        self.assertEqual(
            set(result),
            {
                "schema_version",
                "kind",
                "status",
                "run_id",
                "resource_id",
                "task_id",
                "repo",
                "path",
                "reservation_sha256",
                "reservation_identity_sha256",
                "blockers",
                "observed_at",
            },
        )
        self.assertEqual(result["kind"], HYGIENE.RESERVATION_STATUS_KIND)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["run_id"], "reservation-run")
        self.assertEqual(result["resource_id"], "worker-tree")
        self.assertEqual(result["task_id"], "core")
        self.assertEqual(result["repo"], str(self.repo.resolve()))
        self.assertEqual(result["path"], str(self.worktree))
        self.assertRegex(result["reservation_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            result["reservation_identity_sha256"],
            self.expected_identity_sha256(),
        )
        self.assertEqual(
            result["blockers"],
            [f"reserved worktree path is occupied: {self.worktree}"],
        )
        self.assertRegex(
            result["observed_at"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$",
        )

    def test_cli_blocks_reservation_marker_symlink(self) -> None:
        reservation_path = Path(self.resource["reservation_path"])
        marker_target = self.state / "foreign-reservation.json"
        marker_target.write_bytes(reservation_path.read_bytes())
        reservation_path.unlink()
        reservation_path.symlink_to(marker_target)

        completed = subprocess.run(
            [
                str(SCRIPT),
                "reservation-status",
                "--manifest",
                str(self.manifest),
                "--id",
                "worker-tree",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, "")
        result = json.loads(completed.stdout)
        self.assertEqual(result["kind"], HYGIENE.RESERVATION_STATUS_KIND)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reservation_sha256"], None)
        self.assertEqual(
            result["blockers"],
            [f"worktree reservation marker is a symlink: {reservation_path}"],
        )


class WorktreeLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test User")
        (self.repo / ".gitignore").write_text("ignored.out\n", encoding="utf-8")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", ".gitignore", "README.md")
        git(self.repo, "commit", "-m", "base")
        self.worktree = self.root / "worker"
        self.state = self.root / "state"
        self.state.mkdir()
        self.manifest = self.state / "run.json"
        HYGIENE.init_manifest(self.manifest, "run-worktree", NOW)
        HYGIENE.reserve_worktree(
            self.manifest,
            "worker-tree",
            "core",
            str(self.repo),
            str(self.worktree),
            "isolated core task",
            "delete_when_released",
            NOW,
        )
        git(self.repo, "worktree", "add", "-b", "worker", str(self.worktree))
        HYGIENE.bind_worktree(
            self.manifest,
            "worker-tree",
            NOW + dt.timedelta(milliseconds=500),
        )
        HYGIENE.start_run(self.manifest, NOW + dt.timedelta(seconds=1))

    def commit_worker_change(self) -> str:
        (self.worktree / "core.txt").write_text("result\n", encoding="utf-8")
        git(self.worktree, "add", "core.txt")
        git(self.worktree, "commit", "-m", "result")
        return git(self.worktree, "rev-parse", "HEAD")

    def write_handoff(
        self,
        head: str,
        *,
        cleanup_allowed: bool = True,
    ) -> Path:
        handoff = {
            "schema_version": 1,
            "kind": "parallel-change-handoff",
            "status": "ready",
            "blockers": [],
            "workers": [
                {
                    "task_id": "core",
                    "commit_sha": head,
                    "accepted": True,
                    "preservation": {
                        "kind": "archived",
                        "locator": "refs/archive/core",
                        "contains_head": True,
                        "observed_at": "2030-01-01T00:00:02Z",
                    },
                    "cleanup": {
                        "allowed": cleanup_allowed,
                        "reasons": (
                            []
                            if cleanup_allowed
                            else ["worker has unique artifacts"]
                        ),
                    },
                }
            ],
        }
        path = self.state / "handoff.json"
        write_json(path, handoff)
        return path

    def authorize(self, head: str, *, cleanup_allowed: bool = True) -> None:
        git(self.repo, "update-ref", "refs/archive/core", head)
        handoff = self.write_handoff(head, cleanup_allowed=cleanup_allowed)
        HYGIENE.authorize_worktree(
            self.manifest,
            "worker-tree",
            "refs/archive/core",
            str(handoff),
            NOW + dt.timedelta(seconds=2),
        )

    def test_ready_handoff_and_archive_allow_non_force_worktree_cleanup(self) -> None:
        head = self.commit_worker_change()
        self.authorize(head)
        HYGIENE.finish_run(
            self.manifest,
            "succeeded",
            NOW + dt.timedelta(seconds=3),
        )
        with mock.patch.object(
            HYGIENE,
            "active_handle_check",
            return_value=(False, "test probe found no activity"),
        ):
            plan = HYGIENE.build_plan(
                self.manifest,
                min_age_seconds=0,
                ttl_seconds=900,
                now=NOW + dt.timedelta(minutes=1),
            )
        self.assertTrue(plan["resources"][0]["eligible"])
        plan_path = self.state / "cleanup-plan.json"
        write_json(plan_path, plan)
        digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        with mock.patch.object(
            HYGIENE,
            "active_handle_check",
            return_value=(False, "test probe found no activity"),
        ):
            receipt = HYGIENE.execute_cleanup(
                plan_path,
                expected_plan_sha256=digest,
                receipt_path=self.state / "cleanup-receipt.json",
                now=NOW + dt.timedelta(minutes=2),
            )
        self.assertEqual(receipt["status"], "completed")
        self.assertFalse(self.worktree.exists())
        self.assertNotIn(
            str(self.worktree),
            git(self.repo, "worktree", "list", "--porcelain"),
        )
        self.assertEqual(git(self.repo, "rev-parse", "refs/archive/core"), head)

    def test_orchestrator_cleanup_refusal_blocks_authorization(self) -> None:
        head = self.commit_worker_change()
        with self.assertRaisesRegex(
            HYGIENE.HygieneError,
            "does not authorize cleanup",
        ):
            self.authorize(head, cleanup_allowed=False)
        self.assertTrue(self.worktree.exists())

    def test_ignored_artifact_blocks_worktree_authorization(self) -> None:
        head = self.commit_worker_change()
        (self.worktree / "ignored.out").write_text(
            "unique benchmark artifact",
            encoding="utf-8",
        )
        git(self.repo, "update-ref", "refs/archive/core", head)
        handoff = self.write_handoff(head)
        with self.assertRaisesRegex(HYGIENE.HygieneError, "worktree is dirty"):
            HYGIENE.authorize_worktree(
                self.manifest,
                "worker-tree",
                "refs/archive/core",
                str(handoff),
                NOW + dt.timedelta(seconds=2),
            )
        self.assertTrue((self.worktree / "ignored.out").exists())

    def test_existing_worktree_cannot_be_reserved_retroactively(self) -> None:
        manifest = self.state / "other-run.json"
        HYGIENE.init_manifest(manifest, "other-run", NOW)
        with self.assertRaisesRegex(HYGIENE.HygieneError, "existing worktree path"):
            HYGIENE.reserve_worktree(
                manifest,
                "retroactive",
                "retroactive",
                str(self.repo),
                str(self.worktree),
                "must not adopt an existing worktree",
                "delete_when_released",
                NOW,
            )

    def test_second_run_cannot_reserve_same_uncreated_worktree_path(self) -> None:
        second_root = Path(self.temporary.name) / "reservation-race"
        repo = second_root / "repo"
        state = second_root / "state"
        repo.mkdir(parents=True)
        state.mkdir()
        git(repo, "init")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "config", "user.name", "Test User")
        (repo / "README.md").write_text("base\n", encoding="utf-8")
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "base")
        target = second_root / "shared-worker"
        first_manifest = state / "first.json"
        second_manifest = state / "second.json"
        HYGIENE.init_manifest(first_manifest, "first-run", NOW)
        HYGIENE.init_manifest(second_manifest, "second-run", NOW)
        first = HYGIENE.reserve_worktree(
            first_manifest,
            "first-worker",
            "first-task",
            str(repo),
            str(target),
            "first reservation",
            "delete_when_released",
            NOW,
        )
        with self.assertRaisesRegex(HYGIENE.HygieneError, "reservation already exists"):
            HYGIENE.reserve_worktree(
                second_manifest,
                "second-worker",
                "second-task",
                str(repo),
                str(target),
                "conflicting reservation",
                "delete_when_released",
                NOW,
            )
        self.assertTrue(Path(first["reservation_path"]).exists())
        self.assertFalse(target.exists())

    def test_succeeded_requires_cleanup_authorization_for_disposable_worktree(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            HYGIENE.HygieneError,
            "unauthorized disposable worktrees",
        ):
            HYGIENE.finish_run(
                self.manifest,
                "succeeded",
                NOW + dt.timedelta(seconds=2),
            )
        self.assertEqual(HYGIENE.load_manifest(self.manifest)["status"], "active")


class GoCacheTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.session_manifest = self.state / "session.json"
        self.wave_manifest = self.state / "wave.json"
        HYGIENE.init_manifest(self.session_manifest, "session-1", NOW)
        HYGIENE.init_manifest(self.wave_manifest, "wave-1", NOW)
        self.gocache = self.root / "session-gocache"
        HYGIENE.claim_directory(
            self.session_manifest,
            "gocache",
            str(self.gocache),
            "shared Go build cache",
            "delete_when_released",
            NOW,
            role="go_build_cache",
            scope="session_shared",
        )
        self.worker_a_tmp = self.root / "worker-a-tmp"
        self.worker_b_tmp = self.root / "worker-b-tmp"
        for task_id, path in (
            ("worker-a", self.worker_a_tmp),
            ("worker-b", self.worker_b_tmp),
        ):
            HYGIENE.claim_directory(
                self.wave_manifest,
                f"{task_id}-tmp",
                str(path),
                f"{task_id} Go temporary build root",
                "delete_when_released",
                NOW,
                role="go_tmp",
                scope="task",
                task_id=task_id,
            )
        HYGIENE.start_run(
            self.session_manifest,
            NOW + dt.timedelta(seconds=1),
        )
        HYGIENE.start_run(
            self.wave_manifest,
            NOW + dt.timedelta(seconds=1),
        )

    def test_parallel_tasks_share_gocache_and_isolate_gotmpdir(self) -> None:
        worker_a = HYGIENE.build_go_environment(
            self.session_manifest,
            self.wave_manifest,
            "worker-a",
        )
        worker_b = HYGIENE.build_go_environment(
            self.session_manifest,
            self.wave_manifest,
            "worker-b",
        )
        self.assertEqual(
            worker_a["environment"]["GOCACHE"],
            str(self.gocache.resolve()),
        )
        self.assertEqual(
            worker_b["environment"]["GOCACHE"],
            str(self.gocache.resolve()),
        )
        self.assertEqual(
            worker_a["environment"]["GOTMPDIR"],
            str(self.worker_a_tmp.resolve()),
        )
        self.assertEqual(
            worker_b["environment"]["GOTMPDIR"],
            str(self.worker_b_tmp.resolve()),
        )
        self.assertNotEqual(
            worker_a["environment"]["GOTMPDIR"],
            worker_b["environment"]["GOTMPDIR"],
        )
        self.assertEqual(worker_a["inherited"], ["GOMODCACHE"])

    def test_go_env_receipt_has_no_readiness_status(self) -> None:
        receipt = HYGIENE.build_go_environment(
            self.session_manifest,
            self.wave_manifest,
            "worker-a",
        )
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(receipt["kind"], "dev-disk-budget-go-environment")
        self.assertEqual(receipt["session_run_id"], "session-1")
        self.assertEqual(receipt["wave_run_id"], "wave-1")
        self.assertEqual(receipt["task_id"], "worker-a")
        self.assertEqual(set(receipt["environment"]), {"GOCACHE", "GOTMPDIR"})
        self.assertNotIn("status", receipt)

    def test_host_shared_gocache_reuses_one_cross_session_path(self) -> None:
        host_cache = self.root / "host-go-cache"
        initialized = HYGIENE.init_host_go_cache(host_cache, NOW)
        self.assertEqual(
            initialized["kind"],
            "dev-disk-budget-host-go-cache",
        )
        rendered = HYGIENE.build_go_environment(
            None,
            self.wave_manifest,
            "worker-a",
            host_cache,
        )
        self.assertEqual(
            rendered["gocache_source"],
            "operator_managed_host_shared",
        )
        self.assertEqual(
            rendered["environment"]["GOCACHE"],
            str(host_cache.resolve()),
        )
        topology = HYGIENE.validate_go_topology(
            None,
            self.wave_manifest,
            "ab" * 32,
            ["worker-a", "worker-b"],
            host_cache,
        )
        self.assertEqual(topology["status"], "ready")
        self.assertEqual(
            topology["gocache_source"],
            "operator_managed_host_shared",
        )

    def test_host_shared_gocache_rejects_unmarked_existing_directory(self) -> None:
        unmarked = self.root / "unmarked-go-cache"
        unmarked.mkdir()
        with self.assertRaisesRegex(
            HYGIENE.HygieneError,
            "host-shared Go cache marker",
        ):
            HYGIENE.build_go_environment(
                None,
                self.wave_manifest,
                "worker-a",
                unmarked,
            )

    def test_init_host_shared_gocache_refuses_existing_path(self) -> None:
        existing = self.root / "existing-go-cache"
        existing.mkdir()
        with self.assertRaisesRegex(
            HYGIENE.HygieneError,
            "refusing to adopt an existing host-shared GOCACHE",
        ):
            HYGIENE.init_host_go_cache(existing, NOW)

    def test_init_host_shared_gocache_preserves_existing_sibling_marker(self) -> None:
        target = self.root / "new-go-cache"
        marker = HYGIENE.host_cache_marker_path(target)
        marker.write_text("preserve\n", encoding="utf-8")

        with self.assertRaisesRegex(
            HYGIENE.HygieneError,
            "host-shared Go cache marker already exists",
        ):
            HYGIENE.init_host_go_cache(target, NOW)

        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve\n")
        self.assertFalse(target.exists())

    def test_per_worker_gocache_is_rejected(self) -> None:
        manifest = self.state / "invalid.json"
        HYGIENE.init_manifest(manifest, "invalid-run", NOW)
        with self.assertRaisesRegex(
            HYGIENE.HygieneError,
            "go_build_cache must be session_shared",
        ):
            HYGIENE.claim_directory(
                manifest,
                "cold-cache",
                str(self.root / "cold-cache"),
                "must not create one cold cache per worker",
                "delete_when_released",
                NOW,
                role="go_build_cache",
                scope="task",
                task_id="worker-a",
            )

    def test_wave_tmp_can_release_while_session_cache_remains_active(self) -> None:
        HYGIENE.finish_run(
            self.wave_manifest,
            "succeeded",
            NOW + dt.timedelta(seconds=2),
        )
        with mock.patch.object(
            HYGIENE,
            "active_handle_check",
            return_value=(False, "test probe found no activity"),
        ):
            plan = HYGIENE.build_plan(
                self.wave_manifest,
                min_age_seconds=0,
                ttl_seconds=900,
                now=NOW + dt.timedelta(minutes=1),
            )
        self.assertEqual(plan["eligible_count"], 2)
        plan_path = self.state / "wave-plan.json"
        write_json(plan_path, plan)
        digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        with mock.patch.object(
            HYGIENE,
            "active_handle_check",
            return_value=(False, "test probe found no activity"),
        ):
            receipt = HYGIENE.execute_cleanup(
                plan_path,
                expected_plan_sha256=digest,
                receipt_path=self.state / "wave-receipt.json",
                now=NOW + dt.timedelta(minutes=2),
            )
        self.assertEqual(receipt["status"], "completed")
        self.assertFalse(self.worker_a_tmp.exists())
        self.assertFalse(self.worker_b_tmp.exists())
        self.assertTrue(self.gocache.exists())
        self.assertEqual(
            HYGIENE.load_manifest(self.session_manifest)["status"],
            "active",
        )

    def test_go_env_rejects_missing_task_tmpdir(self) -> None:
        with self.assertRaisesRegex(
            HYGIENE.HygieneError,
            "exactly one task go_tmp",
        ):
            HYGIENE.build_go_environment(
                self.session_manifest,
                self.wave_manifest,
                "worker-c",
            )

    def test_go_topology_binds_all_parallel_participants_to_manifests(self) -> None:
        result = HYGIENE.validate_go_topology(
            self.session_manifest,
            self.wave_manifest,
            "ab" * 32,
            ["worker-b", "worker-a"],
        )

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["task_ids"], ["worker-a", "worker-b"])
        self.assertEqual(
            {row["environment"]["GOCACHE"] for row in result["environments"]},
            {str(self.gocache.resolve())},
        )
        self.assertEqual(len(result["environments"]), 2)

    def test_go_topology_rejects_missing_or_extra_task_tmpdir(self) -> None:
        result = HYGIENE.validate_go_topology(
            self.session_manifest,
            self.wave_manifest,
            "ab" * 32,
            ["worker-a", "worker-c"],
        )

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "wave go_tmp task set must exactly match declared Go participants",
            result["blockers"],
        )
        self.assertIn(
            "task 'worker-c' must have exactly one task-scoped go_tmp",
            result["blockers"],
        )

    def test_go_topology_rejects_generic_go_cache_placeholder(self) -> None:
        manifest = self.state / "placeholder.json"
        HYGIENE.init_manifest(manifest, "placeholder-wave", NOW)
        HYGIENE.claim_directory(
            manifest,
            "placeholder-cache",
            str(self.root / "placeholder-cache"),
            "worker GOCACHE placeholder",
            "delete_when_released",
            NOW,
        )
        HYGIENE.claim_directory(
            manifest,
            "worker-a-tmp",
            str(self.root / "placeholder-worker-a-tmp"),
            "worker-a Go temporary build root",
            "delete_when_released",
            NOW,
            role="go_tmp",
            scope="task",
            task_id="worker-a",
        )
        HYGIENE.start_run(manifest, NOW + dt.timedelta(seconds=1))

        result = HYGIENE.validate_go_topology(
            self.session_manifest,
            manifest,
            "ab" * 32,
            ["worker-a"],
        )

        self.assertEqual(result["status"], "blocked")
        self.assertTrue(
            any("generic role" in blocker for blocker in result["blockers"])
        )

    def test_legacy_owned_directory_without_role_remains_valid(self) -> None:
        legacy_manifest = self.state / "legacy.json"
        HYGIENE.init_manifest(legacy_manifest, "legacy-run", NOW)
        resource = HYGIENE.claim_directory(
            legacy_manifest,
            "legacy-cache",
            str(self.root / "legacy-cache"),
            "legacy generic cache",
            "delete_when_released",
            NOW,
        )
        document = json.loads(legacy_manifest.read_text(encoding="utf-8"))
        for key in ("role", "scope", "task_id"):
            document["resources"][0].pop(key)
        write_json(legacy_manifest, document)
        loaded = HYGIENE.load_manifest(legacy_manifest)
        self.assertNotIn("role", loaded["resources"][0])
        self.assertEqual(loaded["resources"][0]["path"], resource["path"])


class CliTests(unittest.TestCase):
    def test_default_disk_floor_keeps_twenty_gib_and_five_percent(self) -> None:
        self.assertEqual(HYGIENE.DEFAULT_MIN_FREE_BYTES, 20 * 1024**3)
        self.assertEqual(HYGIENE.DEFAULT_MIN_FREE_PERCENT, 5.0)

    def test_disk_check_requires_both_byte_and_percent_floors(self) -> None:
        with mock.patch.object(
            HYGIENE,
            "filesystem_report",
            return_value={
                "path": "/managed",
                "filesystem_probe": "/",
                "total_bytes": 500 * 1024**3,
                "used_bytes": 476 * 1024**3,
                "free_bytes": 24 * 1024**3,
                "free_percent": 4.8,
            },
        ):
            result = HYGIENE.check_disk(
                Path("/managed"),
                HYGIENE.DEFAULT_MIN_FREE_BYTES,
                HYGIENE.DEFAULT_MIN_FREE_PERCENT,
            )
        self.assertFalse(result["healthy"])
        self.assertEqual(
            result["reasons"],
            ["free percent is below required floor 5.0"],
        )

    def test_disk_check_reserves_projected_wave_growth(self) -> None:
        with mock.patch.object(
            HYGIENE,
            "filesystem_report",
            return_value={
                "path": "/managed",
                "filesystem_probe": "/",
                "total_bytes": 500 * 1024**3,
                "used_bytes": 450 * 1024**3,
                "free_bytes": 50 * 1024**3,
                "free_percent": 10.0,
            },
        ):
            result = HYGIENE.check_disk(
                Path("/managed"),
                20 * 1024**3,
                5.0,
                30 * 1024**3,
            )
        self.assertFalse(result["healthy"])
        self.assertEqual(result["expected_growth_bytes"], 30 * 1024**3)
        self.assertEqual(result["projected_free_bytes"], 20 * 1024**3)
        self.assertEqual(result["projected_free_percent"], 4.0)
        self.assertEqual(
            result["reasons"],
            [
                "projected free percent after expected growth is below "
                "required floor 5.0"
            ],
        )

    def test_cli_cleanup_requires_execute(self) -> None:
        completed = subprocess.run(
            [
                str(SCRIPT),
                "cleanup",
                "--plan",
                "missing.json",
                "--confirm-plan-sha256",
                "0" * 64,
                "--receipt",
                "receipt.json",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        result = json.loads(completed.stdout)
        self.assertIn("requires explicit --execute", result["errors"][0])
        self.assertEqual(completed.stderr, "")

    def test_disk_check_returns_retryable_low_space_status(self) -> None:
        completed = subprocess.run(
            [
                str(SCRIPT),
                "check",
                "--path",
                str(Path.cwd()),
                "--min-free-bytes",
                str(10**18),
                "--min-free-percent",
                "100",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 75)
        self.assertFalse(json.loads(completed.stdout)["healthy"])

    def test_cli_check_applies_expected_growth(self) -> None:
        completed = subprocess.run(
            [
                str(SCRIPT),
                "check",
                "--path",
                str(Path.cwd()),
                "--expected-growth-bytes",
                str(10**18),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 75)
        result = json.loads(completed.stdout)
        self.assertEqual(result["expected_growth_bytes"], 10**18)
        self.assertEqual(result["projected_free_bytes"], 0)

    def test_cli_host_shared_gocache_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            wave_manifest = state / "wave.json"
            HYGIENE.init_manifest(wave_manifest, "host-cache-wave", NOW)
            HYGIENE.claim_directory(
                wave_manifest,
                "worker-tmp",
                str(root / "worker-tmp"),
                "worker temporary build root",
                "delete_when_released",
                NOW,
                role="go_tmp",
                scope="task",
                task_id="worker",
            )
            HYGIENE.start_run(wave_manifest, NOW + dt.timedelta(seconds=1))
            host_cache = root / "host-cache"
            initialized = subprocess.run(
                [
                    str(SCRIPT),
                    "init-host-go-cache",
                    "--path",
                    str(host_cache),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stdout)
            rendered = subprocess.run(
                [
                    str(SCRIPT),
                    "go-env",
                    "--host-gocache",
                    str(host_cache),
                    "--wave-manifest",
                    str(wave_manifest),
                    "--task-id",
                    "worker",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(rendered.returncode, 0, rendered.stdout)
            receipt = json.loads(rendered.stdout)
            self.assertEqual(
                receipt["gocache_source"],
                "operator_managed_host_shared",
            )
            self.assertNotIn("status", receipt)

    def test_cli_go_env_renders_manifest_backed_topology(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            session_manifest = state / "session.json"
            wave_manifest = state / "wave.json"
            HYGIENE.init_manifest(session_manifest, "session-cli", NOW)
            HYGIENE.init_manifest(wave_manifest, "wave-cli", NOW)
            HYGIENE.claim_directory(
                session_manifest,
                "gocache",
                str(root / "gocache"),
                "shared Go cache",
                "delete_when_released",
                NOW,
                role="go_build_cache",
                scope="session_shared",
            )
            HYGIENE.claim_directory(
                wave_manifest,
                "worker-tmp",
                str(root / "worker-tmp"),
                "worker temporary build root",
                "delete_when_released",
                NOW,
                role="go_tmp",
                scope="task",
                task_id="worker",
            )
            HYGIENE.start_run(session_manifest, NOW + dt.timedelta(seconds=1))
            HYGIENE.start_run(wave_manifest, NOW + dt.timedelta(seconds=1))
            completed = subprocess.run(
                [
                    str(SCRIPT),
                    "go-env",
                    "--session-manifest",
                    str(session_manifest),
                    "--wave-manifest",
                    str(wave_manifest),
                    "--task-id",
                    "worker",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout)
            rendered = json.loads(completed.stdout)
            self.assertEqual(rendered["kind"], "dev-disk-budget-go-environment")
            self.assertEqual(rendered["task_id"], "worker")
            self.assertEqual(rendered["inherited"], ["GOMODCACHE"])


class GuardedGoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / "state"
        self.cache = self.root / "cache"
        self.modules = self.root / "modules"
        self.cwd = self.root / "repo"
        for path in (self.cache, self.modules, self.cwd):
            path.mkdir()
        guarded_go.configure(self.state, self.cache, self.modules, 0, 0)
        self.fake_go = self.root / "go"
        self.fake_go.write_text(
            "#!" + sys.executable + "\n"
            "import json,os,sys\n"
            "result={k:os.environ.get(k) for k in ('GOCACHE','GOMODCACHE','GOTMPDIR','GOTOOLCHAIN','GOFLAGS')}\n"
            "result['ARGS']=sys.argv[1:]\n"
            "print(json.dumps(result))\n"
            "sys.exit(7 if 'fail' in sys.argv else 0)\n"
        )
        self.fake_go.chmod(0o700)
        self.shim = SCRIPT.parent / "ai-runtime-bin" / "go"

    def reserve(self, growth=6):
        return guarded_go.reserve(self.state, "session-a", self.cwd, growth)

    def test_binding_preserves_existing_cache_and_is_idempotent(self):
        (self.cache / "keep").write_text("cached")
        profile = guarded_go.configure(self.state, self.cache, self.modules, 0, 0)
        self.assertEqual((self.cache / "keep").read_text(), "cached")
        self.assertFalse((self.cache / HYGIENE.MARKER_NAME).exists())
        self.assertEqual(profile["growth_defaults_bytes"], guarded_go.DEFAULT_GROWTH_BYTES)
        with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "different settings"):
            guarded_go.configure(self.state, self.cache, self.modules, 1, 0)

    def test_dedicated_state_root_precedes_xdg_and_legacy_default_remains(self):
        with mock.patch.dict(os.environ, {
            "DEV_DISK_BUDGET_STATE_ROOT": str(self.root / "shared-state"),
            "XDG_STATE_HOME": str(self.root / "xdg-state"),
        }):
            self.assertEqual(guarded_go.default_state(), self.root / "shared-state")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg-state")}, clear=True):
            self.assertEqual(guarded_go.default_state(), self.root / "xdg-state" / "dev-disk-budget")

    def test_command_defaults_are_deterministic_and_full_or_race_tests_reserve_more(self):
        profile, _ = guarded_go.load_state(self.state)
        self.assertEqual(guarded_go.default_growth_for(profile, ["version"]), 512 * guarded_go.MIB)
        self.assertEqual(guarded_go.default_growth_for(profile, ["test", "./pkg"]), 12 * guarded_go.GIB)
        self.assertEqual(guarded_go.default_growth_for(profile, ["test", "./..."]), 24 * guarded_go.GIB)
        self.assertEqual(guarded_go.default_growth_for(profile, ["test", "..."]), 24 * guarded_go.GIB)
        self.assertEqual(guarded_go.default_growth_for(profile, ["test", "-race", "./..."]), 32 * guarded_go.GIB)

    def test_legacy_profile_derives_defaults_without_migration(self):
        profile = json.loads((self.state / "config.json").read_text())
        profile.pop("growth_defaults_bytes")
        guarded_go.hygiene.atomic_write_json(self.state / "config.json", profile)
        loaded, _ = guarded_go.load_state(self.state)
        self.assertNotIn("growth_defaults_bytes", loaded)
        self.assertEqual(
            guarded_go.default_growth_for(loaded, ["test", "./..."]),
            guarded_go.DEFAULT_GROWTH_BYTES["test_full"],
        )
        self.assertNotIn(
            "growth_defaults_bytes", guarded_go.configure(self.state, self.cache, self.modules, 0, 0)
        )

    def test_cross_session_reservations_cannot_spend_same_headroom(self):
        usage = type("Usage", (), {"total": 100, "used": 90, "free": 10})()
        with mock.patch.object(guarded_go.hygiene.shutil, "disk_usage", return_value=usage):
            _, record = self.reserve()
            with self.assertRaises(BlockingIOError):
                guarded_go.reserve(self.state, "session-b", self.cwd, 6)
            self.assertEqual(guarded_go.budget_status(self.state)["reserved_bytes"], 6)
            guarded_go.finish_budget(self.state, record, {"status": "completed"})
            _, second = guarded_go.reserve(self.state, "session-b", self.cwd, 6)
            self.assertNotEqual(record["id"], second["id"])

    def test_concurrent_process_admission_is_atomic(self):
        worker = (
            "import sys,pathlib,json;sys.path.insert(0,sys.argv[1]);import guarded_go as g;"
            "g.hygiene.shutil.disk_usage=lambda p:type('U',(),{'total':100,'used':90,'free':10})();"
            "\ntry:g.reserve(pathlib.Path(sys.argv[2]),sys.argv[3],pathlib.Path(sys.argv[4]),6);print('admitted')"
            "\nexcept BlockingIOError:print('blocked')"
        )
        processes = [subprocess.Popen(
            [sys.executable, "-c", worker, str(SCRIPT.parent), str(self.state), name, str(self.cwd)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) for name in ("session-a", "session-b")]
        output = [p.communicate(timeout=10) for p in processes]
        self.assertEqual(sorted(text.strip() for text, _ in output), ["admitted", "blocked"], output)
        self.assertTrue(all(p.returncode == 0 for p in processes), output)

    def test_corrupt_ledger_and_replaced_cache_fail_closed(self):
        (self.state / "budget.json").write_text('{"schema_version":1,"reservations":{"bad":{}}}')
        with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "reservation"):
            self.reserve()
        guarded_go.hygiene.atomic_write_json(self.state / "budget.json", {"schema_version": 1, "reservations": {}})
        self.cache.rename(self.root / "old-cache")
        self.cache.mkdir()
        with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "identity changed"):
            self.reserve()

    def test_live_owner_or_unknown_launch_cannot_be_reconciled(self):
        _, record = self.reserve()
        digest = guarded_go.record_hash(record)
        with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "owner_alive"):
            guarded_go.reconcile(self.state, record["id"], digest)
        reused = dict(record["owner_identity"], start_token=record["owner_identity"]["start_token"] + "-other")
        with mock.patch.object(guarded_go, "capture_process_identity", return_value=reused):
            with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "launch_outcome_unknown"):
                guarded_go.reconcile(self.state, record["id"], digest)

    def test_linux_identity_distinguishes_dead_mismatch_and_unknown(self):
        expected = {
            "schema_version": 1, "pid": 900041, "uid": 1000, "platform": "linux",
            "boot_id": "boot-a", "start_token": "100", "pid_namespace": "pid:[77]",
            "process_group_id": 900041,
        }
        observer = dict(expected, pid=os.getpid(), process_group_id=os.getpgrp())
        observed = dict(expected, start_token="200")

        def same_namespace(pid):
            return observer if pid == os.getpid() else observed

        with mock.patch.object(guarded_go, "capture_process_identity", side_effect=same_namespace), \
                mock.patch.object(guarded_go, "process_group_present", return_value=False):
            self.assertEqual(guarded_go.inspect_process(expected)["status"], "identity_mismatch")
            self.assertEqual(guarded_go.inspect_process(expected, group=True)["status"], "identity_mismatch")

        def missing_target(pid):
            if pid == os.getpid():
                return observer
            raise ProcessLookupError(pid)

        with mock.patch.object(
            guarded_go, "capture_process_identity", side_effect=missing_target,
        ), \
                mock.patch.object(guarded_go, "process_group_present", return_value=False):
            self.assertEqual(guarded_go.inspect_process(expected, group=True)["status"], "dead")
        with mock.patch.object(guarded_go, "capture_process_identity", side_effect=same_namespace), \
                mock.patch.object(guarded_go, "process_group_present", return_value=True):
            self.assertEqual(guarded_go.inspect_process(expected, group=True)["status"], "unknown")

    def cross_namespace_record(self):
        _, record = self.reserve()
        namespace_a = {
            "schema_version": 1, "pid": record["owner_pid"], "uid": os.getuid(),
            "platform": "linux", "boot_id": "boot-a", "start_token": "100",
            "pid_namespace": "pid:[100]", "process_group_id": record["owner_pid"],
        }
        child = dict(namespace_a, pid=900042, process_group_id=900042, start_token="101")
        record.update(owner_identity=namespace_a, child_pid=900042,
                      child_identity=child, phase="outcome_unknown")
        ledger = json.loads((self.state / "budget.json").read_text())
        ledger["reservations"][record["id"]] = record
        guarded_go.hygiene.atomic_write_json(self.state / "budget.json", ledger)
        observer = dict(namespace_a, pid=os.getpid(), process_group_id=os.getpgrp(),
                        pid_namespace="pid:[200]")
        return record, observer

    def fake_proc(self, processes, observer_nspid="4242", init=True):
        """Build a /proc stand-in: (pid, ns/pid link, NSpid[, uid]) per process."""
        proc = Path(tempfile.mkdtemp(dir=self.root))
        (proc / "self").mkdir()
        (proc / "self/status").write_text(f"Name:\tpython3\nNSpid:\t{observer_nspid}\n")
        for pid, namespace, nspid, *uid in ((("1", "pid:[200]", "1", 0),) if init else ()) + tuple(processes):
            uid = uid[0] if uid else os.getuid()
            (proc / pid / "ns").mkdir(parents=True)
            os.symlink(namespace, proc / pid / "ns/pid")
            (proc / pid / "status").write_text(
                f"Name:\tproc\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nNSpid:\t{nspid}\n"
            )
        return proc

    @contextlib.contextmanager
    def cross_namespace_status(self, observer, proc, readlink_errors=None):
        real_readlink = os.readlink
        errors = readlink_errors or {}

        def readlink(path):
            error = errors.get(Path(path).parent.parent.name)
            if error is not None:
                raise error
            return real_readlink(path)

        with mock.patch.object(guarded_go, "capture_process_identity", return_value=observer), \
                mock.patch.object(guarded_go, "PROC_ROOT", proc), \
                mock.patch.object(guarded_go.os, "readlink", side_effect=readlink), \
                mock.patch.object(guarded_go, "process_group_present") as group_probe:
            yield guarded_go.budget_status(self.state)["reservations"][0]
            group_probe.assert_not_called()

    def assert_cross_namespace_unknown(self, record, rendered):
        self.assertEqual(rendered["owner_liveness"], {
            "status": "unknown", "reason": "pid_namespace_not_comparable",
        })
        self.assertEqual(rendered["child_liveness"]["status"], "unknown")
        self.assertEqual(rendered["reconcile_class"], "owner_unknown")
        with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "owner_unknown"):
            guarded_go.reconcile(self.state, record["id"], rendered["record_sha256"])

    def test_cross_pid_namespace_is_unknown_and_not_reconcilable(self):
        record, observer = self.cross_namespace_record()
        proc = self.fake_proc([("4242", "pid:[200]", "4242"), ("5000", "pid:[100]", "5000\t7")])
        with self.cross_namespace_status(observer, proc) as rendered:
            self.assert_cross_namespace_unknown(record, rendered)

    def test_gone_pid_namespace_is_dead_and_reconcilable(self):
        record, observer = self.cross_namespace_record()
        tmp = self.state / "tmp" / record["id"]
        tmp.mkdir()
        (tmp / "evidence").write_text("keep")
        proc = self.fake_proc([
            ("4242", "pid:[200]", "4242"), ("5001", "pid:[300]", "5001\t3"),
            ("6000", "pid:[200]", "6000"), ("7000", "pid:[400]", "7000\t1", 0),
        ])
        # Unreadable links are excluded only for host-level or other-uid processes.
        unreadable = {"6000": PermissionError(13, "denied"), "7000": PermissionError(13, "denied")}
        with self.cross_namespace_status(observer, proc, unreadable) as rendered:
            gone = {"status": "dead", "reason": "pid_namespace_gone"}
            self.assertEqual(rendered["owner_liveness"], gone)
            self.assertEqual(rendered["child_liveness"], gone)
            self.assertEqual(rendered["reconcile_class"], "eligible")
            with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "digest"):
                guarded_go.reconcile(self.state, record["id"], "0" * 64)
            result = guarded_go.reconcile(self.state, record["id"], rendered["record_sha256"])
            self.assertEqual(result["status"], "budget_reconciled")
        self.assertEqual(guarded_go.budget_status(self.state)["reserved_bytes"], 0)
        self.assertEqual((tmp / "evidence").read_text(), "keep")

    def test_unreadable_pid_namespace_link_is_unknown(self):
        record, observer = self.cross_namespace_record()
        proc = self.fake_proc([("4242", "pid:[200]", "4242"), ("5000", "pid:[300]", "5000\t7")])
        same_uid_nested = {"5000": PermissionError(13, "denied")}
        with self.cross_namespace_status(observer, proc, same_uid_nested) as rendered:
            self.assert_cross_namespace_unknown(record, rendered)
        (proc / "5000/status").write_text("Name:\tproc\nNSpid:\t5000\t7\n")
        with self.cross_namespace_status(observer, proc, same_uid_nested) as rendered:
            self.assert_cross_namespace_unknown(record, rendered)
        io_error = {"4242": OSError(5, "I/O error")}
        with self.cross_namespace_status(observer, proc, io_error) as rendered:
            self.assert_cross_namespace_unknown(record, rendered)
        vanished = {"5000": FileNotFoundError(2, "gone")}
        with self.cross_namespace_status(observer, proc, vanished) as rendered:
            self.assertEqual(rendered["reconcile_class"], "eligible")

    def test_non_root_observer_namespace_is_unknown(self):
        record, observer = self.cross_namespace_record()
        proc = self.fake_proc([("4242", "pid:[200]", "4242")], observer_nspid="4242\t9")
        with self.cross_namespace_status(observer, proc) as rendered:
            self.assert_cross_namespace_unknown(record, rendered)
        hidden = self.fake_proc([("4242", "pid:[200]", "4242")], init=False)
        with self.cross_namespace_status(observer, hidden) as rendered:
            self.assert_cross_namespace_unknown(record, rendered)

    def test_empty_legacy_ledger_allows_same_root_migration(self):
        before = (self.state / "budget.json").read_bytes()
        completed = subprocess.run([
            sys.executable, str(SCRIPT.with_name("guarded_go.py")),
            "--state-root", str(self.state), "migration-preflight",
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["decision"], "same_root_allowed")
        self.assertEqual(result["selected_state_root"], str(self.state))
        self.assertEqual(result["legacy_reservation_count"], 0)
        self.assertFalse(result["mutation_authorized"])
        self.assertEqual((self.state / "budget.json").read_bytes(), before)

    def test_legacy_reservation_requires_fresh_state_root_without_mutation(self):
        _, record = self.reserve()
        ledger = json.loads((self.state / "budget.json").read_text())
        legacy = ledger["reservations"][record["id"]]
        legacy.pop("owner_identity")
        guarded_go.hygiene.atomic_write_json(self.state / "budget.json", ledger)
        status = guarded_go.budget_status(self.state)
        rendered = status["reservations"][0]
        self.assertEqual(rendered["reconcile_class"], "legacy_identity_missing")
        self.assertEqual(rendered["owner_liveness"]["reason"], "legacy_identity_missing")
        self.assertEqual(rendered["record_sha256"], guarded_go.record_hash(legacy))
        with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "legacy reservation"):
            guarded_go.reconcile(self.state, record["id"], rendered["record_sha256"])
        retained_tmp = self.state / "tmp" / record["id"]
        retained_tmp.mkdir()
        (retained_tmp / "evidence").write_text("keep")
        budget_before = (self.state / "budget.json").read_bytes()
        blocked = subprocess.run([
            sys.executable, str(SCRIPT.with_name("guarded_go.py")),
            "--state-root", str(self.state), "migration-preflight",
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(blocked.returncode, 75, blocked.stderr)
        blocked_result = json.loads(blocked.stdout)
        self.assertEqual(blocked_result["status"], "blocked")
        self.assertEqual(blocked_result["decision"], "blocked_requires_external_drain")
        self.assertFalse(blocked_result["legacy_reservations"][0]["child_pid_recorded"])
        fresh_state = self.root / "fresh-state-v2"
        incomplete = subprocess.run([
            sys.executable, str(SCRIPT.with_name("guarded_go.py")),
            "--state-root", str(self.state), "migration-preflight",
            "--new-state-root", str(fresh_state),
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(incomplete.returncode, 2, incomplete.stderr)
        self.assertIn("requires new state root, new gocache, gomodcache, and new temp root", incomplete.stderr)
        new_cache = self.root / "fresh-go-cache"
        new_cache.mkdir()
        new_temp = self.root / "fresh-go-tmp"
        checked = subprocess.run([
            sys.executable, str(SCRIPT.with_name("guarded_go.py")),
            "--state-root", str(self.state), "migration-preflight",
            "--new-state-root", str(fresh_state),
            "--new-gocache", str(new_cache),
            "--gomodcache", str(self.modules),
            "--new-temp-root", str(new_temp),
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(checked.returncode, 75, checked.stderr)
        checked_result = json.loads(checked.stdout)
        self.assertEqual(checked_result["decision"], "blocked_requires_external_drain")
        self.assertEqual(
            checked_result["prospective_configuration"]["status"],
            "valid_but_not_authorized",
        )
        self.assertEqual(checked_result["selected_state_root"], None)
        self.assertFalse(fresh_state.exists())
        self.assertFalse(new_temp.exists())
        self.assertEqual((self.state / "budget.json").read_bytes(), budget_before)
        self.assertEqual((retained_tmp / "evidence").read_text(), "keep")
        self.assertEqual(guarded_go.budget_status(self.state)["reserved_bytes"], 6)

    def test_migration_preflight_rejects_unsafe_prospective_configuration(self):
        _, record = self.reserve()
        ledger = json.loads((self.state / "budget.json").read_text())
        ledger["reservations"][record["id"]].pop("owner_identity")
        guarded_go.hygiene.atomic_write_json(self.state / "budget.json", ledger)
        profile, _ = guarded_go.load_state(self.state)
        new_state = self.root / "prospective-state"
        new_cache = self.root / "prospective-cache"
        new_cache.mkdir()
        new_temp = self.root / "prospective-tmp"

        with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "distinct from the old gocache"):
            guarded_go.migration_preflight(
                self.state, new_state, self.cache, self.modules, new_temp
            )

        new_state.mkdir()
        with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "state root must not already exist"):
            guarded_go.migration_preflight(
                self.state, new_state, new_cache, self.modules, new_temp
            )
        new_state.rmdir()

        new_temp.mkdir()
        with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "temporary root must not already exist"):
            guarded_go.migration_preflight(
                self.state, new_state, new_cache, self.modules, new_temp
            )
        new_temp.rmdir()

        git_parent = self.root / "git-parent"
        (git_parent / ".git").mkdir(parents=True)
        (git_parent / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "Git worktree"):
            guarded_go.migration_preflight(
                self.state, git_parent / "state", new_cache, self.modules, new_temp
            )

        owned_parent = self.root / "owned-parent"
        owned_parent.mkdir()
        (owned_parent / guarded_go.hygiene.MARKER_NAME).write_text("{}")
        with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "owned root"):
            guarded_go.migration_preflight(
                self.state, owned_parent / "state", new_cache, self.modules, new_temp
            )

        bad_cache = self.root / "not-a-cache-directory"
        bad_cache.write_text("not a directory")
        with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "directory must belong"):
            guarded_go.migration_preflight(
                self.state, new_state, bad_cache, self.modules, new_temp
            )

        with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "must not overlap"):
            guarded_go.migration_preflight(
                self.state, new_state, new_cache, self.modules, new_state / "tmp"
            )

        original_parent_identity = guarded_go.existing_parent_identity

        def different_device(path):
            identity = original_parent_identity(path)
            if path == new_temp:
                identity["device"] += 1
            return identity

        with mock.patch.object(guarded_go, "existing_parent_identity", side_effect=different_device):
            with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "same filesystem"):
                guarded_go.migration_preflight(
                    self.state, new_state, new_cache, self.modules, new_temp
                )
        self.assertEqual(profile["gocache"], str(self.cache))

    def test_reconcile_only_releases_dead_command_budget(self):
        _, record = self.reserve()
        child_identity = dict(record["owner_identity"], pid=987654, process_group_id=987654)
        guarded_go.update_reservation(
            self.state, record["id"], child_pid=987654,
            child_identity=child_identity, phase="outcome_unknown",
        )
        record.update(child_pid=987654, child_identity=child_identity, phase="outcome_unknown")
        tmp = self.state / "tmp" / record["id"]
        tmp.mkdir()
        (tmp / "evidence").write_text("keep")
        identities = {record["owner_pid"]: record["owner_identity"], 987654: child_identity}

        def reused(pid):
            return dict(identities[pid], start_token=identities[pid]["start_token"] + "-reused")

        with mock.patch.object(guarded_go, "capture_process_identity", side_effect=reused), \
                mock.patch.object(guarded_go, "process_group_present", return_value=False):
            with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "digest"):
                guarded_go.reconcile(self.state, record["id"], "0" * 64)
            status = guarded_go.budget_status(self.state)
            self.assertNotIn("profile", status)
            self.assertNotIn("owner_identity", status["reservations"][0])
            self.assertEqual(status["summary"]["owner_liveness"]["identity_mismatch"], 1)
            self.assertEqual(status["summary"]["child_liveness"]["identity_mismatch"], 1)
            self.assertEqual(status["summary"]["reconcile_class"], {"eligible": 1})
            digest = status["reservations"][0]["record_sha256"]
            guarded_go.reconcile(self.state, record["id"], digest)
        self.assertEqual(guarded_go.budget_status(self.state)["reserved_bytes"], 0)
        self.assertEqual((tmp / "evidence").read_text(), "keep")

    def test_launch_failure_releases_reservation_without_cache_changes(self):
        with mock.patch.object(guarded_go.subprocess, "Popen", side_effect=OSError("launch failed")):
            with self.assertRaisesRegex(OSError, "launch failed"):
                guarded_go.run_go(self.state, self.cwd, "session-a", 1, str(self.fake_go), ["version"])
        self.assertEqual(guarded_go.budget_status(self.state)["reserved_bytes"], 0)
        self.assertTrue(self.cache.is_dir())

    def test_success_cleans_only_owned_temporary_data(self):
        (self.cache / "keep").write_text("cached")
        with mock.patch.object(guarded_go.hygiene, "active_handle_check", return_value=(False, "fixture")):
            code = guarded_go.run_go(self.state, self.cwd, "session-a", 1, str(self.fake_go), ["version"])
        self.assertEqual(code, 0)
        self.assertEqual(guarded_go.budget_status(self.state)["reserved_bytes"], 0)
        self.assertEqual(list((self.state / "tmp").iterdir()), [])
        self.assertEqual((self.cache / "keep").read_text(), "cached")
        launch = json.loads(next((self.state / "runs").glob("*/launch.json")).read_text())
        self.assertEqual(launch["environment"]["GOCACHE"], str(self.cache))
        self.assertEqual(launch["environment"]["GOMODCACHE"], str(self.modules))

    def test_failed_command_preserves_tmp_and_exit_status(self):
        code = guarded_go.run_go(self.state, self.cwd, "session-a", 1, str(self.fake_go), ["test", "fail"])
        self.assertEqual(code, 7)
        self.assertEqual(guarded_go.budget_status(self.state)["reserved_bytes"], 0)
        self.assertEqual(len(list((self.state / "tmp").iterdir())), 1)

    def test_cli_runs_without_shell_and_reports_low_space_as_75(self):
        command = [sys.executable, str(SCRIPT.with_name("guarded_go.py")),
                   "--state-root", str(self.state), "run", "--session-id", "cli-test",
                   "--cwd", str(self.cwd), "--go-binary", str(self.fake_go),
                   "--expected-growth-bytes", "1", "--", "env", "GOCACHE"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["GOCACHE"], str(self.cache))
        launch = json.loads(next((self.state / "runs").glob("*/launch.json")).read_text())
        self.assertEqual(launch["reservation"]["expected_growth_bytes"], 1)
        self.assertEqual(launch["reservation"]["session_id"], "cli-test")
        command[command.index("--expected-growth-bytes") + 1] = str(10**18)
        refused = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertEqual(refused.stdout, "")
        self.assertEqual(guarded_go.budget_status(self.state)["reserved_bytes"], 0)

    def test_simple_cli_derives_session_and_reuses_cache_with_distinct_tmpdirs(self):
        command = [sys.executable, str(SCRIPT.with_name("guarded_go.py")),
                   "run", "--cwd", str(self.cwd), "--", "version"]
        environment = dict(os.environ)
        environment["DEV_DISK_BUDGET_STATE_ROOT"] = str(self.state)
        environment["XDG_STATE_HOME"] = str(self.root / "different-xdg")
        environment["PATH"] = str(self.root) + os.pathsep + environment.get("PATH", "")
        results = [subprocess.run(command, capture_output=True, text=True, timeout=40, env=environment)
                   for _ in range(2)]
        self.assertTrue(all(result.returncode == 0 for result in results), results)
        environments = [json.loads(result.stdout) for result in results]
        self.assertEqual({item["GOCACHE"] for item in environments}, {str(self.cache)})
        self.assertEqual(len({item["GOTMPDIR"] for item in environments}), 2)
        launches = [json.loads(path.read_text()) for path in sorted((self.state / "runs").glob("*/launch.json"))]
        self.assertEqual(len(launches), 2)
        for launch in launches:
            reservation = launch["reservation"]
            self.assertRegex(reservation["session_id"], r"^repo-version-[0-9a-f]{12}$")
            self.assertEqual(reservation["expected_growth_bytes"], guarded_go.DEFAULT_GROWTH_BYTES["version"])

    def test_ai_runtime_shim_routes_commands_and_read_only_queries(self):
        environment = dict(os.environ)
        environment.update({
            "DEV_DISK_BUDGET_STATE_ROOT": str(self.state),
            "DEV_DISK_BUDGET_REAL_GO": str(self.fake_go),
            "DEV_DISK_BUDGET_EXPECTED_GROWTH_BYTES": "1",
            "DEV_DISK_BUDGET_SESSION_ID": "shim-test",
        })
        persisted_go_env = self.root / "go.env"
        persisted_go_env.write_text("GOFLAGS=-w\n")
        environment["GOENV"] = str(persisted_go_env)
        environment.pop("GOFLAGS", None)
        for subcommand in ("build", "test", "vet", "generate", "list", "run"):
            with self.subTest(subcommand=subcommand):
                completed = subprocess.run(
                    [str(self.shim), "-C", str(self.cwd), subcommand, "./pkg"],
                    capture_output=True, text=True, timeout=40, env=environment,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                rendered = json.loads(completed.stdout)
                self.assertEqual(rendered["ARGS"], [subcommand, "./pkg"])
                self.assertEqual(rendered["GOCACHE"], str(self.cache))
        guarded_runs = len(list((self.state / "runs").iterdir()))
        for read_only_args in (["version"], ["env", "GOCACHE"]):
            read_only = subprocess.run(
                [str(self.shim), *read_only_args], capture_output=True,
                text=True, timeout=40, env=environment,
            )
            self.assertEqual(read_only.returncode, 0, read_only.stderr)
            self.assertEqual(json.loads(read_only.stdout)["ARGS"], read_only_args)
            self.assertEqual(json.loads(read_only.stdout)["GOTOOLCHAIN"], "local")
            self.assertEqual(json.loads(read_only.stdout)["GOFLAGS"], "")
        self.assertEqual(len(list((self.state / "runs").iterdir())), guarded_runs)

    def test_ai_runtime_shim_rejects_mutation_recursion_and_missing_real_go(self):
        base = dict(os.environ, DEV_DISK_BUDGET_STATE_ROOT=str(self.state))
        base.pop("DEV_DISK_BUDGET_REAL_GO", None)
        before = len(list((self.state / "runs").iterdir()))
        mutation_env = dict(base, DEV_DISK_BUDGET_REAL_GO=str(self.fake_go))
        for mutation_flag in ("-w", "--w", "-u", "--u", "-w=true", "--u=true"):
            mutation = subprocess.run(
                [str(self.shim), "env", mutation_flag, "GOFLAGS=-mod=mod"],
                capture_output=True, text=True, timeout=10, env=mutation_env,
            )
            self.assertEqual(mutation.returncode, 2)
            self.assertIn("env mutation", mutation.stderr)
            inherited_mutation = subprocess.run(
                [str(self.shim), "env", "GOFLAGS"], capture_output=True,
                text=True, timeout=10, env=dict(mutation_env, GOFLAGS=mutation_flag),
            )
            self.assertEqual(inherited_mutation.returncode, 2)
            self.assertIn("GOFLAGS", inherited_mutation.stderr)
        recursive = subprocess.run(
            [str(self.shim), "version"], capture_output=True, text=True, timeout=10,
            env=dict(base, DEV_DISK_BUDGET_REAL_GO=str(self.shim)),
        )
        self.assertEqual(recursive.returncode, 2)
        self.assertIn("resolves to the shim", recursive.stderr)
        missing = subprocess.run(
            [str(self.shim), "version"], capture_output=True, text=True, timeout=10, env=base,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("must name an absolute executable", missing.stderr)
        self.assertEqual(len(list((self.state / "runs").iterdir())), before)

    def test_ai_runtime_shim_nested_go_execs_real_binary_without_reserving_again(self):
        nested_go = self.root / "nested-real-go"
        nested_go.write_text(
            "#!" + sys.executable + "\n"
            "import json,os,subprocess,sys\n"
            "if sys.argv[1:] == ['run', 'nested']:\n"
            " result=subprocess.run([os.environ['NESTED_GO_SHIM'],'version'],"
            "capture_output=True,text=True,env=os.environ)\n"
            " print(result.stdout,end='')\n"
            " sys.exit(result.returncode)\n"
            "print(json.dumps({'ARGS':sys.argv[1:],'ACTIVE':os.environ.get('DEV_DISK_BUDGET_ACTIVE_RUN'),"
            "'GOTMPDIR':os.environ.get('GOTMPDIR')}))\n"
        )
        nested_go.chmod(0o700)
        environment = dict(os.environ, **{
            "DEV_DISK_BUDGET_STATE_ROOT": str(self.state),
            "DEV_DISK_BUDGET_REAL_GO": str(nested_go),
            "DEV_DISK_BUDGET_EXPECTED_GROWTH_BYTES": "1",
            "DEV_DISK_BUDGET_SESSION_ID": "nested-shim-test",
            "NESTED_GO_SHIM": str(self.shim),
        })
        completed = subprocess.run(
            [str(self.shim), "-C", str(self.cwd), "run", "nested"],
            capture_output=True, text=True, timeout=40, env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        rendered = json.loads(completed.stdout)
        self.assertEqual(rendered["ARGS"], ["version"])
        self.assertTrue(rendered["ACTIVE"])
        self.assertTrue(rendered["GOTMPDIR"])
        self.assertEqual(len(list((self.state / "runs").glob("*/launch.json"))), 1)

    def test_signal_relay_never_forwards_before_launch_or_after_child_exit(self):
        relay = guarded_go.SignalRelay()
        with mock.patch.object(guarded_go.os, "killpg") as kill_group:
            relay(signal.SIGTERM, None)
            kill_group.assert_not_called()
        self.assertEqual(relay.pending, [signal.SIGTERM])

        relay.pending.clear()
        relay.process = mock.Mock()
        relay.process.poll.return_value = 0
        relay.child_identity = {
            "schema_version": 1, "pid": 71, "uid": os.getuid(),
            "platform": "darwin", "start_token": "1.1", "process_group_id": 71,
        }
        with mock.patch.object(guarded_go, "inspect_process") as inspect, \
                mock.patch.object(guarded_go.os, "killpg") as kill_group:
            relay(signal.SIGTERM, None)
            inspect.assert_not_called()
            kill_group.assert_not_called()
        self.assertEqual(relay.pending, [signal.SIGTERM])

    def test_prelaunch_signal_does_not_spawn_child_and_releases_budget(self):
        original_start = guarded_go.hygiene.start_run

        def interrupt_after_start(*arguments, **kwargs):
            result = original_start(*arguments, **kwargs)
            os.kill(os.getpid(), signal.SIGTERM)
            return result

        with mock.patch.object(guarded_go.hygiene, "start_run", side_effect=interrupt_after_start), \
                mock.patch.object(guarded_go.subprocess, "Popen") as popen:
            code = guarded_go.run_go(
                self.state, self.cwd, "prelaunch-signal", 1, str(self.fake_go), ["version"]
            )
            popen.assert_not_called()
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertEqual(guarded_go.budget_status(self.state)["reserved_bytes"], 0)
        self.assertEqual(len(list((self.state / "tmp").iterdir())), 1)

    def test_run_restores_prior_signal_handler_before_post_child_cleanup(self):
        received = []
        previous = signal.signal(signal.SIGTERM, lambda signum, _frame: received.append(signum))
        original_finish = guarded_go.hygiene.finish_run

        def signal_during_finish(*arguments, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            return original_finish(*arguments, **kwargs)

        try:
            with mock.patch.object(guarded_go.hygiene, "finish_run", side_effect=signal_during_finish), \
                    mock.patch.object(guarded_go.hygiene, "active_handle_check", return_value=(False, "fixture")):
                code = guarded_go.run_go(
                    self.state, self.cwd, "post-exit-signal", 1, str(self.fake_go), ["version"]
                )
        finally:
            signal.signal(signal.SIGTERM, previous)
        self.assertEqual(code, 0)
        self.assertEqual(received, [signal.SIGTERM])
        self.assertEqual(guarded_go.budget_status(self.state)["reserved_bytes"], 0)

    def test_interruption_forwards_signal_releases_budget_and_retains_tmp(self):
        sleeper = self.root / "sleep-go"
        ready = self.root / "sleeper-ready"
        sleeper.write_text(
            "#!" + sys.executable + " -I\n"
            "import signal\n"
            "open(%r, 'w').close()\n"
            "signal.pause()\n" % str(ready)
        )
        sleeper.chmod(0o700)
        command = [
            sys.executable, str(SCRIPT.with_name("guarded_go.py")),
            "--state-root", str(self.state), "run", "--session-id", "interrupt-test",
            "--cwd", str(self.cwd), "--go-binary", str(sleeper),
            "--expected-growth-bytes", "1", "--", "test", "./pkg",
        ]
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        started = json.loads(process.stderr.readline())
        self.assertEqual(started["guarded_go"], "started")
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)  # signal only once the fake go has finished starting
        os.kill(process.pid, signal.SIGINT)
        _, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 130, stderr)
        self.assertEqual(guarded_go.budget_status(self.state)["reserved_bytes"], 0)
        self.assertTrue(Path(started["gotmpdir"]).is_dir())

    def test_lingering_descendant_retains_budget_and_tmp(self):
        with mock.patch.object(guarded_go, "inspect_process",
                               return_value={"status": "alive", "reason": "fixture"}):
            code = guarded_go.run_go(self.state, self.cwd, "session-a", 1, str(self.fake_go), ["version"])
        self.assertEqual(code, 75)
        status = guarded_go.budget_status(self.state)
        self.assertEqual(status["reserved_bytes"], 1)
        self.assertEqual(status["reservations"][0]["phase"], "outcome_unknown")
        self.assertEqual(len(list((self.state / "tmp").iterdir())), 1)

    def test_no_time_based_release_or_nested_runner(self):
        _, record = self.reserve()
        guarded_go.update_reservation(self.state, record["id"], created_at="2000-01-01T00:00:00Z")
        with mock.patch.object(guarded_go, "inspect_process",
                               return_value={"status": "dead", "reason": "fixture"}):
            self.assertEqual(guarded_go.budget_status(self.state)["reserved_bytes"], 6)
        with mock.patch.dict(os.environ, {"DEV_DISK_BUDGET_ACTIVE_RUN": "existing"}):
            with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "nested"):
                guarded_go.run_go(self.state, self.cwd, "session-a", 1, str(self.fake_go), ["version"])

    def test_symlink_lock_and_config_are_rejected(self):
        lock = self.state / ".budget.lock"
        lock.unlink()
        foreign = self.root / "foreign"
        foreign.write_text("preserve")
        lock.symlink_to(foreign)
        with self.assertRaises(OSError):
            self.reserve()
        self.assertEqual(foreign.read_text(), "preserve")
        lock.unlink()
        config = self.state / "config.json"
        config.unlink()
        config.symlink_to(foreign)
        with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "private"):
            self.reserve()
        self.assertEqual(foreign.read_text(), "preserve")

    def test_sandbox_placeholder_git_is_not_a_worktree_boundary(self):
        # bubblewrap-based agent sandboxes mount an empty .git at each writable root
        root = self.root / "sandbox-root"
        (root / ".git").mkdir(parents=True)
        cache, modules = self.root / "pcache", self.root / "pmodules"
        cache.mkdir()
        modules.mkdir()
        profile = guarded_go.configure(root / "state", cache, modules, 0, 0, root / "go-tmp")
        self.assertEqual(profile["temp_root"], str(root / "go-tmp"))
        with mock.patch.object(guarded_go.hygiene, "active_handle_check", return_value=(False, "fixture")):
            code = guarded_go.run_go(root / "state", self.cwd, "placeholder", 1, str(self.fake_go), ["version"])
        self.assertEqual(code, 0)
        for marker, blocks in ((None, False), ("HEAD", True)):
            with self.subTest(marker=marker):
                if marker:
                    (root / ".git" / marker).write_text("ref: refs/heads/main\n", encoding="utf-8")
                check = lambda: guarded_go.hygiene.reject_ancestor_ownership_boundaries(root / "x" / "y")
                if blocks:
                    with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "Git worktree"):
                        check()
                else:
                    check()
        gitfile = self.root / "linked"
        gitfile.mkdir()
        (gitfile / ".git").write_text("", encoding="utf-8")
        guarded_go.hygiene.reject_ancestor_ownership_boundaries(gitfile / "a")
        (gitfile / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
        with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "Git worktree"):
            guarded_go.hygiene.reject_ancestor_ownership_boundaries(gitfile / "a")

    def test_home_repository_cache_binding_uses_external_temporary_root(self):
        home = self.root / "home"
        home.mkdir()
        (home / ".git").mkdir()
        cache, modules = home / "cache", home / "modules"
        cache.mkdir()
        modules.mkdir()
        state = home / "state"
        temporary_root = self.root / "external-tmp"
        profile = guarded_go.configure(state, cache, modules, 0, 0, temporary_root)
        self.assertEqual(profile["temp_root"], str(temporary_root))
        with mock.patch.object(guarded_go.hygiene, "active_handle_check", return_value=(False, "fixture")):
            code = guarded_go.run_go(state, home, "home-repo", 1, str(self.fake_go), ["version"])
        self.assertEqual(code, 0)
        self.assertEqual(list(temporary_root.iterdir()), [])
        self.assertTrue((home / ".git").is_dir())
        temporary_root.rename(self.root / "old-tmp")
        temporary_root.mkdir()
        with self.assertRaisesRegex(guarded_go.hygiene.HygieneError, "identity changed"):
            guarded_go.budget_status(state)

    def test_gc_and_environment_mutation_are_rejected(self):
        for command in (["clean", "-cache"], ["env", "-w", "GOCACHE=elsewhere"]):
            with self.subTest(command=command):
                with self.assertRaises(guarded_go.hygiene.HygieneError):
                    guarded_go.run_go(self.state, self.cwd, "session-a", 1, str(self.fake_go), command)
        self.assertEqual(guarded_go.budget_status(self.state)["reserved_bytes"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
