#!/usr/bin/env python3
"""Tests for skills_repo.py using throwaway repositories."""

import contextlib
import io
import json
import os
import shutil
import signal
import stat
import subprocess
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


def with_deadline(seconds=15):
    """Fail instead of hanging when code blocks on a FIFO."""
    def raise_timeout(signum, frame):
        raise AssertionError("operation blocked (probably opened a special file)")
    signal.signal(signal.SIGALRM, raise_timeout)
    signal.alarm(seconds)


class MarkdownLinkTests(Base):
    def validate_body(self, extra, name="lnk"):
        make_skill(self.repo / "skills", name, body=GOOD % (name, "x") + "\n" + extra)
        return self.run_cli("validate")

    def test_fence_stripper_keeps_line_numbers_and_handles_tilde_and_long_fences(self):
        text = "a\n````\n```\n[x](nope.md)\n```\n[y](nope2.md)\n````\nb\n~~~~\n[z](nope3.md)\n~~~~\n[k](real.md)\n"
        stripped = sr.strip_markdown_fences(text)
        self.assertEqual(stripped.count("\n"), text.count("\n"))
        self.assertNotIn("nope", stripped)
        self.assertIn("[k](real.md)", stripped)
        self.assertEqual(stripped.splitlines()[11], "[k](real.md)")

    def test_inner_short_fence_does_not_close_a_long_fence(self):
        code, out = self.validate_body("````\n```\n[x](nope.md)\n```\n[y](nope2.md)\n````\n")
        self.assertEqual(code, 0, out)

    def test_tilde_fence_hides_links_and_unterminated_fence_hides_the_rest(self):
        self.assertEqual(self.validate_body("~~~\n[x](nope.md)\n~~~\n")[0], 0)
        self.assertEqual(self.validate_body("```\n[x](nope.md)\n", name="open")[0], 0)

    def test_link_after_a_closed_fence_is_checked_with_its_line_number(self):
        code, out = self.validate_body("```\n[x](nope.md)\n```\n[gone](references/missing.md)\n")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"SKILL\.md:13: broken link: references/missing\.md")

    def test_query_and_fragment_are_stripped_before_resolving(self):
        self.assertEqual(self.validate_body("[a](references/a.md?plain=1#L3)\n[b](references/a.md#top)\n")[0], 0)
        code, out = self.validate_body("[c](references/missing.md?plain=1)\n", name="qry")
        self.assertEqual(code, 1)
        self.assertIn("broken link", out)

    def test_reference_definitions_are_checked_even_when_unused(self):
        code, out = self.validate_body("[unused]: references/missing.md\n")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"SKILL\.md:\d+: broken link: references/missing\.md")
        shutil.rmtree(self.repo / "skills" / "lnk")
        self.assertEqual(self.validate_body("[ok]: references/a.md#x\n[web]: https://example.com/p\n", name="refok")[0], 0)

    def test_reference_definition_escaping_the_skill_is_rejected(self):
        (self.repo / "skills" / "outside.md").write_text("# o\n")
        code, out = self.validate_body("[esc]: ../outside.md\n")
        self.assertEqual(code, 1)
        self.assertIn("escapes skill directory", out)

    def test_documented_limits_are_stated(self):
        text = (Path(sr.__file__).resolve().parent.parent / "SKILL.md").read_text().lower()
        self.assertIn("single-line", text)
        self.assertIn("blockquote", text)


class BuiltinSecretTests(Base):
    def setUp(self):
        super().setUp()
        make_skill(self.repo / "skills", "alpha", with_test=True)
        self.note = self.repo / "skills" / "alpha" / "references" / "a.md"

    def gate_with(self, line):
        self.note.write_text("# a\n" + line + "\n")
        return self.run_cli("gate")

    def test_broadened_patterns_are_flagged_and_never_echoed(self):
        fake = "Zx9Qw3Lp7Rt5Vb2N"
        cases = {
            "bearer": "Authorization: Bear" + "er " + fake,
            "basic": "Authorization: Bas" + "ic " + fake + "==",
            "query-token": "curl https://h.example/p?to" + "ken=" + fake,
            "query-key": "https://h.example/p?a=1&ke" + "y=" + fake,
            "query-access": "https://h.example/p?access_" + "token=" + fake,
            "query-refresh": "https://h.example/p?refresh_" + "token=" + fake,
            "unquoted-password": "pass" + "word=hunter2",
            "unquoted-colon": "api_" + "key: " + fake,
            "refresh-assign": "refresh_" + "token = " + fake,
        }
        for label, line in cases.items():
            with self.subTest(case=label):
                code, out = self.gate_with(line)
                self.assertEqual(code, 1, out)
                self.assertIn("a.md:2: probable credential", out)
                self.assertNotIn(fake, out)
                self.assertNotIn("hunter2", out)

    def test_placeholders_and_prose_are_allowed(self):
        for line in ("pass" + "word=${DB_PASSWORD}", "pass" + "word: <your password>",
                     "curl https://h.example/p?to" + "ken=<token>", "?ke" + "y=${API_KEY}",
                     "Authorization: Bear" + "er ${TOKEN}", "Basic configuration steps follow.",
                     "use a bearer credential from the environment", "token = self.token",
                     "token = parse(arguments)"):
            with self.subTest(line=line):
                code, out = self.gate_with(line)
                self.assertNotIn("probable credential", out)

    def test_fixture_marker_is_honored_only_in_test_python_files(self):
        marker = "# skills-repo: allow-test-" + "secret"
        line = "x = 'pass" + "word=hunter2'  " + marker
        self.note.write_text("# a\n" + line + "\n")
        self.assertEqual(self.run_cli("gate")[0], 1)  # markdown ignores the marker
        self.note.write_text("# a\n")
        tests = self.repo / "skills" / "alpha" / "scripts" / "test_tool.py"
        tests.write_text(tests.read_text() + "# pass" + "word=hunter2  " + marker + "\n")
        code, out = self.run_cli("gate")
        self.assertEqual(code, 0, out)
        quoted = "# api_" + "key = 'abcdefghijklmnop'  " + marker + "\n"
        tests.write_text(tests.read_text() + quoted)
        self.assertEqual(self.run_cli("gate")[0], 1)  # quoted literals are never exempt

    def test_existing_quoted_assignment_still_flagged(self):
        code, out = self.gate_with("api_" + "key = 'abcdefghijklmnop'")
        self.assertEqual(code, 1)
        self.assertIn("probable", out)


class SpecialFileTests(Base):
    def setUp(self):
        super().setUp()
        self.skill = make_skill(self.repo / "skills", "alpha", with_script=True, with_test=True)
        self.write_manifest()
        self.claude = self.home / ".claude" / "skills"
        self.claude.mkdir(parents=True)
        self.addCleanup(signal.alarm, 0)

    def test_fifo_in_skill_dir_is_a_validate_error_not_a_hang(self):
        with_deadline()
        os.mkfifo(self.skill / "references" / "pipe")
        code, out = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("references/pipe", out)
        self.assertIn("special file", out)

    def test_socket_in_skill_dir_is_a_validate_error(self):
        import socket
        with_deadline()
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        try:
            sock.bind(str(self.skill / "references" / "s.sock"))
        except OSError:
            self.skipTest("cannot bind unix socket here")
        code, out = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("special file", out)

    def test_sync_and_gate_fail_closed_on_special_files_without_hanging(self):
        with_deadline()
        os.mkfifo(self.skill / "references" / "pipe")
        for argv in (("sync",), ("gate",), ("reproduce",)):
            with self.subTest(argv=argv):
                code, out = self.run_cli(*argv)
                self.assertNotEqual(code, 0, out)
        self.assertFalse((self.claude / "alpha" / "references" / "pipe").exists())

    def test_fifo_planted_in_mirror_is_a_conflict_not_a_hang(self):
        self.run_cli("sync")
        with_deadline()
        mirrored = self.claude / "alpha" / "references" / "a.md"
        mirrored.unlink()
        os.mkfifo(mirrored)
        (self.skill / "references" / "a.md").write_text("# a v2\n")
        for argv in (("sync",), ("--target", "claude", "verify")):
            with self.subTest(argv=argv):
                code, out = self.run_cli(*argv)
                self.assertEqual(code, 1, out)
                self.assertIn("not a regular file", out)

    def test_full_executable_mask_is_compared(self):
        self.run_cli("sync")
        tool = self.claude / "alpha" / "scripts" / "tool.py"
        self.assertEqual(tool.stat().st_mode & 0o111, 0o111)
        tool.chmod(0o744)  # still executable by the owner only
        code, out = self.run_cli("--target", "claude", "verify")
        self.assertEqual(code, 1)
        self.assertIn("scripts/tool.py", out)
        self.run_cli("sync")
        self.assertEqual(tool.stat().st_mode & 0o111, 0o111)
        self.assertEqual(self.run_cli("--target", "claude", "verify")[0], 0)

    def test_digest_helper_refuses_non_regular_files(self):
        with_deadline()
        fifo = self.tmp / "pipe"
        os.mkfifo(fifo)
        with self.assertRaises(sr.RepoError):
            sr.file_digest(fifo)
        link = self.tmp / "link"
        link.symlink_to(self.skill / "SKILL.md")
        with self.assertRaises(sr.RepoError):
            sr.file_digest(link)


class DriftRepairTests(Base):
    def test_injected_drift_is_detected_and_repaired_by_sync(self):
        skill = make_skill(self.repo / "skills", "alpha", with_script=True, with_test=True)
        self.write_manifest()
        claude = self.home / ".claude" / "skills"
        claude.mkdir(parents=True)
        self.assertEqual(self.run_cli("sync")[0], 0)
        dest = claude / "alpha"
        (dest / "SKILL.md").write_text("tampered\n")
        (dest / "references" / "a.md").unlink()
        (dest / "scripts" / "tool.py").chmod(0o644)
        (dest / "stray.txt").write_text("unknown\n")
        code, out = self.run_cli("--target", "claude", "verify")
        self.assertEqual(code, 1)
        for name in ("SKILL.md", "references/a.md", "scripts/tool.py"):
            self.assertIn(name, out)
        self.assertEqual(self.run_cli("sync")[0], 0)
        self.assertEqual(self.run_cli("--target", "claude", "verify")[0], 0)
        self.assertEqual((dest / "SKILL.md").read_bytes(), (skill / "SKILL.md").read_bytes())
        self.assertEqual((dest / "stray.txt").read_text(), "unknown\n")


class OriginIdentityTests(Base):
    GOOD_FORMS = (
        ("ssh://git@GitHub.com/Owner/Repo.git/", "github.com/Owner/Repo"),
        ("https://GITHUB.COM/Owner/Repo.git", "github.com/Owner/Repo"),
        ("http://github.com/Owner/Repo/", "github.com/Owner/Repo"),
        ("git@GitHub.com:Owner/Repo.git/", "github.com/Owner/Repo"),
        ("GitHub.com:Owner/Repo", "github.com/Owner/Repo"),
        ("https://host.example:8443/group/sub/repo.git", "host.example/group/sub/repo"),
    )
    BAD_FORMS = (
        "", "   ", "relative/path", "/fixture/repo", "file:///fixture/repo", "./repo", "../repo",
        "https://h.example/owner/repo?x=1", "https://h.example/owner/repo#frag",
        "https://u:pw@h.example/owner/repo", "https://h.example/owner/../repo",
        "https://h.example/owner/./repo", "git@h.example:/owner/repo", "ssh://h.example//owner/repo",
        "https://h.example/owner//repo", "https://h.example/owner/%72epo", "git@h.example:repo",
        "https://h.example/repo", "ssh://h.example:bad/owner/repo", "https://h.example/owner/re po",
        "https://h.example\\owner\\repo", "C:\\work\\repo", "https://h.example:99999/owner/repo",
    )

    def test_strict_parser_accepts_common_forms_lowercasing_host_only(self):
        for raw, expected in self.GOOD_FORMS:
            with self.subTest(raw=raw):
                self.assertEqual(sr.canonical_origin_identity(raw), expected)

    def test_strict_parser_rejects_ambiguous_inputs(self):
        for raw in self.BAD_FORMS:
            with self.subTest(raw=raw):
                with self.assertRaises(sr.RepoError):
                    sr.canonical_origin_identity(raw)

    def git(self, *args, cwd=None):
        subprocess.run(["git", *args], cwd=str(cwd or self.repo), check=True, capture_output=True, timeout=20)

    def setup_checkout(self, origin="git@GitHub.com:Owner/Repo.git"):
        make_skill(self.repo / "skills", "alpha", with_test=True)
        self.git("init", "-q")
        if origin:
            self.git("remote", "add", "origin", origin)
        self.claude = self.home / ".claude" / "skills"
        self.claude.mkdir(parents=True)

    def test_matching_origin_passes_gate_and_sync(self):
        self.setup_checkout()
        self.write_manifest(expected_origin="https://github.com/Owner/Repo")
        self.assertEqual(self.run_cli("gate")[0], 0)
        self.assertEqual(self.run_cli("sync")[0], 0)
        self.assertTrue((self.claude / "alpha" / "SKILL.md").exists())

    def test_wrong_origin_blocks_gate_and_sync_before_writing(self):
        self.setup_checkout(origin="git@github.com:Other/Fork.git")
        self.write_manifest(expected_origin="github.com/Owner/Repo")
        for argv in (("gate",), ("sync",), ("sync", "--dry-run"), ("reproduce",)):
            with self.subTest(argv=argv):
                code, out = self.run_cli(*argv)
                self.assertNotEqual(code, 0)
                self.assertIn("does not match expected_origin", out)
        self.assertEqual(list(self.claude.iterdir()), [])

    def test_path_case_is_significant_and_host_case_is_not(self):
        self.setup_checkout(origin="git@GITHUB.COM:owner/repo.git")
        self.write_manifest(expected_origin="github.com/Owner/Repo")
        code, out = self.run_cli("sync")
        self.assertNotEqual(code, 0)
        self.assertIn("does not match expected_origin", out)

    def test_missing_origin_remote_or_non_git_repo_fails_closed(self):
        self.setup_checkout(origin=None)
        self.write_manifest(expected_origin="github.com/Owner/Repo")
        self.assertNotEqual(self.run_cli("sync")[0], 0)
        self.assertNotEqual(self.run_cli("gate")[0], 0)

    def test_non_git_directory_fails_closed(self):
        make_skill(self.repo / "skills", "alpha", with_test=True)
        self.write_manifest(expected_origin="github.com/Owner/Repo")
        (self.home / ".claude" / "skills").mkdir(parents=True)
        code, out = self.run_cli("sync")
        self.assertNotEqual(code, 0)
        self.assertEqual(list((self.home / ".claude" / "skills").iterdir()), [])

    def test_repo_must_be_the_git_toplevel(self):
        outer = self.tmp / "outer"
        (outer / "inner" / "skills").mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=str(outer), check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", "git@github.com:Owner/Repo.git"], cwd=str(outer), check=True, capture_output=True)
        make_skill(outer / "inner" / "skills", "alpha", with_test=True)
        self.repo = outer / "inner"
        self.write_manifest(expected_origin="github.com/Owner/Repo")
        code, out = self.run_cli("sync", "--dry-run")
        self.assertNotEqual(code, 0)
        self.assertIn("top-level", out)

    def test_invalid_expected_origin_is_a_clean_error(self):
        self.setup_checkout()
        for bad in ("/local/path", "relative/path", "", 5, "https://h.example/only-one"):
            with self.subTest(bad=bad):
                self.write_manifest(expected_origin=bad)
                code, out = self.run_cli("gate")
                self.assertEqual(code, 2)
                self.assertIn("origin identity", out)

    def test_no_expected_origin_means_no_check(self):
        make_skill(self.repo / "skills", "alpha", with_test=True)
        self.write_manifest()
        (self.home / ".claude" / "skills").mkdir(parents=True)
        self.assertEqual(self.run_cli("sync")[0], 0)


class VendorGuidanceNoteTests(unittest.TestCase):
    def test_skill_md_documents_vendor_guidance_rule(self):
        text = " ".join((Path(sr.__file__).resolve().parent.parent / "SKILL.md").read_text().split())
        self.assertIn("vendor guidance", text.lower())
        self.assertIn("never copy", text.lower())
        self.assertIn("record its version", text.lower())
        self.assertIn("stop", text.lower())


if __name__ == "__main__":
    unittest.main()
