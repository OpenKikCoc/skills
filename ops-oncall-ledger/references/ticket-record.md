# Ticket record (schema_version 1)

One JSON file per ticket at `LEDGER/tickets/<ticket_id>.json`; the file name must
equal `ticket_id`. `ledger.py new` writes a valid draft.

| Field | Meaning |
|---|---|
| `schema_version` | `1` |
| `ticket_id` | stable key, `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` |
| `mode` | `investigate-only` or `investigate-and-rollup` |
| `priority` | `P0`..`P4` |
| `status` | `resolved`, `in_progress`, `stalled` |
| `title`, `date`, `category`, `environment` | identity and grouping (`date` is ISO, decides the ISO week) |
| `reporter`, `ticket_url` | optional |
| `executive_summary`, `impact` | 1-3 sentences; what broke, why, current status; confirmed scope |
| `symptom` | list of strings |
| `source_evidence` | `{locator, complete}`: where the event stream lives and whether it was read in full |
| `evidence_catalog` | `[{id, kind, scope, source, summary}]`; stable IDs |
| `facts` | `[{claim, evidence:[ids]}]`; at least one ref each |
| `hypotheses` | `[{statement, status: open/confirmed/rejected, falsifier, evidence}]` |
| `root_cause` | `{status, summary, evidence_refs}`; status `unknown/suspected/partially_confirmed/confirmed` |
| `recovery` | `{plan_state, execution, verification, checks:[{description, passed, evidence_ref}]}` |
| `rca` | `{publish_status draft/published, agent_used, accuracy, accuracy_basis, agent_summary, material_corrections}` |
| `timeline` | `[{time, event, evidence}]` |
| `action_items` | `[{item, owner, status}]` |
| `open_questions` | required while root cause is unknown or suspected |
| `write_authorization` | `{policy: preview/apply, weekly_id}` |

Evidence `kind` is free text except `post_action_observation`, which is the only
kind that lets a recovery check verify recovery. Evidence `scope` is free text
except `symptom_only`, which can never confirm a cause (a lag or error count
shows something is wrong, not why).

Recovery states: plan `unknown | proposed | blocked | feasibility_verified |
owner_verified | stale`; execution `not_started | executing | failed |
completed`; verification `not_started | failed | verified`. A plan goes `stale`
when the target, generation or episode it was written for has changed.

`material_corrections` entries: `{kind: material_fact|actor|resource_kind|
important_omission, previous_value, corrected_value, evidence_refs}`. Use them
for evidence-backed semantic corrections to agent-authored analysis, not copy
edits. After a correction, re-render the RCA and re-run the roll-up so the
accuracy metrics update.

Never put credentials, private payloads or raw full chat exports in a record.
Keep live records out of the skill repository.

## Event stream

Optionally keep exported history beside the ledger (for example
`events/<ticket_id>.jsonl`, append-only). The record cites it through
`source_evidence.locator` and evidence `source` fields. Do not edit exports; add
new evidence entries instead.
