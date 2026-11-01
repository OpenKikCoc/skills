# Formats and adapters

Each adapter turns the parsed records of one file into a `Fragment`: a session
ID, optional parent ID, optional exclusion reason, and a list of
`(timestamp, redacted ask)`. Adding a format means adding one adapter function
and registering it in `ADAPTERS`; the report builder is format-agnostic.
Formats evolve, so an unrecognized file is reported as a diagnostic and skipped
rather than guessed.

## Codex CLI rollout JSONL

- First record of type `session_meta`; its `payload` has `id`, optional
  `forked_from_id`, `source`, `thread_source`, `originator`.
- Excluded when `source` is an object with a subagent key, or when
  `thread_source`/role markers say subagent, review, or guardian.
- User asks: `response_item` (or `history_mutation`) records whose payload is
  `type: message`, `role: user`; text parts are `input_text`/`text`.
- Developer/assistant messages, tool calls, and events are ignored.

## Claude Code transcripts

- One record per line with `sessionId`, `type` (`user`, `assistant`,
  `system`, plus bookkeeping types that are ignored), `timestamp`, and
  `message` (`role`, `content` as a string or a list of typed parts).
- Excluded when any record has `isSidechain: true` or an `agentId`
  (subagent transcripts, also stored under `subagents/`).
- User asks: `type: user` records, not `isMeta`, whose content is a string or
  `text` parts. A list containing a `tool_result` part is a tool reply, not an
  ask.

## Harness blocks removed before an ask is judged

`<environment_context>`, `<system-reminder>`, `<user_instructions>`,
`<skill>`, slash-command echo tags (`<command-name>` and friends), local
command output, IDE selection tags, injected `# AGENTS.md instructions`, and
interrupt markers. A message left with only XML tags is dropped.

## Output

JSON: `schema_version`, `summary` (counts), `diagnostics` (sorted), and
`candidates`, each with `id` (`ask-` plus 16 hex of the SHA-256 of the
whitespace-folded lower-cased text), `text`, `first_seen` (UTC or null),
`occurrence_count` (distinct fork lineages), `session_ids`, `adapters`.
Text is capped at 4000 characters.
