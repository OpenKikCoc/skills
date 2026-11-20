#!/usr/bin/env python3
"""Tests for fleet.py. Run: python3 -I scripts/test_fleet.py (from the skill dir).

Real throwaway git repos (bare origin + clones) in temp dirs; the runtime CLI is a
tiny python script that records its prompt/env, prints a fake session id and exits.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)  # tests read small files inline
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet  # noqa: E402

FAKE = r'''
import json, os, sys, time, uuid
out = os.environ["FAKE_OUT"]
tag = "%s_%s_%s" % (os.environ["FLEET_ITEM"], os.environ["FLEET_ROLE"], os.environ["FLEET_GEN"])
open(os.path.join(out, tag + ".prompt"), "w").write(sys.stdin.read())
open(os.path.join(out, tag + ".argv"), "w").write(json.dumps(sys.argv[1:]))
tmp = os.path.join(out, tag + ".env.tmp")
json.dump({k: v for k, v in os.environ.items() if k.startswith("FLEET_")}, open(tmp, "w"))
os.rename(tmp, os.path.join(out, tag + ".env"))
print(json.dumps({"thread_id": str(uuid.uuid4())}), flush=True)
hold = os.environ.get("FAKE_HOLD")
while hold and os.path.exists(hold):
    time.sleep(0.05)
'''

SECTIONS = """
# Card

## Contract

- **Goal:** do the thing.

## State (worker-maintained)
`state: in-progress` · `updated: {updated}` · `blocker: none` · `doing: starting`

## Progress (worker-appended)

## Evidence
```json
{{}}
```
"""


def sh(cwd, *a):
    return subprocess.run(["git", "-C", cwd] + list(a), capture_output=True, text=True, check=True).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="fleet-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        t = self.tmp
        self.home, self.out = os.path.join(t, "home"), os.path.join(t, "fakeout")
        self.hold_file = os.path.join(t, "hold")
        os.makedirs(self.out)
        self.base_env = {"FLEET_HOME": self.home, "FAKE_OUT": self.out, "FAKE_HOLD": self.hold_file,
                         "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
                         "GIT_CONFIG_GLOBAL": os.path.join(t, "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}
        script = os.path.join(t, "fake_runtime.py")
        with open(script, "w") as f:
            f.write(FAKE)
        tiers = {"low": {"effort": "low", "model": "m-fast"}, "medium": {"effort": "medium"},
                 "high": {"effort": "high"}, "xhigh": {"effort": "xhigh"}}
        rx = "\"thread_id\"\\s*:\\s*\"([0-9a-f-]{36})\""
        self.cfg = {
            "limits": {"max_active": 4, "max_active_per_repo": 2, "push_timeout_s": 30},
            "runtimes": {
                "fake": {"start": [sys.executable, script, "start"], "resume": [sys.executable, script, "resume", "{session_id}"],
                         "stdin": "{prompt_file}", "session_id_mode": "capture", "session_id_regex": rx,
                         "default_model": "m-main", "tiers": tiers},
                "assign": {"start": [sys.executable, script, "start", "{session_id}", "{model}", "{effort}"],
                           "resume": [sys.executable, script, "resume", "{session_id}"], "stdin": "{prompt_file}",
                           "session_id_mode": "assign", "default_model": "m-main", "tiers": tiers},
                "partial": {"start": [sys.executable, script, "start"], "stdin": "{prompt_file}",
                            "default_model": "m-p", "tiers": {"medium": {"effort": "medium"}, "high": {"effort": "high"}}},
            }}
        os.makedirs(self.home)
        self.write_cfg()
        # git: bare origin, seed, working clone used by the fleet, second clone for moving base
        self.origin = os.path.join(t, "origin.git")
        with self.env():
            sh(t, "init", "--bare", "-b", "main", self.origin)
            seed = os.path.join(t, "seed")
            sh(t, "clone", self.origin, seed)
            sh(seed, "checkout", "-b", "main")
            with open(os.path.join(seed, "README"), "w") as f:
                f.write("hi\n")
            sh(seed, "add", "-A")
            sh(seed, "commit", "-m", "init")
            sh(seed, "push", "origin", "main")
            self.repo = os.path.join(t, "repo")
            sh(t, "clone", self.origin, self.repo)
            self.other = os.path.join(t, "other")
            sh(t, "clone", self.origin, self.other)
        self.assertEqual(self.run_cli("init")[0], 0)

    def write_cfg(self):
        with open(os.path.join(self.home, "fleet.json"), "w") as f:
            json.dump(self.cfg, f)

    @contextlib.contextmanager
    def env(self, extra=None):
        old = dict(os.environ)
        try:
            for k in list(os.environ):
                if k.startswith("FLEET_") or k == "SSH_ORIGINAL_COMMAND":
                    del os.environ[k]
            os.environ.update(self.base_env)
            os.environ.update(extra or {})
            yield
        finally:
            os.environ.clear()
            os.environ.update(old)

    def run_cli(self, *argv, env=None):
        o, e = io.StringIO(), io.StringIO()
        with self.env(env), contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rc = fleet.main(list(argv))
        return rc, o.getvalue(), e.getvalue()

    def j(self, *argv, env=None, rc=0):
        r, o, e = self.run_cli(*(list(argv) + ["--json"]), env=env)
        self.assertEqual(r, rc, "rc=%s out=%s err=%s" % (r, o, e))
        return json.loads(o) if o.strip() else None

    def git(self, cwd, *a):
        with self.env():
            return sh(cwd, *a)

    # --- cards
    def card(self, id="W-1", name=None, body=None, updated="2026-01-01T00:00:00Z", **kw):
        fm = dict(id=id, title="t", repo=self.repo, base="main", runtime="fake", timebox="2h", scope="small",
                  ambiguity="low", risk="low", verify_cost="cheap")
        fm.update(kw)
        text = "---\n" + "".join("%s: %s\n" % (k, v) for k, v in fm.items() if v is not None) + "---\n"
        text += body if body is not None else SECTIONS.format(updated=updated)
        p = os.path.join(self.tmp, "cards_src", (name or id) + ".md")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    def wt(self, id="W-1"):
        return os.path.join(self.home, "worktrees", fleet.slugify(id))

    # --- sessions
    def hold(self):
        open(self.hold_file, "w").close()
        self.addCleanup(self.release)

    def release(self):
        if os.path.exists(self.hold_file):
            os.remove(self.hold_file)

    def cap(self, item, role, gen, timeout=15):
        p = os.path.join(self.out, "%s_%s_%s.env" % (item, role, gen))
        end = time.time() + timeout
        while not os.path.exists(p):
            self.assertLess(time.time(), end, "fake runtime never started: " + p)
            time.sleep(0.05)
        with open(p) as f:
            return json.load(f)

    def wait_dead(self, item, role="worker", timeout=15):
        end = time.time() + timeout
        while True:
            st = self.j("status", item)["generations"][role]
            if not st["alive"]:
                return
            self.assertLess(time.time(), end, "process still alive")
            time.sleep(0.05)

    def launch(self, card, role="worker", gen=1, wait=True):
        d = self.j("launch", card, "--role", role, "--human")
        item = d["item"]
        env = self.cap(item, role, d["gen"])
        if wait:
            self.wait_dead(item, role)
        return d, env

    def finish(self, item="W-1", commits=1, push=True, report_text="all good\n", result="passed"):
        wd = self.wt(item)
        branch = "fleet/" + fleet.slugify(item)
        for i in range(commits):
            with open(os.path.join(wd, "feat%d.txt" % i), "w") as f:
                f.write("feature %d\n" % i)
            self.git(wd, "add", "-A")
            self.git(wd, "commit", "-m", "feat %d" % i)
        sha = self.git(wd, "rev-parse", "HEAD")
        if push:
            self.git(wd, "push", "origin", branch)
        rp = os.path.join(wd, "report.txt")
        with open(rp, "w") as f:
            f.write(report_text)
        man = {"item": item, "claim": "done", "known_gaps": [], "evidence": [
            {"id": "c1", "kind": "commit", "sha": sha, "remote_ref": "origin/" + branch},
            {"id": "r1", "kind": "report", "path": "report.txt", "sha256": hashlib.sha256(report_text.encode()).hexdigest()}]}
        self.set_handoff(item, man, result)
        return sha, man

    def rewrite(self, path, fn):
        with open(path) as f:
            t = f.read()
        with open(path, "w") as f:
            f.write(fn(t))

    def set_handoff(self, item, man, result="passed"):
        hp = os.path.join(self.wt(item), "HANDOFF.md")
        with open(hp) as f:
            t = f.read()
        t = re.sub(r"^`state:.*$", "`state: awaiting-verify` · `updated: %s` · `blocker: none` · `doing: done` · `result: %s`"
                   % (fleet.now(), result), t, flags=re.M)
        t = re.sub(r"```json\n.*?```", lambda m: "```json\n%s\n```" % json.dumps(man), t, flags=re.S)
        with open(hp, "w") as f:
            f.write(t)

    def ready(self, item="W-1", **kw):
        """worker launched, finished, verified; returns (card, sha, worker_env)."""
        c = self.card(item, **kw)
        _, wenv = self.launch(c)
        sha, man = self.finish(item)
        return c, sha, wenv

    def reviewed(self, item="W-1", verdict="PASS", **kw):
        c, sha, wenv = self.ready(item, **kw)
        v = self.j("verify", item)
        d, renv = self.launch(c, "reviewer", gen=2)
        self.j("review-record", item, verdict, "--candidate-sha", sha, "--manifest-sha256", v["manifest_sha256"], env=renv)
        return c, sha, v["manifest_sha256"]

    def events(self, **kw):
        return self.j("events")

    def kinds(self, item=None):
        return [e["kind"] for e in self.j("events", *(["--item", item] if item else []))]


class TestRoute(Base):
    def route(self, **kw):
        return self.j("route", self.card(name="r%d" % time.time_ns(), **kw))

    def test_scoring_table(self):
        table = [  # scope, ambiguity, risk, verify_cost -> score, tier
            ("small", "low", "low", "cheap", 0, "low"),
            ("medium", "low", "low", "cheap", 1, "low"),
            ("small", "medium", "low", "cheap", 2, "medium"),
            ("medium", "low", "medium", "cheap", 2, "medium"),
            ("large", "medium", "low", "cheap", 4, "high"),
            ("large", "medium", "medium", "costly", 6, "high"),
            ("large", "medium", "high", "costly", 7, "xhigh"),
            ("large", "high", "high", "costly", 9, "xhigh"),
        ]
        for s, a, r, v, score, tier in table:
            with self.subTest(case=(s, a, r, v)):
                d = self.route(scope=s, ambiguity=a, risk=r, verify_cost=v)
                self.assertEqual((d["score"], d["tier"]), (score, tier))
                self.assertIsNone(d["override"])
                self.assertNotEqual(d["worker"]["tier"], "max")

    def test_floors(self):
        d = self.route(risk="high")  # score 2 -> medium, floored to high
        self.assertEqual((d["score"], d["computed_tier"]), (2, "high"))
        self.assertTrue(any("floor" in r for r in d["reasons"]))
        d = self.route(ambiguity="high", scope="small")
        self.assertEqual(d["tier"], "high")

    def test_thresholds_from_config(self):
        self.cfg["routing"] = {"thresholds": {"medium": 1, "high": 2, "xhigh": 3}}
        self.write_cfg()
        self.assertEqual(self.route(scope="medium")["tier"], "medium")
        self.assertEqual(self.route(scope="large")["tier"], "high")

    def test_override_reported_next_to_computed(self):
        d = self.route(effort="max", effort_reason="human decision")
        self.assertEqual((d["computed_tier"], d["tier"]), ("low", "max"))
        self.assertEqual(d["override"]["reason"], "human decision")
        self.assertEqual(d["reviewer"]["tier"], "max")

    def test_max_never_routed_by_score(self):
        d = self.route(scope="large", ambiguity="high", risk="high", verify_cost="costly")
        self.assertEqual(d["tier"], "xhigh")

    def test_reviewer_tier(self):
        self.assertEqual(self.route()["reviewer"]["tier"], "medium")  # worker low -> reviewer_min
        self.assertEqual(self.route(risk="medium")["reviewer"]["tier"], "high")  # risky
        d = self.route(scope="large", ambiguity="high", risk="high", verify_cost="costly")
        self.assertEqual(d["reviewer"]["tier"], "xhigh")

    def test_tier_resolution_and_model(self):
        d = self.route()
        self.assertEqual((d["worker"]["model"], d["worker"]["effort"]), ("m-fast", "low"))
        d = self.route(scope="small", ambiguity="medium")
        self.assertEqual((d["worker"]["model"], d["worker"]["effort"]), ("m-main", "medium"))

    def test_clamp_up_and_down(self):
        d = self.route(runtime="partial")  # low unsupported -> medium
        self.assertTrue(d["worker"]["clamped"])
        self.assertEqual((d["worker"]["effective_tier"], d["worker"]["effort"], d["worker"]["clamp_direction"]),
                         ("medium", "medium", "up"))
        d = self.route(runtime="partial", scope="large", ambiguity="high", risk="high", verify_cost="costly")
        self.assertEqual((d["worker"]["effective_tier"], d["worker"]["clamp_direction"]), ("high", "down"))
        d = self.route(runtime="partial", scope="small", ambiguity="medium")
        self.assertFalse(d["worker"]["clamped"])

    def test_template_fails_lint_cleanly(self):
        tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "dispatch-card.template.md")
        rc, o, e = self.run_cli("route", tpl)
        self.assertEqual(rc, 1)
        self.assertIn("placeholder", e)
        self.assertNotIn("Traceback", e)


class TestLint(Base):
    def lint(self, **kw):
        rc, o, e = self.run_cli("card", "lint", self.card(name="l%d" % time.time_ns(), **kw), "--json")
        return rc, json.loads(o)

    def test_ok(self):
        rc, d = self.lint()
        self.assertEqual((rc, d["errors"]), (0, []))

    def test_failures(self):
        cases = [({"timebox": None}, "missing required key: timebox"), ({"scope": "huge"}, "scope must be"),
                 ({"risk": "extreme"}, "risk must be"), ({"verify_cost": "free"}, "verify_cost must be"),
                 ({"ambiguity": "none"}, "ambiguity must be"), ({"effort": "high"}, "effort_reason"),
                 ({"base": None}, "requires base"), ({"timebox": "soon"}, "timebox"), ({"id": "{{ID}}"}, "placeholder"),
                 ({"effort": "ultra", "effort_reason": "x"}, "effort must be")]
        for kw, needle in cases:
            with self.subTest(kw=kw):
                rc, d = self.lint(**kw)
                self.assertEqual(rc, 1)
                self.assertTrue(any(needle in x for x in d["errors"]), d["errors"])

    def test_missing_sections_and_front_matter(self):
        rc, d = self.lint(body="## Contract\n\n## State\n")
        self.assertTrue(any("## Progress" in x for x in d["errors"]) and any("## Evidence" in x for x in d["errors"]))
        p = os.path.join(self.tmp, "nofm.md")
        open(p, "w").write("# no front matter\n")
        rc, o, e = self.run_cli("card", "lint", p, "--json")
        self.assertEqual(rc, 1)

    def test_non_code_card_needs_no_base(self):
        rc, d = self.lint(repo="-", base=None)
        self.assertEqual(rc, 0)


class TestLaunch(Base):
    def test_init_layout(self):
        for d in ("cards", "worktrees", "work", "logs"):
            self.assertTrue(os.path.isdir(os.path.join(self.home, d)))
        self.assertTrue(os.path.exists(os.path.join(self.home, "fleet.db")))
        self.assertTrue(os.path.isdir(os.path.join(self.home, "hostq", fleet.socket.gethostname(), "requests")))

    def test_uninitialised_home_fails_closed(self):
        rc, o, e = self.run_cli("status", env={"FLEET_HOME": os.path.join(self.tmp, "nope")})
        self.assertEqual(rc, 1)
        self.assertIn("init", e)

    def test_dry_run_changes_nothing(self):
        c = self.card()
        before = sorted(os.listdir(self.home)), sorted(os.listdir(os.path.join(self.home, "worktrees")))
        d = self.j("launch", c, "--dry-run")
        self.assertTrue(d["dry_run"])
        self.assertEqual(d["env"], fleet.ENV_NAMES)
        self.assertNotIn("capability_sha256", json.dumps(d))
        self.assertEqual((d["tier"], d["effort"]), ("low", "low"))
        self.assertEqual(self.j("status"), [])
        self.assertEqual(self.j("events"), [])
        self.assertFalse(os.path.exists(self.wt()))
        self.assertEqual(os.listdir(os.path.join(self.home, "worktrees")), [])
        self.assertEqual(sorted(os.listdir(self.home)), before[0])
        self.assertEqual(self.git(self.repo, "branch", "--list", "fleet/*"), "")

    def test_launch_requires_authority(self):
        rc, o, e = self.run_cli("launch", self.card())
        self.assertEqual(rc, 3)

    def test_real_launch(self):
        c = self.card()
        d, env = self.launch(c)
        wd = self.wt()
        self.assertEqual(self.git(wd, "rev-parse", "--abbrev-ref", "HEAD"), "fleet/w-1")
        self.assertEqual(self.git(wd, "status", "--porcelain"), "")  # HANDOFF.md excluded
        self.assertTrue(os.path.exists(os.path.join(wd, "HANDOFF.md")))
        self.assertEqual(env["FLEET_ITEM"], "W-1")
        self.assertEqual((env["FLEET_ROLE"], env["FLEET_GEN"]), ("worker", "1"))
        # prompt content
        prompt = open(os.path.join(self.out, "W-1_worker_1.prompt")).read()
        self.assertIn("only task input", prompt)
        self.assertIn(os.path.join(wd, "HANDOFF.md"), prompt)
        self.assertIn("FLEET_CAPABILITY", prompt)
        self.assertNotIn(env["FLEET_CAPABILITY"], prompt)
        # generation row + log + lazily captured session id
        st = self.j("status", "W-1")
        g = st["generations"]["worker"]
        self.assertTrue(re.match(r"^[0-9a-f-]{36}$", g["session_id"]))
        self.assertIn("thread_id", open(g["log_path"]).read())
        self.assertEqual(st["state"], "in-progress")
        self.assertEqual(st["handoff"]["state"], "in-progress")
        # token never stored, only its sha256
        raw = open(os.path.join(self.home, "fleet.db"), "rb").read()
        self.assertNotIn(env["FLEET_CAPABILITY"].encode(), raw)
        self.assertIn(hashlib.sha256(env["FLEET_CAPABILITY"].encode()).hexdigest().encode(), raw)
        self.assertIn("launched", self.kinds("W-1"))

    def test_assign_mode_uses_uuid(self):
        d, env = self.launch(self.card(runtime="assign"))
        argv = json.load(open(os.path.join(self.out, "W-1_worker_1.argv")))
        self.assertTrue(re.match(r"^[0-9a-f-]{36}$", argv[1]))
        self.assertEqual(self.j("status", "W-1")["generations"]["worker"]["session_id"], argv[1])

    def test_relaunch_reuses_worktree_and_handoff(self):
        c = self.card()
        self.launch(c)
        with open(os.path.join(self.wt(), "HANDOFF.md"), "a") as f:
            f.write("\nEVOLVED\n")
        d = self.j("launch", c, "--human")
        self.assertEqual(d["gen"], 2)
        self.assertIn("EVOLVED", open(os.path.join(self.wt(), "HANDOFF.md")).read())

    def test_live_generation_blocks_plain_relaunch(self):
        self.hold()
        c = self.card()
        self.launch(c, wait=False)
        rc, o, e = self.run_cli("launch", c, "--human")
        self.assertEqual(rc, 1)
        self.assertIn("live", e)

    def test_changed_contract_refused(self):
        c = self.card()
        self.launch(c)
        with open(c, "a"):
            pass
        text = open(c).read().replace("do the thing", "do another thing")
        open(c, "w").write(text)
        rc, o, e = self.run_cli("launch", c, "--human")
        self.assertEqual(rc, 1)
        self.assertIn("contract", e)

    def test_limits(self):
        self.cfg["limits"]["max_active_per_repo"] = 1
        self.write_cfg()
        self.hold()
        self.launch(self.card("W-1"), wait=False)
        rc, o, e = self.run_cli("launch", self.card("W-2"), "--human")
        self.assertEqual(rc, 1)
        self.assertIn("max_active_per_repo", e)
        self.assertEqual(len(self.j("status")), 1)
        self.cfg["limits"].update(max_active_per_repo=5, max_active=1)
        self.write_cfg()
        rc, o, e = self.run_cli("launch", self.card("W-3"), "--human")
        self.assertIn("max_active=1", e)

    def test_non_code_card_work_dir(self):
        d, env = self.launch(self.card("N-1", repo="-", base=None))
        self.assertEqual(d["workdir"], os.path.join(self.home, "work", "n-1"))
        self.assertTrue(os.path.exists(os.path.join(d["workdir"], "HANDOFF.md")))

    def test_reviewer_launch_records_independent_generation(self):
        c, sha, _ = self.ready()
        v = self.j("verify", "W-1")
        d, env = self.launch(c, "reviewer", gen=2)
        self.assertEqual(d["workdir"], self.wt())
        self.assertNotEqual(d["gen"], 1)
        prompt = open(os.path.join(self.out, "W-1_reviewer_2.prompt")).read()
        self.assertIn(sha, prompt)
        self.assertIn(v["manifest_sha256"], prompt)
        self.assertIn("review-record", prompt)
        self.assertEqual(self.j("status", "W-1")["state"], "in-review")
        ev = [e for e in self.j("events") if e["kind"] == "launched" and e["detail"]["role"] == "reviewer"][0]
        self.assertEqual(ev["detail"]["reviewing_worker_gen"], 1)

    def test_reviewer_refused_when_verify_fails(self):
        c = self.card()
        self.launch(c)
        rc, o, e = self.run_cli("launch", c, "--role", "reviewer", "--human")
        self.assertEqual(rc, 1)
        self.assertIn("verify failed", e)


class TestPreflight(Base):
    def pf(self, env, target, action="edit"):
        rc, o, e = self.run_cli("preflight", "--action", action, "--target", target, "--json", env=env)
        return rc, json.loads(o)

    def setUp(self):
        super().setUp()
        self.c = self.card()
        _, self.env1 = self.launch(self.c)

    def test_allow(self):
        for action in ("edit", "commit", "build"):
            rc, d = self.pf(self.env1, os.path.join(self.wt(), "new.txt"), action)
            self.assertEqual((rc, d["allowed"]), (0, True), d)

    def test_wrong_token(self):
        rc, d = self.pf(dict(self.env1, FLEET_CAPABILITY="0" * 32), self.wt())
        self.assertEqual((rc, d["allowed"]), (3, False))
        self.assertTrue(d["permitted_actions"])

    def test_missing_env_and_wrong_role(self):
        rc, d = self.pf({}, self.wt())
        self.assertEqual(rc, 3)
        rc, d = self.pf(dict(self.env1, FLEET_ROLE="reviewer"), self.wt())
        self.assertEqual(rc, 3)

    def test_stale_generation_after_escalate(self):
        self.j("escalate", "W-1", "--human")
        self.cap("W-1", "worker", 2)
        rc, d = self.pf(self.env1, self.wt())
        self.assertEqual(rc, 3)
        self.assertIn("stale", d["reason"])
        # even a fresh unfenced check of gen1 token against gen2 would fail; gen2 works
        rc, d = self.pf(self.cap("W-1", "worker", 2), self.wt())
        self.assertEqual(rc, 0)

    def test_path_escape(self):
        rc, d = self.pf(self.env1, os.path.join(self.wt(), "..", "elsewhere.txt"))
        self.assertEqual(rc, 3)
        self.assertIn("outside", d["reason"])
        rc, d = self.pf(self.env1, self.tmp)
        self.assertEqual(rc, 3)

    def test_symlink_escape(self):
        os.symlink(self.tmp, os.path.join(self.wt(), "link"))
        rc, d = self.pf(self.env1, os.path.join(self.wt(), "link", "x.txt"))
        self.assertEqual(rc, 3)

    def test_wrong_branch_denies_commit_and_build_only(self):
        self.git(self.wt(), "checkout", "-b", "somewhere-else")
        self.assertEqual(self.pf(self.env1, self.wt(), "commit")[0], 3)
        self.assertEqual(self.pf(self.env1, self.wt(), "build")[0], 3)
        self.assertEqual(self.pf(self.env1, self.wt(), "edit")[0], 0)

    def test_wrong_host(self):
        fleet_db = os.path.join(self.home, "fleet.db")
        import sqlite3
        c = sqlite3.connect(fleet_db)
        c.execute("UPDATE generations SET host='elsewhere'")
        c.commit()
        c.close()
        rc, d = self.pf(self.env1, self.wt())
        self.assertEqual(rc, 3)
        self.assertIn("host", d["reason"])


class TestSteerEscalate(Base):
    def test_steer_rate_limit(self):
        self.hold()
        self.launch(self.card(), wait=False)
        for i in range(2):
            d = self.j("steer", "W-1", "fix %d" % i, "--human")
            self.assertFalse(d["delivered"])
        rc, o, e = self.run_cli("steer", "W-1", "fix 3", "--human")
        self.assertEqual(rc, 4)
        self.assertIn("decision", e)
        self.assertEqual(self.kinds("W-1").count("steer"), 2)

    def test_steer_needs_authority(self):
        self.launch(self.card())
        rc, o, e = self.run_cli("steer", "W-1", "x")
        self.assertEqual(rc, 3)
        _, wenv = None, self.cap("W-1", "worker", 1)
        rc, o, e = self.run_cli("steer", "W-1", "x", env=wenv)
        self.assertEqual(rc, 3)

    def test_steer_idle_session_delivers_via_resume(self):
        self.launch(self.card())
        d = self.j("steer", "W-1", "please fix the typo", "--human")
        self.assertTrue(d["delivered"])
        env2 = self.cap("W-1", "worker", 2)
        prompt = open(os.path.join(self.out, "W-1_worker_2.prompt")).read()
        self.assertIn("please fix the typo", prompt)
        argv = json.load(open(os.path.join(self.out, "W-1_worker_2.argv")))
        self.assertEqual(argv[0], "resume")
        self.assertTrue(re.match(r"^[0-9a-f-]{36}$", argv[1]))
        self.assertNotEqual(env2["FLEET_CAPABILITY"], "")

    def test_resume_refuses_live_session(self):
        self.hold()
        self.launch(self.card(), wait=False)
        rc, o, e = self.run_cli("resume", "W-1", "hello", "--human")
        self.assertEqual(rc, 1)
        self.assertIn("still running", e)

    def test_resume_with_message_same_effort(self):
        self.launch(self.card(scope="small", ambiguity="medium"))
        self.j("resume", "W-1", "carry on", "--human")
        self.cap("W-1", "worker", 2)
        g = self.j("status", "W-1")["generations"]["worker"]
        self.assertEqual((g["gen"], g["tier"], g["effort"]), (2, "medium", "medium"))
        self.assertIn("carry on", open(os.path.join(self.out, "W-1_worker_2.prompt")).read())

    def test_escalate_ladder(self):
        self.launch(self.card())  # low
        seen = []
        for expect in ("medium", "high", "xhigh"):
            d = self.j("escalate", "W-1", "--reason", "gap", "--human")
            seen.append(d["tier"])
            self.assertEqual(d["tier"], expect)
            self.cap("W-1", "worker", d["gen"])
        self.assertEqual(seen, ["medium", "high", "xhigh"])
        rc, o, e = self.run_cli("escalate", "W-1", "--human")
        self.assertEqual(rc, 1)
        self.assertIn("split the card", e)
        self.assertEqual(self.kinds("W-1").count("escalated"), 3)
        self.assertEqual(self.j("status", "W-1")["generations"]["worker"]["effort"], "xhigh")

    def test_escalate_fences_old_generation_and_reuses_worktree(self):
        self.hold()
        self.launch(self.card(), wait=False)
        d = self.j("escalate", "W-1", "--human")
        self.assertEqual(d["workdir"], self.wt())
        import sqlite3
        c = sqlite3.connect(os.path.join(self.home, "fleet.db"))
        self.assertEqual(c.execute("SELECT fenced FROM generations WHERE gen=1").fetchone()[0], 1)
        c.close()
        self.cap("W-1", "worker", 2)
        with open(os.path.join(self.out, "W-1_worker_2.prompt")) as f:
            self.assertIn("continue from the evolved HANDOFF", f.read())

    def test_escalate_refuses_reused_tier(self):
        import sqlite3
        self.launch(self.card(scope="large", ambiguity="medium"))  # high
        c = sqlite3.connect(os.path.join(self.home, "fleet.db"))
        c.execute("INSERT INTO generations(item,gen,role,runtime,tier,fenced) VALUES('W-1',0,'worker','fake','xhigh',1)")
        c.commit()
        c.close()
        rc, o, e = self.run_cli("escalate", "W-1", "--human")
        self.assertEqual(rc, 1)
        self.assertIn("already used", e)

    def test_escalate_needs_coordinator(self):
        self.launch(self.card())
        rc, o, e = self.run_cli("escalate", "W-1", env=self.cap("W-1", "worker", 1))
        self.assertEqual(rc, 3)

    def test_rework_appends_block_and_resumes(self):
        self.launch(self.card())
        req = os.path.join(self.tmp, "req.md")
        open(req, "w").write("1. handle the empty input\n")
        d = self.j("rework", "W-1", "--requirements-file", req, "--human")
        self.assertTrue(d["resumed"])
        text = open(os.path.join(self.wt(), "HANDOFF.md")).read()
        self.assertIn("REWORK:\n1. handle the empty input", text)
        self.assertLess(text.index("REWORK:"), text.index("## Evidence"))
        self.assertEqual(self.j("status", "W-1")["state"], "in-progress")


class TestVerify(Base):
    def test_happy(self):
        self.ready()
        d = self.j("verify", "W-1")
        self.assertTrue(d["ok"], d)
        self.assertEqual(len(d["manifest_sha256"]), 64)
        man = json.loads(re.search(r"```json\n(.*?)```", open(os.path.join(self.wt(), "HANDOFF.md")).read(), re.S).group(1))
        want = hashlib.sha256(json.dumps(man, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(d["manifest_sha256"], want)

    def fails(self):
        rc, o, e = self.run_cli("verify", "W-1", "--json")
        self.assertEqual(rc, 1)
        return json.loads(o)["failures"]

    def has(self, needle):
        self.assertTrue(any(needle in f for f in self.fails()), self.fails())

    def test_contract_edited(self):
        self.ready()
        hp = os.path.join(self.wt(), "HANDOFF.md")
        self.rewrite(hp, lambda t: t.replace("do the thing", "do something easier"))
        self.has("contract")

    def test_sha256_mismatch(self):
        self.ready()
        open(os.path.join(self.wt(), "report.txt"), "w").write("tampered\n")
        self.has("sha256 mismatch")

    def test_unpushed_commit(self):
        c = self.card()
        self.launch(c)
        self.finish(push=False)
        self.has("not the remote head")

    def test_two_commits_above_base(self):
        self.launch(self.card())
        self.finish(commits=2)
        self.has("exactly one commit")

    def test_not_awaiting_verify_or_no_result(self):
        self.launch(self.card())
        self.has("expected awaiting-verify")
        self.finish()
        hp = os.path.join(self.wt(), "HANDOFF.md")
        self.rewrite(hp, lambda t: t.replace(" · `result: passed`", ""))
        self.has("no result")

    def test_bad_manifest(self):
        self.launch(self.card())
        sha, man = self.finish()
        man["item"] = "W-9"
        man["evidence"][0]["sha"] = "abc"
        man["evidence"][1]["path"] = "../escape.txt"
        man["extra"] = 1
        self.set_handoff("W-1", man)
        f = " | ".join(self.fails())
        for needle in ("manifest item", "40 hex", "escapes", "unknown key"):
            self.assertIn(needle, f)

    def test_wrong_remote_ref(self):
        self.launch(self.card())
        sha, man = self.finish()
        man["evidence"][0]["remote_ref"] = "origin/main"
        self.set_handoff("W-1", man)
        self.has("item branch")

    def test_unparseable_evidence(self):
        self.launch(self.card())
        self.finish()
        hp = os.path.join(self.wt(), "HANDOFF.md")
        self.rewrite(hp, lambda t: re.sub(r"```json\n.*?```", "```json\n{not json\n```", t, flags=re.S))
        self.has("unreadable")

    def test_non_code_card(self):
        self.launch(self.card("N-1", repo="-", base=None))
        wd = os.path.join(self.home, "work", "n-1")
        open(os.path.join(wd, "out.txt"), "w").write("x")
        man = {"item": "N-1", "claim": "c", "known_gaps": [], "evidence": [
            {"id": "a", "kind": "artifact", "path": "out.txt", "sha256": hashlib.sha256(b"x").hexdigest()}]}
        hp = os.path.join(wd, "HANDOFF.md")
        t = open(hp).read()
        t = re.sub(r"^`state:.*$", "`state: awaiting-verify` · `result: ok`", t, flags=re.M)
        t = re.sub(r"```json\n.*?```", lambda m: "```json\n%s\n```" % json.dumps(man), t, flags=re.S)
        open(hp, "w").write(t)
        self.assertTrue(self.j("verify", "N-1")["ok"])


class TestReviewAndLand(Base):
    def test_review_record_binds_hashes(self):
        c, sha, wenv = self.ready()
        v = self.j("verify", "W-1")
        d, renv = self.launch(c, "reviewer", gen=2)
        rr = lambda s, m, env=renv, verdict="PASS": self.run_cli(  # noqa: E731
            "review-record", "W-1", verdict, "--candidate-sha", s, "--manifest-sha256", m, env=env)
        self.assertEqual(rr(sha, "0" * 64)[0], 1)  # wrong manifest hash
        self.assertEqual(rr("1" * 40, v["manifest_sha256"])[0], 1)  # wrong sha
        self.assertEqual(rr(sha, v["manifest_sha256"], env=wenv)[0], 3)  # worker cannot review
        self.assertEqual(rr(sha, v["manifest_sha256"], env={})[0], 3)
        self.assertEqual(rr(sha, v["manifest_sha256"])[0], 0)
        self.assertIn("review:PASS", self.kinds("W-1"))
        self.assertEqual(self.j("status", "W-1")["state"], "in-review")  # reviewer did not change state
        # any manifest edit invalidates further verdicts
        man = json.loads(re.search(r"```json\n(.*?)```", open(os.path.join(self.wt(), "HANDOFF.md")).read(), re.S).group(1))
        man["claim"] = "changed"
        self.set_handoff("W-1", man)
        self.assertEqual(rr(sha, v["manifest_sha256"])[0], 1)

    def test_reviewer_cannot_mark(self):
        c, sha, wenv = self.ready()
        d, renv = self.launch(c, "reviewer", gen=2)
        rc, o, e = self.run_cli("mark", "W-1", "done", env=renv)
        self.assertEqual(rc, 3)
        rc, o, e = self.run_cli("mark", "W-1", "in-progress", env=renv)
        self.assertEqual(rc, 3)

    def test_land_happy(self):
        c, sha, mh = self.reviewed()
        before = self.git(self.origin, "rev-parse", "main")
        rc, o, e = self.run_cli("land", "W-1", "--sha", sha, "--human")
        self.assertEqual(rc, 0, e)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), sha)
        self.assertEqual(self.git(self.origin, "rev-parse", sha + "^"), before)
        self.assertEqual(self.j("status", "W-1")["state"], "landed")
        self.assertIn("landed", self.kinds("W-1"))
        rc, o, e = self.run_cli("land", "W-1", "--sha", sha, "--human")
        self.assertEqual(rc, 1)  # already landed
        self.assertEqual(self.run_cli("mark", "W-1", "done", "--human")[0], 0)

    def test_land_needs_coordinator_authority(self):
        c, sha, mh = self.reviewed()
        self.assertEqual(self.run_cli("land", "W-1", "--sha", sha)[0], 3)
        self.assertEqual(self.run_cli("land", "W-1", "--sha", sha, env=self.cap("W-1", "worker", 1))[0], 3)

    def test_land_refuses_without_pass(self):
        c, sha, mh = self.reviewed(verdict="REWORK")
        rc, o, e = self.run_cli("land", "W-1", "--sha", sha, "--human")
        self.assertEqual(rc, 1)
        self.assertIn("not PASS", e)
        self.assertNotEqual(self.git(self.origin, "rev-parse", "main"), sha)

    def test_land_refuses_unreviewed_and_other_sha(self):
        c, sha, wenv = self.ready()
        rc, o, e = self.run_cli("land", "W-1", "--sha", sha, "--human")
        self.assertIn("not PASS", e)

    def test_land_refuses_sha_other_than_reviewed(self):
        c, sha, mh = self.reviewed()
        rc, o, e = self.run_cli("land", "W-1", "--sha", "f" * 40, "--human")
        self.assertEqual(rc, 1)
        self.assertIn("bound to", e)

    def test_land_refuses_non_fast_forward(self):
        c, sha, mh = self.reviewed()
        with open(os.path.join(self.other, "moved.txt"), "w") as f:
            f.write("m")
        self.git(self.other, "add", "-A")
        self.git(self.other, "commit", "-m", "base moved")
        self.git(self.other, "push", "origin", "main")
        moved = self.git(self.origin, "rev-parse", "main")
        rc, o, e = self.run_cli("land", "W-1", "--sha", sha, "--human")
        self.assertEqual(rc, 1)
        self.assertIn("fast-forward", e)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), moved)

    def _move_base(self, path, text="m"):
        full = os.path.join(self.other, path)
        os.makedirs(os.path.dirname(full) or self.other, exist_ok=True)
        self.git(self.other, "pull", "-q", "origin", "main")
        with open(full, "w") as f:
            f.write(text)
        self.git(self.other, "add", "-A")
        self.git(self.other, "commit", "-m", "base moved: " + path)
        self.git(self.other, "push", "origin", "main")
        return self.git(self.origin, "rev-parse", "main")

    def _cand_files(self, sha):
        return set(self.git(self.origin, "diff", "--name-only", sha + "^", sha).split())

    def test_land_rebase_equivalent_replays_onto_disjoint_drift(self):
        c, sha, mh = self.reviewed()
        moved = self._move_base("unrelated/doc.txt")
        rc, o, e = self.run_cli("land", "W-1", "--sha", sha, "--human", "--rebase-equivalent",
                                "--guard", "guarded/")
        self.assertEqual(rc, 0, e)
        head = self.git(self.origin, "rev-parse", "main")
        self.assertNotEqual(head, sha)
        self.assertEqual(self.git(self.origin, "rev-parse", head + "^"), moved)  # replayed on top of drift
        self.assertEqual(self._cand_files(head), self._cand_files(sha))
        self.assertEqual(self.j("status", "W-1")["state"], "landed")
        self.assertIn("land:rebased", self.kinds("W-1"))

    def test_land_rebase_equivalent_refuses_guarded_drift(self):
        c, sha, mh = self.reviewed()
        moved = self._move_base("guarded/rule.json")
        rc, o, e = self.run_cli("land", "W-1", "--sha", sha, "--human", "--rebase-equivalent",
                                "--guard", "guarded/")
        self.assertEqual(rc, 1)
        self.assertIn("guarded", e)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), moved)

    def test_land_rebase_equivalent_refuses_overlapping_drift(self):
        c, sha, mh = self.reviewed()
        f = sorted(self._cand_files(sha))[0]
        moved = self._move_base(f, "conflicting\n")
        rc, o, e = self.run_cli("land", "W-1", "--sha", sha, "--human", "--rebase-equivalent")
        self.assertEqual(rc, 1)
        self.assertIn("candidate files", e)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), moved)

    def test_land_unknown_result_and_reconcile(self):
        c, sha, mh = self.reviewed()
        real = fleet.git_push

        def lost_ack(repo, s, base, timeout):  # push happens, then the answer is lost
            real(repo, s, base, timeout)
            raise subprocess.TimeoutExpired("git push", timeout)

        fleet.git_push = lost_ack
        try:
            rc, o, e = self.run_cli("land", "W-1", "--sha", sha, "--human")
        finally:
            fleet.git_push = real
        self.assertEqual(rc, 5)
        urgent = self.j("events", "--urgent-only")
        self.assertEqual([x["kind"] for x in urgent], ["result_unknown"])
        self.assertNotEqual(self.j("status", "W-1")["state"], "landed")
        rc, o, e = self.run_cli("land", "W-1", "--sha", sha, "--human")  # must not blindly retry
        self.assertEqual(rc, 1)
        self.assertIn("--reconcile", e)
        rc, o, e = self.run_cli("land", "W-1", "--sha", sha, "--reconcile", "--human")
        self.assertEqual(rc, 0, e)
        self.assertEqual(self.j("status", "W-1")["state"], "landed")

    def test_reconcile_when_push_did_not_happen(self):
        c, sha, mh = self.reviewed()
        real = fleet.git_push

        def never(repo, s, base, timeout):
            raise subprocess.TimeoutExpired("git push", timeout)

        fleet.git_push = never
        try:
            self.assertEqual(self.run_cli("land", "W-1", "--sha", sha, "--human")[0], 5)
        finally:
            fleet.git_push = real
        rc, o, e = self.run_cli("land", "W-1", "--sha", sha, "--reconcile", "--human")
        self.assertEqual(rc, 1)
        self.assertNotEqual(self.j("status", "W-1")["state"], "landed")
        self.assertIn("land:not_landed", self.kinds("W-1"))
        self.assertEqual(self.run_cli("land", "W-1", "--sha", sha, "--human")[0], 0)  # known result: may retry

    def test_reconcile_without_unknown_refused(self):
        c, sha, mh = self.reviewed()
        self.assertEqual(self.run_cli("land", "W-1", "--sha", sha, "--reconcile", "--human")[0], 1)


class TestMark(Base):
    def test_authority_and_terminal(self):
        self.launch(self.card())
        wenv = self.cap("W-1", "worker", 1)
        self.assertEqual(self.run_cli("mark", "W-1", "blocked", env=wenv)[0], 0)
        self.assertEqual(self.j("status", "W-1")["state"], "blocked")
        self.assertEqual(self.run_cli("mark", "W-1", "landed", env=wenv)[0], 3)
        self.assertEqual(self.run_cli("mark", "W-1", "done", env=wenv)[0], 3)
        self.assertEqual(self.run_cli("mark", "W-1", "blocked")[0], 3)  # no actor
        self.assertEqual(self.run_cli("mark", "W-1", "in-progress", "--human")[0], 3)  # human is not a worker
        self.assertEqual(self.run_cli("mark", "W-1", "bogus", "--human")[0], 1)
        self.assertEqual(self.run_cli("mark", "W-1", "cancelled", "--human")[0], 0)
        self.assertEqual(self.run_cli("mark", "W-1", "in-progress", env=wenv)[0], 1)  # terminal never reverts
        self.assertEqual(self.run_cli("mark", "W-1", "done", "--human")[0], 1)

    def test_session_cannot_claim_human(self):
        self.launch(self.card())
        wenv = self.cap("W-1", "worker", 1)
        self.assertEqual(self.run_cli("mark", "W-1", "cancelled", "--human", env=wenv)[0], 3)
        self.assertEqual(self.run_cli("land", "W-1", "--sha", "0" * 40, "--human", env=wenv)[0], 3)

    def test_worker_cannot_mark_other_item(self):
        self.launch(self.card("W-1"))
        self.launch(self.card("W-2", name="w2"))
        self.assertEqual(self.run_cli("mark", "W-2", "blocked", env=self.cap("W-1", "worker", 1))[0], 3)

    def test_awaiting_verify_requires_verify(self):
        self.launch(self.card())
        wenv = self.cap("W-1", "worker", 1)
        self.assertEqual(self.run_cli("mark", "W-1", "awaiting-verify", env=wenv)[0], 1)
        self.finish()
        self.assertEqual(self.run_cli("mark", "W-1", "awaiting-verify", env=wenv)[0], 0)

    def test_done_needs_landed_for_code_card(self):
        self.launch(self.card())
        self.assertEqual(self.run_cli("mark", "W-1", "done", "--human")[0], 1)

    def test_coordinator_capability_works(self):
        self.launch(self.card("W-1"))
        d, cenv = self.launch(self.card("C-1", repo="-", base=None), "coordinator")
        self.assertEqual(self.run_cli("mark", "W-1", "cancelled", env=cenv)[0], 0)
        self.assertEqual(self.run_cli("launch", self.card("C-2", repo="-", base=None), "--role", "coordinator", env=cenv)[0], 3)


class TestDecisions(Base):
    def setUp(self):
        super().setUp()
        self.launch(self.card())

    def test_tiers(self):
        d = self.j("decide-open", "W-1", "--tier", "self", "--question", "q?", "--human")
        self.assertEqual(d["state"], "void")
        d = self.j("decide-open", "W-1", "--tier", "notify", "--question", "fyi", "--human")
        self.assertEqual(d["state"], "void")
        rc, o, e = self.run_cli("decide-open", "W-1", "--tier", "human_required", "--question", "q", "--human")
        self.assertEqual(rc, 1)
        for missing in ("options", "default", "deadline"):
            self.assertIn(missing, e)
        args = ["decide-open", "W-1", "--tier", "human_required", "--question", "ship?", "--options", "A|B",
                "--default", "A", "--deadline", "2030-01-01T00:00:00Z", "--human"]
        self.assertEqual(self.run_cli(*args[:-1] + ["--deadline", "someday", "--human"])[0], 1)
        d = self.j(*args)
        self.assertEqual(d["id"], "D-3")
        urgent = self.j("events", "--urgent-only")
        self.assertEqual([x["kind"] for x in urgent], ["decision:human_required"])

    def open_human(self):
        return self.j("decide-open", "W-1", "--tier", "human_required", "--question", "ship?", "--options", "A|B",
                      "--default", "A", "--deadline", "2030-01-01T00:00:00Z", "--human")["id"]

    def test_answer_only_human_and_poll_acks_once(self):
        did = self.open_human()
        cenv = self.launch(self.card("C-1", repo="-", base=None), "coordinator")[1]
        self.assertEqual(self.run_cli("decide-answer", did, "B", env=cenv)[0], 3)
        self.assertEqual(self.run_cli("decide-answer", did, "B", env=self.cap("W-1", "worker", 1))[0], 3)
        self.assertEqual(self.j("decide-poll", "--ack", env=cenv), [])
        self.assertEqual(self.run_cli("decide-answer", did, "B", "--human")[0], 0)
        self.assertEqual(self.run_cli("decide-answer", did, "C", "--human")[0], 1)  # no longer open
        self.assertIn("decision:answered", self.kinds("W-1"))
        first = self.j("decide-poll", "--item", "W-1", "--ack", env=cenv)
        self.assertEqual([(d["id"], d["answer"], d["state"]) for d in first], [(did, "B", "acked")])
        self.assertEqual(self.j("decide-poll", "--ack", env=cenv), [])

    def test_poll_requires_coordinator(self):
        self.assertEqual(self.run_cli("decide-poll", "--ack", env=self.cap("W-1", "worker", 1))[0], 3)


class TestEvents(Base):
    def test_events_filters_and_wait(self):
        self.launch(self.card())
        t0 = time.time()
        rc, o, e = self.run_cli("wait", "--after", "999", "--timeout", "1")
        self.assertEqual(rc, 2)
        self.assertGreaterEqual(time.time() - t0, 0.9)
        t0 = time.time()
        rc, o, e = self.run_cli("wait", "--after", "0", "--timeout", "30")
        self.assertEqual(rc, 0)
        self.assertLess(time.time() - t0, 1.5)
        first = json.loads(o.splitlines()[0])
        self.assertEqual(first["kind"], "launched")
        rc, o, e = self.run_cli("wait", "--after", str(first["id"]), "--timeout", "1", "--item", "OTHER")
        self.assertEqual(rc, 2)
        self.assertEqual(self.j("events", "--after", str(first["id"])), [])
        self.assertEqual(self.j("events", "--urgent-only"), [])
        self.assertEqual(len(self.j("events", "--item", "W-1")), 1)

    def test_wait_urgent_only_and_heartbeat(self):
        self.launch(self.card())
        self.assertEqual(self.run_cli("wait", "--after", "0", "--timeout", "1", "--urgent-only")[0], 2)
        self.assertEqual(self.run_cli("heartbeat", "--role", "supervisor")[0], 0)
        self.assertEqual(self.run_cli("heartbeat", "--role", "boss")[0], 2)

    def test_request(self):
        self.launch(self.card())
        self.assertEqual(self.run_cli("request", "dispatch", "--item", "W-1", "--message", "more", "--human")[0], 0)
        self.assertEqual(self.run_cli("request", "dispatch")[0], 3)
        self.assertIn("request:dispatch", self.kinds("W-1"))


class TestWatch(Base):
    def watch(self, *extra):
        return self.j("watch", "--once", *extra)

    def test_state_change_exit_and_idempotency(self):
        self.launch(self.card())
        d = self.watch()
        self.assertEqual(len(d["new_events"]), 1)  # session:exited (worker gone, still in-progress)
        self.assertIn("session:exited", self.kinds("W-1"))
        self.assertEqual(self.watch()["new_events"], [])
        self.finish()
        d = self.watch()
        self.assertEqual(len(d["new_events"]), 1)
        self.assertIn("state:awaiting-verify", self.kinds("W-1"))
        self.assertEqual(self.watch()["new_events"], [])
        n = len(self.j("events"))
        self.watch()
        self.assertEqual(len(self.j("events")), n)

    def test_blocked_state_event(self):
        self.hold()
        self.launch(self.card(), wait=False)
        hp = os.path.join(self.wt(), "HANDOFF.md")
        self.rewrite(hp, lambda t: t.replace("`state: in-progress`", "`state: blocked`").replace(
            "updated: 2026-01-01T00:00:00Z", "updated: " + fleet.now()))
        self.watch()
        self.assertIn("state:blocked", self.kinds("W-1"))
        urgent = [e for e in self.j("events") if e["kind"] == "state:blocked"][0]["urgent"]
        self.assertFalse(urgent)

    def test_stale_once_per_generation(self):
        self.hold()
        self.launch(self.card(timebox="1h"), wait=False)  # HANDOFF updated in 2026-01-01: stale
        self.watch()
        self.watch()
        self.assertEqual(self.kinds("W-1").count("session:stale"), 1)

    def test_drift_once_per_remote_sha(self):
        self.launch(self.card())
        sha, man = self.finish()
        self.watch()
        self.assertNotIn("drift", self.kinds("W-1"))
        wd = self.wt()
        open(os.path.join(wd, "late.txt"), "w").write("late")
        self.git(wd, "add", "-A")
        self.git(wd, "commit", "-m", "late")
        self.git(wd, "push", "origin", "fleet/w-1")
        self.watch()
        self.watch()
        ev = [e for e in self.j("events") if e["kind"] == "drift"]
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0]["urgent"])
        self.assertEqual(ev[0]["detail"]["manifest_sha"], sha)
        open(os.path.join(wd, "later.txt"), "w").write("later")
        self.git(wd, "add", "-A")
        self.git(wd, "commit", "-m", "later")
        self.git(wd, "push", "origin", "fleet/w-1")
        self.watch()
        self.assertEqual(self.kinds("W-1").count("drift"), 2)

    def test_queued_steer_delivered_when_idle(self):
        self.hold()
        self.launch(self.card(), wait=False)
        self.j("steer", "W-1", "queued fix", "--human")
        self.watch()
        self.assertEqual(self.kinds("W-1").count("resumed"), 0)  # still busy
        self.release()
        self.wait_dead("W-1")
        self.watch()
        self.cap("W-1", "worker", 2)
        self.assertIn("queued fix", open(os.path.join(self.out, "W-1_worker_2.prompt")).read())
        self.wait_dead("W-1")
        self.watch()  # may report the resumed session exiting
        self.assertEqual(self.watch()["new_events"], [])

    def test_wake_coordinator(self):
        d, cenv = self.launch(self.card("C-1", repo="-", base=None), "coordinator")
        self.run_cli("request", "dispatch", "--message", "go", "--human")
        self.hold()  # make the resumed coordinator stay busy
        d = self.watch("--wake")
        self.assertEqual(d["woke"], 1)
        self.cap("C-1", "coordinator", 2)
        p = open(os.path.join(self.out, "C-1_coordinator_2.prompt")).read()
        self.assertTrue(p.startswith("[fleet-watch]\n"))
        self.assertEqual(len(p.strip().splitlines()), 2)
        self.assertIn("request:dispatch", p)
        self.run_cli("request", "rework", "--human")
        self.assertIsNone(self.watch("--wake")["woke"])  # coordinator busy: events kept
        self.release()
        self.wait_dead("C-1", "coordinator")
        self.assertEqual(self.watch("--wake")["woke"], 1)
        self.cap("C-1", "coordinator", 3)
        self.wait_dead("C-1", "coordinator")
        self.assertIsNone(self.watch("--wake")["woke"])  # nothing new

    def test_digest_when_supervisor_offline(self):
        sink = os.path.join(self.tmp, "notified.txt")
        self.cfg["notify"] = {"argv": ["sh", "-c", "cat >> " + sink]}
        self.write_cfg()
        self.launch(self.card())
        self.j("decide-open", "W-1", "--tier", "human_required", "--question", "ship?", "--options", "A|B",
               "--default", "A", "--deadline", "2030-01-01T00:00:00Z", "--human")
        self.watch()
        self.assertFalse(os.path.exists(sink))  # no supervisor heartbeat ever: nothing to take over
        import sqlite3
        c = sqlite3.connect(os.path.join(self.home, "fleet.db"))
        c.execute("INSERT OR REPLACE INTO heartbeats VALUES('supervisor','2000-01-01T00:00:00Z')")
        c.commit()
        c.close()
        self.watch()
        self.assertIn("decision:human_required", open(sink).read())
        size = os.path.getsize(sink)
        self.watch()
        self.assertEqual(os.path.getsize(sink), size)  # once per window


class TestSshGate(Base):
    def gate(self, cmd):
        return self.run_cli("ssh-gate", env={"SSH_ORIGINAL_COMMAND": cmd})

    def test_allow(self):
        self.launch(self.card())
        self.assertEqual(self.gate("/opt/x/fleet.py events --json")[0], 0)
        self.assertEqual(self.gate("fleet.py status W-1 --json")[0], 0)
        self.assertEqual(self.gate("fleet.py wait --after 999 --timeout 0")[0], 2)
        self.assertEqual(self.gate("fleet.py heartbeat --role supervisor")[0], 0)
        self.assertEqual(self.gate("fleet.py request other --item W-1 --message 'hello there'")[0], 0)
        rc, o, e = self.gate("fleet.py steer W-1 'fix the thing'")
        self.assertEqual(rc, 0, e)
        steer = [x for x in self.j("events") if x["kind"] == "steer"][0]
        self.assertEqual(steer["detail"]["by"], "supervisor")
        self.assertEqual(self.gate("fleet.py resume W-1 'go on'")[0], 0)

    def test_deny(self):
        self.launch(self.card())
        for cmd in ("rm -rf /", "", "fleet.py land W-1 --sha " + "a" * 40, "fleet.py mark W-1 done",
                    "fleet.py rework W-1 --requirements-file x", "fleet.py escalate W-1", "fleet.py launch c.md",
                    "fleet.py steer W-1 x --human", "fleet.py status --config /tmp/x", "fleet.py heartbeat --role coordinator",
                    "python3 other.py status", "fleet.py events; id", "fleet.py 'unterminated", "fleet.py decide-answer D-1 yes",
                    "fleet.py ssh-gate"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.gate(cmd)[0], 126)
        self.assertEqual(self.j("events") and [x["kind"] for x in self.j("events")].count("steer"), 0)

    def test_gate_does_not_leak_supervisor_authority(self):
        self.launch(self.card())
        self.gate("fleet.py events")
        self.assertEqual(self.run_cli("steer", "W-1", "x")[0], 3)  # override reset after gate

    def test_env_role_cannot_claim_supervisor(self):
        self.launch(self.card())
        rc = self.run_cli("steer", "W-1", "x", env={"FLEET_ROLE": "supervisor", "FLEET_ITEM": "W-1",
                                                     "FLEET_GEN": "1", "FLEET_CAPABILITY": "x"})[0]
        self.assertEqual(rc, 3)


class TestHostJob(Base):
    def setUp(self):
        super().setUp()
        self.launch(self.card("N-1", repo="-", base=None))
        self.hd = os.path.join(self.home, "hostq", "h1")
        os.makedirs(self.hd, exist_ok=True)
        with open(os.path.join(self.hd, "capabilities.json"), "w") as f:
            json.dump({"allowed_argv_prefixes": [[sys.executable, "-c"]]}, f)

    def open(self, argv, **kw):
        a = ["host-job", "open", "N-1", "--host", "h1", "--argv", json.dumps(argv), "--human"]
        for k, v in kw.items():
            a += ["--" + k, v]
        return self.j(*a)["id"]

    def receipt(self, jid):
        with open(os.path.join(self.hd, "receipts", jid + ".json")) as f:
            return json.load(f)

    def test_allowed_job_receipt_hashes(self):
        jid = self.open([sys.executable, "-c", "open('out.bin','w').write('hi'); print('done')"],
                        artifacts=json.dumps(["out.bin"]))
        req = json.load(open(os.path.join(self.hd, "requests", jid + ".json")))
        self.assertEqual(req["item"], "N-1")
        self.assertEqual(self.j("host-job", "pull", "--host", "h1")["ran"], [jid])
        r = self.receipt(jid)
        self.assertEqual(r["exit_code"], 0)
        self.assertIn("done", r["stdout_tail"])
        self.assertEqual(r["artifacts"], {"out.bin": hashlib.sha256(b"hi").hexdigest()})
        self.assertIn("duration_s", r)
        self.assertEqual(self.j("host-job", "pull", "--host", "h1")["ran"], [])  # never re-run
        st = self.j("host-job", "status", "--host", "h1")
        self.assertEqual((st["requests"], st["receipts"]), ([jid + ".json"], [jid + ".json"]))

    def test_disallowed_argv_rejected(self):
        marker = os.path.join(self.tmp, "should-not-exist")
        jid = self.open(["touch", marker])
        self.j("host-job", "pull", "--host", "h1")
        r = self.receipt(jid)
        self.assertTrue(r["rejected"])
        self.assertFalse(os.path.exists(marker))
        self.assertIn("host_job:failed", self.kinds("N-1"))

    def test_failure_event_and_note_no_retry(self):
        jid = self.open([sys.executable, "-c", "import sys; sys.exit(3)"])
        self.j("host-job", "pull", "--host", "h1")
        self.assertEqual(self.receipt(jid)["exit_code"], 3)
        self.assertIn("host_job:failed", self.kinds("N-1"))
        self.assertIn("HOST-JOB %s failed" % jid, open(os.path.join(self.home, "work", "n-1", "HANDOFF.md")).read())

    def test_timeout(self):
        jid = self.open([sys.executable, "-c", "import time; time.sleep(30)"], timeout="1")
        self.j("host-job", "pull", "--host", "h1")
        self.assertEqual(self.receipt(jid)["exit_code"], 124)

    def test_missing_capabilities_fails_closed(self):
        os.remove(os.path.join(self.hd, "capabilities.json"))
        self.open([sys.executable, "-c", "pass"])
        self.assertEqual(self.run_cli("host-job", "pull", "--host", "h1")[0], 1)
        self.assertEqual(os.listdir(os.path.join(self.hd, "receipts")), [])

    def test_open_requires_authority_and_valid_argv(self):
        self.assertEqual(self.run_cli("host-job", "open", "N-1", "--host", "h1", "--argv", '["x"]')[0], 3)
        self.assertEqual(self.run_cli("host-job", "open", "N-1", "--host", "h1", "--argv", "nonsense", "--human")[0], 1)
        self.assertEqual(self.run_cli("host-job", "open", "N-1", "--host", "../x", "--argv", '["x"]', "--human")[0], 1)


class NonCode(Base):
    """Helpers for a finished non-code card."""

    def noncode_ready(self, id="N-1", body=b"x", extra_evidence=None, only=None):
        c = self.card(id, repo="-", base=None)
        self.launch(c)
        wd = os.path.join(self.home, "work", fleet.slugify(id))
        with open(os.path.join(wd, "out.txt"), "wb") as f:
            f.write(body)
        ev = [{"id": "a", "kind": "artifact", "path": "out.txt", "sha256": hashlib.sha256(body).hexdigest()}]
        if only is not None:
            ev = only
        man = {"item": id, "claim": "c", "known_gaps": [], "evidence": ev + (extra_evidence or [])}
        self.set_noncode_handoff(id, man)
        return c, wd

    def set_noncode_handoff(self, id, man):
        hp = os.path.join(self.home, "work", fleet.slugify(id), "HANDOFF.md")
        t = open(hp).read()
        t = re.sub(r"^`state:.*$", "`state: awaiting-verify` · `result: ok`", t, flags=re.M)
        t = re.sub(r"```json\n.*?```", lambda m: "```json\n%s\n```" % json.dumps(man), t, flags=re.S)
        open(hp, "w").write(t)

    def review(self, c, id, verdict="PASS", gen=2):
        v = self.j("verify", id)
        d, renv = self.launch(c, "reviewer", gen=gen)
        self.j("review-record", id, verdict, "--candidate-sha", "-", "--manifest-sha256", v["manifest_sha256"], env=renv)
        return v["manifest_sha256"]

    def db_rows(self, sql, *p):
        import sqlite3
        c = sqlite3.connect(os.path.join(self.home, "fleet.db"))
        try:
            return c.execute(sql, p).fetchall()
        finally:
            c.close()

    def db_exec(self, sql, *p):
        import sqlite3
        c = sqlite3.connect(os.path.join(self.home, "fleet.db"))
        c.execute(sql, p)
        c.commit()
        c.close()


class TestCompleteVerifiedNoncode(NonCode):
    def cv(self, *a, rc=0, **kw):
        return self.j("complete-verified", "N-1", "--kind", "noncode", "--human", *a, rc=rc, **kw)

    def test_reviewer_pass_completes_and_repeat_is_noop(self):
        c, wd = self.noncode_ready()
        self.review(c, "N-1")
        r = self.cv()
        self.assertTrue(r["completed"])
        self.assertEqual(self.j("status", "N-1")["state"], "done")
        n = len(self.events())
        r2 = self.cv()
        self.assertTrue(r2["noop"])
        self.assertEqual(len(self.events()), n)  # no second effect
        self.assertEqual([x[0] for x in self.db_rows("SELECT phase FROM completions")], ["intent", "complete"])

    def test_plain_mark_done_refused_on_noncode_card(self):
        self.noncode_ready()
        rc, o, e = self.run_cli("mark", "N-1", "done", "--human")
        self.assertEqual(rc, 1)
        self.assertIn("complete-verified", e)
        self.assertEqual(self.j("status", "N-1")["state"], "in-progress")

    def test_needs_some_verdict(self):
        self.noncode_ready()
        rc, o, e = self.run_cli("complete-verified", "N-1", "--kind", "noncode", "--human")
        self.assertEqual(rc, 1)
        self.assertIn("verdict", e)

    def test_coordinator_verdict_only_without_reviewer_verdict(self):
        c, wd = self.noncode_ready()
        r = self.cv("--coordinator-verdict", "checked the report by hand")
        self.assertTrue(r["completed"])
        self.assertIn("coordinator", r["basis"])

    def test_coordinator_verdict_refused_when_reviewer_verdict_exists(self):
        c, wd = self.noncode_ready()
        self.review(c, "N-1", "REWORK")
        rc, o, e = self.run_cli("complete-verified", "N-1", "--kind", "noncode", "--human",
                                "--coordinator-verdict", "looks fine to me")
        self.assertEqual(rc, 1)
        self.assertIn("reviewer verdict", e)
        self.assertEqual(self.j("status", "N-1")["state"], "in-review")

    def test_reviewer_rework_blocks_completion(self):
        c, wd = self.noncode_ready()
        self.review(c, "N-1", "REWORK")
        rc, o, e = self.run_cli("complete-verified", "N-1", "--kind", "noncode", "--human")
        self.assertEqual(rc, 1)
        self.assertIn("PASS", e)

    def test_review_of_older_manifest_does_not_count(self):
        c, wd = self.noncode_ready()
        self.review(c, "N-1")
        open(os.path.join(wd, "more.txt"), "w").write("y")
        man = {"item": "N-1", "claim": "c2", "known_gaps": [], "evidence": [
            {"id": "a", "kind": "artifact", "path": "more.txt", "sha256": hashlib.sha256(b"y").hexdigest()}]}
        self.set_noncode_handoff("N-1", man)
        rc, o, e = self.run_cli("complete-verified", "N-1", "--kind", "noncode", "--human")
        self.assertEqual(rc, 1)
        self.assertIn("PASS", e)

    def test_reviewer_in_same_session_as_worker_rejected(self):
        c, wd = self.noncode_ready()
        self.review(c, "N-1")
        sid = self.db_rows("SELECT session_id FROM generations WHERE role='worker'")[0][0]
        self.assertTrue(sid)
        self.db_exec("UPDATE generations SET session_id=? WHERE role='reviewer'", sid)
        rc, o, e = self.run_cli("complete-verified", "N-1", "--kind", "noncode", "--human")
        self.assertEqual(rc, 1)
        self.assertIn("same session", e)

    def test_rehash_after_review_detects_tamper(self):
        c, wd = self.noncode_ready()
        self.review(c, "N-1")
        open(os.path.join(wd, "out.txt"), "w").write("tampered")
        rc, o, e = self.run_cli("complete-verified", "N-1", "--kind", "noncode", "--human")
        self.assertEqual(rc, 1)
        self.assertIn("sha256 mismatch", e)
        self.assertEqual(self.db_rows("SELECT * FROM completions"), [])

    def test_symlink_artifact_refused(self):
        c, wd = self.noncode_ready()
        os.symlink("out.txt", os.path.join(wd, "link.txt"))
        man = {"item": "N-1", "claim": "c", "known_gaps": [], "evidence": [
            {"id": "a", "kind": "artifact", "path": "link.txt", "sha256": hashlib.sha256(b"x").hexdigest()}]}
        self.set_noncode_handoff("N-1", man)
        self.assertTrue(self.j("verify", "N-1")["ok"])  # plain verify follows links inside the dir
        rc, o, e = self.run_cli("complete-verified", "N-1", "--kind", "noncode", "--human",
                                "--coordinator-verdict", "ok")
        self.assertEqual(rc, 1)
        self.assertIn("symlink", e)

    def test_symlinked_directory_component_refused(self):
        c, wd = self.noncode_ready()
        os.makedirs(os.path.join(wd, "real"))
        open(os.path.join(wd, "real", "r.txt"), "w").write("z")
        os.symlink("real", os.path.join(wd, "dirlink"))
        man = {"item": "N-1", "claim": "c", "known_gaps": [], "evidence": [
            {"id": "a", "kind": "report", "path": "dirlink/r.txt", "sha256": hashlib.sha256(b"z").hexdigest()}]}
        self.set_noncode_handoff("N-1", man)
        rc, o, e = self.run_cli("complete-verified", "N-1", "--kind", "noncode", "--human",
                                "--coordinator-verdict", "ok")
        self.assertEqual(rc, 1)
        self.assertIn("symlink", e)

    def test_needs_artifact_or_report(self):
        c, wd = self.noncode_ready(only=[{"id": "d", "kind": "doc", "url": "https://example.invalid/x"}])
        rc, o, e = self.run_cli("complete-verified", "N-1", "--kind", "noncode", "--human",
                                "--coordinator-verdict", "ok")
        self.assertEqual(rc, 1)
        self.assertIn("artifact or report", e)

    def test_needs_coordinator_authority(self):
        c, wd = self.noncode_ready()
        self.review(c, "N-1")
        renv = self.cap("N-1", "reviewer", 2)
        self.assertEqual(self.run_cli("complete-verified", "N-1", "--kind", "noncode", env=renv)[0], 3)
        self.assertEqual(self.run_cli("complete-verified", "N-1", "--kind", "noncode",
                                      env=self.cap("N-1", "worker", 1))[0], 3)
        self.assertEqual(self.run_cli("complete-verified", "N-1", "--kind", "noncode")[0], 3)

    def test_dry_run_persists_nothing(self):
        c, wd = self.noncode_ready()
        self.review(c, "N-1")
        r = self.cv("--dry-run")
        self.assertTrue(r["dry_run"])
        self.assertEqual(self.j("status", "N-1")["state"], "in-review")
        self.assertEqual(self.db_rows("SELECT * FROM completions"), [])

    def test_unknown_outcome_is_terminal_until_reconciled(self):
        c, wd = self.noncode_ready()
        self.review(c, "N-1")
        self.db_exec("INSERT INTO completions VALUES('N-1','noncode','intent','2026-01-01T00:00:00Z','crash')")
        rc, o, e = self.run_cli("complete-verified", "N-1", "--kind", "noncode", "--human")
        self.assertEqual(rc, 5)
        self.assertIn("reconcile", e)
        rc, o, e = self.run_cli("complete-verified", "N-1", "--kind", "noncode", "--human")
        self.assertEqual(rc, 5)  # still terminal: no retry
        self.assertEqual(self.j("complete-verified", "N-1", "--kind", "noncode", "--human", "--reconcile")["reconciled"],
                         "not_completed")
        self.assertTrue(self.cv()["completed"])  # now retry is allowed

    def test_reconcile_recognises_a_finished_completion(self):
        c, wd = self.noncode_ready()
        self.review(c, "N-1")
        self.db_exec("INSERT INTO completions VALUES('N-1','noncode','intent','2026-01-01T00:00:00Z','crash')")
        self.db_exec("UPDATE items SET state='done' WHERE id='N-1'")
        self.assertEqual(self.j("complete-verified", "N-1", "--kind", "noncode", "--human", "--reconcile")["reconciled"],
                         "complete")
        self.assertTrue(self.cv()["noop"])

    def test_wrong_kind_for_card(self):
        c, wd = self.noncode_ready()
        rc, o, e = self.run_cli("complete-verified", "N-1", "--kind", "already-integrated", "--human",
                                "--candidate-sha", "a" * 40)
        self.assertEqual(rc, 1)
        self.assertIn("code card", e)


class TestCompleteVerifiedIntegrated(Base):
    def integrate(self, sha, cherry=False):
        with self.env({"GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"} if cherry else None):
            sh(self.other, "fetch", "origin")
            sh(self.other, "checkout", "-B", "main", "origin/main")
            if cherry:
                sh(self.other, "cherry-pick", sha)
            else:
                sh(self.other, "merge", "--ff-only", sha)
            sh(self.other, "push", "origin", "main")

    def cv(self, sha, *a, rc=0):
        return self.j("complete-verified", "W-1", "--kind", "already-integrated", "--candidate-sha", sha,
                      "--human", *a, rc=rc)

    def test_ancestor_with_reviewer_pass_completes(self):
        c, sha, mh = self.reviewed()
        self.integrate(sha)
        r = self.cv(sha)
        self.assertTrue(r["completed"])
        self.assertEqual(self.j("status", "W-1")["state"], "done")
        self.assertTrue(self.cv(sha)["noop"])

    def test_patch_equivalent_commit_completes(self):
        c, sha, mh = self.reviewed()
        self.integrate(sha, cherry=True)
        self.assertNotEqual(self.git(self.origin, "rev-parse", "main"), sha)
        self.assertTrue(self.cv(sha)["completed"])

    def test_candidate_adding_changes_refused(self):
        c, sha, mh = self.reviewed()
        rc, o, e = self.run_cli("complete-verified", "W-1", "--kind", "already-integrated", "--candidate-sha", sha,
                                "--human")
        self.assertEqual(rc, 1)
        self.assertIn("adds changes", e)
        self.assertEqual(self.j("status", "W-1")["state"], "in-review")

    def test_needs_reviewer_pass_bound_to_candidate_and_manifest(self):
        c, sha, wenv = self.ready()
        self.integrate(sha)
        rc, o, e = self.run_cli("complete-verified", "W-1", "--kind", "already-integrated", "--candidate-sha", sha,
                                "--human")
        self.assertEqual(rc, 1)
        self.assertIn("PASS", e)

    def test_review_rework_refused(self):
        c, sha, mh = self.reviewed(verdict="REWORK")
        self.integrate(sha)
        rc, o, e = self.run_cli("complete-verified", "W-1", "--kind", "already-integrated", "--candidate-sha", sha,
                                "--human")
        self.assertEqual(rc, 1)
        self.assertIn("PASS", e)

    def test_candidate_must_match_manifest_commit(self):
        c, sha, mh = self.reviewed()
        self.integrate(sha)
        rc, o, e = self.run_cli("complete-verified", "W-1", "--kind", "already-integrated",
                                "--candidate-sha", "c" * 40, "--human")
        self.assertEqual(rc, 1)
        self.assertIn("manifest", e)

    def test_noncode_kind_refused_on_code_card(self):
        c, sha, mh = self.reviewed()
        rc, o, e = self.run_cli("complete-verified", "W-1", "--kind", "noncode", "--human", "--coordinator-verdict", "x")
        self.assertEqual(rc, 1)
        self.assertIn("non-code", e)

    def test_land_refuses_an_already_integrated_candidate(self):
        c, sha, mh = self.reviewed()
        self.integrate(sha)
        rc, o, e = self.run_cli("land", "W-1", "--sha", sha, "--human")
        self.assertEqual(rc, 1)
        self.assertIn("complete-verified", e)

    def test_dry_run(self):
        c, sha, mh = self.reviewed()
        self.integrate(sha)
        self.assertTrue(self.cv(sha, "--dry-run")["dry_run"])
        self.assertEqual(self.j("status", "W-1")["state"], "in-review")


class TestBranchRebind(Base):
    def pf(self, env, action="commit"):
        rc, o, e = self.run_cli("preflight", "--action", action, "--target", self.wt(), "--json", env=env)
        return rc, json.loads(o)

    def setUp(self):
        super().setUp()
        _, self.env1 = self.launch(self.card())

    def test_worker_may_force_push_its_own_branch_after_squash(self):
        wd = self.wt()
        for i in range(2):
            open(os.path.join(wd, "f%d" % i), "w").write("x")
            self.git(wd, "add", "-A")
            self.git(wd, "commit", "-m", "c%d" % i)
        self.git(wd, "push", "origin", "fleet/w-1")
        self.git(wd, "reset", "--soft", "origin/main")
        self.git(wd, "commit", "-m", "squashed")
        self.git(wd, "push", "--force-with-lease", "origin", "fleet/w-1")
        self.assertEqual(self.git(self.origin, "rev-parse", "fleet/w-1"), self.git(wd, "rev-parse", "HEAD"))
        self.assertEqual(self.pf(self.env1)[0], 0)

    def test_rework_rename_is_rebound_with_audit_event(self):
        self.git(self.wt(), "branch", "-m", "fleet/w-1-r2")
        rc, d = self.pf(self.env1)
        self.assertEqual((rc, d["allowed"]), (0, True), d)
        st = self.j("status", "W-1")
        self.assertEqual(st["branch"], "fleet/w-1-r2")
        ev = [e for e in self.j("events") if e["kind"] == "branch:rebound"]
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["detail"]["from"], ev[0]["detail"]["to"]), ("fleet/w-1", "fleet/w-1-r2"))
        rc, d = self.pf(self.env1, "build")  # idempotent: no second event
        self.assertEqual(rc, 0)
        self.assertEqual(len([e for e in self.j("events") if e["kind"] == "branch:rebound"]), 1)

    def test_relaunch_after_rebind_reuses_the_renamed_worktree(self):
        c = self.card()
        self.wait_dead("W-1")
        self.git(self.wt(), "branch", "-m", "fleet/w-1-r2")
        self.assertEqual(self.pf(self.env1)[0], 0)
        d = self.j("launch", c, "--human")
        self.assertEqual(d["workdir"], self.wt())
        self.assertEqual(d["branch"], "fleet/w-1-r2")

    def test_second_rework_rename_also_rebinds(self):
        self.git(self.wt(), "branch", "-m", "fleet/w-1-r2")
        self.assertEqual(self.pf(self.env1)[0], 0)
        self.git(self.wt(), "branch", "-m", "fleet/w-1-r3")
        self.assertEqual(self.pf(self.env1)[0], 0)
        self.assertEqual(self.j("status", "W-1")["branch"], "fleet/w-1-r3")

    def test_other_branches_stay_denied(self):
        for name in ("fleet/w-1-rx", "fleet/other-r2", "main", "fleet/w-1-r2/x", "fleet/w-1-r"):
            with self.subTest(branch=name):
                self.git(self.wt(), "checkout", "-B", name)
                rc, d = self.pf(self.env1)
                self.assertEqual(rc, 3, d)
                self.assertEqual(self.j("status", "W-1")["branch"], "fleet/w-1")
        self.assertEqual([e for e in self.j("events") if e["kind"] == "branch:rebound"], [])

    def test_edit_never_rebinds(self):
        self.git(self.wt(), "branch", "-m", "fleet/w-1-r2")
        self.assertEqual(self.pf(self.env1, "edit")[0], 0)
        self.assertEqual(self.j("status", "W-1")["branch"], "fleet/w-1")


class TestRelaunchRelay(Base):
    def log_of(self, item="W-1"):
        return self.j("status", item)["generations"]["worker"]["log_path"]

    def write_log(self, lines):
        with open(self.log_of(), "a") as f:
            for ln in lines:
                f.write((ln if isinstance(ln, str) else json.dumps(ln)) + "\n")

    def prompt(self, gen):
        with open(os.path.join(self.out, "W-1_worker_%d.prompt" % gen)) as f:
            return f.read()

    def test_relaunch_appends_redacted_bounded_tail_and_diffstat(self):
        c = self.card()
        self.launch(c)
        wd = self.wt()
        with open(os.path.join(wd, "new.txt"), "w") as f:
            f.write("hello\n")
        self.git(wd, "add", "-A")
        self.git(wd, "commit", "-m", "wip")
        secret = "sk-" + "A1b2C3d4" * 6
        self.write_log([
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "plan: edit parser. Authorization: Bearer " + secret},
                {"type": "tool_use", "name": "Bash", "input": {"command": "curl 'https://x.invalid/?access_token=abc123def456ghi'"}}]}},  # skills-repo: allow-test-secret
            {"type": "item.completed", "item": {"type": "agent_message", "text": "password=hunter2hunter2 done"}},  # skills-repo: allow-test-secret
            "plain stdout line with API_KEY=zzzzzzzzzzzz",
        ])
        self.j("launch", c, "--human")
        p = self.prompt(2)
        self.assertIn("HANDOFF", p)
        self.assertIn("plan: edit parser", p)
        self.assertIn("Bash", p)
        self.assertIn("new.txt", p)  # diff stat vs base
        for leaked in (secret, "abc123def456ghi", "hunter2hunter2", "zzzzzzzzzzzz"):
            self.assertNotIn(leaked, p)
        self.assertIn("[REDACTED]", p)

    def test_relay_is_bounded_and_keeps_the_tail(self):
        c = self.card()
        self.launch(c)
        self.write_log([{"type": "item.completed", "item": {"type": "agent_message", "text": "line-%05d " % i + "x" * 200}}
                        for i in range(400)])
        self.j("launch", c, "--human")
        p = self.prompt(2)
        start = p.index("Context relay")
        self.assertLessEqual(len(p) - start, 12000)
        self.assertIn("line-00399", p)
        self.assertNotIn("line-00000", p)

    def test_missing_log_gives_empty_relay(self):
        c = self.card()
        self.launch(c)
        os.remove(self.log_of())
        self.j("launch", c, "--human")
        p = self.prompt(2)
        self.assertNotIn("Context relay", p)
        self.assertIn("HANDOFF", p)

    def test_escalate_relays_too(self):
        c = self.card()
        self.launch(c)
        self.write_log(["tail marker QQQ"])
        d = self.j("escalate", "W-1", "--human")
        self.assertIn("tail marker QQQ", self.prompt(d["gen"]))
        self.assertIn("continue from the evolved HANDOFF", self.prompt(d["gen"]))

    def test_first_launch_has_no_relay(self):
        self.launch(self.card())
        self.assertNotIn("Context relay", self.prompt(1))


class TestApprovedTiers(Base):
    TIERS_CFG = {"A": {"model": "m-a", "effort": "medium", "reviewer": "A-review"},
                 "A-review": {"model": "m-a", "effort": "high"},
                 "C": {"model": "m-c", "effort": "xhigh", "reviewer": "C-review"},
                 "C-review": {"model": "m-c", "effort": "max"}}

    def use(self, tiers=None):
        self.cfg["model_tiers"] = self.TIERS_CFG if tiers is None else tiers
        self.write_cfg()

    def fails(self, *argv):
        rc, o, e = self.run_cli(*argv)
        self.assertEqual(rc, 1, o)
        self.assertNotIn("Traceback", e)
        return e

    def test_card_tier_selects_the_tuple(self):
        self.use()
        d = self.j("route", self.card(tier="A", scope="large", ambiguity="high", risk="high"))
        self.assertEqual((d["worker"]["model"], d["worker"]["effort"], d["tier"]), ("m-a", "medium", "A"))
        self.assertEqual((d["reviewer"]["model"], d["reviewer"]["effort"]), ("m-a", "high"))

    def test_launch_uses_the_approved_tuple(self):
        self.use()
        d, env = self.launch(self.card(tier="C"))
        st = self.j("status", "W-1")["generations"]["worker"]
        self.assertEqual((st["model"], st["effort"], st["tier"]), ("m-c", "xhigh", "C"))

    def test_explicit_reviewer_tier_wins(self):
        self.use()
        d = self.j("route", self.card(tier="A", reviewer_tier="C-review"))
        self.assertEqual((d["reviewer"]["model"], d["reviewer"]["effort"]), ("m-c", "max"))

    def test_unknown_tier_fails_closed(self):
        self.use()
        self.assertIn("tier", self.fails("route", self.card(tier="Z")))
        self.assertIn("tier", self.fails("launch", self.card("W-2", tier="Z"), "--human"))

    def test_tier_without_configured_set_fails_closed(self):
        self.assertIn("model_tiers", self.fails("route", self.card(tier="A")))
        self.use({})
        self.assertIn("model_tiers", self.fails("route", self.card(tier="A")))

    def test_empty_card_tier_is_a_lint_error(self):
        self.use()
        rc, o, e = self.run_cli("card-lint", self.card(tier=""))
        self.assertEqual(rc, 1)
        self.assertIn("tier is empty", o)

    def test_tier_and_effort_override_conflict(self):
        self.use()
        self.assertIn("tier", self.fails("route", self.card(tier="A", effort="high", effort_reason="x")))

    def test_partial_entry_fails_closed(self):
        self.use({"A": {"model": "m-a"}})
        self.assertIn("effort", self.fails("route", self.card(tier="A")))
        self.use({"A": {"effort": "low"}})
        self.assertIn("model", self.fails("route", self.card(tier="A")))
        self.use({"A": {"model": "", "effort": "low"}})
        self.assertIn("model", self.fails("route", self.card(tier="A")))

    def test_duplicate_tuple_fails_closed(self):
        self.use({"A": {"model": "m", "effort": "high"}, "B": {"model": "m", "effort": "high"}})
        self.assertIn("duplicate", self.fails("route", self.card(tier="A")))

    def test_duplicate_json_key_fails_closed(self):
        self.use()
        p = os.path.join(self.home, "fleet.json")
        with open(p) as f:
            txt = f.read()
        with open(p, "w") as f:
            f.write(txt.replace('"model_tiers": {', '"model_tiers": {"A": {"model": "x", "effort": "low"}, ', 1))
        self.assertIn("duplicate", self.fails("route", self.card(tier="A")))

    def test_dangling_reviewer_link_fails_closed(self):
        self.use({"A": {"model": "m-a", "effort": "low", "reviewer": "nope"}})
        self.assertIn("reviewer", self.fails("route", self.card(tier="A")))

    def test_relaunch_keeps_the_registry_tuple_exactly(self):
        self.use()
        c = self.card(tier="A")
        self.launch(c)
        self.cfg["model_tiers"]["A"] = {"model": "m-new", "effort": "low", "reviewer": "A-review"}
        self.write_cfg()
        d = self.j("launch", c, "--human")
        self.assertEqual((d["model"], d["effort"], d["tier"]), ("m-a", "medium", "A"))

    def test_explicit_tier_on_relaunch_changes_the_tuple(self):
        self.use()
        c = self.card(tier="A")
        self.launch(c)
        d = self.j("launch", c, "--human", "--tier", "C")
        self.assertEqual((d["model"], d["effort"], d["tier"]), ("m-c", "xhigh", "C"))
        rc, o, e = self.run_cli("launch", c, "--human", "--tier", "Z")
        self.assertEqual(rc, 1)

    def test_relaunch_of_complexity_routed_card_keeps_tuple_too(self):
        c = self.card()
        self.launch(c)
        self.cfg["runtimes"]["fake"]["tiers"]["low"] = {"effort": "high", "model": "m-changed"}
        self.write_cfg()
        d = self.j("launch", c, "--human")
        self.assertEqual((d["model"], d["effort"]), ("m-fast", "low"))

    def test_escalate_refuses_approved_tier_generation(self):
        self.use()
        self.launch(self.card(tier="A"))
        self.assertIn("approved", self.fails("escalate", "W-1", "--human"))

    def test_coordinator_change_needs_idle_and_fresh_generation_from_handoff(self):
        self.hold()
        cc = self.card("C-1", repo="-", base=None)
        self.cfg["coordinator"] = {"effort": "high"}
        self.write_cfg()
        self.launch(cc, "coordinator", wait=False)
        self.cfg["coordinator"] = {"effort": "xhigh"}
        self.write_cfg()
        self.assertIn("idle", self.fails("launch", cc, "--role", "coordinator", "--human"))
        self.release()
        self.wait_dead("C-1", "coordinator")
        d = self.j("launch", cc, "--role", "coordinator", "--human")
        self.assertEqual((d["gen"], d["effort"]), (2, "xhigh"))
        self.assertTrue([e for e in self.j("events") if e["kind"] == "coordinator:tuple_changed"])
        # a changed tuple needs the HANDOFF the fresh generation resumes from
        self.wait_dead("C-1", "coordinator")
        os.remove(os.path.join(self.home, "work", "c-1", "HANDOFF.md"))
        self.cfg["coordinator"] = {"effort": "medium"}
        self.write_cfg()
        self.assertIn("HANDOFF", self.fails("launch", cc, "--role", "coordinator", "--human"))


class TestDocContracts(unittest.TestCase):
    """The card template carries the worker rules that the prose docs refer to."""

    def test_template_carries_worker_rules(self):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(root, "assets", "dispatch-card.template.md")) as f:
            tpl = f.read()
        for phrase in ("Not blockers", "wait 1-2 minutes", "at most 5 times", "known_gaps", "--force-with-lease",
                       "45 minutes", "next_action:", "Pre-push self-check", "review rounds of your own"):
            self.assertIn(phrase, tpl)
        prohibited = tpl[tpl.index("**Prohibited:**"):tpl.index("**Escalation:**")]
        self.assertIn("force-push of anything but this", prohibited)


if __name__ == "__main__":
    unittest.main(verbosity=1)
