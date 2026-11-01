---
name: dev-disk-budget
description: "Prevent disk exhaustion during builds, tests, large artifact generation, and multi-session agent work. Provides a shared-cache runner (worked example: Go with GOCACHE/GOMODCACHE/GOTMPDIR) with atomic cross-session disk budgets and owned temporary roots, bounded read-only disk audits, and cleanup of worktrees and caches only when they are owned and verified clean. Use when several agent sessions share one disk, when a build or test may grow the disk, or when disk pressure needs diagnosis. Do not use it to delete anything on inference: never remove active caches, sessions, or unknown data."
---

# Disk Budget

Go is the primary worked example. Use the guarded Go runner for ordinary
compilation, tests, vet, generators, and other supported Go commands. It gives
concurrent agent sessions one shared build cache, distinct invocation
temporary roots, and one atomic disk budget. Do not create a parallel-work
plan merely to run a local test. The ownership, budget, and audit rules below
apply to any ecosystem; only `guarded_go.py` and the `go-*` topology commands
are Go-specific. For other toolchains, see "Other Ecosystems".

Cleanup authority must be declared before work starts. Never infer permission
to delete from a path name, age, size, PID alone, or historical inventory.

## Guarded Go Quick Start

Configure one host profile once with existing shared caches. This records their
identity but grants no cache-deletion authority:

```bash
export DEV_DISK_BUDGET_STATE_ROOT=<persistent-shared-state-root>
python3 <skill-root>/scripts/guarded_go.py configure \
  --gocache "$(go env GOCACHE)" \
  --gomodcache "$(go env GOMODCACHE)"
```

Then run Go without inventing a session ID or byte estimate:

```bash
python3 <skill-root>/scripts/guarded_go.py run \
  --cwd <repo> -- test ./pkg/...
python3 <skill-root>/scripts/guarded_go.py status
```

The generated session label is lowercase and collision-resistant. Explicit
overrides remain available:

```bash
python3 <skill-root>/scripts/guarded_go.py run \
  --cwd <repo> \
  --session-id <lowercase-label> \
  --expected-growth-bytes <peak-new-bytes> \
  -- test -race ./...
```

State root precedence: `--state-root`, `DEV_DISK_BUDGET_STATE_ROOT`,
`$XDG_STATE_HOME/dev-disk-budget`, `~/.local/state/dev-disk-budget`.

Every cooperating runtime sharing a `GOCACHE` must use the same state root.
Do not create another ledger to bypass an admission refusal. The configured
temporary parent defaults under the system temporary root, outside HOME and
repositories, and every invocation receives a distinct `GOTMPDIR`/`TMPDIR`.
The runner changes only the child environment; it never changes shell startup
files or `go env -w` state.

## Deterministic Growth Defaults

New profiles store these conservative defaults; legacy profiles derive the
same values without migration:

| Command class | Reservation |
|---|---:|
| `version` | 512 MiB |
| read-only `env` | 1 GiB |
| `list` | 2 GiB |
| `build`, `generate`, `run`, `vet` | 8 GiB |
| targeted `test` | 12 GiB |
| full test targets (`...`, `./...`, `.../...`, `all`, `std`, `cmd`) | 24 GiB |
| any `test -race` | 32 GiB |

An explicit `--expected-growth-bytes` always wins. Never lower a race or full
test estimate silently. If a known workload needs more, pass its conservative
peak explicitly.

Admission holds one host lock and subtracts all outstanding reservations plus
the new peak from current free space before applying both configured floors.
The budget is cooperative, not a quota: unguarded processes, detached children,
and underestimated output remain outside it. Exit 75 means defer the new work
or investigate retained activity; do not weaken floors.

## Launcher, Liveness, and Migration

- `scripts/ai-runtime-bin/go` is a launcher for an agent runtime whose PATH
  you control. It routes `build`, `test`, `vet`, `generate`, `list`, and `run`
  through the runner, runs `version`/`env` directly, and rejects `go env -w/-u`.
  It needs `DEV_DISK_BUDGET_REAL_GO=<absolute path to real go>`. Never
  install it into a human login shell.
- Each reservation records process identity (PID, UID, start token, process
  group; on Linux boot ID and PID namespace). `status` reports owner and child
  as `alive`, `dead`, `identity_mismatch`, or `unknown`. Ambiguity is never
  "dead", and there is no TTL release.
- After investigating an abandoned reservation, release it with
  `guarded_go.py reconcile --id <id> --expect-record-sha256 <digest>` using the
  digest from a fresh `status`. `status` and `reconcile` never delete caches
  or temporary data.
- Before moving an existing state root to the identity-aware runner, run
  `guarded_go.py --state-root <root> migration-preflight`; a nonempty legacy
  ledger blocks until externally drained.

Details: [guarded-go.md](references/guarded-go.md).

## Parallel Workflow

For real parallel waves, use one operator-managed host `GOCACHE` when
available, one task-owned `GOTMPDIR` per participant, and the existing shared
`GOMODCACHE`. Keep the shared cache report-only. Use a separate bounded
manifest per wave so completed work does not remain coupled to later work.

Before each large wave:

```bash
python3 <skill-root>/scripts/disk_budget.py check \
  --path <managed-root> \
  --expected-growth-bytes <next-wave-peak-bytes>
```

The default floor is 20 GiB and 5% free after projected growth. A lower
estimate is allowed only through receipt-backed `growth_calibration.py` with
at least two homogeneous completed observations and a positive safety margin.

Topology commands (`init-host-go-cache`, `go-topology`, `go-env`) bind one
shared `GOCACHE` and one task-owned `GOTMPDIR` per participant. Call
`go-topology` once with the complete participant set (repeat `--task-id`),
and apply only the environment it returns. `go-env` is a receipt with no
`status` field.

Read [parallel-go-workflow.md](references/parallel-go-workflow.md) for session
and wave examples, worktree reservation/binding, handoff authorization
(handoffs come from the `fleet-parallel-merge` skill), cleanup
planning, and recovery. Read
[resource-contract.md](references/resource-contract.md) before composing this
skill with another parallel workflow, and
[growth-calibration.md](references/growth-calibration.md) before lowering an
estimate.

## Other Ecosystems

The runner and topology commands support Go only. For Rust, Node, and Python,
use the same principles by hand: point the tool's cache and output variables
(`CARGO_TARGET_DIR`, `UV_CACHE_DIR`, `PYTHONPYCACHEPREFIX`) at a declared,
owned root, check headroom with `disk_budget.py check` before a
large run, and treat shared package caches as report-only. See
[disk-sources.md](references/disk-sources.md).

## Hard Boundaries

- Declare only nonexistent paths during `planning`; let `claim-directory`
  create and mark owned directories.
- Reserve a nonexistent worktree path, create it externally, then bind it.
- Seal with `start` before workers run. Never add, repurpose, or rebind after
  sealing.
- Keep manifests, plans, receipts, and budgets outside all disposable roots.
- Treat preexisting, historical, shared, unknown, symlinked, overlapping,
  cross-filesystem, differently owned, or active paths as report-only.
- Never force Git removal, globally prune worktrees, or globally clean Go
  caches.
- Never delete an active cache, session, unknown path, failed run, or data with
  inconclusive activity evidence.
- A ready handoff and durable Git ref are required before authorizing worktree
  cleanup. Cleanup still requires a fresh reviewed plan and its exact SHA-256.
- Preserve all data after failure, interruption, launch ambiguity, stale plan,
  changed identity, or partial cleanup; use the read-only recovery classifier.
- In zsh, never assign to special variable `path`; use arrays and explicit
  optional-glob handling. Read
  [shell-state-contract.md](references/shell-state-contract.md) for wrappers.

## Read-Only Pressure Audit

Use `quick_disk_audit.py` for bounded, deduplicated size collection. It never
grants cleanup authority. Start with a stable shallow scope, compare only an
identical scope hash, then deepen inspection for a reviewed Top N item or an
unexplained filesystem delta. See
[read-only-audit.md](references/read-only-audit.md) and
[disk-sources.md](references/disk-sources.md).
