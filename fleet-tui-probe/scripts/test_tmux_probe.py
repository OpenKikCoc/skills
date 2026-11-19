#!/usr/bin/env python3
"""Portable positive and negative tests for the minimal read-only tmux probe."""

from __future__ import annotations

import json
import os
import subprocess
import time
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent


def capture(*sessions: tuple[str, str, str]) -> str:
    rows = []
    for name, command, pane in sessions:
        rows.append(f"@@SESSION {name} {command}")
        rows.extend(line for line in pane.splitlines() if line.strip())
        rows.append("@@END")
    return "\n".join(rows) + "\n"


def run_capture(text: str, extra_env: dict[str, str] | None = None):
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "capture.txt"
        path.write_text(text, encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("PROBE_")}
        env.update(TMPDIR=temporary)
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(SCRIPTS / "probe.sh"), "--from-capture", str(path)],
            capture_output=True, text=True, check=False, env=env,
        )


class ProbeTests(unittest.TestCase):
    def test_shared_pane_fixture_corpus(self):
        corpus = json.loads((SCRIPTS.parent / "assets/fixtures/panes.json").read_text())
        for fixture in corpus["fixtures"]:
            with self.subTest(fixture=fixture["id"]):
                result = run_capture(capture((fixture["id"], fixture["command"], fixture["pane"])))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.split("|")[1:3],
                                 [fixture["state"], fixture.get("attention", "-")])

    def run_remote(self, mode, extra_args=(), extra_env=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ssh = root / "ssh"
            ssh.write_text("#!/bin/bash\n"
                           "if [ \"$FAKE_MODE\" = many ]; then for i in $(seq 1 80); do printf '@@SESSION s%s codex\\n@@DATA 4peGIERvbmUuCg==\\n@@END\\n' \"$i\"; done; exit 0; fi\n"
                           "if [ \"$FAKE_MODE\" = secret ] || [ \"$FAKE_MODE\" = redaction ]; then printf 'Authorization: Bearer FAKE_TOKEN_NOT_A_SECRET\\n' >&2; sleep 0.1; exit 255; fi\n"
                           "if [ \"$FAKE_MODE\" = transport ]; then exit 255; fi\n"
                           "if [ \"$FAKE_MODE\" = remote-error ]; then exit 2; fi\n"
                           "if [ \"$FAKE_MODE\" = hang ]; then exec sleep 0.6; fi\n"
                           "bash -c \"${!#}\"\n")
            tmux = root / "tmux"
            tmux.write_text("#!/bin/bash\n"
                            "[ \"$FAKE_MODE\" != slow ] || sleep 0.12\n"
                            "if [ \"$FAKE_MODE\" = socket ]; then printf 'permission denied: socket\\n' >&2; exit 1; fi\n"
                            "case $1 in\n"
                            "list-sessions) [ \"$FAKE_MODE\" != list-error ] || exit 1; printf 'present\\n';;\n"
                            "list-panes) [ \"$FAKE_MODE\" != pane-error ] || exit 1; printf 'codex\\n';;\n"
                            "capture-pane) [ \"$FAKE_MODE\" != capture-error ] || exit 1; "
                            "if [ \"$FAKE_MODE\" = marker ]; then "
                            "if [ \"$FAKE_MARKER\" = '@@END' ]; then printf '%s\\n• Working (2s • esc to interrupt)\\n' \"$FAKE_MARKER\"; "
                            "else printf '• Working (2s • esc to interrupt)\\n%s\\n' \"$FAKE_MARKER\"; fi; "
                            "elif [ \"$FAKE_MODE\" != empty ]; then printf '◆ Done.\\n'; fi;;\n"
                            "*) exit 2;;\nesac\n")
            ssh.chmod(0o755)
            tmux.chmod(0o755)
            env = {key: value for key, value in os.environ.items() if not key.startswith("PROBE_")}
            env.update(PATH=str(root) + os.pathsep + env["PATH"], FAKE_MODE=mode,
                       TMPDIR=temporary)
            env.update(extra_env or {})
            args = ["bash", str(SCRIPTS / "probe.sh"), "--host", "fake",
                    "--sessions", "s*" if mode == "many" else "present,missing", *extra_args]
            if mode != "secret":
                return subprocess.run(args, capture_output=True, text=True, env=env, timeout=3)
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, env=env, umask=int(env["FAKE_UMASK"]))
            seen = False
            private = True
            while process.poll() is None:
                for path in root.rglob("*"):
                    if path.name.endswith(".err") and path.is_file() and "FAKE_TOKEN" in path.read_text(errors="replace"):
                        seen = True
                        private &= path.stat().st_mode & 0o077 == 0
                        private &= path.parent.stat().st_mode & 0o077 == 0
                time.sleep(0.005)
            stdout, stderr = process.communicate(timeout=3)
            self.assertTrue(seen, "fake transport must exercise file-backed stderr")
            self.assertTrue(private, "transport error files and their directory must be private")
            return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)

    def test_live_reserved_markers_are_pane_data(self):
        for marker in ("@@GONE present", "@@SESSION injected bash", "@@END"):
            with self.subTest(marker=marker):
                result = self.run_remote("marker", extra_env={"FAKE_MARKER": marker})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count("present|"), 1)
                self.assertIn("present|working|", result.stdout)
                self.assertNotIn("injected|", result.stdout)

    def test_fast_ssh_many_panes_has_end_to_end_deadline(self):
        started = time.monotonic()
        result = self.run_remote("many", ("--timeout-seconds", "0.15"))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(result.returncode, 3)
        self.assertIn("observer/transport timeout", result.stderr)
        self.assertNotIn("|gone|", result.stdout)

    def test_malformed_control_records_fail_closed(self):
        for text in ("@@SESSION x codex\n@@GONE x\n@@END\n",
                     capture(("x", "codex", "Done"), ("x", "codex", "Done")),
                     "@@END\n", "@@SESSION x codex\nDone\n"):
            with self.subTest(text=text):
                result = run_capture(text)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_live_control_identity_and_encoding_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ssh = root / "ssh"
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"], TMPDIR=temporary)
            for text in ("@@GONE injected\n", "@@SESSION injected codex\n@@DATA \n@@END\n",
                         "@@SESSION present codex\n@@DATA invalid!\n@@END\n",
                         "@@GONE present\n@@GONE present\n", "@@SESSION present codex\n@@END\n"):
                with self.subTest(text=text):
                    ssh.write_text("#!/usr/bin/env python3\nprint(" + repr(text) + ", end='')\n")
                    ssh.chmod(0o755)
                    result = subprocess.run(["bash", str(SCRIPTS / "probe.sh"), "--host", "fake",
                                             "--sessions", "present"], env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 3)
                    self.assertIn("probe_error", result.stdout + result.stderr)
                    self.assertNotIn("|gone|", result.stdout)

    def test_raw_probe_nonreading_stdout_is_bounded(self):
        import base64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ssh = root / "ssh"
            payload = base64.b64encode(b"x" * 131072).decode()
            ssh.write_text("#!/usr/bin/env python3\nprint(" +
                           repr("@@SESSION present codex\n@@DATA " + payload + "\n@@END") + ")\n")
            ssh.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"], TMPDIR=temporary)
            process = subprocess.Popen(["bash", str(SCRIPTS / "probe.sh"), "--host", "fake",
                                        "--sessions", "present", "--raw", "--timeout-seconds", "0.15"],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            try:
                process.wait(timeout=0.8)
                stdout, stderr = process.communicate()
                self.assertEqual(process.returncode, 3)
                self.assertIn(b"observer/transport timeout", stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_transport_error_text_never_forwarded(self):
        result = self.run_remote("redaction")
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("FAKE_TOKEN", result.stdout + result.stderr)
        self.assertNotIn("Authorization:", result.stdout + result.stderr)

    def test_transport_errors_are_private_and_redacted(self):
        for mask in (0o022, 0o000):
            with self.subTest(mask=mask):
                result = self.run_remote("secret", extra_env={"FAKE_UMASK": str(mask)})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("FAKE_TOKEN", result.stdout + result.stderr)

    def test_collection_errors_are_not_gone(self):
        for mode in ("list-error", "pane-error", "capture-error", "transport", "socket", "remote-error"):
            with self.subTest(mode=mode):
                result = self.run_remote(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("|probe_error|", result.stdout)
                self.assertNotIn("|gone|", result.stdout)

    def test_only_successful_enumeration_proves_gone(self):
        result = self.run_remote("empty")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("present|unknown|", result.stdout)
        self.assertIn("missing|gone|", result.stdout)

    def test_whole_collection_deadline(self):
        started = time.monotonic()
        result = self.run_remote("hang", ("--timeout-seconds", "0.15"))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(result.returncode, 3)
        self.assertIn("observer/transport timeout", result.stderr)
        self.assertIn("|probe_error|", result.stdout)

    def test_collection_budget_is_not_reset_per_tmux_call(self):
        result = self.run_remote("slow", ("--timeout-seconds", "0.2"))
        self.assertEqual(result.returncode, 3)
        self.assertIn("observer/transport timeout", result.stderr)
        self.assertNotIn("|gone|", result.stdout)

    def test_rejects_invalid_deadline_before_ssh(self):
        for value in ("0", "-1", "nan", "inf", "invalid"):
            with self.subTest(value=value):
                result = self.run_remote("transport", ("--timeout-seconds", value))
                self.assertEqual(result.returncode, 2)
                self.assertIn("finite and positive", result.stderr)

    def test_remote_probe_has_non_login_path_fallback(self):
        source = (SCRIPTS / "probe.sh").read_text(encoding="utf-8")
        self.assertIn("command -v tmux", source)
        for candidate in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"):
            self.assertIn(candidate, source)
        self.assertIn("remote tmux executable not found", source)

    def test_missing_tmux_is_probe_error_not_gone(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ssh = root / "ssh"
            ssh.write_text("#!/bin/bash\nPATH=/nonexistent /bin/bash -c \"${!#}\"\n")
            ssh.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"], TMPDIR=temporary)
            result = subprocess.run(["bash", str(SCRIPTS / "probe.sh"), "--host", "fake",
                                     "--sessions", "missing"], env=env, capture_output=True,
                                    text=True, timeout=5)
            if Path("/usr/bin/tmux").exists() or Path("/usr/local/bin/tmux").exists() \
                    or Path("/opt/homebrew/bin/tmux").exists():
                self.skipTest("a real tmux exists at a fallback path")
            self.assertEqual(result.returncode, 3)
            self.assertIn("missing|probe_error|", result.stdout)
            self.assertNotIn("|gone|", result.stdout)

    def test_classifies_working_idle_approval_and_question_from_recent_tail(self):
        working = """• Working (12m 3s • esc to interrupt)
› Implement the change
  gpt-5-codex high · Context 61% left
"""
        claude_working = """✻ Pondering… (42s · ↑ 1.2k tokens · esc to interrupt)
> Implement the change
  ? for shortcuts
"""
        idle = """◆ Done.
─ Worked for 14m 18s · 15:49 ─
› Next request
  gpt-5-codex high · Context 18% left
"""
        approval = """ Bash command
   rm -rf build
 Do you want to proceed?
 ❯ 1. Yes
   2. No, and tell Claude what to do differently (esc)
"""
        question = """Which rollout should I take?
  tab to navigate questions · enter to submit all
"""
        result = run_capture(capture(
            ("working", "codex", working),
            ("claude-working", "claude", claude_working),
            ("idle", "node", idle),
            ("approval", "claude", approval),
            ("question", "codex", question),
            ("shell", "bash", idle),
        ))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {line.split("|", 1)[0]: line.split("|")[1:]
                for line in result.stdout.splitlines()}
        self.assertEqual(rows["working"][0], "working")
        self.assertEqual(rows["claude-working"][0], "working")
        self.assertEqual(rows["idle"][:3], ["idle", "-", "18"])
        self.assertEqual(rows["approval"][:2], ["approval", "-"])
        self.assertEqual(rows["question"][:2], ["idle", "question"])
        self.assertEqual(rows["shell"][0], "exited")

    def test_queued_state_is_opt_in(self):
        pane = "Queued for capacity\n› Wait\n"
        default = run_capture(capture(("q", "codex", pane)))
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(default.stdout.split("|")[1], "idle")
        custom = run_capture(capture(("q", "codex", pane)),
                             {"PROBE_QUEUED_RE": "Queued for capacity"})
        self.assertEqual(custom.returncode, 0, custom.stderr)
        self.assertEqual(custom.stdout.split("|")[1], "queued")

    def test_history_approval_and_busy_low_context_do_not_request_attention(self):
        approval_history = "Permission required\n" + \
            "".join(f"output {index}\n" for index in range(25)) + "◆ Done.\n› Next\n"
        busy = "• Working (2m • esc to interrupt)\n› Wait\n  Context 5% left\n"
        result = run_capture(capture(
            ("history", "codex", approval_history),
            ("busy", "claude", busy),
        ))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {line.split("|", 1)[0]: line.split("|")[1:]
                for line in result.stdout.splitlines()}
        self.assertEqual(rows["history"][:2], ["idle", "-"])
        self.assertEqual(rows["busy"][:2], ["working", "-"])

    def test_bare_esc_to_footer_is_not_working(self):
        result = run_capture(capture(("f", "codex", "esc to cancel\n› Next\n")))
        self.assertEqual(result.stdout.split("|")[1], "idle")

    def test_events_are_read_from_last_twenty_nonblank_lines(self):
        pane = "Permission required\n" + "\n".join(f"line {i}" for i in range(19)) + "\n"
        near = run_capture(f"@@SESSION s codex\n{pane}@@END\n")
        self.assertEqual(near.stdout.split("|")[1], "approval")
        far = run_capture(f"@@SESSION s codex\n{pane}line x\n@@END\n")
        self.assertEqual(far.stdout.split("|")[1], "idle")

    def test_empty_pane_is_unknown_not_gone(self):
        result = run_capture(capture(("s", "codex", "")))
        self.assertEqual(result.stdout.split("|")[1], "unknown")

    def test_raw_view_decodes_and_indents_pane_text(self):
        result = self.run_remote("marker", ("--raw",), {"FAKE_MARKER": "@@GONE present"})
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "@@SESSION present codex")
        self.assertIn("    @@GONE present", lines)
        self.assertEqual(sum(1 for line in lines if line.startswith("@@")), 2)

    def test_rejects_unsafe_remote_arguments_before_ssh(self):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("PROBE_")}
        for args in (["--host", "bad host", "--sessions", "x"],
                     ["--host", "host", "--sessions", "x';rm"]):
            result = subprocess.run(
                ["bash", str(SCRIPTS / "probe.sh"), *args],
                capture_output=True, text=True, check=False, env=env,
            )
            self.assertEqual(result.returncode, 2)
            self.assertTrue(result.stderr.startswith("error:"), result.stderr)


if __name__ == "__main__":
    unittest.main()
