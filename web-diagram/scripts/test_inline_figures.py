#!/usr/bin/env python3
"""Tests for inline_figures.py. Run: python3 -I scripts/test_inline_figures.py"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "inline_figures.py")

PAGE = ('<html><head><link rel="stylesheet" href="diag.css"></head><body>\n'
        '<!--FIG:one-->\n<script src="diag.js"></script></body></html>\n')


def run(*args):
    return subprocess.run([sys.executable, "-I", TOOL] + list(args), capture_output=True, text=True)


class InlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        os.makedirs(os.path.join(self.d, "figures"))
        self.write("diag.css", ".diag{color:red}")
        self.write("diag.js", "window.x=1;")
        self.write("figures/one.html", "<div>FIG-ONE</div>\n")
        self.write("page.html", PAGE)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        with open(os.path.join(self.d, rel), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_happy_path(self):
        out = os.path.join(self.d, "out.html")
        r = run(os.path.join(self.d, "page.html"), "-o", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(out, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("FIG-ONE", text)
        self.assertIn("<style>", text)
        self.assertIn(".diag{color:red}", text)
        self.assertIn("window.x=1;", text)
        self.assertNotIn("<!--FIG", text)
        self.assertNotIn('href="diag.css"', text)
        self.assertNotIn('src="diag.js"', text)

    def test_unknown_figure_fails_and_writes_nothing(self):
        self.write("page.html", PAGE.replace("FIG:one", "FIG:missing"))
        out = os.path.join(self.d, "out.html")
        r = run(os.path.join(self.d, "page.html"), "-o", out)
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown figure 'missing'", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(os.path.exists(out))

    def test_unused_figure_warns(self):
        self.write("figures/spare.html", "<div>spare</div>")
        out = os.path.join(self.d, "out.html")
        r = run(os.path.join(self.d, "page.html"), "-o", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("figure 'spare' is never referenced", r.stderr)

    def test_missing_page_is_clean_error(self):
        r = run(os.path.join(self.d, "nope.html"), "-o", os.path.join(self.d, "o.html"))
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)

    def test_assets_fall_back_to_skill_assets(self):
        os.remove(os.path.join(self.d, "diag.css"))
        os.remove(os.path.join(self.d, "diag.js"))
        out = os.path.join(self.d, "out.html")
        r = run(os.path.join(self.d, "page.html"), "-o", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(out, encoding="utf-8") as fh:
            self.assertIn("diagFitAll", fh.read())


if __name__ == "__main__":
    unittest.main()
