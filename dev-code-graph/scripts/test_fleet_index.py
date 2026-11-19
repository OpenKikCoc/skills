#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
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
        "target_timeout": 3,
        "kill_grace": 0.15,
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

    def test_target_deadline_records_receipts_and_kills_owned_descendants(self) -> None:
        row = self.add_target("hung")
        marker = self.base / "child-pid"
        child_code = f"import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(10)"
        self.launcher.write_text(f"#!{sys.executable}\nimport subprocess,time\nsubprocess.Popen([{sys.executable!r}, '-c', {child_code!r}])\ntime.sleep(10)\n")
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
        try:
            started = time.monotonic()
            result = fleet_index.build_target(self.config, row, build_arguments(target_timeout=0.3))
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(result.status, "timed_out")
            self.assertEqual(result.return_code, 124)
            self.assertFalse(fleet_index.is_complete(self.config, "hung"))
            start = json.loads((self.output / "status/hung.build-start.json").read_text())
            terminal = json.loads((self.output / "status/hung.build-terminal.json").read_text())
            self.assertEqual(start["operation_id"], terminal["operation_id"])
            self.assertEqual(start["status"], "running")
            self.assertEqual(terminal["status"], "timed_out")
            self.assertIn("finished_at", terminal)
            child = int(marker.read_text())
            if Path(f"/proc/{child}/stat").exists():
                # A reparented zombie is terminated but may await host init reaping.
                deadline = time.monotonic() + 1
                while Path(f"/proc/{child}/stat").exists() and time.monotonic() < deadline:
                    if Path(f"/proc/{child}/stat").read_text().split()[2] == "Z":
                        break
                    time.sleep(0.01)
                self.assertTrue(not Path(f"/proc/{child}/stat").exists() or Path(f"/proc/{child}/stat").read_text().split()[2] == "Z")
            else:
                with self.assertRaises(ProcessLookupError):
                    os.kill(child, 0)
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=2)

    def test_repair_orchestrator_uses_bounded_worker_and_terminal_receipt(self) -> None:
        self.add_target("repair-hung")
        write_json(self.config.graph_path("repair-hung"), {"nodes": [], "edges": []})
        self.config.log_path("repair-hung").parent.mkdir(parents=True)
        self.config.log_path("repair-hung").write_text("[graphify] chunk 1/1 failed: fixture\n")
        self.launcher.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(10)\n")
        args = argparse.Namespace(targets=[], chunk=None, token_budget=12000,
                                  target_timeout=0.1, kill_grace=0.1)
        self.assertEqual(fleet_index.repair(self.config, args), 1)
        terminal = json.loads((self.output / "status/repair-hung.repair-terminal.json").read_text())
        self.assertEqual(terminal["status"], "timed_out")
        self.assertEqual(fleet_index.read_status(self.config, "repair-hung").status, "timed_out")

    def test_repair_worker_with_supported_fixture_persists_provenance_and_fragment(self) -> None:
        self.add_target("repair-worker")
        write_json(self.config.graph_path("repair-worker"), {"nodes": [], "edges": []})
        self.config.log_path("repair-worker").parent.mkdir(parents=True)
        self.config.log_path("repair-worker").write_text("[graphify] chunk 1/1 failed: fixture\n")
        package_root = self.base / "packages"
        package = package_root / "graphify"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "llm.py").write_text(
            "BACKENDS = {}\n"
            "def extract_files_direct(files, backend='kimi', api_key=None, model=None, root=None, deep_mode=False): return {}\n"
            "def _read_files(files, root): return ''\n"
            "def _extraction_system(deep=False): return ''\n"
            "def _parse_llm_json(content): return {}\n"
            "def _pack_chunks_by_tokens(files, token_budget=12000): return [files]\n"
            "def _extract_with_adaptive_retry(files, backend, api_key, model, root, max_depth): return {'nodes': [{'id': 'repaired'}]}\n")
        (package / "detect.py").write_text("def detect(root, extra_excludes): return {'files': {'document': [root / 'doc.md']}}\n")
        metadata = package_root / "graphify-1.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text("Name: graphify\nVersion: 1.0\nHome-page: https://example.test/graphify\n")
        (metadata / "top_level.txt").write_text("graphify\n")
        env_file = self.base / "fixture.env"
        env_file.write_text("OPENAI_API_KEY=fixture\n")
        env_file.chmod(0o600)
        config = make_config(self.root, self.output, MODULE_PATH.with_name("graphify-fleet"))
        args = argparse.Namespace(targets=["repair-worker"], chunk=None, token_budget=12000,
                                  target_timeout=3, kill_grace=0.1)
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(package_root), "GRAPHIFY_PYTHON": sys.executable,
                                        "GRAPHIFY_FLEET_ENV": str(env_file), "GRAPHIFY_LLM_BASE_URL": "https://example.test/v1", "GRAPHIFY_LLM_MODEL": "m"}):
            self.assertEqual(fleet_index.repair(config, args), 0)
        self.assertEqual(fleet_index.read_status(config, "repair-worker").status, "repaired")
        self.assertTrue(config.fragment_path("repair-worker").is_file())
        provenance = json.loads((self.output / "status/repair-worker.repair-graphify.json").read_text())
        terminal = json.loads((self.output / "status/repair-worker.repair-terminal.json").read_text())
        self.assertEqual(provenance["version"], "1.0")
        self.assertEqual(provenance["operation_id"], terminal["operation_id"])
        self.assertIn("_extract_with_adaptive_retry", provenance["signatures"])

    def test_sigterm_cancels_only_started_worker_and_records_terminal(self) -> None:
        self.add_target("cancel")
        marker = self.base / "started"
        self.launcher.write_text(f"#!{sys.executable}\nimport pathlib,time\npathlib.Path({str(marker)!r}).write_text('ready')\ntime.sleep(10)\n")
        process = subprocess.Popen([sys.executable, str(MODULE_PATH), "build", "--root", str(self.root),
                                    "--output", str(self.output), "--launcher", str(self.launcher),
                                    "--disk-floor-bytes", "1", "--target-timeout", "30", "--kill-grace", "1"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "worker never became ready")
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=3)
            self.assertNotEqual(process.returncode, 0, stdout + stderr)
            terminal = json.loads((self.output / "status/cancel.build-terminal.json").read_text())
            self.assertEqual(terminal["status"], "cancelled")
            self.assertEqual(fleet_index.read_status(self.config, "cancel").status, "cancelled")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=2)

    def test_preparation_failure_still_records_failed_terminal(self) -> None:
        compatibility = self.output / "status/repo.build-graphify.json"
        compatibility.mkdir(parents=True)
        with mock.patch.object(fleet_index.subprocess, "Popen") as spawn:
            with self.assertRaises(IsADirectoryError):
                fleet_index.run_target_process(self.config, "repo", [str(self.launcher)],
                                               phase="build", timeout=1, grace=0.1, log_path=self.base / "build.log")
            spawn.assert_not_called()
        start = json.loads((self.output / "status/repo.build-start.json").read_text())
        terminal = json.loads((self.output / "status/repo.build-terminal.json").read_text())
        self.assertEqual(start["operation_id"], terminal["operation_id"])
        self.assertEqual(terminal["status"], "failed")
        self.assertNotEqual(terminal["return_code"], 0)

    def test_repair_missing_or_invalid_artifacts_cannot_be_noop(self) -> None:
        self.add_target("repo")
        args = argparse.Namespace(targets=[], chunk=None, token_budget=12000, target_timeout=1, kill_grace=0.1)
        for graph, log in ((None, None), ({"nodes": [], "edges": []}, None),
                           (None, "complete"), ([], "complete"), ({"nodes": "invalid"}, "complete")):
            with self.subTest(graph=graph, log=log):
                self.config.graph_path("repo").unlink(missing_ok=True)
                self.config.log_path("repo").unlink(missing_ok=True)
                if graph is not None:
                    write_json(self.config.graph_path("repo"), graph)
                if log is not None:
                    self.config.log_path("repo").parent.mkdir(parents=True, exist_ok=True)
                    self.config.log_path("repo").write_text(log)
                with mock.patch.object(fleet_index, "run_target_process") as worker:
                    self.assertEqual(fleet_index.repair(self.config, args), 1)
                    worker.assert_not_called()
                summary = json.loads((self.output / "repair-summary.json").read_text())
                self.assertEqual(summary["results"][0]["status"], "failed")

    def test_repair_rejects_stale_or_inconsistent_success_evidence(self) -> None:
        self.add_target("repo")
        write_json(self.config.graph_path("repo"), {"nodes": [], "edges": []})
        self.config.log_path("repo").parent.mkdir(parents=True)
        self.config.log_path("repo").write_text("[graphify] chunk 1/1 failed: fixture\n")
        args = argparse.Namespace(targets=[], chunk=None, token_budget=12000, target_timeout=1, kill_grace=0.1)
        for broken in ("provenance", "fragment", "audit", "state", "coverage", "counts", None):
            with self.subTest(broken=broken):
                def worker(*_args, **_kwargs):
                    fragment = {"nodes": [{"id": "new"}], "edges": [], "repo": "repo", "repair_fragment": True, "operation_id": "current"}
                    audit = {"operation_id": "current", "status": "repaired", "repository": "repo",
                             "fragment_path": str(self.config.fragment_path("repo")), "chunk_indexes": [1],
                             "repaired_files": ["doc.md"], "nodes_added": 1, "edges_added": 0, "hyperedges_added": 0}
                    provenance = {"operation_id": "current", "package": "graphify", "version": "fixture",
                                  "source": "fixture", "module_path": "fixture", "module_sha256": "a" * 64,
                                  "signatures": {"_pack_chunks_by_tokens": [], "_extract_with_adaptive_retry": []}}
                    if broken in ("provenance", "fragment", "audit"):
                        {"provenance": provenance, "fragment": fragment, "audit": audit}[broken]["operation_id"] = "old"
                    if broken == "coverage":
                        audit["chunk_indexes"] = []
                    if broken == "counts":
                        audit["nodes_added"] = 2
                    write_json(self.config.fragment_path("repo"), fragment)
                    write_json(self.config.repair_audit_path("repo"), audit)
                    write_json(self.output / "status/repo.repair-graphify.json", provenance)
                    fleet_index.write_status(self.config, fleet_index.BuildResult(name="repo", status="running" if broken == "state" else "repaired", repair_fragment=str(self.config.fragment_path("repo"))))
                    return {"operation_id": "current", "status": "exited", "return_code": 0}
                with mock.patch.object(fleet_index, "run_target_process", side_effect=worker):
                    self.assertEqual(fleet_index.repair(self.config, args), 0 if broken is None else 1)
        self.config.log_path("repo").write_text("complete\n")
        with mock.patch.object(fleet_index, "run_target_process") as worker:
            self.assertEqual(fleet_index.repair(self.config, args), 0)
            worker.assert_not_called()

    def test_repair_zero_exit_without_current_evidence_fails(self) -> None:
        self.add_target("repo")
        write_json(self.config.graph_path("repo"), {"nodes": [], "edges": []})
        self.config.log_path("repo").parent.mkdir(parents=True)
        self.config.log_path("repo").write_text("[graphify] chunk 1/1 failed: fixture\n")
        self.launcher.write_text(f"#!{sys.executable}\npass\n")
        args = argparse.Namespace(targets=[], chunk=None, token_budget=12000, target_timeout=1, kill_grace=0.1)
        self.assertEqual(fleet_index.repair(self.config, args), 1)
        self.assertEqual(fleet_index.read_status(self.config, "repo").status, "failed")
        self.assertFalse(self.config.fragment_path("repo").exists())
        summary = json.loads((self.output / "repair-summary.json").read_text())
        self.assertEqual(summary["results"][0]["status"], "failed")

    def test_spawn_failure_still_records_terminal_receipt(self) -> None:
        with self.assertRaises(FileNotFoundError):
            fleet_index.run_target_process(self.config, "missing", [str(self.base / "absent")],
                                           phase="build", timeout=1, grace=0.1, log_path=self.base / "missing.log")
        terminal = json.loads((self.output / "status/missing.build-terminal.json").read_text())
        self.assertEqual(terminal["status"], "failed")
        self.assertIn("finished_at", terminal)

    def test_second_coordinator_on_same_output_root_is_refused(self) -> None:
        self.add_target("repo")
        args = build_arguments()
        with fleet_index.CoordinatorLock(self.config):
            with self.assertRaisesRegex(fleet_index.FleetError, "another coordinator"):
                fleet_index.build(self.config, args)
            with self.assertRaisesRegex(fleet_index.FleetError, "another coordinator"):
                fleet_index.repair(self.config, argparse.Namespace(
                    targets=[], chunk=None, token_budget=12000, target_timeout=1, kill_grace=0.1))
            # A dry run only reads and does not need the lock.
            self.assertEqual(fleet_index.build(self.config, build_arguments(dry_run=True)), 0)
        self.assertEqual(fleet_index.build(self.config, args), 0)

    def test_start_only_receipt_is_an_unknown_attempt_and_is_not_relaunched(self) -> None:
        row = self.add_target("crashed")
        status = self.output / "status"
        write_json(status / "crashed.build-start.json", {"operation_id": "x", "status": "running"})
        result = fleet_index.build_target(self.config, row, build_arguments())
        self.assertEqual(result.status, "unknown_attempt")
        self.assertFalse(self.config.graph_path("crashed").exists())
        self.assertEqual(fleet_index.unknown_attempts(self.config, "crashed"), ["build"])
        # A target named like a phase-suffixed neighbour is not confused with it.
        self.assertEqual(fleet_index.unknown_attempts(self.config, "crash"), [])
        forced = fleet_index.build_target(self.config, row, build_arguments(force=True))
        self.assertEqual(forced.status, "success")
        self.assertEqual(fleet_index.unknown_attempts(self.config, "crashed"), [])

    def test_timed_out_target_is_failure_and_is_not_retried_in_the_same_run(self) -> None:
        self.add_target("slow")
        self.launcher.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(10)\n")
        args = build_arguments(target_timeout=0.2, kill_grace=0.1)
        self.assertEqual(fleet_index.build(self.config, args), 1)
        status = fleet_index.read_status(self.config, "slow")
        self.assertEqual(status.status, "timed_out")
        terminal = json.loads((self.output / "status/slow.build-terminal.json").read_text())
        self.assertEqual(terminal["status"], "timed_out")


if __name__ == "__main__":
    unittest.main(verbosity=2)
