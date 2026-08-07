---
name: ai-companion
description: >-
  Embed a floating "ask the AI" chat panel into any HTML page, backed by a
  small local Python bridge server that shells out to a real CLI coding
  assistant (Claude Code's `claude`, or GitHub Copilot CLI's `copilot` on
  Windows) so it can read actual source code and hold a real multi-turn
  session — no API key, billed through the CLI's own subscription. Every
  question automatically includes whatever's on screen as grounding context.
  Use when the user wants an in-page AI Q&A widget for a deck, a doc, a
  dashboard, or any other HTML artifact they'll open locally or share as a
  file, and wants it to answer from a real codebase rather than only the
  page's own text.
---

# ai-companion

A floating chat widget (toggle button + panel: chat, Settings) plus a local
Python HTTP server the widget talks to. The server serves the page itself as
static files and bridges questions to a local CLI AI session — the AI can
read a real codebase (`--repo`, changeable later from Settings) because it's
an actual `claude`/`copilot` CLI process, not a hosted API call. One
continuous session is kept via `--resume`, so follow-ups retain context.

Everything (panel markup/CSS/JS *and* the server's own Python source) lives
in one paste-ready fragment: the page stays a single shareable HTML file,
and the fragment carries a "Export script" button that hands the user the
companion server as a real `.py` file with one click, no separate download
to remember to attach.

## When to reach for this

- The user wants an AI Q&A affordance embedded in a deck, internal doc,
  generated report, or any other standalone HTML page — especially one that
  gets copied/shared as a file rather than deployed behind a real backend.
- The user wants answers grounded in a *real codebase* the page is about,
  not just the page's own text, and doesn't want to pay per-token for it —
  this rides on the user's existing Claude Code / Copilot CLI subscription.
- The user wants this to work for whoever they hand the file to, on
  Windows or macOS, without assuming which AI CLI that person has.

Not for: a hosted product (use a real backend + API key there — this is a
`file://`/localhost pattern), or a page where "read the actual source" isn't
needed (if the page's own content is all the AI should ever see, a plain
API-key chat widget is simpler — see the "why not simpler" note below).

## Quickstart

1. Copy `assets/` into your project (or just the three files:
   `ai-companion.html`, `ai_companion_server.py`, `build.py`).
2. Open `ai_companion_server.py` and edit `SYSTEM_HINT` — name your
   page/product and what "the codebase" means for it. That's the only
   required customization.
3. If your page has a "current section" concept (tabs, slides, wizard
   steps), define `window.AI_COMPANION_CONTEXT()` — see
   `reference/context-provider.md`. Otherwise skip this: the default
   (whole-page title + text) just works.
4. Build: `python3 assets/build.py --into your-page.html` inserts the panel
   right before `</body>`, embedding the current `ai_companion_server.py` as
   the exportable script. Re-running is idempotent (replaces, doesn't
   duplicate).
5. Try it: `python3 ai_companion_server.py` (copy it next to `your-page.html`
   first, or pass `--repo`), open the URL it prints, ask something.
6. Verify: `python3 assets/verify.py` — checks the assets, builds into a
   throwaway plain page (deliberately with none of any host page's styling,
   to prove the fallbacks hold up), boots the server, and exercises the API.
   Python 3.8+, standard library only. Runs a real end-to-end `/api/ask` if
   `claude` or `copilot` is on PATH, skips that one check otherwise.

Read `reference/protocol.md` for the full HTTP API, `reference/security.md`
for exactly what the localhost/origin/picker/repo-scoping guarantees are
(don't loosen any of them without re-reading why), and
`reference/context-provider.md` before wiring up a custom context hook.

## Files

```
ai-companion/
  SKILL.md                     # this file
  assets/
    ai-companion.html          # the panel: CSS + markup + JS, paste-ready;
                                #   ships with a ___BRIDGE_SOURCE___ placeholder
    ai_companion_server.py     # the bridge server -- CUSTOMIZE SYSTEM_HINT here
    build.py                   # merges the two above; --into inserts/replaces
                                #   in a target page, idempotently
    verify.py                  # self-check: build + boot + hit the API,
                                #   stdlib only, skips what needs a human/CLI
  reference/
    protocol.md                # full HTTP API: every /api/* route, request/
                                #   response shapes, error cases
    context-provider.md        # window.AI_COMPANION_CONTEXT contract +
                                #   a worked live-deck example
    security.md                # why localhost-only, origin checks, the
                                #   static-serve/--repo split, and the
                                #   server-side native picker are load-bearing
```

## Invariants to preserve when editing the system itself

- Static HTTP serving stays scoped to the server script's own folder; the
  `--repo` codebase path is only ever a subprocess `cwd`, never added to the
  HTTP server's directory (`reference/security.md`).
- Every mutating endpoint (`POST`) keeps the `Origin` check — dropping it
  lets any other open tab drive the local AI session.
- `window.AI_COMPANION_CONTEXT` stays optional with a working default; don't
  make the panel require a page-specific hook to function.
- Every CSS custom property the panel reads keeps its fallback value — the
  panel must render correctly on a page that defines none of them.
- `build.py --into` stays idempotent (replace via the `ai-companion:start`/
  `:end` markers, don't duplicate on re-run).

## Why not simpler (a plain API-key chat widget)?

That's a real, simpler alternative when "read the actual source" isn't
needed: embed the page's own text as context and call a hosted API directly
from the browser with a user-supplied key — genuinely zero local process,
works from a double-clicked `file://` page. This skill exists for the case
that alternative can't cover: grounding answers in a codebase too large (or
too sensitive) to inline into the page, using a subscription the user
already pays for instead of metered API tokens. If you don't need that,
don't reach for this — the local-process requirement is a real cost.
