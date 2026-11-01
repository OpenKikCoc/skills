---
name: fleet-tui-probe
description: "Read-only probe of an already-running interactive coding-agent TUI (e.g. Claude Code or Codex CLI) inside remote tmux. Use when you need to know whether such a session is working, idle, waiting on a question/approval, exited, or gone, without sending it any input."
---

# Fleet TUI Probe

Use this skill only for a pre-existing interactive coding-agent TUI (Claude
Code, Codex CLI, or a similar terminal agent) whose state is visible in a
remote tmux pane. It provides a bounded read-only probe; it does not own the
session lifecycle. Use `fleet-remote-runner` for non-interactive
remote workflows. Headless sessions launched and tracked by a dispatch fleet
(cards with worker/reviewer/coordinator roles) are observed through
`fleet-dispatch`; this skill is only for ad-hoc interactive TUIs outside
such a fleet.

## Probe

```bash
PROBE_HOST=<ssh-alias> PROBE_SESSIONS='<exact-glob>' \
  bash <skill-root>/scripts/probe.sh
```

For an already captured fixture, avoid SSH and tmux:

```bash
bash <skill-root>/scripts/probe.sh --from-capture capture.txt
```

Use `--raw` to print the capture (including each pane's
`pane_current_command`) instead of classifying it; this is the quickest way to
check why a session is classified the way it is.

Output is one line per matching session:
`session|state|attention|context|tasks|worked`.

- `state`: `working`, `idle`, `exited` (pane command is not an agent TUI),
  `gone` (session missing or empty pane), and `queued` (only when
  `PROBE_QUEUED_RE` is set).
- `attention` (idle sessions only): `question` (a question or approval prompt
  such as "Do you want to proceed?" is visible), `compact-request` (the agent
  asked for `/compact`), or `low-context` (Codex `Context N% left` below
  `PROBE_LOW_CONTEXT`, default 15).
- `context`, `tasks`, `worked`: Codex footer metadata when present, else empty.

Classification uses only the recent pane tail (`PROBE_STATUS_TAIL`, default 20
lines) so approval text in scrollback is not treated as pending.

## Supported TUIs and tuning

Default patterns live at the top of `scripts/probe-lib.sh` and are all
overridable through the environment (`PROBE_WORKING_RE`, `PROBE_QUESTION_RE`,
`PROBE_QUEUED_RE`, `PROBE_COMPACT_REQ_RE`, `PROBE_AGENT_CMD_RE`):

- Codex CLI: the "Working (… • esc to interrupt)" status, goal/sub-agent
  status lines, multi-question picker hints, and footer metadata.
- Claude Code: the "esc to interrupt" spinner line while working and the
  "Do you want to proceed?" permission prompt. Claude Code does not print the
  Codex context/tasks/worked footer, so those columns stay empty.
- `PROBE_AGENT_CMD_RE` defaults to `^(codex|claude|node)$`: a native Claude
  Code install reports `claude`, an npm-installed Codex reports `node`. Adjust
  it if `--raw` shows something else (for example a version string or a
  wrapper script name).

TUI wording changes between releases. Patterns were checked against Codex
CLI 0.160 and a current Claude Code build; if a session is misclassified,
capture it with `--raw`, adjust the regex via environment, and add the capture
as a fixture in `scripts/test_tmux_probe.py`.

## Safety

The host must be a plain SSH alias and the session selector must be explicit.
The probe validates both before use. `probe.sh` only runs `tmux list-sessions`,
`list-panes`, and `capture-pane -p`; it never sends keys, launches a TUI,
approves actions, or writes remote state.

If intervention is needed, stop probing and act through the owning workflow
(or tell the user). Do not add prompt delivery, retry loops, or key sending to
this skill.

## Tests

```bash
cd <skill-root>/scripts && python3 -m unittest -q test_tmux_probe
```
