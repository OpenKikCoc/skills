# Backend troubleshooting

Start with `graphify-fleet doctor` (offline), then
`graphify-fleet smoke-test --json-check`.

| Symptom | Likely cause | Fix |
|---|---|---|
| `GRAPHIFY_LLM_BASE_URL and GRAPHIFY_LLM_MODEL are required` | config not loaded | Export them or point `GRAPHIFY_FLEET_ENV` at a file; `doctor` prints which file was found |
| `API key is missing` | key variable unset, wrong `GRAPHIFY_LLM_API_KEY_ENV`, or helper not configured | Check the variable *name* in `doctor`'s key source line |
| `credential helper failed (... empty output)` | helper prints nothing or needs a login | Run the helper by hand (not echoing to shared logs); it must print only the key |
| `endpoint returned invalid JSON` | base URL points at a web page, login redirect, or a path missing `/v1` | `curl -sS -o /dev/null -w '%{http_code} %{content_type}\n'` the endpoint; fix the base URL |
| HTTP 401/403 | wrong or expired key, key not valid for that model, IP allow-list | Re-issue key; non-retryable so it fails at once by design |
| HTTP 404 | wrong path or model name unknown to the gateway | Compare with the provider's `/models` list |
| HTTP 400 mentioning `max_tokens` | newer models reject `max_tokens` | `GRAPHIFY_LLM_MAX_TOKENS_PARAM=max_completion_tokens` |
| HTTP 429 loops | rate limit | Lower fleet `--parallel` and `--graphify-concurrency`; raise `GRAPHIFY_LLM_RETRY_BASE_SECONDS` |
| `response had no model text` | reasoning model spent the budget thinking, or empty reply | Raise `GRAPHIFY_LLM_MAX_TOKENS`; pick a non-reasoning model |
| smoke-test reply "not parseable JSON" | model wraps/chats instead of returning JSON | Use a stronger/instruction-following model; extraction would produce `partial` chunks |
| Timeouts / `reason=timed out` | slow model on large chunks | Raise `GRAPHIFY_LLM_TIMEOUT_SECONDS` or lower fleet `--token-budget` |
| Works in a shell, fails from the launcher | proxy env differs | `doctor` lists which proxy variables are set; set `no_proxy` for local/private endpoints |
| `graphify is not installed` | launcher cannot find Graphify | Set `GRAPHIFY_BIN` or `GRAPHIFY_PYTHON` |
| Many `partial` targets | transient 429/5xx exhausted retries, or model returns invalid JSON | Run `fleet repair`; if it persists, fix the backend before rebuilding |

Rules while debugging: never print the key, never paste request URLs with
credentials embedded into tickets, and prefer `doctor` over dumping the
environment.

## Compatibility

The backend hooks `graphify.llm.BACKENDS`, `extract_files_direct`,
`_extraction_system`, `_read_files`, and `_parse_llm_json`. Private names can
change between releases; they were checked against `graphifyy` 0.9.100. After
upgrading Graphify, run `graphify-fleet doctor` and `smoke-test` before a fleet
build.
