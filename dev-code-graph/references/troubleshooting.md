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

## Compatibility guard

The backend hooks `graphify.llm.BACKENDS` and `extract_files_direct`, and calls
the private helpers `_extraction_system`, `_read_files` and `_parse_llm_json`
(repair also calls `_pack_chunks_by_tokens` and `_extract_with_adaptive_retry`).
Private names can change between releases, so before patching anything the
wrapper inspects each function with `inspect.signature` and `bind()`s the exact
call shape it uses. A missing function, a changed signature, or a variadic
signature (`*args` / `**kwargs`, which cannot be verified) stops the run with
`unsupported Graphify` before any extraction or backend registration.

The same check writes a provenance receipt: package version, source URL with
credentials, query and fragment removed, and the sha256 of the loaded
`graphify/llm.py`. It is printed to stderr and, when `GRAPHIFY_COMPAT_RECEIPT`
is set (the fleet orchestrator sets it per target phase), saved as JSON. The
API key is redacted from the receipt; only parameter names, kinds and whether
they are required are recorded, never default values.

Registration is idempotent: calling it again wraps the pristine
`extract_files_direct` saved on the first call, so wrappers never stack.

After upgrading Graphify, run `graphify-fleet doctor` and `smoke-test`, then a
small `fleet build` and read the receipt before a large run.
