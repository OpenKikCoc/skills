---
name: fleet-tui-probe
description: "Read-only probe of an already-running interactive coding-agent TUI (e.g. Claude Code or Codex CLI) inside remote tmux. Use when you need to know whether such a session is working, idle, paused, waiting on an approval or question, exited, gone, or unknown (probe failed), without sending it any input."
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

- `state`: `working`, `idle`, `approval` (a permission card such as Claude Code
  "Do you want to proceed?" is visible), `paused` (goal paused), `exited` (pane
  command is not an agent TUI), `gone`, `unknown`, `probe_error`, and `queued`
  (only when `PROBE_QUEUED_RE` is set).
- `attention` (idle sessions only): `question` (multi-question picker waiting),
  `compact-request` (the agent asked for `/compact`), or `low-context` (Codex
  `Context N% left` below `PROBE_LOW_CONTEXT`, default 15).
- `context`, `tasks`, `worked`: Codex footer metadata when present, else empty.

A failed collection is never `gone`. Any ssh, tmux, parse, or timeout failure
discards partial captures, prints `probe_error` rows for the requested
selectors, and exits 3. `gone` comes only from an exact-name selector that a
successful session listing did not contain; wildcard selectors with no match
print no rows. An empty pane or missing pane command is `unknown`, never proof
of exit. Exit 2 means invalid input.

Classification reads status events in order from the last 20 non-blank lines
(`PROBE_STATUS_TAIL`). Approval text in scrollback is not pending. `Approved.`
or `Approval resolved` clears only an approval block, `Goal resumed` only a
pause. Working, queued, or question evidence after an unresolved blocker is
contradictory and reported as `unknown`; a fresh blocker after working wins. A
bare "esc to cancel/resume" footer is not working; only "esc to interrupt" in
the status line, compaction, and goal/sub-agent lines are.

Remote output is untrusted: each pane is sent as one `@@DATA <base64>` line, and
a local decoder validates ordering, encoding, duplicates, and that every record
matches a requested selector before any state is emitted, so pane text cannot
forge control records. Plaintext panes are accepted only for offline captures.
`--raw` prints the validated capture decoded, with pane lines indented.

The whole collection (ssh, enumeration, all pane reads, parsing, output) has one
hard deadline: `--timeout-seconds` or `PROBE_COLLECTION_TIMEOUT`, default 30,
finite and positive. A local supervisor in its own process group, with private
temp files and bounded output, kills only the local observer on expiry; the
result is `probe_error` plus an observer timeout message, never proof that a
session is gone and never an action on remote work. Local `python3` is required.

The remote shell may lack Homebrew in `PATH`. `tmux` is resolved from `PATH`,
then `/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin`; a missing binary is a
probe failure. The shared synthetic fixture corpus and its contract are in
[pane-fixtures.md](references/pane-fixtures.md).

## Supported TUIs and tuning

Default patterns live at the top of `scripts/probe-lib.sh` and are all
overridable through the environment (`PROBE_WORKING_RE`, `PROBE_PAUSED_RE`,
`PROBE_APPROVAL_RE`, `PROBE_QUESTION_RE`, `PROBE_QUEUED_RE`,
`PROBE_COMPACT_REQ_RE`, `PROBE_AGENT_CMD_RE`):

- Codex CLI: the "Working (… • esc to interrupt)" status, goal/sub-agent
  status lines, multi-question picker hints, and footer metadata.
- Claude Code: the "(... esc to interrupt)" spinner line while working and the
  "Do you want to proceed?" permission prompt (state `approval`). Claude Code does not print the
  Codex context/tasks/worked footer, so those columns stay empty.
- `PROBE_AGENT_CMD_RE` defaults to `^(codex|claude|node)$`: a native Claude
  Code install reports `claude`, an npm-installed Codex reports `node`. Adjust
  it if `--raw` shows something else (for example a version string or a
  wrapper script name).

TUI wording changes between releases. Patterns were checked against Codex
CLI 0.160 and a current Claude Code build; if a session is misclassified,
capture it with `--raw`, adjust the regex via environment, and add a synthetic
fixture to `assets/fixtures/panes.json`.

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
