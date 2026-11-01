# HTTP API — the panel ↔ bridge server protocol

`ai_companion_server.py` serves the folder it sits in as static files and
exposes a small JSON API under `/api/*`. The panel talks to it with plain
`fetch()`, same-origin (the page is opened *through* the bridge server, not
via `file://`, so no CORS handling is needed — see `security.md`).

Every response carries `Cache-Control: no-store`. The script gets redeployed
as it's updated; a stale cached response silently serving old behavior is a
worse failure mode than never caching.

## `GET /api/health`

Polled every 4s by the panel to drive the connected/disconnected indicator.

```json
{
  "ok": true,
  "backend": "claude",
  "backend_path": "/Users/you/.local/bin/claude",
  "repo": "/Users/you/code/some-repo",
  "session_id": "e6c1d255-...",
  "turns": 3,
  "version": "1.0.0"
}
```

`ok` is `false` (not an HTTP error) when neither `claude` nor `copilot` could
be resolved — the server still runs and serves static files either way.

## `POST /api/ask`

```json
{
  "question": "What does this function do?",
  "context_title": "Billing pipeline",
  "context_text": "...(visible text of whatever's on screen)...",
  "context_index": 3,
  "context_count": 15
}
```

Only `question` is required. `context_*` fields describe whatever the panel
resolved as "the current view" (see `context-provider.md`) — the server
folds them into a `[Currently viewing ...]` block prepended to the prompt.
`context_index`/`context_count` are optional integers used only to render
"item N/M" in that block.

Success:
```json
{ "result": "...", "session_id": "e6c1d255-..." }
```
Cancelled mid-flight (see `/api/cancel` below):
```json
{ "cancelled": true }
```
Failure: HTTP 400 (empty question), 500 (backend error / none found), or 504
(timed out after 600s) with `{ "error": "..." }`.

Turns are serialized through a single lock — this is a **single continuous
session**, not a pool. A second `/api/ask` while one is in flight simply
waits its turn.

## `POST /api/cancel`

No body. Terminates the in-flight CLI subprocess, if any:
`{ "ok": true, "cancelled": true|false }`. The panel also aborts its own
`fetch` locally via `AbortController` so the UI updates immediately rather
than waiting for the killed process's response to trickle back.

## `POST /api/reset`

No body. Clears `session_id` and the turn log — the next `/api/ask` starts a
fresh CLI session. `{ "ok": true }`.

## `GET /api/history`

`{ "session_id": "...", "log": [{ "q", "a", "context", "ts" }, ...] }` — lets
the panel repopulate the chat log after a page refresh mid-session.

## `GET /api/settings` / `POST /api/settings`

GET returns current overrides, resolved backend, and repo path:
```json
{ "overrides": { "copilot": "C:\\...\\copilot.cmd" }, "backend": "claude", "backend_path": "...", "repo": "..." }
```

POST accepts either or both fields, applies immediately (no restart), and
persists to `.ai-companion-config.json` next to the script:

- `copilot_path` — absolute path to a `copilot` executable, or `""` to clear
  the override and fall back to PATH auto-detection. 400 if the path doesn't
  exist.
- `repo_path` — absolute path to the codebase directory. 400 if it's not a
  directory. If it actually changes (resolves to a different path than the
  current one), the session is reset — the response includes
  `"repo_changed": true` so the panel knows to clear its own log too.

## `GET /api/copilot-candidates`

`{ "candidates": ["/usr/local/bin/copilot", ...] }` — known install
locations that actually exist on this machine, beyond plain PATH lookup.
Shown as one-click choices in Settings before the user needs to browse or
type a path by hand.

## `POST /api/browse`

```json
{ "kind": "file" }   // or "dir"
```
Pops a **native OS file/folder picker** (blocks the request until the user
picks or cancels) and returns `{ "path": "..." }` (`null` if cancelled or no
picker tool is available on this platform). See `security.md` for why this
has to be server-side.

## Origin check

Every `POST` checks `Origin` and rejects (403) anything that isn't
`http://127.0.0.1*` or `http://localhost*` — any web page open in the same
browser could otherwise `fetch()` your local bridge.
