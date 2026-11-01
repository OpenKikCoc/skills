#!/usr/bin/env python3
"""Regression tests for bounded read-only disk audits."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("quick_disk_audit.py")
SPEC = importlib.util.spec_from_file_location("quick_disk_audit", SCRIPT)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)

NOW = dt.datetime(2026, 8, 13, 19, 0, tzinfo=dt.timezone.utc)


class QuickDiskAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.scan_root = self.root / "scan"
        self.scan_root.mkdir()
        self.alpha = self.scan_root / "alpha-cache"
        self.beta = self.scan_root / "beta-cache"
        self.alpha.mkdir()
        self.beta.mkdir()
        (self.alpha / "payload").write_bytes(b"a" * 1024**2)
        (self.beta / "payload").write_bytes(b"b" * (2 * 1024**2))
        self.focus = self.root / "focus"
        self.focus.mkdir()
        (self.focus / "payload").write_bytes(b"f" * 1024**2)

    def audit(
        self,
        previous: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return AUDIT.quick_audit(
            self.root,
            scan_roots=[str(self.scan_root)],
            focused_paths=[str(self.focus)],
            raw_groups=[r"alpha=^alpha-", r"beta=^beta-"],
            jobs=2,
            top=1,
            timeout_seconds=30,
            previous=previous,
            previous_path=self.root / "previous.json" if previous else None,
            now=NOW,
        )

    def test_groups_children_once_and_limits_top_entries(self) -> None:
        link = self.scan_root / "linked-cache"
        link.symlink_to(self.alpha, target_is_directory=True)

        result = self.audit()

        self.assertEqual(result["kind"], AUDIT.REPORT_KIND)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["jobs"], 2)
        root = result["scan_roots"][0]
        self.assertEqual(root["entry_count"], 2)
        self.assertEqual(root["measured_entry_count"], 2)
        self.assertEqual(root["ignored_entry_count"], 1)
        self.assertEqual(len(root["top_entries"]), 1)
        self.assertEqual(root["top_entries"][0]["name"], "beta-cache")
        groups = {group["name"]: group for group in root["groups"]}
        self.assertEqual(groups["alpha"]["entry_count"], 1)
        self.assertEqual(groups["beta"]["entry_count"], 1)
        self.assertEqual(groups["other"]["entry_count"], 0)
        self.assertGreater(root["allocated_bytes"], 0)
        self.assertEqual(result["focused_paths"][0]["status"], "measured")

    def test_reports_deltas_only_for_matching_scope(self) -> None:
        previous = self.audit()
        (self.beta / "new-payload").write_bytes(b"n" * (3 * 1024**2))
        (self.focus / "new-payload").write_bytes(b"n" * (2 * 1024**2))

        current = self.audit(previous)

        root = current["scan_roots"][0]
        self.assertGreater(root["delta_allocated_bytes"], 0)
        groups = {group["name"]: group for group in root["groups"]}
        self.assertGreater(groups["beta"]["delta_allocated_bytes"], 0)
        self.assertGreater(
            current["focused_paths"][0]["delta_allocated_bytes"],
            0,
        )

        changed_scope = json.loads(json.dumps(previous))
        changed_scope["scope_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            AUDIT.AuditError,
            "scope_sha256 does not match",
        ):
            self.audit(changed_scope)
        partial = json.loads(json.dumps(previous))
        partial["status"] = "partial"
        with self.assertRaisesRegex(AUDIT.AuditError, "status complete"):
            self.audit(partial)

    def test_load_previous_rejects_tampered_scope(self) -> None:
        previous = self.audit()
        previous["scope"]["top"] = 99
        previous_path = self.root / "tampered.json"
        previous_path.write_text(
            json.dumps(previous, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            AUDIT.AuditError,
            "scope_sha256 does not match",
        ):
            AUDIT.load_previous(previous_path)

        with self.assertRaisesRegex(
            AUDIT.AuditError,
            "scope_sha256 does not match",
        ):
            self.audit(previous)

    def test_rejects_invalid_contracts(self) -> None:
        with self.assertRaisesRegex(
            AUDIT.AuditError,
            "requires --scan-root or --focus",
        ):
            AUDIT.quick_audit(
                self.root,
                scan_roots=[],
                focused_paths=[],
                raw_groups=[],
                jobs=1,
                top=1,
                timeout_seconds=30,
                previous=None,
                previous_path=None,
                now=NOW,
            )
        with self.assertRaisesRegex(AUDIT.AuditError, "NAME=REGEX"):
            AUDIT.parse_groups(["invalid"])
        with self.assertRaisesRegex(AUDIT.AuditError, "between 1 and 16"):
            AUDIT.quick_audit(
                self.root,
                scan_roots=[str(self.scan_root)],
                focused_paths=[],
                raw_groups=[],
                jobs=17,
                top=1,
                timeout_seconds=30,
                previous=None,
                previous_path=None,
                now=NOW,
            )

    def test_cli_writes_reusable_baseline(self) -> None:
        output = self.root / "audit.json"
        completed = subprocess.run(
            [
                str(SCRIPT),
                "--path",
                str(self.root),
                "--scan-root",
                str(self.scan_root),
                "--group",
                r"alpha=^alpha-",
                "--group",
                r"beta=^beta-",
                "--focus",
                str(self.focus),
                "--output",
                str(output),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr + (output.read_text() if output.exists() else ""))
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(document["kind"], AUDIT.REPORT_KIND)
        self.assertRegex(document["scope_sha256"], r"^[0-9a-f]{64}$")

    def test_cleanup_candidates_are_read_only_and_do_not_infer_authority(self) -> None:
        marker = self.alpha / ".dev-disk-budget-owner.json"
        marker.write_text("not yet validated")
        report = self.audit()
        report["focused_paths"].extend([
            {"path": str(self.alpha), "status": "measured", "allocated_bytes": 1024},
            {"path": str(self.alpha), "status": "measured", "allocated_bytes": 1024},
            {"path": str(self.root / "sessions"), "status": "measured", "allocated_bytes": 4096},
        ])
        before = marker.read_bytes()
        candidates = AUDIT.cleanup_candidates(report)
        self.assertEqual(sum(c["path"] == str(self.alpha) for c in candidates), 1)
        self.assertTrue(all(c["cleanup_authorized"] is False for c in candidates))
        self.assertTrue(all(c["reclaimable_bytes"] is None for c in candidates))
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(next(c for c in candidates if c["path"] == str(self.alpha))["classification"], "owned_marker_present")

    def test_cli_falls_back_to_stdout_when_output_is_unwritable(self) -> None:
        with mock.patch.object(
            AUDIT,
            "quick_audit",
            side_effect=OSError("audit output failed"),
        ), mock.patch.object(
            AUDIT,
            "write_output",
            side_effect=[OSError("requested path denied"), None],
        ) as write_output:
            with mock.patch(
                "sys.argv",
                [str(SCRIPT), "--focus", str(self.focus), "--output", "/denied"],
            ):
                exit_code = AUDIT.main()

        self.assertEqual(exit_code, 2)
        self.assertEqual(write_output.call_count, 2)
        fallback_document, fallback_path = write_output.call_args_list[1].args
        self.assertIsNone(fallback_path)
        self.assertIn("requested path denied", fallback_document["errors"][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
