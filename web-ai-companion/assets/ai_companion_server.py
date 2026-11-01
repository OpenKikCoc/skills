#!/usr/bin/env python3
"""Local bridge for this page's in-page "AI Companion" panel.

Serves the folder this script sits in as static files (so it can serve the
page itself) and bridges questions typed into the panel to a local CLI coding
assistant -- `claude` (Claude Code) or `copilot` (GitHub Copilot CLI) --
keeping one continuous session so follow-up questions retain context.

Usage:
    python3 ai_companion_server.py [--port 8787] [--repo PATH] [--backend auto|claude|copilot]
    (Windows: python ai_companion_server.py ...)

--repo points the assistant at the *actual codebase* the page is about (its
answers are only as good as what it can read there). It defaults to this
script's own folder -- pass --repo explicitly once you copy this next to a
real checkout, e.g.:
    python3 ai_companion_server.py --repo /path/to/some/repo

Both the codebase path and (on Windows) the copilot executable can also be
set later from the page itself via the Settings view -- no need to restart
the server. Settings persist in .ai-companion-config.json.

Security: binds to 127.0.0.1 only, rejects non-localhost Origins, and serves
ONLY this script's folder over HTTP -- never --repo, which may be a real
codebase you don't want exposed even to localhost.
"""
import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

SCRIPT_DIR = Path(__file__).resolve().parent
SESSION_FILE = SCRIPT_DIR / ".ai-companion-session.json"
CONFIG_FILE = SCRIPT_DIR / ".ai-companion-config.json"
LOCK = threading.Lock()
STATE = {"session_id": None, "log": []}
try:
    STATE.update(json.loads(SESSION_FILE.read_text(encoding="utf-8")))
except (OSError, json.JSONDecodeError):
    pass

CONFIG = {"overrides": {}}  # e.g. {"copilot": "C:\\tools\\copilot\\copilot.cmd"}
try:
    CONFIG.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
except (OSError, json.JSONDecodeError):
    pass

# The one CLI call in flight, if any -- lets a separate request thread cancel
# it (see /api/cancel) without the two needing to coordinate through LOCK.
CURRENT = {"proc": None, "cancel_requested": False}

# Bump on every meaningful change and check it in /api/health / the page's
# Settings view -- this script gets copied to other machines by hand, so "am
# I actually running the latest one" needs to be answerable at a glance.
BRIDGE_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# CUSTOMIZE ME: name your page/product and what "the codebase" means here.
# This is the only piece of the server most integrations need to touch.
# --------------------------------------------------------------------------
SYSTEM_HINT = (
    "You are a helper embedded in this web page, and you can also read the "
    "source of the codebase this session was pointed at, if any. Prefer a "
    "conclusion up front, and cite concrete file paths when discussing code. "
    "Questions may be prefixed with a \"[Currently viewing ...]\" context "
    "block describing what's currently on screen -- ground your answer in "
    "that instead of answering generically."
)


class Cancelled(Exception):
    pass


def save_state():
    try:
        SESSION_FILE.write_text(json.dumps(STATE, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def save_config():
    try:
        CONFIG_FILE.write_text(json.dumps(CONFIG, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def is_windows():
    return platform.system() == "Windows"


def resolve_backend(preferred, overrides):
    """Return (name, resolved_path) for the first available CLI, honoring
    `preferred` if given. `overrides` maps backend name -> explicit
    executable path (from --copilot-path or the page's Settings view),
    which wins over PATH lookup. Order favors `claude` everywhere except
    when the caller asked for copilot explicitly -- machines that have only
    one of the two CLIs installed still work via --backend auto because
    the missing one simply won't resolve."""
    order = [preferred] if preferred and preferred != "auto" else ["claude", "copilot"]
    for name in order:
        override = (overrides or {}).get(name)
        if override:
            if Path(override).exists():
                return name, override
            continue  # configured path is stale/wrong -- don't silently fall through to PATH
        path = shutil.which(name)
        if path:
            return name, path
    return None, None


def copilot_candidates():
    """Known places `copilot` tends to install to, beyond plain PATH lookup
    -- shown as one-click choices in Settings before falling back to a
    manual browse. Best-effort; only returns paths that actually exist."""
    found = []
    which = shutil.which("copilot")
    if which:
        found.append(which)
    home = Path.home()
    if is_windows():
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        guesses = [appdata / "npm" / "copilot.cmd", appdata / "npm" / "copilot.ps1"]
    else:
        guesses = [
            Path("/usr/local/bin/copilot"),
            Path("/opt/homebrew/bin/copilot"),
            home / ".npm-global" / "bin" / "copilot",
            home / "node_modules" / ".bin" / "copilot",
        ]
    for g in guesses:
        gs = str(g)
        if g.exists() and gs not in found:
            found.append(gs)
    return found


def native_picker(kind):
    """Pop a native OS file/folder picker and return the chosen path, or
    None if unavailable/cancelled. A browser <input type=file> can't hand
    back a real filesystem path (by design), so for a localhost tool like
    this the picker has to be server-side."""
    system = platform.system()
    try:
        if system == "Darwin":
            prompt = "Select the executable" if kind == "file" else "Select the repository folder"
            script = f'POSIX path of (choose {"file" if kind == "file" else "folder"} with prompt "{prompt}")'
            proc = subprocess.run(
                ["osascript", "-e", 'tell application "Finder" to activate', "-e", script],
                capture_output=True, text=True, timeout=180,
            )
        elif system == "Windows":
            if kind == "file":
                ps = (
                    "Add-Type -AssemblyName System.Windows.Forms;"
                    "$f = New-Object System.Windows.Forms.OpenFileDialog;"
                    "$f.Filter = 'Executables (*.exe;*.cmd;*.bat;*.ps1)|*.exe;*.cmd;*.bat;*.ps1|All files (*.*)|*.*';"
                    "$f.Title = 'Select the executable';"
                    "if ($f.ShowDialog() -eq 'OK') { Write-Output $f.FileName }"
                )
            else:
                ps = (
                    "Add-Type -AssemblyName System.Windows.Forms;"
                    "$f = New-Object System.Windows.Forms.FolderBrowserDialog;"
                    "$f.Description = 'Select the repository folder';"
                    "if ($f.ShowDialog() -eq 'OK') { Write-Output $f.SelectedPath }"
                )
            proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                                   capture_output=True, text=True, timeout=180)
        else:  # Linux best-effort
            args = ["zenity", "--file-selection", "--title=Select"]
            if kind == "dir":
                args.append("--directory")
            proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
        path = (proc.stdout or "").strip()
        return path or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def run_cli(exe_path, args, timeout=600):
    """Popen-based (rather than subprocess.run) so a concurrent /api/cancel
    request can terminate an in-flight call. Also copes with Windows'
    npm-installed CLIs, which resolve to a `.cmd`/`.ps1` shim that
    CreateProcess cannot exec directly -- route through cmd.exe explicitly."""
    cmd = ["cmd", "/c", exe_path] + args if is_windows() else [exe_path] + args
    proc = subprocess.Popen(cmd, cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    CURRENT["proc"] = proc
    CURRENT["cancel_requested"] = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    finally:
        cancelled = CURRENT["cancel_requested"]
        CURRENT["proc"] = None
    if cancelled:
        raise Cancelled()
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def ask_claude(exe_path, context_block, question, resume_id):
    prompt = context_block + "User question: " + question
    cmd_args = ["-p", prompt, "--output-format", "json", "--append-system-prompt", SYSTEM_HINT]
    if resume_id:
        cmd_args += ["--resume", resume_id]
    proc = run_cli(exe_path, cmd_args)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "claude failed").strip()[:2000])
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout.strip(), resume_id
    if data.get("is_error"):
        raise RuntimeError(data.get("result") or "claude returned an error")
    return data.get("result", ""), (data.get("session_id") or resume_id)


_COPILOT_RESUME_RE = re.compile(r'--resume[= ]([0-9a-fA-F-]{8,})')
_COPILOT_FOOTER_RE = re.compile(r'^(Changes|AI Credits|Tokens|Resume)\b', re.MULTILINE)


def _parse_copilot_output(stdout, prev_session_id):
    """Docs describe `--output-format json` as JSONL, but what's actually
    been observed in the wild (confirmed against a real run) is plain text
    followed by a metadata footer:

        <answer text>

        Changes    +0 -0
        AI Credits 36.1 (41s)
        Tokens     ...
        Resume     copilot --resume=<uuid>

    Try JSONL first in case some version genuinely emits it, then fall back
    to stripping that footer off the raw text -- and pull the session id out
    of the "Resume" line, which is far more reliable in practice than
    hoping a session_id field shows up somewhere in JSON."""
    session_id = prev_session_id
    text_parts = []
    saw_json = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        saw_json = True
        sid = obj.get("session_id") or obj.get("sessionId")
        if sid:
            session_id = sid
        for key in ("text", "content", "message", "delta"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                text_parts.append(val)
    if saw_json and text_parts:
        return "".join(text_parts).strip(), session_id

    m = _COPILOT_RESUME_RE.search(stdout)
    if m:
        session_id = m.group(1)
    footer = _COPILOT_FOOTER_RE.search(stdout)
    body = (stdout[:footer.start()] if footer else stdout).strip()
    return body, session_id


def ask_copilot(exe_path, context_block, question, resume_id):
    # Real headless support confirmed via `copilot --help` docs (GA Feb 2026):
    # -p/--prompt (non-interactive), --resume, --output-format json,
    # --add-dir (repo scope), --allow-all-tools + --no-ask-user (unattended).
    # There is no confirmed --append-system-prompt equivalent, so the hint is
    # folded into the prompt text itself instead of a separate flag.
    #
    # KNOWN ISSUE: -p/--resume on Windows has several open upstream bugs --
    # silent empty output on some builds (github/copilot-cli#1164, #1181) and
    # "--resume: no supported shell detected" (github/copilot-cli#3458). If a
    # resumed call fails or comes back empty, we retry once *without*
    # --resume (fresh session) rather than failing outright -- most of the
    # time that's exactly the broken part, and a fresh answer beats none.
    #
    # Note: with the context block and question just
    # concatenated together, copilot reliably answered with only a
    # "ready to help" acknowledgment instead of the actual answer, on every
    # turn -- it was reading the whole thing as scene-setting, not a task.
    # Making the instruction to actually answer explicit and putting the
    # question in its own clearly-labeled section fixed that in practice.
    full_prompt = (
        SYSTEM_HINT + "\n\n" +
        context_block +
        "Answer the question below right now, directly and completely, grounded in "
        "the context above. Your entire response must be the answer itself -- do not "
        "reply with only an acknowledgment, a summary of what you can help with, or a "
        "statement that you are ready.\n\n"
        "Question: " + question
    )

    def attempt(with_resume):
        cmd_args = ["-p", full_prompt, "-s", "--output-format", "json",
                    "--add-dir", str(REPO), "--allow-all-tools", "--no-ask-user"]
        if with_resume and resume_id:
            cmd_args += ["--resume", resume_id]
        return run_cli(exe_path, cmd_args)

    used_resume = bool(resume_id)
    proc = attempt(with_resume=True)

    if proc.returncode != 0 and used_resume:
        print(f"[copilot] --resume failed (exit {proc.returncode}): "
              f"{(proc.stderr or proc.stdout or '').strip()[:500]!r} -- retrying without --resume",
              file=sys.stderr)
        proc = attempt(with_resume=False)
        used_resume = False

    if proc.returncode != 0:
        raise RuntimeError(
            f"copilot exited {proc.returncode}.\nstderr: {(proc.stderr or '').strip()[:800]}\n"
            f"stdout: {(proc.stdout or '').strip()[:800]}"
        )

    text, session_id = _parse_copilot_output(proc.stdout, resume_id if used_resume else None)

    if not text and used_resume:
        print("[copilot] --resume produced no parseable output -- retrying without --resume",
              file=sys.stderr)
        proc = attempt(with_resume=False)
        used_resume = False
        if proc.returncode != 0:
            raise RuntimeError(
                f"copilot (retry without --resume) exited {proc.returncode}.\n"
                f"stderr: {(proc.stderr or '').strip()[:800]}\nstdout: {(proc.stdout or '').strip()[:800]}"
            )
        text, session_id = _parse_copilot_output(proc.stdout, None)

    if not text:
        raise RuntimeError(
            "copilot produced no parseable output even without --resume.\n"
            f"Raw stdout: {(proc.stdout or '').strip()[:800]}\nstderr: {(proc.stderr or '').strip()[:800]}\n"
            "Try `copilot -p \"hello\"` by hand to confirm the CLI works outside this bridge, "
            "or pin an older @github/copilot version (see github/copilot-cli issues #1164, #1181, #3458)."
        )
    return text, (session_id or (resume_id if used_resume else None))


ASKERS = {"claude": ask_claude, "copilot": ask_copilot}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SCRIPT_DIR), **kwargs)

    def end_headers(self):
        # This script gets redeployed as it's updated; a cached copy of the
        # page (or a stale API response) silently serving old JS/behavior is
        # a worse failure mode than just never caching.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        return origin is None or origin.startswith(("http://127.0.0.1", "http://localhost"))

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self):
        if self.path == "/api/health":
            return self._json(200, {
                "ok": BACKEND_NAME is not None,
                "backend": BACKEND_NAME,
                "backend_path": BACKEND_PATH,
                "repo": str(REPO),
                "session_id": STATE["session_id"],
                "turns": len(STATE["log"]),
                "version": BRIDGE_VERSION,
            })
        if self.path == "/api/history":
            return self._json(200, {"session_id": STATE["session_id"], "log": STATE["log"]})
        if self.path == "/api/settings":
            return self._json(200, {
                "overrides": CONFIG.get("overrides", {}),
                "backend": BACKEND_NAME,
                "backend_path": BACKEND_PATH,
                "repo": str(REPO),
            })
        if self.path == "/api/copilot-candidates":
            return self._json(200, {"candidates": copilot_candidates()})
        return super().do_GET()

    def do_POST(self):
        if not self._origin_ok():
            return self._json(403, {"error": "forbidden origin"})
        if self.path == "/api/reset":
            STATE["session_id"] = None
            STATE["log"] = []
            save_state()
            return self._json(200, {"ok": True})
        if self.path == "/api/settings":
            return self._handle_settings()
        if self.path == "/api/browse":
            return self._handle_browse()
        if self.path == "/api/cancel":
            proc = CURRENT["proc"]
            if proc and proc.poll() is None:
                CURRENT["cancel_requested"] = True
                proc.terminate()
                return self._json(200, {"ok": True, "cancelled": True})
            return self._json(200, {"ok": True, "cancelled": False})
        if self.path != "/api/ask":
            return self._json(404, {"error": "not found"})

        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError):
            body = {}
        question = (body.get("question") or "").strip()
        if not question:
            return self._json(400, {"error": "empty question"})
        if BACKEND_NAME is None:
            return self._json(500, {"error": "no local AI CLI found (tried claude, copilot)"})

        context_title = (body.get("context_title") or "").strip()
        context_text = (body.get("context_text") or "").strip()
        context_block = ""
        if context_text:
            idx = body.get("context_index")
            total = body.get("context_count")
            where = f"item {idx}/{total}" if idx and total else "the current view"
            label = f": {context_title}" if context_title else ""
            context_block = (
                f"[Currently viewing {where}{label}]\n"
                f"Visible content:\n{context_text}\n\n"
            )
        with LOCK:  # single session -- serialize turns
            try:
                result, new_session = ASKERS[BACKEND_NAME](BACKEND_PATH, context_block, question, STATE["session_id"])
            except Cancelled:
                return self._json(200, {"cancelled": True})
            except subprocess.TimeoutExpired:
                return self._json(504, {"error": f"{BACKEND_NAME} timed out (600s)"})
            except RuntimeError as e:
                return self._json(500, {"error": str(e)})
            STATE["session_id"] = new_session
            STATE["log"].append({"q": question, "a": result, "context": context_title, "ts": int(time.time())})
            save_state()
            return self._json(200, {"result": result, "session_id": STATE["session_id"]})

    def _handle_settings(self):
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "invalid JSON body"})

        global BACKEND_NAME, BACKEND_PATH, REPO
        with LOCK:
            if "copilot_path" in body:
                raw = (body.get("copilot_path") or "").strip()
                overrides = CONFIG.setdefault("overrides", {})
                if raw:
                    if not Path(raw).exists():
                        return self._json(400, {"error": f"path does not exist: {raw}"})
                    overrides["copilot"] = raw
                else:
                    overrides.pop("copilot", None)  # blank clears it -> back to PATH auto-detect
                save_config()
                BACKEND_NAME, BACKEND_PATH = resolve_backend(REQUESTED_BACKEND, CONFIG.get("overrides", {}))

            repo_changed = False
            if "repo_path" in body:
                raw = (body.get("repo_path") or "").strip()
                if raw:
                    p = Path(raw)
                    if not p.is_dir():
                        return self._json(400, {"error": f"not a directory: {raw}"})
                    if p.resolve() != REPO:
                        REPO = p.resolve()
                        repo_changed = True

            if repo_changed:
                # The AI's whole picture of "the codebase" just changed --
                # keeping the old session around would silently mix contexts.
                STATE["session_id"] = None
                STATE["log"] = []
                save_state()

        return self._json(200, {
            "ok": True,
            "backend": BACKEND_NAME,
            "backend_path": BACKEND_PATH,
            "overrides": CONFIG.get("overrides", {}),
            "repo": str(REPO),
            "repo_changed": repo_changed,
        })

    def _handle_browse(self):
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError):
            body = {}
        kind = "dir" if body.get("kind") == "dir" else "file"
        path = native_picker(kind)
        return self._json(200, {"path": path})

    def log_message(self, fmt, *args):
        line = args[0] if args else ""
        if isinstance(line, str) and "/api/" in line:
            super().log_message(fmt, *args)


def main():
    global REPO, BACKEND_NAME, BACKEND_PATH, REQUESTED_BACKEND
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--repo", default=None,
                         help="Codebase directory the AI should read (cwd for the CLI). "
                              "Defaults to this script's own folder. Changeable later from "
                              "the page's Settings view too.")
    parser.add_argument("--backend", choices=["auto", "claude", "copilot"], default="auto")
    parser.add_argument("--copilot-path", default=None,
                         help="Explicit path to a copilot executable, e.g. a custom install "
                              "not on PATH. Persists (and can also be set/cleared later from "
                              "the page's Settings view) in .ai-companion-config.json.")
    args = parser.parse_args()

    REPO = Path(args.repo).resolve() if args.repo else SCRIPT_DIR
    if not REPO.is_dir():
        sys.exit(f"error: --repo path does not exist or is not a directory: {REPO}")

    if args.copilot_path:
        if not Path(args.copilot_path).exists():
            sys.exit(f"error: --copilot-path does not exist: {args.copilot_path}")
        CONFIG.setdefault("overrides", {})["copilot"] = args.copilot_path
        save_config()

    REQUESTED_BACKEND = args.backend
    BACKEND_NAME, BACKEND_PATH = resolve_backend(REQUESTED_BACKEND, CONFIG.get("overrides", {}))
    if BACKEND_NAME is None:
        print("warning: neither `claude` nor `copilot` was found -- "
              "questions will fail until one is installed, or its path is set "
              "via --copilot-path / the page's Settings view.", file=sys.stderr)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"AI Companion bridge server {BRIDGE_VERSION} -- backend: {BACKEND_NAME or '(none found)'}"
          f"{' (' + BACKEND_PATH + ')' if BACKEND_PATH else ''}, repo: {REPO}")
    for html in sorted(SCRIPT_DIR.glob("*.html")):
        print(f"  open: http://127.0.0.1:{args.port}/{quote(html.name)}")
    print("Ctrl+C to quit.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
