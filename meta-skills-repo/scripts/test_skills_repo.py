#!/usr/bin/env python3
"""Tests for skills_repo.py using throwaway repositories."""

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
import skills_repo as sr  # noqa: E402

GOOD = '---\nname: %s\ndescription: "Does a thing. Use when needed."\n---\n\n# %s\n\nSee [ref](references/a.md).\n'


def make_skill(root, name, body=None, with_script=False, with_test=False):
    d = Path(root) / name
    (d / "references").mkdir(parents=True)
    (d / "SKILL.md").write_text(body if body is not None else GOOD % (name, name))
    (d / "references" / "a.md").write_text("# a\n")
    if with_script or with_test:
        (d / "scripts").mkdir()
    if with_script:
        (d / "scripts" / "tool.py").write_text("print('x')\n")
        (d / "scripts" / "tool.py").chmod(0o755)
    if with_test:
        (d / "scripts" / "test_tool.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
            "if __name__ == '__main__':\n    unittest.main()\n")
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.home = self.tmp / "home"
        (self.repo / "skills").mkdir(parents=True)
        self.home.mkdir()

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = sr.main(["--repo", str(self.repo), "--home", str(self.home), *argv])
        return code, out.getvalue()

    def write_manifest(self, **extra):
        data = {"targets": [{"id": "claude", "path": "~/.claude/skills"}, {"id": "codex", "path": "~/.codex/skills"}]}
        data.update(extra)
        (self.repo / "skills-manifest.json").write_text(json.dumps(data))


class ValidateTests(Base):
    def test_good_repo_passes(self):
        make_skill(self.repo / "skills", "alpha-skill", with_script=True, with_test=True)
        code, out = self.run_cli("validate")
        self.assertEqual(code, 0, out)

    def test_name_mismatch_and_broken_link(self):
        make_skill(self.repo / "skills", "beta", body=GOOD % ("other", "x") + "\n[gone](references/missing.md)\n")
        code, out = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("must equal directory name", out)
        self.assertIn("broken link", out)

    def test_missing_frontmatter(self):
        make_skill(self.repo / "skills", "gamma", body="# no front matter\n")
        code, out = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("front matter", out)

    def test_script_without_test_flagged(self):
        make_skill(self.repo / "skills", "delta", with_script=True)
        code, out = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("no test_*.py", out)

    def test_links_in_code_fences_and_urls_ignored(self):
        body = GOOD % ("eps", "x") + "\n```\n[x](nope.md)\n```\n[w](https://example.com/x)\n[a](#anchor)\n"
        make_skill(self.repo / "skills", "eps", body=body)
        self.assertEqual(self.run_cli("validate")[0], 0)

    def test_test_action_runs_and_reports_failure(self):
        d = make_skill(self.repo / "skills", "zeta", with_test=True)
        self.assertEqual(self.run_cli("test")[0], 0)
        (d / "scripts" / "test_tool.py").write_text("raise SystemExit(1)\n")
        self.assertEqual(self.run_cli("test")[0], 1)


class SyncTests(Base):
    def setUp(self):
        super().setUp()
        self.skill = make_skill(self.repo / "skills", "alpha", with_script=True, with_test=True)
        self.write_manifest()
        self.claude = self.home / ".claude" / "skills"
        self.codex = self.home / ".codex" / "skills"
        self.claude.mkdir(parents=True)  # codex root deliberately missing

    def test_dry_run_writes_nothing(self):
        code, out = self.run_cli("sync", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would sync claude/alpha", out)
        self.assertEqual(list(self.claude.iterdir()), [])

    def test_sync_verify_and_missing_target_skipped(self):
        code, out = self.run_cli("sync")
        self.assertEqual(code, 0, out)
        self.assertIn("skip target codex", out)
        self.assertFalse(self.codex.exists())
        dest = self.claude / "alpha"
        self.assertEqual((dest / "SKILL.md").read_bytes(), (self.skill / "SKILL.md").read_bytes())
        self.assertTrue((dest / "scripts" / "tool.py").stat().st_mode & stat.S_IXUSR)
        self.assertEqual(self.run_cli("--target", "claude", "verify")[0], 0)

    def test_verify_detects_drift_and_mode(self):
        self.run_cli("sync")
        dest = self.claude / "alpha"
        (dest / "references" / "a.md").write_text("changed\n")
        (dest / "scripts" / "tool.py").chmod(0o644)
        code, out = self.run_cli("--target", "claude", "verify")
        self.assertEqual(code, 1)
        self.assertIn("references/a.md", out)
        self.assertIn("scripts/tool.py", out)

    def test_unknown_files_and_other_skills_survive(self):
        other = self.claude / "foreign-skill"
        other.mkdir()
        (other / "SKILL.md").write_text("keep me")
        self.run_cli("sync")
        extra = self.claude / "alpha" / "notes.txt"
        extra.write_text("mine")
        (self.skill / "references" / "a.md").write_text("# a v2\n")
        code, out = self.run_cli("sync")
        self.assertEqual(code, 0)
        self.assertEqual(extra.read_text(), "mine")
        self.assertIn("kept unknown file", out)
        self.assertEqual((other / "SKILL.md").read_text(), "keep me")
        self.assertEqual((self.claude / "alpha" / "references" / "a.md").read_text(), "# a v2\n")
        # verify tolerates unknown extras
        self.assertEqual(self.run_cli("--target", "claude", "verify")[0], 0)

    def test_removed_source_file_pruned_from_mirror(self):
        self.run_cli("sync")
        (self.skill / "references" / "a.md").unlink()
        (self.skill / "SKILL.md").write_text(GOOD % ("alpha", "alpha"))
        self.run_cli("sync")
        self.assertFalse((self.claude / "alpha" / "references" / "a.md").exists())
        self.assertFalse((self.claude / "alpha" / "references").exists())

    def test_preexisting_unmanaged_conflict_refused_then_forced(self):
        dest = self.claude / "alpha"
        dest.mkdir()
        (dest / "SKILL.md").write_text("someone else's file")
        code, out = self.run_cli("sync")
        self.assertEqual(code, 1)
        self.assertIn("not written by this tool", out)
        self.assertEqual((dest / "SKILL.md").read_text(), "someone else's file")
        self.assertEqual(self.run_cli("sync", "--force")[0], 0)
        self.assertEqual((dest / "SKILL.md").read_bytes(), (self.skill / "SKILL.md").read_bytes())

    def test_symlinked_destination_refused(self):
        real = self.tmp / "elsewhere"
        real.mkdir()
        (self.claude / "alpha").symlink_to(real)
        code, out = self.run_cli("sync")
        self.assertEqual(code, 1)
        self.assertEqual(list(real.iterdir()), [])

    def test_skill_filter_and_unknown_skill(self):
        code, out = self.run_cli("--skill", "nope", "validate")
        self.assertEqual(code, 2)


class GateTests(Base):
    def setUp(self):
        super().setUp()
        make_skill(self.repo / "skills", "alpha", with_test=True)

    def test_gate_passes_without_deny_list_and_notes_it(self):
        code, out = self.run_cli("gate")
        self.assertEqual(code, 0, out)
        self.assertIn("none configured", out)

    def test_require_deny_list(self):
        self.assertEqual(self.run_cli("gate", "--require-deny-list")[0], 1)

    def test_deny_list_hits_do_not_echo_term(self):
        (self.repo / ".sensitive-terms.txt").write_text("# comment\nacme-internal\nre:proj-\\d{4}\n")
        (self.repo / "skills" / "alpha" / "references" / "a.md").write_text("# a\nuses ACME-Internal host\nticket proj-1234\n")
        code, out = self.run_cli("gate")
        self.assertEqual(code, 1)
        self.assertIn("a.md:2: matches deny-list entry #1", out)
        self.assertIn("a.md:3: matches deny-list entry #2", out)
        self.assertNotIn("acme-internal", out.lower())

    def test_path_match_and_builtin_checks(self):
        (self.repo / "skills" / "alpha" / "references" / "a.md").write_text("# a\nsee /ho" + "me/bob/x\n")
        (self.repo / "skills" / "alpha" / "references" / "secret.txt").write_text("api_" + "key = 'abcdefghijklmnop'\n")
        code, out = self.run_cli("gate")
        self.assertEqual(code, 1)
        self.assertIn("absolute home-directory path", out)
        self.assertIn("probable literal secret", out)

    def test_placeholder_home_paths_are_allowed(self):
        (self.repo / "skills" / "alpha" / "references" / "a.md").write_text(
            "# a\nsee /Us" + "ers/you/x and /ho" + "me/user/y\n")
        code, out = self.run_cli("gate")
        self.assertNotIn("absolute home-directory path", out)

    def test_clean_with_deny_list_passes(self):
        (self.repo / ".sensitive-terms.txt").write_text("forbidden-word\n")
        code, out = self.run_cli("gate", "--require-deny-list")
        self.assertEqual(code, 0, out)

    def test_bad_regex_is_clean_error(self):
        (self.repo / ".sensitive-terms.txt").write_text("re:(unclosed\n")
        code, out = self.run_cli("gate")
        self.assertEqual(code, 2)


class ReproduceTests(Base):
    def test_reproduce_end_to_end(self):
        make_skill(self.repo / "skills", "alpha", with_script=True, with_test=True)
        self.write_manifest()
        (self.home / ".claude" / "skills").mkdir(parents=True)
        code, out = self.run_cli("reproduce")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.home / ".claude" / "skills" / "alpha" / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
