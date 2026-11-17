# HTML Box Authoring Playbook

## Contents

1. Choose the authoring route
2. Choose the interaction pattern
3. Synthesis-first, no-click gate
4. Generation contract
5. Interaction and accessibility
6. Page composition
7. Validation checklist

## 1. Choose the authoring route

| Route | Use when | Behavior |
|---|---|---|
| Collaborative source | A person wants to edit the HTML live in the host (code block, wiki editor, CMS field) | The host re-renders on edit; the host copy is the source of truth until you archive it |
| Deterministic automation | An agent must create, replace, verify, and archive | Source lives in a local file; publish through the host's API or file, read back, hash-compare, archive |

Do not mix lifecycle assumptions. A collaborative surface can change under
you; an automation surface needs exact bytes archived and compared. If the
host rewrites HTML on save (minifies, re-indents, strips attributes), hash
comparison will fail by design: compare a normalized form you define and
record that rule in the manifest notes.

## 2. Choose the interaction pattern

Use a box when interaction materially improves the page:

| Goal | Suitable patterns |
|---|---|
| Communication and storytelling | tabs, click-to-reveal cards, image switching, carousel |
| Knowledge extraction | summary, key-point cards, relationship map, guided Q&A |
| Participation | quiz, random draw, check-in, form-backed interaction |
| Creative visualization | player UI, particles, 2D/3D simulation |
| Host-aware application | page context, shared state, an AI or data action |

Prefer native paragraphs, tables, callouts, and images when they express the
content without custom interaction. For diagrams that must build step by step
across a whole deck, see `web-live-deck`.

## 3. Synthesis-first, no-click gate

For architecture, capability, and roadmap views, the initial viewport is the
primary synthesized view. A reader must reconstruct the main model without
clicking, hovering, switching tabs, scrolling an inner panel, or panning.
Interaction may reveal evidence, definitions, examples, and history. It must
not supply a missing core claim or relationship.

First paint shows: scope or thesis; major entities or groups; the
relationships between them; any legend, status, or time semantics. Never make
a reader infer an essential connection from proximity, color, or an unlabeled
hover target.

Review a static screenshot of the untouched viewport. It passes when a
reviewer can answer without interaction:

- what the main claim is;
- which major elements and boundaries are in scope;
- how elements connect, depend on, or follow one another;
- which status, phase, sequence, or legend distinctions change the meaning;
- architecture: boundaries, components, flows, dependencies;
- capability: grouping and coverage, status, or gap distinctions;
- roadmap: the time or ordering model, milestones, workstreams, dependencies.

Disabling pointer input, hover, and optional JavaScript detail panels must
leave the same core model visible. An active state may emphasize or explain
part of the model; it must not redefine the overview.

When the first viewport is too dense, degrade in this order:

1. remove decoration, motion, and repeated supporting prose;
2. simplify styling and group repeated secondary items under an accurately
   labeled category;
3. move evidence, examples, definitions, and history behind optional detail;
4. use a taller, explicitly verified box, or split into an overview plus
   supporting views, keeping the overview self-sufficient.

Do not fix density with tiny type, overlapping labels, ambiguous
abbreviations, internal scrollbars, panning canvases, or essential content in
tooltips, tabs, carousels, or click-to-reveal cards. If the core model still
does not fit, use several views or native prose and tables.

## 4. Generation contract

```text
Create one complete standalone HTML document.
Fix the viewport at <W>px x <H>px (default 817x560) with explicit px width and
height on html and body, overflow hidden.
Use inline HTML, CSS, JavaScript, and SVG only.
Do not depend on CDNs, web fonts, or icon libraries unless declared.
Do not use localStorage or sessionStorage.
Fit the viewport with no internal scrollbars or clipped text.
Include loading, empty, error, and host-unavailable states for async paths.
Preserve every required label, relationship, operation, and interaction.
Make the core model legible on first paint without click or hover.
```

A richer simulation may use a taller explicit height, but it still needs exact
browser verification at that height. Default to a self-contained box; add the
host bridge only when behavior needs page context, persistence, or a host
action (`host-capabilities.md`).

## 5. Interaction and accessibility

- Clickable cards and custom controls are `button`, or `role="button"` plus
  `tabindex="0"` with Enter and Space handlers.
- Keep touch targets usable; avoid duplicate touch/click activation.
- Images and SVG meaningful to the story carry `alt` or `aria-label`.
- Show visible active, loading, disabled, and error states.
- No autoplay or continuous motion unless it adds meaning; prefer stable
  highlight transitions over bounce, scale, or floating effects.
- Respect `prefers-reduced-motion`.
- Nothing important exclusively behind hover.

Asynchronous actions: disable duplicate submission; show progress; catch and
display a safe error; never print tokens or unnecessary personal data; require
an explicit gesture for writes or external side effects.

## 6. Page composition

Surround the box with native context: say what the interaction does before it,
add a summary after it, put limitations in a callout, and keep searchable
facts in native tables. This preserves meaning when scripts are blocked, the
page is exported, or the reader uses assistive tech.

## 7. Validation checklist

Before publishing:

- `validate-html-box-source.mjs` passes;
- exact-viewport preview passes with no page or text overflow;
- the untouched first-paint screenshot passes the gate in section 3;
- every click, keyboard, and async state is exercised;
- every source label and relationship is still present (`grep -F` each);
- the box works with the host bridge absent;
- writes are user-triggered and scoped to the intended resource;
- the stored copy was read back and its sha256 matches before any old copy
  is removed.
