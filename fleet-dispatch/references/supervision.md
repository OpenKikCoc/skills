# Remote supervision (L3) and host queues

## The supervisor

The supervisor is a long-lived session on a *different* host from the fleet —
typically the human's own workstation — that watches over several items for
days. It exists because the coordinator's context is short-lived and
event-scoped, while some goals span many cards.

Properties:

- **Pull, one-way.** The supervisor connects to the fleet host (SSH); the fleet
  host never connects back. The fleet needs no credential for the supervisor's
  machine.
- **Woken by events, not a clock.** It long-polls the attention log:

  ```bash
  ssh fleet-host fleet.py wait --after "$LAST_ID" --timeout 1800 [--urgent-only] [--item W-12]
  ```

  `wait` returns as soon as there are events after `LAST_ID` (exit 0, JSON
  lines), or exits 2 on timeout. The supervisor stores the last id it handled,
  so a reconnect never loses or repeats events.
- **Heartbeat.** `fleet.py heartbeat --role supervisor` on each wake. If the
  heartbeat is older than `limits.supervisor_offline_after_minutes`, the platform sends
  the human a digest of `human_required` and urgent events instead.
- **Never blocking.** Nothing in the fleet waits for the supervisor.
- **Orchestration only.** `status`, `events`, `wait`, `heartbeat`, `steer`,
  `resume`, and `request <kind>` (e.g. `request rework --item W-12 --message
  ...`, `request dispatch ...`), which the coordinator executes on its next
  batch. It never lands, never marks done, never answers a decision for the
  human, never edits a worktree.

### Restricting the supervisor's key

On the fleet host, give the supervisor's SSH key a forced command so a leaked
key can only run the fleet CLI:

```text
command="/path/to/fleet.py ssh-gate",from="<supervisor-ip>",no-pty,no-port-forwarding,no-agent-forwarding <key>
```

`ssh-gate` reads `SSH_ORIGINAL_COMMAND` and allows only the supervisor
subcommands listed above, without `--human` or `--config`.

## Attention log

Every event the platform emits is one row: `id` (monotonic), `ts`, `item`,
`kind`, `urgent`, `detail`. Urgent kinds: `decision:human_required`,
`result_unknown`, `drift`, `land:failed`, `land:not_landed`.
`fleet.py events --after N` lists them; `wait` is the blocking form.

## Host-specific work: pull queues

Some steps must run on a particular machine (a GUI app, a device, a licensed
tool). They never run over an inbound connection to that machine. Instead:

1. The coordinator writes a request into that host's queue with a predeclared
   `argv`, expected artifacts, and a timeout:
   `fleet.py host-job open ITEM --host mac --argv '["make","sign"]' --timeout 600`.
2. The host pulls its own queue (`fleet.py host-job pull --host mac`) on its
   own schedule, checks the request against its `capabilities.json` (allowed
   argv prefixes), runs it, and writes a receipt: exit code, duration, sha256
   of declared artifacts, stdout/stderr tails.
3. The coordinator reads the receipt as evidence (`kind: host_job`).

A failure creates one rework item. There is no automatic retry and no
automatic rollback. For long multi-step host work, the host runs it with
`fleet-remote-runner`, and the receipt references its status file.

## Runtime on another host

When the agent CLIs themselves run on a separate machine (execution and
credentials stay there, driven over SSH), see
[remote-runtime-host.md](remote-runtime-host.md) for persona isolation,
loopback-only dashboards, failure-domain separation, and credential preflight.

## Interactive sessions

Sessions launched by `fleet.py` are headless and observed through their logs
and HANDOFF. For a session a human started interactively in tmux, use
`fleet-tui-probe` to classify its state read-only; never send
keys to it from the fleet.
