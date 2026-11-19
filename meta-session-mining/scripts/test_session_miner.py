#!/usr/bin/env python3
"""Tests for session_miner using synthetic fixtures only."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session_miner as miner  # noqa: E402

HOME_DIR = "/ho" + "me/alice/project"


def codex_file(path, sid, asks, source="cli", parent=None, ts="2030-03-02T10:00:00Z"):
    meta = {"id": sid, "source": source, "thread_source": "user"}
    if parent:
        meta["forked_from_id"] = parent
    rows = [{"type": "session_meta", "timestamp": ts, "payload": meta}]
    rows.append({"type": "response_item", "timestamp": ts, "payload": {
        "type": "message", "role": "developer", "content": [{"type": "input_text", "text": "dev rules"}]}})
    for text in asks:
        rows.append({"type": "response_item", "timestamp": ts, "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def claude_file(path, sid, asks, sidechain=False, ts="2030-03-03T09:00:00.000Z"):
    rows = [{"type": "user", "sessionId": sid, "timestamp": ts, "isSidechain": sidechain,
             "message": {"role": "user", "content": a}} for a in asks]
    rows.append({"type": "assistant", "sessionId": sid, "timestamp": ts, "isSidechain": sidechain,
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


class RedactionTests(unittest.TestCase):
    def test_secrets_paths_emails(self):
        text = ("mail bob@example.com, file %s/a.py, key sk-abcdefghijklmnopqrstuvwx, "
                "curl -H 'Authorization: Bearer abcdefgh12345678' https://u:hunter2pass@example.com/x?token=abcd1234efgh" % HOME_DIR)  # skills-repo: allow-test-secret
        out = miner.redact(text)
        for leaked in ("bob@example.com", "alice", "sk-abcdef", "abcdefgh12345678", "hunter2pass", "abcd1234efgh"):
            self.assertNotIn(leaked, out)
        self.assertIn("<email>", out)
        self.assertIn("<path>", out)

    def test_plain_text_untouched(self):
        self.assertEqual(miner.redact("fix the parser please"), "fix the parser please")


class NormalizeTests(unittest.TestCase):
    def test_harness_blocks_dropped(self):
        self.assertIsNone(miner.normalize_ask("<environment_context>cwd</environment_context>"))
        self.assertIsNone(miner.normalize_ask("<command-name>/clear</command-name>"))
        self.assertIsNone(miner.normalize_ask("# AGENTS.md instructions for x"))
        self.assertEqual(miner.normalize_ask("<system-reminder>x</system-reminder>\nadd tests"), "add tests")


class MinerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def run_miner(self, *extra, fmt="json"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = miner.main(["--format", fmt, *extra])
        return code, out.getvalue(), err.getvalue()

    def report(self, *extra):
        code, out, _ = self.run_miner(*extra, str(self.root))
        self.assertEqual(code, 0)
        return json.loads(out)

    def test_both_adapters(self):
        codex_file(self.root / "rollout-a.jsonl", "c1", ["please add a retry to the uploader"])
        claude_file(self.root / "b.jsonl", "k1", ["rename the config flag", [{"type": "text", "text": "and update docs"}]])
        rep = self.report()
        texts = sorted(c["text"] for c in rep["candidates"])
        self.assertEqual(texts, ["and update docs", "please add a retry to the uploader", "rename the config flag"])
        self.assertEqual({a for c in rep["candidates"] for a in c["adapters"]}, {"codex", "claude"})

    def test_excludes_subagents_and_reviewers(self):
        codex_file(self.root / "rollout-sub.jsonl", "s1", ["worker prompt"], source={"subagent": {"parent": "x"}})
        claude_file(self.root / "side.jsonl", "s2", ["sidechain prompt"], sidechain=True)
        codex_file(self.root / "rollout-ok.jsonl", "p1", ["real ask"])
        rep = self.report()
        self.assertEqual([c["text"] for c in rep["candidates"]], ["real ask"])
        self.assertEqual(rep["summary"]["excluded_subagent"], 2)

    def test_tool_results_not_asks(self):
        claude_file(self.root / "t.jsonl", "k2", [[{"type": "tool_result", "content": "output"}], "do the thing"])
        rep = self.report()
        self.assertEqual([c["text"] for c in rep["candidates"]], ["do the thing"])

    def test_fork_dedupe_counts_lineage_once(self):
        codex_file(self.root / "rollout-p.jsonl", "root", ["shared ask"])
        codex_file(self.root / "rollout-f.jsonl", "fork", ["shared ask", "new ask"], parent="root")
        codex_file(self.root / "rollout-q.jsonl", "other", ["shared ask"])
        rep = self.report()
        by_text = {c["text"]: c for c in rep["candidates"]}
        self.assertEqual(len(rep["candidates"]), 2)
        self.assertEqual(by_text["shared ask"]["occurrence_count"], 2)  # root+fork is one lineage
        self.assertEqual(len(by_text["shared ask"]["session_ids"]), 3)

    def test_date_filter(self):
        codex_file(self.root / "rollout-o.jsonl", "o", ["old ask"], ts="2030-01-05T00:00:00Z")
        codex_file(self.root / "rollout-n.jsonl", "n", ["new ask"], ts="2030-03-05T00:00:00Z")
        rep = self.report("--since", "2030-02-01", "--until", "2030-03-31")
        self.assertEqual([c["text"] for c in rep["candidates"]], ["new ask"])

    def test_deterministic_and_read_only(self):
        codex_file(self.root / "rollout-a.jsonl", "c1", ["ask one", "ask two"])
        claude_file(self.root / "b.jsonl", "k1", ["ask three"])
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        first = self.run_miner(str(self.root))[1]
        second = self.run_miner(str(self.root / "b.jsonl"), str(self.root / "rollout-a.jsonl"))[1]
        self.assertEqual(first, second)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_malformed_lines_reported_not_echoed(self):
        path = self.root / "rollout-x.jsonl"
        codex_file(path, "c9", ["good ask"])
        with path.open("a") as handle:
            handle.write("{not json SECRET-LINE\n")
        code, out, _ = self.run_miner(str(self.root))
        self.assertEqual(code, 0)
        self.assertNotIn("SECRET-LINE", out)
        self.assertEqual(json.loads(out)["diagnostics"][0]["kind"], "malformed_lines")

    def test_seen_inventory_incremental(self):
        codex_file(self.root / "rollout-a.jsonl", "c1", ["ask one"])
        first = self.run_miner(str(self.root))[1]
        prior = self.root.parent / (self.root.name + "-prior.json")
        self.addCleanup(lambda: prior.exists() and prior.unlink())
        prior.write_text(first)
        rep = self.report("--seen", str(prior))
        self.assertEqual(rep["candidates"], [])
        self.assertEqual(rep["summary"]["already_seen_filtered"], 1)

    def test_markdown(self):
        codex_file(self.root / "rollout-a.jsonl", "c1", ["ask one"])
        code, out, _ = self.run_miner(str(self.root), fmt="markdown")
        self.assertEqual(code, 0)
        self.assertIn("> ask one", out)

    def test_bad_input_no_traceback(self):
        code, out, err = self.run_miner(str(self.root / "missing"))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
