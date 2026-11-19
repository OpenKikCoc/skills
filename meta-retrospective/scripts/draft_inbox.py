#!/usr/bin/env python3
"""Local inbox of improvement proposals produced by a workflow review.

Commands:
  propose  validate a proposal packet (JSON) and store it as an immutable,
           content-addressed draft
  list     show drafts and their latest decision
  decide   record a decision receipt for a draft (never edits the draft)

The inbox only ever writes inside --inbox. It never edits skills, memory, or
project files: promotion is a separate, human-authorized step.
Standard library only (Python >= 3.9).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from credential_policy import credential_findings  # noqa: E402

TRIGGERS = ("user_correction", "repeated_failure", "retrospective")
TARGETS = ("draft_only", "project_guidance", "existing_skill", "casebook", "memory", "new_skill")
DECISIONS = ("promoted", "rejected", "deferred")
REQUIRED = ("title", "trigger", "mechanism", "evidence", "target", "proposed_change", "existing_rule_checked")
NAME_RE = re.compile(r"^[0-9a-f]{16}-[a-z0-9-]{1,48}\.json$")


class InboxError(Exception):
    pass


def reject_credentials(value: Any, what: str) -> None:
    """Refuse credential-like content. Reports labels only, never the matched text."""
    kinds = credential_findings(value)
    if kinds:
        raise InboxError("%s contains credential-like content (%s); redact it first" % (what, ", ".join(kinds)))


def validate_packet(packet: Any) -> Dict[str, Any]:
    reject_credentials(packet, "packet")
    if not isinstance(packet, dict):
        raise InboxError("packet must be a JSON object")
    missing = [k for k in REQUIRED if k not in packet]
    if missing:
        raise InboxError("missing fields: %s" % ", ".join(missing))
    unknown = sorted(set(packet) - set(REQUIRED) - {"target_ref", "blocking", "origin"})
    if unknown:
        raise InboxError("unknown fields: %s" % ", ".join(unknown))
    if packet["trigger"] not in TRIGGERS:
        raise InboxError("trigger must be one of: %s" % ", ".join(TRIGGERS))
    if packet["target"] not in TARGETS:
        raise InboxError("target must be one of: %s" % ", ".join(TARGETS))
    if packet.get("blocking", False) is not False:
        raise InboxError("review proposals must be non-blocking (blocking=false)")
    for key in ("title", "mechanism", "proposed_change", "existing_rule_checked"):
        if not isinstance(packet[key], str) or not packet[key].strip():
            raise InboxError("%s must be a non-empty string" % key)
    evidence = packet["evidence"]
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 12 or not all(
            isinstance(e, str) and e.strip() for e in evidence):
        raise InboxError("evidence must be 1-12 non-empty strings")
    if packet["target"] != "draft_only" and not packet.get("target_ref"):
        raise InboxError("target_ref (skill name, file, or memory entry) is required for target %s" % packet["target"])
    clean = {k: packet[k] for k in REQUIRED}
    clean["blocking"] = False
    if packet.get("target_ref"):
        clean["target_ref"] = packet["target_ref"]
    if packet.get("origin"):
        clean["origin"] = packet["origin"]
    return clean


def canonical(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48].strip("-")
    return s or "draft"


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def propose(inbox: Path, packet: Any) -> Dict[str, Any]:
    clean = validate_packet(packet)
    # Dedupe on what the proposal asks for, not on prose around it.
    key = {k: clean[k] for k in ("trigger", "target", "title", "proposed_change")}
    key["target_ref"] = clean.get("target_ref", "")
    digest = hashlib.sha256(canonical(key)).hexdigest()[:16]
    name = "%s-%s.json" % (digest, slug(clean["title"]))
    existing = sorted((inbox / "drafts").glob(digest + "-*.json")) if (inbox / "drafts").is_dir() else []
    if existing:
        return {"status": "duplicate", "draft": existing[0].name}
    atomic_write(inbox / "drafts" / name, canonical(clean))
    return {"status": "created", "draft": name}


def latest_decision(inbox: Path, draft: str) -> Optional[Dict[str, Any]]:
    receipts = sorted((inbox / "decisions").glob(draft[:-5] + ".r*.json")) if (inbox / "decisions").is_dir() else []
    receipts.sort(key=lambda p: int(p.name.rsplit(".r", 1)[1].split(".")[0]))
    if not receipts:
        return None
    receipt = json.loads(receipts[-1].read_text(encoding="utf-8"))
    reject_credentials(receipt, "decision receipt")
    return receipt


def list_drafts(inbox: Path) -> List[Dict[str, Any]]:
    rows = []
    for path in sorted((inbox / "drafts").glob("*.json")) if (inbox / "drafts").is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows.append({"draft": path.name, "status": "unreadable"})
            continue
        kinds = credential_findings(data)
        if kinds:
            rows.append({"draft": path.name, "status": "unsafe_draft", "unsafe_kinds": list(kinds)})
            continue
        try:
            decision = latest_decision(inbox, path.name)
        except InboxError:
            rows.append({"draft": path.name, "status": "unsafe_decision"})
            continue
        except (OSError, ValueError):
            rows.append({"draft": path.name, "status": "unreadable_decision"})
            continue
        rows.append({
            "draft": path.name, "title": data.get("title"), "target": data.get("target"),
            "target_ref": data.get("target_ref", ""),
            "status": decision["decision"] if decision else "pending",
        })
    return rows


def decide(inbox: Path, draft: str, decision: str, note: str) -> Dict[str, Any]:
    if not NAME_RE.match(draft) or not (inbox / "drafts" / draft).is_file():
        raise InboxError("unknown draft: %s" % draft)
    if decision not in DECISIONS:
        raise InboxError("decision must be one of: %s" % ", ".join(DECISIONS))
    if not note.strip():
        raise InboxError("a note is required")
    reject_credentials(note, "note")
    revision = 1 + len(list((inbox / "decisions").glob(draft[:-5] + ".r*.json"))) if (inbox / "decisions").is_dir() else 1
    draft_digest = hashlib.sha256((inbox / "drafts" / draft).read_bytes()).hexdigest()
    receipt = {"draft": draft, "draft_sha256": draft_digest, "decision": decision, "note": note.strip(), "revision": revision}
    out = "%s.r%d.json" % (draft[:-5], revision)
    atomic_write(inbox / "decisions" / out, canonical(receipt))
    return {"status": "recorded", "receipt": out}


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--inbox", required=True, help="inbox directory (keep it gitignored)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("propose", help="store a proposal packet as an immutable draft")
    s.add_argument("packet", help="packet JSON file, or - for stdin")
    sub.add_parser("list", help="list drafts with latest decision")
    d = sub.add_parser("decide", help="record a decision receipt")
    d.add_argument("--draft", required=True)
    d.add_argument("--decision", required=True, choices=DECISIONS)
    d.add_argument("--note", required=True)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = make_parser().parse_args(argv)
    inbox = Path(args.inbox).expanduser()
    try:
        if args.cmd == "propose":
            raw = sys.stdin.read() if args.packet == "-" else Path(args.packet).read_text(encoding="utf-8")
            try:
                packet = json.loads(raw)
            except ValueError as error:
                raise InboxError("packet is not valid JSON: %s" % error)
            result: Any = propose(inbox, packet)
        elif args.cmd == "list":
            result = list_drafts(inbox)
        else:
            result = decide(inbox, args.draft, args.decision, args.note)
    except (InboxError, OSError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
