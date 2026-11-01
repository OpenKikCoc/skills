---
name: web-live-deck
description: >-
  Build presentation decks as plain HTML instead of PowerPoint — each slide a
  standalone 1600x900 HTML file with live DOM/SVG diagrams and step-through
  builds, hosted by an iframe shell (speaker notes, present mode, all-slides
  grid, crisp re-layout zoom/pan, presenter-pointer trail, Explore overlay)
  and bundled by a small Python script into one self-contained HTML file with
  zero external requests. Use when the user wants to create, extend, restyle,
  or present a slide deck, turn an architecture walkthrough into slides, or
  convert a .pptx into something that presents well over screen share.
---

# Live Deck

A presentation system made of plain HTML files. Each slide is a complete,
standalone HTML document: a fixed 1600x900 stage carrying a live DOM/SVG
diagram, a step engine that reveals the diagram build-by-build, and a small
`deck-bridge:v1` script. A shell page hosts the slides in iframes and adds
deck chrome: paging with an arc-segmented progress rail, an all-slides grid,
speaker notes, full-screen present mode, zoom/pan that re-renders text crisp,
a presenter-pointer canvas so remote viewers can follow the cursor, and an
Explore overlay for stepping through a slide's build live. A Python bundler
inlines every slide into the shell to produce **one** self-contained HTML
file that presents from `file://` with no network.

## When to reach for this

- The user wants a technical/architecture deck that will be presented over
  video-call screen share — diagrams that build up step by step beat static
  pictures there.
- The user wants to extend or restyle an existing deck built with this
  system (recognizable by `deck-bridge:v1` in the slide files or
  `data-doc` iframes in a single-file build).
- The user wants to replace a `.pptx`: recreate each slide as an HTML slide;
  the speaker-notes field in the shell replaces PowerPoint notes.

Not for: print documents, multi-page reports, or content that must be edited
by non-technical people in a GUI.

## Quickstart

1. Create a deck folder and copy in this skill's `assets/`.
   Rename `deck-shell.html` to `index.html`.
2. For each slide: copy `slide-template.html` to a new file, build the
   diagram inside `#stage` (absolute positions, 1600x900 stage pixels),
   fill in `STEPS`. Use `example-slide.html` as the worked reference — it
   shows component cards, runtime-measured SVG connectors, edge labels,
   step highlighting, and the bridge in use.
3. Register each slide in `index.html`: one `SLIDES` entry **and** one
   `<iframe class="slide">` tag, same order (the bundler enforces the count,
   not the order).
4. Build: `python3 build-single-file.py` → `deck.html`, one shareable file.
5. Verify: `python3 verify.py` — checks the slide list, self-containment,
   the bridge, the bundle, and that a headless browser renders it. Python 3.8+,
   standard library only; it finds Chrome/Chromium/Edge itself on macOS, Linux
   and Windows, and skips only the render step if no browser exists.

Read `reference/conventions.md` **before authoring slides** — its rules
(12px type floor, iframe isolation, fixed stage, re-layout zoom, wheel
normalization, self-containment) all encode bugs that were found in
practice. `reference/bridge.md` specifies the shell↔slide protocol.

## Files

```
web-live-deck/
  SKILL.md                     # this file
  assets/
    deck-shell.html            # the deck shell; copy to index.html per deck
    build-single-file.py       # bundler: shell + slides -> one HTML file
    verify.py                  # self-check: build + render, stdlib only,
                               #   cross-platform browser discovery
    slide-template.html        # blank slide: stage + topbar + controlbar
                               #   + step engine + deck bridge
    example-slide.html         # worked slide: an order-pipeline diagram with
                               #   runtime-measured SVG connectors and 4 steps
  reference/
    authoring.md               # add/reorder slides, notes format, build,
                               #   preview, headless verification, presenting
    conventions.md             # the load-bearing rules AND why each exists
    bridge.md                  # deck-bridge:v1 postMessage protocol, precisely
```

## Invariants to preserve when editing the system itself

- One `<iframe class="slide">` in the shell per `SLIDES` entry, same order —
  this is the bundler's contract.
- Slides stay isolated documents; never merge slide markup into one page
  (see conventions §2 for the four ways that breaks).
- The slide-side control contract is the ids `next-step`, `prev-step`,
  `play-toggle`, `reset-view`, `progress` (text `"n / m"`) — the bridge and
  shell drive slides only through those.
- The bundle must stay dependency-free: no CDN, no webfonts, no requests.
