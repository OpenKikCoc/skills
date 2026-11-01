#!/usr/bin/env python3
"""Tests for ledger.py (synthetic records only)."""

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger  # noqa: E402


def make(tid="T-1", date="2030-01-30", **over):
    t = ledger.scaffold(tid, date)
    t.update({
        "mode": "investigate-and-rollup", "title": "Jobs fail at start", "category": "compute",
        "environment": "prod-a", "executive_summary": "Jobs failed; cause confirmed.", "impact": "Batch jobs blocked",
        "source_evidence": {"locator": "chat-export/T-1.jsonl", "complete": True},
        "evidence_catalog": [
            {"id": "ev-1", "kind": "log", "scope": "mechanism", "source": "kubectl logs x", "summary": "OOM at init"},
            {"id": "ev-2", "kind": "post_action_observation", "scope": "recovery", "source": "kubectl get", "summary": "jobs run"},
        ],
        "facts": [{"claim": "init container OOMKilled", "evidence": ["ev-1"]}],
        "root_cause": {"status": "confirmed", "summary": "memory limit too low", "evidence_refs": ["ev-1"]},
        "open_questions": [],
        "rca": {"publish_status": "published", "agent_used": True, "accuracy": "confirmed",
                "accuracy_basis": "owner confirmed", "agent_summary": "found OOM", "material_corrections": []},
        "write_authorization": {"policy": "apply", "weekly_id": "w"},
    })
    t.update(over)
    return t


def write_ticket(ledger_dir, t):
    path = os.path.join(ledger_dir, "tickets", t["ticket_id"] + ".json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(t, fh)
    return path


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ledger.main(argv)
    return code, out.getvalue(), err.getvalue()


class ValidationTests(unittest.TestCase):
    def test_valid_and_scaffold(self):
        self.assertEqual(ledger.validate_ticket(make(), for_rollup=True), [])
        self.assertEqual(ledger.validate_ticket(ledger.scaffold("T-9", "2030-01-01")), [])

    def test_confirmed_needs_nonsymptom_evidence(self):
        t = make()
        t["evidence_catalog"][0]["scope"] = "symptom_only"
        self.assertTrue(any("symptom_only" in e for e in ledger.validate_ticket(t)))
        t = make(root_cause={"status": "confirmed", "summary": "x", "evidence_refs": []})
        self.assertTrue(any("requires evidence_refs" in e for e in ledger.validate_ticket(t)))

    def test_unknown_ref(self):
        t = make()
        t["facts"].append({"claim": "c", "evidence": ["ev-404"]})
        self.assertTrue(any("ev-404" in e for e in ledger.validate_ticket(t)))

    def test_resolved_gate(self):
        t = make(status="resolved")
        self.assertTrue(any("owner_verified" in e for e in ledger.validate_ticket(t)))
        t["recovery"] = {"plan_state": "owner_verified", "execution": "completed", "verification": "verified",
                         "checks": [{"description": "jobs run", "passed": True, "evidence_ref": "ev-2"}]}
        self.assertEqual(ledger.validate_ticket(t), [])
        t["recovery"]["checks"][0]["evidence_ref"] = "ev-1"
        self.assertTrue(any("post_action" in e for e in ledger.validate_ticket(t)))

    def test_accuracy_rules(self):
        t = make()
        t["rca"]["agent_used"] = False
        self.assertTrue(any("unreviewed" in e for e in ledger.validate_ticket(t)))
        t = make()
        t["rca"]["material_corrections"] = [{"kind": "actor", "previous_value": "a", "corrected_value": "b", "evidence_refs": ["ev-1"]}]
        self.assertTrue(any("material_corrections require accuracy" in e for e in ledger.validate_ticket(t)))
        t["rca"]["accuracy"] = "partially_confirmed"
        self.assertEqual(ledger.validate_ticket(t), [])

    def test_rollup_gates(self):
        for mutate, text in (
            (lambda t: t.update(mode="investigate-only"), "mode"),
            (lambda t: t["write_authorization"].update(policy="preview"), "apply"),
            (lambda t: t["rca"].update(publish_status="draft"), "published"),
            (lambda t: t["source_evidence"].update(complete=False), "complete"),
        ):
            t = make()
            mutate(t)
            self.assertTrue(any(text in e for e in ledger.validate_ticket(t, for_rollup=True)), text)

    def test_garbage_input(self):
        self.assertTrue(ledger.validate_ticket([1, 2]))
        self.assertTrue(ledger.validate_ticket({}))


class RollupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_metrics_and_idempotence(self):
        write_ticket(self.dir, make("T-1"))
        t2 = make("T-2", priority="P1", category="network")
        t2["rca"].update(accuracy="incorrect", accuracy_basis="owner disagreed")
        t2["rca"]["material_corrections"] = [{"kind": "actor", "previous_value": "a", "corrected_value": "b", "evidence_refs": ["ev-1"]}]
        write_ticket(self.dir, t2)
        t3 = make("T-3", category="network")
        t3["rca"].update(agent_used=False, accuracy="unreviewed", accuracy_basis="")
        write_ticket(self.dir, t3)
        t4 = make("T-4", mode="investigate-only")  # excluded
        write_ticket(self.dir, t4)
        write_ticket(self.dir, make("T-5", date="2030-03-01"))  # other week
        code, out, _ = run(["rollup", self.dir, "--week", "2030-W05"])
        self.assertEqual(code, 0)
        self.assertIn("Total tickets: 3", out)
        self.assertIn("P1=1", out)
        self.assertIn("| All | 3 | 2/3 (66.7%) | 1/2 (50.0%) |", out)
        self.assertIn("| category: network | 2 | 1/2 (50.0%) | 0/1 (0.0%) |", out)
        self.assertNotIn("T-4", out)
        self.assertNotIn("T-5", out)
        self.assertLess(out.index("T-2 | P1"), out.index("T-1 | P3"))
        self.assertEqual(run(["rollup", self.dir, "--week", "2030-W05", "--write"])[1].split()[0], "created")
        self.assertEqual(run(["rollup", self.dir, "--week", "2030-W05", "--write"])[1].split()[0], "noop")
        self.assertEqual(run(["rollup", self.dir, "--week", "2030-W05", "--check"])[0], 0)
        t3["title"] = "changed title"
        write_ticket(self.dir, t3)
        self.assertEqual(run(["rollup", self.dir, "--week", "2030-W05", "--check"])[0], 1)
        self.assertEqual(run(["rollup", self.dir, "--week", "2030-W05", "--write"])[1].split()[0], "updated")

    def test_fail_closed(self):
        bad = make("T-1")
        bad["rca"]["publish_status"] = "draft"
        write_ticket(self.dir, bad)
        code, _, err = run(["rollup", self.dir, "--week", "2030-W05", "--write"])
        self.assertEqual(code, 2)
        self.assertIn("fail closed", err)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "weekly")))

    def test_bad_input_no_traceback(self):
        code, _, err = run(["rollup", self.dir, "--week", "2030-W05"])
        self.assertEqual(code, 2)
        code, _, err = run(["rollup", self.dir, "--week", "nope"])
        self.assertEqual(code, 2)
        broken = os.path.join(self.dir, "bad.json")
        with open(broken, "w") as fh:
            fh.write("{oops")
        code, _, err = run(["validate", broken])
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)


class RenderAndNewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_render_preview_vs_apply(self):
        path = write_ticket(self.dir, make("T-1"))
        code, out, _ = run(["render-rca", path])
        self.assertEqual(code, 0)
        self.assertIn("## 5. Root cause analysis", out)
        self.assertIn("memory limit too low", out)
        code, out, _ = run(["render-rca", path, "--write", self.dir])
        self.assertEqual(out.split()[0], "created")
        self.assertEqual(run(["render-rca", path, "--write", self.dir])[1].split()[0], "noop")
        pv = make("T-2", write_authorization={"policy": "preview", "weekly_id": ""})
        path = write_ticket(self.dir, pv)
        code, _, err = run(["render-rca", path, "--write", self.dir])
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "rca", "T-2.md")))

    def test_new_scaffold_validates_but_not_rollup(self):
        code, _, _ = run(["new", self.dir, "T-7", "--date", "2030-01-02"])
        self.assertEqual(code, 0)
        path = os.path.join(self.dir, "tickets", "T-7.json")
        self.assertEqual(run(["validate", path])[0], 0)
        self.assertEqual(run(["validate", path, "--for-rollup"])[0], 1)
        self.assertEqual(run(["new", self.dir, "T-7"])[0], 2)
        self.assertEqual(run(["new", self.dir, "../x"])[0], 2)


if __name__ == "__main__":
    unittest.main()
