#!/usr/bin/env python3
"""fleet.py - reference CLI for the fleet-dispatch skill (stdlib only).

State lives in $FLEET_HOME (SQLite + git worktrees). Authority comes from a
per-generation capability token (only its sha256 is stored) or --human.
Exit codes: 0 ok, 1 failure, 2 timeout/usage, 3 denied, 4 rate-limited,
5 result unknown, 126 ssh-gate rejection.
"""
import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import sys
import time
import types
import uuid

TIERS = ["low", "medium", "high", "xhigh", "max"]
SIGNALS = {"scope": {"small": 0, "medium": 1, "large": 2},
           "ambiguity": {"low": 0, "medium": 1, "high": 2},
           "risk": {"low": 0, "medium": 1, "high": 2},
           "verify_cost": {"cheap": 0, "costly": 1}}
SIGNAL_DEFAULT = {"scope": "medium", "ambiguity": "medium", "risk": "medium", "verify_cost": "costly"}
WORKER_STATES = ("in-progress", "blocked", "awaiting-verify")
COORD_STATES = ("landed", "done", "cancelled")
TERMINAL = ("done", "cancelled")
LIVE_STATES = ("in-progress", "blocked", "awaiting-verify", "in-review")
KINDS = ("commit", "pr", "ci", "test", "artifact", "report", "doc", "host_job")
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA2 = re.compile(r"^[0-9a-f]{64}$")
ENV_NAMES = ["FLEET_HOME", "FLEET_ITEM", "FLEET_ROLE", "FLEET_GEN", "FLEET_CAPABILITY"]
SCRIPT = os.path.abspath(__file__)
DEFAULTS = {
    "fleet_home": "~/.agent-fleet",
    "coordinator": {"effort": "high"},
    "routing": {"thresholds": {"medium": 2, "high": 4, "xhigh": 7},
                "reviewer_min": "medium", "reviewer_min_when_risky": "high"},
    "limits": {"max_active": 4, "max_active_per_repo": 2, "steers_per_window": 2,
               "steer_window_minutes": 30, "stale_fraction_of_timebox": 0.5,
               "supervisor_offline_after_minutes": 120, "push_timeout_s": 120},
    "notify": {}, "runtimes": {}, "model_tiers": {},
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, card_path TEXT, card_sha256 TEXT, repo TEXT,
  base TEXT, branch TEXT, state TEXT, runtime TEXT, created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS generations(item TEXT, gen INTEGER, role TEXT, runtime TEXT, model TEXT,
  effort TEXT, tier TEXT, host TEXT, branch TEXT, worktree TEXT, session_id TEXT,
  capability_sha256 TEXT, pid INTEGER, log_path TEXT, started TEXT, fenced INTEGER DEFAULT 0,
  PRIMARY KEY(item, gen));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, item TEXT,
  kind TEXT, urgent INTEGER, detail_json TEXT);
CREATE TABLE IF NOT EXISTS steers(item TEXT, ts TEXT, message TEXT, delivered INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reviews(item TEXT, verdict TEXT, candidate_sha TEXT, manifest_sha256 TEXT,
  reviewer_gen INTEGER, ts TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS lands(item TEXT, sha TEXT, phase TEXT, ts TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY, item TEXT, tier TEXT, question TEXT,
  options TEXT, "default" TEXT, deadline TEXT, state TEXT, answer TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS heartbeats(role TEXT PRIMARY KEY, ts TEXT);
CREATE TABLE IF NOT EXISTS host_jobs(id TEXT PRIMARY KEY, item TEXT, host TEXT, state TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS wakes(key TEXT PRIMARY KEY, ts TEXT);
CREATE TABLE IF NOT EXISTS completions(item TEXT, kind TEXT, phase TEXT, ts TEXT, detail TEXT);
"""

S = types.SimpleNamespace(home=None, cfg=None, db=None, config_path=None)
ACTOR_OVERRIDE = None  # set by ssh-gate: the caller is the supervisor


class Fail(Exception):
    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- basics

def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(minutes):
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    try:
        t = dt.datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def deep_merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def out(args, data, text=None):
    if text is not None and not getattr(args, "json", False):
        print(text)
    else:
        print(json.dumps(data, sort_keys=True) if getattr(args, "json", False)
              else json.dumps(data, indent=2, sort_keys=True))


def no_duplicate_keys(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("duplicate key %r" % k)
        d[k] = v
    return d


def setup(args):
    S.db = None
    env_home = os.environ.get("FLEET_HOME")
    path = getattr(args, "config", None) or os.environ.get("FLEET_CONFIG") or \
        os.path.join(os.path.expanduser(env_home or DEFAULTS["fleet_home"]), "fleet.json")
    cfg = DEFAULTS
    if os.path.exists(path):
        try:
            with open(path) as f:
                cfg = deep_merge(DEFAULTS, json.load(f, object_pairs_hook=no_duplicate_keys))
        except (OSError, ValueError) as e:
            raise Fail("cannot read config %s: %s" % (path, e))
    S.cfg, S.config_path = cfg, path
    S.home = os.path.abspath(os.path.expanduser(env_home or cfg["fleet_home"]))


def db():
    if S.db is None:
        p = os.path.join(S.home, "fleet.db")
        if not os.path.exists(p):
            raise Fail("fleet not initialised at %s (run: fleet.py init)" % S.home)
        S.db = sqlite3.connect(p, timeout=30, isolation_level=None)
        S.db.row_factory = sqlite3.Row
        S.db.execute("CREATE TABLE IF NOT EXISTS completions(item TEXT, kind TEXT, phase TEXT, ts TEXT, detail TEXT)")
    return S.db


def q(sql, *p):
    return db().execute(sql, p).fetchall()


def q1(sql, *p):
    r = db().execute(sql, p).fetchone()
    return r


def x(sql, *p):
    return db().execute(sql, p)


def emit(item, kind, urgent=False, **detail):
    return x("INSERT INTO events(ts,item,kind,urgent,detail_json) VALUES(?,?,?,?,?)",
             now(), item, kind, 1 if urgent else 0, json.dumps(detail, sort_keys=True)).lastrowid


def once(key):
    """True the first time a wake key is claimed."""
    return x("INSERT OR IGNORE INTO wakes(key,ts) VALUES(?,?)", key, now()).rowcount == 1


def run_git(cwd, *a, timeout=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", "-C", cwd] + list(a), capture_output=True, text=True,
                          timeout=timeout, env=env)


def git(cwd, *a, timeout=None):
    r = run_git(cwd, *a, timeout=timeout)
    if r.returncode != 0:
        raise Fail("git %s failed: %s" % (" ".join(a[:2]), (r.stderr or r.stdout).strip()))
    return r.stdout.strip()


def git_push(repo, sha, base, timeout):
    return run_git(repo, "push", "origin", "%s:refs/heads/%s" % (sha, base), timeout=timeout)


def remote_sha(repo, branch):
    r = run_git(repo, "ls-remote", "origin", "refs/heads/" + branch, timeout=60)
    if r.returncode != 0:
        raise Fail("ls-remote failed: %s" % r.stderr.strip())
    return r.stdout.split()[0] if r.stdout.strip() else None


def alive(pid):
    if not pid:
        return False
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open("/proc/%d/stat" % pid) as f:
            if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                return False
    except (OSError, IndexError):
        pass
    return True


# ---------------------------------------------------------------- cards

def section_span(text, name):
    m = re.search(r"^## " + re.escape(name) + r"\b[^\n]*\n?", text, re.M)
    if not m:
        return None
    n = re.search(r"^## ", text[m.end():], re.M)
    return m.start(), m.end(), (m.end() + n.start() if n else len(text))


def parse_card(text):
    lines = text.splitlines()
    fm = {}
    if not lines or lines[0].strip() != "---":
        return None
    for ln in lines[1:]:
        if ln.strip() == "---":
            return fm
        if ":" in ln and not ln.lstrip().startswith("#"):
            k, _, v = ln.partition(":")
            fm[k.strip()] = v.strip()
    return None


def contract_hash(text):
    sp = section_span(text, "State")
    return sha256_bytes((text[:sp[0]] if sp else text).encode())


def lint_card(text):
    fm = parse_card(text)
    if fm is None:
        return None, ["front matter missing or not terminated by '---'"]
    e = []
    for k in ("id", "title", "repo", "runtime", "timebox", "scope", "ambiguity", "risk", "verify_cost"):
        if not fm.get(k):
            e.append("missing required key: %s" % k)
    for k, v in fm.items():
        if "{{" in v:
            e.append("unfilled placeholder in %s: %s" % (k, v))
    if fm.get("id") and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", fm["id"]):
        e.append("id must match [A-Za-z0-9][A-Za-z0-9._-]*")
    for k, allowed in SIGNALS.items():
        if fm.get(k) and "{{" not in fm[k] and fm[k] not in allowed:
            e.append("%s must be one of %s (got %r)" % (k, "|".join(allowed), fm[k]))
    if fm.get("timebox") and "{{" not in fm["timebox"] and not re.match(r"^\d+(\.\d+)?[mhd]$", fm["timebox"]):
        e.append("timebox must look like 90m, 2h or 1d")
    if fm.get("host_jobs") and fm["host_jobs"] not in ("none", "after-land", "explicit"):
        e.append("host_jobs must be none|after-land|explicit")
    if fm.get("effort"):
        if fm["effort"] not in TIERS:
            e.append("effort must be one of %s" % "|".join(TIERS))
        if not fm.get("effort_reason"):
            e.append("effort override requires effort_reason")
    if "tier" in fm and not fm["tier"]:
        e.append("tier is empty (omit the key or name an approved tier)")
    if fm.get("tier") and fm.get("effort"):
        e.append("tier and effort are mutually exclusive")
    if fm.get("reviewer_tier") and not fm.get("tier"):
        e.append("reviewer_tier needs tier")
    if fm.get("repo") and fm["repo"] != "-" and "{{" not in fm["repo"]:
        if not fm.get("base"):
            e.append("code card (repo != '-') requires base")
        b = fm.get("branch") or "fleet/" + slugify(fm.get("id", ""))
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", b) or ".." in b:
            e.append("invalid branch name: %r" % b)
    for sec in ("Contract", "State", "Progress", "Evidence"):
        if not section_span(text, sec):
            e.append("missing section: ## %s" % sec)
    return fm, e


def card_info(fm):
    code = fm["repo"] != "-"
    return {"id": fm["id"], "slug": slugify(fm["id"]), "code": code,
            "repo": os.path.realpath(os.path.expanduser(fm["repo"])) if code else "-",
            "base": fm.get("base", ""), "branch": (fm.get("branch") or "fleet/" + slugify(fm["id"])) if code else ""}


def handoff_state(text):
    sp = section_span(text, "State")
    d = {}
    if sp:
        for piece in re.split(r"[·\n]", text[sp[1]:sp[2]].replace("`", "")):
            k, s, v = piece.partition(":")
            if s and k.strip().lower() in ("state", "updated", "blocker", "doing", "next", "eta", "result"):
                d[k.strip().lower()] = v.strip()
    return d


def read_manifest(text):
    sp = section_span(text, "Evidence")
    m = re.search(r"```json\s*\n(.*?)```", text[sp[1]:sp[2]] if sp else "", re.S)
    if not m:
        raise ValueError("no ```json block under ## Evidence")
    return json.loads(m.group(1))


def append_progress(path, line):
    with open(path) as f:
        t = f.read()
    sp = section_span(t, "Progress")
    at = sp[2] if sp else len(t)
    t = t[:at].rstrip("\n") + "\n" + line.rstrip("\n") + "\n\n" + t[at:]
    with open(path, "w") as f:
        f.write(t)


def timebox_minutes(s):
    m = re.match(r"^(\d+(?:\.\d+)?)([mhd])$", s or "")
    return float(m.group(1)) * {"m": 1, "h": 60, "d": 1440}[m.group(2)] if m else None


# ---------------------------------------------------------------- routing

def tier_at_least(a, b):
    return a if TIERS.index(a) >= TIERS.index(b) else b


def resolve_tier(rtname, tier):
    rt = S.cfg["runtimes"].get(rtname)
    if not rt:
        raise Fail("unknown runtime %r (not in config runtimes)" % rtname)
    tiers = rt.get("tiers") or {}
    eff, clamped, direction = tier, False, None
    if tier not in tiers:
        higher = [t for t in TIERS[TIERS.index(tier):] if t in tiers]
        lower = [t for t in reversed(TIERS[:TIERS.index(tier)]) if t in tiers]
        if higher:
            eff, direction = higher[0], "up"
        elif lower:
            eff, direction = lower[0], "down"
        else:
            raise Fail("runtime %r defines no tiers" % rtname)
        clamped = True
    t = tiers[eff]
    return {"tier": tier, "effective_tier": eff, "clamped": clamped, "clamp_direction": direction,
            "model": t.get("model") or rt.get("default_model", ""), "effort": t.get("effort", eff)}


def approved_tiers():
    """Validated config `model_tiers`: name -> {model, effort, reviewer?}. Anything off fails closed."""
    mt = S.cfg.get("model_tiers")
    if not isinstance(mt, dict) or not mt:
        raise Fail("a tier was requested but the config has no model_tiers (fail closed)")
    seen = {}
    for name, t in mt.items():
        if not isinstance(name, str) or not name.strip() or name in TIERS:
            raise Fail("model_tiers: invalid tier name %r (empty or a canonical effort tier)" % (name,))
        if not isinstance(t, dict):
            raise Fail("model_tiers[%s]: must be an object" % name)
        for k in ("model", "effort"):
            if not isinstance(t.get(k), str) or not t[k].strip():
                raise Fail("model_tiers[%s]: missing or empty %s" % (name, k))
        extra = sorted(set(t) - {"model", "effort", "reviewer"})
        if extra:
            raise Fail("model_tiers[%s]: unknown keys %s" % (name, extra))
        key = (t["model"], t["effort"])
        if key in seen:
            raise Fail("model_tiers: duplicate (model, effort) tuple in %s and %s" % (seen[key], name))
        seen[key] = name
    for name, t in mt.items():
        if t.get("reviewer") is not None and t["reviewer"] not in mt:
            raise Fail("model_tiers[%s]: reviewer %r is not an approved tier" % (name, t["reviewer"]))
    return mt


def resolve_model_tier(name):
    mt = approved_tiers()
    if name not in mt:
        raise Fail("unknown tier %r (approved: %s)" % (name, ", ".join(sorted(mt))))
    return {"tier": name, "effective_tier": name, "clamped": False, "clamp_direction": None,
            "model": mt[name]["model"], "effort": mt[name]["effort"]}


def resolve_any_tier(rtname, tier):
    """An explicit tier: an approved (model, effort) name when configured, else a canonical effort tier."""
    if S.cfg.get("model_tiers") and tier not in TIERS:
        return resolve_model_tier(tier)
    if tier not in TIERS:
        raise Fail("unknown tier %r" % tier)
    return resolve_tier(rtname, tier)


def route_card(fm):
    cfg = S.cfg["routing"]
    sig = {k: fm.get(k) or SIGNAL_DEFAULT[k] for k in SIGNALS}
    score = SIGNALS["scope"][sig["scope"]] + 2 * SIGNALS["ambiguity"][sig["ambiguity"]] + \
        SIGNALS["risk"][sig["risk"]] + SIGNALS["verify_cost"][sig["verify_cost"]]
    th = cfg["thresholds"]
    tier = "low"
    for t in ("medium", "high", "xhigh"):
        if score >= th[t]:
            tier = t
    reasons = ["score %d (scope+2*ambiguity+risk+verify_cost) -> %s" % (score, tier)]
    for k in ("risk", "ambiguity"):
        if sig[k] == "high" and TIERS.index(tier) < TIERS.index("high"):
            tier = "high"
            reasons.append("floor: %s=high -> at least high" % k)
    override = None
    final = tier
    if fm.get("effort"):
        override = {"tier": fm["effort"], "reason": fm.get("effort_reason", "")}
        final = fm["effort"]
        reasons.append("explicit override %s (computed %s): %s" % (final, tier, override["reason"]))
    rev = tier_at_least(final, cfg["reviewer_min"])
    if sig["risk"] != "low":
        rev = tier_at_least(rev, cfg["reviewer_min_when_risky"])
        reasons.append("reviewer: risk>=medium -> at least %s" % cfg["reviewer_min_when_risky"])
    rt = fm["runtime"]
    if fm.get("tier"):
        mt = approved_tiers()
        worker = resolve_model_tier(fm["tier"])
        rname = fm.get("reviewer_tier") or mt[fm["tier"]].get("reviewer")
        if not rname:
            raise Fail("tier %s has no reviewer tier: set reviewer_tier on the card or reviewer in model_tiers"
                       % fm["tier"])
        reasons.append("approved tier %s -> %s/%s; reviewer tier %s" % (
            fm["tier"], worker["model"], worker["effort"], rname))
        return {"item": fm["id"], "signals": sig, "score": score, "computed_tier": tier, "override": None,
                "tier": fm["tier"], "reasons": reasons, "worker": worker, "reviewer": resolve_model_tier(rname)}
    return {"item": fm["id"], "signals": sig, "score": score, "computed_tier": tier, "override": override,
            "tier": final, "reasons": reasons, "worker": resolve_tier(rt, final),
            "reviewer": resolve_tier(rt, rev)}


def load_card_file(path):
    try:
        with open(path) as f:
            text = f.read()
    except OSError as e:
        raise Fail("cannot read card: %s" % e)
    fm, errs = lint_card(text)
    return text, fm, errs


def cmd_card_lint(args):
    text, fm, errs = load_card_file(args.card)
    out(args, {"ok": not errs, "errors": errs, "id": (fm or {}).get("id")},
        "ok" if not errs else "\n".join("lint: " + e for e in errs))
    return 0 if not errs else 1


def cmd_route(args):
    text, fm, errs = load_card_file(args.card)
    if errs:
        raise Fail("card failed lint:\n  " + "\n  ".join(errs))
    r = route_card(fm)
    out(args, r, "%s: tier=%s (computed %s) score=%d\n  worker: %s/%s\n  reviewer: %s/%s\n  %s" % (
        r["item"], r["tier"], r["computed_tier"], r["score"], r["worker"]["model"], r["worker"]["effort"],
        r["reviewer"]["model"], r["reviewer"]["effort"], "\n  ".join(r["reasons"])))
    return 0


# ---------------------------------------------------------------- actors

def current_gen(item, role):
    return q1("SELECT * FROM generations WHERE item=? AND role=? ORDER BY gen DESC LIMIT 1", item, role)


def check_cap(item, role, gen, token):
    """Return (generation row, None) or (None, reason)."""
    if not (item and role and token):
        return None, "missing FLEET_ITEM/FLEET_ROLE/FLEET_CAPABILITY"
    g = current_gen(item, role)
    if not g:
        return None, "no %s generation for %s" % (role, item)
    if g["fenced"]:
        return None, "generation is fenced"
    if str(g["gen"]) != str(gen):
        return None, "stale generation (current is %s)" % g["gen"]
    if not hmac.compare_digest(g["capability_sha256"], sha256_bytes(token.encode())):
        return None, "capability does not match current generation"
    return g, None


def actor(args, allowed):
    """Single auth helper: returns (role, item). Fails closed with exit 3."""
    if ACTOR_OVERRIDE:
        if ACTOR_OVERRIDE in allowed:
            return ACTOR_OVERRIDE, None
        raise Fail("denied: %s may not run this command" % ACTOR_OVERRIDE, 3)
    if getattr(args, "human", False):
        if os.environ.get("FLEET_ROLE") or os.environ.get("FLEET_CAPABILITY"):
            # a fleet-launched session must act under its own capability
            raise Fail("denied: --human is not accepted from a fleet session", 3)
        if "human" in allowed:
            return "human", None
        raise Fail("denied: --human not accepted here", 3)
    e = os.environ
    role = e.get("FLEET_ROLE")
    if role not in allowed or role in ("human", "supervisor"):
        raise Fail("denied: need capability of role %s (or --human)" % "/".join(sorted(allowed)), 3)
    g, why = check_cap(e.get("FLEET_ITEM"), role, e.get("FLEET_GEN"), e.get("FLEET_CAPABILITY"))
    if not g:
        raise Fail("denied: " + why, 3)
    return role, g["item"]


def get_item(item):
    r = q1("SELECT * FROM items WHERE id=?", item)
    if not r:
        raise Fail("unknown item %r" % item)
    return r


def workdir(item, role="worker"):
    g = current_gen(item, role)
    return g["worktree"] if g else None


def read_handoff(item):
    wd = workdir(item)
    p = os.path.join(wd, "HANDOFF.md") if wd else None
    if not p or not os.path.exists(p):
        return None, None
    with open(p) as f:
        return p, f.read()


# ---------------------------------------------------------------- spawning

def next_gen(item):
    return (q1("SELECT MAX(gen) m FROM generations WHERE item=?", item)["m"] or 0) + 1


def build_argv(rt, resume, vals):
    tmpl = rt.get("resume" if resume else "start")
    if not tmpl:
        raise Fail("runtime has no %s template" % ("resume" if resume else "start"))
    argv = []
    for a in tmpl:
        for k, v in vals.items():
            a = a.replace("{%s}" % k, str(v))
        argv.append(a)
    return argv


def spawn(argv, cwd, prompt_file, log_path, env):
    with open(log_path, "ab") as log:
        stdin = open(prompt_file, "rb")
        try:
            p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=stdin, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            stdin.close()
    return p.pid


def start_gen(it, role, rtname, res, wd, branch, prompt, tier, session_id, resume, log_path=None):
    """Create a new generation (fencing the previous one of this role) and spawn it."""
    rt = S.cfg["runtimes"][rtname]
    gen = next_gen(it["id"])
    token = secrets.token_hex(16)
    logs = os.path.join(S.home, "logs")
    log_path = log_path or os.path.join(logs, "%s-%s-g%d.log" % (it["id"], role, gen))
    pf = os.path.join(logs, "%s-%s-g%d.prompt-%d.md" % (it["id"], role, gen, int(time.time() * 1000)))
    with open(pf, "w") as f:
        f.write(prompt.replace("@@GEN@@", str(gen)))
    if not resume and rt.get("session_id_mode", "assign") == "assign" and not session_id:
        session_id = str(uuid.uuid4())
    if resume and not session_id and "{session_id}" in " ".join(rt.get("resume", [])):
        raise Fail("session id unknown (not captured yet); cannot resume")
    argv = build_argv(rt, resume, {"prompt_file": pf, "cwd": wd, "model": res["model"],
                                   "effort": res["effort"], "session_id": session_id or ""})
    env = dict(os.environ, FLEET_HOME=S.home, FLEET_ITEM=it["id"], FLEET_ROLE=role,
               FLEET_GEN=str(gen), FLEET_CAPABILITY=token)
    if getattr(S, "config_arg", None):
        env["FLEET_CONFIG"] = S.config_arg
    x("UPDATE generations SET fenced=1 WHERE item=? AND role=? AND fenced=0", it["id"], role)
    x("INSERT INTO generations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,0)", it["id"], gen, role, rtname,
      res["model"], res["effort"], tier, socket.gethostname(), branch, wd, session_id or "",
      sha256_bytes(token.encode()), log_path, now())
    try:
        pid = spawn(argv, wd, pf, log_path, env)
    except OSError as e:
        x("UPDATE generations SET fenced=1 WHERE item=? AND gen=?", it["id"], gen)
        raise Fail("cannot start %s: %s" % (argv[0], e))
    x("UPDATE generations SET pid=? WHERE item=? AND gen=?", pid, it["id"], gen)
    return {"gen": gen, "pid": pid, "log_path": log_path, "session_id": session_id or "", "argv": argv}


def refresh_sessions():
    for g in q("SELECT * FROM generations WHERE session_id=''"):
        rt = S.cfg["runtimes"].get(g["runtime"], {})
        if rt.get("session_id_mode") != "capture" or not rt.get("session_id_regex"):
            continue
        try:
            with open(g["log_path"], errors="replace") as f:
                m = re.search(rt["session_id_regex"], f.read())
        except OSError:
            continue
        if m:
            x("UPDATE generations SET session_id=? WHERE item=? AND gen=?", m.group(1) if m.groups() else m.group(0),
              g["item"], g["gen"])


def resume_gen(g, message=None, use_steers=True):
    """Resume a generation's session as a fresh generation (new token, same session/log/effort)."""
    it = get_item(g["item"])
    if alive(g["pid"]):
        raise Fail("session %s g%d is still running; refusing to resume" % (g["item"], g["gen"]))
    if it["state"] in TERMINAL:
        raise Fail("item is %s" % it["state"])
    refresh_sessions()
    g = q1("SELECT * FROM generations WHERE item=? AND gen=?", g["item"], g["gen"])
    parts = []
    steers = q("SELECT rowid r, * FROM steers WHERE item=? AND delivered=0 ORDER BY rowid", g["item"]) if use_steers else []
    for s in steers:
        parts.append("Steer (%s): %s" % (s["ts"], s["message"]))
    if message:
        parts.append(message)
    prompt = ("Resuming %s %s (generation @@GEN@@). Re-read HANDOFF.md first; it is your only task input.\n\n%s\n"
              % (g["role"], g["item"], "\n\n".join(parts) or "Continue from the HANDOFF."))
    if message and message.startswith("[fleet-watch]"):
        prompt = message + "\n"  # watch wake: first line is the marker, one line per event
    res = {"model": g["model"], "effort": g["effort"]}
    r = start_gen(it, g["role"], g["runtime"], res, g["worktree"], g["branch"], prompt, g["tier"],
                  g["session_id"], True, g["log_path"])
    for s in steers:
        x("UPDATE steers SET delivered=1 WHERE rowid=?", s["r"])
    emit(g["item"], "resumed", role=g["role"], gen=r["gen"], from_gen=g["gen"], steers=len(steers))
    return r


# ---------------------------------------------------------------- launch

def count_active(exclude_item, repo=None):
    n = 0
    for g in q("SELECT g.*, i.repo FROM generations g JOIN items i ON i.id=g.item WHERE g.role='worker' "
               "AND g.fenced=0 AND g.item<>? AND g.gen=(SELECT MAX(gen) FROM generations "
               "WHERE item=g.item AND role='worker')", exclude_item):
        if (repo is None or g["repo"] == repo) and alive(g["pid"]):
            n += 1
    return n


def ensure_worktree(info, wt):
    repo = info["repo"]
    if not os.path.isdir(repo):
        raise Fail("repo not found: %s" % repo)
    git(repo, "fetch", "origin", timeout=120)
    git(repo, "rev-parse", "--verify", "origin/" + info["base"])
    out_ = git(repo, "worktree", "list", "--porcelain")
    cur = None
    for ln in out_.splitlines():
        if ln.startswith("worktree "):
            cur = ln[9:]
        elif ln == "branch refs/heads/" + info["branch"]:
            return cur
    if os.path.exists(wt):
        raise Fail("worktree path exists but is not a worktree of %s: %s" % (info["branch"], wt))
    if run_git(repo, "show-ref", "--verify", "--quiet", "refs/heads/" + info["branch"]).returncode == 0:
        git(repo, "worktree", "add", wt, info["branch"])
    else:
        git(repo, "worktree", "add", "-b", info["branch"], wt, "origin/" + info["base"])
    return wt


def exclude_handoff(wd):
    common = git(wd, "rev-parse", "--git-common-dir")
    common = common if os.path.isabs(common) else os.path.join(wd, common)
    p = os.path.join(common, "info", "exclude")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    cur = open(p).read() if os.path.exists(p) else ""
    if "HANDOFF.md" not in cur.splitlines():
        with open(p, "a") as f:
            f.write(("" if cur.endswith("\n") or not cur else "\n") + "HANDOFF.md\n")


RELAY_MAX = 12000
RELAY_READ = 512 * 1024
REDACTIONS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), "[REDACTED]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)([?&][a-z0-9_.-]*(?:token|key|secret|passw(?:or)?d|signature|sig|credential)[a-z0-9_.-]*=)[^&\s'\"]+"),
     r"\1[REDACTED]"),
    (re.compile(r"(?i)\b([a-z0-9_]*(?:token|secret|passw(?:or)?d|api[_-]?key|access[_-]?key|credential|capability)"
                r"[a-z0-9_]*)(\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;\"']+)"), r"\1\2[REDACTED]"),
    (re.compile(r"\b(?:sk|pk|ghp|gho|ghs|github_pat|xox[bap]|AKIA)[-_A-Za-z0-9]{12,}"), "[REDACTED]"),
    (re.compile(r"(?![0-9a-f]{40}\b)(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])[A-Za-z0-9+/_-]{40,}"), "[REDACTED]"),
]


def redact(s):
    for rx, sub in REDACTIONS:
        s = rx.sub(sub, s)
    return s


def turn_text(o):
    """Flatten one JSON log record into the text a person would read: messages and tool calls."""
    parts = []

    def walk(v):
        if isinstance(v, dict):
            if isinstance(v.get("text"), str):
                parts.append(v["text"])
            cmd = v.get("command") or v.get("cmd")
            if isinstance(cmd, (str, list)):
                parts.append("tool: %s" % (cmd if isinstance(cmd, str) else " ".join(map(str, cmd))))
            elif v.get("type") == "tool_use" and v.get("name"):
                parts.append("tool %s: %s" % (v["name"], json.dumps(v.get("input", {}), sort_keys=True)[:300]))
            for k, c in v.items():
                if isinstance(c, (dict, list)) and k != "input":
                    walk(c)
        elif isinstance(v, list):
            for c in v:
                walk(c)

    walk(o)
    return "[%s] %s" % (o.get("type") or "event", " | ".join(parts)) if parts else ""


def relay_context(prev, wd, base):
    """Bounded, redacted tail of the previous generation's log plus a diff stat. Empty when the log is missing."""
    try:
        size = os.path.getsize(prev["log_path"])
        with open(prev["log_path"], "rb") as f:
            if size > RELAY_READ:
                f.seek(size - RELAY_READ)
                f.readline()
            raw = f.read().decode("utf-8", "replace")
    except (OSError, TypeError):
        return ""
    lines = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                ln = turn_text(json.loads(ln))
            except (ValueError, AttributeError):
                pass
        if ln:
            lines.append(ln)
    stat = ""
    if wd and base and os.path.isdir(wd):
        r = run_git(wd, "diff", "--stat", "origin/" + base, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            stat = "Diff stat vs origin/%s:\n%s\n" % (base, redact(r.stdout.strip())[:2000])
    head = ("\nContext relay from the previous generation (read-only hints, redacted, possibly incomplete; "
            "the HANDOFF is the only authority):\n")
    body = redact("\n".join(lines)).replace("@@GEN@@", "")
    room = RELAY_MAX - len(head) - len(stat) - len("Log tail:\n") - 1
    return head + stat + "Log tail:\n" + body[-room:] + "\n"


def launch_prompt(role, item, handoff, wd, extra=""):
    base = ("You are the %s for fleet item %s (generation @@GEN@@).\n"
            "The HANDOFF is your only task input: %s\n"
            "Working directory: %s\n"
            "Environment variables set for you: %s (never print FLEET_CAPABILITY).\n" % (
                role, item, handoff, wd, ", ".join(ENV_NAMES)))
    if role == "worker":
        base += ("Before editing, committing or building, run: python3 %s preflight --action edit|commit|build "
                 "--target PATH\nWrite only the State, Progress and Evidence sections of HANDOFF.md.\n" % SCRIPT)
    return base + extra


def launch_core(card, role, dry, tier=None, replace=False, reason=""):
    text, fm, errs = load_card_file(card)
    if errs:
        raise Fail("card failed lint:\n  " + "\n  ".join(errs))
    info = card_info(fm)
    rtname = fm["runtime"]
    rt = S.cfg["runtimes"].get(rtname)
    if not rt:
        raise Fail("unknown runtime %r" % rtname)
    routed = route_card(fm)
    if tier and role == "coordinator":
        raise Fail("--tier does not apply to the coordinator; its effort comes from config")
    if role == "worker":
        res = resolve_any_tier(rtname, tier) if tier else routed["worker"]
    elif role == "reviewer":
        res = resolve_any_tier(rtname, tier) if tier else routed["reviewer"]
    else:
        res = resolve_tier(rtname, S.cfg["coordinator"]["effort"])
    chash = contract_hash(text)
    it = None
    if os.path.exists(os.path.join(S.home, "fleet.db")):
        it = q1("SELECT * FROM items WHERE id=?", info["id"])
    prev, tuple_changed = None, False
    if it:
        if it["card_sha256"] != chash:
            raise Fail("card contract differs from the registered one; a changed contract needs a new item")
        if it["state"] in TERMINAL:
            raise Fail("item is %s; reopening needs a new card" % it["state"])
        if info["code"] and it["branch"]:
            info["branch"] = it["branch"]  # the registry owns the branch (a rework rename is rebound there)
        prev = current_gen(it["id"], role)
        if prev and role in ("worker", "reviewer") and not tier and prev["model"] is not None:
            # a relaunch without an explicit tier keeps the registered tuple exactly
            res = {"tier": prev["tier"], "effective_tier": prev["tier"], "clamped": False, "clamp_direction": None,
                   "model": prev["model"], "effort": prev["effort"]}
        if prev and role == "coordinator" and (prev["runtime"], prev["model"], prev["effort"]) != (
                rtname, res["model"], res["effort"]):
            tuple_changed = True
            if alive(prev["pid"]):
                raise Fail("a coordinator effort/runtime change needs an idle coordinator")
            if not os.path.exists(os.path.join(S.home, "work", info["slug"], "HANDOFF.md")):
                raise Fail("a coordinator effort/runtime change needs a fresh generation started from its HANDOFF; none found")
        if prev and not prev["fenced"] and alive(prev["pid"]) and not replace:
            raise Fail("a live %s generation already exists (g%d); use escalate to replace it" % (role, prev["gen"]))
    if role == "worker":
        lim = S.cfg["limits"]
        if os.path.exists(os.path.join(S.home, "fleet.db")):
            if count_active(info["id"]) >= lim["max_active"]:
                raise Fail("limit: max_active=%d workers already running" % lim["max_active"])
            if info["code"] and count_active(info["id"], info["repo"]) >= lim["max_active_per_repo"]:
                raise Fail("limit: max_active_per_repo=%d workers already running for this repo" % lim["max_active_per_repo"])
    extra, wd, branch, rev_info = "", None, info["branch"], None
    if role == "reviewer":
        if not it or not workdir(info["id"]):
            raise Fail("no worker generation to review")
        v = run_verify(info["id"])
        if not v["ok"]:
            raise Fail("verify failed; cannot start a reviewer:\n  " + "\n  ".join(v["failures"]))
        wd = workdir(info["id"])
        rev_info = {"candidate_sha": v["candidate_sha"] or "-", "manifest_sha256": v["manifest_sha256"]}
        extra = ("Review the candidate commit %s (manifest sha256 %s) in this worktree. Do NOT edit it and do not "
                 "change item state. Record your verdict with:\n  python3 %s review-record %s PASS|REWORK "
                 "--candidate-sha %s --manifest-sha256 %s --notes-file review.md\n"
                 % (rev_info["candidate_sha"], rev_info["manifest_sha256"], SCRIPT, info["id"],
                    rev_info["candidate_sha"], rev_info["manifest_sha256"]))
    elif role == "worker" and info["code"]:
        wd = os.path.join(S.home, "worktrees", info["slug"])
    else:
        wd = os.path.join(S.home, "work", info["slug"])
    if role == "worker" and reason == "escalation":
        extra = "You continue from the evolved HANDOFF of an earlier generation; do not restart from scratch.\n"
    gen_no = next_gen(info["id"]) if it else 1
    plan = {"item": info["id"], "role": role, "runtime": rtname, "gen": gen_no, "tier": res["tier"],
            "effective_tier": res["effective_tier"], "clamped": res["clamped"], "model": res["model"],
            "effort": res["effort"], "branch": branch, "workdir": wd, "env": ENV_NAMES,
            "argv": build_argv(rt, False, {"prompt_file": "<prompt_file>", "cwd": wd, "model": res["model"],
                                           "effort": res["effort"], "session_id": "<session-id>"})}
    if dry:
        return plan
    if role == "worker":
        if info["code"]:
            wd = ensure_worktree(info, wd)
        else:
            os.makedirs(wd, exist_ok=True)
        plan["workdir"] = wd
    elif role == "coordinator":
        os.makedirs(wd, exist_ok=True)
    if not it:
        cp = os.path.join(S.home, "cards", info["slug"] + ".md")
        with open(cp, "w") as f:
            f.write(text)
        x("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?)", info["id"], cp, chash, info["repo"], info["base"],
          info["branch"], "queued", rtname, now(), now())
        it = get_item(info["id"])
    hp = os.path.join(wd, "HANDOFF.md")
    if role != "reviewer":
        if not os.path.exists(hp):
            with open(hp, "w") as f:
                f.write(text)
        if info["code"] and role == "worker":
            exclude_handoff(wd)
    if prev:
        extra += relay_context(prev, wd if role != "coordinator" else None, info["base"] if info["code"] else None)
    prompt = launch_prompt(role, info["id"], hp, wd, extra)
    r = start_gen(it, role, rtname, res, wd, branch, prompt, res["tier"], None, False)
    if tuple_changed:
        emit(info["id"], "coordinator:tuple_changed", gen=r["gen"], effort=res["effort"], model=res["model"],
             runtime=rtname)
    state = {"worker": "in-progress", "reviewer": "in-review", "coordinator": it["state"]}[role]
    if role == "coordinator" and it["state"] == "queued":
        state = "in-progress"
    x("UPDATE items SET state=?, updated=? WHERE id=?", state, now(), info["id"])
    worker_gen = current_gen(info["id"], "worker")
    emit(info["id"], "launched", role=role, gen=r["gen"], tier=res["tier"], effort=res["effort"],
         model=res["model"], reviewing_worker_gen=(worker_gen["gen"] if role == "reviewer" else None),
         reason=reason, **(rev_info or {}))
    plan.update(gen=r["gen"], pid=r["pid"], log=r["log_path"], session_id=r["session_id"])
    return plan


def cmd_launch(args):
    if not args.dry_run:
        actor(args, {"human"} if args.role == "coordinator" else {"coordinator", "human"})
    plan = launch_core(args.card, args.role, args.dry_run, tier=args.tier)
    out(args, dict(plan, dry_run=args.dry_run),
        "%s %s g%s tier=%s effort=%s\n  workdir: %s\n  argv: %s" % (
            "would launch" if args.dry_run else "launched", plan["role"], plan["gen"], plan["tier"],
            plan["effort"], plan["workdir"], shlex.join(plan["argv"])))
    return 0


# ---------------------------------------------------------------- verify / review / land

def adds_nothing(repo, base, sha):
    """True when the candidate contributes nothing to origin/<base>: an ancestor, or every commit has a
    patch-equivalent already there (all `git cherry` marks are '-')."""
    if run_git(repo, "merge-base", "--is-ancestor", sha, "origin/" + base).returncode == 0:
        return True
    r = run_git(repo, "cherry", "origin/" + base, sha)
    marks = [ln[:1] for ln in r.stdout.splitlines() if ln.strip()] if r.returncode == 0 else []
    return bool(marks) and all(m == "-" for m in marks)


def check_manifest(man, item, wd):
    f = []
    if not isinstance(man, dict):
        return ["manifest is not a JSON object"], []
    for k in ("item", "claim", "evidence", "known_gaps"):
        if k not in man:
            f.append("manifest missing key: %s" % k)
    for k in man:
        if k not in ("item", "claim", "acceptance_map", "evidence", "known_gaps"):
            f.append("manifest has unknown key: %s" % k)
    if "item" in man and man["item"] != item:
        f.append("manifest item %r != %r" % (man["item"], item))
    if "claim" in man and not isinstance(man["claim"], str):
        f.append("claim must be a string")
    if "known_gaps" in man and not (isinstance(man["known_gaps"], list) and all(isinstance(s, str) for s in man["known_gaps"])):
        f.append("known_gaps must be a list of strings")
    ev = man.get("evidence")
    commits, ids = [], set()
    if not isinstance(ev, list) or not ev:
        return f + ["evidence must be a non-empty list"], commits
    realwd = os.path.realpath(wd)
    for i, e in enumerate(ev):
        tag = "evidence[%d]" % i
        if not isinstance(e, dict) or not e.get("id") or e.get("kind") not in KINDS:
            f.append("%s needs id and kind in %s" % (tag, "|".join(KINDS)))
            continue
        ids.add(e["id"])
        if "sha" in e and not (isinstance(e["sha"], str) and SHA1.match(e["sha"])):
            f.append("%s sha must be 40 hex" % tag)
        if "sha256" in e and not (isinstance(e["sha256"], str) and SHA2.match(e["sha256"])):
            f.append("%s sha256 must be 64 hex" % tag)
        if e["kind"] == "commit":
            commits.append(e)
            if not e.get("sha") or not e.get("remote_ref"):
                f.append("%s commit needs sha and remote_ref" % tag)
        if e["kind"] in ("artifact", "report", "test") and (e.get("path") or e["kind"] != "test"):
            p = e.get("path")
            if not p:
                f.append("%s %s needs path" % (tag, e["kind"]))
                continue
            full = os.path.realpath(os.path.join(wd, p))
            if full != realwd and not full.startswith(realwd + os.sep):
                f.append("%s path escapes the work directory: %s" % (tag, p))
            elif not os.path.isfile(full):
                f.append("%s path not found: %s" % (tag, p))
            elif not e.get("sha256"):
                f.append("%s path needs sha256" % tag)
            elif sha256_file(full) != e["sha256"]:
                f.append("%s sha256 mismatch for %s" % (tag, p))
    for m in man.get("acceptance_map") or []:
        if not isinstance(m, dict) or "criterion" not in m or m.get("evidence_ref") not in ids:
            f.append("acceptance_map entry must reference an evidence id: %r" % (m,))
    return f, commits


def run_verify(itemid):
    it = get_item(itemid)
    res = {"ok": False, "failures": [], "manifest_sha256": None, "candidate_sha": None}
    f = res["failures"]
    hp, text = read_handoff(itemid)
    if text is None:
        f.append("HANDOFF.md not found")
        return res
    wd = workdir(itemid)
    if contract_hash(text) != it["card_sha256"]:
        f.append("contract hash differs from registered card (contract was edited)")
    st = handoff_state(text)
    if st.get("state") != "awaiting-verify":
        f.append("state line is %r, expected awaiting-verify" % st.get("state"))
    elif not st.get("result"):
        f.append("state line has no result")
    try:
        man = read_manifest(text)
    except ValueError as e:
        f.append("evidence manifest unreadable: %s" % e)
        return res
    res["manifest_sha256"] = sha256_bytes(json.dumps(man, sort_keys=True, separators=(",", ":")).encode())
    mf, commits = check_manifest(man, itemid, wd)
    f.extend(mf)
    if it["repo"] != "-":
        if len(commits) != 1:
            f.append("code card needs exactly one commit entry (found %d)" % len(commits))
        for c in commits:
            sha, ref = c.get("sha"), c.get("remote_ref", "")
            res["candidate_sha"] = sha
            if ref not in ("origin/" + it["branch"], "refs/heads/" + it["branch"], it["branch"]):
                f.append("remote_ref %r is not the item branch %s" % (ref, it["branch"]))
                continue
            if not sha or not SHA1.match(sha):
                continue
            try:
                rs = remote_sha(it["repo"], it["branch"])
            except Fail as e:
                f.append(str(e))
                continue
            if rs != sha:
                f.append("commit %s is not the remote head of %s (remote: %s)" % (sha[:12], it["branch"], rs))
            g = run_git(it["repo"], "fetch", "origin", timeout=120)
            if g.returncode != 0:
                f.append("fetch failed: %s" % g.stderr.strip())
                continue
            r = run_git(it["repo"], "rev-list", "--count", "origin/%s..%s" % (it["base"], sha))
            if r.returncode != 0:
                f.append("candidate %s not found locally" % sha[:12])
            elif r.stdout.strip() != "1":
                if adds_nothing(it["repo"], it["base"], sha):
                    res["already_integrated"] = True  # nothing to land; complete-verified handles it
                else:
                    f.append("candidate must be exactly one commit above origin/%s (found %s)" % (it["base"], r.stdout.strip()))
            else:
                mb = run_git(it["repo"], "merge-base", "origin/" + it["base"], sha).stdout.strip()
                par = run_git(it["repo"], "rev-parse", sha + "^").stdout.strip()
                if mb != par:
                    f.append("candidate parent %s is not the merge-base %s" % (par[:12], mb[:12]))
    res["ok"] = not f
    return res


def cmd_verify(args):
    r = run_verify(args.item)
    out(args, r, "ok manifest=%s" % r["manifest_sha256"] if r["ok"] else "\n".join("verify: " + s for s in r["failures"]))
    return 0 if r["ok"] else 1


def cmd_review_record(args):
    role, who = actor(args, {"reviewer"})
    if who != args.item:
        raise Fail("denied: capability is for item %s" % who, 3)
    it = get_item(args.item)
    v = run_verify(args.item)
    if not v["ok"]:
        raise Fail("verify failed; no verdict recorded:\n  " + "\n  ".join(v["failures"]))
    if args.manifest_sha256 != v["manifest_sha256"]:
        raise Fail("manifest hash does not match the current manifest (%s)" % v["manifest_sha256"])
    if it["repo"] != "-" and args.candidate_sha != v["candidate_sha"]:
        raise Fail("candidate sha does not match the manifest commit")
    notes = ""
    if args.notes_file:
        try:
            with open(args.notes_file) as f:
                notes = f.read()
        except OSError as e:
            raise Fail("cannot read notes: %s" % e)
    verdict = args.verdict.upper()
    x("INSERT INTO reviews VALUES(?,?,?,?,?,?,?)", args.item, verdict, args.candidate_sha,
      args.manifest_sha256, int(os.environ["FLEET_GEN"]), now(), notes)
    emit(args.item, "review:" + verdict, candidate_sha=args.candidate_sha, manifest_sha256=args.manifest_sha256)
    out(args, {"item": args.item, "verdict": verdict}, "recorded %s" % verdict)
    return 0


def _patch_id(repo, a, b):
    d = run_git(repo, "diff", "--binary", a, b)
    p = subprocess.run(["git", "-C", repo, "patch-id", "--stable"], input=d.stdout, capture_output=True, text=True)
    return p.stdout.split()[0] if p.stdout.strip() else ""


def replay_equivalent(repo, base, sha, guards):
    """Replay the reviewed commits onto origin/<base> when the base drift cannot affect them.
    Safe only when (1) no drift file is a candidate file, (2) no drift file is under a guard prefix,
    (3) the candidate range has no merges, and (4) the replayed patch-id equals the reviewed one.
    Returns (new_sha, detail) or (None, reason)."""
    ob = "origin/" + base
    mb = git(repo, "merge-base", ob, sha)
    if git(repo, "rev-list", "--merges", mb + ".." + sha):
        return None, "candidate range contains merges"
    cand = set(git(repo, "diff", "--name-only", mb, sha).splitlines())
    drift = set(git(repo, "diff", "--name-only", mb, ob).splitlines())
    if cand & drift:
        return None, "base drift touches candidate files: " + ", ".join(sorted(cand & drift)[:5])
    hit = sorted(f for f in drift if any(f.startswith(g) for g in guards))
    if hit:
        return None, "base drift touches guarded paths: " + ", ".join(hit[:5])
    tmp = tempfile.mkdtemp(prefix="fleet-land-")
    try:
        git(repo, "worktree", "add", "--detach", tmp, ob)
        r = run_git(tmp, "cherry-pick", "--allow-empty", mb + ".." + sha)
        if r.returncode != 0:
            run_git(tmp, "cherry-pick", "--abort")
            return None, "replay failed: " + (r.stderr or r.stdout).strip()[-200:]
        new = git(tmp, "rev-parse", "HEAD")
    finally:
        run_git(repo, "worktree", "remove", "--force", tmp)
        shutil.rmtree(tmp, True)
    if _patch_id(repo, mb, sha) != _patch_id(repo, ob, new):
        return None, "patch-id changed after replay"
    return new, "replayed %s onto %s (drift %d files, disjoint from candidate and guards)" % (
        sha[:12], git(repo, "rev-parse", ob)[:12], len(drift))


def land_record(item, sha, phase, detail=""):
    x("INSERT INTO lands VALUES(?,?,?,?,?)", item, sha, phase, now(), detail)


def finish_landed(item, sha, via):
    land_record(item, sha, "landed", via)
    x("UPDATE items SET state='landed', updated=? WHERE id=?", now(), item)
    emit(item, "landed", sha=sha, via=via)


def cmd_land(args):
    actor(args, {"coordinator", "human"})
    it = get_item(args.item)
    sha = args.sha
    if it["repo"] == "-":
        raise Fail("land applies to code cards only")
    if not SHA1.match(sha):
        raise Fail("--sha must be a full 40-hex sha")
    last = q1("SELECT * FROM lands WHERE item=? ORDER BY rowid DESC LIMIT 1", args.item)
    unknown = bool(last and last["phase"] == "result_unknown")
    if args.reconcile:
        if not unknown:
            raise Fail("nothing to reconcile")
        if last["sha"] != sha:
            raise Fail("reconcile --sha must equal the unknown land's sha %s" % last["sha"])
        rs = remote_sha(it["repo"], it["base"])
        if rs == sha:
            finish_landed(args.item, sha, "reconcile")
            out(args, {"landed": True, "sha": sha}, "reconciled: remote %s == %s, recorded landed" % (it["base"], sha[:12]))
            return 0
        land_record(args.item, sha, "not_landed", "remote %s is %s" % (it["base"], rs))
        emit(args.item, "land:not_landed", urgent=True, sha=sha, remote=rs)
        raise Fail("remote %s is %s, not %s: the push did not take effect (recorded; land may be retried)" % (it["base"], rs, sha))
    if unknown:
        raise Fail("previous land result is unknown; run land --reconcile (inspects the remote only)")
    if last and last["phase"] == "landed":
        raise Fail("already landed")
    if it["state"] in TERMINAL:
        raise Fail("item is %s" % it["state"])

    def refuse(why):
        land_record(args.item, sha, "refused", why)
        raise Fail(why)

    rv = q1("SELECT * FROM reviews WHERE item=? ORDER BY rowid DESC LIMIT 1", args.item)
    if not rv or rv["verdict"] != "PASS":
        refuse("latest review is not PASS")
    if rv["candidate_sha"] != sha:
        refuse("latest review is bound to %s, not %s" % (rv["candidate_sha"], sha))
    v = run_verify(args.item)
    if not v["ok"]:
        refuse("verify failed: " + "; ".join(v["failures"]))
    if v["manifest_sha256"] != rv["manifest_sha256"] or v["candidate_sha"] != sha:
        refuse("review is stale: manifest or candidate changed since the verdict")
    land_record(args.item, sha, "checked")
    g = run_git(it["repo"], "fetch", "origin", timeout=120)
    if g.returncode != 0:
        refuse("fetch failed: " + g.stderr.strip())
    if adds_nothing(it["repo"], it["base"], sha):
        refuse("the candidate adds nothing to origin/%s: close it with complete-verified --kind already-integrated" % it["base"])
    reviewed, via = sha, "push"
    for attempt in range(3):
        push_sha = reviewed
        if run_git(it["repo"], "merge-base", "--is-ancestor", "origin/" + it["base"], reviewed).returncode != 0:
            if not args.rebase_equivalent:
                refuse("not a fast-forward: origin/%s moved; rework to rebase, then re-verify and re-review "
                       "(or land --rebase-equivalent when the drift is disjoint)" % it["base"])
            push_sha, why = replay_equivalent(it["repo"], it["base"], reviewed, args.guard or [])
            if not push_sha:
                refuse("not a fast-forward and not rebase-equivalent: " + why)
            land_record(args.item, push_sha, "rebased", "reviewed %s; %s" % (reviewed, why))
            emit(args.item, "land:rebased", reviewed=reviewed, sha=push_sha, detail=why)
            via = "push-rebased"
        land_record(args.item, push_sha, "pushing")
        try:
            r = git_push(it["repo"], push_sha, it["base"], S.cfg["limits"]["push_timeout_s"])
        except Exception as e:  # timeout or anything unexpected: the effect is unknown
            land_record(args.item, push_sha, "result_unknown", repr(e))
            emit(args.item, "result_unknown", urgent=True, sha=push_sha, error=repr(e))
            print("error: push result unknown (%s); never retry blindly: run land --reconcile" % type(e).__name__, file=sys.stderr)
            return 5
        if r.returncode == 0:
            finish_landed(args.item, push_sha, via)
            out(args, {"landed": True, "sha": push_sha, "reviewed_sha": reviewed},
                "landed %s on %s" % (push_sha[:12], it["base"]))
            return 0
        racing = args.rebase_equivalent and re.search(r"non-fast-forward|fetch first|rejected", r.stderr)
        if not racing or attempt == 2:
            land_record(args.item, push_sha, "failed", r.stderr.strip())
            emit(args.item, "land:failed", urgent=True, sha=push_sha, stderr=r.stderr.strip()[-300:])
            raise Fail("push rejected: " + r.stderr.strip())
        land_record(args.item, push_sha, "raced", r.stderr.strip()[-200:])  # base moved between fetch and push
        if run_git(it["repo"], "fetch", "origin", timeout=120).returncode != 0:
            refuse("fetch failed after a raced push")
    raise Fail("unreachable")


# ---------------------------------------------------------------- state / status

def cmd_mark(args):
    role, who = actor(args, {"worker", "coordinator", "human"})
    st = args.state
    it = get_item(args.item)
    if st not in WORKER_STATES + COORD_STATES:
        raise Fail("unknown state %r" % st)
    if it["state"] in TERMINAL:
        raise Fail("item is %s; terminal states never revert" % it["state"])
    if st in WORKER_STATES:
        if role != "worker" or who != args.item:
            raise Fail("denied: %s is set only by the item's worker capability" % st, 3)
        if st == "awaiting-verify":
            v = run_verify(args.item)
            if not v["ok"]:
                raise Fail("verify failed; cannot claim awaiting-verify:\n  " + "\n  ".join(v["failures"]))
    else:
        if role not in ("coordinator", "human"):
            raise Fail("denied: %s needs coordinator capability or --human" % st, 3)
        if st == "done" and it["repo"] == "-":
            raise Fail("a non-code card completes through complete-verified --kind noncode, not mark")
        if st == "done" and it["repo"] != "-" and it["state"] != "landed":
            raise Fail("a code card must be landed before done")
    x("UPDATE items SET state=?, updated=? WHERE id=?", st, now(), args.item)
    emit(args.item, "mark:" + st, by=role)
    out(args, {"item": args.item, "state": st}, "%s -> %s" % (args.item, st))
    return 0


def no_symlink(wd, rel):
    cur = wd
    for part in os.path.normpath(rel).split(os.sep):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return False
    return True


def verdict_basis(itemid, kind, v, candidate, coordinator_verdict):
    """Return (basis string, review rowid or None). Raises Fail when no acceptable verdict exists."""
    rows = q("SELECT rowid r, * FROM reviews WHERE item=? ORDER BY rowid", itemid)
    if coordinator_verdict is not None:
        if kind != "noncode":
            raise Fail("a coordinator verdict is accepted only for --kind noncode")
        if rows:
            raise Fail("a reviewer verdict exists for this item; a coordinator verdict is allowed only when none does")
        if not coordinator_verdict.strip() or "\n" in coordinator_verdict.strip():
            raise Fail("--coordinator-verdict must be one non-empty line")
        return "coordinator_verdict", None
    mine = [r for r in rows if r["manifest_sha256"] == v["manifest_sha256"] and (kind == "noncode" or r["candidate_sha"] == candidate)]
    if not mine:
        raise Fail("needs a reviewer PASS bound to the current manifest%s (or, for noncode with no reviewer verdict, "
                   "a --coordinator-verdict)" % ("" if kind == "noncode" else " and candidate"))
    last = mine[-1]
    if last["verdict"] != "PASS":
        raise Fail("the latest reviewer verdict is %s; needs a reviewer PASS" % last["verdict"])
    refresh_sessions()
    rg = q1("SELECT * FROM generations WHERE item=? AND gen=?", itemid, last["reviewer_gen"])
    workers = {g["session_id"] for g in q("SELECT session_id FROM generations WHERE item=? AND role='worker'", itemid)
               if g["session_id"]}
    if not rg or rg["role"] != "reviewer":
        raise Fail("the PASS was not recorded by a reviewer generation")
    if rg["session_id"] and rg["session_id"] in workers:
        raise Fail("the reviewer ran in the same session as the worker")
    return "reviewer_pass:%d" % last["r"], last["r"]


def cmd_complete_verified(args):
    role, _ = actor(args, {"coordinator", "human"})
    it = get_item(args.item)
    last = q1("SELECT * FROM completions WHERE item=? ORDER BY rowid DESC LIMIT 1", args.item)
    unknown = bool(last and last["phase"] in ("intent", "result_unknown"))
    if args.reconcile:
        if not unknown:
            raise Fail("nothing to reconcile")
        phase = "complete" if it["state"] == "done" else "not_completed"
        x("INSERT INTO completions VALUES(?,?,?,?,?)", args.item, last["kind"], phase, now(), "reconcile")
        out(args, {"item": args.item, "reconciled": phase}, "reconciled: %s" % phase)
        return 0
    if unknown:
        raise Fail("the previous completion outcome is unknown; run complete-verified --reconcile (inspects state only)", 5)
    if last and last["phase"] == "complete":
        if last["kind"] != args.kind:
            raise Fail("already completed as %s" % last["kind"])
        out(args, {"item": args.item, "kind": args.kind, "completed": True, "noop": True}, "already completed (no-op)")
        return 0
    if it["state"] not in LIVE_STATES:
        raise Fail("item is %s; only a live item can be completed" % it["state"])
    code = it["repo"] != "-"
    if args.kind == "noncode" and code:
        raise Fail("--kind noncode applies to non-code cards only (this card has a repo)")
    if args.kind == "already-integrated" and not code:
        raise Fail("--kind already-integrated applies to code cards only")
    v = run_verify(args.item)
    if not v["ok"]:
        raise Fail("verify failed:\n  " + "\n  ".join(v["failures"]))
    candidate = None
    if args.kind == "noncode":
        wd = os.path.realpath(workdir(args.item))
        _, text = read_handoff(args.item)
        ev = read_manifest(text)["evidence"]
        for e in ev:
            if e.get("path") and not no_symlink(wd, e["path"]):
                raise Fail("evidence path goes through a symlink: %s" % e["path"])
        if not any(e.get("kind") in ("artifact", "report") and e.get("path") for e in ev):
            raise Fail("a non-code completion needs at least one artifact or report with a path")
    else:
        candidate = args.candidate_sha
        if not candidate or not SHA1.match(candidate):
            raise Fail("--candidate-sha must be a full 40-hex sha")
        if candidate != v["candidate_sha"]:
            raise Fail("--candidate-sha differs from the manifest commit %s" % v["candidate_sha"])
        if not adds_nothing(it["repo"], it["base"], candidate):
            raise Fail("the candidate adds changes to origin/%s: land it instead" % it["base"])
    basis, review_id = verdict_basis(args.item, args.kind, v, candidate, args.coordinator_verdict)
    intent = {"item": args.item, "kind": args.kind, "manifest_sha256": v["manifest_sha256"], "candidate": candidate,
              "basis": basis, "coordinator_verdict": args.coordinator_verdict}
    digest = sha256_bytes(json.dumps(intent, sort_keys=True, separators=(",", ":")).encode())
    res = {"item": args.item, "kind": args.kind, "basis": basis, "intent_sha256": digest, "noop": False}
    if args.dry_run:
        out(args, dict(res, dry_run=True, would_complete=True), "dry run: would complete (%s)" % basis)
        return 0
    x("INSERT INTO completions VALUES(?,?,?,?,?)", args.item, args.kind, "intent", now(), json.dumps(intent, sort_keys=True))
    try:
        x("UPDATE items SET state='done', updated=? WHERE id=?", now(), args.item)
        emit(args.item, "complete-verified", completion=args.kind, basis=basis, intent_sha256=digest, by=role)
        x("INSERT INTO completions VALUES(?,?,?,?,?)", args.item, args.kind, "complete", now(), digest)
    except Exception as e:  # the effect may or may not have happened: terminal until reconciled
        try:
            x("INSERT INTO completions VALUES(?,?,?,?,?)", args.item, args.kind, "result_unknown", now(), repr(e))
        except Exception:
            pass
        print("error: completion result unknown (%s); run complete-verified --reconcile" % type(e).__name__, file=sys.stderr)
        return 5
    out(args, dict(res, completed=True, dry_run=False), "completed %s (%s, %s)" % (args.item, args.kind, basis))
    return 0


def gen_view(g):
    d = {k: g[k] for k in ("gen", "tier", "model", "effort", "session_id", "pid", "fenced", "worktree", "log_path")}
    d["alive"] = alive(g["pid"])
    try:
        d["log_mtime"] = dt.datetime.fromtimestamp(os.path.getmtime(g["log_path"]), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        d["log_mtime"] = None
    return d


def item_status(it):
    d = {k: it[k] for k in ("id", "state", "repo", "base", "branch", "runtime", "updated")}
    _, text = read_handoff(it["id"])
    d["handoff"] = handoff_state(text) if text else {}
    d["generations"] = {}
    for role in ("worker", "reviewer", "coordinator"):
        g = current_gen(it["id"], role)
        if g:
            d["generations"][role] = gen_view(g)
    return d


def cmd_status(args):
    refresh_sessions()
    rows = [get_item(args.item)] if args.item else q("SELECT * FROM items ORDER BY created")
    data = [item_status(r) for r in rows]
    text = "\n".join("%s  %s  handoff=%s  %s" % (d["id"], d["state"], d["handoff"].get("state", "-"), " ".join(
        "%s:g%d%s" % (r, g["gen"], "*" if g["alive"] else "") for r, g in d["generations"].items())) for d in data)
    out(args, data[0] if args.item else data, text or "(no items)")
    return 0


# ---------------------------------------------------------------- steering

def cmd_steer(args):
    role, _ = actor(args, {"coordinator", "supervisor", "human"})
    it = get_item(args.item)
    lim = S.cfg["limits"]
    n = q1("SELECT COUNT(*) c FROM steers WHERE item=? AND ts>=?", args.item, ago(lim["steer_window_minutes"]))["c"]
    if n >= lim["steers_per_window"]:
        raise Fail("steer rate limit: %d steers in the last %d min; open a decision or re-slice the card"
                   % (n, lim["steer_window_minutes"]), 4)
    x("INSERT INTO steers VALUES(?,?,?,0)", args.item, now(), args.message)
    emit(args.item, "steer", by=role, message=args.message[:200])
    g = current_gen(args.item, "worker")
    delivered, note = False, "queued until the session is idle"
    if g and not g["fenced"] and not alive(g["pid"]) and it["state"] not in TERMINAL:
        try:
            resume_gen(g)
            delivered, note = True, "delivered via resume"
        except Fail as e:
            note = "queued (%s)" % e
    out(args, {"item": args.item, "delivered": delivered, "note": note}, "steer %s: %s" % (args.item, note))
    return 0


def worker_gen_for_resume(item):
    g = current_gen(item, "worker")
    if not g:
        raise Fail("no worker generation for %s" % item)
    return g


def cmd_resume(args):
    actor(args, {"coordinator", "supervisor", "human"})
    r = resume_gen(worker_gen_for_resume(args.item), args.message)
    out(args, r, "resumed %s as g%d (pid %d)" % (args.item, r["gen"], r["pid"]))
    return 0


def cmd_rework(args):
    actor(args, {"coordinator", "human"})
    it = get_item(args.item)
    if it["state"] in TERMINAL:
        raise Fail("item is %s" % it["state"])
    try:
        with open(args.requirements_file) as f:
            req = f.read().strip()
    except OSError as e:
        raise Fail("cannot read requirements: %s" % e)
    hp, _ = read_handoff(args.item)
    if not hp:
        raise Fail("no HANDOFF to append to")
    append_progress(hp, "REWORK:\n" + req)
    x("UPDATE items SET state='in-progress', updated=? WHERE id=?", now(), args.item)
    emit(args.item, "rework", chars=len(req))
    res = {"item": args.item, "resumed": False}
    try:
        res.update(resumed=True, gen=resume_gen(worker_gen_for_resume(args.item), "Rework requested: see the REWORK block in Progress.")["gen"])
    except Fail as e:
        res["note"] = str(e)
    out(args, res, "rework recorded; " + ("resumed g%s" % res.get("gen") if res["resumed"] else res["note"]))
    return 0


def cmd_escalate(args):
    actor(args, {"coordinator", "human"})
    it = get_item(args.item)
    g = worker_gen_for_resume(args.item)
    if g["tier"] in (S.cfg.get("model_tiers") or {}):
        raise Fail("worker runs an approved tier (%s): change it with launch --tier, not escalate" % g["tier"])
    if g["tier"] not in TIERS[:4]:
        raise Fail("already at %s: split the card or open a human decision for max" % g["tier"])
    nxt = TIERS[TIERS.index(g["tier"]) + 1]
    if nxt == "max":
        raise Fail("already at xhigh: split the card or open a human decision for max")
    used = {r["tier"] for r in q("SELECT tier FROM generations WHERE item=? AND role='worker'", args.item)}
    if nxt in used:
        raise Fail("tier %s was already used for %s" % (nxt, args.item))
    plan = launch_core(it["card_path"], "worker", False, tier=nxt, replace=True, reason="escalation")
    emit(args.item, "escalated", urgent=False, from_tier=g["tier"], to_tier=nxt, reason=args.reason or "", gen=plan["gen"])
    out(args, plan, "escalated %s: %s -> %s (g%d)" % (args.item, g["tier"], nxt, plan["gen"]))
    return 0


# ---------------------------------------------------------------- preflight

def rebind_branch(g, cur):
    """A rework rename (<registered stem>-rN) of the card's own branch is rebound in the registry, with an audit event."""
    stem = re.sub(r"-r\d+$", "", g["branch"])
    if not re.fullmatch(re.escape(stem) + r"-r[1-9]\d*", cur):
        return False
    x("BEGIN IMMEDIATE")
    try:
        n = x("UPDATE items SET branch=?, updated=? WHERE id=? AND branch=?", cur, now(), g["item"], g["branch"]).rowcount
        if n != 1:
            x("ROLLBACK")
            return False
        x("UPDATE generations SET branch=? WHERE item=? AND branch=?", cur, g["item"], g["branch"])
        emit(g["item"], "branch:rebound", **{"from": g["branch"], "to": cur, "gen": g["gen"]})
        x("COMMIT")
    except Exception:
        x("ROLLBACK")
        raise
    return True


def cmd_preflight(args):
    e = os.environ
    reason = None
    g, why = check_cap(e.get("FLEET_ITEM"), e.get("FLEET_ROLE"), e.get("FLEET_GEN"), e.get("FLEET_CAPABILITY")) \
        if e.get("FLEET_ROLE") == "worker" else (None, "role is not worker")
    if not g:
        reason = why
    elif g["host"] != socket.gethostname():
        reason = "host mismatch"
    else:
        root, tgt = os.path.realpath(g["worktree"]), os.path.realpath(args.target)
        if tgt != root and not tgt.startswith(root + os.sep):
            reason = "target is outside the bound worktree"
        elif args.action in ("commit", "build") and g["branch"]:
            r = run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
            cur = r.stdout.strip()
            if r.returncode != 0 or (cur != g["branch"] and not rebind_branch(g, cur)):
                reason = "worktree is on branch %r, registered %r" % (cur, g["branch"])
    ok = reason is None
    res = {"allowed": ok, "reason": "ok" if ok else reason,
           "permitted_actions": ["edit", "commit", "build"] if ok else
           ["write a blocker in HANDOFF Progress", "ask the coordinator to relaunch or steer (fleet.py steer/resume)"]}
    out(args, res, ("allowed" if ok else "denied: " + reason))
    return 0 if ok else 3


# ---------------------------------------------------------------- decisions

def cmd_decide_open(args):
    actor(args, {"coordinator", "supervisor", "human"})
    get_item(args.item)
    if args.tier == "human_required":
        miss = [k for k in ("options", "default", "deadline") if not getattr(args, k)]
        if miss:
            raise Fail("human_required decisions need --%s" % ", --".join(miss))
        if not parse_ts(args.deadline):
            raise Fail("--deadline must be ISO 8601")
    n = (q1("SELECT COUNT(*) c FROM decisions")["c"]) + 1
    did = "D-%d" % n
    state = "open" if args.tier == "human_required" else "void"
    x('INSERT INTO decisions(id,item,tier,question,options,"default",deadline,state,answer,ts) '
      "VALUES(?,?,?,?,?,?,?,?,NULL,?)", did, args.item, args.tier, args.question, args.options, args.default,
      args.deadline, state, now())
    emit(args.item, "decision:" + args.tier, urgent=args.tier == "human_required", id=did, question=args.question)
    out(args, {"id": did, "state": state}, "%s %s" % (did, state))
    return 0


def cmd_decide_answer(args):
    actor(args, {"human"})
    d = q1("SELECT * FROM decisions WHERE id=?", args.id)
    if not d or d["state"] != "open":
        raise Fail("no open decision %s" % args.id)
    x("UPDATE decisions SET state='answered', answer=?, ts=? WHERE id=? AND state='open'", args.text, now(), args.id)
    emit(d["item"], "decision:answered", id=args.id)
    out(args, {"id": args.id, "state": "answered"}, "%s answered" % args.id)
    return 0


def cmd_decide_poll(args):
    actor(args, {"coordinator", "human"})
    db().execute("BEGIN IMMEDIATE")
    try:
        sql, p = "SELECT * FROM decisions WHERE state='answered'", []
        if args.item:
            sql, p = sql + " AND item=?", [args.item]
        rows = [dict(r) for r in db().execute(sql + " ORDER BY ts, id", p).fetchall()]
        if args.ack:
            for r in rows:
                db().execute("UPDATE decisions SET state='acked' WHERE id=? AND state='answered'", (r["id"],))
                r["state"] = "acked"
        db().execute("COMMIT")
    except Exception:
        db().execute("ROLLBACK")
        raise
    out(args, rows, "\n".join("%s %s: %s" % (r["id"], r["item"], r["answer"]) for r in rows) or "(none)")
    return 0


# ---------------------------------------------------------------- events

def event_rows(after, item=None, urgent=False):
    sql, p = "SELECT * FROM events WHERE id>?", [after]
    if item:
        sql, p = sql + " AND item=?", p + [item]
    if urgent:
        sql += " AND urgent=1"
    return [{"id": r["id"], "ts": r["ts"], "item": r["item"], "kind": r["kind"], "urgent": bool(r["urgent"]),
             "detail": json.loads(r["detail_json"] or "{}")} for r in q(sql + " ORDER BY id", *p)]


def cmd_events(args):
    rows = event_rows(args.after, args.item, args.urgent_only)
    if getattr(args, "json", False):
        print(json.dumps(rows))
    else:
        for r in rows:
            print("%d %s %s %s%s" % (r["id"], r["ts"], r["item"], r["kind"], " URGENT" if r["urgent"] else ""))
    return 0


def cmd_wait(args):
    end = time.time() + args.timeout
    while True:
        rows = event_rows(args.after, args.item, args.urgent_only)
        if rows:
            for r in rows:
                print(json.dumps(r))
            return 0
        left = end - time.time()
        if left <= 0:
            return 2
        time.sleep(min(2, left))


def cmd_heartbeat(args):
    x("INSERT OR REPLACE INTO heartbeats VALUES(?,?)", args.role, now())
    out(args, {"role": args.role, "ts": now()}, "ok")
    return 0


def cmd_request(args):
    role, _ = actor(args, {"coordinator", "supervisor", "human"})
    if args.item:
        get_item(args.item)
    eid = emit(args.item or "-", "request:" + args.kind, by=role, message=args.message or "")
    out(args, {"event": eid}, "request recorded as event %d" % eid)
    return 0


# ---------------------------------------------------------------- watch

WAKEABLE = ("state:", "session:", "drift", "review:", "decision:answered", "dispatch:ready", "context:",
            "host_job:", "request:", "land:", "result_unknown")


def watch_items(new):
    def ev(item, kind, urgent=False, key=None, **d):
        if key is None or once(key):
            new.append(emit(item, kind, urgent, **d))

    for it in q("SELECT * FROM items WHERE state NOT IN ('done','cancelled')"):
        wg = current_gen(it["id"], "worker")
        if not wg or it["state"] == "queued":
            continue
        _, text = read_handoff(it["id"])
        st = handoff_state(text) if text else {}
        cur = st.get("state")
        last = q1("SELECT kind FROM events WHERE item=? AND kind LIKE 'state:%' ORDER BY id DESC LIMIT 1", it["id"])
        if cur in WORKER_STATES and (last["kind"] if last else "state:in-progress") != "state:" + cur:
            ev(it["id"], "state:" + cur, state=cur, doing=st.get("doing", ""))
        if it["state"] not in ("in-progress", "blocked", "awaiting-verify"):
            continue
        if not wg["fenced"] and wg["pid"] and not alive(wg["pid"]) and cur != "awaiting-verify":
            ev(it["id"], "session:exited", key="exited:%s:%d" % (it["id"], wg["gen"]), gen=wg["gen"])
        if cur == "in-progress" and alive(wg["pid"]):
            fm = parse_card(open(it["card_path"]).read()) or {}
            tb = timebox_minutes(fm.get("timebox"))
            upd = parse_ts(st.get("updated", "")) or parse_ts(wg["started"])
            if tb and upd and (dt.datetime.now(dt.timezone.utc) - upd).total_seconds() / 60 > \
                    S.cfg["limits"]["stale_fraction_of_timebox"] * tb:
                ev(it["id"], "session:stale", key="stale:%s:%d" % (it["id"], wg["gen"]), gen=wg["gen"])
        if it["repo"] != "-" and (cur == "awaiting-verify" or it["state"] == "awaiting-verify"):
            try:
                man_sha = next(e.get("sha") for e in read_manifest(text)["evidence"] if e.get("kind") == "commit")
                rs = remote_sha(it["repo"], it["branch"])
            except (ValueError, KeyError, StopIteration, TypeError, Fail):
                continue
            if rs and rs != man_sha:
                ev(it["id"], "drift", True, key="drift:%s:%s" % (it["id"], rs), manifest_sha=man_sha, remote_sha=rs)


def deliver_queued_steers(new):
    for r in q("SELECT DISTINCT item FROM steers WHERE delivered=0"):
        it = q1("SELECT * FROM items WHERE id=?", r["item"])
        g = current_gen(r["item"], "worker")
        if not it or it["state"] in TERMINAL or not g or g["fenced"] or alive(g["pid"]):
            continue
        try:
            resume_gen(g)
        except Fail as e:
            if once("undeliverable:%s:%d" % (r["item"], g["gen"])):
                new.append(emit(r["item"], "steer:undeliverable", reason=str(e)))


def send_digest():
    hb = q1("SELECT ts FROM heartbeats WHERE role='supervisor'")
    argv = (S.cfg.get("notify") or {}).get("argv")
    if not hb or not argv:
        return
    win = S.cfg["limits"]["supervisor_offline_after_minutes"]
    if hb["ts"] >= ago(win):
        return
    last = q1("SELECT MAX(CAST(substr(key,8) AS INTEGER)) m, MAX(ts) t FROM wakes WHERE key LIKE 'digest:%'")
    if last["t"] and last["t"] >= ago(win):
        return
    rows = [e for e in event_rows(last["m"] or 0, urgent=True)]
    rows += [e for e in event_rows(last["m"] or 0) if e["kind"] == "decision:human_required" and not e["urgent"]]
    if not rows:
        return
    mx = max(e["id"] for e in event_rows(last["m"] or 0))
    once("digest:%d" % mx)
    text = "fleet digest (supervisor offline)\n" + "\n".join("%d %s %s" % (e["id"], e["item"], e["kind"]) for e in rows) + "\n"
    try:
        subprocess.run(argv, input=text, text=True, timeout=30, capture_output=True, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        emit("-", "notify:failed", error=str(e)[:200])


def wake_coordinator():
    floor = q1("SELECT MAX(CAST(substr(key,6) AS INTEGER)) m FROM wakes WHERE key LIKE 'woke:%'")["m"] or 0
    coord_items = {r["item"] for r in q("SELECT DISTINCT item FROM generations WHERE role='coordinator'")}
    pending = [e for e in event_rows(floor) if e["item"] not in coord_items and e["kind"].startswith(WAKEABLE)]
    if not pending:
        return None
    cg = q1("SELECT * FROM generations WHERE role='coordinator' AND fenced=0 ORDER BY started DESC, gen DESC LIMIT 1")
    if not cg or alive(cg["pid"]):
        return None
    msg = "[fleet-watch]\n" + "\n".join("%d %s %s%s" % (e["id"], e["item"], e["kind"], " URGENT" if e["urgent"] else "")
                                         for e in pending)
    try:
        resume_gen(cg, msg, use_steers=False)
    except Fail as e:
        if once("wakefail:%d" % pending[-1]["id"]):
            emit(cg["item"], "wake:failed", reason=str(e))
        return None
    once("woke:%d" % pending[-1]["id"])
    return len(pending)


def cmd_watch(args):
    if not args.once:
        raise Fail("only --once is supported (run it from cron or a timer)")
    new = []
    refresh_sessions()
    watch_items(new)
    deliver_queued_steers(new)
    send_digest()
    woke = wake_coordinator() if args.wake else None
    out(args, {"new_events": new, "woke": woke}, "%d new event(s)" % len(new))
    return 0


# ---------------------------------------------------------------- ssh-gate

GATE_OK = ("status", "events", "wait", "heartbeat", "steer", "resume", "request")


def cmd_ssh_gate(args):
    global ACTOR_OVERRIDE
    try:
        toks = shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    except ValueError:
        toks = []
    ok = len(toks) >= 2 and os.path.basename(toks[0]) == "fleet.py" and toks[1] in GATE_OK and \
        not any(t == "--human" or t == "--config" or t.startswith("--config=") for t in toks[2:])
    if ok and toks[1] == "heartbeat":
        ok = toks[2:4] == ["--role", "supervisor"]
    if not ok:
        print("ssh-gate: command not allowed", file=sys.stderr)
        return 126
    ACTOR_OVERRIDE = "supervisor"
    try:
        return main(toks[1:])
    finally:
        ACTOR_OVERRIDE = None


# ---------------------------------------------------------------- host jobs

def host_dir(host):
    if not host or "/" in host or host.startswith("."):
        raise Fail("bad host name")
    return os.path.join(S.home, "hostq", host)


def cmd_host_job(args):
    host = args.host or socket.gethostname()
    hd = host_dir(host)
    if args.op == "open":
        actor(args, {"coordinator", "human"})
        get_item(args.item)
        try:
            argv, arts = json.loads(args.argv), json.loads(args.artifacts)
            assert isinstance(argv, list) and all(isinstance(a, str) for a in argv) and argv
            assert isinstance(arts, list)
        except (ValueError, AssertionError, TypeError):
            raise Fail("--argv must be a non-empty JSON list of strings; --artifacts a JSON list")
        jid = "J-%d" % (q1("SELECT COUNT(*) c FROM host_jobs")["c"] + 1)
        os.makedirs(os.path.join(hd, "requests"), exist_ok=True)
        os.makedirs(os.path.join(hd, "receipts"), exist_ok=True)
        with open(os.path.join(hd, "requests", jid + ".json"), "w") as f:
            json.dump({"id": jid, "item": args.item, "argv": argv, "timeout": args.timeout, "artifacts": arts}, f)
        x("INSERT INTO host_jobs VALUES(?,?,?,'open',?)", jid, args.item, host, now())
        emit(args.item, "host_job:opened", id=jid, host=host)
        out(args, {"id": jid}, jid)
        return 0
    rdir, qdir = os.path.join(hd, "receipts"), os.path.join(hd, "requests")
    if args.op == "status":
        data = {"requests": sorted(os.listdir(qdir)) if os.path.isdir(qdir) else [],
                "receipts": sorted(os.listdir(rdir)) if os.path.isdir(rdir) else []}
        out(args, data)
        return 0
    try:
        with open(os.path.join(hd, "capabilities.json")) as f:
            prefixes = json.load(f)["allowed_argv_prefixes"]
    except (OSError, ValueError, KeyError):
        raise Fail("missing or invalid capabilities.json for host %s (fail closed)" % host)
    done = []
    for name in sorted(os.listdir(qdir)) if os.path.isdir(qdir) else []:
        jid = name[:-5]
        rp = os.path.join(rdir, name)
        if os.path.exists(rp):
            continue  # never retry
        with open(os.path.join(qdir, name)) as f:
            rq = json.load(f)
        argv = rq["argv"]
        rc = {"id": jid, "item": rq["item"], "argv": argv}
        wd = workdir(rq["item"]) if os.path.exists(os.path.join(S.home, "fleet.db")) else None
        cwd = wd or hd
        if not any(argv[:len(p)] == p for p in prefixes):
            rc.update(exit_code=None, rejected=True, reason="argv not in allowed_argv_prefixes", duration_s=0,
                      stdout_tail="", stderr_tail="", artifacts={})
        else:
            t0 = time.time()
            try:
                r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=rq["timeout"])
                rc.update(exit_code=r.returncode, stdout_tail=r.stdout[-2000:], stderr_tail=r.stderr[-2000:])
            except subprocess.TimeoutExpired as e:
                rc.update(exit_code=124, timed_out=True, stdout_tail="", stderr_tail="timeout")
            except OSError as e:
                rc.update(exit_code=127, stdout_tail="", stderr_tail=str(e))
            rc["duration_s"] = round(time.time() - t0, 3)
            rc["artifacts"] = {}
            for a in rq["artifacts"]:
                ap = a if os.path.isabs(a) else os.path.join(cwd, a)
                rc["artifacts"][a] = sha256_file(ap) if os.path.isfile(ap) else None
        failed = rc.get("rejected") or rc["exit_code"] != 0 or None in rc["artifacts"].values()
        with open(rp, "w") as f:
            json.dump(rc, f, sort_keys=True)
        x("UPDATE host_jobs SET state=? WHERE id=?", "failed" if failed else "done", jid)
        if failed:
            emit(rq["item"], "host_job:failed", id=jid, exit_code=rc["exit_code"], rejected=bool(rc.get("rejected")))
            hp, _ = read_handoff(rq["item"])
            if hp:
                append_progress(hp, "HOST-JOB %s failed (exit %s); rework item needed." % (jid, rc["exit_code"]))
        done.append(jid)
    out(args, {"ran": done}, "processed: %s" % (", ".join(done) or "nothing"))
    return 0


# ---------------------------------------------------------------- init / parser

def cmd_init(args):
    for d in ("cards", "worktrees", "work", "logs", os.path.join("hostq", socket.gethostname(), "requests"),
              os.path.join("hostq", socket.gethostname(), "receipts")):
        os.makedirs(os.path.join(S.home, d), exist_ok=True)
    cfgp = os.path.join(S.home, "fleet.json")
    ex = os.path.join(os.path.dirname(SCRIPT), "..", "assets", "fleet.example.json")
    if not os.path.exists(cfgp) and not getattr(args, "config", None) and os.path.exists(ex):
        shutil.copy(ex, cfgp)
    c = sqlite3.connect(os.path.join(S.home, "fleet.db"))
    c.executescript(SCHEMA)
    c.close()
    out(args, {"home": S.home}, "initialised %s" % S.home)
    return 0


def A(*names, **kw):
    return names, kw


def build_parser():
    p = argparse.ArgumentParser(prog="fleet.py", allow_abbrev=False,
                                description="Reference CLI for fleet-dispatch (FLEET_HOME state, SQLite + git).")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def cmd(name, fn, help_, *flags):
        sp = sub.add_parser(name, help=help_, description=help_, allow_abbrev=False)
        sp.add_argument("--config", default=argparse.SUPPRESS, help="config file (default $FLEET_HOME/fleet.json)")
        sp.add_argument("--human", action="store_true", default=argparse.SUPPRESS, help="act as the human")
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output")
        for names, kw in flags:
            sp.add_argument(*names, **kw)
        sp.set_defaults(fn=fn)

    cmd("init", cmd_init, "create FLEET_HOME layout and database")
    cmd("card-lint", cmd_card_lint, "lint a dispatch card", A("card"))
    cmd("route", cmd_route, "compute tier/model/effort for a card", A("card"))
    cmd("launch", cmd_launch, "start a worker/reviewer/coordinator generation", A("card"),
        A("--role", choices=["worker", "reviewer", "coordinator"], default="worker"),
        A("--dry-run", action="store_true"), A("--tier", help="explicit tier: an approved model_tiers name or an effort tier"))
    cmd("status", cmd_status, "show items, HANDOFF state and sessions", A("item", nargs="?"))
    cmd("mark", cmd_mark, "set item state (capability-gated)", A("item"), A("state"))
    cmd("preflight", cmd_preflight, "gate a worker mutation", A("--action", choices=["edit", "commit", "build"], required=True),
        A("--target", required=True))
    cmd("steer", cmd_steer, "queue a correction (rate limited)", A("item"), A("message"))
    cmd("resume", cmd_resume, "resume the worker session", A("item"), A("message", nargs="?"))
    cmd("rework", cmd_rework, "send review requirements back to the worker", A("item"),
        A("--requirements-file", required=True))
    cmd("escalate", cmd_escalate, "new worker generation one tier up", A("item"), A("--reason"))
    cmd("verify", cmd_verify, "mechanical evidence check", A("item"))
    cmd("review-record", cmd_review_record, "record a reviewer verdict", A("item"),
        A("verdict", type=str.upper, choices=["PASS", "REWORK"]), A("--candidate-sha", required=True),
        A("--manifest-sha256", required=True), A("--notes-file"))
    cmd("land", cmd_land, "ff-only push of the reviewed SHA", A("item"), A("--sha", required=True),
        A("--reconcile", action="store_true"),
        A("--rebase-equivalent", action="store_true",
          help="if the base moved, replay the reviewed commits onto it when the drift is disjoint from the "
               "candidate files and every --guard prefix and the patch-id is unchanged"),
        A("--guard", action="append", help="path prefix the base drift must not touch (repeatable)"))
    cmd("complete-verified", cmd_complete_verified, "close a finished non-code card or an already-integrated candidate",
        A("item"), A("--kind", required=True, choices=["noncode", "already-integrated"]), A("--candidate-sha"),
        A("--coordinator-verdict"), A("--dry-run", action="store_true"), A("--reconcile", action="store_true"))
    cmd("decide-open", cmd_decide_open, "open a tiered decision", A("item"),
        A("--tier", required=True, choices=["self", "notify", "human_required"]), A("--question", required=True),
        A("--options"), A("--default"), A("--deadline"))
    cmd("decide-answer", cmd_decide_answer, "answer a decision (human only)", A("id"), A("text"))
    cmd("decide-poll", cmd_decide_poll, "list answered decisions; --ack consumes them", A("--item"),
        A("--ack", action="store_true"))
    cmd("events", cmd_events, "list attention-log events", A("--after", type=int, default=0), A("--item"),
        A("--urgent-only", action="store_true"))
    cmd("wait", cmd_wait, "long-poll for events (exit 2 on timeout)", A("--after", type=int, required=True),
        A("--timeout", type=float, default=1800), A("--item"), A("--urgent-only", action="store_true"))
    cmd("heartbeat", cmd_heartbeat, "record a supervisor/coordinator heartbeat",
        A("--role", required=True, choices=["supervisor", "coordinator"]))
    cmd("request", cmd_request, "record a control request for the coordinator",
        A("kind", choices=["dispatch", "rework", "escalate", "other"]), A("--item"), A("--message"))
    cmd("watch", cmd_watch, "platform pass: emit events, deliver steers, wake coordinator",
        A("--once", action="store_true"), A("--wake", action="store_true"))
    cmd("ssh-gate", cmd_ssh_gate, "forced-command gate for the supervisor key")
    cmd("host-job", cmd_host_job, "host pull queue: open | pull | status", A("op", choices=["open", "pull", "status"]),
        A("item", nargs="?"), A("--host"), A("--argv"), A("--artifacts", default="[]"),
        A("--timeout", type=int, default=600))
    return p


NO_STATE = ("card-lint", "ssh-gate")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "card" and len(argv) > 1 and argv[1] == "lint":
        argv = ["card-lint"] + argv[2:]
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    saved = (S.home, S.cfg, S.db, S.config_path)
    try:
        if args.cmd == "host-job" and args.op in ("open",) and not args.item:
            raise Fail("host-job open needs ITEM")
        S.config_arg = getattr(args, "config", None)
        setup(args)
        return args.fn(args) or 0
    except Fail as e:
        print("error: %s" % e, file=sys.stderr)
        return e.code
    except sqlite3.Error as e:
        print("error: database: %s" % e, file=sys.stderr)
        return 1
    finally:
        if S.db is not None:
            S.db.close()
        S.home, S.cfg, S.db, S.config_path = saved


if __name__ == "__main__":
    sys.exit(main())
