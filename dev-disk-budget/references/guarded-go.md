# Guarded Go Runner

## Host Profile

`guarded_go.py configure` binds one private host state directory to an existing
`GOCACHE`, existing `GOMODCACHE`, temporary parent, byte floor, and percentage
floor. The caches remain report-only. State, cache, module cache, temporary
root, and command working directory must share one filesystem so admission
uses one meaningful free-space value.

The state directory and JSON files must belong to the current UID, remain
private, and retain their recorded device/inode identities. Cache paths cannot
overlap state or temporary roots, sit under a disposable ownership marker, or
be repository roots. The temporary root cannot cross a real Git boundary. An
empty sandbox-mounted `.git` placeholder is not treated as a repository; a
non-empty `.git` directory or `gitdir:` file is.

State selection is explicit `--state-root`, then
`DEV_DISK_BUDGET_STATE_ROOT`, then the backward-compatible XDG/HOME
default. Set the dedicated environment variable consistently for every AI
runtime sharing the configured build cache.

New profiles persist the command growth table documented in `SKILL.md`.
Profiles created before that field existed derive the same built-in table
without modifying host state. `--expected-growth-bytes` is an exact per-run
override. Full test targets and `-race` are recognized before admission.

## Reservation and Process Identity

Admission occurs under `.budget.lock`. The ledger retains the complete declared
peak until the command is resolved, even if some growth has already consumed
free space. This intentional double counting is conservative.

Every new reservation records:

- owner PID, UID, platform, start token, and process group;
- boot identity when the platform exposes it;
- Linux boot ID and PID namespace;
- the same identity for the child process-group leader after launch.

Linux evidence comes from `/proc/<pid>/stat`, `/proc/<pid>/ns/pid`, the process
directory UID, and `/proc/sys/kernel/random/boot_id`. macOS uses native
`libproc`; it does not depend on sandbox-visible `ps` output.

Identity evaluation has four states:

| State | Meaning |
|---|---|
| `alive` | The current process matches every recorded identity field. |
| `dead` | The PID and, for a child, its recorded process group are absent. |
| `identity_mismatch` | The numeric PID exists but its stable identity differs; the old process is gone. |
| `unknown` | Evidence is missing/inaccessible or the leader is gone while its numeric process group still exists. |

A numeric PID is inspected only when the observer and recorded Linux boot and
PID namespace evidence are comparable. A different boot proves the old process
is gone; a different PID namespace is `unknown` because numeric IDs cannot be
translated safely, unless the whole recorded namespace is proven gone (`dead`,
reason `pid_namespace_gone`). That proof requires the observer at the top of
its `/proc` PID namespace tree (one `NSpid` entry) and a full `/proc` scan in
which no `ns/pid` link names the recorded namespace. An unreadable link is
excluded only for a process in the observer's namespace or one running under
other UIDs only; any other unreadable link, hidden process list (`hidepid`),
partial `/proc`, or unexpected `/proc` error stays `unknown`. A record from a
sibling namespace outside the observer's hierarchy would also look gone;
ledger writers are descendants of the host namespace, so that residual is
accepted. A group-level mismatch is safe only when the comparable
recorded group ID is absent. This prevents PID reuse or namespace skew from
making an unrelated process look like the reservation owner.

Legacy records that lack owner or launched-child identity remain readable and
are always `legacy_identity_missing`. No reconciliation path accepts caller
assertions as proof that those processes are gone.

## Outcomes

The runner writes a launch receipt before executing Go. It creates one marked
temporary root and sets `GOCACHE`, `GOMODCACHE`, `GOTMPDIR`, and `TMPDIR` only
for the child. Nested guarded runs and external `GOCACHEPROG` are rejected.

- Successful command and exited group: finish the invocation manifest, apply
  the existing identity/activity/plan checks, and remove only that newly owned
  temporary root. Shared caches are untouched.
- Failed command: retain temporary data and return the original exit status;
  release the budget after the process group is proven gone.
- Interrupted command: install signal capture before reservation, never spawn a
  child after a queued pre-launch signal, and forward SIGINT/SIGTERM only while
  the recorded identity and process object both prove the child leader live.
  Post-exit signals are queued instead of targeting a reused group; prior
  handlers are restored before cleanup, and any already queued signal becomes
  the run's signal-derived result. Temporary data is retained and budget
  release still requires the group to be gone.
- Launch failure before a child exists: retain any created evidence and release
  the reservation with a `launch_failed` outcome.
- Uninspectable launch or surviving group: mark `outcome_unknown`, retain the
  budget and temporary data, and return 75.

Commands that detach or escape the process group are outside the contract.

## Status and Explicit Reconciliation

`status` reports total reserved bytes, disk projection, GOCACHE, counts by
owner/child liveness and reconciliation class, plus one concise record per
reservation. The per-record `record_sha256` hashes the exact immutable record,
not the rendered liveness fields.

Re-run status immediately before reconciliation. `reconcile` requires that
exact digest and an `eligible` class: the owner is `dead` or
`identity_mismatch`, the child identity exists, and its group is `dead` or
`identity_mismatch`. It writes an outcome, releases only the budget record, and
retains all temporary data. No status or reconciliation command removes a
cache, run directory, or temporary path.

### Read-only migration preflight

Run `migration-preflight` before switching an existing configured state root to
the identity-aware runner. It reads the ledger under its existing lock and
reports each legacy record by ID, phase, child-PID presence, and exact record
digest. It never changes the ledger or creates or removes any path.

- No legacy records: `same_root_allowed`.
- Any legacy record, including `child_pid=null`:
  `blocked_requires_external_drain`. No new-root selection changes this result.
- Optional prospective validation requires all of `--new-state-root`,
  `--new-gocache`, `--gomodcache`, and `--new-temp-root`; a partial set is
  rejected.
- New state and temporary roots must be nonexistent and outside Git or owned
  roots. Build/module caches must be existing current-user directories with
  stable identities. The prospective build cache and temporary root must be
  distinct from the old ones; all roots must be non-overlapping and share one
  filesystem.
- A valid prospective configuration is reported as
  `valid_but_not_authorized`; the overall rollout remains blocked.

Preserve the old state root, ledger, run evidence, caches, and temporary data.
Do not copy the legacy ledger into another root or reuse its cache/temp
topology to bypass the block. There is deliberately no legacy mutation,
self-attested drain, or force-reconciliation command. After a real external
drain, rerun preflight and require an empty legacy ledger before same-root
rollout.

## AI-Runtime Launcher Contract

`scripts/ai-runtime-bin/go` is a PATH-compatible executable for a controlled AI
runtime. It is not a host installer and does not edit shell configuration.

Required environment:

```text
DEV_DISK_BUDGET_REAL_GO=/absolute/path/to/real/go
DEV_DISK_BUDGET_STATE_ROOT=/persistent/shared/state
```

The real binary must be an absolute executable and must not resolve to the
launcher. The launcher uses the running Python interpreter to execute the
adjacent canonical `guarded_go.py`; it never searches PATH for Go.

When `DEV_DISK_BUDGET_ACTIVE_RUN` is present, the launcher still validates the
real executable, command, read-only constraint, and working directory, then
executes the real binary directly. It inherits the outer run's cache,
temporary directory, and budget rather than nesting the guarded runner.
`GOCACHEPROG` remains rejected on both outer and nested paths.

Allowed commands are `build`, `test`, `vet`, `generate`, `list`, `run`,
`version`, and read-only `env`. The launcher sends build-affecting commands
through the guarded runner and executes `version` plus read-only `env` directly
with the pinned real Go binary and `GOTOOLCHAIN=local`, without allocating a
build reservation. `env -w`, `env -u`, `env --w`, and `env --u` (including
assignment forms and flags inherited through `GOFLAGS`), unsupported commands,
missing subcommands, malformed `-C`, a relative/missing real binary, and
recursive configuration fail before guarded execution.
The direct-query environment always carries an explicit `GOFLAGS` value, even
when empty, so a previously persisted `go env -w GOFLAGS=...` value cannot be
loaded from the Go environment file behind the launcher's validation.

Global `-C <dir>` and `-C=<dir>` are translated to runner `--cwd`. Defaults
come from the host profile. These optional variables provide explicit
per-invocation overrides without overloading Go arguments:

```text
DEV_DISK_BUDGET_SESSION_ID
DEV_DISK_BUDGET_EXPECTED_GROWTH_BYTES
```
