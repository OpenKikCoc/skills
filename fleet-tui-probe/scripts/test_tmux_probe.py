#!/usr/bin/env python3
"""Portable positive and negative tests for the minimal read-only tmux probe."""

from __future__ import annotations

import os
import subprocess
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
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(SCRIPTS / "probe.sh"), "--from-capture", str(path)],
            capture_output=True, text=True, check=False, env=env,
        )


class ProbeTests(unittest.TestCase):
    def test_classifies_working_idle_and_question_from_recent_tail(self):
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
        result = run_capture(capture(
            ("working", "codex", working),
            ("claude-working", "claude", claude_working),
            ("idle", "node", idle),
            ("approval", "claude", approval),
            ("shell", "bash", idle),
        ))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {line.split("|", 1)[0]: line.split("|")[1:]
                for line in result.stdout.splitlines()}
        self.assertEqual(rows["working"][0], "working")
        self.assertEqual(rows["claude-working"][0], "working")
        self.assertEqual(rows["idle"][:3], ["idle", "-", "18"])
        self.assertEqual(rows["approval"][:2], ["idle", "question"])
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
        approval_history = "approval review approved: git status\n" + \
            "".join(f"output {index}\n" for index in range(12)) + "◆ Done.\n❯ Next\n"
        busy = "• Working (2m • esc to interrupt)\n❯ Wait\n  Context 5% left\n"
        result = run_capture(capture(
            ("history", "codex", approval_history),
            ("busy", "claude", busy),
        ))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {line.split("|", 1)[0]: line.split("|")[1:]
                for line in result.stdout.splitlines()}
        self.assertEqual(rows["history"][:2], ["idle", "-"])
        self.assertEqual(rows["busy"][:2], ["working", "-"])

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
