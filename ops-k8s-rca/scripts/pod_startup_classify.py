#!/usr/bin/env python3
"""Classify stuck Pod startups from `kubectl get pods,events -o json` output.

Offline and read-only: it only parses JSON files you collected earlier.

Typical use:
  kubectl get pods -A --field-selector status.phase=Pending -o json > first.json
  sleep 20
  kubectl get pods,events -A -o json > second.json
  pod_startup_classify.py --first first.json --second second.json

With only --second the result is labelled single-snapshot and no Pod is called
a persistent stall.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

IMAGE_REASONS = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"}
CRASH_REASONS = {"CrashLoopBackOff", "RunContainerError", "Error", "OOMKilled"}
CONFIG_REASONS = {"CreateContainerConfigError", "CreateContainerError"}
CNI_WORDS = ("cni", "ipam", "allocate address", "no available ip", "failed to set up pod network", "network plugin")
VOLUME_REASONS = {"FailedMount", "FailedAttachVolume", "FailedMapVolume"}

NEXT_CHECK = {
    "terminating_residue": "exclude from active impact; check finalizers only if it never disappears",
    "too_young": "re-snapshot after the stuck threshold before judging",
    "scheduler_or_policy": "read the PodScheduled message: taints, affinity, quota, resource requests",
    "image": "pull error text, registry reachability and credentials from the bound node",
    "volume_mount": "PVC/PV state, attach events, CSI node plugin logs on the bound node",
    "cni_ipam": "CNI daemon logs, IP pool exhaustion, route state on the bound node",
    "kubelet_cri_sandbox": "kubelet and container-runtime logs on the node; compare with a healthy control node",
    "container_config": "referenced Secret/ConfigMap/env and container create error text",
    "app_runtime_or_probe": "container logs, previous logs and probe definition; not a node-startup problem",
    "bound_no_signal": "kubelet watch/sync backlog on the bound node; node Ready and heartbeat",
    "historical_orphan": "owner/controller cleanup path; report separately from active impact",
    "unclassified": "collect describe output and node-side logs; no deterministic rule matched",
}


class InputError(Exception):
    pass


def parse_ts(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def load_items(path: str) -> List[Dict[str, Any]]:
    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        doc = json.loads(text)
    except (OSError, ValueError) as exc:
        raise InputError(f"cannot read {path}: {exc}") from exc
    if isinstance(doc, dict) and isinstance(doc.get("items"), list):
        items = doc["items"]
    elif isinstance(doc, dict) and "kind" in doc:
        items = [doc]
    elif isinstance(doc, list):
        items = doc
    else:
        raise InputError(f"{path}: expected a kubectl List, an object, or an array")
    return [i for i in items if isinstance(i, dict)]


def split(items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    pods = [i for i in items if i.get("kind") == "Pod"]
    events = [i for i in items if i.get("kind") == "Event"]
    return pods, events


def waiting_reasons(pod: Dict[str, Any]) -> List[str]:
    status = pod.get("status") or {}
    out = []
    for key in ("initContainerStatuses", "containerStatuses"):
        for cs in status.get(key) or []:
            reason = ((cs.get("state") or {}).get("waiting") or {}).get("reason")
            if reason:
                out.append(reason)
    return out


def is_candidate(pod: Dict[str, Any]) -> bool:
    status = pod.get("status") or {}
    if status.get("phase") == "Pending":
        return True
    return any(r != "ContainerCreating" and r != "PodInitializing" for r in waiting_reasons(pod))


def pod_key(pod: Dict[str, Any]) -> str:
    meta = pod.get("metadata") or {}
    return meta.get("uid") or f"{meta.get('namespace', '')}/{meta.get('name', '')}"


def event_time(ev: Dict[str, Any]) -> Optional[dt.datetime]:
    series = ev.get("series") or {}
    for value in (ev.get("lastTimestamp"), series.get("lastObservedTime"), ev.get("eventTime"),
                  (ev.get("metadata") or {}).get("creationTimestamp")):
        parsed = parse_ts(value)
        if parsed:
            return parsed
    return None


def events_for(pod: Dict[str, Any], events: List[Dict[str, Any]], since: Optional[dt.datetime]) -> List[Dict[str, Any]]:
    meta = pod.get("metadata") or {}
    out = []
    for ev in events:
        obj = ev.get("involvedObject") or {}
        if obj.get("kind", "Pod") != "Pod":
            continue
        same = (obj.get("uid") and obj.get("uid") == meta.get("uid")) or (
            not obj.get("uid") and obj.get("name") == meta.get("name")
            and obj.get("namespace", meta.get("namespace")) == meta.get("namespace"))
        if not same:
            continue
        when = event_time(ev)
        if since and when and when < since:
            continue
        out.append(ev)
    return out


def classify(pod: Dict[str, Any], evs: List[Dict[str, Any]], now: dt.datetime,
             min_stuck: int, orphan_age: int) -> Tuple[str, str]:
    """Return (class, reason)."""
    meta = pod.get("metadata") or {}
    spec = pod.get("spec") or {}
    status = pod.get("status") or {}
    if meta.get("deletionTimestamp"):
        return "terminating_residue", "deletionTimestamp is set"
    created = parse_ts(meta.get("creationTimestamp"))
    age = (now - created).total_seconds() if created else None
    if age is not None and age < min_stuck:
        return "too_young", f"age {int(age)}s below {min_stuck}s"
    waits = set(waiting_reasons(pod))
    if not spec.get("nodeName"):
        sched = [c for c in status.get("conditions") or [] if c.get("type") == "PodScheduled"]
        msg = (sched[0].get("message") or sched[0].get("reason") or "") if sched else ""
        return "scheduler_or_policy", msg or "no nodeName assigned"
    reasons = {e.get("reason") for e in evs}
    text = " ".join(f"{e.get('reason', '')} {e.get('message', '')}" for e in evs).lower()
    if waits & IMAGE_REASONS or ({"Failed", "BackOff"} & reasons and "image" in text):
        return "image", sorted(waits & IMAGE_REASONS)[0] if waits & IMAGE_REASONS else "image pull events"
    if reasons & VOLUME_REASONS:
        return "volume_mount", sorted(reasons & VOLUME_REASONS)[0]
    sandbox = [e for e in evs if e.get("reason") == "FailedCreatePodSandBox"]
    if sandbox:
        stext = " ".join((e.get("message") or "") for e in sandbox).lower()
        if any(w in stext for w in CNI_WORDS):
            return "cni_ipam", "explicit CNI/IPAM text in FailedCreatePodSandBox"
        return "kubelet_cri_sandbox", "FailedCreatePodSandBox without CNI/IPAM text"
    if waits & CONFIG_REASONS:
        return "container_config", sorted(waits & CONFIG_REASONS)[0]
    if (waits & CRASH_REASONS) or "Unhealthy" in reasons:
        return "app_runtime_or_probe", sorted(waits & CRASH_REASONS)[0] if waits & CRASH_REASONS else "probe failures"
    if not evs and not status.get("hostIP") and not status.get("podIP"):
        if age is not None and age >= orphan_age:
            return "historical_orphan", "old, bound, no host/pod IP, no retained events"
        return "bound_no_signal", "bound to a node but no IP and no events"
    return "unclassified", "no deterministic rule matched"


def analyze(first: Optional[List[Dict[str, Any]]], second: List[Dict[str, Any]], *, now: dt.datetime,
            min_stuck: int = 300, orphan_age: int = 86400, event_window: int = 1800) -> Dict[str, Any]:
    pods2, events2 = split(second)
    events = events2 + (split(first)[1] if first else [])
    first_keys = {pod_key(p) for p in split(first)[0] if is_candidate(p)} if first is not None else None
    since = now - dt.timedelta(seconds=event_window) if event_window > 0 else None
    mode = "two-snapshot" if first is not None else "single-snapshot"
    results = []
    for pod in pods2:
        if not is_candidate(pod):
            continue
        meta = pod.get("metadata") or {}
        evs = events_for(pod, events, since)
        cls, why = classify(pod, evs, now, min_stuck, orphan_age)
        persistent = first_keys is not None and pod_key(pod) in first_keys and cls not in ("too_young", "terminating_residue")
        results.append({
            "namespace": meta.get("namespace"), "name": meta.get("name"), "uid": meta.get("uid"),
            "node": (pod.get("spec") or {}).get("nodeName") or None,
            "created": meta.get("creationTimestamp"), "class": cls, "reason": why,
            "persistent": persistent, "next_check": NEXT_CHECK[cls],
            "event_reasons": sorted({e.get("reason") for e in evs if e.get("reason")}),
        })
    results.sort(key=lambda r: (r["class"], r["namespace"] or "", r["name"] or ""))
    counts = Counter(r["class"] for r in results)
    by_node: Dict[str, Counter] = defaultdict(Counter)
    for r in results:
        if r["node"] and r["class"] in ("kubelet_cri_sandbox", "cni_ipam", "bound_no_signal", "volume_mount", "image"):
            by_node[r["node"]][r["class"]] += 1
    return {
        "mode": mode, "now": now.isoformat().replace("+00:00", "Z"),
        "thresholds": {"min_stuck_seconds": min_stuck, "orphan_age_seconds": orphan_age, "event_window_seconds": event_window},
        "candidates": len(results), "class_counts": dict(sorted(counts.items())),
        "persistent_active": sum(1 for r in results if r["persistent"]),
        "node_concentration": {n: dict(c) for n, c in sorted(by_node.items())},
        "pods": results,
        "caveats": [
            "A class is a lead, not a root cause; confirm with node-side evidence.",
            "Missing events can be retention, not absence of failure.",
        ] + (["Single snapshot: persistence is not established."] if first is None else []),
    }


def summary(report: Dict[str, Any]) -> str:
    lines = [f"mode={report['mode']} candidates={report['candidates']} persistent_active={report['persistent_active']}"]
    for cls, n in report["class_counts"].items():
        lines.append(f"  {cls}: {n}  -> {NEXT_CHECK[cls]}")
    for node, c in report["node_concentration"].items():
        lines.append(f"  node {node}: {c}")
    lines.extend(f"  note: {c}" for c in report["caveats"])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--first", help="earlier snapshot JSON (kubectl get pods -o json); enables persistence")
    p.add_argument("--second", required=True, help="later snapshot JSON with pods and events ('-' for stdin)")
    p.add_argument("--now", help="override current time (ISO 8601 UTC); default: system clock")
    p.add_argument("--min-stuck-seconds", type=int, default=300)
    p.add_argument("--orphan-age-seconds", type=int, default=86400)
    p.add_argument("--event-window-seconds", type=int, default=1800, help="0 disables the window")
    p.add_argument("--format", choices=("summary", "json"), default="summary")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        now = parse_ts(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
        if now is None:
            raise InputError(f"invalid --now {args.now!r}")
        first = load_items(args.first) if args.first else None
        second = load_items(args.second)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report = analyze(first, second, now=now, min_stuck=args.min_stuck_seconds,
                     orphan_age=args.orphan_age_seconds, event_window=args.event_window_seconds)
    print(json.dumps(report, indent=2, sort_keys=True) if args.format == "json" else summary(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
