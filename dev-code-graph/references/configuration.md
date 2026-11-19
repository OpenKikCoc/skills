# Configuration

## Variables

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `GRAPHIFY_LLM_BASE_URL` | yes | | Base URL, usually ending in `/v1`. `/chat/completions` is appended unless already present |
| `GRAPHIFY_LLM_MODEL` | yes | | Model name sent in the request |
| `GRAPHIFY_LLM_API_KEY_ENV` | no | `OPENAI_API_KEY` | Name of the env var that holds the key |
| `GRAPHIFY_LLM_API_KEY_COMMAND` | no | | Credential helper, split with `shlex` and run without a shell (30 s timeout). Stdout is the key; wins over the env var |
| `GRAPHIFY_LLM_MAX_TOKENS` | no | `16384` | Completion budget |
| `GRAPHIFY_LLM_MAX_TOKENS_PARAM` | no | `max_tokens` | Set `max_completion_tokens` for models/gateways that reject `max_tokens` |
| `GRAPHIFY_LLM_TIMEOUT_SECONDS` | no | `600` | Per-request timeout (fleet `--api-timeout` overrides it per build) |
| `GRAPHIFY_LLM_MAX_RETRIES` | no | `6` | Extra attempts on 408/409/425/429/5xx and network errors |
| `GRAPHIFY_LLM_RETRY_BASE_SECONDS` | no | `10` | Exponential backoff start; `Retry-After` is honored but capped |
| `GRAPHIFY_LLM_RETRY_MAX_SECONDS` | no | `120` | Backoff cap |
| `GRAPHIFY_BIN` / `GRAPHIFY_PYTHON` | no | from `PATH` | Graphify executable / its interpreter |

Auth is `Authorization: Bearer <key>`. The key never appears in the URL.

## Env file (optional, non-secret by preference)

First existing path wins: `$GRAPHIFY_FLEET_ENV`, `$GRAPHIFY_FLEET_HOME/.env`,
`$XDG_CONFIG_HOME/graphify-fleet/.env`, `~/.config/graphify-fleet/.env`. If
`GRAPHIFY_FLEET_ENV` or `GRAPHIFY_FLEET_HOME` is set but the file is missing,
loading fails instead of falling back. Files accessible by group/other are
rejected. Real environment variables are never overridden by file values.

Example (no key in it):

```
GRAPHIFY_LLM_BASE_URL=https://llm.example.com/v1
GRAPHIFY_LLM_MODEL=my-model
GRAPHIFY_LLM_API_KEY_COMMAND=pass show llm/api-key
```

## Which commands need credentials

Fleet `inventory`, `build --dry-run`, `merge`, `audit` do not. `build` needs
them only when its launcher starts semantic extraction; `repair` needs them
before replaying failed chunks.

## Retry ownership

The adapter owns transient network retries. The fleet layer adds no second
retry loop: it records failed chunks as `partial` so they can be repaired
deterministically. Keep adapter retry tests separate from fleet tests.

## Variables set by the fleet orchestrator

Do not set these by hand: `GRAPHIFY_OUT`, `GRAPHIFY_DISABLE_AST_PARALLEL`,
`GRAPHIFY_OPERATION_ID` (binds receipts to one attempt) and
`GRAPHIFY_COMPAT_RECEIPT` (where the Graphify compatibility receipt is written).
