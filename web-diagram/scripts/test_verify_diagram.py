#!/usr/bin/env python3
"""Tests for verify_diagram.py. Run: python3 -I scripts/test_verify_diagram.py"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "verify_diagram.py")
EXAMPLE = os.path.join(HERE, os.pardir, "assets", "example.html")


def node(i, x, y, w=100, h=60, extra=""):
    return ('<div class="node" data-node="%s" data-kind="compute" '
            'style="left:%dpx;top:%dpx;width:%dpx;height:%dpx"%s></div>' % (i, x, y, w, h, extra))


def page(nodes, edges="[]", w=600, h=300):
    return ('<div class="diag-wrap"><div class="diag" data-w="%d" data-h="%d">%s'
            '<script type="application/json" class="edges">%s</script></div></div>'
            % (w, h, "".join(nodes), edges))


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def run_html(self, html):
        p = os.path.join(self.tmp.name, "t.html")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(html)
        r = subprocess.run([sys.executable, "-I", TOOL, "--no-render", p], capture_output=True, text=True)
        self.assertNotIn("Traceback", r.stderr)
        return r

    def assertFails(self, html, text):
        r = self.run_html(html)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("[FAIL]", r.stdout)
        self.assertIn(text, r.stdout)

    def test_pass(self):
        r = self.run_html(page([node("a", 20, 100), node("b", 300, 100)],
                               '[{"from":"a","to":"b","label":"go"}]'))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("[ok]", r.stdout)
        self.assertNotIn("[warn]", r.stdout)

    def test_missing_node_ref(self):
        self.assertFails(page([node("a", 20, 100)], '[{"from":"a","to":"zzz"}]'), "unknown node 'zzz'")

    def test_duplicate_id(self):
        self.assertFails(page([node("a", 20, 100), node("a", 300, 100)]), "duplicate data-node 'a'")

    def test_out_of_stage(self):
        self.assertFails(page([node("a", 560, 100)]), "outside the 600x300 stage")

    def test_overlap(self):
        self.assertFails(page([node("a", 20, 100), node("b", 80, 120)]), "overlap")

    def test_unknown_kind(self):
        self.assertFails(page([node("a", 20, 100, extra=' data-kind="bogus"')]).replace(
            'data-kind="compute" ', '', 1), "unknown data-kind")

    def test_bad_json(self):
        self.assertFails(page([node("a", 20, 100)], "[{oops"), "does not parse")

    def test_non_numeric_stage(self):
        self.assertFails(page([node("a", 20, 100)]).replace('data-w="600"', 'data-w="wide"'), "data-w")

    def test_edge_crosses_third_node(self):
        r = page([node("a", 20, 100), node("mid", 250, 100), node("b", 480, 100)],
                 '[{"from":"a","to":"b"}]')
        self.assertFails(r, "passes through node 'mid'")

    def test_edge_routed_around_passes(self):
        r = page([node("a", 20, 100), node("mid", 250, 100), node("b", 480, 100)],
                 '[{"from":"a","to":"b","fromSide":"bottom","toSide":"bottom","out":60}]')
        res = self.run_html(r)
        self.assertEqual(res.returncode, 0, res.stdout)

    def test_label_overlap_warns_but_passes(self):
        r = page([node("a", 20, 100), node("b", 150, 100)],
                 '[{"from":"a","to":"b","label":"a very long label that cannot fit"}]')
        res = self.run_html(r)
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("[warn]", res.stdout)
        self.assertIn("overlaps node", res.stdout)

    def test_sequence_long_label_warns(self):
        seq = ('<div class="diag-wrap"><div class="diag seq" data-w="600"><script type="application/json" '
               'class="seqdef">{"participants":[{"id":"a","label":"A"},{"id":"b","label":"B"}],'
               '"messages":[{"from":"a","to":"b","label":"%s"}]}</script></div></div>' % ("long " * 30))
        res = self.run_html(seq)
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("longer than the lane gap", res.stdout)

    def test_sequence_unknown_participant(self):
        seq = ('<div class="diag-wrap"><div class="diag seq" data-w="600"><script type="application/json" '
               'class="seqdef">{"participants":[{"id":"a","label":"A"}],'
               '"messages":[{"from":"a","to":"x","label":"hi"}]}</script></div></div>')
        self.assertFails(seq, "unknown participant")

    def test_missing_file(self):
        r = subprocess.run([sys.executable, "-I", TOOL, "--no-render", os.path.join(self.tmp.name, "none.html")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("[FAIL]", r.stdout)
        self.assertNotIn("Traceback", r.stderr)

    def test_example_passes_clean(self):
        r = subprocess.run([sys.executable, "-I", TOOL, "--no-render", EXAMPLE], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("[warn]", r.stdout)


if __name__ == "__main__":
    unittest.main()
