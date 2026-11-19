#!/usr/bin/env python3
"""Tests for draft_inbox.py (synthetic data only)."""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import draft_inbox as inbox_mod  # noqa: E402
from credential_fixtures import BENIGN, CREDENTIAL_KEYS, FIXTURES  # noqa: E402


def packet(**over):
    base = {
        "title": "Check the lockfile before bumping deps",
        "trigger": "user_correction",
        "mechanism": "Agent bumped a dependency without regenerating the lockfile; user corrected it twice.",
        "evidence": ["User: regenerate the lockfile after any dependency change", "CI failed on stale lockfile"],
        "target": "existing_skill",
        "target_ref": "dependency-upgrades",
        "proposed_change": "Add a step: regenerate and diff the lockfile before commit.",
        "existing_rule_checked": "dependency-upgrades covers bumps but not lockfile regeneration",
    }
    base.update(over)
    return base


class InboxTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.inbox = Path(self._tmp.name) / "inbox"

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = inbox_mod.main(["--inbox", str(self.inbox), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_propose_creates_private_immutable_draft_and_dedupes(self):
        first = inbox_mod.propose(self.inbox, packet())
        self.assertEqual(first["status"], "created")
        path = self.inbox / "drafts" / first["draft"]
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        before = path.read_bytes()
        # Same ask with different supporting prose is a duplicate; bytes unchanged.
        second = inbox_mod.propose(self.inbox, packet(mechanism="Rephrased mechanism text."))
        self.assertEqual(second, {"status": "duplicate", "draft": first["draft"]})
        self.assertEqual(path.read_bytes(), before)
        # A different proposed change is a new draft.
        third = inbox_mod.propose(self.inbox, packet(proposed_change="Different change."))
        self.assertEqual(third["status"], "created")

    def test_rejections(self):
        bad = [
            packet(trigger="routine_error"),
            packet(target="everything"),
            packet(blocking=True),
            packet(evidence=[]),
            packet(target_ref=""),
            packet(evidence=["to" + "ken = abcdefghijklmnop1234"]),
            packet(evidence=["see /ho" + "me/alice/notes.txt"]),
            packet(surprise="x"),
        ]
        for p in bad:
            with self.assertRaises(inbox_mod.InboxError, msg=str(p)[:60]):
                inbox_mod.propose(self.inbox, p)
        missing = packet()
        del missing["mechanism"]
        with self.assertRaises(inbox_mod.InboxError):
            inbox_mod.propose(self.inbox, missing)
        self.assertFalse((self.inbox / "drafts").exists())

    def test_draft_only_needs_no_target_ref(self):
        p = packet(target="draft_only")
        del p["target_ref"]
        self.assertEqual(inbox_mod.propose(self.inbox, p)["status"], "created")

    def test_decisions_are_separate_receipts_with_revisions(self):
        name = inbox_mod.propose(self.inbox, packet())["draft"]
        draft_bytes = (self.inbox / "drafts" / name).read_bytes()
        self.assertEqual(inbox_mod.list_drafts(self.inbox)[0]["status"], "pending")
        inbox_mod.decide(self.inbox, name, "deferred", "wait for second occurrence")
        inbox_mod.decide(self.inbox, name, "promoted", "landed in skill update")
        self.assertEqual(inbox_mod.list_drafts(self.inbox)[0]["status"], "promoted")
        self.assertEqual((self.inbox / "drafts" / name).read_bytes(), draft_bytes)
        self.assertEqual(len(list((self.inbox / "decisions").iterdir())), 2)

    def test_decide_rejects_bad_input(self):
        with self.assertRaises(inbox_mod.InboxError):
            inbox_mod.decide(self.inbox, "../escape.json", "rejected", "x")
        name = inbox_mod.propose(self.inbox, packet())["draft"]
        with self.assertRaises(inbox_mod.InboxError):
            inbox_mod.decide(self.inbox, name, "rejected", "   ")

    def assert_rejected_without_echo(self, error, text):
        message = str(error)
        self.assertNotIn(text, message)
        for piece in text.replace("=", " ").replace(":", " ").split():
            if len(piece) >= 12:
                self.assertNotIn(piece, message)

    def test_shared_fixtures_are_rejected_in_every_packet_field_without_echo(self):
        for label, text, kind in FIXTURES:
            for field in ("evidence", "title", "mechanism", "proposed_change", "existing_rule_checked", "origin"):
                with self.subTest(fixture=label, field=field):
                    value = [text] if field == "evidence" else text
                    with self.assertRaises(inbox_mod.InboxError) as raised:
                        inbox_mod.propose(self.inbox, packet(**{field: value}))
                    self.assert_rejected_without_echo(raised.exception, text)
                    self.assertIn(kind, str(raised.exception))
        self.assertFalse((self.inbox / "drafts").exists())

    def test_credential_text_in_dict_keys_and_nested_values_is_rejected_without_echo(self):
        for label, text, _ in FIXTURES:
            with self.subTest(fixture=label, where="unknown-key"):
                with self.assertRaises(inbox_mod.InboxError) as raised:
                    inbox_mod.propose(self.inbox, packet(**{text: "x"}))
                self.assert_rejected_without_echo(raised.exception, text)
            with self.subTest(fixture=label, where="nested-origin"):
                with self.assertRaises(inbox_mod.InboxError) as raised:
                    inbox_mod.propose(self.inbox, packet(origin={"a": [{"b": text}]}))
                self.assert_rejected_without_echo(raised.exception, text)
        for key in CREDENTIAL_KEYS:
            with self.subTest(key=key):
                with self.assertRaises(inbox_mod.InboxError) as raised:
                    inbox_mod.propose(self.inbox, packet(origin={key: "x"}))
                self.assertIn("credential-field-name", str(raised.exception))

    def test_placeholders_and_plain_prose_are_not_rejected(self):
        for index, text in enumerate(BENIGN):
            with self.subTest(text=text):
                created = inbox_mod.propose(self.inbox, packet(proposed_change="Benign change %d: %s" % (index, text)))
                self.assertEqual(created["status"], "created")

    def test_shared_fixtures_are_rejected_in_decision_notes_without_echo(self):
        name = inbox_mod.propose(self.inbox, packet())["draft"]
        for label, text, _ in FIXTURES:
            with self.subTest(fixture=label):
                with self.assertRaises(inbox_mod.InboxError) as raised:
                    inbox_mod.decide(self.inbox, name, "rejected", "note " + text)
                self.assert_rejected_without_echo(raised.exception, text)
        self.assertFalse((self.inbox / "decisions").exists())

    def test_cli_error_output_never_echoes_the_match(self):
        pfile = Path(self._tmp.name) / "leak.json"
        for label, text, _ in FIXTURES:
            with self.subTest(fixture=label):
                pfile.write_text(json.dumps(packet(evidence=[text])))
                code, out, err = self.cli("propose", str(pfile))
                self.assertEqual(code, 2)
                self.assertNotIn(text, out + err)
                self.assertNotIn(text[-12:], out + err)

    def test_read_and_list_paths_reject_credential_text_planted_after_the_fact(self):
        name = inbox_mod.propose(self.inbox, packet())["draft"]
        inbox_mod.decide(self.inbox, name, "deferred", "wait")
        receipt = next((self.inbox / "decisions").iterdir())
        draft = self.inbox / "drafts" / name
        clean_draft = draft.read_text()
        clean_receipt = receipt.read_text()
        for label, text, _ in FIXTURES:
            with self.subTest(fixture=label, where="decision"):
                data = json.loads(clean_receipt)
                data["note"] = text
                receipt.write_text(json.dumps(data))
                rows = inbox_mod.list_drafts(self.inbox)
                self.assertEqual(rows[0]["status"], "unsafe_decision")
                self.assertNotIn(text, json.dumps(rows))
                with self.assertRaises(inbox_mod.InboxError) as raised:
                    inbox_mod.latest_decision(self.inbox, name)
                self.assert_rejected_without_echo(raised.exception, text)
            receipt.write_text(clean_receipt)
            with self.subTest(fixture=label, where="draft"):
                data = json.loads(clean_draft)
                data["title"] = text
                draft.write_text(json.dumps(data))
                rows = inbox_mod.list_drafts(self.inbox)
                self.assertEqual(rows[0]["status"], "unsafe_draft")
                self.assertNotIn(text, json.dumps(rows))
            draft.write_text(clean_draft)
        self.assertEqual(inbox_mod.list_drafts(self.inbox)[0]["status"], "deferred")

    def test_cli_paths(self):
        pfile = Path(self._tmp.name) / "p.json"
        pfile.write_text(json.dumps(packet()))
        code, out, _ = self.cli("propose", str(pfile))
        self.assertEqual(code, 0)
        draft = json.loads(out)["draft"]
        self.assertEqual(self.cli("list")[0], 0)
        self.assertEqual(self.cli("decide", "--draft", draft, "--decision", "rejected", "--note", "not general")[0], 0)
        pfile.write_text("{broken")
        code, out, err = self.cli("propose", str(pfile))
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.cli("propose", str(Path(self._tmp.name) / "nope.json"))[0], 2)


if __name__ == "__main__":
    unittest.main()
