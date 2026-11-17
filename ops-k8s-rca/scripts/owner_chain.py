#!/usr/bin/env python3
"""Build owner chains from collected `kubectl get ... -o json` files.

Offline and read-only. Input files may hold a List or a single object.
Usage: owner_chain.py FILE [FILE ...] [--pod NAME] [--namespace NS] [--format text|json]
"""

import argparse
import json
import sys


def load_objects(paths):
    objs = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        items = data.get("items") if isinstance(data, dict) and "items" in data else [data]
        objs.extend(o for o in items if isinstance(o, dict) and o.get("metadata"))
    return objs


def index(objs):
    by_uid = {}
    for o in objs:
        uid = o["metadata"].get("uid")
        if uid:
            by_uid[uid] = o
    return by_uid


def find_by_name(objs, kind, ns, name):
    return [o for o in objs if o.get("kind") == kind and o["metadata"].get("name") == name
            and o["metadata"].get("namespace") == ns]


def ref_of(o):
    m = o["metadata"]
    return {"kind": o.get("kind"), "namespace": m.get("namespace"), "name": m.get("name"), "uid": m.get("uid")}


def chain(obj, objs, by_uid):
    """Return (hops, findings) following the controller ownerReference upward."""
    hops = [dict(ref_of(obj), state="start")]
    findings = []
    seen = {obj["metadata"].get("uid")}
    cur = obj
    while True:
        refs = cur["metadata"].get("ownerReferences") or []
        if not refs:
            break
        ctrl = [r for r in refs if r.get("controller")]
        if len(ctrl) > 1:
            findings.append("multiple controller owners on %s" % cur["metadata"].get("name"))
        ref = (ctrl or refs)[0]
        owner = by_uid.get(ref.get("uid"))
        if owner is None:
            named = find_by_name(objs, ref.get("kind"), cur["metadata"].get("namespace"), ref.get("name"))
            if named:
                findings.append("uid mismatch: %s/%s exists with uid %s, reference says %s" % (
                    ref.get("kind"), ref.get("name"), named[0]["metadata"].get("uid"), ref.get("uid")))
                state = "uid_mismatch"
            else:
                findings.append("dangling owner: %s/%s not in collected data" % (ref.get("kind"), ref.get("name")))
                state = "missing"
            hops.append({"kind": ref.get("kind"), "namespace": cur["metadata"].get("namespace"),
                         "name": ref.get("name"), "uid": ref.get("uid"), "state": state})
            break
        if owner["metadata"].get("uid") in seen:
            findings.append("cycle at %s/%s" % (owner.get("kind"), owner["metadata"].get("name")))
            break
        seen.add(owner["metadata"].get("uid"))
        hops.append(dict(ref_of(owner), state="resolved"))
        cur = owner
    return hops, findings


def build(objs, pod=None, namespace=None):
    by_uid = index(objs)
    starts = [o for o in objs if o.get("kind") == "Pod"
              and (pod is None or o["metadata"].get("name") == pod)
              and (namespace is None or o["metadata"].get("namespace") == namespace)]
    out = []
    for o in starts:
        hops, findings = chain(o, objs, by_uid)
        out.append({"pod": ref_of(o), "chain": hops, "findings": findings})
    return out


def render_text(results):
    lines = []
    for r in results:
        lines.append("%s/%s" % (r["pod"]["namespace"], r["pod"]["name"]))
        for h in r["chain"]:
            lines.append("  %-12s %-30s uid=%s [%s]" % (h["kind"], h["name"], h["uid"], h["state"]))
        for f in r["findings"]:
            lines.append("  FINDING: " + f)
        if not r["chain"][1:]:
            lines.append("  no owner (bare Pod)")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--pod")
    ap.add_argument("--namespace")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    a = ap.parse_args(argv)
    results = build(load_objects(a.files), a.pod, a.namespace)
    if not results:
        print("no matching Pod in input", file=sys.stderr)
        return 2
    print(json.dumps(results, indent=2) if a.format == "json" else render_text(results))
    return 1 if any(r["findings"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
