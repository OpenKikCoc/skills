#!/usr/bin/env python3
"""Local oncall RCA ledger: validate ticket records, render RCAs, roll up a week.

Layout of a ledger directory:
  tickets/<ticket_id>.json   source of truth (one schema'd record per ticket)
  rca/<ticket_id>.md         per-ticket investigation view, rendered from the record
  weekly/<YYYY-Www>.md       compiled roll-up, recomputed from records only

Commands: new, validate, render-rca, rollup. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1
MODES = ("investigate-only", "investigate-and-rollup")
PRIORITIES = ("P0", "P1", "P2", "P3", "P4")
STATUSES = ("resolved", "in_progress", "stalled")
ROOT_STATES = ("unknown", "suspected", "partially_confirmed", "confirmed")
PLAN_STATES = ("unknown", "proposed", "blocked", "feasibility_verified", "owner_verified", "stale")
EXECUTION = ("not_started", "executing", "failed", "completed")
VERIFICATION = ("not_started", "failed", "verified")
ACCURACY = ("unreviewed", "confirmed", "partially_confirmed", "incorrect")
PUBLISH = ("draft", "published")
POLICIES = ("preview", "apply")
CORRECTION_KINDS = ("material_fact", "actor", "resource_kind", "important_omission")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")


class LedgerError(Exception):
    """A deterministic, user-facing failure."""


def _enum(errors: List[str], data: Dict[str, Any], key: str, allowed, path: str = "") -> Any:
    value = data.get(key)
    if value not in allowed:
        errors.append(f"{path}{key} must be one of {', '.join(allowed)} (got {value!r})")
    return value


def _text(errors: List[str], data: Dict[str, Any], key: str, path: str = "") -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}{key} must be a non-empty string")
        return ""
    return value


def _list(errors: List[str], data: Dict[str, Any], key: str, path: str = "") -> List[Any]:
    value = data.get(key, [])
    if not isinstance(value, list):
        errors.append(f"{path}{key} must be a list")
        return []
    return value


def iso_week(date_text: str) -> str:
    year, week, _ = dt.date.fromisoformat(date_text).isocalendar()
    return f"{year}-W{week:02d}"


def validate_ticket(t: Any, for_rollup: bool = False) -> List[str]:
    """Return a list of error strings; empty means valid."""
    errors: List[str] = []
    if not isinstance(t, dict):
        return ["record must be a JSON object"]
    if t.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    tid = t.get("ticket_id")
    if not isinstance(tid, str) or not ID_RE.match(tid):
        errors.append("ticket_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    mode = _enum(errors, t, "mode", MODES)
    _enum(errors, t, "priority", PRIORITIES)
    status = _enum(errors, t, "status", STATUSES)
    for key in ("title", "category", "environment", "executive_summary", "impact"):
        _text(errors, t, key)
    try:
        dt.date.fromisoformat(str(t.get("date")))
    except ValueError:
        errors.append("date must be an ISO date (YYYY-MM-DD)")

    catalog = {}
    for i, ev in enumerate(_list(errors, t, "evidence_catalog")):
        p = f"evidence_catalog[{i}]."
        if not isinstance(ev, dict):
            errors.append(f"evidence_catalog[{i}] must be an object")
            continue
        eid = _text(errors, ev, "id", p)
        for key in ("kind", "scope", "source", "summary"):
            _text(errors, ev, key, p)
        if eid in catalog:
            errors.append(f"duplicate evidence id {eid}")
        catalog[eid] = ev

    def refs(items: Any, where: str) -> List[Dict[str, Any]]:
        out = []
        for ref in items or []:
            if ref not in catalog:
                errors.append(f"{where} cites unknown evidence ref {ref!r}")
            else:
                out.append(catalog[ref])
        return out

    for i, fact in enumerate(_list(errors, t, "facts")):
        if not isinstance(fact, dict) or not fact.get("claim"):
            errors.append(f"facts[{i}] needs a claim")
            continue
        if not fact.get("evidence"):
            errors.append(f"facts[{i}] needs at least one evidence ref")
        refs(fact.get("evidence"), f"facts[{i}]")
    for i, h in enumerate(_list(errors, t, "hypotheses")):
        if not isinstance(h, dict):
            errors.append(f"hypotheses[{i}] must be an object")
            continue
        _text(errors, h, "statement", f"hypotheses[{i}].")
        _enum(errors, h, "status", ("open", "confirmed", "rejected"), f"hypotheses[{i}].")
        if h.get("status") in ("confirmed", "rejected") and not h.get("evidence"):
            errors.append(f"hypotheses[{i}] resolved without evidence")
        refs(h.get("evidence"), f"hypotheses[{i}]")

    rc = t.get("root_cause")
    if not isinstance(rc, dict):
        errors.append("root_cause must be an object")
        rc = {}
    rc_state = _enum(errors, rc, "status", ROOT_STATES, "root_cause.")
    _text(errors, rc, "summary", "root_cause.")
    rc_ev = refs(rc.get("evidence_refs"), "root_cause")
    if rc_state == "confirmed":
        if not rc_ev:
            errors.append("confirmed root_cause requires evidence_refs")
        if any(e.get("scope") == "symptom_only" for e in rc_ev) or (rc_ev and all(e.get("scope") == "symptom_only" for e in rc_ev)):
            errors.append("confirmed root_cause cannot cite symptom_only evidence")
    if rc_state in ("unknown", "suspected") and not t.get("open_questions"):
        errors.append("unconfirmed root_cause requires open_questions")

    rec = t.get("recovery")
    if not isinstance(rec, dict):
        errors.append("recovery must be an object")
        rec = {}
    _enum(errors, rec, "plan_state", PLAN_STATES, "recovery.")
    execution = _enum(errors, rec, "execution", EXECUTION, "recovery.")
    verification = _enum(errors, rec, "verification", VERIFICATION, "recovery.")
    passed_post_action = False
    for i, chk in enumerate(_list(errors, rec, "checks", "recovery.")):
        if not isinstance(chk, dict):
            errors.append(f"recovery.checks[{i}] must be an object")
            continue
        found = refs([chk["evidence_ref"]] if chk.get("evidence_ref") else [], f"recovery.checks[{i}]")
        if chk.get("passed") is True and any(e.get("kind") == "post_action_observation" for e in found):
            passed_post_action = True
    if status == "resolved":
        if rec.get("plan_state") != "owner_verified":
            errors.append("resolved requires recovery.plan_state=owner_verified")
        if execution != "completed" or verification != "verified":
            errors.append("resolved requires execution=completed and verification=verified")
        if not passed_post_action:
            errors.append("resolved requires a passed check citing post_action_observation evidence")
    if verification == "verified" and execution != "completed":
        errors.append("verification=verified requires execution=completed")

    rca = t.get("rca")
    if not isinstance(rca, dict):
        errors.append("rca must be an object")
        rca = {}
    _enum(errors, rca, "publish_status", PUBLISH, "rca.")
    accuracy = _enum(errors, rca, "accuracy", ACCURACY, "rca.")
    if not isinstance(rca.get("agent_used"), bool):
        errors.append("rca.agent_used must be a boolean")
    if rca.get("agent_used") is False and accuracy != "unreviewed":
        errors.append("rca.accuracy must be unreviewed when agent_used is false")
    if accuracy not in (None, "unreviewed") and str(rca.get("accuracy_basis", "")).strip().lower() in ("", "none", "n/a", "unknown"):
        errors.append("reviewed accuracy requires a concrete rca.accuracy_basis")
    corrections = _list(errors, rca, "material_corrections", "rca.")
    for i, c in enumerate(corrections):
        p = f"rca.material_corrections[{i}]"
        if not isinstance(c, dict):
            errors.append(f"{p} must be an object")
            continue
        if c.get("kind") not in CORRECTION_KINDS:
            errors.append(f"{p}.kind must be one of {', '.join(CORRECTION_KINDS)}")
        old, new = str(c.get("previous_value", "")).split(), str(c.get("corrected_value", "")).split()
        if not old or not new or old == new:
            errors.append(f"{p} must change a non-empty previous_value")
        if not c.get("evidence_refs"):
            errors.append(f"{p} needs evidence_refs")
        refs(c.get("evidence_refs"), p)
    if corrections:
        if rca.get("agent_used") is not True:
            errors.append("material_corrections require rca.agent_used=true")
        if accuracy not in ("partially_confirmed", "incorrect"):
            errors.append("material_corrections require accuracy partially_confirmed or incorrect")

    auth = t.get("write_authorization")
    if not isinstance(auth, dict):
        errors.append("write_authorization must be an object")
        auth = {}
    policy = _enum(errors, auth, "policy", POLICIES, "write_authorization.")

    if for_rollup:
        if mode != "investigate-and-rollup":
            errors.append("roll-up requires mode=investigate-and-rollup")
        if policy != "apply":
            errors.append("roll-up requires write_authorization.policy=apply")
        if rca.get("publish_status") != "published":
            errors.append("roll-up requires rca.publish_status=published")
        src = t.get("source_evidence")
        if not isinstance(src, dict) or not src.get("locator") or src.get("complete") is not True:
            errors.append("roll-up requires source_evidence with a locator and complete=true")
    return errors


def load_ticket(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise LedgerError(f"cannot read {path}: {exc}") from exc


def scaffold(ticket_id: str, date: str) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION, "ticket_id": ticket_id, "mode": "investigate-only",
        "priority": "P3", "status": "in_progress", "title": "TODO", "date": date,
        "category": "TODO", "environment": "TODO", "reporter": "", "ticket_url": "",
        "executive_summary": "TODO", "impact": "TODO", "symptom": [],
        "source_evidence": {"locator": "", "complete": False},
        "evidence_catalog": [], "facts": [], "hypotheses": [],
        "root_cause": {"status": "unknown", "summary": "Not yet determined", "evidence_refs": []},
        "recovery": {"plan_state": "unknown", "execution": "not_started", "verification": "not_started", "checks": []},
        "rca": {"publish_status": "draft", "agent_used": False, "accuracy": "unreviewed",
                "accuracy_basis": "", "agent_summary": "", "material_corrections": []},
        "timeline": [], "action_items": [], "open_questions": ["Root cause not yet investigated"],
        "write_authorization": {"policy": "preview", "weekly_id": ""},
    }


def bullets(items: List[Any]) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- none recorded"


def render_rca(t: Dict[str, Any]) -> str:
    rc, rec, rca = t["root_cause"], t["recovery"], t["rca"]
    cat = {e["id"]: e for e in t.get("evidence_catalog", [])}
    out = [f"# {t['ticket_id']}: {t['title']}", "",
           "## 1. Executive summary and current status", t["executive_summary"], "",
           f"Status: {t['status']}. Diagnosis: {rc['status']}. Recovery plan: {rec['plan_state']}; "
           f"execution: {rec['execution']}; verification: {rec['verification']}.", "",
           "## 2. Ticket identity and scope",
           f"- Priority: {t['priority']}", f"- Date: {t['date']}", f"- Category: {t['category']}",
           f"- Environment: {t['environment']}", f"- Reporter: {t.get('reporter') or 'unknown'}",
           f"- Source: {(t.get('source_evidence') or {}).get('locator') or 'unrecorded'}"
           f" (complete: {bool((t.get('source_evidence') or {}).get('complete'))})", "",
           "## 3. Impact and failure boundary", t["impact"], "", bullets(t.get("symptom", [])), "",
           "## 4. Timeline"]
    out.append("\n".join(f"- {e.get('time', '?')}: {e.get('event', '')} ({e.get('evidence', 'no evidence')})"
                         for e in t.get("timeline", [])) or "- none recorded")
    out += ["", "## 5. Root cause analysis", f"Status: {rc['status']}", rc["summary"], "",
            "Evidence: " + (", ".join(rc.get("evidence_refs", [])) or "none"), "",
            "## 6. Evidence chain and hypotheses", "### Facts"]
    out.append("\n".join(f"- {f['claim']} [{', '.join(f.get('evidence', []))}]" for f in t.get("facts", [])) or "- none recorded")
    out += ["", "### Hypotheses"]
    out.append("\n".join(f"- ({h['status']}) {h['statement']}; falsifier: {h.get('falsifier') or 'unspecified'}"
                         for h in t.get("hypotheses", [])) or "- none recorded")
    out += ["", "### Evidence catalog"]
    out.append("\n".join(f"- {e['id']} [{e['kind']}/{e['scope']}] {e['summary']} - {e['source']}" for e in cat.values()) or "- none recorded")
    out += ["", "## 7. Remediation and responsibility",
            bullets([f"{a.get('item', '')} (owner: {a.get('owner') or 'unassigned'}, {a.get('status', 'open')})" for a in t.get("action_items", [])]), "",
            "## 8. Recovery acceptance and open questions"]
    out.append("\n".join(f"- [{'x' if c.get('passed') else ' '}] {c.get('description', '')} ({c.get('evidence_ref') or 'no evidence'})"
                         for c in rec.get("checks", [])) or "- no acceptance checks recorded")
    out += ["", bullets(t.get("open_questions", [])), "",
            "## 9. AI assistance",
            f"- Agent used: {'yes' if rca['agent_used'] else 'no'}", f"- Accuracy: {rca['accuracy']}",
            f"- Basis: {rca.get('accuracy_basis') or 'n/a'}", f"- Summary: {rca.get('agent_summary') or 'n/a'}"]
    for c in rca.get("material_corrections", []):
        out.append(f"- Correction ({c['kind']}): {c['previous_value']} -> {c['corrected_value']}")
    return "\n".join(out) + "\n"


def pct(num: int, den: int) -> str:
    return "0/0 (n/a)" if den == 0 else f"{num}/{den} ({num * 100.0 / den:.1f}%)"


def ai_stats(items: List[Dict[str, Any]]) -> Dict[str, str]:
    used = [t for t in items if t["rca"]["agent_used"]]
    reviewed = [t for t in used if t["rca"]["accuracy"] != "unreviewed"]
    confirmed = [t for t in reviewed if t["rca"]["accuracy"] == "confirmed"]
    return {"coverage": pct(len(used), len(items)), "accuracy": pct(len(confirmed), len(reviewed))}


def render_weekly(week: str, tickets: List[Dict[str, Any]]) -> str:
    tickets = sorted(tickets, key=lambda t: (PRIORITIES.index(t["priority"]), t["ticket_id"]))
    pr = Counter(t["priority"] for t in tickets)
    st = Counter(t["status"] for t in tickets)
    out = [f"# Oncall weekly {week}", "", "## Totals",
           f"- Total tickets: {len(tickets)}",
           "- By priority: " + ", ".join(f"{p}={pr.get(p, 0)}" for p in PRIORITIES),
           "- By status: " + ", ".join(f"{s}={st.get(s, 0)}" for s in STATUSES), "",
           "## AI assistance",
           "Coverage = agent used / all tickets. Accuracy = confirmed / reviewed (agent used and not unreviewed); "
           "partially_confirmed and incorrect count in the denominator only.", "",
           "| Group | Tickets | Coverage | Accuracy |", "|---|---|---|---|"]
    overall = ai_stats(tickets)
    out.append(f"| All | {len(tickets)} | {overall['coverage']} | {overall['accuracy']} |")
    for label, key in (("category", "category"), ("environment", "environment")):
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for t in tickets:
            groups[t[key]].append(t)
        for name in sorted(groups):
            s = ai_stats(groups[name])
            out.append(f"| {label}: {name} | {len(groups[name])} | {s['coverage']} | {s['accuracy']} |")
    out += ["", "## Tickets", "| Ticket | Priority | Status | Category | Environment | Diagnosis | Title |", "|---|---|---|---|---|---|---|"]
    for t in tickets:
        title = t["title"].replace("|", "\\|")
        out.append(f"| {t['ticket_id']} | {t['priority']} | {t['status']} | {t['category']} | {t['environment']} | {t['root_cause']['status']} | {title} |")
    for t in tickets:
        out += ["", f"### {t['ticket_id']}: {t['title']}", t["executive_summary"], f"RCA: rca/{t['ticket_id']}.md"]
    return "\n".join(out) + "\n"


def write_if_changed(path: str, content: str) -> str:
    existing = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            existing = fh.read()
        if existing == content:
            return "noop"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return "created" if existing is None else "updated"


def collect_week(ledger: str, week: str) -> List[Dict[str, Any]]:
    tdir = os.path.join(ledger, "tickets")
    if not os.path.isdir(tdir):
        raise LedgerError(f"no tickets directory in {ledger}")
    selected, problems = [], []
    for name in sorted(os.listdir(tdir)):
        if not name.endswith(".json"):
            continue
        t = load_ticket(os.path.join(tdir, name))
        errs = validate_ticket(t)
        if errs:
            problems.append(f"{name}: {errs[0]}")
            continue
        if t["ticket_id"] + ".json" != name:
            problems.append(f"{name}: ticket_id does not match file name")
            continue
        if iso_week(t["date"]) != week or t["mode"] != "investigate-and-rollup":
            continue
        errs = validate_ticket(t, for_rollup=True)
        if errs:
            problems.append(f"{name}: {'; '.join(errs)}")
            continue
        selected.append(t)
    if problems:
        raise LedgerError("roll-up refused (fail closed):\n  " + "\n  ".join(problems))
    return selected


def cmd_new(a: argparse.Namespace) -> int:
    if not ID_RE.match(a.ticket_id):
        raise LedgerError("invalid ticket id")
    path = os.path.join(a.ledger, "tickets", a.ticket_id + ".json")
    if os.path.exists(path):
        raise LedgerError(f"{path} already exists")
    date = a.date or dt.date.today().isoformat()
    print(write_if_changed(path, json.dumps(scaffold(a.ticket_id, date), indent=2) + "\n"), path)
    return 0


def cmd_validate(a: argparse.Namespace) -> int:
    errs = validate_ticket(load_ticket(a.ticket), for_rollup=a.for_rollup)
    for e in errs:
        print(f"error: {e}", file=sys.stderr)
    if not errs:
        print("ok")
    return 1 if errs else 0


def cmd_render(a: argparse.Namespace) -> int:
    t = load_ticket(a.ticket)
    errs = validate_ticket(t)
    if errs:
        for e in errs:
            print(f"error: {e}", file=sys.stderr)
        return 1
    text = render_rca(t)
    if not a.write:
        sys.stdout.write(text)
        return 0
    if t["write_authorization"]["policy"] != "apply":
        raise LedgerError("write_authorization.policy is 'preview'; refusing to write (omit --write to preview)")
    target = os.path.join(a.write, "rca", t["ticket_id"] + ".md")
    print(write_if_changed(target, text), target)
    return 0


def cmd_rollup(a: argparse.Namespace) -> int:
    if not WEEK_RE.match(a.week):
        raise LedgerError("--week must look like 2030-W05")
    tickets = collect_week(a.ledger, a.week)
    text = render_weekly(a.week, tickets)
    target = os.path.join(a.ledger, "weekly", a.week + ".md")
    if a.check:
        current = open(target, encoding="utf-8").read() if os.path.exists(target) else None
        if current == text:
            print("noop")
            return 0
        print("drift: weekly report differs from records", file=sys.stderr)
        return 1
    if not a.write:
        sys.stdout.write(text)
        return 0
    print(write_if_changed(target, text), target)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="scaffold a draft ticket record")
    n.add_argument("ledger")
    n.add_argument("ticket_id")
    n.add_argument("--date", help="ISO date; default today")
    n.set_defaults(fn=cmd_new)
    v = sub.add_parser("validate", help="validate a ticket record")
    v.add_argument("ticket")
    v.add_argument("--for-rollup", action="store_true", help="also apply the publication/authorization gates")
    v.set_defaults(fn=cmd_validate)
    r = sub.add_parser("render-rca", help="render the per-ticket RCA (stdout unless --write LEDGER)")
    r.add_argument("ticket")
    r.add_argument("--write", metavar="LEDGER", help="write rca/<id>.md; requires policy=apply")
    r.set_defaults(fn=cmd_render)
    w = sub.add_parser("rollup", help="compile the weekly report from records")
    w.add_argument("ledger")
    w.add_argument("--week", required=True, help="ISO week, e.g. 2030-W05")
    w.add_argument("--write", action="store_true", help="write weekly/<week>.md (default: print)")
    w.add_argument("--check", action="store_true", help="exit 1 if the stored weekly differs from records")
    w.set_defaults(fn=cmd_rollup)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
