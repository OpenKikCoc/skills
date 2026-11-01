# Long-Session Design Rationale

Why the long-session control in
[long-session-control.md](long-session-control.md) is shaped the way it is. Read
it when deciding whether to deviate from the protocol, not for routine use. The
file formats and commands live in
[long-session-state-contract.md](long-session-state-contract.md).

## Goal

Let one root session carry a task across many milestones, several compactions,
ad-hoc interruptions, and bounded worker help, without losing the design intent
and without silently stopping while work remains.

## Two independent problems

```text
active host goal             -> liveness : unfinished work guarantees another turn
contract + state + journal   -> accuracy : that turn resumes the right goal, cursor, evidence
```

- A compact summary or checkpoint only answers "what do I remember after
  waking". It cannot make the host schedule another turn after a mistakenly
  emitted terminal answer.
- An active goal alone can keep executing the wrong objective.

Both layers are needed. When the host exposes no goal primitive, the only
liveness authority is the current turn: keep working and never end a turn by
announcing future work.

## Common failure mode

A common failure mode is a session that stalls and waits for the user to type
"continue". The cause is usually not "compact breaks things" (most compactions
resume normally). A common chain is:

```text
compaction -> first message is future-tense progress or internal recovery
thinking, marked as the final answer -> turn ends -> no active goal exists to
wake another turn -> idle until the user nudges
```

Consequences for the design:

- Do not patch with per-case "if stuck, continue" rules. The invariant is:
  while the goal is active, authority unchanged, and no blocker exists, take the
  next action from the durable cursor and actually execute it.
- A receipt, a skill re-read, a status check, a plan, or "I will continue" is
  never a terminal answer.
- When auditing stalls, first filter to genuine user requests, then read the
  event chain. Keyword searches over transcripts produce false positives from
  system text, skill bodies, subagent output, and business wording.

## Principles

1. One active goal covers the whole user outcome; do not split goals per
   milestone or worker.
2. One root session spans all milestones. Small, tightly coupled work is done
   directly; the root is not a manager-only role.
3. Ordinary compact is only an epoch boundary: stay `ACTIVE`/armed, read the
   continuation receipt, execute `next_action` in the same continued turn.
4. Enter `disarm -> recover -> rearm` only for real anomalies: uncertain
   resume/fork ownership, authority drift, uncertain worker/effect/intent, or a
   damaged control packet. Recovery is a bridge back to work, never a result.
5. Subagents take independent, bounded, parallelizable work and return a
   structured receipt. They never own top-level goal interpretation or root
   rearm rights, and their completion only moves a milestone to review.

## Three kinds of authority

| Kind | Question | Ordering (strongest first) |
| --- | --- | --- |
| Liveness | Must another turn exist? | host goal active / complete / blocked |
| Intent | What does the user want? | current system constraints, explicit new user instruction, confirmed contract projection, root plan, worker suggestion, compact summary or memory |
| Fact | What is true now? | freshly re-read repo/runtime/API, evidence bound to the current authority, worker receipt, plan, transcript or summary |

The contract persists intent but never outranks a newer user instruction.
Bind evidence to the narrowest authority that matches the claim (file hash,
scoped tree/blob SHA, PID plus argv plus config, API revision); use the whole
repository HEAD only when acceptance depends on the whole checkout.

## Why three files

- `contract.json`: objective, constraints, non-goals, milestones, required
  acceptance. Changes only on explicit user instruction (or unambiguous
  inference), each change bumping a revision and logging a decision.
- `state.json`: overwritable cursor (milestone, epoch, mode, armed flag,
  single executable `next_action`, suspension stack). Updated by
  compare-and-swap on a revision so a stale writer or a fork cannot clobber it.
- `journal.jsonl`: append-only, only events that affect future recovery
  (decisions, evidence and invalidations, worker lifecycle, effect
  intent/outcome, continuation/recovery/checkpoint). Ordinary reads and
  side-effect-free commands are not logged; Git and live systems remain the
  source of truth for ordinary engineering facts.

Deliberate non-goals: transcript copies, an event store or database, a
background scheduler, a durable mailbox or worker queue, model-driven compact,
checkpoint restore, exactly-once external effects, restoring OS or remote
processes. Hooks are triggers only; they hold no business state, and the
manual continuation/recovery path must work with hooks disabled.

## Lifecycle

```text
normal   : ACTIVE/armed -> compact -> continuation -> ACTIVE/armed -> run next_action
anomaly  : ACTIVE/armed -> anomaly -> DISARMED -> RECOVERING -> ACTIVE/armed
                                                       \-> WAITING_USER
complete : ACTIVE/armed -> all required acceptance proved -> COMPLETE
```

Session and goal span the user outcome; a milestone is an acceptable
deliverable; an epoch is the context between two compactions. Compact bumps the
epoch only: it does not change milestone, invalidate evidence, alter the goal,
or pause work. Only when every required acceptance has currently valid evidence
may the session and host goal complete.

## Design choices worth keeping

- The continuation receipt is an instruction to the agent, not a deliverable.
  If its predicate fails (inactive goal, non-root caller, authority drift,
  blocker) the command changes nothing; find the cause before recovering.
- While disarmed or recovering, only reads, packet repair, journal
  reconciliation, and clarifying questions are allowed; no business writes,
  commits, deploys, external messages, or new writer workers.
- External effects of meaningful risk get a flushed intent record (with a stable
  idempotency key when available) before execution, and a re-read outcome
  (`confirmed`, `failed`, `unknown`) after. An intent with no outcome, or an
  unknown outcome, is never blindly retried.
- New user instructions are classified (clarify, add, pause, supersede,
  independent); ambiguous classification that would materially change scope
  goes to `WAITING_USER` instead of letting the latest message overwrite
  history.
- Escalate to a dedicated orchestrator rather than growing this protocol when
  the need is a scheduler, cross-process wakeup, shared queue, leases, complex
  replay, compensation, or multi-tenant permissions.

## Prior art (summary)

Surveyed agent harnesses (Codex CLI, Claude Code, DeepSeek-style harnesses)
agree on invariants this design reuses: transcript, summary, and plan are not
project facts; resume does not restore runtime state; completion needs
re-readable evidence; workers fit bounded receipts; external effects must
tolerate unknown outcomes. Their larger machinery (thread graphs, workflow
replay, checkpoint restore, team mailboxes) is intentionally not copied: this
protocol stays single-machine, transparent, and repairable with the standard
library.

## Acceptance scenarios the tests mirror

One goal and root across three milestones; compact bumps the epoch and then
executes `next_action` with no new prompt; continuation fails closed without
mutating state; future-tense or receipt-only text is rejected as a terminal
answer; resume/fork and real anomalies go through disarm/recover/rearm; a pause
returns to its parent milestone; add and supersede both leave decisions;
authority change invalidates dependent evidence; worker completion only reaches
`awaiting_review`; a stale revision update fails; an intent without outcome is
`OUTCOME_UNKNOWN`; short tasks create no persistent state; a damaged packet
fails closed with a candidate repair instead of a silent overwrite.
