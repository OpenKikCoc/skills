---
name: fleet-dispatch
description: "Run a supervised fleet of coding-agent sessions from dispatch cards: a deterministic platform (L0), workers and independent reviewers (L1), an event-driven coordinator (L2), and an optional remote supervisor (L3). Use to dispatch work with complexity-matched reasoning effort, steer and escalate sessions, verify evidence, review, land exact SHAs, triage human decisions, and supervise the fleet from another host. Not for a single interactive edit."
---

# Fleet Dispatch

A protocol for running several coding-agent sessions (Codex CLI, Claude Code,
or any CLI with a headless mode) without letting any of them grade its own
work. The reference CLI `scripts/fleet.py` implements it on SQLite and Git with
the Python standard library only; the protocol stays valid with another backend.

## Roles

Numeric levels are display labels only. Authority comes from the canonical
role plus the capability bound to a session generation at launch.

| Display | Role | Owns | Never does |
|---|---|---|---|
| L0 | `platform` | deterministic services: watch, events, wakes, context relaunch, notifications | judgement, edits, approvals |
| L1 | `worker` | one card, in its own worktree and branch | land, change its own effort/model, touch other cards |
| L1 | `reviewer` | one verdict bound to a candidate SHA and manifest hash | edit the candidate, change item state |
| L2 | `coordinator` | dispatch, steer, rework, escalate, start reviewers, land, triage decisions | business edits, self-review, personal approvals |
| L3 | `supervisor` | long-horizon oversight from another host: status, steer, resume, rework, dispatch | land, mark done, approve for the human, block the fleet when offline |
| — | human | goals, trade-offs, authorization, final acceptance | — (not a system role; reached only through the decision inbox) |

Full authority matrix and generation rules: [roles.md](references/roles.md).

## Lifecycle

```text
card ──route──▶ launch worker (L1, effort from complexity)
  │                │ HANDOFF: state / progress / evidence
  │                ▼
  │     claims "awaiting-verify" ──▶ fleet verify (mechanical)
  │                                     ▼
  │                       reviewer (L1, different session) PASS | REWORK
  │                                     ▼
  └──── coordinator (L2) ── land exact SHA (ff-only) ── done
            ▲   events (state change, idle, stale, drift, decision answered)
            └── platform (L0) watch ──▶ attention log ──▶ supervisor (L3, pull)
```

1. **Write a card** from [the template](assets/dispatch-card.template.md):
   goal, acceptance, scope (repo/branch), gates, prohibitions, timebox, and the
   four complexity signals. The card is the worker's only task input. See
   [card-and-handoff.md](references/card-and-handoff.md).
2. **Route** reasoning effort from complexity, then **launch**:

   ```bash
   fleet.py init                                   # once: ~/.agent-fleet, fleet.json
   fleet.py card lint cards/W-12.md
   fleet.py route cards/W-12.md                    # tier, effort per runtime, reasons
   fleet.py launch cards/W-12.md --dry-run         # worktree, argv, env (no token)
   fleet.py launch cards/W-12.md --human           # or under a coordinator capability
   ```

   Effort is fixed per generation; only the coordinator changes it, by
   escalating to a new generation. Rules: [effort-routing.md](references/effort-routing.md).
3. **Run the coordinator event-driven.** `fleet.py watch --once --wake` (from
   cron or a systemd timer) emits events and wakes the coordinator once per
   batch; it handles the batch, writes its journal, and goes idle. It never
   polls. Playbook: [coordinator.md](references/coordinator.md).
4. **Gate writes.** Workers call `fleet.py preflight --action edit|commit|build
   --target PATH` before mutating; anything but the bound worker generation in
   its own worktree is denied.
5. **Verify, review, land.** A worker's "awaiting-verify" is a claim.
   `fleet.py verify` checks the manifest mechanically; a reviewer in a
   different session records a bound verdict; only the coordinator lands, and
   only that SHA. A non-code card, or a candidate that adds nothing to the base,
   closes through `fleet.py complete-verified` instead of `land`. See
   [review-and-land.md](references/review-and-land.md).
6. **Supervise from elsewhere (optional).** The L3 supervisor pulls the
   attention log over SSH (`fleet.py wait --after N`); the fleet never connects
   to it. See [supervision.md](references/supervision.md).

## Non-negotiable rules

- Evidence over claims: a state line, a summary, or "tests pass" is not proof.
  Bind every claim to a SHA, a hash, or a receipt.
- No self-review: the reviewer is a different session; the coordinator does not
  review what it dispatched alone.
- One steer fixes one thing; at most 2 steers per session per 30 minutes; the
  third becomes a decision or a re-slice. The third fix of one failure class
  starts with a one-page design; the fourth splits the card.
- Ask the human only through the decision inbox, tiered `self` / `notify` /
  `human_required`; the last needs context, options, a deadline, and a safe
  default. Never infer or relay a personal approval.
- Unknown identity, ownership, result, or effect fails closed. Never retry an
  effect whose result is unknown; record it and ask.
- Host-specific work goes through that host's pull queue with hashed receipts;
  failures create one rework item and never trigger automatic rollback.

## Related skills

| Skill | Role in the fleet |
|---|---|
| `dev-iteration-loop` | the worker's loop discipline (L0/L1/L2 *loops*, unrelated to fleet levels) and long-session packet |
| `fleet-remote-runner` | executor for host-specific queued jobs |
| `fleet-tui-probe` | read-only state probe for interactive TUI sessions |
| `dev-ci-fix-loop` | drive the landed branch's CI to green |
| `dev-code-review` | the reviewer role's standard for finding quality (evidence first, reproduce before claiming) |
| `fleet-parallel-merge` | exception path when several writers share one change (immutable base, disjoint write sets, ordered integration) |
| `dev-disk-budget` | reserve and safely clean worktrees, caches, and temp roots |

Integration details and runtime adapters (Codex, Claude Code, others):
[runtime-adapters.md](references/runtime-adapters.md). Running the CLIs on a
separate host (durable SSH sessions, personas and their qualification,
loopback dashboards, credential preflight, outer-transport trust):
[remote-runtime-host.md](references/remote-runtime-host.md).
