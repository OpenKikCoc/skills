---
name: dev-code-graph
description: "Build Graphify code knowledge graphs with any OpenAI-compatible LLM endpoint for semantic extraction, for one repository or a resumable, disk-aware fleet of many (inventory, build, repair, merge, audit). Includes endpoint validation, backend troubleshooting and credential-safety rules. Use when you want a queryable graph of one or more codebases; not for plain code search or when no LLM endpoint is available (Graphify's AST-only mode needs none)."
---

# Code Graph (Graphify)

Wrapper around Graphify (the open-source code knowledge-graph tool) that plugs
any OpenAI-compatible `/chat/completions` endpoint in as its semantic
extraction backend (registered as `openai-compat`), plus a fleet orchestrator
for indexing many repositories. Graphify must already be installed; the
launcher runs under the Python interpreter that owns the `graphify` command
(override with `GRAPHIFY_PYTHON` / `GRAPHIFY_BIN`).

## Configure

Set these in the environment or in an optional `0600` env file
(`$GRAPHIFY_FLEET_ENV`, `$GRAPHIFY_FLEET_HOME/.env`, or
`~/.config/graphify-fleet/.env`; real environment values win):

| Variable | Meaning |
|---|---|
| `GRAPHIFY_LLM_BASE_URL` | e.g. `https://llm.example.com/v1` (required) |
| `GRAPHIFY_LLM_MODEL` | model name (required) |
| `GRAPHIFY_LLM_API_KEY_ENV` | name of the env var holding the key (default `OPENAI_API_KEY`) |
| `GRAPHIFY_LLM_API_KEY_COMMAND` | credential helper; prints the key on stdout, takes priority |

Keep the key out of the env file when you can: prefer the helper command (for
example a secret-manager CLI). Full variable list and retry knobs:
[configuration.md](references/configuration.md).

## Workflow

1. `scripts/graphify-fleet doctor` shows the resolved config offline (key
   value hidden).
2. `scripts/graphify-fleet smoke-test --json-check` probes the endpoint and
   checks the model can return parseable JSON. Run it whenever the endpoint,
   model, key, or network changes. If it fails, read
   [troubleshooting.md](references/troubleshooting.md).
3. Single repository: `scripts/graphify-fleet extract <path>`; extra
   `graphify extract` options pass through and `--backend openai-compat` is
   added unless you give a backend.
4. Use the installed `graphify` command directly for query, path, explain, and
   visualization; those need no LLM.

## Fleet indexing (many repositories)

```bash
scripts/graphify-fleet fleet inventory --root <fleet-root> --output <out>
scripts/graphify-fleet fleet build --root <fleet-root> --output <out> --dry-run
scripts/graphify-fleet fleet build --root <fleet-root> --output <out> \
  --parallel 3 --disk-floor-bytes 5368709120
scripts/graphify-fleet fleet repair --root <fleet-root> --output <out>
scripts/graphify-fleet fleet merge  --root <fleet-root> --output <out> \
  --compact-limit-bytes 536870912
scripts/graphify-fleet fleet audit  --root <fleet-root> --output <out> \
  --compact-limit-bytes 536870912
```

- Inventory, dry-run, merge and audit never load credentials.
- A Graphify exit code of zero is still `partial` when the log shows failed
  semantic chunks; `repair` replays only those chunks into a separate
  fragment, and `merge` combines base graphs and fragments.
- Builds are resumable: `success` and `repaired` targets are skipped unless
  `--force`. The disk floor is checked before every target launch.
- Do not call a fleet complete until `audit` passes.

Read [fleet-indexing.md](references/fleet-indexing.md) before a large run or
when using custom roots, manifests, launchers, or compact limits.

## Credential safety

- Never put a key or env file with a key inside this skill directory, a fleet
  output tree, a log, or a repository. Errors and previews are redacted, but
  do not rely on that: do not `echo`/`cat` keys while debugging.
- Env files that are group- or world-accessible are rejected.
- Fleet defaults exclude `secrets/`, `.env*` and cache directories; keep them
  unless a reviewed manifest says otherwise.
- Behind a proxy, the standard `https_proxy` / `no_proxy` variables are
  honored by the adapter; `doctor` reports when they are set.

## Tests

```bash
python3 -I scripts/test_graphify_fleet.py
python3 -I scripts/test_fleet_index.py
```

Both use stubs (loopback HTTP server, fake Graphify launcher); no network.
