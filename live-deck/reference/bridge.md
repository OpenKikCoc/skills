# deck-bridge:v1 — the shell ↔ slide postMessage protocol

Every slide ends with the `deck-bridge:v1` script (copy it verbatim from
`assets/slide-template.html`). Its first line is:

```js
if (window.parent === window) return;   // opened directly: do nothing at all
```

so the bridge is completely **inert when a slide is opened standalone** — a
slide file double-clicked from disk behaves exactly as authored, with its own
buttons and keyboard handling and zero deck coupling.

All messages are plain objects with a `type` field. Both sides post with
targetOrigin `'*'` and the shell identifies senders by comparing
`event.source` against each iframe's `contentWindow` (required anyway,
because `srcdoc`/`file://` documents have opaque origins). Nothing sensitive
crosses the bridge; tighten the origins if you ever host slides cross-origin.

## The real slide-side contract

The bridge does not touch the slide's step logic. It drives the slide through
its **public controls** and reads its **visible state**:

- clicks `#next-step`, `#prev-step`, `#play-toggle`, `#reset-view`
- scrapes `#progress` text, expecting the form `"<step> / <total>"`
- reads `disabled` off `#prev-step` / `#next-step` for `atStart` / `atEnd`
- detects a running autoplay by `#play-toggle`'s label matching `/pause|stop/i`

So any slide that keeps those five element ids and the `n / m` progress text
is fully deck-drivable without writing a line of bridge-aware code.

## Shell → slide

| Message        | Sent to             | Effect in the slide                                        |
| -------------- | ------------------- | ---------------------------------------------------------- |
| `deck:next`    | Explore copy        | click Next (advance one build step)                        |
| `deck:prev`    | Explore copy        | click Previous                                             |
| `deck:play`    | Explore copy        | click Play/Pause (toggle autoplay)                         |
| `deck:reset`   | Explore copy        | click Reset (back to step 0)                               |
| `deck:pause`   | every non-current slide on navigation | click Play only if its label says Pause/Stop — stops a stray autoplay |
| `deck:fit`     | current slide when it becomes current, on window resize, on present-mode toggle, on shell load | slide dispatches its own `resize` event, re-running `fitStage()`; then reports state |
| `deck:state`   | any slide           | request an immediate `slide:state` report                  |
| `deckui:cursor`| Explore copy        | `{ value }` — set/clear `document.documentElement.style.cursor` (used for the grab/grabbing pan cursor) |

In the main deck view the slide iframes are `pointer-events: none` and the
shell pages between slides itself — `deck:next/prev/play/reset` are only ever
sent to the interactive copy inside the **Explore overlay**, which is where
step-through builds are driven.

## Slide → shell

| Message       | Payload                                             | When                                                        |
| ------------- | --------------------------------------------------- | ----------------------------------------------------------- |
| `slide:ready` | —                                                   | once, when the bridge script runs; the shell replies `deck:state` |
| `slide:state` | `{ hasSteps, step, total, atStart, atEnd }` (booleans/ints) | after load, after every step change, and on request. Reported via a **double `requestAnimationFrame`** so the DOM has repainted before it is scraped |
| `slide:key`   | `{ key }`                                           | a navigation key was pressed while focus was inside the slide |

Key forwarding details: the bridge listens in the **capture phase**, filters
to a fixed `NAV` set (arrows, paging, Home/End, Space, Escape, `?`, `o n f p
r`, digits), calls `preventDefault()` **and** `stopImmediatePropagation()` so
the slide's own document-level keydown handler never double-acts, and posts
the key up. Keys typed into inputs/textareas/contenteditable, chords with
modifiers, and Space/Enter on a focused button are all left alone.

What the shell does with `slide:key`:

- from a **stage** slide: feeds its normal deck keyboard handler (arrows page
  slides, `o` overview, `n` notes, `f` present, digits jump, ...).
- from the **Explore copy**: remaps to build stepping — ArrowRight/Down/
  PageDown/Space → `deck:next`, ArrowLeft/Up/PageUp → `deck:prev`, Home/`r` →
  `deck:reset`, `p` → `deck:play`, Escape → close the overlay.

## `deckui:*` — Explore input forwarding

The Explore overlay shows an *interactive* copy of the current slide, so the
parent cannot observe pointer or wheel input over that iframe. When opening
the overlay the shell injects a small reporter script into the copy's `<head>`
(`deck-explore-reporter`; it is injected at open time and is **not** part of
slide source files). It forwards:

| Message             | Payload                                | Used by the shell for                          |
| ------------------- | -------------------------------------- | ---------------------------------------------- |
| `deckui:pointermove`| `{ x, y, sx, sy }` client + screen px  | presenter-pointer trail; pan-drag tracking     |
| `deckui:pointerdown`| `{ x, y, sx, sy, button }`             | pointer burst FX; arming a pan drag (button 0) |
| `deckui:pointerup`  | `{ x, y, sx, sy }`                     | ending a pan drag                              |
| `deckui:wheel`      | `{ x, y, sx, sy, dy, dm }` (`deltaY`, `deltaMode`; the event is `preventDefault`ed in the copy) | Explore zoom — the shell normalizes `dy` by `dm` before use |

The shell translates the copy-local client coordinates through the iframe's
bounding rect into window coordinates, so the pointer FX canvas (which lives
in the shell, above everything) lines up with what the presenter is touching.

## Versioning

The protocol is deliberately tiny and tolerant: unknown message types are
ignored by both sides, so additive changes are safe. If a change would break
either side's handling of the messages above, rename the marker comment
(`deck-bridge:v2`) and update shell and slides together.
