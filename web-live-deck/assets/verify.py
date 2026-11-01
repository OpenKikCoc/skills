#!/usr/bin/env python3
"""Self-check for a web-live-deck deck. Python 3 stdlib only, nothing to install.

    python3 verify.py [deck-dir]

Runs the whole pipeline and reports what a broken deck would fail on:

  1. Python is new enough.
  2. The shell and every slide it lists are present.
  3. The shell has one <iframe class="slide"> per slide (the bundler contract).
  4. No slide reaches the network -- decks must stay self-contained.
  5. Every slide carries the deck bridge.
  6. The bundler produces a bundle that contains every slide.
  7. A headless browser renders that bundle to a non-blank page.

Step 7 needs Chrome, Chromium or Edge. It is looked up in this order:
  $LIVE_DECK_BROWSER, $CHROME, $BROWSER, PATH, then the usual install paths
  for macOS / Linux / Windows. If none is found the step is SKIPPED, not
  failed -- everything else still verifies on a machine with no browser.

Exit code 0 = all checks passed (skips allowed), 1 = something failed.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

MIN_PY = (3, 8)

BROWSER_ENV = ("LIVE_DECK_BROWSER", "CHROME", "CHROME_PATH", "BROWSER")

BROWSER_CMDS = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "chrome", "microsoft-edge", "msedge",
)

BROWSER_PATHS = {
    "darwin": (
        # one candidate in a platform lookup table, not a hardcoded dependency
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ),
    "linux": (
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/snap/bin/chromium", "/usr/bin/microsoft-edge",
    ),
    "win32": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ),
}

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results = []


def report(status, label, detail=""):
    results.append((status, label))
    mark = {PASS: "ok  ", FAIL: "FAIL", SKIP: "skip"}[status]
    print(f"  [{mark}] {label}" + (f" -- {detail}" if detail else ""))


def find_browser():
    for var in BROWSER_ENV:
        p = os.environ.get(var)
        if p and os.path.exists(p):
            return p
    for cmd in BROWSER_CMDS:
        p = shutil.which(cmd)
        if p:
            return p
    for p in BROWSER_PATHS.get(sys.platform, ()):
        if os.path.exists(p):
            return p
    return None


def find_shell(root):
    for name in ("index.html", "deck-shell.html"):
        p = os.path.join(root, name)
        if os.path.exists(p):
            return p
    return None


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    print(f"web-live-deck verify -- {root}\n")

    if sys.version_info < MIN_PY:
        report(FAIL, "python version", f"need >= {'.'.join(map(str, MIN_PY))}")
        return 1
    report(PASS, "python version", ".".join(map(str, sys.version_info[:3])))

    shell_path = find_shell(root)
    if not shell_path:
        report(FAIL, "shell present", "no index.html or deck-shell.html")
        return 1
    shell = open(shell_path, encoding="utf-8").read()
    report(PASS, "shell present", os.path.basename(shell_path))

    slides = re.findall(r"file:\s*'([^']+)'", shell)
    if not slides:
        report(FAIL, "slide list", "no SLIDES entries found in the shell")
        return 1
    report(PASS, "slide list", f"{len(slides)} slide(s)")

    missing = [s for s in slides if not os.path.exists(os.path.join(root, s))]
    report(FAIL if missing else PASS, "slide files exist",
           ", ".join(missing) if missing else "")

    # the bundler's own pattern -- a mention inside a comment must not count
    tags = len(re.findall(r'<iframe class="slide"[^>]*?></iframe>', shell))
    report(PASS if tags == len(slides) else FAIL, "iframe/slide counts match",
           f"{tags} iframes vs {len(slides)} slides")

    # self-containment + bridge, per slide
    net = re.compile(r'(?:src|href)="(?!#)(?!data:)[^"]+"|@import\s|https?://(?!www\.w3\.org)')
    offenders, nobridge = [], []
    for s in slides:
        p = os.path.join(root, s)
        if not os.path.exists(p):
            continue
        body = open(p, encoding="utf-8").read()
        if net.search(body):
            offenders.append(s)
        if "deck-bridge" not in body:
            nobridge.append(s)
    report(FAIL if offenders else PASS, "slides make no network requests",
           ", ".join(offenders))
    report(FAIL if nobridge else PASS, "slides carry the deck bridge",
           ", ".join(nobridge))

    # build
    builder = os.path.join(root, "build-single-file.py")
    if not os.path.exists(builder):
        report(SKIP, "bundle builds", "build-single-file.py not in this folder")
        bundle = None
    else:
        out = os.path.join(tempfile.mkdtemp(prefix="web-live-deck-"), "deck.html")
        proc = subprocess.run([sys.executable, builder, out],
                              cwd=root, capture_output=True, text=True)
        if proc.returncode != 0 or not os.path.exists(out):
            report(FAIL, "bundle builds", (proc.stderr or proc.stdout).strip()[:160])
            bundle = None
        else:
            blob = open(out, encoding="utf-8").read()
            payloads = len(re.findall(r'<iframe class="slide"[^>]*?\sdata-doc="', blob))
            ok = payloads == len(slides)
            report(PASS if ok else FAIL, "bundle contains every slide",
                   f"{payloads}/{len(slides)} payloads, {len(blob)//1024} kB")
            bundle = out if ok else None

    # render
    browser = find_browser()
    if not browser:
        report(SKIP, "bundle renders", "no Chrome/Chromium/Edge found; set $LIVE_DECK_BROWSER")
    elif not bundle:
        report(SKIP, "bundle renders", "no bundle to render")
    else:
        shot = os.path.join(os.path.dirname(bundle), "shot.png")
        url = "file://" + bundle.replace(os.sep, "/")
        if not url.startswith("file:///"):
            url = "file:///" + url[len("file://"):]
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--virtual-time-budget=7000", "--window-size=1600,950",
                        f"--screenshot={shot}", url],
                       capture_output=True, text=True, timeout=120)
        if not os.path.exists(shot):
            report(FAIL, "bundle renders", "browser produced no screenshot")
        else:
            size = os.path.getsize(shot)
            png = open(shot, "rb").read(8) == b"\x89PNG\r\n\x1a\n"
            # a blank page compresses to a few kB; a real slide does not
            ok = png and size > 20000
            report(PASS if ok else FAIL, "bundle renders",
                   f"{size//1024} kB png" + ("" if ok else " -- looks blank"))

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
