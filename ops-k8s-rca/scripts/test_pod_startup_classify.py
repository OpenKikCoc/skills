#!/usr/bin/env python3
"""Tests for pod_startup_classify.py (synthetic data only)."""

import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pod_startup_classify as psc  # noqa: E402

NOW = dt.datetime(2030, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


def iso(seconds_ago):
    return (NOW - dt.timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pod(name, age=900, node=None, phase="Pending", host_ip=None, deleting=False, waiting=None, scheduled=None):
    status = {"phase": phase}
    if host_ip:
        status["hostIP"] = host_ip
    if waiting:
        status["containerStatuses"] = [{"state": {"waiting": {"reason": waiting}}}]
    if scheduled is not None:
        status["conditions"] = [{"type": "PodScheduled", "status": scheduled, "message": "0/3 nodes are available: taints"}]
    meta = {"name": name, "namespace": "demo", "uid": "uid-" + name, "creationTimestamp": iso(age)}
    if deleting:
        meta["deletionTimestamp"] = iso(10)
    spec = {"nodeName": node} if node else {}
    return {"kind": "Pod", "metadata": meta, "spec": spec, "status": status}


def event(pod_name, reason, message="", ago=60):
    return {"kind": "Event", "reason": reason, "message": message, "lastTimestamp": iso(ago),
            "involvedObject": {"kind": "Pod", "name": pod_name, "namespace": "demo", "uid": "uid-" + pod_name}}


def write(path, text):
    with open(path, "w") as fh:
        fh.write(text)


class ClassifyTests(unittest.TestCase):
    def run_one(self, p, evs=(), **kw):
        report = psc.analyze([p], [p] + list(evs), now=NOW, **kw)
        self.assertEqual(report["candidates"], 1)
        return report["pods"][0]

    def test_scheduler(self):
        r = self.run_one(pod("a", scheduled="False"))
        self.assertEqual(r["class"], "scheduler_or_policy")
        self.assertTrue(r["persistent"])

    def test_deleting_and_young(self):
        self.assertEqual(self.run_one(pod("a", node="n1", deleting=True))["class"], "terminating_residue")
        young = self.run_one(pod("b", age=30, node="n1"))
        self.assertEqual(young["class"], "too_young")
        self.assertFalse(young["persistent"])

    def test_cni_vs_cri(self):
        cni = self.run_one(pod("a", node="n1"), [event("a", "FailedCreatePodSandBox", "failed to set up pod network: IPAM exhausted")])
        self.assertEqual(cni["class"], "cni_ipam")
        cri = self.run_one(pod("b", node="n1"), [event("b", "FailedCreatePodSandBox", "context deadline exceeded")])
        self.assertEqual(cri["class"], "kubelet_cri_sandbox")

    def test_image_volume_crash(self):
        self.assertEqual(self.run_one(pod("a", node="n1", waiting="ImagePullBackOff"))["class"], "image")
        self.assertEqual(self.run_one(pod("b", node="n1"), [event("b", "FailedMount", "timeout")])["class"], "volume_mount")
        crash = self.run_one(pod("c", node="n1", phase="Running", host_ip="192.0.2.5", waiting="CrashLoopBackOff"))
        self.assertEqual(crash["class"], "app_runtime_or_probe")

    def test_bound_no_signal_and_orphan(self):
        self.assertEqual(self.run_one(pod("a", node="n1"))["class"], "bound_no_signal")
        self.assertEqual(self.run_one(pod("b", age=200000, node="n1"))["class"], "historical_orphan")

    def test_events_outside_window_ignored(self):
        r = self.run_one(pod("a", node="n1"), [event("a", "FailedCreatePodSandBox", "deadline", ago=99999)])
        self.assertEqual(r["class"], "bound_no_signal")

    def test_persistence_requires_both_snapshots(self):
        p = pod("a", node="n1")
        report = psc.analyze([], [p], now=NOW)
        self.assertEqual(report["persistent_active"], 0)
        single = psc.analyze(None, [p], now=NOW)
        self.assertEqual(single["mode"], "single-snapshot")
        self.assertEqual(single["persistent_active"], 0)

    def test_running_healthy_pod_not_candidate(self):
        healthy = pod("ok", phase="Running", node="n1", host_ip="192.0.2.1")
        self.assertEqual(psc.analyze(None, [healthy], now=NOW)["candidates"], 0)

    def test_node_concentration(self):
        pods = [pod(f"p{i}", node="n1") for i in range(3)]
        evs = [event(p["metadata"]["name"], "FailedCreatePodSandBox", "deadline") for p in pods]
        report = psc.analyze(pods, pods + evs, now=NOW)
        self.assertEqual(report["node_concentration"], {"n1": {"kubelet_cri_sandbox": 3}})


class CliTests(unittest.TestCase):
    def call(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = psc.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_cli_json_and_bad_input(self):
        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, "a.json")
            b = os.path.join(d, "b.json")
            bad = os.path.join(d, "bad.json")
            p = pod("a", node="n1")
            write(a, json.dumps({"kind": "List", "items": [p]}))
            write(b, json.dumps({"kind": "List", "items": [p, event("a", "FailedMount", "x")]}))
            write(bad, "{not json")
            code, out, _ = self.call(["--first", a, "--second", b, "--now", NOW.isoformat(), "--format", "json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["pods"][0]["class"], "volume_mount")
            code, out, _ = self.call(["--second", b, "--now", "2030-01-01T12:00:00Z"])
            self.assertEqual(code, 0)
            self.assertIn("single-snapshot", out)
            code, _, err = self.call(["--second", bad])
            self.assertEqual(code, 2)
            self.assertNotIn("Traceback", err)
            code, _, err = self.call(["--second", os.path.join(d, "missing.json")])
            self.assertEqual(code, 2)
            code, _, err = self.call(["--second", b, "--now", "garbage"])
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
