# Authoring reference

## Page skeleton

```html
<link rel="stylesheet" href="diag.css">
...
<div class="diag-wrap"><div class="diag" data-w="1100" data-h="480"> ... </div></div>
...
<script src="diag.js"></script>
```

`.diag-wrap` scales its `.diag` down to the container width (never up) and sets
its own height. After showing a hidden diagram (tab, accordion) or changing
container width programmatically, call `window.diagFitAll()`. Diagrams inside a
`<dialog>` are skipped by the fitter.

## Box diagram

`.diag` attributes: `data-w`, `data-h` (logical stage px, both required).

Elements, all absolutely positioned with inline `style="left:..;top:..;width:..;height:.."`:

| Element | Purpose |
|---|---|
| `.grp[data-label]` | Background group box with an upper-case caption. `.tight` pulls the caption closer to the corner. `--gc:#hex` in style recolours the caption. |
| `.node[data-node][data-kind][data-type]` | A card. `data-node` is the id edges refer to; `data-type` is the small caption on top (omit it and add `.notype` to drop the caption row). Content: `<h4>` title, `<p>` lines, `<code>`, `<b>`. |
| `.txt` | Free text (note beside a group). No id, not an edge endpoint. |
| `.lane` | Dashed vertical guide line (give left/top/height). |

Node shape modifiers: `.plain` (compact, no caption), `.pill` (rounded label),
`.state` (rounded flow state), `.center` (vertically centre the content),
`.notype`.

### Node kinds (`data-kind`, default `primary`)

| Kind | Use for |
|---|---|
| `primary` | the main actor or entry point |
| `compute` | services, workers, processing steps |
| `store` | databases, queues, caches, logs |
| `control` | gateways, schedulers, coordinators, policy |
| `external` | third-party or outside-the-boundary systems |
| `note` | caveats, decisions, gates |
| `ok` / `bad` | success / failure outcomes |
| `muted` | inert or background items |

An unknown kind is a verifier error.

## Edges

```html
<script type="application/json" class="edges">[
  {"from":"a","to":"b","label":"place order","kind":"primary"}
]</script>
```

| Key | Meaning |
|---|---|
| `from`, `to` | `data-node` ids (required unless `points` is given) |
| `fromSide`, `toSide` | `left`, `right`, `top`, `bottom`. Default: the facing sides, chosen by the larger centre-to-centre axis |
| `fromAt`, `toAt` | position along that side, 0..1, default 0.5. Use to align edges or fan several edges out of one side |
| `out` | px (default 40) a same-side loop leaves the boxes before turning. Both ends on the same side (`right`/`right`, `bottom`/`bottom`, ...) makes a U-shaped loop routed outside both boxes |
| `kind` | edge colour/style, below |
| `label` | HTML text in a pill at the midpoint of the path |
| `dx`, `dy` | shift the label from the midpoint (px) |
| `thick` | heavier stroke |
| `noarrow` | omit the arrowhead |
| `points` | explicit polyline `[[x,y],...]`, bypasses routing (verifier still checks crossings) |

Routing: opposite horizontal sides give a Z with the bend halfway; a
horizontal and a vertical side give an L; same-side ends give a U at distance
`out`. Edges are plain paths with no avoidance: you choose sides, `At`, and
`out` so that nothing is crossed.

### Edge kinds

| Kind | Look | Use for |
|---|---|---|
| `primary` | solid indigo | the main flow |
| `data` | dashed teal | data/event/side flows |
| `outcome` | solid green | results and success paths |
| `feedback` | dashed amber | loops back, retries, acknowledgements |
| `bulk` | thick grey | high-volume transfers |
| `danger` | solid red | errors, rejects |
| `tree` | thin grey | containment/hierarchy |

## Sequence diagram

```html
<div class="diag-wrap"><div class="diag seq" data-w="1100">
<script type="application/json" class="seqdef">{
 "participants":[{"id":"a","label":"Client","kind":"external"}, {"id":"b","label":"Server"}],
 "messages":[
  {"from":"a","to":"b","label":"request"},
  {"loop":"for each item","items":[ {"from":"b","to":"b","label":"validate"} ]},
  {"from":"b","to":"a","label":"response","reply":true},
  {"note":"Done","over":["b"]}
 ]}</script>
</div></div>
```

`data-w` is optional (default 1100); height is computed. Optional `rowH`
(default 40) in the definition sets the row pitch.

- Participants: `id`, `label`, optional `kind` (same node kinds). Columns are
  equal width; the lane gap is `(data-w - 48) / participants`.
- Message: `from`, `to`, `label`; `reply:true` draws a dashed return arrow;
  `danger:true` draws it red. `from == to` is a self-call (label to the right
  of the lifeline).
- `{"loop": "text", "items":[...]}` and `{"alt": "text", "items":[...]}` draw a
  dashed frame around the items; frames nest.
- `{"note":"text","over":["id",...],"tall":true}` draws a note box centred over
  the listed participants; `tall` reserves a taller row.
- Labels are one line and never wrap: keep them shorter than the lane gap.

## Single file

`scripts/inline_figures.py page.html -o out.html` replaces
`<!--FIG:name-->` with `figures/name.html` (a fragment holding one
`.diag-wrap`), then inlines diag.css and diag.js. Unknown figure names are
errors; unreferenced fragments are warnings.
