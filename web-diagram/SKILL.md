---
name: web-diagram
description: "Author architecture, flow, and sequence diagrams as hand-placed HTML cards with runtime-measured orthogonal SVG edges, driven by a small JSON edge list and a dependency-free JS/CSS engine. Includes a verifier that checks node references, overlaps, edges crossing unrelated boxes, label collisions, and takes a headless screenshot, plus a script that inlines figure fragments into a single-file page. Use when a design doc, HTML page, web-live-deck slide, or web-html-box block needs an editable, diffable, crisp-at-any-zoom diagram. Not for data charts or freeform illustration."
---

# Web Diagram

Diagrams as plain HTML: every box is an absolutely positioned `<div>` on a
fixed logical stage, every connector is a JSON entry routed orthogonally and
drawn as SVG after the browser has measured the boxes. The stage is scaled to
its container, so text stays real text (searchable, editable, crisp at any
zoom) and the whole diagram diffs line by line in git.

## When to use

- Architecture, component, data-flow, state, decision-flow, or sequence
  diagrams inside an HTML design doc or report.
- A diagram block for a `web-live-deck` slide or a `web-html-box` section.
- You want to fix a label with a one-line edit instead of re-exporting an image.

Not for: data charts (use a charting approach), photos or freeform
illustration, graphs with dozens of auto-laid-out nodes (this engine does no
auto layout; you place every box).

## Files

```
assets/diag.js            engine (no dependencies); exposes window.diagFitAll()
assets/diag.css           stage, node kinds, edge kinds, sequence styling
assets/example.html       three working diagrams: architecture, flow, sequence
scripts/verify_diagram.py static + geometric checks, optional headless PNG
scripts/inline_figures.py <!--FIG:name--> fragments + css + js -> one HTML file
references/authoring.md   every attribute and JSON key
references/layout-rules.md placement and routing lessons
```

## Workflow

1. **Sketch on a grid.** Decide groups, columns, and the one main flow
   (left to right or top to bottom) on paper or in a comment. Pick the stage
   width once for the whole document (1100 is the default in the example).
2. **Place nodes** as `.node[data-node][data-kind]` with inline
   `left/top/width/height` in px, on a 10 or 20px grid. Add `.grp` boxes
   behind related nodes. Leave 60px or more between connected nodes if the
   edge carries a label.
3. **Write edges** in the `script.edges` JSON list: `from`, `to`, optional
   `fromSide/toSide`, `fromAt/toAt`, `out`, `kind`, `label`.
4. **Verify**: `python3 -I scripts/verify_diagram.py page.html`. Fix every
   `[FAIL]`; read every `[warn]`.
5. **Look at the render.** The verifier writes a PNG when a Chromium exists;
   open it. Measured layout can differ from the source (text wrapping changes
   card heights, labels land where the midpoint is, not where you pictured).
6. For a single shareable file with figures kept in separate fragments:
   `python3 scripts/inline_figures.py page.html -o out.html`.

Minimal page: `<link rel="stylesheet" href="diag.css">` in the head,
`<script src="diag.js"></script>` before `</body>`, then one
`<div class="diag-wrap"><div class="diag" data-w="1100" data-h="400">...</div></div>`
per diagram. Copy from `assets/example.html`.

## Rules

- One stage width per document. Different `data-w` values scale text by
  different factors and the diagrams stop looking like one set.
- Every `data-w`/`data-h` is a number; every edge endpoint is an existing
  `data-node`; ids are unique per diagram.
- Stay inside the stage; cards never overlap. Groups and lanes are
  backgrounds and are exempt.
- Keep text readable after scaling: 12px floor, 12.5px for body text. Do not
  shrink fonts to make a card fit; enlarge the card or cut the words.
- An edge must not pass through a box that is not its endpoint. Route long
  loops around the outside with `out`; if no clean route exists, drop the edge
  and state the relationship in prose.
- Use semantic `data-kind` values (`primary compute store control external
  note ok bad muted`) and keep feedback/error edges in their own kinds.
- Never edit measured output; edit the source attributes and re-render.
- The verifier is a model of the engine, not a browser: a clean run is
  necessary, not sufficient. Always look at the render.

## Verifier output

`[ok]` a check passed (per diagram, plus the render path), `[FAIL]` an error
(exit 1), `[warn]` a probable problem (label overlap, long sequence label),
`[skip]` render skipped (no Chromium, or `--no-render`). Set
`WEB_DIAGRAM_BROWSER` to point at a specific Chromium binary.

Tests: `python3 -I scripts/test_verify_diagram.py` and
`python3 -I scripts/test_inline_figures.py`.

## Invariants when editing the engine

- `route`, `anchor`, `defaultSides`, `midpoint` in `diag.js` are ported to
  `verify_diagram.py`; change both together and add a test.
- Keep `diag.js` and `diag.css` free of external requests and fonts.
