# Disk Sources and Placement

Disk pressure from parallel engineering work is usually cumulative. A worktree
may be small while its language cache, temporary compiler output, test
artifacts, agent snapshots, and container layers occupy different roots on the
same filesystem.

## Audit Order

Start with read-only filesystem facts:

```bash
df -h <managed-root>
df -i <managed-root>
```

The bundled `check` gate defaults to requiring both 20 GiB free and 5% free,
which is equivalent to a floor of `max(20 GiB, 5% of filesystem capacity)`.
Treat that as a minimum launch gate, not a capacity estimate. Raise
`--min-free-bytes` or `--min-free-percent` when the declared worktrees, caches,
and outputs can plausibly consume more headroom. Before every large wave, pass
`--expected-growth-bytes` for the projected peak new allocation; the gate
subtracts it from current free space before applying both floors.

Then measure separately:

1. managed run roots and Git worktrees;
2. effective language caches and temporary roots;
3. user and system temporary directories;
4. agent hooks, snapshots, sessions, and tool downloads;
5. project artifacts such as benchmark output, coverage, graphs, and binaries;
6. Docker through its own daemon or UI;
7. APFS volumes and snapshots through system tools.

Do not sum every `du` row when parent and child paths overlap. On APFS, clones,
hardlinks, compression, and sparse files also make apparent sizes differ from
actual reclaim.

For frequent read-only status checks, use `scripts/quick_disk_audit.py` before
manual deep scans. One run should:

- enumerate each configured scan root once;
- measure each immediate child and focused path at most once;
- use bounded parallelism instead of concurrent whole-home scans;
- derive grouped totals and Top N from the same measurements;
- compare only against a complete prior report with the same self-verified
  scope.

Keep the fast scope stable and small: scan `/private/tmp` children and focus
system VM usage. If that cannot explain the filesystem delta, run a separately
baselined deep scope over known heavy paths such as the Go build cache, agent
state directories, the code checkout root, module cache, and user temporary root. Do not compare
different scopes or recursively scan the entire home merely to refresh a
status summary. Escalate to deeper `du`, exact-path `lsof`, Docker/APFS tooling,
or artifact inspection only when a material delta remains unexplained.

## Resource Classes

| Class | Examples | Default |
|---|---|---|
| Worker-exclusive disposable | `GOTMPDIR`, one worker's Python bytecode | Declare before run; eligible after worker/run gates |
| Run-shared disposable | run `GOCACHE`, run Rust target | Declare before run; release only after all users stop |
| Host-shared rebuildable | global Go build cache, npm/uv cache | Report-only; use native GC under separate policy |
| Host-shared expensive | `GOMODCACHE`, private dependency downloads | Retain by default |
| Durable artifact | coverage, benchmark output, Graphify graph | Preserve or explicitly disposition; never assume cache |
| Agent state | agent CLI sessions, hook snapshots | Tool-owned; exclude active/resumable sessions |
| Product-managed | Docker images, layers, volumes | Native product GC only |
| System-managed | APFS system/VM/recovery volumes | Never manually delete |
| Unknown | historical temp/worktree-like directory | Report-only |

## Go

Query actual locations instead of assuming platform defaults:

```bash
go env GOCACHE GOMODCACHE GOTMPDIR GOPATH
```

For routine local Go work, prefer `scripts/guarded_go.py`: one explicit host
profile binds existing cache identities, and every invocation gets an atomic
budget and a new owned temporary root. This avoids asking agents to assemble
several manifest commands for a single test. All sessions must share the same
state root; separate registries cannot coordinate each other's reservations.

A shared build cache still grows across source revisions, toolchain versions,
architectures, and race/non-race builds. Cache sharing reduces duplicates but
does not bound total retention. Treat oversized caches as report-only native-GC
candidates, not a reason to initialize another cold cache. Existing active
sessions keep their selected cache until explicitly migrated at a safe boundary.

Recommended parallel placement:

- share `GOMODCACHE` at host scope to avoid repeated private module downloads;
- prefer one explicitly initialized operator-managed `GOCACHE` across
  concurrent workers, successive waves, and concurrent agent sessions;
- otherwise use one session-scoped `GOCACHE` across concurrent workers and
  successive waves so only the first wave pays the cold compilation cost;
- use one `GOTMPDIR` per worker;
- place coverage profiles, test binaries, and benchmark output in separate
  declared roots because they are artifacts, not caches.

Do not create a new `GOCACHE` for every `go test`, `go vet`, target module,
worker, or agent session. Independent cold caches multiply identical compiled
package objects and can consume several GiB each. Preserve concurrency by
sharing the content-addressed build cache while isolating only mutable compiler
temporary state.

For frequent multi-session work, initialize the host cache once through the
skill and use a wave manifest for worktrees and task `GOTMPDIR` roots. The
marker proves identity but grants no automatic deletion authority. For an
isolated long session, use one session manifest for `GOCACHE` instead. In both
cases, clean a completed wave before starting the next large wave and rerun the
projected-headroom gate. This bounds accumulated wave resources without
serializing workers inside a wave.

Do not run `go clean -modcache` as automatic post-worker cleanup. It affects
other repositories and makes later private builds depend on network/auth.

A global `go clean -cache` is a native GC option only after separately proving
no relevant Go work is active and accepting the rebuild cost. It is outside
the per-run deletion manifest.

## Rust, Node, and Python

Prefer explicit task roots:

- `CARGO_TARGET_DIR` for Rust build output;
- npm cache inspection and `npm cache verify` before native cleanup;
- `UV_CACHE_DIR` for uv cache;
- pip cache commands for pip-owned data;
- `PYTHONPYCACHEPREFIX` for Python bytecode in sandboxed or parallel runs;
- explicit virtual environment roots when they are intentionally disposable.

Installed runtimes, NVM versions, uv tools, and virtual environments with
unknown ownership are not equivalent to caches.

## Git Worktrees

Read all registrations from each repository:

```bash
git -C <repo> worktree list --porcelain
git -C <repo> worktree prune --dry-run --verbose 2>&1
```

The prune dry-run writes candidates to stderr on common Git versions. Missing
metadata can be reviewed and pruned separately, but the space is usually small
compared with build caches.

Never delete an existing worktree merely because:

- the path is under a temporary root;
- the branch looks old;
- the worktree is clean;
- the directory is absent from a process listing;
- its commit appears in another branch.

The per-run contract requires creation-time reservation plus current
preservation and orchestrator evidence.

## Temporary Roots

Audit both the system-visible root and the process-specific temp root:

```bash
printf '%s\n' "${TMPDIR:-}"
```

On macOS, `/tmp` and `/private/tmp` may refer to the same storage through path
aliases. Canonicalize before comparing identities. Per-user `$TMPDIR` often
contains updater downloads, browser profiles, compiler output, and tests.

Age is only a ranking signal. A resumed agent or long-running test may still
use an old directory. Check complete paths with `lsof +D` immediately before
any user-approved historical cleanup.

## Agent Session State

Agent hook snapshots and session artifacts can become large, but their
retention semantics differ:

- hook snapshots may support reporting, repair, or active hooks;
- agent CLI sessions may support resume, audit, or memory extraction;
- temporary files may be in the middle of atomic publication.

Do not apply a simple "older than N days" deletion rule. First map active
process/session IDs, current hook writers, open files, and the owning plugin's
retention contract. These locations remain outside the automatic per-run
manifest unless the owning tool creates and marks a task-scoped subdirectory
before use.

## Project Artifacts

Typical large artifacts include:

- benchmark result trees;
- coverage profiles;
- generated binaries;
- knowledge graphs and semantic caches;
- browser traces, screenshots, and recordings;
- downloaded CI artifacts.

They may be reproducible but still valuable. Require an explicit disposition:

- `preserve`: retain with a durable locator and optional hash;
- `archive`: move through an artifact-specific workflow;
- `discard`: create a new declared disposable root before generation;
- `unknown`: report and retain.

## Docker

Do not manually remove Docker's virtual disk file. Inspect through the running
daemon:

```bash
docker system df
```

Use Docker Desktop cleanup or carefully scoped native prune commands after
reviewing images, containers, build cache, and volumes. Container-internal
cleanup may not immediately shrink the sparse virtual disk.

## macOS and APFS

System, Preboot, Recovery, and VM volumes are system-managed. Inspect local
snapshots with supported APFS or Time Machine tools, but do not treat OS update
snapshots or virtual-memory files as ordinary cache.

Deleting an open file may not return space until the process closes it. Always
compare filesystem free space after each cleanup tier instead of trusting the
sum of apparent directory sizes.

## Long-Term Policy

Use three layers:

1. **Prevention**: disk floor, bounded parallelism, declared output placement.
2. **Deterministic release**: sealed per-run ownership, live validation,
   reviewed short-lived plan, durable receipt.
3. **Periodic native GC**: separate policies for shared language caches,
   updater downloads, agent retention, Docker, and system storage.

Only layer 2 belongs to automatic worktree completion cleanup. Layers 1 and 3
must not weaken its ownership gates.
