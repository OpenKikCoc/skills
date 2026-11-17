# Host Capabilities

A box should run standalone. Some hosts add a bridge (a global object, a
`postMessage` protocol, or URL parameters). Treat every capability as
optional, discover it at run time, and degrade visibly.

## Contents

1. Capabilities to check before relying on them
2. Bridge pattern
3. State and persistence
4. Safety rules

## 1. Capabilities to check before relying on them

Confirm each against the host's own documentation or a disposable test page.
Do not invent methods or guess signatures.

| Question | Why it matters |
|---|---|
| Is the frame sandboxed (`allow-scripts`, `allow-same-origin`, `allow-forms`, `allow-popups`)? | Decides storage, forms, and links |
| Is the frame size fixed or responsive? Is the content width exact? | Decides the viewport to verify |
| Are external network requests allowed (CSP, `connect-src`)? | Decides whether any `fetch` can work |
| Are fonts, images, and scripts from other origins allowed? | Decides what must be inlined |
| Is `localStorage`/cookies available? | Often not in `srcdoc` or opaque origins |
| Does the host expose page context (title, viewer, metadata)? | Optional personalization |
| Does the host expose persistence, and with what scope (per viewer, shared, per copy)? | Shared writes affect every viewer |
| Can the box open links, navigate, or resize itself? | Decides link handling and height |
| Does the host rewrite or minify stored HTML? | Affects hash verification |
| Is there a size limit on stored HTML? | Decides inlining budgets |
| Does export or print render scripts? | Decides whether the static first paint carries the meaning |

## 2. Bridge pattern

Feature-detect, guard, catch, and keep a standalone view. The shipped
template `assets/html-box-host-817x560.html` follows this shape:

```js
const host = window.htmlBoxHost;            // illustrative name
if (!host || typeof host.getContext !== "function") {
  showStandaloneView();                      // not an error for the reader
  return;
}
try { render(await host.getContext()); }
catch (e) { showError(e && e.message); }
```

For `postMessage` bridges, post a request with a unique id, accept replies
only from the expected `event.source` and origin, and time out into the
degraded view.

Verify with and without the bridge (`browser-verification.md`, stub script).

## 3. State and persistence

- Prefer in-memory state; a reload may reset it and that should be acceptable.
- No `localStorage`/`sessionStorage` unless you confirmed they work in the
  host frame (`--allow-storage` in the validator).
- Decide scope deliberately: private to one viewer, shared among viewers,
  tied to one copy of the box or shared across copies. Shared writes need a
  clear action, a visible actor, validation, and concurrency handling.
- Never persist credentials, tokens, or sensitive free text.

## 4. Safety rules

- Side effects (writes, requests, messages, redirects) require an explicit
  user gesture and a visible result.
- No secrets or identities in source; the validator flags common patterns.
- Show errors safely: no tokens, raw responses, or unnecessary personal data.
- Never ship stub scripts or fixture data used for local verification.
