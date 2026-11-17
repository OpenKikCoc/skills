---
name: web-html-box
description: "Author, verify, and archive a self-contained interactive HTML block (an HTML box) for embedding in a document, wiki page, slide, or web page at a fixed viewport (default 817x560, configurable). Covers authoring routes, interaction patterns, a synthesis-first no-click gate, a source validator, exact-viewport headless-Chromium verification over the DevTools Protocol, and hash-verified archiving with publish, read back, compare, then delete. Use when the user wants an interactive diagram, explainer, or widget inside a host that accepts HTML, or needs a box redrawn, replaced, or checked for overflow."
---

# HTML Box

An HTML box is one standalone HTML file, rendered by a host (document editor,
wiki, slide, web page) inside a fixed-size frame. This skill is the method for
making one that looks right at the real size, keeps working when the host is
absent, and can be replaced or restored without losing the source.

```text
source file -> validate -> exact-viewport preview -> publish to embed target
            -> read back -> sha256 compare -> (only then) delete old copy -> archive
```

## When to use

- A diagram, explainer, tabbed view, quiz, or small simulation should live
  inside a document or page and the host accepts HTML (iframe, `srcdoc`, an
  embed block, a slide element).
- An existing box overflows, clips text, or needs a faithful redraw.
- A box must be replaced or migrated and the exact source must be preserved.

Prefer native prose, tables, and images when they carry the content without
custom interaction. A box augments the page; it does not replace it.

## Workflow

1. **Pick the route** (`references/authoring-playbook.md` section 1):
   collaborative source (a person edits the HTML live in the host) or
   deterministic automation (an agent publishes, reads back, and archives).
2. **Start from a template**: `assets/html-box-817x560.html` (plain) or
   `assets/html-box-host-817x560.html` (optional host bridge). Change the
   fixed `width`/`height` px in CSS if the viewport is not 817x560.
3. **Inventory before redrawing**: list every node title, edge label, step
   number, legend entry, metric, and interaction. Check each with
   `grep -F` after the edit. Never trade information for compactness.
4. **Apply the synthesis-first gate**: the untouched first paint must show the
   thesis, entities, relationships, and legend. Interaction only adds
   evidence, definitions, or examples.
5. **Validate source**:
   `node scripts/validate-html-box-source.mjs --html box.html [--width 817] [--height 560]`
6. **Verify at the exact viewport**:
   `node scripts/preview-html-box.mjs --html box.html --screenshot box.png --report box.json`
   Review the PNG of the untouched first paint, then exercise interactions with
   `--click` and `--expect-active`.
7. **Publish and archive** (`references/publish-and-archive.md`):
   `node scripts/archive-html-box.mjs archive --html box.html --out archive/<id> --preview box.png`,
   publish to the embed target, read back, then
   `archive-html-box.mjs verify --dir archive/<id> --readback readback.html`.
   Delete the old copy only after verify passes.

## Generation contract

Put this in any code-generation prompt:

- One complete standalone HTML document with `<!doctype html>`, `<title>`,
  and a viewport meta.
- Fixed pixel `width` and `height` on `html` and `body`; `overflow: hidden`;
  no internal scrollbars, clipped text, or layout overflow.
- Inline HTML, CSS, JavaScript, and SVG. No CDNs, web fonts, or icon libraries
  unless the user declares the network dependency, and then add a visible
  failure state (`--allow-network` in the validator).
- No `localStorage`/`sessionStorage` (sandboxed frames may throw); keep state
  in memory or use a host capability you have feature-detected.
- No credentials, tokens, or personal identifiers in the source.
- Loading, empty, error, and host-unavailable states for every async path.
- Core model legible on first paint without click or hover.
- `overflow-wrap: anywhere; min-width: 0` on text in grid/flex children.

## Interaction patterns

| Goal | Patterns |
|---|---|
| Storytelling | tabs, step-through, click-to-reveal detail, image switch |
| Knowledge | summary cards, relationship map, guided Q&A |
| Participation | quiz, draw, checklist, form-backed widget |
| Visualization | chart, timeline, 2D simulation |
| Host-aware | reads page context, persists state, calls a host action |

Keyboard-operable controls (`button`, or `role="button"` plus `tabindex` and
Enter/Space), visible active/disabled/error states, subtle highlight
transitions, no autoplay, nothing essential behind hover. Side effects need an
explicit user gesture and a visible result.

## Guardrails

- Do not rely on the host bridge; feature-detect and degrade (`references/host-capabilities.md`).
- Do not publish preview mocks, fixture data, or init scripts inside the box.
- Do not use `--window-size` for verification; it is the outer window. Use CDP
  device metrics (`references/browser-verification.md`).
- Do not delete the previous copy before read back and hash compare pass.
- Do not edit a published record in place if the host rejects it; append a
  replacement, verify, then remove the old one.
- Hash exact bytes: no trailing newline added by shell tools.

## Resources

- `scripts/validate-html-box-source.mjs`: static source checks (node >= 18).
- `scripts/preview-html-box.mjs`: headless Chromium over CDP, exact viewport,
  overflow assertions, screenshot, JSON report.
- `scripts/archive-html-box.mjs`: extract HTML from a stored record, write an
  archive with a sha256 manifest, verify a read-back copy.
- `scripts/test-html-box.mjs`: `node scripts/test-html-box.mjs`.
- `assets/`: plain and host-bridge templates.
- `references/`: authoring playbook, browser verification, publish and
  archive, host capabilities.
- Related: `web-live-deck` for multi-slide HTML decks.
