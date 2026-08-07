#!/usr/bin/env python3
"""Self-check for the ai-companion skill. Python 3 stdlib only.

    python3 verify.py

Builds the fragment, inserts it into a throwaway minimal HTML page (deliberately
carrying none of live-deck's --chrome-ink/--surround-2/etc custom properties,
to prove the widget doesn't secretly depend on a host page's styling), boots
the bridge server against a temp directory, and exercises everything that can
be exercised without a human:

  1. Python is new enough.
  2. assets/ai_companion_server.py has valid syntax.
  3. assets/ai-companion.html has the placeholder, balanced <script> tags,
     no stray literal '</script' inside content that will be embedded raw.
  4. build.py merges them and can insert into (and idempotently replace
     within) a target page.
  5. The server boots and serves the page over HTTP.
  6. /api/health, /api/settings, /api/copilot-candidates respond with the
     expected shape.
  7. /api/ask against a real `claude`/`copilot` if one is on PATH --
     SKIPPED, not failed, if neither is installed (most dev machines running
     this check won't have one).
  8. /api/browse is wired up (checked by source inspection, not by actually
     invoking it -- it pops a real native OS dialog and would hang an
     unattended run).

Exit code 0 = all checks passed (skips allowed), 1 = something failed.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

MIN_PY = (3, 8)
HERE = os.path.dirname(os.path.abspath(__file__))

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results = []


def report(status, label, detail=""):
    results.append((status, label))
    mark = {PASS: "ok  ", FAIL: "FAIL", SKIP: "skip"}[status]
    print(f"  [{mark}] {label}" + (f" -- {detail}" if detail else ""))


def http_get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def http_post(url, payload, timeout=60):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


TEST_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>ai-companion verify page</title></head>
<body>
<h1>Plain page, zero shared CSS variables</h1>
<p>This page defines none of live-deck's --chrome-ink/--surround-2/etc tokens
on purpose, to prove the widget's fallbacks are load-bearing, not decorative.</p>
</body></html>
"""


def main():
    print(f"ai-companion verify -- {HERE}\n")

    if sys.version_info < MIN_PY:
        report(FAIL, "python version", f"need >= {'.'.join(map(str, MIN_PY))}")
        return 1
    report(PASS, "python version", ".".join(map(str, sys.version_info[:3])))

    server_py = os.path.join(HERE, "ai_companion_server.py")
    fragment_html = os.path.join(HERE, "ai-companion.html")
    build_py = os.path.join(HERE, "build.py")
    for name, path in (("ai_companion_server.py", server_py),
                        ("ai-companion.html", fragment_html),
                        ("build.py", build_py)):
        report(PASS if os.path.exists(path) else FAIL, f"{name} present")
    if not all(os.path.exists(p) for p in (server_py, fragment_html, build_py)):
        return 1

    proc = subprocess.run([sys.executable, "-m", "py_compile", server_py],
                          capture_output=True, text=True)
    report(PASS if proc.returncode == 0 else FAIL, "ai_companion_server.py syntax",
           proc.stderr.strip()[:200] if proc.returncode else "")

    frag = open(fragment_html, encoding="utf-8").read()
    report(PASS if "___BRIDGE_SOURCE___" in frag else FAIL, "fragment has bridge placeholder")
    opens, closes = frag.count("<script"), frag.count("</script>")
    report(PASS if opens == closes and opens > 0 else FAIL, "fragment <script> tags balanced",
           f"{opens} open, {closes} close")
    server_src = open(server_py, encoding="utf-8").read()
    report(FAIL if "</script" in server_src.lower() else PASS,
           "server source has no literal '</script'",
           "would break out of the text/plain embedding" if "</script" in server_src.lower() else "")

    for route in ("/api/health", "/api/ask", "/api/reset", "/api/history",
                  "/api/settings", "/api/browse", "/api/cancel", "/api/copilot-candidates"):
        report(PASS if route in server_src else FAIL, f"server implements {route}")

    tmp = tempfile.mkdtemp(prefix="ai-companion-verify-")
    page_path = os.path.join(tmp, "index.html")
    with open(page_path, "w", encoding="utf-8") as f:
        f.write(TEST_PAGE)

    proc = subprocess.run([sys.executable, build_py, "--into", page_path],
                          capture_output=True, text=True)
    report(PASS if proc.returncode == 0 else FAIL, "build.py --into inserts the panel",
           proc.stderr.strip()[:200] if proc.returncode else "")
    inserted = open(page_path, encoding="utf-8").read()
    report(PASS if "aic-toggle" in inserted else FAIL, "inserted page contains the panel markup")

    proc = subprocess.run([sys.executable, build_py, "--into", page_path],
                          capture_output=True, text=True)
    reinserted = open(page_path, encoding="utf-8").read()
    report(PASS if reinserted.count("aic-toggle") == inserted.count("aic-toggle") else FAIL,
           "re-running build.py --into is idempotent (no duplicate panel)")

    shutil.copy(server_py, os.path.join(tmp, "ai_companion_server.py"))
    port = 8799
    env = dict(os.environ)
    server_proc = subprocess.Popen(
        [sys.executable, "ai_companion_server.py", "--port", str(port)],
        cwd=tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        booted = False
        for _ in range(30):
            try:
                status, health = http_get(base + "/api/health", timeout=1)
                booted = True
                break
            except Exception:
                time.sleep(0.2)
        report(PASS if booted else FAIL, "server boots and answers /api/health")
        if not booted:
            return _finish()

        needed = {"ok", "backend", "backend_path", "repo", "session_id", "turns", "version"}
        report(PASS if needed.issubset(health) else FAIL, "/api/health has the documented shape",
               "" if needed.issubset(health) else f"missing {needed - set(health)}")

        status, page = 0, ""
        try:
            with urllib.request.urlopen(base + "/index.html", timeout=5) as r:
                status, page = r.status, r.read().decode("utf-8")
        except Exception as e:
            status = str(e)
        report(PASS if status == 200 and "aic-toggle" in page else FAIL,
               "static file serving works", f"status={status}")

        status, settings = http_get(base + "/api/settings")
        report(PASS if status == 200 and "overrides" in settings else FAIL, "/api/settings responds")

        status, candidates = http_get(base + "/api/copilot-candidates")
        report(PASS if status == 200 and "candidates" in candidates else FAIL,
               "/api/copilot-candidates responds")

        status, empty_ask = http_post(base + "/api/ask", {"question": ""})
        report(PASS if status == 400 else FAIL, "/api/ask rejects an empty question", f"status={status}")

        backend = shutil.which("claude") or shutil.which("copilot")
        if not backend:
            report(SKIP, "/api/ask against a real backend", "no claude/copilot on PATH")
        else:
            status, ask = http_post(base + "/api/ask",
                                     {"question": "Reply with exactly: verify-ok"}, timeout=90)
            ok = status == 200 and bool((ask or {}).get("result"))
            report(PASS if ok else FAIL, "/api/ask against a real backend",
                   f"status={status}" if not ok else (ask.get("result", "")[:80]))

        report(SKIP, "/api/browse native dialog", "pops a real OS dialog -- not run unattended")
    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()

    return _finish()


def _finish():
    failed = [l for s, l in results if s == FAIL]
    skipped = [l for s, l in results if s == SKIP]
    print()
    if failed:
        print(f"FAILED: {len(failed)} check(s) -- " + "; ".join(failed))
        return 1
    print("All checks passed" + (f" ({len(skipped)} skipped)" if skipped else "") + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
