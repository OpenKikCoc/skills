#!/usr/bin/env python3
"""Verify web-diagram HTML files. Python 3.8+, standard library only.

    python3 verify_diagram.py FILE.html [FILE.html ...] [--no-render] [--out-dir DIR]

Static checks (errors):
  - every .diag sits in a .diag-wrap; data-w / data-h are positive numbers
    (sequence diagrams: data-h is computed at runtime, data-w optional)
  - edges / seqdef JSON parses and has the expected shape
  - every edge endpoint references an existing data-node; node ids are unique
  - data-kind / edge kind / participant kind are known
  - nodes have inline left/top/width/height and lie inside the stage
  - no two cards (.node) overlap (groups, lanes and free text are excluded)
Geometric checks (the routing in diag.js is ported here):
  - ERROR  an edge segment passes through a card that is not its endpoint
  - warn   an estimated edge-label box overlaps a card or another label
  - warn   sequence label longer than the lane gap it sits in
Render (optional): a headless Chromium screenshots each file to a PNG. If no
browser is found the step is skipped; it never fails the run.

Label size heuristic (no browser needed, deliberately rough):
  width  = chars * 12.5px * 0.56 + 20px padding   (CJK / wide chars count 12.5px each)
  height = 12.5px * 1.35 + 8px
Exit code 0 = no [FAIL] lines (warnings and skips allowed), 1 otherwise.
"""
import argparse
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from html.parser import HTMLParser

NODE_KINDS = {"primary", "compute", "store", "control", "external", "note", "ok", "bad", "muted"}
EDGE_KINDS = {"primary", "data", "outcome", "feedback", "bulk", "danger", "tree"}
SIDES = {"left", "right", "top", "bottom"}
CARD_CLASSES = {"node"}                    # overlap + crossing apply to these
BOX_CLASSES = {"node", "grp"}              # must lie inside the stage

LABEL_FONT = 12.5
CHAR_W = 0.56
LABEL_PAD_X = 20.0
LABEL_H = LABEL_FONT * 1.35 + 8
CROSS_TOL = 2.0       # px: edge may graze a card border by this much
OVERLAP_TOL = 1.0     # px

BROWSER_ENV = ("WEB_DIAGRAM_BROWSER",)
BROWSER_CMDS = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
                "chrome", "headless_shell", "microsoft-edge", "msedge")
BROWSER_GLOBS = (   # Playwright cache; the directory name differs between versions
    "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell",
    "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell",
    "~/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
    "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome",
)


# ---------------------------------------------------------------- parsing
class Diag:
    def __init__(self, classes, attrs, in_wrap):
        self.classes = classes
        self.attrs = attrs
        self.in_wrap = in_wrap
        self.is_seq = "seq" in classes
        self.elements = []      # dicts: classes, attrs, rect, id
        self.edges_text = None
        self.seq_text = None


class Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.diags = []
        self.stack = []          # one entry per open <div>: (is_wrap, is_diag)
        self.cur = None
        self.script_for = None   # "edges" | "seqdef"
        self.buf = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        if tag == "script" and self.cur is not None:
            if "edges" in classes:
                self.script_for = "edges"
            elif "seqdef" in classes:
                self.script_for = "seqdef"
            self.buf = []
            return
        if tag != "div":
            return
        is_wrap = "diag-wrap" in classes
        is_diag = "diag" in classes
        if is_diag and self.cur is None:
            self.cur = Diag(classes, a, any(w for w, _ in self.stack))
            self.diags.append(self.cur)
        elif self.cur is not None:
            interesting = classes & {"node", "grp", "txt", "lane"} or "data-node" in a
            if interesting:
                self.cur.elements.append({"classes": classes, "attrs": a,
                                          "rect": parse_rect(a.get("style", "")),
                                          "id": a.get("data-node")})
        self.stack.append((is_wrap, is_diag and self.cur is not None and
                           self.cur.attrs is a))

    def handle_endtag(self, tag):
        if tag == "script" and self.script_for:
            text = "".join(self.buf)
            if self.script_for == "edges":
                self.cur.edges_text = text
            else:
                self.cur.seq_text = text
            self.script_for = None
            return
        if tag == "div" and self.stack:
            _, owns = self.stack.pop()
            if owns:
                self.cur = None

    def handle_data(self, data):
        if self.script_for:
            self.buf.append(data)


def parse_rect(style):
    vals = {}
    for k, v in re.findall(r"([a-z-]+)\s*:\s*([^;]+)", style):
        m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)(?:px)?\s*", v)
        if m and k in ("left", "top", "width", "height"):
            vals[k] = float(m.group(1))
    if len(vals) == 4:
        return (vals["left"], vals["top"], vals["left"] + vals["width"], vals["top"] + vals["height"])
    return None


def num(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- routing port (diag.js)
def anchor(r, side, at):
    at = 0.5 if at is None else at
    l, t, rr, b = r
    w, h = rr - l, b - t
    if side == "left":
        return (l, t + h * at, True, -1)
    if side == "right":
        return (rr, t + h * at, True, 1)
    if side == "top":
        return (l + w * at, t, False, -1)
    return (l + w * at, b, False, 1)


def default_sides(a, b):
    dx = (b[0] + b[2]) / 2 - (a[0] + a[2]) / 2
    dy = (b[1] + b[3]) / 2 - (a[1] + a[3]) / 2
    if abs(dx) > abs(dy):
        return ("right", "left") if dx > 0 else ("left", "right")
    return ("bottom", "top") if dy > 0 else ("top", "bottom")


def route(p1, p2, out):
    out = out or 40
    x1, y1, h1, d1 = p1
    x2, y2, h2, d2 = p2
    pts = [(x1, y1)]
    if h1 and h2:
        mx = (max(x1, x2) + out if d1 > 0 else min(x1, x2) - out) if d1 == d2 else (x1 + x2) / 2
        pts += [(mx, y1), (mx, y2)]
    elif not h1 and not h2:
        my = (max(y1, y2) + out if d1 > 0 else min(y1, y2) - out) if d1 == d2 else (y1 + y2) / 2
        pts += [(x1, my), (x2, my)]
    elif h1 and not h2:
        pts.append((x2, y1))
    else:
        pts.append((x1, y2))
    pts.append((x2, y2))
    return pts


def midpoint(pts):
    segs = [abs(pts[i][0] - pts[i - 1][0]) + abs(pts[i][1] - pts[i - 1][1]) for i in range(1, len(pts))]
    half = sum(segs) / 2
    for j, s in enumerate(segs):
        if half <= s:
            t = half / s if s else 0
            return (pts[j][0] + (pts[j + 1][0] - pts[j][0]) * t,
                    pts[j][1] + (pts[j + 1][1] - pts[j][1]) * t)
        half -= s
    return pts[-1]


def seg_hits_rect(p, q, rect, tol):
    """Liang-Barsky: does segment p-q pass through the interior of rect shrunk by tol?"""
    xmin, ymin, xmax, ymax = rect[0] + tol, rect[1] + tol, rect[2] - tol, rect[3] - tol
    if xmin >= xmax or ymin >= ymax:
        return False
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for pp, qq in ((-dx, p[0] - xmin), (dx, xmax - p[0]), (-dy, p[1] - ymin), (dy, ymax - p[1])):
        if pp == 0:
            if qq <= 0:
                return False
        else:
            r = qq / pp
            if pp < 0:
                t0 = max(t0, r)
            else:
                t1 = min(t1, r)
            if t0 >= t1:
                return False
    return t1 - t0 > 1e-9


def overlap(a, b, tol=OVERLAP_TOL):
    return min(a[2], b[2]) - max(a[0], b[0]) > tol and min(a[3], b[3]) - max(a[1], b[1]) > tol


# ---------------------------------------------------------------- label estimate
def plain_text(s):
    return html.unescape(re.sub(r"<[^>]*>", "", str(s)))


def text_width(s):
    t = plain_text(s)
    return sum(LABEL_FONT if ord(c) >= 0x2E80 else LABEL_FONT * CHAR_W for c in t) + LABEL_PAD_X


# ---------------------------------------------------------------- checks
class Report:
    def __init__(self, name):
        self.name = name
        self.errors = []
        self.warnings = []
        self.info = []

    def err(self, where, msg):
        self.errors.append("%s: %s" % (where, msg))

    def warn(self, where, msg):
        self.warnings.append("%s: %s" % (where, msg))


def load_json(text, where, rep, what):
    try:
        return json.loads(text)
    except ValueError as e:
        rep.err(where, "%s JSON does not parse (%s)" % (what, e))
        return None


def check_box(d, where, rep):
    W, H = num(d.attrs.get("data-w")), num(d.attrs.get("data-h"))
    if W is None or H is None:
        rep.err(where, "data-w and data-h must be positive numbers (got %r, %r)"
                % (d.attrs.get("data-w"), d.attrs.get("data-h")))
        W = W or 1100
        H = H or 10 ** 6
    ids, rects = {}, {}
    cards = []
    for el in d.elements:
        cls = el["classes"]
        if el["id"] is not None:
            if el["id"] in ids:
                rep.err(where, "duplicate data-node %r" % el["id"])
            ids[el["id"]] = True
        if el["id"] is not None and not (cls & {"node", "grp", "txt", "lane"}):
            rep.warn(where, "data-node %r has no node/grp/txt/lane class" % el["id"])
        kind = el["attrs"].get("data-kind")
        if kind is not None and kind not in NODE_KINDS:
            rep.err(where, "unknown data-kind %r on %s (known: %s)"
                    % (kind, el["id"] or "a node", ", ".join(sorted(NODE_KINDS))))
        r = el["rect"]
        label = el["id"] or el["attrs"].get("data-label") or "/".join(sorted(cls))
        if cls & BOX_CLASSES:
            if r is None:
                rep.err(where, "%r needs inline left/top/width/height in px" % label)
                continue
            if r[0] < 0 or r[1] < 0 or r[2] > W or r[3] > H:
                rep.err(where, "%r (%g,%g)-(%g,%g) is outside the %gx%g stage"
                        % (label, r[0], r[1], r[2], r[3], W, H))
        if r is not None and el["id"] is not None:
            rects.setdefault(el["id"], r)
        if "node" in cls and r is not None:
            cards.append((label, r, el["id"]))
    for i in range(len(cards)):
        for j in range(i + 1, len(cards)):
            if overlap(cards[i][1], cards[j][1]):
                rep.err(where, "nodes %r and %r overlap" % (cards[i][0], cards[j][0]))

    edges = []
    if d.edges_text is not None:
        data = load_json(d.edges_text, where, rep, "edges")
        if data is not None and not isinstance(data, list):
            rep.err(where, "edges JSON must be a list")
        elif data:
            edges = data
    labels = []   # (text, rect, edge index)
    for n, e in enumerate(edges):
        ew = "%s edge #%d" % (where, n + 1)
        if not isinstance(e, dict):
            rep.err(ew, "edge must be an object")
            continue
        kind = e.get("kind", "primary")
        if kind not in EDGE_KINDS:
            rep.err(ew, "unknown edge kind %r (known: %s)" % (kind, ", ".join(sorted(EDGE_KINDS))))
        for key in ("fromSide", "toSide"):
            if key in e and e[key] not in SIDES:
                rep.err(ew, "%s must be one of left/right/top/bottom" % key)
        for key in ("fromAt", "toAt", "out", "dx", "dy"):
            if key in e and (isinstance(e[key], bool) or not isinstance(e[key], (int, float))):
                rep.err(ew, "%s must be a number" % key)
        pts = None
        if "points" in e:
            try:
                pts = [(float(x), float(y)) for x, y in e["points"]]
                if len(pts) < 2:
                    raise ValueError
            except (TypeError, ValueError):
                rep.err(ew, "points must be a list of at least two [x, y] pairs")
                continue
        ends = [e.get("from"), e.get("to")]
        bad = False
        for role, ref in (("from", e.get("from")), ("to", e.get("to"))):
            if ref is None:
                if pts is None:
                    rep.err(ew, "missing %r" % role)
                    bad = True
            elif ref not in ids:
                rep.err(ew, "%s references unknown node %r" % (role, ref))
                bad = True
        if bad:
            continue
        if pts is None:
            a, b = rects.get(e["from"]), rects.get(e["to"])
            if a is None or b is None:
                continue          # geometry already reported above
            sd = default_sides(a, b)
            if any(key in e and e[key] not in SIDES for key in ("fromSide", "toSide")):
                continue
            p1 = anchor(a, e.get("fromSide") or sd[0], e.get("fromAt"))
            p2 = anchor(b, e.get("toSide") or sd[1], e.get("toAt"))
            try:
                pts = route(p1, p2, e.get("out"))
            except TypeError:
                continue
        for i in range(1, len(pts)):
            for label, r, nid in cards:
                if nid in ends:
                    continue
                if seg_hits_rect(pts[i - 1], pts[i], r, CROSS_TOL):
                    rep.err(ew, "%s -> %s passes through node %r (route it with fromSide/toSide/out, "
                            "or drop the edge)" % (e.get("from", "?"), e.get("to", "?"), label))
        if e.get("label"):
            mx, my = midpoint(pts)
            try:
                mx += float(e.get("dx", 0))
                my += float(e.get("dy", 0))
            except (TypeError, ValueError):
                continue
            w = text_width(e["label"])
            labels.append((plain_text(e["label"]), (mx - w / 2, my - LABEL_H / 2, mx + w / 2, my + LABEL_H / 2), n + 1))
    for text, r, n in labels:
        for label, cr, _ in cards:
            if overlap(r, cr):
                rep.warn("%s edge #%d" % (where, n), "label %r probably overlaps node %r (move it with dx/dy)" % (text, label))
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            if overlap(labels[i][1], labels[j][1]):
                rep.warn(where, "labels %r (edge #%d) and %r (edge #%d) probably overlap"
                         % (labels[i][0], labels[i][2], labels[j][0], labels[j][2]))
    rep.info.append("%s: box %gx%g, %d nodes, %d edges" % (where, W, H if H < 10 ** 6 else 0, len(cards), len(edges)))


def check_seq(d, where, rep):
    W = 1100.0
    if "data-w" in d.attrs:
        W = num(d.attrs["data-w"])
        if W is None:
            rep.err(where, "data-w must be a positive number")
            W = 1100.0
    if d.seq_text is None:
        rep.err(where, "sequence diagram has no script.seqdef")
        return
    sd = load_json(d.seq_text, where, rep, "seqdef")
    if sd is None:
        return
    if not isinstance(sd, dict) or not isinstance(sd.get("participants"), list) or not sd["participants"] \
            or not isinstance(sd.get("messages"), list):
        rep.err(where, "seqdef needs a non-empty 'participants' list and a 'messages' list")
        return
    P = sd["participants"]
    ids = []
    for p in P:
        if not isinstance(p, dict) or "id" not in p or "label" not in p:
            rep.err(where, "each participant needs id and label")
            return
        if p["id"] in ids:
            rep.err(where, "duplicate participant id %r" % p["id"])
        ids.append(p["id"])
        if p.get("kind") is not None and p["kind"] not in NODE_KINDS:
            rep.err(where, "unknown participant kind %r" % p["kind"])
    n = len(P)
    col = (W - 48) / n
    xs = {pid: 24 + col * i + col / 2 for i, pid in enumerate(ids)}
    count = [0]

    def walk(items, depth):
        for m in items:
            if not isinstance(m, dict):
                rep.err(where, "message must be an object")
                continue
            if "loop" in m or "alt" in m:
                if not isinstance(m.get("items"), list):
                    rep.err(where, "loop/alt frame %r needs an 'items' list" % (m.get("loop") or m.get("alt")))
                else:
                    walk(m["items"], depth + 1)
                continue
            count[0] += 1
            if "note" in m:
                over = m.get("over")
                if not isinstance(over, list) or not over or any(o not in xs for o in over):
                    rep.err(where, "note %r: 'over' must list known participant ids" % plain_text(m["note"]))
                continue
            f, t = m.get("from"), m.get("to")
            if f not in xs or t not in xs:
                rep.err(where, "message %r references unknown participant (%r -> %r)" % (m.get("label"), f, t))
                continue
            if "label" not in m:
                rep.err(where, "message %s -> %s has no label" % (f, t))
                continue
            w = text_width(m["label"])
            if f != t:
                gap = abs(xs[f] - xs[t])
                if w > gap:
                    rep.warn(where, "label %r (~%dpx) is longer than the lane gap (%dpx): shorten it or reorder lanes"
                             % (plain_text(m["label"]), w, gap))
            else:
                limit = min([x for x in xs.values() if x > xs[f]] + [W]) - 10
                if xs[f] + 40 + w > limit:
                    rep.warn(where, "self-call label %r (~%dpx) runs into the next lane: split it into two calls"
                             % (plain_text(m["label"]), w))
    walk(sd["messages"], 0)
    rep.info.append("%s: sequence %gpx wide, %d participants, %d messages" % (where, W, n, count[0]))


def check_file(path):
    rep = Report(path)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        rep.err("file", "cannot read (%s)" % e.strerror)
        return rep, []
    p = Parser()
    try:
        p.feed(text)
        p.close()
    except Exception as e:  # malformed markup should be a report line, not a traceback
        rep.err("file", "HTML could not be parsed (%s)" % e)
        return rep, []
    if not p.diags:
        rep.warn("file", "no .diag found")
    for i, d in enumerate(p.diags, 1):
        where = "diag #%d" % i
        if not d.in_wrap:
            rep.warn(where, "not inside a .diag-wrap (it will not be scaled to its container)")
        (check_seq if d.is_seq else check_box)(d, where, rep)
    return rep, p.diags


# ---------------------------------------------------------------- render
def find_browser():
    for var in BROWSER_ENV:
        v = os.environ.get(var)
        if v and os.path.exists(v):
            return v
    for c in BROWSER_CMDS:
        w = shutil.which(c)
        if w:
            return w
    for pat in BROWSER_GLOBS:
        hits = sorted(glob.glob(os.path.expanduser(pat)))
        if hits:
            return hits[-1]
    return None


def render(browser, path, diags, out_dir):
    heights = [num(d.attrs.get("data-h")) or 700 for d in diags]
    height = int(260 + sum(h + 140 for h in heights))
    png = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + ".png")
    cmd = [browser, "--headless", "--no-sandbox", "--hide-scrollbars",
           "--virtual-time-budget=3000", "--window-size=1280,%d" % height,
           "--screenshot=" + png, "file://" + os.path.abspath(path)]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if os.path.exists(png) and os.path.getsize(png) > 0:
        return png, ""
    return None, "browser produced no screenshot"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Verify web-diagram HTML files (static, geometric, optional render).")
    ap.add_argument("files", nargs="+", metavar="FILE.html")
    ap.add_argument("--no-render", action="store_true", help="skip the headless screenshot")
    ap.add_argument("--out-dir", help="where to write PNGs (default: a new temp directory)")
    args = ap.parse_args(argv)

    failed = False
    parsed = []
    for path in args.files:
        rep, diags = check_file(path)
        for line in rep.info:
            if not rep.errors:
                print("[ok] %s - %s" % (path, line))
        for msg in rep.errors:
            print("[FAIL] %s - %s" % (path, msg))
        for msg in rep.warnings:
            print("[warn] %s - %s" % (path, msg))
        failed = failed or bool(rep.errors)
        parsed.append((path, diags, rep))

    if args.no_render:
        print("[skip] render - disabled by --no-render")
    else:
        browser = find_browser()
        if not browser:
            print("[skip] render - no Chromium found (set WEB_DIAGRAM_BROWSER to enable)")
        else:
            out_dir = args.out_dir or tempfile.mkdtemp(prefix="web-diagram-")
            os.makedirs(out_dir, exist_ok=True)
            for path, diags, rep in parsed:
                if not os.path.exists(path):
                    continue
                png, why = render(browser, path, diags, out_dir)
                if png:
                    print("[ok] %s - render: %s (open it: measured layout can differ from source)" % (path, png))
                else:
                    print("[warn] %s - render failed: %s" % (path, why))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
