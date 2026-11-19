#!/usr/bin/env python3
"""Tests for the shared credential detector (synthetic fixtures only)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import credential_policy as policy  # noqa: E402
from credential_fixtures import BENIGN, CREDENTIAL_KEYS, FIXTURES  # noqa: E402


class DetectorTests(unittest.TestCase):
    def test_every_shared_fixture_reports_its_kind_and_never_the_match(self):
        for label, text, kind in FIXTURES:
            with self.subTest(fixture=label):
                kinds = policy.credential_findings(text)
                self.assertIn(kind, kinds)
                self.assertNotIn(text, " ".join(kinds))

    def test_scan_covers_strings_dict_keys_and_nesting(self):
        for label, text, kind in FIXTURES:
            for shape in ({text: 1}, {"a": [{"b": (text,)}]}, [text], text):
                with self.subTest(fixture=label, shape=type(shape).__name__):
                    self.assertIn(kind, policy.credential_findings(shape))

    def test_scan_does_not_depend_on_serialized_json_escaping(self):
        # A quote or backslash before the secret changes the JSON blob, not the value.
        text = 'say "hi" \\ ' + FIXTURES[7][1]
        self.assertIn("assignment", policy.credential_findings({"k": text}))

    def test_credential_like_field_names(self):
        for key in CREDENTIAL_KEYS:
            with self.subTest(key=key):
                self.assertIn("credential-field-name", policy.credential_findings({key: "x"}))
        self.assertEqual(policy.credential_findings({"title": "x", "evidence": ["y"]}), ())

    def test_placeholders_and_prose_are_clean(self):
        for text in BENIGN:
            with self.subTest(text=text):
                self.assertEqual(policy.credential_findings(text), ())

    def test_home_paths_are_reported_as_a_kind(self):
        self.assertIn("home-path", policy.credential_findings("see /ho" + "me/alice/x"))
        self.assertIn("home-path", policy.credential_findings({"/Us" + "ers/bob/": 1}))
        self.assertEqual(policy.credential_findings("/Users/you/project"), ())

    def test_excessive_depth_fails_closed(self):
        value = "ok"
        for _ in range(60):
            value = [value]
        self.assertIn("depth-limit", policy.credential_findings(value))


if __name__ == "__main__":
    unittest.main()
