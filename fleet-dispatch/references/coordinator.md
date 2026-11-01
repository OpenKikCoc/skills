# Coordinator (L2) playbook

The coordinator orchestrates; it does not do the work. It reads state, decides
one control action per item, records it, and goes idle.

## Event-driven turns

The coordinator never polls and never loops. The platform (`fleet.py watch
--once --wake`, run from a timer) detects events and wakes the coordinator
session once per batch, with one line per event:

| Event | Coordinator action |
|---|---|
| `state:awaiting-verify` | run `verify`; on pass, launch a reviewer |
| `state:blocked` | read Progress; answer from the card, steer, or open a decision |
| `review:PASS` | land the bound SHA |
| `review:REWORK` | `rework` with the reviewer's requirements; consider escalation |
| `session:exited` / `session:idle` without a terminal state | check receipts, then resume once or relaunch |
| `session:stale` (no HANDOFF update past timebox/2) | inspect evidence of progress before steering |
| `context:low` | platform relaunches from HANDOFF; coordinator only checks the result |
| `drift` (remote branch ≠ manifest SHA after awaiting-verify) | send back to rebind the manifest |
| `decision:answered` | `decide-poll --ack`, then act on the answer |
| `dispatch:ready` (dependencies done) | launch, if within concurrency limits |

Each event wakes the coordinator once (the key is persisted). The reference
CLI emits `state:*`, `session:exited`, `session:stale`, `drift`, `review:*`,
`decision:*`, `result_unknown`, `land:*`, `host_job:failed`, and `request:*`;
`session:idle`, `context:low`, and `dispatch:ready` need runtime-specific
telemetry and are left to your platform. After handling
the whole batch, the coordinator appends to its journal and ends the turn.

### One bounded batch per turn

Combine independent reads (status of several items) into one step. Take at
most one control action per item per turn, each with its receipt. Do not batch
steps that depend on a previous step's meaning, irreversible writes, or
retries of unknown results.

### Idle is not stalled

An idle worker may be waiting on a provider queue, a long build, or a host job.
Before classifying a stall, check the actual command, build, gate, or job
receipts. A provider-accepted capacity wait is never a reason to steer,
interrupt, relaunch, or downgrade.

## Dispatch

Dispatch only items the human scoped or whose dependencies just completed and
that policy marks auto-dispatchable. Respect concurrency limits per repo and
overall (`fleet.json: limits`). Keep one slot free for review and recovery.
One card, one worker, one branch; parallel writers on one change need separate
cards with disjoint write sets.

## Steering rules

- One steer fixes one thing, stated concretely: what is wrong, the evidence,
  the expected change. "Please be careful" is not a steer.
- At most 2 steers per session per 30 minutes (enforced by `fleet.py steer`).
  A third need means the card is wrong: open a decision or re-slice.
- Third fix of the same failure class: require a one-page design first.
  Fourth: split the card or change the design; stop patching.
- Prefer escalating effort (effort-routing.md) over a third steer when the
  failures show reasoning gaps rather than missing information.

## Decision triage — the only way to reach the human

| Tier | Use for | Who acts |
|---|---|---|
| `self` | anything answerable from the card, repo, or evidence | coordinator, now |
| `notify` | the human should know, nothing to decide | coordinator acts, platform notifies |
| `human_required` | personal authorization, irreversible production risk, product trade-offs, explicit live acceptance | the human |

`human_required` must carry context, options with consequences, a deadline,
and the default applied if unanswered. Items that are blocked by an open
decision stay blocked; other items continue.

Never answer a decision on the human's behalf, and never treat silence as
approval unless the declared default was explicitly safe.

## Write boundary

The coordinator does not edit business files, commit, build, or deploy through
generic tools. Its only effects are fleet commands (`launch`, `steer`,
`resume`, `rework`, `escalate`, `land`, `decide-*`, `mark`), each of which
leaves a receipt. Corrections go through `steer`/`rework`, not side files.

## Journal

After each turn, append one entry: events handled, actions taken with
receipts, decisions opened, and anything left for the next turn. The journal
plus the HANDOFFs is enough to relaunch the coordinator from scratch.
