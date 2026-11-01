#!/usr/bin/env python3
"""Portable tests for the fleet remote runner."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock


MODULE_PATH = Path(__file__).with_name("remote_runner.py")
SPEC = importlib.util.spec_from_file_location(
    "remote_runner", MODULE_PATH
)
assert SPEC and SPEC.loader
RR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RR
SPEC.loader.exec_module(RR)


def command(
    command_id: str,
    argv: List[str],
    *,
    cwd: str = ".",
    timeout_seconds: float | None = None,
) -> Dict[str, Any]:
    value: Dict[str, Any] = {"id": command_id, "argv": argv, "cwd": cwd}
    if timeout_seconds is not None:
        value["timeout_seconds"] = timeout_seconds
    return value


class RemoteRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def manifest(
        self,
        *,
        steps: List[Dict[str, Any]] | None = None,
        artifacts: List[Dict[str, Any]] | None = None,
        restoration_steps: List[Dict[str, Any]] | None = None,
        restoration_checks: List[Dict[str, Any]] | None = None,
        policy: Dict[str, float] | None = None,
        state_dir: Path | None = None,
        work_dir: Path | None = None,
        task_id: str = "portable-task",
        session: str | None = None,
    ) -> Dict[str, Any]:
        state = state_dir or (self.root / "state")
        work = work_dir or (self.root / "work")
        value = {
            "schema_version": 1,
            "task_id": task_id,
            "remote": {
                "host": "host.example",
                "state_dir": str(state),
                "work_dir": str(work),
                "session": session or f"rr-{task_id}",
            },
            "steps": steps
            or [
                command(
                    "work",
                    [sys.executable, "-c", "from pathlib import Path; Path('done').write_text('ok')"],
                )
            ],
            "restoration": {
                "steps": restoration_steps or [],
                "checks": restoration_checks
                or [command("restored", [sys.executable, "-c", "raise SystemExit(0)"])],
            },
            "artifacts": artifacts or [],
            "policy": {
                "heartbeat_interval_seconds": 0.05,
                "heartbeat_timeout_seconds": 0.5,
                "stall_timeout_seconds": 5.0,
                "task_timeout_seconds": 10.0,
                "default_step_timeout_seconds": 5.0,
                "restoration_timeout_seconds": 2.0,
                "terminate_grace_seconds": 0.1,
                **(policy or {}),
            },
        }
        return RR.validate_manifest(value)

    def write_remote_manifest(self, manifest: Dict[str, Any]) -> Path:
        state_dir = Path(manifest["remote"]["state_dir"])
        state_dir.mkdir(parents=True)
        RR.atomic_write_json(state_dir / "manifest.json", manifest)
        return state_dir

    def run_task(self, manifest: Dict[str, Any]) -> tuple[Path, Dict[str, Any]]:
        state_dir = self.write_remote_manifest(manifest)
        terminal = RR.remote_run(state_dir)
        return state_dir, terminal

    def assert_launch_not_materialized(self, manifest: Dict[str, Any]) -> None:
        state_dir = Path(manifest["remote"]["state_dir"])
        self.assertFalse(state_dir.exists())
        self.assertFalse(Path(manifest["remote"]["work_dir"]).exists())
        self.assertEqual(
            [], list(state_dir.parent.glob(f".{state_dir.name}.prestage-*"))
        )

    def test_command_arguments_and_launch_paths_are_quoted_safely(self) -> None:
        literal = "spaces ' quotes ; $(must-not-run) *"
        artifact = "artifact with spaces.txt"
        state_dir = self.root / "state ' ; $(not-run)"
        work_dir = self.root / "work ' ; $(not-run)"
        script = (
            "from pathlib import Path; import sys; "
            f"Path({artifact!r}).write_text(sys.argv[1], encoding='utf-8')"
        )
        manifest = self.manifest(
            state_dir=state_dir,
            work_dir=work_dir,
            steps=[command("literal-argv", [sys.executable, "-c", script, literal])],
            artifacts=[{"path": artifact, "required": True}],
        )
        completed_state, terminal = self.run_task(manifest)
        self.assertEqual(terminal["status"], "succeeded")
        self.assertEqual((work_dir / artifact).read_text(encoding="utf-8"), literal)

        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        capture = self.root / "tmux-argv.json"
        fake_tmux = fake_bin / "tmux"
        fake_tmux.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "if sys.argv[1] == 'has-session':\n"
            "    raise SystemExit(1)\n"
            "json.dump(sys.argv[1:], open(os.environ['RR_TMUX_CAPTURE'], 'w'))\n",
            encoding="utf-8",
        )
        fake_tmux.chmod(0o755)
        fresh_manifest = self.manifest(
            state_dir=self.root / "launch state ' ; $(not-run)",
            work_dir=self.root / "launch work ' ; $(not-run)",
            task_id="quoted-launch",
        )
        remote_command = RR.build_remote_launch_command(
            fresh_manifest, MODULE_PATH.read_bytes()
        )
        launch_payload = RR.build_launch_payload(
            fresh_manifest, MODULE_PATH.read_bytes()
        )
        environment = dict(os.environ)
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        environment["RR_TMUX_CAPTURE"] = str(capture)
        completed = subprocess.run(
            ["sh", "-c", remote_command],
            input=launch_payload,
            env=environment,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        expected_tmux_argv = RR.detached_tmux_argv(fresh_manifest)[1:]
        expected_tmux_argv[5] = shutil.which(
            RR.REMOTE_PYTHON_EXECUTABLE, path=environment["PATH"]
        )
        self.assertEqual(json.loads(capture.read_text()), expected_tmux_argv)
        uploaded = RR.load_manifest(
            Path(fresh_manifest["remote"]["state_dir"]) / "manifest.json"
        )
        self.assertEqual(uploaded, fresh_manifest)
        self.assertTrue(completed_state.joinpath("terminal.json").is_file())

    def test_tmux_session_collision_fails_before_writing_state(self) -> None:
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        fake_tmux = fake_bin / "tmux"
        fake_tmux.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = -V ]; then exit 0; fi\n"
            "if [ \"$1\" = has-session ]; then exit 0; fi\n"
            "exit 99\n",
            encoding="utf-8",
        )
        fake_tmux.chmod(0o755)
        manifest = self.manifest(task_id="collision")
        remote_command = RR.build_remote_launch_command(
            manifest, MODULE_PATH.read_bytes()
        )
        launch_payload = RR.build_launch_payload(
            manifest, MODULE_PATH.read_bytes()
        )
        environment = dict(os.environ)
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        completed = subprocess.run(
            ["/bin/sh", "-c", remote_command],
            input=launch_payload,
            env=environment,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 73)
        self.assertIn(b"tmux session collision", completed.stderr)
        self.assertFalse(Path(manifest["remote"]["state_dir"]).exists())

    def test_python_version_preflight_fails_before_remote_writes(self) -> None:
        manifest = self.manifest(task_id="old-python")
        unsupported = (sys.version_info.major + 1, 0)
        with mock.patch.object(RR, "MIN_REMOTE_PYTHON_VERSION", unsupported):
            remote_command = RR.build_remote_launch_command(
                manifest, MODULE_PATH.read_bytes()
            )

        completed = subprocess.run(
            ["sh", "-c", remote_command],
            input=RR.build_launch_payload(manifest, MODULE_PATH.read_bytes()),
            capture_output=True,
            check=False,
        )

        observed = ".".join(str(component) for component in sys.version_info[:3])
        required = ".".join(str(component) for component in unsupported)
        self.assertEqual(completed.returncode, 70)
        self.assertIn(
            f"required >= {required}; observed {observed}".encode(), completed.stderr
        )
        self.assert_launch_not_materialized(manifest)

    def test_python_module_preflight_preserves_import_failure(self) -> None:
        manifest = self.manifest(task_id="missing-module")
        missing_module = "rr_module_that_must_not_exist"
        with mock.patch.object(
            RR,
            "REQUIRED_REMOTE_PYTHON_MODULES",
            RR.REQUIRED_REMOTE_PYTHON_MODULES + (missing_module,),
        ):
            remote_command = RR.build_remote_launch_command(
                manifest, MODULE_PATH.read_bytes()
            )

        completed = subprocess.run(
            ["sh", "-c", remote_command],
            input=RR.build_launch_payload(manifest, MODULE_PATH.read_bytes()),
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 71)
        self.assertIn(
            (
                "remote capability preflight failed: Python module import "
                f"{missing_module}: ModuleNotFoundError: No module named "
                f"'{missing_module}'"
            ).encode(),
            completed.stderr,
        )
        self.assert_launch_not_materialized(manifest)

    def test_tmux_preflight_preserves_probe_failure_before_remote_writes(self) -> None:
        fake_bin = self.root / "tmux-preflight-bin"
        fake_bin.mkdir()
        (fake_bin / "python3").symlink_to(sys.executable)
        (fake_bin / "sh").symlink_to("/bin/sh")
        unexpected = self.root / "unexpected-tmux-command"
        fake_tmux = fake_bin / "tmux"
        fake_tmux.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = -V ]; then\n"
            "  printf 'synthetic tmux loader failure\\n' >&2\n"
            "  exit 42\n"
            "fi\n"
            "printf '%s\\n' \"$*\" > \"$RR_TMUX_UNEXPECTED\"\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_tmux.chmod(0o755)
        manifest = self.manifest(task_id="broken-tmux")
        remote_command = RR.build_remote_launch_command(
            manifest, MODULE_PATH.read_bytes()
        )
        environment = dict(os.environ)
        environment["PATH"] = str(fake_bin)
        environment["RR_TMUX_UNEXPECTED"] = str(unexpected)

        completed = subprocess.run(
            ["/bin/sh", "-c", remote_command],
            input=RR.build_launch_payload(manifest, MODULE_PATH.read_bytes()),
            env=environment,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 72)
        self.assertIn(b"tmux probe exited 42", completed.stderr)
        self.assertIn(b"synthetic tmux loader failure", completed.stderr)
        self.assertFalse(unexpected.exists())
        self.assert_launch_not_materialized(manifest)

    def test_launch_payload_checksum_mismatch_fails_before_writing_state(self) -> None:
        fake_bin = self.root / "checksum-bin"
        fake_bin.mkdir()
        fake_tmux = fake_bin / "tmux"
        fake_tmux.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = -V ]; then exit 0; fi\n"
            "exit 99\n",
            encoding="utf-8",
        )
        fake_tmux.chmod(0o755)
        manifest = self.manifest(task_id="corrupt-launch")
        runner_bytes = MODULE_PATH.read_bytes()
        remote_command = RR.build_remote_launch_command(manifest, runner_bytes)
        launch_payload = bytearray(RR.build_launch_payload(manifest, runner_bytes))
        launch_payload[-1] ^= 1
        environment = dict(os.environ)
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"

        completed = subprocess.run(
            ["sh", "-c", remote_command],
            input=bytes(launch_payload),
            env=environment,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 75)
        self.assertIn(b"launch payload checksum mismatch", completed.stderr)
        self.assertFalse(Path(manifest["remote"]["state_dir"]).exists())

    def test_existing_state_is_not_overwritten_or_left_with_staging(self) -> None:
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_tmux = fake_bin / "tmux"
        fake_tmux.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = -V ]; then exit 0; fi\n"
            "if [ \"$1\" = has-session ]; then exit 1; fi\n"
            "exit 99\n",
            encoding="utf-8",
        )
        fake_tmux.chmod(0o755)
        manifest = self.manifest(task_id="existing-state")
        state_dir = Path(manifest["remote"]["state_dir"])
        state_dir.mkdir(parents=True)
        sentinel = state_dir / "sentinel"
        sentinel.write_text("keep", encoding="utf-8")
        remote_command = RR.build_remote_launch_command(
            manifest, MODULE_PATH.read_bytes()
        )
        environment = dict(os.environ)
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"

        completed = subprocess.run(
            ["sh", "-c", remote_command],
            input=RR.build_launch_payload(manifest, MODULE_PATH.read_bytes()),
            env=environment,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 74)
        self.assertIn(b"remote state directory already exists", completed.stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertEqual([], list(state_dir.parent.glob(f".{state_dir.name}.prestage-*")))

    def test_launch_payload_is_hashed_and_kept_out_of_ssh_argv(self) -> None:
        manifest = self.manifest(task_id="stdin-launch")
        runner_bytes = (b"large-runner-payload-" * 20_000) + b"\n"
        payload = RR.build_launch_payload(manifest, runner_bytes)
        envelope = json.loads(RR.zlib.decompress(payload))
        normalized_manifest = (
            json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")

        self.assertEqual(
            RR.base64.b64decode(envelope["manifest"], validate=True),
            normalized_manifest,
        )
        self.assertEqual(
            RR.base64.b64decode(envelope["runner"], validate=True),
            runner_bytes,
        )
        self.assertEqual(
            envelope["manifest_sha256"],
            RR.hashlib.sha256(normalized_manifest).hexdigest(),
        )
        self.assertEqual(
            envelope["runner_sha256"],
            RR.hashlib.sha256(runner_bytes).hexdigest(),
        )

        plan = RR.build_launch_plan(manifest, runner_bytes)
        self.assertEqual(
            plan["stdin_payload_sha256"],
            RR.hashlib.sha256(payload).hexdigest(),
        )
        self.assertEqual(plan["stdin_payload_size_bytes"], len(payload))
        self.assertLess(sum(len(argument) + 1 for argument in plan["ssh_argv"]), 8_192)

    def test_poll_recovers_after_ssh_transport_disconnect(self) -> None:
        manifest = self.manifest()
        state_dir, _ = self.run_task(manifest)
        responses: List[Any] = [
            RR.TransportError("connection dropped"),
            RR.remote_status(state_dir),
        ]

        def fetch() -> Dict[str, Any]:
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        result = RR.poll_until_terminal(
            fetch,
            interval_seconds=0.01,
            max_wait_seconds=1.0,
            sleep=lambda _: None,
        )
        self.assertEqual(result["effective_status"], "succeeded")
        self.assertEqual(result["transport_errors"], 1)

    def test_log_without_progress_triggers_stall_failure(self) -> None:
        manifest = self.manifest(
            steps=[
                command(
                    "silent",
                    [sys.executable, "-c", "import time; time.sleep(5)"],
                    timeout_seconds=3.0,
                )
            ],
            policy={
                "stall_timeout_seconds": 0.25,
                "restoration_timeout_seconds": 60.0,
            },
        )
        state_dir, terminal = self.run_task(manifest)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["reason"], "log_stalled")
        status = RR.remote_status(state_dir)
        self.assertTrue(status["complete"])
        self.assertEqual(status["effective_status"], "failed")
        self.assertTrue(status["restoration_receipt"]["success"])

    def test_success_and_failure_terminal_markers(self) -> None:
        success_manifest = self.manifest(task_id="success")
        success_state, success = self.run_task(success_manifest)
        self.assertEqual(success["status"], "succeeded")
        self.assertEqual(RR.remote_status(success_state)["effective_status"], "succeeded")

        failure_manifest = self.manifest(
            task_id="failure",
            state_dir=self.root / "failure-state",
            work_dir=self.root / "failure-work",
            steps=[command("fail", [sys.executable, "-c", "raise SystemExit(7)"])],
        )
        failure_state, failure = self.run_task(failure_manifest)
        self.assertEqual(failure["status"], "failed")
        failure_status = RR.remote_status(failure_state)
        self.assertTrue(failure_status["complete"])
        self.assertEqual(failure_status["effective_status"], "failed")

    def test_spawn_failure_still_finalizes_with_restoration_receipt(self) -> None:
        manifest = self.manifest(
            steps=[command("missing", ["definitely-not-a-real-executable"])],
        )
        state_dir, terminal = self.run_task(manifest)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["reason"], "spawn_failed")
        status = RR.remote_status(state_dir)
        self.assertTrue(status["complete"])
        self.assertEqual(status["effective_status"], "failed")
        self.assertTrue(status["restoration_receipt"]["success"])

    def test_artifact_receipt_contains_verified_hash(self) -> None:
        payload = "durable artifact\n"
        artifact = "result/output.txt"
        script = (
            "from pathlib import Path; "
            "Path('result').mkdir(); "
            f"Path({artifact!r}).write_text({payload!r}, encoding='utf-8')"
        )
        manifest = self.manifest(
            steps=[command("produce", [sys.executable, "-c", script])],
            artifacts=[{"path": artifact, "required": True}],
        )
        state_dir, terminal = self.run_task(manifest)
        receipt = RR.read_json(state_dir / "artifact-receipt.json")
        expected_hash = __import__("hashlib").sha256(payload.encode()).hexdigest()
        self.assertEqual(receipt["artifacts"][0]["sha256"], expected_hash)
        self.assertEqual(
            terminal["artifact_receipt_sha256"],
            RR.file_sha256(state_dir / "artifact-receipt.json"),
        )

    def test_artifact_symlink_escape_finalizes_as_failure(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        script = (
            "from pathlib import Path; "
            f"Path('result').symlink_to({str(outside)!r})"
        )
        manifest = self.manifest(
            steps=[command("produce-link", [sys.executable, "-c", script])],
            artifacts=[{"path": "result", "required": True}],
        )

        state_dir, terminal = self.run_task(manifest)

        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["reason"], "required_artifact_missing")
        status = RR.remote_status(state_dir)
        self.assertTrue(status["complete"])
        self.assertEqual(status["effective_status"], "failed")
        receipt = RR.read_json(state_dir / "artifact-receipt.json")
        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["artifacts"][0]["error_type"], "SupervisorError")

    def test_observer_exit_does_not_affect_remote_runner(self) -> None:
        artifact = "observer-independent.txt"
        script = (
            "import time; from pathlib import Path; "
            "time.sleep(0.8); "
            f"Path({artifact!r}).write_text('finished', encoding='utf-8')"
        )
        manifest = self.manifest(
            steps=[command("delayed", [sys.executable, "-c", script])],
            artifacts=[{"path": artifact, "required": True}],
        )
        state_dir = self.write_remote_manifest(manifest)
        runner = subprocess.Popen(
            [
                sys.executable,
                str(MODULE_PATH),
                "_remote-run",
                "--state-dir",
                str(state_dir),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 2.0
        while not (state_dir / "state.json").is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        observer = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "_remote-status",
                "--state-dir",
                str(state_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(observer.returncode, 0, observer.stderr)
        self.assertIsNone(runner.poll())
        self.assertEqual(runner.wait(timeout=5), 0)
        assert runner.stderr is not None
        runner_stderr = runner.stderr.read().decode("utf-8", errors="replace")
        runner.stderr.close()
        self.assertEqual(runner_stderr, "")
        self.assertEqual(RR.remote_status(state_dir)["effective_status"], "succeeded")

    def test_missing_restoration_receipt_cannot_complete(self) -> None:
        manifest = self.manifest()
        state_dir, terminal = self.run_task(manifest)
        self.assertEqual(terminal["status"], "succeeded")
        (state_dir / "restoration-receipt.json").unlink()
        status = RR.remote_status(state_dir)
        self.assertFalse(status["complete"])
        self.assertEqual(status["effective_status"], "integrity_error")
        self.assertIn("missing restoration-receipt.json", status["issues"])

    def test_self_advancing_steps_do_not_require_an_observer_decision(self) -> None:
        first = command(
            "first",
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('round-one').write_text('ready')",
            ],
        )
        second = command(
            "second",
            [
                sys.executable,
                "-c",
                "from pathlib import Path; "
                "assert Path('round-one').read_text() == 'ready'; "
                "Path('round-two').write_text('advanced')",
            ],
        )
        manifest = self.manifest(
            steps=[first, second],
            artifacts=[{"path": "round-two", "required": True}],
        )
        state_dir, terminal = self.run_task(manifest)
        self.assertEqual(terminal["status"], "succeeded")
        events = (state_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertLess(events.index('"command_id":"first"'), events.index('"command_id":"second"'))

    def test_credentials_and_missing_restoration_checks_are_rejected(self) -> None:
        raw = self.manifest()
        raw["steps"][0]["env"] = {"API_TOKEN": "must-not-persist"}
        with self.assertRaisesRegex(RR.SupervisorError, "credential-like"):
            RR.validate_manifest(raw)

        raw = self.manifest(
            state_dir=self.root / "other-state",
            work_dir=self.root / "other-work",
        )
        raw["steps"][0]["argv"].append("--password=must-not-persist")
        with self.assertRaisesRegex(RR.SupervisorError, "inline credential"):
            RR.validate_manifest(raw)

        for argv in (
            ["curl", "-H", "Authorization: Basic Zm9vOmJhcg==", "https://example.test"],
            ["curl", "-H", "Cookie:", "session=opaque", "https://example.test"],
            ["tool", "--client_secret", "opaque"],
            ["tool", "--api-key=synthetic"],
        ):
            with self.subTest(argv=argv):
                raw = self.manifest(
                    state_dir=self.root / f"credential-{len(argv)}-{argv[0]}",
                    work_dir=self.root / f"credential-work-{len(argv)}-{argv[0]}",
                )
                raw["steps"][0]["argv"] = argv
                with self.assertRaisesRegex(
                    RR.SupervisorError,
                    "credential|inline",
                ):
                    RR.validate_manifest(raw)

        raw = self.manifest(
            state_dir=self.root / "third-state",
            work_dir=self.root / "third-work",
        )
        raw["restoration"]["checks"] = []
        with self.assertRaisesRegex(RR.SupervisorError, "explicit check"):
            RR.validate_manifest(raw)

    def test_sync_verification_rejects_missing_terminal_chain(self) -> None:
        manifest = self.manifest()
        destination = self.root / "incomplete-sync"
        control = destination / "control"
        control.mkdir(parents=True)
        RR.atomic_write_json(control / "manifest.json", manifest)

        with self.assertRaisesRegex(
            RR.SupervisorError,
            "integrity validation failed",
        ):
            RR.verify_synced_artifacts(destination)
        self.assertFalse((destination / "sync-receipt.json").exists())

    def test_sync_verification_checks_terminal_chain_and_artifacts(self) -> None:
        payload = "portable receipt\n"
        script = (
            "from pathlib import Path; "
            f"Path('result.txt').write_text({payload!r}, encoding='utf-8')"
        )
        manifest = self.manifest(
            steps=[command("produce", [sys.executable, "-c", script])],
            artifacts=[{"path": "result.txt", "required": True}]
        )
        state_dir, terminal = self.run_task(manifest)
        self.assertEqual(terminal["status"], "succeeded")

        destination = self.root / "complete-sync"
        destination.mkdir()
        control = destination / "control"
        control.mkdir()
        for filename in RR.CONTROL_FILES:
            source = state_dir / filename
            if source.is_file():
                (control / filename).write_bytes(source.read_bytes())
        artifact_dir = destination / "artifacts"
        artifact_dir.mkdir()
        (artifact_dir / "result.txt").write_bytes(
            Path(manifest["remote"]["work_dir"])
            .joinpath("result.txt")
            .read_bytes()
        )

        sync_receipt = RR.verify_synced_artifacts(destination)

        self.assertEqual(sync_receipt["terminal_status"], "succeeded")
        self.assertEqual(sync_receipt["verified_artifacts"][0]["path"], "result.txt")

    def test_launch_defaults_to_dry_run(self) -> None:
        manifest = self.manifest()
        manifest_path = self.root / "local-manifest.json"
        RR.atomic_write_json(manifest_path, manifest)
        completed = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "launch",
                "--manifest",
                str(manifest_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        plan = json.loads(completed.stdout)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(
            plan["ssh_argv"][0:3],
            ["ssh", "-o", "BatchMode=yes"],
        )
        self.assertEqual(plan["detached_tmux_argv"][0:3], ["tmux", "new-session", "-d"])
        self.assertEqual(
            plan["remote_capability_preflight"]["python"]["minimum_version"],
            "3.9",
        )
        self.assertEqual(
            plan["remote_capability_preflight"]["python"]["required_modules"],
            list(RR.REQUIRED_REMOTE_PYTHON_MODULES),
        )
        self.assertEqual(
            plan["remote_capability_preflight"]["tmux"]["probe_argv"], ["-V"]
        )
        self.assertFalse(Path(manifest["remote"]["state_dir"]).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
