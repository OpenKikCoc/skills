---
name: ops-oncall-ledger
description: "Keep oncall investigations as an evidence-traceable local ledger: one schema'd JSON ticket record per ticket as source of truth, a per-ticket Markdown RCA rendered from it, and a weekly report compiled idempotently from records (totals, priority counts, AI-assist coverage and accuracy recomputed, never hand-edited). Use to investigate an oncall thread or alert history, write or update a ticket RCA, or produce a weekly roll-up, and to separate investigate-only work from authorized reporting. Not for live incident mitigation or for platforms that need their own API writer."
---

# Oncall Ledger

Chat and alert history is the **event stream**. One ticket record (JSON) is the
**source of truth**. The per-ticket RCA (Markdown) is the **investigation view**
rendered from it. The weekly report is a **compiled roll-up** recomputed from
records. Never summarize a chat directly into the weekly report, and never
hand-edit derived numbers.

```text
chat / alerts  ->  ticket record  ->  rca/<id>.md  ->  weekly/<week>.md
 (events)        (truth, tickets/)    (view)            (compiled, idempotent)
```

## Modes

- **investigate-only** (default). Read the history, investigate, keep the record
  and preview the RCA on stdout. Write nothing a reader treats as published, and
  never touch the weekly report. A bare chat or ticket locator means this mode.
- **authorized reporting.** Only when the user explicitly authorizes writing
  (`write_authorization.policy=apply`) and names the weekly it joins
  (`mode=investigate-and-rollup`). Publish the RCA first, then roll up.

Never infer weekly authorization from the existence of a weekly report.

## Workflow

1. **Bind identity.** Choose a stable `ticket_id`; aliases (job, deployment,
   message IDs) are not separate tickets. Record where the history lives
   (`source_evidence.locator`) and whether you read all of it
   (`complete`). Pagination completeness gates publication, not investigation.
2. **Scaffold.** `python3 scripts/ledger.py new LEDGER TICKET_ID` creates
   `tickets/<id>.json`. Keep raw chat exports outside the repo; store locators
   and concise evidence statements, never credentials or personal data.
3. **Investigate** with [rca-framework.md](references/rca-framework.md). Add
   typed `evidence_catalog` entries and cite them from facts, hypotheses, root
   cause, and recovery checks. Unknown is a valid result; record the open
   question and the next discriminating check.
4. **Validate.** `python3 scripts/ledger.py validate tickets/<id>.json`.
5. **Render the RCA.** `python3 scripts/ledger.py render-rca tickets/<id>.json`
   previews on stdout. `--write LEDGER` writes `rca/<id>.md` and is refused
   unless `write_authorization.policy=apply`.
6. **Stop here for investigate-only.**
7. **Roll up when authorized.**
   `python3 scripts/ledger.py rollup LEDGER --week 2030-W05 [--write|--check]`.
   It refuses (fail closed) if any in-week authorized ticket is invalid or
   unpublished; re-running on unchanged records yields `noop`; `--check` exits 1
   when the stored weekly drifts from the records. Edit the record, then
   re-render, never the weekly.

Tests: `python3 -I scripts/test_ledger.py`.

## Hard gates

- Confirmed root cause needs evidence refs that exist in the catalog and are not
  `symptom_only`.
- `resolved` needs an owner-verified plan, completed execution, verified
  recovery, and a passed check backed by `post_action_observation` evidence. A
  known fix, blocked dry-run, or handoff is not resolved.
- Keep diagnosis, recovery plan, execution, and verification as independent
  states ([ticket-record.md](references/ticket-record.md)).
- AI accuracy values: `confirmed`, `partially_confirmed`, `incorrect`,
  `unreviewed`. Only reviewed tickets enter the accuracy denominator. A material
  correction (fact, actor, resource kind, important omission) must cite evidence,
  change the old value, and drop accuracy below `confirmed`.
- Do not roll up a draft RCA or a record without complete source evidence.
- Preserve `unknown` instead of inventing owners, impact, times, or causes.
- Production remediation is outside this skill; diagnosis does not authorize it.

## Staged writes and dependencies

Treat every external write as an immutable intent with a pre-read, an
`outcome_unknown` state after any ambiguity, and a reconcile step; absence after
an unknown outcome is not permission to replay. Before using an external CLI,
record its version and probe `--help` for each flag; missing guidance stops that
step. Details: [staged-writes.md](references/staged-writes.md).

## References

- [ticket-record.md](references/ticket-record.md) - schema and enums.
- [rca-framework.md](references/rca-framework.md) - RCA sections and method.
- [weekly-rollup.md](references/weekly-rollup.md) - what the weekly contains,
  metric definitions, idempotence.
- [staged-writes.md](references/staged-writes.md) - external-write vocabulary
  mapping and dependency receipt.
