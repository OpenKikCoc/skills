# Task Manifest

Use JSON with `schema_version: 1`. Keep commands as argument arrays so the
runner invokes them without a shell.

```json
{
  "schema_version": 1,
  "task_id": "nightly-analysis",
  "remote": {
    "host": "host.example",
    "user": "operator",
    "port": 22,
    "state_dir": "/srv/tasks/nightly-analysis/state",
    "work_dir": "/srv/tasks/nightly-analysis/work",
    "session": "rr-nightly-analysis"
  },
  "steps": [
    {
      "id": "prepare",
      "argv": ["python3", "bin/task.py", "prepare"],
      "cwd": ".",
      "env": {"TASK_MODE": "batch"},
      "timeout_seconds": 1800
    },
    {
      "id": "run",
      "argv": ["python3", "bin/task.py", "run"],
      "cwd": ".",
      "timeout_seconds": 21600
    }
  ],
  "restoration": {
    "steps": [
      {
        "id": "restore-runtime-state",
        "argv": ["python3", "bin/task.py", "restore"],
        "cwd": ".",
        "timeout_seconds": 600
      }
    ],
    "checks": [
      {
        "id": "verify-restored-state",
        "argv": ["python3", "bin/task.py", "verify-restored"],
        "cwd": ".",
        "timeout_seconds": 300
      }
    ]
  },
  "artifacts": [
    {"path": "out/report.json", "required": true},
    {"path": "out/debug.txt", "required": false}
  ],
  "policy": {
    "heartbeat_interval_seconds": 10,
    "heartbeat_timeout_seconds": 45,
    "stall_timeout_seconds": 600,
    "task_timeout_seconds": 86400,
    "default_step_timeout_seconds": 21600,
    "restoration_timeout_seconds": 900,
    "terminate_grace_seconds": 10
  }
}
```

## Rules

- Use a unique `task_id`, tmux `session`, and previously absent `state_dir`.
  Launch fails on a session collision or an existing state directory.
- Make `state_dir` and `work_dir` absolute, non-root POSIX paths.
- Keep every command `id` unique.
- Put the complete finite workflow in `steps`. The runner advances in order and
  stops work at the first failed step.
- Use `restoration.steps` only for pre-reviewed, bounded, idempotent,
  non-destructive restoration. The runner invokes them after success or failure.
- Include at least one `restoration.checks` command. Its zero exit verifies the
  restored state. A receipt is mandatory even when no action is needed.
- Declare artifact files relative to `work_dir`. Required missing artifacts
  make the task fail.
- Set realistic bounds. Log stall means the active combined stdout/stderr file
  has not grown.

## Safety

SSH runs with password prompts disabled. Keep authentication in existing remote
machine facilities. The validator rejects credential-like environment names,
inline credential arguments, bearer material, credential-bearing URLs, and
private-key text. Do not weaken this by wrapping credentials in another file.

Prefer a reviewed executable for complex behavior. Manually reject privilege
escalation, destructive cleanup, infrastructure mutation, indefinite
interaction, and unsafe retries before using `--execute`.
