#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("fleet_index.py")
SPEC = importlib.util.spec_from_file_location("fleet_index_under_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load fleet_index.py")
fleet_index = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fleet_index
SPEC.loader.exec_module(fleet_index)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def make_config(root: Path, output: Path, launcher: Path) -> object:
    manifest = output / "inventory.json"
    return fleet_index.FleetConfig(
        root=root,
        output=output,
        launcher=str(launcher),
        detect_manifest=manifest,
        excludes=fleet_index.DEFAULT_EXCLUDES,
    )


def build_arguments(**overrides: object) -> argparse.Namespace:
    values = {
        "targets": [],
        "parallel": 2,
        "disk_floor_bytes": 1,
        "force": False,
        "dry_run": False,
        "graphify_workers": 1,
        "graphify_concurrency": 1,
        "token_budget": 12000,
        "api_timeout": 30,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class FleetIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.base = Path(self.temporary_directory.name)
        self.root = self.base / "fleet"
        self.output = self.base / "output"
        self.root.mkdir()
        self.launcher = self.base / "fake-graphify"
        self.launcher.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import pathlib
                import sys

                args = sys.argv[1:]
                target = pathlib.Path(args[1])
                output = pathlib.Path(args[args.index("--out") + 1])
                graph = output / "graphify-out" / "graph.json"
                graph.parent.mkdir(parents=True, exist_ok=True)
                counter = output / "invocations.txt"
                count = int(counter.read_text() or "0") if counter.exists() else 0
                counter.write_text(str(count + 1))
                payload = {
                    "nodes": [{"id": target.name, "label": target.name}],
                    "edges": [],
                    "hyperedges": [],
                    "metadata": {"large": "x" * 200},
                }
                graph.write_text(json.dumps(payload))
                cache = output / "cache"
                cache.mkdir(exist_ok=True)
                (cache / "state").write_text("resume")
                if os.environ.get("FAKE_FAILED_CHUNK") == target.name:
                    print("[graphify] chunk 2/3 failed: fake capacity")
                sys.exit(0)
                """
            ),
            encoding="utf-8",
        )
        self.launcher.chmod(
            self.launcher.stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
        self.config = make_config(self.root, self.output, self.launcher)

    def add_target(self, name: str, *, total_files: int = 1) -> dict[str, object]:
        target = self.root / name
        target.mkdir()
        (target / "doc.md").write_text(f"# {name}\n", encoding="utf-8")
        row: dict[str, object] = {
            "name": name,
            "path": name,
            "status": "success",
            "total_files": total_files,
            "total_words": 2,
            "counts": {"document": total_files},
            "skipped_sensitive": 0,
        }
        rows = []
        if self.config.detect_manifest.exists():
            rows = json.loads(
                self.config.detect_manifest.read_text(encoding="utf-8")
            )["targets"]
        rows.append(row)
        write_json(
            self.config.detect_manifest,
            {
                "schema_version": 1,
                "root": str(self.root),
                "targets": rows,
            },
        )
        return row

    def test_exit_zero_with_failed_chunk_is_partial_and_resume_skips_success(
        self,
    ) -> None:
        self.add_target("partial")
        self.add_target("complete")
        args = build_arguments()
        with mock.patch.dict(
            os.environ, {"FAKE_FAILED_CHUNK": "partial"}, clear=False
        ):
            self.assertEqual(fleet_index.build(self.config, args), 1)

        partial = fleet_index.read_status(self.config, "partial")
        complete = fleet_index.read_status(self.config, "complete")
        self.assertIsNotNone(partial)
        self.assertIsNotNone(complete)
        self.assertEqual(partial.status, "partial")
        self.assertEqual(partial.failed_chunks, (2,))
        self.assertEqual(complete.status, "success")
        self.assertTrue(
            (self.config.target_output("partial") / "cache" / "state").exists()
        )
        self.assertFalse(
            (self.config.target_output("complete") / "cache").exists()
        )

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FAKE_FAILED_CHUNK", None)
            self.assertEqual(fleet_index.build(self.config, args), 0)

        self.assertEqual(
            (
                self.config.target_output("complete") / "invocations.txt"
            ).read_text(),
            "1",
        )
        self.assertEqual(
            (
                self.config.target_output("partial") / "invocations.txt"
            ).read_text(),
            "2",
        )

    def test_disk_floor_blocks_before_launcher(self) -> None:
        row = self.add_target("low-disk")
        args = build_arguments(disk_floor_bytes=10_000)
        with mock.patch.object(
            fleet_index, "disk_free_bytes", return_value=9_999
        ):
            result = fleet_index.build_target(self.config, row, args)

        self.assertEqual(result.status, "blocked_low_disk")
        self.assertEqual(result.return_code, 75)
        self.assertFalse(
            (self.config.target_output("low-disk") / "invocations.txt").exists()
        )

    def test_inventory_and_build_dry_run_do_not_launch_graphify(self) -> None:
        target = self.root / "inventory-only"
        target.mkdir()
        (target / "doc.md").write_text("# inventory\n", encoding="utf-8")
        detection = {
            "files": {
                "code": [],
                "document": [str(target / "doc.md")],
                "paper": [],
                "image": [],
                "video": [],
            },
            "total_files": 1,
            "total_words": 1,
            "needs_graph": False,
            "warning": "small",
            "skipped_sensitive": [],
        }
        fake_detect = types.ModuleType("graphify.detect")
        fake_detect.detect = lambda *_args, **_kwargs: detection
        fake_graphify = types.ModuleType("graphify")
        fake_graphify.detect = fake_detect
        inventory_args = argparse.Namespace(
            target=["inventory-only"],
            parallel=1,
        )
        with mock.patch.dict(
            sys.modules,
            {"graphify": fake_graphify, "graphify.detect": fake_detect},
        ):
            self.assertEqual(fleet_index.inventory(self.config, inventory_args), 0)

        manifest = json.loads(
            self.config.detect_manifest.read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["targets"][0]["path"], "inventory-only")
        self.assertEqual(manifest["targets"][0]["total_files"], 1)
        with mock.patch("builtins.print") as output:
            self.assertEqual(
                fleet_index.build(
                    self.config,
                    build_arguments(targets=["inventory-only"], dry_run=True),
                ),
                0,
            )
        plan = json.loads(output.call_args.args[0])
        self.assertFalse(plan["targets"][0]["would_resume"])
        self.assertEqual(plan["targets"][0]["name"], "inventory-only")
        self.assertFalse(
            (
                self.config.target_output("inventory-only") / "invocations.txt"
            ).exists()
        )

    def test_repair_writes_only_unique_fragment(self) -> None:
        row = self.add_target("repairable")
        graph_path = self.config.graph_path("repairable")
        write_json(
            graph_path,
            {
                "nodes": [{"id": "existing"}],
                "edges": [
                    {
                        "source": "existing",
                        "target": "old",
                        "relation": "uses",
                    }
                ],
                "hyperedges": [],
            },
        )
        log_path = self.config.log_path("repairable")
        log_path.parent.mkdir(parents=True)
        log_path.write_text(
            "[graphify] chunk 2/2 failed: fake capacity\n", encoding="utf-8"
        )
        fleet_index.write_status(
            self.config,
            fleet_index.BuildResult(
                name="repairable",
                status="partial",
                failed_chunks=(2,),
            ),
        )
        chunks = [
            [self.root / "repairable" / "first.md"],
            [self.root / "repairable" / "second.md"],
        ]
        repair_result = {
            "nodes": [{"id": "existing"}, {"id": "new"}],
            "edges": [
                {
                    "source": "existing",
                    "target": "old",
                    "relation": "uses",
                },
                {
                    "source": "existing",
                    "target": "new",
                    "relation": "uses",
                },
            ],
            "hyperedges": [],
            "input_tokens": 4,
            "output_tokens": 2,
        }
        fake_llm = types.ModuleType("graphify.llm")
        fake_llm._pack_chunks_by_tokens = lambda *_args, **_kwargs: chunks
        fake_llm._extract_with_adaptive_retry = (
            lambda *_args, **_kwargs: repair_result
        )
        fake_graphify = types.ModuleType("graphify")
        fake_graphify.llm = fake_llm
        with mock.patch.object(
            fleet_index, "semantic_files", return_value=sum(chunks, [])
        ), mock.patch.dict(
            sys.modules,
            {"graphify": fake_graphify, "graphify.llm": fake_llm},
        ):
            result = fleet_index.repair_target(
                self.config, row, None, token_budget=12000
            )

        fragment = json.loads(
            self.config.fragment_path("repairable").read_text(encoding="utf-8")
        )
        self.assertEqual(result["status"], "repaired")
        self.assertEqual(fragment["nodes"], [{"id": "new"}])
        self.assertEqual(
            fragment["edges"],
            [
                {
                    "source": "existing",
                    "target": "new",
                    "relation": "uses",
                }
            ],
        )
        self.assertEqual(
            fleet_index.read_status(self.config, "repairable").status,
            "repaired",
        )

    def test_repair_accumulates_existing_fragment_and_chunks(self) -> None:
        row = self.add_target("repair-twice")
        write_json(
            self.config.graph_path("repair-twice"),
            {"nodes": [{"id": "base"}], "edges": [], "hyperedges": []},
        )
        log_path = self.config.log_path("repair-twice")
        log_path.parent.mkdir(parents=True)
        log_path.write_text(
            "[graphify] chunk 1/2 failed: fake\n"
            "[graphify] chunk 2/2 failed: fake\n",
            encoding="utf-8",
        )
        fleet_index.write_status(
            self.config,
            fleet_index.BuildResult(
                name="repair-twice",
                status="partial",
                failed_chunks=(1, 2),
            ),
        )
        chunks = [
            [self.root / "repair-twice" / "first.md"],
            [self.root / "repair-twice" / "second.md"],
        ]

        def extract(chunk, **_kwargs):
            identifier = "first" if chunk == chunks[0] else "second"
            return {
                "nodes": [{"id": identifier}],
                "edges": [],
                "hyperedges": [],
                "input_tokens": 1,
                "output_tokens": 1,
            }

        fake_llm = types.ModuleType("graphify.llm")
        fake_llm._pack_chunks_by_tokens = lambda *_args, **_kwargs: chunks
        fake_llm._extract_with_adaptive_retry = extract
        fake_graphify = types.ModuleType("graphify")
        fake_graphify.llm = fake_llm
        with mock.patch.object(
            fleet_index, "semantic_files", return_value=sum(chunks, [])
        ), mock.patch.dict(
            sys.modules,
            {"graphify": fake_graphify, "graphify.llm": fake_llm},
        ):
            fleet_index.repair_target(
                self.config, row, [0], token_budget=12000
            )
            fleet_index.repair_target(
                self.config, row, [1], token_budget=12000
            )

        fragment = json.loads(
            self.config.fragment_path("repair-twice").read_text(encoding="utf-8")
        )
        audit = json.loads(
            self.config.repair_audit_path("repair-twice").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            [node["id"] for node in fragment["nodes"]],
            ["first", "second"],
        )
        self.assertEqual(fragment["input_tokens"], 2)
        self.assertEqual(audit["chunk_indexes"], [1, 2])
        self.assertEqual(audit["nodes_added"], 2)

    def test_force_success_clears_stale_repair_state(self) -> None:
        row = self.add_target("force-clean")
        write_json(
            self.config.fragment_path("force-clean"),
            {"nodes": [{"id": "stale"}], "edges": [], "hyperedges": []},
        )
        write_json(
            self.config.repair_audit_path("force-clean"),
            {"chunk_indexes": [1]},
        )
        fleet_index.write_status(
            self.config,
            fleet_index.BuildResult(name="force-clean", status="repaired"),
        )

        result = fleet_index.build_target(
            self.config, row, build_arguments(force=True)
        )

        self.assertEqual(result.status, "success")
        self.assertFalse(self.config.fragment_path("force-clean").exists())
        self.assertFalse(self.config.repair_audit_path("force-clean").exists())
        self.assertEqual(
            fleet_index.merge_inputs(self.config, [row]),
            [("force-clean", self.config.graph_path("force-clean"))],
        )

    def test_compact_merge_honors_configured_strict_limit(self) -> None:
        input_path = self.base / "input.json"
        write_json(
            input_path,
            {
                "nodes": [
                    {
                        "id": "n1",
                        "label": "Node",
                        "metadata": "x" * 10_000,
                        "author": "not needed",
                    }
                ],
                "edges": [
                    {
                        "source": "n1",
                        "target": "n1",
                        "relation": "self",
                        "metadata": "y" * 10_000,
                    }
                ],
            },
        )
        output_path = self.base / "compact.json"
        counts, _ = fleet_index.stream_merge(
            [("repo", input_path)],
            output_path,
            compact=True,
            max_bytes=1_000,
        )
        compact = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(counts, {"nodes": 1, "links": 1, "hyperedges": 0})
        self.assertLess(output_path.stat().st_size, 1_000)
        self.assertNotIn("metadata", compact["nodes"][0])
        self.assertEqual(compact["nodes"][0]["id"], "repo::n1")

        previous = output_path.read_bytes()
        with self.assertRaisesRegex(fleet_index.FleetError, "configured limit"):
            fleet_index.stream_merge(
                [("repo", input_path)],
                output_path,
                compact=True,
                max_bytes=10,
            )
        self.assertEqual(output_path.read_bytes(), previous)

    def test_merge_and_audit_completed_fleet(self) -> None:
        row = self.add_target("audited")
        write_json(
            self.config.graph_path("audited"),
            {
                "nodes": [{"id": "n1", "label": "Node", "metadata": "large"}],
                "edges": [
                    {"source": "n1", "target": "n1", "relation": "self"}
                ],
                "hyperedges": [],
            },
        )
        log_path = self.config.log_path("audited")
        log_path.parent.mkdir(parents=True)
        log_path.write_text("complete\n", encoding="utf-8")
        fleet_index.write_status(
            self.config,
            fleet_index.BuildResult(name="audited", status="success"),
        )
        merge_args = argparse.Namespace(
            targets=[],
            archive_out=None,
            compact_out=None,
            compact_limit_bytes=10_000,
        )
        self.assertEqual(fleet_index.merge(self.config, merge_args), 0)
        audit_payload = fleet_index.audit_fleet(
            self.config,
            [row],
            self.output / "fleet-query-graph.json",
            10_000,
        )
        self.assertTrue(audit_payload["ok"], audit_payload["issues"])
        self.assertEqual(audit_payload["issues"], [])

    def test_merge_rejects_output_collisions(self) -> None:
        row = self.add_target("collision")
        write_json(
            self.config.graph_path("collision"),
            {"nodes": [{"id": "n1"}], "edges": [], "hyperedges": []},
        )
        self.config.log_path("collision").parent.mkdir(parents=True)
        self.config.log_path("collision").write_text("complete\n")
        fleet_index.write_status(
            self.config,
            fleet_index.BuildResult(name="collision", status="success"),
        )
        graph_path = self.config.graph_path("collision")
        with self.assertRaisesRegex(fleet_index.FleetError, "conflicts"):
            fleet_index.merge(
                self.config,
                argparse.Namespace(
                    targets=[],
                    archive_out=graph_path,
                    compact_out=None,
                    compact_limit_bytes=10_000,
                ),
            )
        same = self.base / "same.json"
        with self.assertRaisesRegex(
            fleet_index.FleetError, "outputs must differ"
        ):
            fleet_index.merge(
                self.config,
                argparse.Namespace(
                    targets=[],
                    archive_out=same,
                    compact_out=same,
                    compact_limit_bytes=10_000,
                ),
            )
        self.assertEqual(
            json.loads(graph_path.read_text(encoding="utf-8"))["nodes"][0]["id"],
            "n1",
        )

    def test_audit_rejects_stale_compact_after_input_change(self) -> None:
        row = self.add_target("stale-audit")
        graph_path = self.config.graph_path("stale-audit")
        write_json(
            graph_path,
            {"nodes": [{"id": "before"}], "edges": [], "hyperedges": []},
        )
        self.config.log_path("stale-audit").parent.mkdir(parents=True)
        self.config.log_path("stale-audit").write_text("complete\n")
        fleet_index.write_status(
            self.config,
            fleet_index.BuildResult(name="stale-audit", status="success"),
        )
        args = argparse.Namespace(
            targets=[],
            archive_out=None,
            compact_out=None,
            compact_limit_bytes=10_000,
        )
        fleet_index.merge(self.config, args)
        write_json(
            graph_path,
            {"nodes": [{"id": "after"}], "edges": [], "hyperedges": []},
        )

        audit = fleet_index.audit_fleet(
            self.config,
            [row],
            self.output / "fleet-query-graph.json",
            10_000,
        )

        self.assertFalse(audit["ok"])
        self.assertTrue(
            any(
                "inputs do not match" in issue["message"]
                for issue in audit["issues"]
            )
        )

    def test_atomic_publish_fails_closed_when_replace_is_denied(self) -> None:
        destination = self.base / "audit.json"
        destination.write_text("old", encoding="utf-8")
        real_replace = os.replace

        def deny_replace(source: object, target: object) -> None:
            if Path(target) == destination:
                raise PermissionError("simulated provenance restriction")
            real_replace(source, target)

        with mock.patch.object(
            fleet_index.os, "replace", side_effect=deny_replace
        ), self.assertRaisesRegex(
            fleet_index.FleetError, "atomic replacement is not permitted"
        ):
            fleet_index.atomic_write_json(destination, {"ok": True})

        self.assertEqual(destination.read_text(encoding="utf-8"), "old")
        self.assertEqual(list(self.base.glob(".audit.json.*.tmp")), [])

    def test_cli_fake_graphify_exit_zero_partial(self) -> None:
        self.add_target("cli-partial")
        process = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "build",
                "--root",
                str(self.root),
                "--output",
                str(self.output),
                "--launcher",
                str(self.launcher),
                "--detect-manifest",
                str(self.config.detect_manifest),
                "--disk-floor-bytes",
                "1",
                "cli-partial",
            ],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "FAKE_FAILED_CHUNK": "cli-partial"},
        )

        self.assertEqual(process.returncode, 1, process.stderr)
        status = json.loads(
            self.config.status_path("cli-partial").read_text(encoding="utf-8")
        )
        self.assertEqual(status["status"], "partial")
        self.assertEqual(status["failed_chunks"], [2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
