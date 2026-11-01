# Resource Contract

The cleanup authority is created before parallel execution, not inferred after
it. A path can be large, old, named like a cache, and absent from active process
output without being safe to delete.

## State Machine

```text
planning
  -> declare new owned directories
  -> reserve nonexistent worktree paths
  -> externally create worktrees
  -> bind only their prior reservations
  -> start
active (resource set sealed)
  -> workers run
  -> orchestrator validates results
  -> durable Git refs preserve accepted HEADs
  -> authorize worktrees from live handoff evidence
  -> finish
succeeded
  -> inventory
  -> short-lived plan
  -> explicit cleanup
failed/interrupted
  -> retain everything
  -> investigate and preserve
  -> explicit released transition
released
  -> only independently eligible resources may be planned
```

The following transitions are forbidden:

- adding, adopting, repurposing, nesting, or rebinding a resource after
  `start`;
- reserving an existing path or registered worktree;
- binding a worktree that was not first reserved in the same manifest;
- authorizing a worktree before `start`, after a terminal state, or without a
  ready orchestrator handoff;
- converting a historical inventory result into automatic cleanup ownership.

## Session and Wave Layers

A high-frequency engineering session must not keep every future worker,
worktree, and temporary root in one long-lived active manifest. Prefer an
operator-managed host-shared Go build cache across concurrent sessions, plus
one wave manifest per bounded stage. When cross-session reuse is unavailable,
use two manifest lifecycle layers:

1. **Session manifest** — one reusable cache or similarly shared resource whose
   lifetime spans several waves.
2. **Wave manifest** — only the worktrees, task temporary roots, and disposable
   outputs for one bounded parallel stage.

Each wave independently reaches `succeeded`, receives a reviewed plan, and is
cleaned before the next large wave accumulates. The session manifest stays
active only while a later wave may reuse its shared cache.

Do not mix resources that cannot share the same terminal state and
preservation policy merely because they were observed in one implementation
wave. Use separate manifests for ordinary workers, read-only or migrated
fixtures, and failed or authorization-pending resources. The read-only
`lifecycle_recovery.py finish-readiness` command reports the exact resources
that prevent `succeeded`; it does not move resources between manifests or
authorize cleanup.

This does not relax the rule that an `active` manifest is ineligible for
cleanup. Mid-session reclamation comes from making waves separate bounded runs,
not from deleting resources out of a still-active ownership set.

For Go:

- select exactly one cache source: one marked operator-managed host-shared
  directory, or one session-owned directory using
  `role=go_build_cache, scope=session_shared`;
- every concurrent task gets exactly one wave-owned directory using
  `role=go_tmp, scope=task, task_id=<worker>`;
- all tasks reuse the same `GOCACHE`;
- `GOMODCACHE` remains host-shared and outside automatic cleanup.

`init-host-go-cache` creates a new private directory and immutable identity
marker. It refuses existing paths and never registers the cache as an
automatic cleanup resource. `go-env` validates the selected cache marker or
sealed session manifest plus the wave manifest, path identities, roles,
scopes, and task binding before returning environment values. It never creates
a cache after dispatch or infers a path from a directory name.

`go-topology` binds the complete declared Go participant set and parallel plan
SHA to that cache source and wave manifest before dispatch. Pass all
participants through repeated `--task-id` flags in one invocation. It rejects
missing or extra task temporary roots, a wave-owned build cache, a
session-owned task tmp, and generic placeholder directories whose purpose
claims Go cache/tmp usage. Its per-task environment hashes are the only
accepted source for worker/controller Go execution environments.

`go-env` is a receipt with no `status`. A consumer validates
`schema_version=1`, `kind=dev-disk-budget-go-environment`, task and wave
identity, cache source and identity, optional session identity, and exactly the
returned `GOCACHE`/`GOTMPDIR` keys. Testing `.status == "ready"` is a contract
error, not evidence that the topology is blocked.

This topology preserves parallel compilation while avoiding repeated cold
cache growth across workers and sessions. Before every large wave, subtract
the declared peak expected growth from current free space and require the
remaining bytes and percentage floors. The projected disk floor remains a
last-resort launch refusal, not the primary scheduler.

The optional receipt-backed growth calibration is advisory and read-only. It
can lower a conservative estimate only from at least two completed receipts
whose command class, target class, and concrete shared-cache identity all
match the proposed wave. Each receipt must prove owned-output allocation by a
validated baseline/peak delta, and shared-cache allocation must be a separate
continuous observation with every positive delta explained. The largest owned
delta, largest nonnegative cache delta, and explicit safety margin form the
calibrated candidate. The safety margin must be positive; zero retains the
conservative estimate. Negative cache deltas never become headroom. The same
absolute and percentage floors apply to the candidate and must both pass. See
[growth-calibration.md](growth-calibration.md) for the exact schema and reason
codes.

## Ownership Evidence

An owned directory requires all of:

1. the run was in `planning`;
2. the path did not exist;
3. the CLI created the directory;
4. the CLI wrote the exact ownership marker;
5. the resource set was sealed before task output appeared;
6. device, inode, and UID still match;
7. the tree contains no symlink traversal, different UID, or mount boundary;
8. no process has an open handle or working directory in the tree;
9. the run reached `succeeded` or an explicit post-investigation `released`
   state.

A Git worktree additionally requires all of:

1. its nonexistent path was reserved before `git worktree add`;
2. an atomic same-parent reservation marker proves that no other run reserved
   the same nonexistent path first;
3. the registered worktree was bound to that reservation before `start`;
4. its current HEAD and complete status still match the accepted result;
5. ignored and untracked content are absent;
6. a live local Git ref resolves to a commit containing the worker HEAD;
7. a `fleet-parallel-merge` handoff is `ready`, blocker-free, identifies
   the same task and commit, reports `accepted: true`, and reports
   `cleanup.allowed: true`;
8. the handoff bytes have not changed since authorization.

Before `git worktree add`, `reservation-status` revalidates the planning
manifest, exact reservation marker bytes and identity, absent target path, and
current worktree registry. Its structured receipt is consumed directly by the
multi-writer integration Git-mutation validator. Do not replace the receipt with
manually transcribed `path_absent` or `registered=false` claims.

The script never treats an old `contains_head: true` boolean as current
reachability proof. It reruns local Git ancestry checks.

Integration and smoke fixtures that exercise this workflow should also finish
their manifests and clean through reviewed plans and receipts. Do not teach
direct recursive deletion as the teardown path for manifest-owned resources.

## Manifest Seal

The resource-set hash includes the immutable cleanup identity:

- resource ID and kind;
- canonical path;
- purpose and cleanup policy;
- directory role, lifecycle scope, and task ID when present;
- claim timestamp;
- path identity;
- repository and task ID for a worktree;
- reservation/binding state, initial HEAD, and binding timestamp.

Authorization fields are intentionally outside the seal because they become
available only after worker completion. They may narrow cleanup eligibility but
cannot add or redirect a target.

Keep the manifest under a persistent state root outside every disposable
resource. The CLI serializes manifest mutation with an advisory file lock.

## Plan and TOCTOU

`plan` is read-only and short-lived. Each eligible resource records:

- path device, inode, and UID;
- deterministic tree fingerprint over relative path, type, device, inode,
  size, and nanosecond mtime;
- ownership-marker hash for owned directories;
- worktree HEAD, preservation commit, and handoff hash for worktrees;
- `lsof` result;
- directional logical and allocated byte estimates.

`cleanup` requires all of:

- explicit `--execute`;
- the exact SHA-256 of the reviewed plan file;
- an unexpired plan;
- unchanged manifest bytes;
- a new receipt path outside all cleanup targets;
- fresh eligibility and an identical fingerprint for every selected resource.

The script performs that validation once for the whole plan and again
immediately before each resource mutation. It does not offer a force override.

## Receipt and Recovery

The receipt is atomically written before the first deletion:

- `prepared`: validation passed and no mutation started;
- `cleaning`: names the resource whose mutation is about to begin;
- `completed`: every selected resource was removed and post-cleanup disk usage
  was recorded;
- `recovery_required`: an action failed or partial progress may exist.

Never overwrite an existing receipt. On `recovery_required`, compare the
manifest, plan hash, receipt, Git registration, filesystem paths, and durable
refs before deciding whether a manual retry is safe. Do not rerun the original
plan as though no mutation occurred.

`lifecycle_recovery.py recovery-plan` validates that the receipt binds the
exact manifest and reviewed plan, then requires receipt results to be the
executed prefix of eligible plan resources ending in exactly one failure. It
classifies already removed, failed-current, eligible remaining, blocked
remaining, and intentionally retained resources. The output is read-only and
sets `mutation_authorized=false`; a later cleanup still requires a newly
generated, revalidated, explicitly reviewed plan.

## Cleanup Operations

Owned directories are first atomically renamed within the same parent into a
plan-hash-derived quarantine path. The script verifies that the quarantined
inode is the reviewed inode, records the quarantine in the durable receipt,
and only then removes it recursively. This closes path-replacement races and
leaves a recovery locator if the process crashes after quarantine. Read-only
files may receive current-user write permission during that removal because Go
caches commonly contain read-only entries.

Worktrees use:

```bash
git -C <repo> worktree remove <exact-worktree-path>
```

They never use `--force`. A Git refusal preserves the worktree and produces
`recovery_required`.

The skill never runs global `git worktree prune` automatically. Missing
worktree metadata is small, crosses run ownership boundaries, and should be
reviewed with Git's dry-run output before a separate native prune.

## Failure Matrix

| Observation | Result |
|---|---|
| Path existed before declaration | Reject ownership |
| Reserved worktree was never bound | Refuse `start` |
| Manifest resource set changed after `start` | Reject manifest |
| Run is active, failed, or interrupted | Retain |
| Marker, inode, owner, tree, or filesystem changed | Block |
| `lsof` finds activity or cannot decide | Block |
| Worktree has tracked, untracked, or ignored content | Block |
| HEAD or preservation ancestry changed | Block |
| Handoff is blocked, changed, or cleanup-refused | Block |
| Plan expired or hash differs | Block before mutation |
| Receipt path already exists | Block before mutation |
| Non-force Git removal fails | Preserve and require recovery |

## Parallel Orchestrator Composition

The orchestrator remains a pure decision layer. It does not create, reserve,
bind, inventory, or remove resources. The controller must:

1. create the disk-budget manifest before dispatch;
2. give each worker only its already bound worktree and declared output roots;
3. validate and integrate/archive through the orchestrator;
4. use the ready handoff to authorize the matching worktree;
5. finish the disk-budget run and generate a separate cleanup plan.

This separation prevents an accepted code result from silently becoming
authority to delete an unrelated path.
