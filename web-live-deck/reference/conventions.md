# Conventions — the rules and why they exist

Each rule below encodes a problem found in practice. The reasons matter more than the rules: they are
what stops a well-meaning refactor from "simplifying" the system back into the
bugs it was built to avoid.

## 1. 12px minimum type size

No text on a slide may be smaller than 12px at the 1600x900 stage size.

**Why:** these decks are presented over video-call screen share, where video
compression and viewer-side scaling destroy small text. Dense diagrams
with 8–9px labels are unreadable on a screen share. If a diagram only fits with sub-12px
text, the diagram has too much on it — split it across build steps or slides
instead of shrinking the type.

Working scale (from the example slides):

| Role                          | Size      | Notes                              |
| ----------------------------- | --------- | ---------------------------------- |
| Slide title (`h1`)            | 23–24px   | `letter-spacing: -.02em`           |
| Big statement / lane question | 25–28px   | weight 700–750                     |
| Card heading (`.component h2`)| 16–18px   | accent-colored                     |
| Body / step detail            | 13–15px   | `line-height` ≥ 1.35               |
| Eyebrow / kind labels / legend| 12–13px   | weight 700–800, `letter-spacing: .10–.13em`, uppercase |
| Absolute floor                | **12px**  | nothing below this, ever           |

## 2. Slides are isolated in iframes — never merged into one document

Each slide is a complete standalone HTML document. The shell hosts them in
iframes; the bundler inlines each *whole document* into an iframe `data-doc`
attribute. Nothing ever concatenates slide markup into a single page.

**Why merging silently breaks slides:**

- Every slide declares `const STEPS` and `let currentStep` at global scope.
  A second declaration in the same document throws `SyntaxError` and that
  entire script never runs — the slide renders but is dead.
- Slides deliberately reuse the same ~12 element ids (`stage`, `stage-shell`,
  `viewport`, `edges-layer`, `prev-step`, `next-step`, `play-toggle`,
  `reset-view`, `step-title`, `step-detail`, `progress`, plus per-slide node
  ids). `getElementById` in a merged document finds the *first* one, so every
  later slide's controls drive the first slide's DOM.
- Slides define SVG `<marker>` ids like `arrow-primary`. Marker references
  resolve document-wide, so a merged page paints every slide's arrowheads
  with the first slide's colors.
- Slides ship conflicting `:root` custom properties (same names, different
  values per slide). In one document, last stylesheet wins and recolors
  every other slide.

This id/name reuse is a *feature*: it keeps every slide copy-pasteable from
the same template and keeps the deck bridge trivial. Iframe isolation is the
load-bearing wall that makes it safe. Do not "optimize" it away.

## 3. Fixed 1600x900 stage, scaled by `fitStage()`

Slide content lives on an absolutely-positioned 1600x900 stage
(`#stage` inside `#stage-shell` inside `#viewport`). Everything on the stage
uses absolute positions in stage pixels. `fitStage()` computes
`scale = min(vw/1600, vh/900)`, applies a `transform: scale()` to the stage,
and sizes the shell to the scaled box.

**Why:** absolute layout at a fixed logical size is the only way dense
diagrams stay pixel-stable across window sizes, projectors, and the shell's
zoom. Responsive reflow would re-wrap labels and detach them from the SVG
connectors drawn against measured positions.

**The minimum-logical-width guard:** slide chrome (topbar title, subtitle,
controlbar) uses `white-space: nowrap` and clips when the document is laid
out below roughly 1440px. The shell therefore never lays a slide out narrower
than `MIN_LOGICAL` (1520px): when the shell's zoom layer would be smaller, it
lays out at the floor width and scales *down* with a transform instead. Keep
that guard when touching the shell, and keep new slide chrome inside the same
width budget.

## 4. Zoom re-lays-out; it does not CSS-scale

The shell zooms by **resizing the layer** that holds the slide iframe. The
resize fires the slide's own `resize` handler, the slide re-runs `fitStage()`,
and text re-renders crisp at the new size. A cheap `transform: scale()` is
used only *during* the gesture (wheel/drag animation), then a settle timer
(~130ms) commits the real layout.

**Why:** CSS-scaling an already-rendered slide just magnifies pixels — at
250% zoom the text a presenter zoomed in to read is a blur, which defeats
the whole point of zooming. Re-layout is a reflow, so it is deliberately
deferred to gesture-settle; doing it per wheel tick janks.

## 5. Normalize wheel deltas by `deltaMode`

Never feed `event.deltaY` straight into zoom math. Convert to pixels first:

```js
function wheelPixels(deltaY, deltaMode) {
  var d = deltaY;
  if (deltaMode === 1) d *= 16;    // lines -> px
  else if (deltaMode === 2) d *= 400; // pages -> px
  return clamp(d, -260, 260);
}
```

then apply an exponential step (`z * Math.exp(-px * 0.0014)`).

**Why:** one mouse-wheel notch reports ~100px (or 3 "lines"); a trackpad
streams dozens of tiny pixel deltas per second. A fixed per-event step makes
the mouse leap whole zoom levels while the trackpad rockets past the target.
The clamp tames free-spinning wheels.

## 6. Diagrams are live DOM/SVG, never images

Boxes are positioned HTML elements; connectors are SVG paths **measured at
runtime** from the boxes they join (see `drawEdges()` in
`assets/example-slide.html`: an `EDGES` list references `data-node` ids,
`box()` reads `offsetLeft/offsetTop/offsetWidth/offsetHeight`, and orthogonal
paths + arrowheads + label pills are generated from those boxes).

**Why:**
- Moving a box automatically re-routes its arrows — no image editor round-trip.
- Build steps can highlight/mute individual nodes and edges with classes.
- Text stays real text: crisp at any zoom, searchable, screen-readable.
- Diffs are reviewable.

Redraw edges from `fitStage()` and once more on the next animation frame
(fonts and layout may settle late).

## 7. Everything self-contained

No CDN scripts, no webfonts, no analytics, no external requests of any kind.
Fonts are system stacks (`"Segoe UI", Arial, sans-serif`; monospace
`"Cascadia Code", "Cascadia Mono", Consolas, monospace`). Icons are inline
SVG. The single-file bundle must present correctly from `file://` on a
machine with no network — that is its job.

## 8. Palette

Slides are light (white or `#f7f9fc` page, 24px background grid at
`rgba(148,163,184,.12)`); the shell surround is dark by default with a
light-theme variant. House colors:

| Token         | Value               | Use                                |
| ------------- | ------------------- | ---------------------------------- |
| ink           | `#0f172a` / `#152238` | primary text                     |
| muted         | `#64748b` / `#5b6f88` | secondary text, labels           |
| line          | `#d7e0ea` / `#c9d6e5` | hairline borders                 |
| blue          | `#1677c8`           | first narrative path / group A     |
| indigo        | `#5b5bd6`           | second path, primary edges         |
| green         | `#249c62`           | outcomes, convergence, success     |
| amber         | `#d97706`           | queues, caution                    |
| violet        | `#7c3aed` / `#7b52b9` | stores, durable records          |
| teal          | `#0f9d8a`           | data reads / joins (dashed edges)  |
| slate         | `#94a3b8`           | framing / neutral arc              |

Accent tints for card fills/borders are derived with
`color-mix(in srgb, var(--accent) 6% (fill) / 38% (border), ...)` so all
kinds stay visually related. Edge kinds: primary = solid indigo, data =
dashed teal, outcome = solid green; active edges thicken (2.6 -> 4.5) while
muted ones drop to ~.09 opacity.

## 9. Smaller rules that still matter

- **Step 0 is the resting overview**: nothing muted, nothing highlighted.
  Steps highlight (`is-active`) their nodes/edges and mute (`is-muted`)
  everything else only while some step has focus.
- **Autoplay must stop on navigation**: the shell sends `deck:pause` to every
  non-current slide; the slide-side handler clicks Play only if its label
  currently reads Pause.
- **`prefers-reduced-motion`** collapses all transitions/animations; the
  shell's pointer FX degrades to a still marker.
- Keep `aria-label`s on the stage, buttons, and overlays; step copy uses
  `aria-live="polite"`.
- Edge labels are DOM pills positioned by the edge code, not SVG `<text>` —
  they get real font rendering, padding, and borders for free.
