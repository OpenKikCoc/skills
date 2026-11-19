# Pane fixture contract

[assets/fixtures/panes.json](../assets/fixtures/panes.json) is the shared,
synthetic corpus that drives the tests. It contains invented pane text only;
never add a real capture. Consumers in other skills locate it under this skill
in the same skills root; if it is absent, report the missing dependency rather
than a passing check.

Schema version 1 has a `fixtures` array. Each entry supplies `id`, `command`
(tmux pane current command), `pane` (UTF-8 captured text), expected `state`, and
optional `attention` (default `-`). Pane state is a reading of the screen, not
the agent's real task state.

The default classifier first rejects empty/missing commands (`unknown`) and
non-agent commands (`exited`), then reads status events in order from the last
20 non-blank lines. Explicit `Approved.` / `Approval resolved` and `Goal
resumed` clear only their matching blocker. Later working/queued/question
evidence without a matching resolution is contradictory (`unknown`); a fresh
blocker after working remains approval/paused. Compaction is working, but a
generic "esc to cancel" footer is not. Blocker text outside the recent tail has
no authority. Environment regex overrides are operator-selected heuristics, not
corpus defaults.

For offline classification wrap each pane in `@@SESSION <id> <command>` and
`@@END` lines and pass the file to `probe.sh --from-capture`. `@@GONE <name>` is
reserved for a successful remote listing proving an exact selector absent; an
empty `@@SESSION` stays `unknown`. A failed live collection emits `probe_error`
rows for the requested selectors and exits 3, even in `--raw` mode; partial
output is discarded. Exit 2 is invalid input. Neither is authority to recreate a
session or send input.

Live captures encode each pane as one `@@DATA <base64>` line between its
SESSION and END records (an empty pane yields an empty DATA record). Reserved
marker text inside a pane is therefore ordinary encoded data. The decoder
rejects invalid ordering, encoding, duplicate identities, and records outside
the requested selectors before emitting any state. Plaintext wrappers are
accepted only offline and cannot contain reserved marker lines.

The collection deadline covers SSH connection, enumeration, every pane read,
local parsing/classification, and output backpressure, not one allowance per
tmux command. Transport output lives in private 0700 directories and 0600
files, and external error text is never forwarded. The supervisor reaps only
its own observer process group, never the remote TUI. Final timeout output is
best effort and nonblocking. This is not a remote-task execution timeout.
