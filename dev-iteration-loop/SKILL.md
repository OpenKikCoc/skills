---
name: dev-iteration-loop
description: "Choose a lightweight engineering loop when repeated failures, costly validation, unclear direction, or production effects need explicit control, and keep one root session advancing across milestones and compaction when state cannot be cheaply reconstructed. Ordinary clear local edits need no workflow ceremony."
---

# Iteration Loop

Trust model judgment. Execute the next useful action before another plan,
status-only update, receipt, or decomposition. Use one orchestration owner;
other skills provide domain operations, not duplicate lifecycles.

## Choose the smallest loop

- **L0 — clear local work:** inspect the relevant code, make the smallest
  coherent change, run focused verification, inspect the diff. No ledger,
  formal hypothesis, advisor invocation, or full suite merely for using a skill.
- **L1 — uncertain direction or expensive validation:** name the goal, main
  uncertainty, cheapest discriminating check, and expected change. A coherent
  multi-file fix is fine; isolate variables in experiments, not file counts.
- **L2 — repeated ambiguity or hard boundaries:** freeze scope, baseline and
  acceptance; reproduce; state a prediction and falsifier; run one attributable
  experiment; broaden validation only after the focused result supports it.
  Use for irreversible effects, production/security/data changes, or interacting
  control planes whose state cannot be reconstructed safely.

Read [mode-selection.md](references/mode-selection.md) only when classification
is unclear. `scripts/iteration_advisor.py` is advisory, never a prerequisite.

These L0/L1/L2 loop levels are unrelated to the L0-L3 roles used by
`fleet-dispatch`; fleet dispatch cards, workers, reviewers and coordinators
are tracked there, so do not build a parallel control packet for them.
A task's size, one writer plus read-only checks, or ordinary compact alone does
not require L2 or persistent state.

## Execute and converge

Before adding a control, name the failure it prevents. Omit it if absent or
already covered. Reuse passing evidence for unchanged source, command,
environment, and claim; planning, mirror sync, and unrelated docs do not
invalidate it.

Once direction is known and each ready acceptance gap has an owner or result,
stop discovery and generic review. Integrate, close concrete failures, run the
smallest remaining gates, and finish. Another worker must own an unassigned
gap. De-escalate after cause isolation; if coordination repeatedly exceeds
implementation, preserve work and continue under a simpler owner.

After a failed contextual edit, reread the exact symbol and strengthen its
anchor before retrying. Repeated blocks need a unique local invariant and a
scoped diff check. Before introducing shadow state, a fallback state machine,
or a full scan, inspect canonical lifecycle hooks and test the explanation
that would require it. Reuse one owner for recurring invariant mechanisms.

## Load contracts only at their boundary

| Boundary | Read |
|---|---|
| Choosing or changing L0/L1/L2; opaque harness failures | [mode-selection.md](references/mode-selection.md) |
| Claiming a performance delta | [exact-sha-benchmark-contract.md](references/exact-sha-benchmark-contract.md) |
| Attributing a regression across runs or releases | [validation-provenance-contract.md](references/validation-provenance-contract.md) |
| External write may commit despite a lost response | [external-write-transaction-contract.md](references/external-write-transaction-contract.md) |
| Durable long-session packet, compact continuation, recovery | [long-session-control.md](references/long-session-control.md) |
| Why the long-session protocol is shaped this way (liveness vs accuracy) | [long-session-design.md](references/long-session-design.md) |

Define what each gate proves and what remains untested. Derive integrity fields
from authoritative bytes, not model-authored completion claims. Pin the deployed
version and configuration before reasoning about runtime behavior. Resolve an
uncertain provider contract from exact-version docs, registered schema, then a
minimal probe; do not stack speculative compatibility patches.

Give expensive stochastic validation one owner per artifact/configuration/claim.
Reuse its matching receipt; post-install checks verify loaded runtime facts,
not an unnecessary rerun. Never retry an unknown external write before
reconciling authoritative state and proving retry safety.

## Long-running sessions

Keep one active host goal for the full user outcome across milestones and
compactions; never emit a final answer whose substance is only "I will
continue". After ordinary compact, reread Git, the plan, and running processes,
then execute the saved `next_action` in the same turn. Create the durable
contract/state/journal packet (`scripts/session_state.py`) only for parallel
writers, external effects, or state that cannot be rebuilt from current
authorities; recover (`disarm -> recover -> rearm`) only for real ownership,
authority, worker, effect, or intent anomalies. If the host or harness already
tracks the session's workers and goal, do not build a parallel packet. Details, new
instruction classification (CLARIFY/ADD/PAUSE/SUPERSEDE/INDEPENDENT), effect
guards, hooks, and handoff: [long-session-control.md](references/long-session-control.md).

## Compose only when needed

- Multiple writers or ambiguous integration: `fleet-parallel-merge`
  (isolated worktree per writer, one integration owner).
- Worktree/cache ownership and cleanup: `dev-disk-budget`.
- Remote CI repair loop: `dev-ci-fix-loop`.
- Remote work that must survive SSH loss: `fleet-remote-runner`.
- Fleet dispatch cards and worker/reviewer sessions: `fleet-dispatch`.
- Production diagnosis: the relevant domain skill and exact live evidence.
- Material correction, repeated mechanism, or requested retrospective: `meta-retrospective`,
  only off the critical path. A routine error or successful
  milestone is not a review trigger.

## Hard boundaries

Preserve user changes. Never fabricate test, runtime, production, or SHA evidence.
Obtain authority for irreversible, production, permission, and credential
side effects. Verify the claimed surface before completion, report gaps, and
stop patching when evidence contradicts the explanation.
