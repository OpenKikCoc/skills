# Durable workspace state

State is private and local. Initialize with `scripts/init_workspace.py` (see
SKILL.md). Exit 0 created or skipped, 2 usage or I/O error (including a
symlinked workspace or state directory), 3 existing files would be overwritten.
A state-file symlink counts as an existing file and is never followed.

| State | Role | Update rule |
|---|---|---|
| CONTRACT.md | Audience, ownership evidence, skeleton, voice, scope, safe defaults, safety | Amend only against newer FEEDBACK |
| FEEDBACK.md | Verbatim owner messages: id `M<n>`, date, reacted-to version, superseded-by | Append before acting; never rewrite a quote; supersede by reference |
| RULES.md | Single distilled, reader-visible rule source | Cite message ids; wins over other planning state except newer FEEDBACK; never overrides permission safety |
| TRIAGE.md | Every review finding and its adopt/reject/defer ruling, by round | Append rounds; a rejection cites a rule or message id; record gate closure |
| HANDOFF.md | Live document and revision, current version, `next_action`, approvals, ledgers | Update at each milestone and verified write |
| NUMBERS.md | Value, section, source, basis, snapshot date | Verify before whitelist use |
| SOURCES.md | Kept claim, tier (sourced or plan-proposal), source | Unsupported claims are removed from prose and figures |
| CONCEPTS.md | Concept cards | Complete before drafting |
| lint.json | Banned terms, names, required links, length limits | Add forbidden literal slogans here |
| reviews/ | One file per lane per round | Never edited after the triage that used it |
| versions/ | Immutable draft files | New file per edit; lint each |
| publish/ | Backups, manifests, ledgers, read-back results | Bound to document, version, revision |

HANDOFF holds one active goal and one `next_action`; business records are not a
second control packet. Message classification when new feedback arrives:
clarify (refines a rule), add (new rule), pause, supersede (replaces an older
message; link both), independent (unrelated). Resolve conflicts by newest
message, then recheck live state before any write.
