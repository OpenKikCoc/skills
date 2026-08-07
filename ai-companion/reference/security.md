# Security model

This runs a local HTTP server that shells out to a CLI with real filesystem
and (via `--allow-all-tools` for copilot) shell/tool access. The defaults
below aren't optional hardening — they're the reason this is safe to run at
all on a personal machine.

## Binds to 127.0.0.1 only

`ThreadingHTTPServer(("127.0.0.1", port), Handler)` — never `0.0.0.0`. Only
processes on the same machine can reach it, full stop. There is no flag to
change this; if you need remote access, put a real reverse proxy with auth
in front of it yourself rather than exposing the bridge directly.

## Origin check on every POST

`_origin_ok()` rejects any `POST` whose `Origin` header isn't
`http://127.0.0.1*` or `http://localhost*` (a same-machine request with no
`Origin` header — e.g. `curl` — is allowed through, since it could only come
from something already running locally). Without this, **any other tab open
in your browser** could silently `fetch('http://127.0.0.1:8787/api/ask', ...)`
and drive your local AI session. `GET`s aren't origin-checked since they're
side-effect-free and a page could always trigger one via an `<img>`-style
request anyway.

## Static serving is scoped to the script's own folder — never `--repo`

Two different directories, two different trust levels:

- **`SCRIPT_DIR`** (wherever `ai_companion_server.py` lives) is served over
  HTTP by `SimpleHTTPRequestHandler`. This is meant to be small and public —
  the page itself, maybe a couple of assets.
- **`--repo`** (or the Settings-set codebase path) is only ever passed as
  the CLI subprocess's `cwd`. It is *never* added to the HTTP server's
  directory. Pointing `--repo` at a large private codebase does not put that
  codebase on the network, even to localhost — the only way its contents
  leave the process is through whatever the AI CLI chooses to say back.

Keep it that way if you extend the server: resist the urge to serve `--repo`
directly even for convenience (e.g. "let me browse the repo in-page") — that
reintroduces exactly the exposure this split exists to avoid.

## The native file/folder picker is server-side on purpose

`window.showOpenFilePicker()` / `<input type=file>` deliberately do not hand
back a real filesystem path — that's a browser security boundary, not an
oversight, and it can't be worked around from page JS. `/api/browse` instead
shells out to a real OS dialog (`osascript`, PowerShell's
`System.Windows.Forms`, or `zenity`) and returns the path the *user*
selected in that native dialog. This is legitimate specifically because the
server only runs on localhost, launched by the same person using the page —
it is not a pattern to reach for in anything that isn't a personal local
tool.

## `Cache-Control: no-store` on everything

Not a security control, but adjacent: without it, a browser can silently
keep serving a stale page (old JS, old behavior) after you've redeployed a
newer script, which is confusing enough to look like a bug. Every response
sets it.

## What this does *not* protect against

- Another **local user account** on a shared machine can still reach
  `127.0.0.1` — this is a single-user-machine tool, not a multi-tenant one.
- `--allow-all-tools --no-ask-user` (used for the `copilot` backend) grants
  broad unattended tool access *to the CLI's own sandboxing*, not to this
  server. Whatever `claude`/`copilot` itself is willing to do unattended, it
  will do. Don't point `--repo` at something you wouldn't hand that CLI
  access to directly from a terminal.
