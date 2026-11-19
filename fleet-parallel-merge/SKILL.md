---
name: fleet-parallel-merge
description: "Coordinate several repository writers on one change: immutable base SHA, disjoint write sets, controller-only main workspace, liveness replacement, ordered integration, restart recovery, and safe cleanup, with pure JSON advisor scripts. Use only when two or more writers, overlapping ownership, multi-commit integration, or loss-sensitive state is involved. Not for the normal one-card-one-worker case (use fleet-dispatch) or a single writer."
---

# Fleet Parallel Merge

## Default path

One task, one worker, one worktree: follow `fleet-dispatch` (its current card
and mandate own worker/reviewer lifecycle, review, testing and landing policy)
and do not create this skill's multi-writer packet. Parallelism alone does not
justify more process. With at most one repository writer (everyone else
read-only or writing disposable task-local artifacts), record only the base
SHA, one owner and scope per task, and one acceptance criterion per task. Do
not create plan/handoff JSON, reader worktrees, or cleanup manifests there.

Use `dev-disk-budget` for worktree and cache reservation and cleanup,
and `dev-ci-fix-loop` for remote CI.

## When to enter this protocol

Two or more repository writers on one change, overlapping or uncertain
ownership, multi-commit integration, substrate recovery, or state whose loss
would make integration ambiguous. Collapse back to the default path as soon as
one slice and one authorized writer remain and accepted state is recoverable
from Git. Another session editing the same checkout counts as another writer.

## Non-negotiable rules

- One immutable full base SHA for every worker; accept only a single result
  commit whose observed parent and merge-base equal it.
- Disjoint repository-relative write sets (exact paths or `dir/**`); merge or
  serialize tasks that cannot be made disjoint.
- Partition by end-to-end acceptance slices, not components or activity. Do not
  spawn workers for exploration, duplicate review, or status the controller can
  read. Once every ready slice is owned, stop fan-out and converge.
- The main workspace is controller-only; stop if its HEAD or clean status
  changes before integration.
- Scripts are pure decision layers over JSON snapshots: they never run Git,
  create worktrees, spawn workers, integrate, or clean. Never claim they did.
- Host status (`running`, `accepted`) is not progress. After two accepted
  follow-ups with no task-scoped progress signal, treat the execution as lost,
  prove it stopped, allow at most one replacement in a new worktree, then
  collapse to one named owner or block.
- A failed spawn is not a capacity wait and never authorizes another runtime.
  A provider-side queue after acceptance is different from local capacity
  limits; see the protocol note.
- Integrate accepted commits one at a time in plan order with focused checks;
  never force cleanup; clean only preserved, clean, accepted workers via
  `dev-disk-budget`.
- Checkpoint long waves outside disposable paths so a restart can resume or
  safely reconstruct.

## Scripts (pure advisors, stdlib only)

| Script | Purpose |
|---|---|
| `parallel_merge.py plan/validate/handoff/digest` | DAG, write sets, snapshot validation, integration order, `plan_sha256` |
| `worktree_context_attestation.py` | Bind worker CWD/HEAD/write-set/path operands to the assigned worktree |
| `worker_liveness_advisor.py` | Lost-execution classification and single replacement |
| `restart_checkpoint.py checkpoint/resume-validate` | Interruption-safe wave state |

Run each with `--input file.json [--output out.json]`; `--help` lists options.
Tests: `python3 -I scripts/test_<name>.py` from the skill directory.

## References

- [references/multi-writer-protocol.md](references/multi-writer-protocol.md): procedure, dispatch, cleanup, restart recovery
- [references/contracts.md](references/contracts.md): JSON schemas for plan, execution, validation, handoff, attestation
- [references/worker-liveness.md](references/worker-liveness.md): liveness packet boundary and replacement budget
