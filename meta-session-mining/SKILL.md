---
name: meta-session-mining
description: "Mine coding-agent session logs (Codex CLI rollout JSONL, Claude Code transcripts) into deterministic, redacted user-request inventories without modifying the sources. Use for periodic audits of what users actually asked, to find recurring requests that deserve a skill or memory entry. Not for reading a single conversation in depth, summarizing assistant output, or any task that must see unredacted text."
---

# Session Mining

A read-only, standard-library parser that turns session logs into a stable
list of user-request candidates. Treat all inputs as read-only; the script
never writes files, so redirect stdout only to a new path.

## Workflow

1. Pick explicit inputs (files or directories; searched recursively for
   `*.jsonl`). Typical roots, when the user wants them mined:
   - Codex CLI: `~/.codex/sessions/` (`rollout-*.jsonl`)
   - Claude Code: `~/.claude/projects/<project>/*.jsonl`
2. Run:

   ```bash
   python3 <skill-root>/scripts/session_miner.py \
     --format json --since 2030-01-01 --until 2030-01-31 \
     <file-or-dir> [...] > inventory.json
   ```

   `--format markdown` is for reading; JSON is for reuse. `--adapter` forces a
   format; the default auto-detects each file.
3. Read `summary` and `diagnostics` first: excluded subagents, unrecognized
   files, malformed lines (reported by count, never echoed).
4. Incremental review: pass the previous JSON report back with
   `--seen inventory.json`; already-seen candidate IDs are omitted.
5. Review candidates by hand. A request that recurs across lineages
   (`occurrence_count >= 2`) is a candidate for a skill or memory update; route
   it through `meta-retrospective` rather than editing anything directly.

## Guarantees

- Only primary sessions: subagent, sidechain, and reviewer/guardian sessions
  are excluded; tool results and harness blocks (environment context,
  system reminders, slash-command echoes, injected instructions) are not asks.
- Forks are deduplicated: one ask counts once per fork lineage; sessions that
  share an ID are merged.
- Redaction before output: private keys, JWTs, bearer/auth headers, secret
  query/flag/assignment values, URL passwords, common key prefixes, emails,
  home-directory paths, IPv4 addresses. This is pattern-based defense in
  depth, not a guarantee; review before sharing outside the trust boundary.
- Stable output: no generation time, no source paths, sorted everywhere,
  content-derived IDs. Same inputs in any order give identical bytes.
- Date filters use UTC and the message timestamp; an ask with no timestamp is
  dropped when a filter is active (counted in the summary).

Field names and per-format rules: [formats.md](references/formats.md).

## Validation

```bash
python3 -I <skill-root>/scripts/test_session_miner.py
```
