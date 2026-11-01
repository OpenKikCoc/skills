# Long-Session Control

Read this only when work must keep one root session advancing across
milestones or compaction (see the "Long-running sessions" section of
[SKILL.md](../SKILL.md)). `<skill-root>` below is the installed
`dev-iteration-loop` directory; the packet still lives under
`<workspace>/.claude/long-session/`.

Keep an unfinished goal producing work while preserving its exact execution cursor outside lossy conversation context. Treat ordinary compact as a transparent epoch boundary, not as completion, pause, or project-state change.

Design rationale (liveness vs accuracy, why three files): [long-session-design.md](long-session-design.md).

Read [state-contract.md](long-session-state-contract.md) before initializing, continuing, or repairing a control packet. Read [templates.md](long-session-templates.md) when drafting a contract, classifying a new instruction, dispatching a worker, or interpreting a continuation/recovery receipt.

## Choose the lightest level

These L0–L2 levels describe state depth only. If the host or harness already
launches and tracks workers and reviewers for the task, do not build a parallel
control packet for them.

- Use **L0** for short or cheaply reconstructable work, including several small
  milestones whose state is already clear from Git, the current plan, and
  running process output. Do not create persistent state.
- Use **L1** when a likely compact needs liveness but the work remains cheaply
  reconstructable. Use one active host goal and one executable `next_action`;
  do not create the three-file control packet.
- Use **L2** only for parallel writers, external or irreversible effects, or
  state that cannot be safely reconstructed from current authorities. Create
  the control packet and add only the isolated scopes or effect records needed
  by that risk.

Escalate to a dedicated harness instead of expanding this skill when work needs a background scheduler, cross-process wakeup, shared worker queue, durable mailbox/lease/ack, complex replay, remote compensation, or multi-tenant authorization.

## Bind liveness to the goal

Use two independent layers:

```text
active host goal             -> another work turn must exist
contract + state + journal   -> that turn resumes the right work
```

When the host exposes a durable goal and the user explicitly asks to continue, finish, or manage a long-running task, create one goal for the full user outcome. Keep it `active` across milestones and compactions. Mark it complete only after all required acceptance is proved; mark it blocked only under the host's true-blocker policy. Do not create one goal per milestone.

If no host goal is available, keep working in the current turn and do not emit a terminal answer merely to announce future work. The control packet preserves accuracy but cannot itself wake a terminated host turn.

While the goal is active:

1. Read the current cursor and select its single `next_action`.
2. Execute that action, or a concrete part of it, before reporting another continuation.
3. At L2, checkpoint only at a durable boundary: a commit, ownership change,
   external-effect transition, compact/restart boundary, or material change to
   acceptance or the next action.
4. Continue without waiting for another user prompt.

Use commentary for progress. Never emit a final answer whose substance is only “I will continue”, “next I will”, a recovery receipt, or a plan. A final answer is valid only when the goal is complete, genuinely blocked, explicitly paused by the user, or the user asked only for an interim answer.

## Establish the durable cursor at L2

Do not initialize this packet for L0/L1 work. A host goal plus the current Git
and process state is sufficient when the next action can be reconstructed
cheaply.

1. Resolve the workspace and narrow fact authority: artifact hash, scoped Git tree/blob SHA, runtime PID/argv/config, API revision, or another rereadable source. Use repository `HEAD` only when the whole checkout is relevant.
2. Draft the user outcome, easily lost design intent, hard constraints, non-goals, milestones, deliverables, and testable acceptance.
3. Initialize one control packet and one executable next action:

   ```bash
   python3 <skill-root>/scripts/session_state.py \
     --workspace '<workspace>' init \
     --contract-file '<contract-draft.json>' \
     --root-session-id '<root-session-id>' \
     --next-action '<one executable action>' \
     --authority-kind '<kind>' \
     --authority-value '<value>'
   ```

4. Run `validate` and read `status`. Treat the contract as the durable projection of intent, never as authority over a newer user instruction.
5. Keep the host's plan or todo tool (for example Claude Code's todo list) as a current-turn view only. Transcript, compact summary, memory, plan, todo, and worker text are navigation aids, not completion evidence.

## Operate one milestone at a time

Keep exactly one foreground milestone and one `next_action`. Work in one mode at a time:

- `DIRECT`: perform small, bounded work in the root session.
- `ORCHESTRATE`: dispatch independent workers with material parallel value.
- `REVIEW`: verify receipts and acceptance evidence.
- `RECOVERY`: reconcile a real uncertainty; do not advance ordinary work.

Persist only information needed after context loss: decisions that change interpretation, evidence for acceptance IDs, worker lifecycle events, and effect intent/outcome pairs. Do not log every read, model message, or local edit. Git and the current workspace remain fact authority for ordinary code changes.

Switch from `ORCHESTRATE` to `REVIEW` as soon as every ready acceptance gap in
the milestone has an owner or result. While converging, do not spawn discovery,
general review, or speculative follow-up workers. Accept or reject existing
results, integrate them, run the remaining gates, and close the milestone. A
new dispatch must name one currently unowned acceptance gap and the material
wall-clock saving it provides.

At L2, update state through `checkpoint --patch-file ... --expected-revision ...`. The script uses a lock, compare-and-swap revision, atomic state replacement, and invariant validation. Do not checkpoint routine reads, local edits, status messages, or every test invocation. Before completing a milestone, record fresh evidence for every required acceptance ID. A worker's `complete` status alone is never evidence.

## Continue through ordinary compact

After compact, first determine whether the host goal is still active. At L0/L1,
reread Git, the current plan, and any running process, then execute the saved
`next_action` without constructing a recovery packet. At L2, reread the control
packet and current narrow authority. When the packet remains `ACTIVE` and
armed, authority is unchanged, and no operational blocker exists, run:

```bash
python3 <skill-root>/scripts/session_state.py \
  --workspace '<workspace>' continue-after-compact \
  --expected-revision '<state-revision>' \
  --actor '<root-session-id>' \
  --goal-status active \
  --authority-kind '<kind>' \
  --authority-value '<value>'
```

This increments the context epoch without disarming. It returns the objective, success outcomes, hard constraints, foreground milestone, required acceptance, and single next action. Execute that `next_action` in the same continued turn. Do not stop at the receipt, a commentary update, skill rereads, status checks, or a future-tense final answer.

Ordinary compact does not require instruction reclassification when no new real user instruction arrived. It does not invalidate evidence or worker state by itself.

## Recover only real anomalies

Use `disarm -> recover -> rearm` only for one of these conditions:

- session resume or fork with uncertain execution ownership;
- contract revision mismatch or ambiguous new user intent;
- workspace/runtime/API authority drift;
- corrupt or incomplete control packet;
- lost/conflicted worker or conflicting writer;
- unclosed or unknown external effect;
- inactive host goal despite unfinished state.

While disarmed, allow only reads, packet repair, journal reconciliation, and necessary clarification. Do not edit business files, commit, deploy, send an external message, or dispatch a write worker.

Recover in this order: reread contract and state; inspect journal changes; reread current authority; invalidate stale evidence; reconcile workers and effects; classify new real user instructions; run `recover`; resolve all blockers; then let only the root run `rearm` and `assert-ready`. After rearm, immediately execute the recovered `next_action`; recovery is a bridge, not a result.

## Classify new user instructions

- `CLARIFY`: refine current meaning without adding work.
- `ADD`: add deliverables or acceptance while preserving the main line.
- `PAUSE`: save the foreground cursor and return condition, then activate a bounded temporary milestone.
- `SUPERSEDE`: explicitly replace an objective, constraint, or milestone and increment contract revision.
- `INDEPENDENT`: perform a bounded side task, then return to the saved foreground milestone.

If a scope-changing classification stays ambiguous, disarm and ask. Never let the latest message silently replace the project.

## Delegate bounded work

Keep simple or tightly coupled work in the root session. Delegate only independent work with a clear base authority, read/write scope, acceptance evidence, and non-goals. Default workers to read-only; give each writer an isolated worktree, and run remote processes that must survive observer disconnection under a detached supervisor (for example `tmux` or `systemd-run`).

Require the receipt schema in [templates.md](long-session-templates.md). A greeting, plan, “started” message, or worker completion flag is not a result. The root reviews receipts against current authority and owns milestone acceptance. A fork gets a new execution ID and never rearms or writes the root control packet.

Do not let one L2 controller grow indefinitely. After repeated compactions,
completed waves, resource pressure, or coordination overtaking implementation,
stop dispatch at a durable boundary and write one minimal handoff: objective,
hard constraints, authority, open acceptance gaps, active workers/effects,
preserved commits, and one next action. Start a fresh primary only after the
old one stops dispatching; never copy the transcript or stale worker ledgers.

## Guard external effects

Before an external or difficult-to-reconstruct mutation, persist and fsync an effect intent with `effect prepare`. Use its `call_id` as an idempotency key when supported. After the action, reread external truth and use `effect finish` with `confirmed`, `failed`, or `unknown`. Never retry an unknown outcome blindly.

Run `assert-ready --effect-id <id>` immediately before the prepared mutation. This authorizes that one open intent while blocking unrelated writes.

## Hook integration

Hooks are optional accelerators, never the source of truth or a complete
effect detector: `PostCompact` runs the continuation check (no disarm);
`SessionStart` disarms only a resumed/forked session with uncertain ownership;
`UserPromptSubmit` classifies real instructions (not injected summaries);
`PreToolUse` calls `assert-ready` for identified writes; `SubagentStart/Stop`
require a registered dispatch; `Stop` resumes when an active goal is still
actionable.

## Finish or hand off

Before completing the goal, run `validate`; verify fresh evidence for all required acceptance; confirm no unclosed effect or lost/conflicted worker; reread actual deliverables or external state; and set the session `COMPLETE`. Then mark the host goal complete and send the final outcome.

If genuinely blocked, checkpoint the exact blocker and next action before using the host's blocked transition. Keep credentials and human identity in the root process; worker receipts may contain opaque references but never secrets.
