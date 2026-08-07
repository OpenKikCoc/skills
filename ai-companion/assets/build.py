#!/usr/bin/env python3
"""Merge ai-companion.html + ai_companion_server.py into one paste-ready
fragment, and optionally insert it straight into a target page.

    python3 build.py                          -> writes ai-companion.fragment.html
    python3 build.py --into path/to/page.html -> inserts/replaces in place, before </body>

Customize ai_companion_server.py's SYSTEM_HINT (and this folder's copy of
ai-companion.html, if you want different branding/behavior) *before*
running this -- it embeds whatever's on disk right now.

Re-running with --into is idempotent: it finds the markers left by the
previous insertion and replaces that span, instead of duplicating the panel
on every rebuild.
"""
import argparse
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
TEMPLATE = HERE / "ai-companion.html"
SERVER = HERE / "ai_companion_server.py"
START = "<!-- ai-companion:start -->"
END = "<!-- ai-companion:end -->"


def build_fragment():
    if not TEMPLATE.exists():
        sys.exit(f"error: {TEMPLATE.name} not found next to build.py")
    if not SERVER.exists():
        sys.exit(f"error: {SERVER.name} not found next to build.py")
    template = TEMPLATE.read_text(encoding="utf-8")
    server = SERVER.read_text(encoding="utf-8")
    if "___BRIDGE_SOURCE___" not in template:
        sys.exit(f"error: {TEMPLATE.name} has no ___BRIDGE_SOURCE___ placeholder")
    if "</script" in server.lower():
        sys.exit(f"error: {SERVER.name} contains a literal '</script' sequence -- "
                 "would break out of the embedding <script type=text/plain> block")
    merged = template.replace("___BRIDGE_SOURCE___", server)
    return START + "\n" + merged.strip() + "\n" + END + "\n"


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--into", metavar="PAGE_HTML",
                         help="insert/replace in this file, right before </body>")
    parser.add_argument("-o", "--output", default=None,
                         help="output path when not using --into "
                              "(default: ai-companion.fragment.html next to this script)")
    args = parser.parse_args()

    fragment = build_fragment()

    if args.into:
        target = pathlib.Path(args.into)
        if not target.exists():
            sys.exit(f"error: {target} does not exist")
        data = target.read_text(encoding="utf-8")
        if START in data and END in data:
            pre = data[:data.index(START)]
            post = data[data.index(END) + len(END):]
            new_data = pre + fragment + post
            action = "replaced existing"
        else:
            idx = data.rfind("</body>")
            if idx == -1:
                sys.exit(f"error: no </body> found in {target}")
            new_data = data[:idx] + fragment + data[idx:]
            action = "inserted"
        target.write_text(new_data, encoding="utf-8")
        print(f"{action} ai-companion panel in {target} ({len(new_data)} bytes total)")
    else:
        out = pathlib.Path(args.output) if args.output else HERE / "ai-companion.fragment.html"
        out.write_text(fragment, encoding="utf-8")
        print(f"wrote {out} ({len(fragment)} bytes) -- paste its contents before </body> in your page")


if __name__ == "__main__":
    main()
