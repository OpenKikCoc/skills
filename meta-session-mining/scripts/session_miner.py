#!/usr/bin/env python3
"""Mine coding-agent session logs into deterministic, redacted user-request candidates.

Supported formats (small adapters, auto-detected per file):
  * Codex CLI rollout JSONL   (~/.codex/sessions/**/rollout-*.jsonl)
  * Claude Code transcripts   (~/.claude/projects/*/*.jsonl)

Inputs are only ever read. Output goes to stdout; no timestamps of generation,
no source paths, no input-order dependence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA_VERSION = "1.0"
MAX_ASK_CHARS = 4000

# ---------------------------------------------------------------- redaction

_REDACTIONS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "<redacted:private-key>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"), "<redacted:jwt>"),
    (re.compile(r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key)\s*:\s*[^\r\n]+"), r"\1: <redacted>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer <redacted>"),
    (re.compile(r"(?i)(://[^/\s:@]+:)[^@\s/]+(@)"), r"\1<redacted>\2"),
    (re.compile(
        r"(?i)([?&;\s\"']?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|token|api[_-]?key|apikey|"
        r"secret|client[_-]?secret|password|passwd|signature|sig|cookie|session[_-]?id)[\"']?\s*[=:]\s*[\"']?)"
        r"[^\s\"'&;,)}\]]{4,}"), r"\1<redacted>"),
    (re.compile(r"(?i)(--(?:token|password|passwd|api-key|secret|auth)(?:=|\s+))\S+"), r"\1<redacted>"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "<redacted:key>"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "<redacted:key>"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "<redacted:key>"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "<redacted:key>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"), "<email>"),
    (re.compile(r"(?:/Users|/home)/[^/\s\"'`)]+(?:/[^\s\"'`)]*)?"), "<path>"),
    (re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s\"']+(?:\\[^\s\"']*)?"), "<path>"),
    (re.compile(r"(?<![\w.])~/[^\s\"'`)]+"), "<path>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


# --------------------------------------------------------- ask normalization

_BLOCK_TAGS = (
    "environment_context", "system-reminder", "user_instructions", "permissions instructions",
    "skills_instructions", "plugins_instructions", "skill", "local-command-stdout",
    "local-command-stderr", "local-command-caveat", "command-name", "command-message",
    "command-args", "ide_opened_file", "ide_selection", "turn_aborted", "task-notification",
)
_BLOCK_RE = re.compile(
    r"<(%s)\b[^>]*>.*?</\1>" % "|".join(re.escape(t) for t in _BLOCK_TAGS), re.S | re.I
)
_HARNESS_PREFIXES = ("# AGENTS.md instructions", "<INSTRUCTIONS>", "[Request interrupted")
_XML_ONLY_RE = re.compile(r"^(?:\s*<[^>]+>\s*)+$")


def normalize_ask(text: str) -> Optional[str]:
    """Strip harness-only blocks; return the redacted user text or None."""
    text = _BLOCK_RE.sub(" ", text).strip()
    if not text or text.startswith(_HARNESS_PREFIXES) or _XML_ONLY_RE.match(text):
        return None
    text = redact(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_ASK_CHARS:
        text = text[:MAX_ASK_CHARS].rstrip() + " ...[truncated]"
    return text or None


def content_digest(text: str) -> str:
    folded = " ".join(text.lower().split())
    return hashlib.sha256(folded.encode("utf-8")).hexdigest()


def parse_ts(value: Any) -> Optional[dt.datetime]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return dt.datetime.fromtimestamp(value / 1000 if value > 1e11 else value, dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    return None


def fmt_ts(value: Optional[dt.datetime]) -> Optional[str]:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


# ----------------------------------------------------------------- adapters

class Fragment:
    """What one file says about one session."""

    def __init__(self, adapter: str, session_id: Optional[str]) -> None:
        self.adapter = adapter
        self.session_id = session_id
        self.parent_id: Optional[str] = None
        self.excluded: Optional[str] = None
        self.asks: List[Tuple[Optional[dt.datetime], str]] = []


def _texts(content: Any, kinds: Sequence[str]) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type") in kinds and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "\n".join(parts)


def _codex_exclusion(meta: Dict[str, Any]) -> Optional[str]:
    source = meta.get("source")
    if isinstance(source, dict) and any("subagent" in str(k).lower() for k in source):
        return "subagent"
    marker = " ".join(str(meta.get(k, "")) for k in ("thread_source", "agent_role", "role", "originator")).lower()
    if "subagent" in marker:
        return "subagent"
    if "review" in marker or "guardian" in marker:
        return "reviewer"
    if isinstance(source, str) and "subagent" in source.lower():
        return "subagent"
    return None


def adapt_codex(records: Sequence[Dict[str, Any]]) -> Optional[Fragment]:
    meta = next((r for r in records if r.get("type") == "session_meta"), None)
    if meta is None:
        return None
    payload = meta.get("payload") if isinstance(meta.get("payload"), dict) else {}
    frag = Fragment("codex", payload.get("id") or payload.get("session_id"))
    frag.parent_id = payload.get("forked_from_id") or None
    frag.excluded = _codex_exclusion(payload)
    for rec in records:
        p = rec.get("payload")
        if not isinstance(p, dict) or p.get("type") != "message" or p.get("role") != "user":
            continue
        if rec.get("type") not in ("response_item", "history_mutation"):
            continue
        text = normalize_ask(_texts(p.get("content"), ("input_text", "text")))
        if text:
            frag.asks.append((parse_ts(rec.get("timestamp")), text))
    return frag


def adapt_claude(records: Sequence[Dict[str, Any]]) -> Optional[Fragment]:
    rows = [r for r in records if isinstance(r.get("sessionId"), str) and r.get("type") in ("user", "assistant", "system")]
    if not rows:
        return None
    frag = Fragment("claude", rows[0]["sessionId"])
    if any(r.get("isSidechain") is True or r.get("agentId") for r in rows):
        frag.excluded = "subagent"
    for rec in rows:
        if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            continue
        text = normalize_ask(_texts(content, ("text",)))
        if text:
            frag.asks.append((parse_ts(rec.get("timestamp")), text))
    return frag


ADAPTERS = {"codex": adapt_codex, "claude": adapt_claude}


# ---------------------------------------------------------------- discovery

def discover(inputs: Sequence[str]) -> List[Path]:
    found = set()
    for raw in inputs:
        path = Path(raw).expanduser()
        if path.is_dir():
            found.update(p.resolve() for p in path.rglob("*.jsonl") if p.is_file())
        elif path.is_file():
            found.add(path.resolve())
        else:
            raise FileNotFoundError("input not found: %s" % raw)
    return sorted(found)


def load_records(path: Path, diagnostics: List[Dict[str, Any]], file_id: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    bad = 0
    with path.open("rb") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                bad += 1
                continue
            if isinstance(value, dict):
                records.append(value)
            else:
                bad += 1
    if bad:
        diagnostics.append({"kind": "malformed_lines", "file": file_id, "count": bad})
    return records


# ------------------------------------------------------------------- report

def build_report(
    files: Sequence[Path],
    adapter: str,
    since: Optional[dt.date],
    until: Optional[dt.date],
    seen_ids: Sequence[str] = (),
) -> Dict[str, Any]:
    diagnostics: List[Dict[str, Any]] = []
    sessions: Dict[str, Fragment] = {}
    skipped = {"subagent": 0, "reviewer": 0, "unrecognized": 0, "empty_primary": 0}

    for path in files:
        file_id = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        records = load_records(path, diagnostics, file_id)
        frag = None
        for name in ([adapter] if adapter != "auto" else ["codex", "claude"]):
            frag = ADAPTERS[name](records)
            if frag is not None:
                break
        if frag is None or not frag.session_id:
            skipped["unrecognized"] += 1
            diagnostics.append({"kind": "unrecognized_file", "file": file_id})
            continue
        if frag.excluded:
            skipped[frag.excluded] += 1
            continue
        existing = sessions.get(frag.session_id)
        if existing is None:
            sessions[frag.session_id] = frag
        else:  # fragments of the same session (duplicates, split files)
            existing.asks.extend(frag.asks)
            existing.parent_id = existing.parent_id or frag.parent_id

    def lineage_root(sid: str) -> str:
        seen = set()
        while sid in sessions and sessions[sid].parent_id and sid not in seen:
            seen.add(sid)
            sid = sessions[sid].parent_id  # type: ignore[assignment]
        return sid

    def in_window(ts: Optional[dt.datetime]) -> bool:
        if since is None and until is None:
            return True
        if ts is None:
            return False
        day = ts.date()
        return (since is None or day >= since) and (until is None or day <= until)

    cands: Dict[str, Dict[str, Any]] = {}
    unknown_ts = 0
    for sid in sorted(sessions):
        frag = sessions[sid]
        kept = 0
        for ts, text in frag.asks:
            if not in_window(ts):
                if ts is None:
                    unknown_ts += 1
                continue
            kept += 1
            digest = content_digest(text)
            cid = "ask-" + digest[:16]
            c = cands.setdefault(cid, {
                "id": cid, "text": text, "first_seen": None,
                "session_ids": set(), "lineages": set(), "adapters": set(),
            })
            c["session_ids"].add(sid)
            c["lineages"].add(lineage_root(sid))
            c["adapters"].add(frag.adapter)
            if ts and (c["first_seen"] is None or ts < c["first_seen"]):
                c["first_seen"] = ts
        if not frag.asks:
            skipped["empty_primary"] += 1

    seen = set(seen_ids)
    out = []
    for c in cands.values():
        if c["id"] in seen:
            continue
        out.append({
            "id": c["id"],
            "text": c["text"],
            "first_seen": fmt_ts(c["first_seen"]),
            "occurrence_count": len(c["lineages"]),
            "session_ids": sorted(c["session_ids"]),
            "adapters": sorted(c["adapters"]),
        })
    out.sort(key=lambda c: (c["first_seen"] or "~", c["id"]))
    diagnostics.sort(key=lambda d: json.dumps(d, sort_keys=True))
    return {
        "schema_version": SCHEMA_VERSION,
        "summary": {
            "files_read": len(files),
            "primary_sessions": len(sessions),
            "excluded_subagent": skipped["subagent"],
            "excluded_reviewer": skipped["reviewer"],
            "unrecognized_files": skipped["unrecognized"],
            "empty_primary_sessions": skipped["empty_primary"],
            "unknown_timestamp_asks_filtered": unknown_ts,
            "already_seen_filtered": len(cands) - len(out),
            "candidates": len(out),
        },
        "diagnostics": diagnostics,
        "candidates": out,
    }


def md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: Dict[str, Any]) -> str:
    lines = ["# Session request inventory", "", "## Summary", ""]
    for key, value in report["summary"].items():
        lines.append("- %s: %s" % (key, value))
    lines += ["", "## Candidates", ""]
    if not report["candidates"]:
        lines.append("_none_")
    for c in report["candidates"]:
        lines.append("### %s" % c["id"])
        lines.append("")
        lines.append("- first_seen: %s; occurrences: %d; adapters: %s" % (
            c["first_seen"] or "unknown", c["occurrence_count"], ", ".join(c["adapters"])))
        lines.append("")
        lines.extend("> " + ln for ln in c["text"].splitlines() or [""])
        lines.append("")
    if report["diagnostics"]:
        lines += ["## Diagnostics", ""]
        lines += ["- %s" % md_escape(json.dumps(d, sort_keys=True)) for d in report["diagnostics"]]
    return "\n".join(lines).rstrip() + "\n"


def load_seen(paths: Sequence[str]) -> List[str]:
    ids: List[str] = []
    for raw in paths:
        data = json.loads(Path(raw).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
            raise ValueError("--seen must be a prior JSON report: %s" % raw)
        ids.extend(c["id"] for c in data["candidates"] if isinstance(c, dict) and "id" in c)
    return ids


def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD, got %r" % value)


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inputs", nargs="+", help="JSONL files or directories (searched recursively)")
    p.add_argument("--adapter", choices=["auto", "codex", "claude"], default="auto")
    p.add_argument("--format", choices=["json", "markdown"], default="json")
    p.add_argument("--since", type=parse_date, help="inclusive UTC date YYYY-MM-DD")
    p.add_argument("--until", type=parse_date, help="inclusive UTC date YYYY-MM-DD")
    p.add_argument("--seen", action="append", default=[], metavar="PRIOR.json",
                   help="prior JSON report; its candidate ids are omitted (incremental review)")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        files = discover(args.inputs)
        seen = load_seen(args.seen)
        report = build_report(files, args.adapter, args.since, args.until, seen)
    except (OSError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 2
    if args.format == "markdown":
        sys.stdout.write(render_markdown(report))
    else:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
