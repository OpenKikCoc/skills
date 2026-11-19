#!/usr/bin/env python3
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import start_serena  # noqa: E402


class LauncherTests(unittest.TestCase):
    def test_git_worktree_path_is_not_replaced_with_common_git_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            response = subprocess.CompletedProcess([], 0, str(root) + "\n", "")
            with patch("start_serena.subprocess.run", return_value=response) as run:
                self.assertEqual(start_serena.project_root(root / "pkg"), root)
            self.assertEqual(run.call_args.args[0][-1], "--show-toplevel")

    def test_home_and_non_repository_are_not_activated(self):
        for response in (
            subprocess.CompletedProcess([], 128, "", "not a repository"),
            subprocess.CompletedProcess([], 0, str(Path.home()) + "\n", ""),
        ):
            with self.subTest(response=response.returncode):
                with patch("start_serena.subprocess.run", return_value=response):
                    self.assertIsNone(start_serena.project_root(Path.cwd()))

    def test_same_directory_name_has_distinct_cache_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = start_serena.cache_folder(root / "state", root / "one" / "repo")
            second = start_serena.cache_folder(root / "state", root / "two" / "repo")
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_relative_to(root / "state"))

    def test_missing_language_server_fails_without_starting(self):
        with patch("start_serena.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "serena must be installed"):
                start_serena.launch_spec(Path.cwd(), Path("session"), Path("state"))

    def test_preflight_names_missing_inferred_gopls_before_semantic_query(self):
        project = Path("/repo")

        def which(name, path=None):
            return {"go": "/bin/go", "gopls": None}.get(name)

        with patch("start_serena.infer_languages", return_value=["go"]):
            with patch("start_serena.shutil.which", side_effect=which):
                report = start_serena.preflight_project(project)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["missing_executables"], ["gopls"])
        self.assertEqual(report["resolved_executables"], {"go": "/bin/go"})

    def test_non_go_project_does_not_require_gopls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch("start_serena.project_root", return_value=root / "repo"):
                with patch("start_serena.shutil.which", return_value="/bin/serena") as which:
                    with patch("start_serena.serena_cli_prefix", return_value=["/bin/python", "/bootstrap"]):
                        command, _, _ = start_serena.launch_spec(root, root / "session", root / "state")
            self.assertEqual(command[:3], ["/bin/python", "/bootstrap", "start-mcp-server"])
            which.assert_called_once()

    def test_discovers_git_directories_and_pointer_files_with_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "repo-a" / ".git").mkdir(parents=True)
            (root / "nested" / "repo-b").mkdir(parents=True)
            (root / "nested" / "repo-b" / ".git").write_text("gitdir: elsewhere\n")
            (root / "repo-a" / "nested-generated" / ".git").mkdir(parents=True)
            (root / "runtime" / "data" / "workspaces" / "generated" / ".git").mkdir(parents=True)
            (root / "archive" / "raw" / "shards" / "generated" / ".git").mkdir(parents=True)
            (root / "ordinary" / "node_modules" / "ignored" / ".git").mkdir(parents=True)
            (root / "ordinary" / "vendor" / "ignored" / ".git").mkdir(parents=True)
            with patch.dict(os.environ, {"SERENA_DISCOVERY_EXCLUDES": json.dumps([
                    "runtime/data/workspaces", "raw/shards"])}):
                report = []
                self.assertEqual(start_serena.discover_projects([root], report),
                                 [root / "nested" / "repo-b", root / "repo-a"])
                configured = [row for row in report if row["reason"].startswith("configured")]
                self.assertEqual(len(configured), 2)
                self.assertTrue(all({"path", "pattern", "reason"} <= set(row) for row in report))

    def test_default_discovery_has_no_site_specific_excludes(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SERENA_DISCOVERY_EXCLUDES": "[]"}):
            root = Path(directory).resolve()
            names = ["workdir/repos/repo", "runtime/data/workspaces/repo",
                     "raw/shards/repo", ".claude/worktrees/repo"]
            for name in [*names, "tmp/ignored", ".cache/ignored", "a/__pycache__/ignored"]:
                (root / name / ".git").mkdir(parents=True)
            report = []
            self.assertEqual(start_serena.discover_projects([root], report),
                             sorted(root / name for name in names))
            self.assertEqual({row["pattern"] for row in report if row["reason"] == "temporary directory"},
                             {"tmp", ".cache", "__pycache__"})

    def test_explicit_root_inside_excluded_name_is_still_scanned(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SERENA_DISCOVERY_EXCLUDES": "[]"}):
            root = Path(directory).resolve() / "tmp"
            (root / "repo" / ".git").mkdir(parents=True)
            self.assertEqual(start_serena.discover_projects([root]), [root / "repo"])

    def test_invalid_discovery_excludes_fail_closed(self):
        for value in ('{}', '[1]', '["../repo"]', '["/repo"]', '[""]', 'not json', '["a/./b"]'):
            with patch.dict(os.environ, {"SERENA_DISCOVERY_EXCLUDES": value}), self.assertRaises(ValueError):
                start_serena.discover_projects([])

    def test_workspace_root_may_be_a_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / ".git").mkdir()
            (root / "nested-generated" / ".git").mkdir(parents=True)
            self.assertEqual(start_serena.discover_projects([root]), [root])

    def test_nested_repository_prunes_its_internal_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "repo" / ".git").mkdir(parents=True)
            (root / "repo" / "nested-generated" / ".git").mkdir(parents=True)
            self.assertEqual(start_serena.discover_projects([root]), [root / "repo"])

    def test_runtime_config_registers_only_configured_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime = root / "runtime"
            runtime.mkdir()
            state = root / "state"
            configured = root / "configured"
            missing = root / "missing"
            (start_serena.cache_folder(state, configured)).mkdir(parents=True)
            (start_serena.cache_folder(state, configured) / "project.yml").write_text("project_name: configured\n")
            config_path = start_serena.write_runtime_config(runtime, state, [missing, configured])
            config = json.loads(config_path.read_text())
            self.assertEqual(config["projects"], [str(configured)])

    def test_real_serena_dynamic_activation_ignores_repository_local_project_config(self):
        binary = start_serena.shutil.which("serena")
        if binary is None:
            self.skipTest("Serena executable is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            runtime = root / "runtime"
            local = project / ".serena" / "project.yml"
            local.parent.mkdir(parents=True)
            local.write_text(textwrap.dedent("""\
                project_name: repository-local
                language_servers:
                  - python
                ignored_paths:
                  - generated/**
                encoding: utf-8
                ls_specific_settings:
                  python:
                    diagnostics: workspace
                activation_command: touch should-not-run
            """))
            local_contents = local.read_text()
            (project / "tool.py").write_text("value = 1\n")
            runtime.mkdir()
            start_serena.write_runtime_config(runtime, runtime, [])
            with patch("start_serena.project_root", return_value=project):
                _, env, _ = start_serena.launch_spec(project, runtime, root / "state")
            bootstrap = start_serena.ASSETS / "runtime" / "serena_bootstrap.py"
            probe = root / "activation_probe.py"
            probe.write_text(textwrap.dedent("""\
                import importlib.util
                import json
                import sys

                spec = importlib.util.spec_from_file_location("serena_bootstrap", sys.argv[1])
                bootstrap = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(bootstrap)
                bootstrap.patch_project_resolution()
                from serena.config.serena_config import SerenaConfig
                config = SerenaConfig.from_config_file()
                project = config.add_project_from_path(sys.argv[2], asynchronous_autogen=False)
                value = project.project_config
                print(json.dumps({
                    "project_name": value.project_name,
                    "language_servers": [item.value for item in value.language_servers],
                    "ignored_paths": value.ignored_paths,
                    "encoding": value.encoding,
                    "ls_specific_settings": value.ls_specific_settings,
                    "activation_command": value.activation_command,
                }, sort_keys=True))
            """))
            python = Path(binary).resolve().parent / "python"
            result = subprocess.run(
                [str(python), str(probe), str(bootstrap), str(project)],
                env=env, capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            private = start_serena.cache_folder(runtime, project) / "project.yml"
            self.assertTrue(private.is_file())
            private_config = json.loads(result.stdout.splitlines()[-1])
            self.assertEqual(private_config["project_name"], "repository-local")
            self.assertEqual(private_config["language_servers"], ["python"])
            self.assertEqual(private_config["ignored_paths"], ["generated/**"])
            self.assertEqual(private_config["encoding"], "utf-8")
            self.assertEqual(private_config["ls_specific_settings"], {"python": {"diagnostics": "workspace"}})
            self.assertIsNone(private_config["activation_command"])
            self.assertNotIn("should-not-run", private.read_text())
            selection = json.loads((private.parent / start_serena.PROJECT_SELECTION_FILE).read_text())
            self.assertEqual(selection, {"ignored_paths": ["generated/**"], "language_servers": ["python"]})
            self.assertEqual(local.read_text(), local_contents)
            self.assertEqual({path.name for path in local.parent.iterdir()}, {"project.yml"})

    def test_repository_project_settings_are_normalized_into_private_state(self):
        binary = start_serena.shutil.which("serena")
        if binary is None:
            self.skipTest("Serena executable is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            runtime = root / "runtime"
            local = project / ".serena" / "project.yml"
            local.parent.mkdir(parents=True)
            local.write_text(textwrap.dedent("""\
                project_name: configured-project
                language_servers:
                  - python
                ignored_paths:
                  - vendor/**
                ls_workspace_folders:
                  - .
                encoding: utf-8
                ls_specific_settings:
                  python:
                    diagnostics: workspace
                activation_command: touch should-not-run
                fixed_tools:
                  - replace_symbol_body
            """))
            runtime.mkdir()
            start_serena.write_runtime_config(runtime, runtime, [])
            with patch("start_serena.project_root", return_value=project):
                _, env, _ = start_serena.launch_spec(project, runtime, root / "state")
            ready, failures = start_serena.ensure_project_configs(
                start_serena.serena_cli_prefix(binary), env, runtime, [project],
            )
            self.assertEqual(ready, [project])
            self.assertEqual(failures, [])
            private = json.loads((start_serena.cache_folder(runtime, project) / "project.yml").read_text())
            self.assertEqual(private["project_name"], "configured-project")
            self.assertEqual(private["language_servers"], ["python"])
            self.assertEqual(private["ignored_paths"], ["vendor/**"])
            self.assertEqual(private["ls_workspace_folders"], ["."])
            self.assertEqual(private["encoding"], "utf-8")
            self.assertEqual(private["ls_specific_settings"], {"python": {"diagnostics": "workspace"}})
            self.assertNotIn("activation_command", private)
            self.assertNotIn("fixed_tools", private)
            selection = json.loads((start_serena.cache_folder(runtime, project) / start_serena.PROJECT_SELECTION_FILE).read_text())
            self.assertEqual(selection, {"ignored_paths": ["vendor/**"], "language_servers": ["python"]})

    def test_repository_project_config_rejects_symlink_and_workspace_escape(self):
        binary = start_serena.shutil.which("serena")
        if binary is None:
            self.skipTest("Serena executable is unavailable")
        prefix = start_serena.serena_cli_prefix(binary)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name, make_config, expected in (
                (
                    "symlink",
                    lambda project, outside: (project / ".serena/project.yml").symlink_to(outside),
                    "non-symlink",
                ),
                (
                    "escape",
                    lambda project, outside: (project / ".serena/project.yml").write_text(
                        "project_name: unsafe\nlanguage_servers: [python]\nls_workspace_folders: [../outside]\n"
                    ),
                    "escapes the project root",
                ),
            ):
                with self.subTest(name=name):
                    project = root / name
                    runtime = root / f"runtime-{name}"
                    outside = root / f"outside-{name}.yml"
                    (project / ".serena").mkdir(parents=True)
                    outside.write_text("project_name: outside\nlanguage_servers: [python]\n")
                    make_config(project, outside)
                    runtime.mkdir()
                    start_serena.write_runtime_config(runtime, runtime, [])
                    env = os.environ.copy()
                    env["SERENA_HOME"] = str(runtime)
                    ready, failures = start_serena.ensure_project_configs(prefix, env, runtime, [project])
                    self.assertEqual(ready, [])
                    self.assertEqual(len(failures), 1)
                    self.assertIn(expected, failures[0]["error"])
                    self.assertFalse((start_serena.cache_folder(runtime, project) / "project.yml").exists())

    def test_language_inference_uses_tracked_markers_and_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            listed = subprocess.CompletedProcess([], 0, "go.mod\ncmd/main.go\nscripts/tool.py\n", "")
            with patch("start_serena.subprocess.run", return_value=listed):
                self.assertEqual(start_serena.infer_languages(project), ["go"])

    def test_language_inference_falls_back_for_source_only_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            listed = subprocess.CompletedProcess([], 0, "src/tool.py\n", "")
            with patch("start_serena.subprocess.run", return_value=listed):
                self.assertEqual(start_serena.infer_languages(project), ["python"])

    def test_language_inference_supports_document_and_config_repositories(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            listed = subprocess.CompletedProcess([], 0, "README.md\nconfig/template.yaml\n", "")
            with patch("start_serena.subprocess.run", return_value=listed):
                self.assertEqual(start_serena.infer_languages(project), ["yaml", "markdown"])

    def test_project_creation_passes_inferred_languages_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "repo"
            project.mkdir()
            state = root / "state"

            def create(command, **kwargs):
                config = start_serena.cache_folder(state, project) / "project.yml"
                config.parent.mkdir(parents=True)
                config.write_text("project_name: repo\nlanguage_servers: [go, python]\n")
                return subprocess.CompletedProcess(command, 0, "created", "")

            with patch("start_serena.infer_languages", return_value=["go", "python"]):
                with patch("start_serena.subprocess.run", side_effect=create) as run:
                    ready, failures = start_serena.ensure_project_configs("/bin/serena", {}, state, [project])
            self.assertEqual(ready, [project])
            self.assertEqual(failures, [])
            self.assertEqual(run.call_args.args[0][-4:], ["--ls", "go", "--ls", "python"])

    def test_project_selectors_require_unambiguous_names(self):
        projects = [Path("/one/repo"), Path("/two/repo"), Path("/one/other")]
        self.assertEqual(start_serena.resolve_projects(["other"], projects), [Path("/one/other")])
        with self.assertRaisesRegex(ValueError, "ambiguous Serena project"):
            start_serena.resolve_projects(["repo"], projects)
        with self.assertRaisesRegex(ValueError, "unknown Serena project"):
            start_serena.resolve_projects(["absent"], projects)

    def test_targeted_doctor_does_not_configure_unrelated_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = root / "state"
            parent = root / "codebase"
            target = parent / "service"
            target.mkdir(parents=True)
            identity = {
                "schema_version": 1,
                "serena_version": "Serena 1.7.0",
                "platform": "linux",
                "machine": "x86_64",
                "sha256": "c" * 64,
                "key": "v1-cccccccccccccccc",
            }
            launch = (["/bin/python", "/bootstrap", "start-mcp-server"], {"PATH": os.environ.get("PATH", "")}, {})
            preflight = [{
                "project": str(target),
                "languages": ["go"],
                "resolved_executables": {"go": "/bin/go", "gopls": "/bin/gopls"},
                "missing_executables": [],
                "status": "ready",
            }]
            with patch.dict(os.environ, {"SERENA_CODE_STATE_ROOT": str(state)}):
                with patch("start_serena.project_root", side_effect=[None, target]):
                    with patch("start_serena.launch_spec", return_value=launch):
                        with patch("start_serena.runtime_identity", return_value=identity):
                            with patch("start_serena.discover_projects", return_value=[parent]):
                                with patch("start_serena.ensure_project_configs", return_value=([target], [])) as ensure:
                                    with patch("start_serena.preflight_projects", return_value=preflight):
                                        with patch("start_serena.run_project_command", return_value=[{
                                            "project": str(target), "status": "ok", "detail": "18 symbols",
                                        }]):
                                            with patch("sys.argv", ["start_serena.py", "--doctor", str(target)]):
                                                with patch("builtins.print") as output:
                                                    self.assertEqual(start_serena.main(), 0)
            self.assertEqual(ensure.call_args.args[3], [target])
            document = json.loads(output.call_args.args[0])
            self.assertEqual(document["target_failures"], [])
            self.assertEqual(document["unrelated_discovery_failures"], [])
            self.assertEqual(document["unrelated_discovery"], [{
                "project": str(parent),
                "status": "not_evaluated",
                "reason": "outside the explicit target set",
            }])
            self.assertEqual(document["results"][0]["status"], "ok")

    def test_doctor_missing_gopls_never_runs_semantic_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = root / "state"
            target = root / "service"
            target.mkdir()
            identity = {
                "schema_version": 1,
                "serena_version": "Serena 1.7.0",
                "platform": "linux",
                "machine": "x86_64",
                "sha256": "d" * 64,
                "key": "v1-dddddddddddddddd",
            }
            launch = (["/bin/python", "/bootstrap", "start-mcp-server"], {"PATH": os.environ.get("PATH", "")}, {})
            preflight = [{
                "project": str(target),
                "languages": ["go"],
                "resolved_executables": {"go": "/bin/go"},
                "missing_executables": ["gopls"],
                "status": "blocked",
            }]
            with patch.dict(os.environ, {"SERENA_CODE_STATE_ROOT": str(state)}):
                with patch("start_serena.project_root", return_value=target):
                    with patch("start_serena.launch_spec", return_value=launch):
                        with patch("start_serena.runtime_identity", return_value=identity):
                            with patch("start_serena.discover_projects", return_value=[target]):
                                with patch("start_serena.ensure_project_configs", return_value=([target], [])):
                                    with patch("start_serena.preflight_projects", return_value=preflight):
                                        with patch("start_serena.run_project_command") as run:
                                            with patch("sys.argv", ["start_serena.py", "--doctor", str(target)]):
                                                with patch("builtins.print") as output:
                                                    self.assertEqual(start_serena.main(), 1)
            run.assert_not_called()
            document = json.loads(output.call_args.args[0])
            self.assertEqual(document["results"][0]["status"], "blocked")
            self.assertIn("gopls", document["results"][0]["detail"])

    def test_doctor_runs_a_real_single_file_symbol_query(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            source = project / "main.go"
            source.write_text("package main\nfunc main() {}\n")
            listed = subprocess.CompletedProcess([], 0, "main.go\n", "")
            queried = subprocess.CompletedProcess([], 0, "Successfully indexed file\n", "")
            with patch("start_serena.subprocess.run", side_effect=[listed, queried]) as run:
                results = start_serena.run_project_command("/bin/serena", {}, "doctor", [project])
            self.assertEqual(results[0]["status"], "ok")
            self.assertEqual(run.call_args_list[1].args[0][:3], ["/bin/serena", "project", "index-file"])

    def test_doctor_skips_ignored_yaml_and_selects_configured_go_source(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            ignored = project / ".ci" / "pipelines" / "ci.yaml"
            source = project / "pkg" / "config.go"
            ignored.parent.mkdir(parents=True)
            source.parent.mkdir(parents=True)
            ignored.write_text("steps: []\n")
            source.write_text("package pkg\n")
            listed = subprocess.CompletedProcess([], 0, ".ci/pipelines/ci.yaml\npkg/config.go\n", "")
            queried = subprocess.CompletedProcess([], 0, "18 symbols\n", "")
            profiles = {
                str(project): {"languages": ["go"], "ignored_paths": [".ci/**"]},
            }
            with patch("start_serena.filter_ignored_sources", return_value=["pkg/config.go"]):
                with patch("start_serena.subprocess.run", side_effect=[listed, queried]) as run:
                    results = start_serena.run_project_command(
                        "/bin/serena", {}, "doctor", [project], project_profiles=profiles,
                    )
            self.assertEqual(results[0]["status"], "ok")
            self.assertEqual(run.call_args_list[1].args[0][3], str(source))

    def test_unknown_configured_language_falls_back_to_broad_source_suffixes(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            source = project / "src" / "Main.java"
            source.parent.mkdir()
            source.write_text("class Main {}\n")
            listed = subprocess.CompletedProcess([], 0, "src/Main.java\n", "")
            with patch("start_serena.subprocess.run", return_value=listed):
                self.assertEqual(start_serena.first_source_file(project, ["java"]), source)

    def test_serena_ignore_filter_handles_nested_directory_and_negation(self):
        binary = start_serena.shutil.which("serena")
        if binary is None:
            self.skipTest("Serena executable is unavailable")
        paths = [
            "docs/a.yaml",
            "docs/nested/b.yaml",
            "generated/drop.go",
            "generated/keep.go",
            "pkg/main.go",
        ]
        selected = start_serena.filter_ignored_sources(
            start_serena.serena_cli_prefix(binary),
            os.environ.copy(),
            paths,
            ["docs/*.yaml", "generated/", "!generated/keep.go"],
        )
        self.assertEqual(selected, ["docs/nested/b.yaml", "generated/keep.go", "pkg/main.go"])

    def test_doctor_accepts_tracked_runtime_managed_language_files(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            source = project / "README.md"
            source.write_text("# Project\n")
            listed = subprocess.CompletedProcess([], 0, "README.md\n", "")
            with patch("start_serena.subprocess.run", return_value=listed):
                self.assertEqual(start_serena.first_source_file(project), source)

    def test_two_connections_share_only_versioned_runtime_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = root / "state"
            first = root / "session-one"
            second = root / "session-two"
            first.mkdir()
            second.mkdir()
            identity = {
                "schema_version": 1,
                "serena_version": "Serena 1.7.0",
                "platform": "linux",
                "machine": "x86_64",
                "sha256": "a" * 64,
                "key": "v1-aaaaaaaaaaaaaaaa",
            }
            calls = []

            def prepare(preparation_home):
                calls.append(preparation_home)
                (preparation_home / "language_servers/static/marksman").write_text("ready")

            with patch("start_serena.runtime_identity", return_value=identity):
                first_cache = start_serena.prepare_runtime_resources("/bin/serena", state, first, ["markdown"], prepare)
                second_cache = start_serena.prepare_runtime_resources("/bin/serena", state, second, ["markdown"], prepare)
            project = root / "project"
            project.mkdir()
            for session in (first, second):
                start_serena.write_runtime_config(session, session, [])

                def create(command, env, **kwargs):
                    config = json.loads((Path(env["SERENA_HOME"]) / "serena_config.yml").read_text())
                    self.assertEqual(config["project_serena_folder_location"], str(session / "projects") + "/$projectDir/.serena")
                    project_cache = start_serena.cache_folder(session, project)
                    project_cache.mkdir(parents=True)
                    (project_cache / "project.yml").write_text("project_name: project\n")
                    return subprocess.CompletedProcess(command, 0, "created", "")

                with patch("start_serena.infer_languages", return_value=["go"]):
                    with patch("start_serena.subprocess.run", side_effect=create):
                        ready, failures = start_serena.ensure_project_configs("/bin/serena", {"SERENA_HOME": str(session)}, session, [project])
                self.assertEqual(ready, [project])
                self.assertEqual(failures, [])
                start_serena.write_runtime_config(session, session, ready)
            self.assertEqual(first_cache["status"], "prepared")
            self.assertEqual(second_cache["status"], "reused")
            self.assertEqual(len(calls), 1)
            first_static = first / "language_servers/static"
            second_static = second / "language_servers/static"
            self.assertNotEqual(first_static.resolve(), second_static.resolve())
            self.assertEqual(
                (first_static / "marksman").resolve(),
                (second_static / "marksman").resolve(),
            )
            first_config = json.loads((first / "serena_config.yml").read_text())
            second_config = json.loads((second / "serena_config.yml").read_text())
            self.assertNotEqual(first_config["project_serena_folder_location"], second_config["project_serena_folder_location"])
            self.assertNotEqual(start_serena.cache_folder(first, project), start_serena.cache_folder(second, project))
            self.assertEqual(first_config["projects"], [str(project)])
            self.assertEqual(second_config["projects"], [str(project)])
            self.assertFalse((first / "logs").is_symlink())
            self.assertFalse((second / "logs").is_symlink())

    def test_concurrent_runtime_preparation_uses_os_lock_and_publishes_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = root / "state"
            sessions = [root / "session-one", root / "session-two"]
            for session in sessions:
                session.mkdir()
            identity = {
                "schema_version": 1,
                "serena_version": "Serena 1.7.0",
                "platform": "linux",
                "machine": "x86_64",
                "sha256": "b" * 64,
                "key": "v1-bbbbbbbbbbbbbbbb",
            }
            try:
                context = multiprocessing.get_context("fork")
            except ValueError:
                self.skipTest("fork multiprocessing is unavailable on this platform")
            calls = context.Value("i", 0)
            queue = context.Queue()
            barrier = context.Barrier(2)

            def prepare(preparation_home):
                with calls.get_lock():
                    calls.value += 1
                time.sleep(0.05)
                (preparation_home / "language_servers/static/yaml-server").write_text("complete")

            def run(session):
                try:
                    barrier.wait(timeout=5)
                    result = start_serena.prepare_runtime_resources("/bin/serena", state, session, ["yaml"], prepare)
                    queue.put({"status": result["status"]})
                except BaseException as exc:
                    queue.put({"error": repr(exc)})

            with patch("start_serena.runtime_identity", return_value=identity):
                processes = [context.Process(target=run, args=(session,)) for session in sessions]
                for process in processes:
                    process.start()
                for process in processes:
                    process.join(10)
            self.assertTrue(all(process.exitcode == 0 for process in processes))
            results = [queue.get(timeout=2) for _ in processes]
            self.assertFalse(any("error" in result for result in results), results)
            self.assertEqual(calls.value, 1)
            self.assertEqual(sorted(result["status"] for result in results), ["prepared", "reused"])
            self.assertEqual(
                (sessions[0] / "language_servers/static/yaml-server").resolve(),
                (sessions[1] / "language_servers/static/yaml-server").resolve(),
            )

    def test_go_connection_keeps_cold_python_activation_writes_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = root / "state"
            session = root / "go-session"
            session.mkdir()
            go_project = root / "go-project"
            python_project = root / "python-project"
            go_project.mkdir()
            python_project.mkdir()
            identity = {
                "schema_version": 1,
                "serena_version": "Serena 1.7.0",
                "platform": "linux",
                "machine": "x86_64",
                "sha256": "e" * 64,
                "key": "v1-eeeeeeeeeeeeeeee",
            }
            reports = [
                {"project": str(go_project), "languages": ["go"], "missing_executables": []},
                {"project": str(python_project), "languages": ["python"], "missing_executables": []},
            ]
            with patch("start_serena.runtime_identity", return_value=identity):
                cache = start_serena.prepare_runtime_resources("/bin/serena", state, session, ["go"])
            registered = start_serena.registrable_projects([go_project, python_project], reports, cache["manifest"])
            self.assertEqual(registered, [go_project])
            for project in registered:
                folder = start_serena.cache_folder(session, project)
                folder.mkdir(parents=True)
                (folder / "project.yml").write_text("project_name: private\n")
            start_serena.write_runtime_config(session, session, registered)
            config = json.loads((session / "serena_config.yml").read_text())
            self.assertEqual(config["projects"], [str(go_project)])
            private_download = session / "language_servers/static/cold-python-runtime"
            private_download.mkdir()
            (private_download / "ready").write_text("private")
            shared_generation = Path(cache["generation"])
            self.assertFalse((shared_generation / "static/cold-python-runtime").exists())

    def test_warm_seed_is_persistent_and_restored_as_private_writable_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = root / "state"
            project = root / "project"
            first = root / "first-session"
            second = root / "second-session"
            project.mkdir()
            first.mkdir()
            second.mkdir()
            identity = {
                "schema_version": 1,
                "project": str(project),
                "revision": "abc123",
                "project_config_sha256": "config-hash",
                "saved_worktree_sha256": "worktree-hash",
                "sha256": "f" * 64,
            }
            first_cache = start_serena.cache_folder(first, project)
            first_cache.mkdir(parents=True)
            (first_cache / "project.yml").write_text("project_name: project\n")
            (first_cache / "cache").mkdir()
            (first_cache / "cache/symbols.json").write_text("warm-symbols")
            with patch("start_serena.project_seed_identity", return_value=identity):
                published = start_serena.publish_project_seed(state, first, project)
                second_cache = start_serena.cache_folder(second, project)
                second_cache.mkdir(parents=True)
                (second_cache / "project.yml").write_text("project_name: project\n")
                restored = start_serena.restore_project_seed(state, second, project)
            self.assertEqual(published["status"], "published")
            self.assertEqual(restored["status"], "restored")
            self.assertEqual((second_cache / "cache/symbols.json").read_text(), "warm-symbols")
            (second_cache / "cache/symbols.json").write_text("connection-private")
            seed_file = Path(published["seed"]) / "state/cache/symbols.json"
            self.assertEqual(seed_file.read_text(), "warm-symbols")
            self.assertEqual((first_cache / "cache/symbols.json").read_text(), "warm-symbols")

    def test_two_launcher_processes_restore_warm_state_and_isolate_cold_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "yaml-project"
            state = root / "state"
            records = root / "records"
            tools = root / "tools"
            project.mkdir()
            records.mkdir()
            tools.mkdir()
            (project / "config.yaml").write_text("enabled: true\n")
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            subprocess.run(["git", "-C", str(project), "add", "config.yaml"], check=True)
            subprocess.run([
                "git", "-C", str(project), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-qm", "fixture",
            ], check=True)

            fake_modules = root / "fake-modules"
            fake_package = fake_modules / "serena"
            (fake_package / "config").mkdir(parents=True)
            (fake_package / "__init__.py").write_text("")
            (fake_package / "config" / "__init__.py").write_text("")
            (fake_package / "config" / "serena_config.py").write_text(textwrap.dedent("""\
                class SerenaConfig:
                    def get_configured_project_serena_folder(self, project_root):
                        return project_root

                    def add_project_from_path(self, project_root, asynchronous_autogen=False):
                        raise NotImplementedError
            """))
            (fake_package / "cli.py").write_text(textwrap.dedent("""\
                import json
                import os
                from pathlib import Path
                import sys

                def top_level():
                    args = sys.argv[1:]
                    if args == ["--version"]:
                        print("Serena 1.7.0-fake")
                        return 0
                    home = Path(os.environ["SERENA_HOME"])
                    config = json.loads((home / "serena_config.yml").read_text())

                    def project_state(project):
                        location = config["project_serena_folder_location"].replace("$projectDir", str(Path(project).resolve()))
                        return Path(location)

                    if args[:2] == ["project", "create"]:
                        state = project_state(args[2])
                        state.mkdir(parents=True, exist_ok=True)
                        (state / "project.yml").write_text("project_name: yaml-project\\nlanguage_servers:\\n- yaml\\n")
                        print("created")
                        return 0
                    if args[:2] == ["project", "index"]:
                        state = project_state(args[2])
                        (state / "cache").mkdir(parents=True, exist_ok=True)
                        (state / "cache" / "warm.seed").write_text("warm")
                        resource = home / "language_servers" / "static" / "yaml-server"
                        resource.mkdir(parents=True, exist_ok=True)
                        (resource / "ready").write_text("ready")
                        print("indexed")
                        return 0
                    if args and args[0] == "start-mcp-server":
                        project = config["projects"][0]
                        state = project_state(project)
                        resource_meta = json.loads((home / "runtime-resources.json").read_text())
                        generation = Path(resource_meta["seed_generation"])
                        overlay = home / "language_servers" / "static"
                        cold = overlay / "cold-python-runtime"
                        cold.mkdir()
                        (cold / "ready").write_text("private")
                        record = {
                            "home": str(home),
                            "project_state": str(state),
                            "warm_seed_seen": (state / "cache" / "warm.seed").is_file(),
                            "generation": str(generation),
                            "resource_target": str((overlay / "yaml-server").resolve()),
                            "overlay_is_symlink": overlay.is_symlink(),
                            "cold_shared_exists": (generation / "static" / "cold-python-runtime").exists(),
                        }
                        output = Path(os.environ["FAKE_RECORD_DIR"]) / f"{os.getpid()}.json"
                        output.write_text(json.dumps(record))
                        return 0
                    raise SystemExit("unsupported fake Serena invocation: " + repr(args))
            """))
            site_marker = root / "site-customization-ran"
            (fake_modules / "sitecustomize.py").write_text(textwrap.dedent("""\
                import os
                from pathlib import Path
                marker = os.environ.get("SITE_CUSTOMIZATION_MARKER")
                if marker:
                    with Path(marker).open("a") as stream:
                        stream.write("loaded\\n")
            """))

            fake_serena = tools / "serena"
            fake_serena.write_text(textwrap.dedent(f"""\
                #!{sys.executable}
                import json
                import os
                from pathlib import Path
                import sys

                args = sys.argv[1:]
                if args == ["--version"]:
                    print("Serena 1.7.0-fake")
                    raise SystemExit(0)

                home = Path(os.environ["SERENA_HOME"])
                config = json.loads((home / "serena_config.yml").read_text())

                def project_state(project):
                    location = config["project_serena_folder_location"].replace("$projectDir", str(Path(project).resolve()))
                    return Path(location)

                if args[:2] == ["project", "create"]:
                    state = project_state(args[2])
                    state.mkdir(parents=True, exist_ok=True)
                    (state / "project.yml").write_text("project_name: yaml-project\\nlanguage_servers:\\n- yaml\\n")
                    print("created")
                    raise SystemExit(0)

                if args[:2] == ["project", "index"]:
                    state = project_state(args[2])
                    (state / "cache").mkdir(parents=True, exist_ok=True)
                    (state / "cache" / "warm.seed").write_text("warm")
                    resource = home / "language_servers" / "static" / "yaml-server"
                    resource.mkdir(parents=True, exist_ok=True)
                    (resource / "ready").write_text("ready")
                    print("indexed")
                    raise SystemExit(0)

                if args and args[0] == "start-mcp-server":
                    project = config["projects"][0]
                    state = project_state(project)
                    resource_meta = json.loads((home / "runtime-resources.json").read_text())
                    generation = Path(resource_meta["seed_generation"])
                    overlay = home / "language_servers" / "static"
                    cold = overlay / "cold-python-runtime"
                    cold.mkdir()
                    (cold / "ready").write_text("private")
                    record = {{
                        "home": str(home),
                        "project_state": str(state),
                        "warm_seed_seen": (state / "cache" / "warm.seed").is_file(),
                        "generation": str(generation),
                        "resource_target": str((overlay / "yaml-server").resolve()),
                        "overlay_is_symlink": overlay.is_symlink(),
                        "cold_shared_exists": (generation / "static" / "cold-python-runtime").exists(),
                    }}
                    output = Path(os.environ["FAKE_RECORD_DIR"]) / f"{{os.getpid()}}.json"
                    output.write_text(json.dumps(record))
                    raise SystemExit(0)

                raise SystemExit("unsupported fake Serena invocation: " + repr(args))
            """))
            fake_serena.chmod(0o755)
            for executable in ("node", "npm"):
                path = tools / executable
                path.write_text("#!/bin/sh\nexit 0\n")
                path.chmod(0o755)

            launcher = str(Path(start_serena.__file__).resolve())
            env = os.environ.copy()
            env.update({
                "SERENA_EXECUTABLE": str(fake_serena),
                "SERENA_CODE_ROOTS": str(project),
                "SERENA_CODE_STATE_ROOT": str(state),
                "FAKE_RECORD_DIR": str(records),
                "SITE_CUSTOMIZATION_MARKER": str(site_marker),
                "PATH": str(tools) + os.pathsep + env.get("PATH", ""),
                "PYTHONPATH": str(fake_modules),
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            warmed = subprocess.run(
                [sys.executable, launcher, "--warm", str(project)],
                cwd=project, env=env, capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(warmed.returncode, 0, warmed.stderr or warmed.stdout)
            warm_report = json.loads(warmed.stdout)
            self.assertEqual(warm_report["project_seeds"][0]["status"], "published")

            launchers = [subprocess.Popen(
                [sys.executable, launcher], cwd=project, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ) for _ in range(2)]
            outputs = [process.communicate(timeout=30) for process in launchers]
            self.assertTrue(all(process.returncode == 0 for process in launchers), outputs)
            runtime_records = [json.loads(path.read_text()) for path in sorted(records.glob("*.json"))]
            self.assertEqual(len(runtime_records), 2)
            self.assertNotEqual(runtime_records[0]["home"], runtime_records[1]["home"])
            self.assertNotEqual(runtime_records[0]["project_state"], runtime_records[1]["project_state"])
            self.assertEqual(runtime_records[0]["generation"], runtime_records[1]["generation"])
            self.assertEqual(runtime_records[0]["resource_target"], runtime_records[1]["resource_target"])
            self.assertTrue(all(record["warm_seed_seen"] for record in runtime_records))
            self.assertTrue(all(not record["overlay_is_symlink"] for record in runtime_records))
            self.assertTrue(all(not record["cold_shared_exists"] for record in runtime_records))
            self.assertTrue(site_marker.is_file())
            self.assertGreaterEqual(len(site_marker.read_text().splitlines()), 3)

    def test_binding_distinguishes_exact_worktree_unbound_and_wrong_worktree(self):
        expected = Path("/worktrees/expected")
        other = Path("/worktrees/other")
        self.assertEqual(start_serena.binding_report(expected, expected)["status"], "bound_exact_worktree")
        self.assertEqual(start_serena.binding_report(None, expected)["status"], "unbound")
        with patch("start_serena.git_common_dir", return_value=Path("/repo/.git")):
            report = start_serena.binding_report(other, expected)
        self.assertEqual(report["status"], "wrong_worktree")
        self.assertIn("shell cd does not retarget", report["guidance"])

    def test_lease_classification_never_age_deletes_active_or_unknown_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity = {"key": "runtime"}
            with patch("start_serena.process_identity", return_value={"status": "active", "identity": "birth-1"}):
                session, _ = start_serena.create_session(root / "sessions", identity, None)
            hostname = json.loads((session / "lease.json").read_text())["hostname"]
            active = start_serena.classify_session(
                session,
                host=hostname,
                probe=lambda pid: {"status": "active", "identity": "birth-1"},
            )
            abandoned = start_serena.classify_session(
                session,
                host=hostname,
                probe=lambda pid: {"status": "absent", "identity": ""},
            )
            unknown_dir = root / "sessions" / "mcp-unknown"
            unknown_dir.mkdir()
            unknown = start_serena.classify_session(unknown_dir, host=hostname)
            self.assertEqual(active["classification"], "active")
            self.assertEqual(active["proposed_action"], "preserve")
            self.assertEqual(abandoned["classification"], "abandoned_candidate")
            self.assertEqual(abandoned["proposed_action"], "review_then_remove")
            self.assertEqual(unknown["classification"], "unknown")
            self.assertTrue(session.exists())
            self.assertTrue(unknown_dir.exists())

    def test_real_process_identity_transitions_from_active_to_abandoned_candidate(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            observed = start_serena.process_identity(child.pid)
            if observed["status"] != "active":
                self.skipTest("platform cannot expose a stable process start identity")
            with tempfile.TemporaryDirectory() as directory:
                session = Path(directory).resolve() / "mcp-crashed"
                session.mkdir()
                lease = {
                    "schema_version": start_serena.SESSION_LEASE_SCHEMA,
                    "owner": start_serena.SESSION_OWNER,
                    "session_id": "test-crash",
                    "runtime_dir": str(session),
                    "hostname": start_serena.socket.gethostname(),
                    "runtime_identity": {"key": "runtime"},
                    "processes": [{"role": "serena", "pid": child.pid, "identity": observed["identity"]}],
                }
                (session / "lease.json").write_text(json.dumps(lease))
                self.assertEqual(start_serena.classify_session(session)["classification"], "active")
                child.terminate()
                child.wait(timeout=5)
                self.assertEqual(start_serena.classify_session(session)["classification"], "abandoned_candidate")
                self.assertTrue(session.exists())
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)

    def test_runtime_registry_is_isolated_and_code_tools_are_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            original_pythonpath = os.environ.get("PYTHONPATH")
            with patch("start_serena.project_root", return_value=root / "repo"):
                with patch("start_serena.shutil.which", side_effect=["/bin/serena", "/bin/gopls"]):
                    with patch("start_serena.serena_cli_prefix", return_value=["/bin/python", "/bootstrap"]):
                        command, env, config = start_serena.launch_spec(root, root / "session", root / "state")
            self.assertEqual(command[-2:], ["--project", str(root / "repo")])
            self.assertEqual(env["SERENA_HOME"], str(root / "session"))
            self.assertNotIn("SERENA_STRICT_PROJECT_STATE_ROOT", env)
            self.assertEqual(env.get("PYTHONPATH"), original_pythonpath)
            self.assertEqual(env["SERENA_USAGE_REPORTING"], "false")
            self.assertEqual(env["GOTELEMETRY"], "off")
            self.assertEqual(config["projects"], [])
            self.assertEqual(config["base_modes"], [])
            self.assertFalse(config["web_dashboard"])
            self.assertEqual(config["default_modes"], ["no-memories"])
            self.assertIn("$projectDir", config["project_serena_folder_location"])
            context = (start_serena.ASSETS / "code-navigation.yml").read_text()
            self.assertIn("single_project: false", context)
            for tool in start_serena.TOOLS:
                self.assertIn(f"  - {tool}\n", context)
            self.assertNotIn("  - execute_shell_command\n", context)
            self.assertNotIn("  - replace_symbol_body\n", context)

    def test_check_reports_discovery_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "absent"
            roots = Path(directory) / "roots"
            (roots / "tmp" / "scratch" / ".git").mkdir(parents=True)
            (roots / "app" / ".git").mkdir(parents=True)
            env = {"SERENA_CODE_STATE_ROOT": str(state), "SERENA_CODE_ROOTS": str(roots)}
            with patch.dict(os.environ, env):
                with patch("start_serena.project_root", return_value=None):
                    with patch("start_serena.shutil.which", return_value="/bin/tool"):
                        with patch("start_serena.serena_cli_prefix", return_value=["/bin/python", "/bootstrap"]):
                            with patch("start_serena.runtime_identity", return_value={"key": "v1-test"}):
                                with patch("sys.argv", ["start_serena.py", "--check"]):
                                    with patch("builtins.print") as output:
                                        start_serena.main()
            document = json.loads(output.call_args.args[0])
            self.assertEqual(document["discovered_projects"], 1)
            self.assertIn({"path": str(roots / "tmp"), "pattern": "tmp", "reason": "temporary directory"},
                          document["discovery_exclusions"])

    def test_check_has_no_state_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "absent"
            with patch.dict(os.environ, {"SERENA_CODE_STATE_ROOT": str(state)}):
                with patch("start_serena.project_root", return_value=None):
                    with patch("start_serena.shutil.which", return_value="/bin/tool"):
                        with patch("start_serena.serena_cli_prefix", return_value=["/bin/python", "/bootstrap"]):
                            with patch("start_serena.runtime_identity", return_value={"key": "v1-test"}):
                                with patch("sys.argv", ["start_serena.py", "--check"]):
                                    with patch("builtins.print") as output:
                                        self.assertEqual(start_serena.main(), 1)
            document = json.loads(output.call_args.args[0])
            self.assertIsNone(document["project"])
            self.assertEqual(document["binding"]["status"], "unbound")
            self.assertFalse(state.exists())

    def test_cleanup_plan_is_read_only_for_absent_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "absent"
            with patch.dict(os.environ, {"SERENA_CODE_STATE_ROOT": str(state)}):
                with patch("sys.argv", ["start_serena.py", "--cleanup-plan"]):
                    with patch("builtins.print") as output:
                        self.assertEqual(start_serena.main(), 0)
            document = json.loads(output.call_args.args[0])
            self.assertFalse(document["automatic_deletion"])
            self.assertEqual(document["cleanup_candidates"], [])
            self.assertFalse(state.exists())


class NavigationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = start_serena.ASSETS.parent
        cls.skill = " ".join((root / "SKILL.md").read_text().split())
        cls.context = " ".join((root / "assets/code-navigation.yml").read_text().split())

    def test_symbol_queries_take_priority_over_broad_search(self):
        for phrase in (
            "use Serena before grep-based symbol navigation or broad file reads",
            "switch to a bounded symbol query",
            "`find_referencing_symbols` for callers/usages",
            "`find_implementations` for interface implementations",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.skill)

    def test_text_and_historical_search_do_not_require_serena(self):
        for phrase in (
            "Use ordinary search for unknown names or paths, string literals",
            "configuration, documentation, generated/vendor sources",
            "exact historical revisions not active in Serena",
            "tiny known-file fixes on existing tools",
            "Normal text/config searches need no fallback warning",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.skill)

    def test_readiness_requires_current_agent_query_not_inventory(self):
        for phrase in (
            "check this session's own tool list once",
            "another agent's successful call does not prove this agent can call Serena",
            "Each child uses its own tool surface and working tree",
            "complete the task-relevant symbol query",
            "Activation with no active language server is not symbol readiness",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.skill)

    def test_fallback_preserves_evidence_and_recovery_boundaries(self):
        for phrase in (
            "state the observed reason once",
            "An empty result alone does not prove Serena is unavailable",
            "Do not auto-reconnect MCP, restart servers",
            "do not use a shell wrapper as an MCP substitute",
            "Resume symbol navigation when the limitation is resolved",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.skill)

    def test_discovery_and_warming_remain_bounded(self):
        for phrase in (
            "Only selected projects enter the private connection registry",
            "Repository discovery does not start language servers",
            "warm symbol caches only for explicit projects",
            "These commands never run repository builds",
            "Duplicate directory names require absolute-path selectors",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.skill)

    def test_mcp_context_preserves_navigation_priority(self):
        self.assertIn("Prefer symbol tools", self.context)
        self.assertIn("complete the task-relevant symbol query", self.context)
        self.assertIn("Use ordinary search", self.context)
        self.assertIn("state the observed reason once", self.context)
        self.assertIn("do not auto-reconnect or restart servers", self.context)


if __name__ == "__main__":
    unittest.main()
