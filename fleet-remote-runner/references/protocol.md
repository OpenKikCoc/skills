# Durable Supervision Protocol

## State Machine

The local observer has four operations:

1. `launch`: validate, display the plan, and optionally create detached tmux.
2. `status`: reconnect and read durable state.
3. `poll`: repeat status; transport errors do not alter remote state.
4. `sync`: copy control evidence, logs, and declared artifact files into a
   temporary local directory, verify them, then atomically publish.

The remote runner alone executes:

```text
work steps
  -> restoration steps
  -> restoration checks
  -> artifact hashing
  -> receipts and handoff
  -> terminal marker
```

The observer cannot insert, skip, retry, or choose a round. Closing it or losing
SSH does not signal the detached process.

## Capability Preflight

The dry-run declares the exact remote launch capabilities. Execution resolves
the selected `python3`, requires Python 3.9 or newer, imports every standard
library module used by the generated runner, resolves `tmux`, and executes
`tmux -V`. Python 3.9 is the minimum because the runner source uses PEP 585
built-in generic syntax.

This preflight is fail-closed and precedes creation of the state parent,
prestage directory, work directory, or tmux session. Exit 69 means the Python
executable was not found, 70 means its version is too old, 71 means a required
module import failed, and 72 means tmux is absent or its version probe failed.
The failing executable, observed version, module plus exception type/message,
or tmux probe output remains on stderr as applicable. Preserve that evidence;
do not relaunch against another undeclared runtime or create state manually.

Launch sends the normalized manifest and runner as one checksum-bound stdin
payload instead of embedding them in SSH arguments. The dry-run exposes the
payload hash and size while keeping SSH argv bounded.
After preflight, the bootstrap verifies the complete payload, writes it in a
sibling staging directory, then atomically renames that directory to
`remote.state_dir`. Once that directory is published, a later launch error is
reconciled through `status`; it is never treated as permission to overwrite
state or start a second runner.

## Durable Files

All control files are under `remote.state_dir`.

- `manifest.json`: normalized immutable task contract.
- `runner.lock`: exclusive runner start marker.
- `state.json`: latest declared state.
- `heartbeat.json`: active phase and command, heartbeat, log size, and last
  log-progress time.
- `events.jsonl`: append-only transitions.
- `logs/*.log`: combined stdout/stderr per command with boundary records.
- `restoration-receipt.json`: restoration actions and checks with log hashes.
- `artifact-receipt.json`: declared path, presence, size, and SHA-256.
- `handoff.json`: final locations, status, reason, and receipt hashes.
- `terminal.json`: last durable marker, written after all receipts.

Atomic JSON replacement plus receipt hashes protects against partial writes.
`status` returns `complete: true` only for a valid terminal marker whose
referenced receipts verify.

The producer catches failures in work, restoration, and artifact collection.
It writes manifest-bound failure receipts and a failed terminal marker whenever
the state directory remains writable; an invalid artifact path cannot silently
strand the task between phases.

## Policies

- Step and task timeouts bound work. Restoration still runs after work failure.
- Restoration timeout bounds each restoration action and check.
- A log stall terminates only the active owned process group, records
  `log_stalled`, and proceeds to restoration.
- Heartbeat timeout classifies an abnormal nonterminal runner from the observer.
- SSH transport failure remains observer failure, not remote task failure.
- Each local SSH operation has a finite positive `--timeout-seconds` (default
  30). `poll --max-wait-seconds` uses one monotonic end-to-end deadline, passes
  `min(per-call timeout, remaining budget)` into every status call, bounds sleep,
  and never accepts a terminal response received after its deadline. A local
  deadline alarm also interrupts status decoding/serialization; it controls
  only the observer. Poll output packets are capped at 4096 bytes (oversized
  updates carry `output_truncated:true`) and pipe writes are nonblocking with
  the remaining budget. Expiry emits a final `observer_timeout`, `complete:false`
  and `remote work is unchanged`; if stdout is full it attempts stderr without
  waiting. If both channels are unread/full, delivery cannot be guaranteed, but
  observer exit remains bounded. Omitted max-wait permits continued observation,
  not an unbounded SSH connection or output write.
- Timeout reaps only local SSH and reports `observer/transport timeout`;
  `observer_timeout` means `complete: false`. No remote kill or relaunch follows.
  Reconnect to reconcile durable state. A launch timeout is outcome unknown,
  including whether preflight or publication completed; never repeat launch
  automatically. Sync timeout removes its partial local archive and publishes
  no destination. File-backed SSH output avoids waiting on proxy-held pipes.

## Restoration Gate

A task cannot complete without a restoration receipt bound to the manifest,
successful checks for terminal success, and an exact receipt hash in the
terminal marker. Missing, malformed, mismatched, or tampered evidence produces
`integrity_error`. Never fabricate a receipt to clear this state.

Local `sync` requires the downloaded manifest to match the manifest supplied by
the observer. It verifies terminal-to-receipt hashes, receipt types and task
binding, restoration/artifact/handoff consistency, every declared artifact
path/hash/size, and the absence of undeclared payload files. Validation failure
removes the staging directory and leaves no final sync destination.

## Handoff

Record task ID, manifest hash, SSH target, session, state and work directories,
effective status, terminal reason, last heartbeat and log progress, restoration
outcomes, artifact hashes, local sync destination, and unresolved transport or
integrity issues. Never include credential values, undeclared remote files, or
an automatically selected remediation command.
