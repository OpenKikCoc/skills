# Parallel Go Workflow

Use this protocol only when several participants genuinely run Go in parallel.
The guarded runner is the simpler default for ordinary commands.

## Lifecycle Layers

Prefer a marked operator-managed host cache. If cross-session cache reuse is
unavailable, create one session manifest containing only a session-shared
`go_build_cache`. Each bounded wave gets a separate manifest containing one
task-scoped `go_tmp` directory per complete participant and any reserved
worktrees or owned outputs.

Initialize the fallback session cache:

```bash
python3 <skill-root>/scripts/disk_budget.py init \
  --manifest <state-root>/session.json --run-id <session-id>
python3 <skill-root>/scripts/disk_budget.py claim-directory \
  --manifest <state-root>/session.json --id go-cache \
  --path <managed-root>/<session-id>/go-cache \
  --purpose 'session-shared GOCACHE' \
  --role go_build_cache --scope session_shared
python3 <skill-root>/scripts/disk_budget.py start \
  --manifest <state-root>/session.json
```

Initialize one wave and its task root:

```bash
python3 <skill-root>/scripts/disk_budget.py init \
  --manifest <state-root>/wave-1.json --run-id <session-id>-wave-1
python3 <skill-root>/scripts/disk_budget.py claim-directory \
  --manifest <state-root>/wave-1.json --id core-tmp \
  --path <managed-root>/<session-id>/wave-1/core-tmp \
  --purpose 'core worker GOTMPDIR' \
  --role go_tmp --scope task --task-id core
```

## Worktree Transaction

Reserve before native creation, bind after creation, and seal before dispatch:

```bash
python3 <skill-root>/scripts/disk_budget.py reserve-worktree \
  --manifest <state-root>/wave-1.json --id worker-core --task-id core \
  --repo <repo> --path <managed-root>/<session-id>/wave-1/worker-core \
  --purpose 'isolated core task'
python3 <skill-root>/scripts/disk_budget.py reservation-status \
  --manifest <state-root>/wave-1.json --id worker-core \
  --output <state-root>/worker-core-reservation.json
git -C <repo> worktree add \
  <managed-root>/<session-id>/wave-1/worker-core <base-sha>
python3 <skill-root>/scripts/disk_budget.py bind-worktree \
  --manifest <state-root>/wave-1.json --id worker-core
python3 <skill-root>/scripts/disk_budget.py start \
  --manifest <state-root>/wave-1.json
```

Give the structured reservation-status receipt directly to the parallel
orchestrator's Git mutation validator. Do not transcribe it into booleans.

## Topology Receipt

Call `go-topology` once with every Go participant, including a controller that
runs validation:

```bash
python3 <skill-root>/scripts/disk_budget.py go-topology \
  --session-manifest <state-root>/session.json \
  --wave-manifest <state-root>/wave-1.json \
  --plan-sha256 <parallel-plan-sha256> \
  --task-id core --task-id controller
python3 <skill-root>/scripts/disk_budget.py go-env \
  --session-manifest <state-root>/session.json \
  --wave-manifest <state-root>/wave-1.json \
  --task-id core
```

Validate schema/version, kind, task, wave, cache source/identity, optional
session ID, and exactly the returned `GOCACHE`/`GOTMPDIR` keys. Never test a
`status` field on `go-env`; it intentionally has none. Apply the environment to
all Go commands, generators, tests, vet, and benchmarks. Redirect non-Go
generated data and coverage to separately declared roots.

## Finish and Cleanup

After acceptance and durable Git preservation, authorize the exact worktree:

```bash
python3 <skill-root>/scripts/disk_budget.py authorize-worktree \
  --manifest <state-root>/wave-1.json --id worker-core \
  --preservation-ref refs/archive/<session-id>/wave-1/core \
  --handoff <state-root>/handoff.json
python3 <skill-root>/scripts/disk_budget.py finish \
  --manifest <state-root>/wave-1.json --status succeeded
```

Inventory, create a short-lived plan, review exact actions/refusals/hash, then
execute only with the reviewed digest:

```bash
python3 <skill-root>/scripts/disk_budget.py inventory \
  --manifest <state-root>/wave-1.json --observe <shared-cache>
python3 <skill-root>/scripts/disk_budget.py plan \
  --manifest <state-root>/wave-1.json \
  --output <state-root>/wave-1-plan.json
python3 <skill-root>/scripts/disk_budget.py cleanup \
  --plan <state-root>/wave-1-plan.json --execute \
  --confirm-plan-sha256 <reviewed-sha256> \
  --receipt <state-root>/wave-1-receipt.json
```

Finish and clean one wave before accumulating the next. Keep the session cache
active only while later work can reuse it.

Before finishing a mixed manifest, run:

```bash
python3 <skill-root>/scripts/lifecycle_recovery.py finish-readiness \
  --manifest <state-root>/wave-1.json
```

If cleanup returns `recovery_required`, do not rerun the old plan. Classify the
immutable evidence:

```bash
python3 <skill-root>/scripts/lifecycle_recovery.py recovery-plan \
  --manifest <state-root>/wave-1.json \
  --plan <state-root>/wave-1-plan.json \
  --receipt <state-root>/wave-1-receipt.json
```

The classifier is read-only. Revalidate current state and generate a fresh
reviewed plan for anything still eligible.
