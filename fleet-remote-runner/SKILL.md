---
name: fleet-remote-runner
description: "Run predeclared, self-advancing SSH workflows in detached tmux with durable status, stall detection, artifact hashes, restoration receipts, and local sync. Use when a long remote task must survive observer or SSH disconnection. Not for credentials, privilege escalation, interactive work, or automatic destructive remediation."
---

# Fleet Remote Runner

Keep the remote runner in control of the complete predeclared workflow. Use the
local machine only to `launch`, `status`, `poll`, and `sync`.

## Workflow

1. Read [manifest.md](references/manifest.md) and create a task manifest.
2. Predeclare every non-interactive work step, restoration action, restoration
   check, artifact, and timeout. Express commands as `argv` arrays.
3. Inspect the default dry-run. Confirm that `remote_capability_preflight`
   names the intended `python3` runtime, Python 3.9 minimum, required modules,
   and `tmux -V` probe:

   ```bash
   <skill-root>/scripts/fleet-remote-runner \
     launch --manifest task.json
   ```

4. Execute only after reviewing the exact SSH and detached tmux plan:

   ```bash
   <skill-root>/scripts/fleet-remote-runner \
     launch --manifest task.json --execute
   ```

5. Observe without controlling workflow progression:

   ```bash
   <skill-root>/scripts/fleet-remote-runner \
     status --manifest task.json

   <skill-root>/scripts/fleet-remote-runner \
     poll --manifest task.json --interval-seconds 15
   ```

6. Sync declared artifacts plus control receipts after terminal completion:

   ```bash
   <skill-root>/scripts/fleet-remote-runner \
     sync --manifest task.json --destination ./task-receipt
   ```

7. Read [protocol.md](references/protocol.md) before interpreting stalls,
   failures, incomplete receipts, reconnection behavior, or handoff.

## Invariants

- Keep all round selection and task advancement on the remote runner. Never use
  local polling output to decide or inject a next step.
- Treat SSH loss as observer loss, not task failure. Reconnect with `status` or
  `poll`; never relaunch the same task or session blindly.
- Require the remote capability preflight to pass before launch materializes
  state. A runtime, module, or tmux failure must leave no staging directory or
  tmux session; report its exact stderr evidence instead of retrying blindly.
- Accept terminal completion only when the terminal marker and all referenced
  receipt hashes verify. A missing or failed restoration receipt is incomplete.
- Use `BatchMode` SSH and the remote host's own credential mechanism (e.g. an agent on that host). Never put a
  password, token, private key, credential-bearing URL, or credential-like
  environment variable in the manifest, command line, logs, or skill files.
- Do not add generated remediation after launch. Restoration actions and checks
  must be explicitly reviewed, bounded, idempotent, and non-destructive.
- Do not auto-delete state, kill unrelated sessions, retry failed work, mutate
  infrastructure, escalate privileges, or infer host, cluster, or root steps.

## Outcomes

- `succeeded`: work, required artifacts, restoration, and receipt hashes pass.
- `failed`: the remote runner finalized a failure with valid receipts.
- `integrity_error`: a terminal or receipt is missing, mismatched, or tampered.
- `heartbeat_lost`, `stalled_unfinalized`, `timed_out_unfinalized`: report and
  inspect the abnormal state without automatic remediation.
- `transport_error`: local connectivity failed; the remote task may still run.
- `observer_timeout`: the local polling deadline expired, not remote completion.

Each local SSH call is bounded by `--timeout-seconds` (default 30). Polling
with `--max-wait-seconds` passes the remaining monotonic budget to SSH and caps
sleeps; without it polling may continue, but each connection remains bounded.
Timeouts close only local SSH, never kill or relaunch remote work. A launch
timeout leaves an unknown outcome: reconcile its declared state/session first.
A sync timeout deletes its partial archive and publishes nothing.

Report the task ID, session, effective status, last heartbeat and log progress,
terminal reason, restoration result, artifact hashes, sync path, and unresolved
integrity or transport issues in handoff.
