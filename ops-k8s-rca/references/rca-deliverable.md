# RCA deliverable

## Time-box

Start the clock at intake. Default to a 10-minute provisional brief: bind the
target (or report the blocker) by minute 2, launch independent read-only lanes
by minute 3, and at the deadline emit what is confirmed plus explicit
pending/blocked gaps instead of waiting for every lane. Lanes worth separating:
live object state, controller/component logs, node-side evidence, source and
version, recent changes.

## Structure

1. **Impact and timeline** - UTC, with source for each key transition.
2. **Live evidence** - each item with a stable ID, source command or locator,
   and observation time.
3. **Hypotheses tested** - including rejected ones, each with prediction,
   falsifier, check, result.
4. **Diagnosis brief** - episode, last authoritative stage, causal lanes,
   confidence, unknowns, the next read-only check.
5. **Recovery plan** - kept separate from diagnosis (see states below).
6. **Change attribution** - only when a change is temporally plausible.
7. **Residual risk and next checks.**

## Causal lanes

Classify findings so a trigger is not mistaken for the mechanism:

| Lane | Question |
|---|---|
| direct blocker | what stopped progress at the last good stage |
| trigger | what change or event started it |
| failure gap | what should have prevented or caught it |
| systemic gap | what organizational/design weakness allowed it |
| persistence gap | why it did not self-heal |
| retry symptom | noise produced by retries (do not chase) |

Each lane is `confirmed`, `observed`, `unresolved` or `not_applicable`. A lane
that is only `observed` supports at most a suspected cause.

## State vocabulary

- Diagnosis: `unknown -> suspected -> partially_confirmed -> confirmed`;
  applicability `current` or `historical` relative to the active episode.
- Recovery plan: `unknown | proposed | blocked | feasibility_verified |
  owner_verified | stale`. A capability or dry-run rejection makes the plan
  `blocked`; drift in target, generation or episode makes it `stale`.
- Execution: `not_started | executing | failed | completed`.
- Verification: `not_started | failed | verified`, backed by a passed
  post-action observation.

Having a plan is not recovery. Diagnosis being complete does not authorize a
production action. Keep an action ledger of who did what (actor, action, target,
time, before/after state) so ownership of side effects is never inferred from
object names.

## Unknown is a result

Preserve `unknown` and open questions instead of inventing owners, impact,
times or causes. For multi-agent handoffs, require each lane to return evidence
IDs, not conclusions alone, and re-verify any claim that crosses a trust
boundary against the primary source.
