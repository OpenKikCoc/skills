---
name: meta-retrospective
description: "Capture reusable improvements as evidence-backed proposals after a material user correction, a repeated failure mechanism, or an explicit retrospective request, without slowing the main task. Produces draft proposals to update a skill, project guidance, or memory; never edits them. Use for agent-neutral post-incident learning (Claude Code, Codex, others). Not for routine command errors, expected negative tests, transient timeouts, or successful milestones, and not a substitute for code review."
---

# Retrospective

A sidecar, not a phase of the main workflow. It turns a real lesson into a
reviewable proposal and gets out of the way. Output is a proposal with
evidence; nothing is promoted by the review itself.

## Admission

Review only when at least one holds:

- the user made a material correction that changes a reusable rule;
- the same failure mechanism occurred repeatedly (not just the same symptom);
- the user explicitly asked for a retrospective.

Not triggers: a routine command error, an expected negative test, a transient
timeout or network blip, a typo fix, a successful milestone.

Before drafting, check whether an existing skill, guidance file, or memory
entry already covers the point. If so, prefer a proposal to enforce or sharpen
that rule over a new entry; if it is already adequately stated, stop.

## Keep it off the critical path

- Defer until real progress or an unavoidable wait. Never delay the next
  implementation, validation, or delivery action to run a review.
- If the agent supports background workers, use exactly one read-only worker
  with bounded evidence (the corrections, the failing commands, the relevant
  rule files). The worker must not spawn another reviewer.
- If background workers are unavailable, do not review inline in the middle of
  a task: note that the review was skipped, or do it at a natural stopping
  point if the user asks.
- Do not redo the main diagnosis or code review, and do not scan broad raw
  history. For periodic multi-session audits use `meta-session-mining`.

## What to produce

One proposal per distinct lesson, with:

- a trigger (`user_correction`, `repeated_failure`, `retrospective`) and the
  mechanism, stated as a reusable discriminator rather than a story;
- 1-12 evidence items (quotes or observations, redacted: no secrets, home
  paths, or private identifiers);
- the smallest durable target that would prevent recurrence;
- the concrete proposed change and what existing rule you checked.

Target routing, smallest first (details in
[promotion-routing.md](references/promotion-routing.md)):

1. unverified or run-local observation: keep as a draft only;
2. project convention: that project's guidance file;
3. gap in an existing reusable workflow: update that skill;
4. narrow domain mechanism: merge into an existing casebook or reference;
5. durable personal or team fact: a memory entry;
6. new skill: only for a cross-project workflow with stable inputs/outputs,
   a distinctive trigger, testable behavior, and recurrence or explicit demand.

## Draft inbox

`scripts/draft_inbox.py` stores proposals as immutable, content-addressed JSON
in a local inbox directory (private file mode, atomic writes, deduplicated by
what the proposal asks for). Keep the inbox out of version control.

```bash
python3 <skill-root>/scripts/draft_inbox.py --inbox <dir> propose packet.json
python3 <skill-root>/scripts/draft_inbox.py --inbox <dir> list
python3 <skill-root>/scripts/draft_inbox.py --inbox <dir> decide \
  --draft <file.json> --decision deferred --note 'waiting for a second case'
```

Packet fields and rules: [proposal-packet.md](references/proposal-packet.md).
The script rejects secret-like values and absolute home paths, blocking
proposals, and unknown targets. Decisions (`promoted`, `rejected`, `deferred`)
are separate receipts; drafts never change.

## Boundary

A draft never authorizes an edit. A later foreground task reviews the inbox,
re-verifies evidence and recurrence, compares existing rules, gets whatever
write authorization the user requires, and then makes the change (for a
skills repository, with `meta-skills-repo`). The review itself may write
only its inbox: no edits to the active project, no commits, no mirror syncs.
The final user-facing summary may mention new drafts, but the inbox is the
record.

## Validation

```bash
python3 -I <skill-root>/scripts/test_draft_inbox.py
```
