#!/usr/bin/env python3
"""Tests for owner_chain.py (synthetic data only)."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import owner_chain as oc  # noqa: E402


def obj(kind, name, uid, owner=None, ns="demo"):
    m = {"name": name, "namespace": ns, "uid": uid}
    if owner:
        m["ownerReferences"] = [{"kind": owner[0], "name": owner[1], "uid": owner[2], "controller": True}]
    return {"kind": kind, "metadata": m}


class OwnerChainTest(unittest.TestCase):
    def test_full_chain(self):
        objs = [obj("Deployment", "web", "d1"), obj("ReplicaSet", "web-abc", "r1", ("Deployment", "web", "d1")),
                obj("Pod", "web-abc-x", "p1", ("ReplicaSet", "web-abc", "r1"))]
        res = oc.build(objs)
        self.assertEqual([h["kind"] for h in res[0]["chain"]], ["Pod", "ReplicaSet", "Deployment"])
        self.assertEqual(res[0]["findings"], [])

    def test_dangling_owner(self):
        res = oc.build([obj("Pod", "p", "p1", ("ReplicaSet", "gone", "r9"))])
        self.assertEqual(res[0]["chain"][-1]["state"], "missing")
        self.assertTrue(res[0]["findings"])

    def test_uid_mismatch_after_recreate(self):
        objs = [obj("ReplicaSet", "rs", "r2"), obj("Pod", "p", "p1", ("ReplicaSet", "rs", "r1"))]
        res = oc.build(objs)
        self.assertEqual(res[0]["chain"][-1]["state"], "uid_mismatch")

    def test_cycle(self):
        objs = [obj("Pod", "p", "a", ("Job", "j", "b")), obj("Job", "j", "b", ("Pod", "p", "a"))]
        res = oc.build(objs)
        self.assertTrue(any("cycle" in f for f in res[0]["findings"]))

    def test_bare_pod_and_cli(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "o.json")
            with open(path, "w") as fh:
                json.dump({"items": [obj("Pod", "solo", "s1")]}, fh)
            self.assertEqual(oc.main([path]), 0)
            self.assertEqual(oc.main([path, "--pod", "nope"]), 2)


if __name__ == "__main__":
    unittest.main()
