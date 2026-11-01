# Runtime adapters

A runtime adapter tells `fleet.py` how to start and resume a headless session
of one CLI. Adapters are data in `fleet.json`, not code: argv templates with
placeholders.

| Placeholder | Value |
|---|---|
| `{prompt_file}` | file containing the launch or resume message |
| `{cwd}` | the item's worktree or work directory |
| `{model}` / `{effort}` | resolved from the routed tier |
| `{session_id}` | pre-assigned UUID, or the id captured from output |

`session_id_mode` is `assign` (the CLI accepts an id at start) or
`capture` (a regex over the session log finds it).

## Examples

Verify every flag against your installed version (`<cli> --help`); the
examples below were checked against Codex CLI 0.165 and Claude Code 2.1.

### Codex CLI

```json
"codex": {
  "start":  ["codex", "exec", "--json", "-C", "{cwd}", "--sandbox", "workspace-write",
             "-m", "{model}",
             "-c", "model_reasoning_effort=\"{effort}\"", "-"],
  "resume": ["codex", "exec", "resume", "--json", "-c",
             "model_reasoning_effort=\"{effort}\"", "{session_id}", "-"],
  "stdin": "{prompt_file}",
  "session_id_mode": "capture",
  "session_id_regex": "\"thread_id\"\\s*:\\s*\"([0-9a-f-]{36})\"",
  "tiers": {"low": {"effort": "low"}, "medium": {"effort": "medium"},
            "high": {"effort": "high"}, "xhigh": {"effort": "xhigh"}}
}
```

### Claude Code

```json
"claude": {
  "start":  ["claude", "-p", "--session-id", "{session_id}", "--model", "{model}",
             "--effort", "{effort}", "--output-format", "stream-json", "--verbose"],
  "resume": ["claude", "-p", "--resume", "{session_id}", "--effort", "{effort}",
             "--output-format", "stream-json", "--verbose"],
  "stdin": "{prompt_file}",
  "session_id_mode": "assign",
  "tiers": {"low": {"effort": "low"}, "medium": {"effort": "medium"},
            "high": {"effort": "high"}, "xhigh": {"effort": "xhigh"}, "max": {"effort": "max"}}
}
```

`fleet.py` also runs every command with the working directory set to `{cwd}`,
so a resume template without a directory flag still lands in the worktree.

Headless sessions need a permission policy decided up front (e.g. Codex
`--sandbox workspace-write`, Claude Code `--permission-mode` or an allowlist).
Pick the narrowest that lets the worker edit its worktree and run its gates;
put it in the adapter so every generation gets the same policy.

A Git worktree keeps its objects and refs in the main repository's common
directory, outside `{cwd}`. A sandbox limited to the worktree therefore cannot
commit. Allow that directory explicitly (Codex: `--add-dir <git-common-dir>`;
find it with `git rev-parse --git-common-dir`). Pushing also needs network
access from the sandbox, or leave the push to a host job.

## Steering a headless session

Headless CLIs cannot accept input in the middle of a turn. `fleet.py steer`
therefore queues the correction in the item inbox; if the session is idle it
resumes immediately with the inbox, otherwise the platform delivers it on the
next idle. Interrupting a running turn (killing the pid shown by `status`) is a
safety measure only, never a way to deliver a correction faster.

## Adding a runtime

1. Write `start`/`resume` templates and a `tiers` map; confirm each effort
   value is accepted by running a one-line prompt at each tier.
2. Choose `session_id_mode`; test that a resume continues the same session.
3. Decide the permission policy.
4. Run `fleet.py launch <card> --dry-run` and read the argv before the first
   real launch.
