#!/usr/bin/env python3
"""Build a single-file HTML from a page + figure fragments. Python 3.8+, stdlib only.

    python3 inline_figures.py PAGE.html -o OUT.html [--figures DIR] [--css FILE] [--js FILE]

  - every <!--FIG:name--> is replaced by the contents of <figures>/name.html
    (default figures dir: "figures" next to PAGE.html). Unknown name = error,
    nothing is written. Figure files that are never referenced = warning.
  - <link rel="stylesheet" href="...diag.css"> and <script src="...diag.js"></script>
    are replaced by inline <style>/<script> so OUT.html needs no other files.
    The href/src is resolved next to PAGE.html first, then in this skill's assets/.
Exit code 0 = written, 1 = error.
"""
import argparse
import os
import re
import sys

FIG_RE = re.compile(r"<!--\s*FIG:([A-Za-z0-9_.-]+)\s*-->")
LINK_RE = re.compile(r'<link\b[^>]*href="([^"]*diag\.css)"[^>]*>', re.I)
SCRIPT_RE = re.compile(r'<script\b[^>]*src="([^"]*diag\.js)"[^>]*>\s*</script>', re.I)
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "assets")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def find_asset(ref, base_dir, override):
    for cand in ([override] if override else []) + [os.path.join(base_dir, ref),
                                                    os.path.join(ASSETS, os.path.basename(ref))]:
        if cand and os.path.isfile(cand):
            return cand
    return None


def inline(text, base_dir, figures_dir, css=None, js=None):
    """Return (output, errors, warnings)."""
    errors, warnings = [], []
    used = set()

    def fig(m):
        name = m.group(1)
        path = os.path.join(figures_dir, name + ".html")
        if not os.path.isfile(path):
            errors.append("unknown figure %r (expected %s)" % (name, path))
            return m.group(0)
        used.add(name)
        return read(path).rstrip("\n")

    out = FIG_RE.sub(fig, text)
    if os.path.isdir(figures_dir):
        for f in sorted(os.listdir(figures_dir)):
            if f.endswith(".html") and f[:-5] not in used:
                warnings.append("figure %r is never referenced" % f[:-5])

    def asset(regex, wrap, override, label):
        nonlocal out
        m = regex.search(out)
        if not m:
            warnings.append("no %s reference found; nothing inlined for it" % label)
            return
        path = find_asset(m.group(1), base_dir, override)
        if not path:
            errors.append("cannot find %s (%s)" % (label, m.group(1)))
            return
        body = read(path)
        out = out[:m.start()] + wrap(body) + out[m.end():]

    asset(LINK_RE, lambda b: "<style>\n%s\n</style>" % b, css, "diag.css")
    asset(SCRIPT_RE, lambda b: "<script>\n%s\n</script>" % b.replace("</script", "<\\/script"), js, "diag.js")
    return out, errors, warnings


def main(argv=None):
    ap = argparse.ArgumentParser(description="Inline <!--FIG:name--> placeholders, diag.css and diag.js into one HTML file.")
    ap.add_argument("page", metavar="PAGE.html")
    ap.add_argument("-o", "--output", required=True, metavar="OUT.html")
    ap.add_argument("--figures", help="figures directory (default: figures/ next to PAGE.html)")
    ap.add_argument("--css", help="diag.css to inline (default: resolved from the page, then assets/)")
    ap.add_argument("--js", help="diag.js to inline (default: resolved from the page, then assets/)")
    args = ap.parse_args(argv)
    try:
        text = read(args.page)
    except (OSError, UnicodeDecodeError) as e:
        print("error: cannot read %s (%s)" % (args.page, e), file=sys.stderr)
        return 1
    base = os.path.dirname(os.path.abspath(args.page))
    figures = args.figures or os.path.join(base, "figures")
    out, errors, warnings = inline(text, base, figures, args.css, args.js)
    for w in warnings:
        print("warning: " + w, file=sys.stderr)
    if errors:
        for e in errors:
            print("error: " + e, file=sys.stderr)
        return 1
    try:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out)
    except OSError as e:
        print("error: cannot write %s (%s)" % (args.output, e), file=sys.stderr)
        return 1
    print("wrote %s (%d bytes)" % (args.output, len(out.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
