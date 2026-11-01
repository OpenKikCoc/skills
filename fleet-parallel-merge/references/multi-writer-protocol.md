# Multi-Writer Protocol

Procedure for the exception path in [SKILL.md](../SKILL.md). `<skill-root>` is
the installed skill directory.

## Safety boundary

- The scripts are pure advisors. Never claim they created a worktree, spawned a
  worker, inspected Git, integrated a commit, or cleaned anything.
- One immutable full base SHA is shared by every worker. Accept only a single
  result commit whose independently observed parent and merge-base both equal it.
- Write sets are repository-relative POSIX paths: exact files or `dir/**`.
  Reject every other glob form. Keep write sets disjoint; merge or serialize
  tasks that cannot be made disjoint.
- Another session editing the same checkout is another writer. Use separate
  worktrees from the same base, or serialize ownership before either edits.
- The main workspace is controller-only. Stop before integration if its HEAD or
  clean status changed after the initial snapshot.
- Never force cleanup. Refuse it for dirty, interrupted, failed, unaccepted, or
  unpreserved workers. A boolean "integrated" claim is not preservation proof.

## 1. Freeze the controller

1. Confirm the main workspace is clean; record its full base SHA and a clean
   initial snapshot.
2. Define tasks with stable IDs, dependencies, summaries, write sets, and
   deterministic acceptance commands or criteria. Start from end-to-end
   acceptance slices: the smallest independently verifiable outcome that can
   turn green across all required boundaries. Component edits, test-writing,
   review, and docs are work inside a slice, not separate progress units.
3. Run every acceptance item against the immutable base before dispatch and
   record which are already red, who may repair them, and which wait on
   dependencies. Worker repair paths must fit the named owner's write set.
4. Reserve every worktree path and task-owned cache/artifact root with
   `dev-disk-budget` before dispatch; do not retroactively adopt existing
   paths for cleanup. Use one session-level manifest for shared build caches and
   one disposable manifest per wave.

## 2. Plan

```bash
python3 <skill-root>/scripts/parallel_merge.py \
  plan --input plan-input.json --output plan.json
```

Planning rejects missing dependencies, cycles, duplicate IDs, invalid paths,
overlapping ownership, and empty acceptance contracts, and emits deterministic
parallel stages plus a stable topological integration order. Set
`worker_substrate` (the execution runtime workers use) before dispatch; a
dispatch failure may not silently change it. Use the `digest` action to get
`plan_sha256` for downstream advisors.

## 3. Dispatch isolated workers

Dispatch only work that closes a named, unowned acceptance slice and has
material parallel value. Track each slice as unowned, owned, accepted, or
blocked. When every ready slice is owned, stop fan-out and converge: review,
integrate, run focused gates.

Create one worktree per task from exactly `base_sha`. Give each worker its task
summary, exact write-set allowlist, acceptance contract, and the requirement to
commit and report: final full SHA, parent SHA, merge-base SHA, commit range,
changed files, clean status, and acceptance evidence. Collect these
independently; do not trust "tests passed" prose.

A failed spawn is not a capacity wait and does not authorize another runtime.
If the backend never accepted the request, retry on the sealed substrate. A
cross-substrate retry needs explicit authorization and a newly sealed plan.
Record each dispatched worker's task ID, substrate, external job ID, and the
owner who will reclaim it; an untracked external worker cannot be accepted.

Provider capacity wait versus local capacity: if the provider accepted a request
and reports it queued, shrinking your own fan-out does not speed the queue, so
keep accepted work and dispatch more ready independent slices only while local
headroom (threads, disk, execution channels) remains, keeping one slot for
integration and recovery. Rate-limit/quota errors, local limits, and low disk
are not queueing: back off or release resources.

Before every mutation or validation follow-up, run
`scripts/worktree_context_attestation.py` to bind observed CWD, Git top-level,
HEAD, plan digest, write-set digest, command root, and path operands to the
assigned worktree. The controller computes digests and observations itself.
Require `status: ready` (flat schema: see [contracts.md](contracts.md)).

## 4. Liveness

A host-reported `running` or `accepted` is not progress. Within a review window
require a task-scoped acknowledgement or a controller-observed change in the
assigned worktree's HEAD, file-state digest, or receipt digest. After two
accepted follow-ups with none, classify the execution as lost, preserve and
inspect its worktree, prove its writer stopped, then allow exactly one
replacement (new execution ID, new non-overlapping worktree, same plan, base,
task, write set, substrate). If the replacement stalls too, stop re-spawning:
collapse the slice to one named, authorized owner or block it. See
[worker-liveness.md](worker-liveness.md).

## 5. Validate and hand off

```bash
python3 <skill-root>/scripts/parallel_merge.py \
  validate --input execution.json --output validation.json
python3 <skill-root>/scripts/parallel_merge.py \
  handoff --input execution.json --output handoff.json
```

Validation rejects a worker that started from another base, stopped early,
touched files outside its allowlist, retained dirty state, lacked evidence, or
failed acceptance, and rejects the whole run if the main workspace was dirty at
start or changed before integration. For parallel semantic migrations, disjoint
files are not enough: give each cross-slice invariant one owner and a
controller-owned integration check.

Integrate accepted commits one at a time in `integration_order`; after each,
run focused checks and inspect the diff. The handoff is evidence, not an
integration command. Run combined checks after the last task, and use
`dev-ci-fix-loop` for remote CI.

## 6. Cleanup

Clean a worker only after confirming: status clean; accepted commit reachable
from an observed controller commit or durable archive ref (recorded as proof);
no unique artifact in the worktree. `cleanup_allowed` is upstream evidence, not
deletion authority: hand the ready handoff to `dev-disk-budget`, which
revalidates path ownership, ignored content, Git reachability, and process
activity before non-force removal. Clean each wave before the next large one.

## Restart recovery

Before a wave that may be interrupted (sleep, reboot, observer loss), run
`scripts/restart_checkpoint.py checkpoint` to persist the immutable base, plan,
gate, worker ownership, durable archive refs, disk-manifest locator,
non-durable roots, and one structured next action outside all disposable paths.

After restart, run `resume-validate` against newly observed state:

- resume existing resources only when manifest, hashes, roots, and ownership
  still match;
- reconstruct fresh roots only when the old manifest and old roots are both
  absent and every unique commit has durable preservation;
- block when an old root survives without its manifest, a unique commit lacks a
  durable ref, or stale worker state is presented as success.

A checkpoint is not acceptance, integration evidence, cleanup authority, or
permission to reuse a historical path.
