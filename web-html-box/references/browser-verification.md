# Browser Verification

## Contents

1. Why the DevTools Protocol
2. Script usage
3. What to assert
4. Overflow rules
5. Troubleshooting

## 1. Why the DevTools Protocol

`--window-size=817,560` sets the outer window and can yield a shorter content
viewport. Override device metrics over CDP instead:

```json
{ "method": "Emulation.setDeviceMetricsOverride",
  "params": { "width": 817, "height": 560, "deviceScaleFactor": 1, "mobile": false } }
```

Then require:

```text
documentElement.clientWidth  == 817    documentElement.scrollWidth  == 817
documentElement.clientHeight == 560    documentElement.scrollHeight == 560
```

Apply metrics before `Page.navigate`.

## 2. Script usage

```bash
node scripts/preview-html-box.mjs --html box.html --screenshot box.png --report box.json
node scripts/preview-html-box.mjs --html box.html --width 1024 --height 640
node scripts/preview-html-box.mjs --html box.html --click '[data-stage="2"]' \
  --expect-active '.stage.active=2' --expect-active '.item.related=3'
node scripts/preview-html-box.mjs --html box.html --text-selector '.title,.detail'
```

Browser lookup order: `--chrome`, `$HTML_BOX_BROWSER`, `$CHROME_BIN`, PATH
(`google-chrome`, `chromium`, `chromium-browser`, `chrome-headless-shell`),
common macOS paths, then `~/.cache/ms-playwright/chromium_headless_shell-*`
and `chromium-*`. A temporary profile is used and removed; never reuse a
personal browser profile.

Host-bridge boxes: inject a task-local stub for verification only.

```bash
cat > stub.js <<'JS'
window.htmlBoxHost = { async getContext() { return { title: "Preview Page" }; } };
JS
node scripts/preview-html-box.mjs --html box.html --init-script stub.js --click '#load'
```

Run once with the stub (success path) and once without (degraded path). Never
publish the stub or its fixture data inside the box.

Report fields: `viewport`, `bounds`, `textOverflow`, `longTokens` (warnings),
`expectedCounts`, `htmlSha256`, `screenshot`. The script exits non-zero if the
viewport scrolls, a major region leaves the viewport, text overflows, an
expected count differs, or the body is blank.

## 3. What to assert

Always: exact viewport and no page scroll; major regions inside the viewport;
text-bearing elements free of horizontal and vertical overflow; the click
selector exists; the clicked state changes the expected classes and counts;
the screenshot is non-blank and exactly viewport-sized.

When counts are not enough, add a task-local assertion: exact active labels,
reset on second click, detail text update, dimmed or related counts.

## 4. Overflow rules

Check text nodes, not every ancestor:

```js
node.scrollWidth > node.clientWidth || node.scrollHeight > node.clientHeight
```

Decorative pseudo-elements may extend past a card (`::after { right: -13px }`)
and inflate the parent's `scrollWidth` even when all text fits. Inspect the
real text children, and validate the parent's bounding rectangle against the
viewport instead. Text must not clip, overlap another region, force page
scroll, or expand a fixed board or card.

`longTokens` lists unbroken words of 24+ characters inside elements that lack
`overflow-wrap: anywhere|break-word`. They are warnings: a long identifier or
URL passes today and breaks on the next edit.

## 5. Troubleshooting

- **Browser exits immediately (for example code 134)**: a sandbox blocks
  browser initialization. Run outside the sandbox, with a temp profile.
- **`ECONNREFUSED` on the debug port**: the browser is not ready or exited.
  Poll `/json/version` before connecting (the script does).
- **Blank screenshot**: wait for `Page.loadEventFired` plus one frame; confirm
  `file://` access works and the page has no external dependency.
- **Wrong viewport height**: do not trust `--window-size`; confirm metrics
  were applied before navigation.
- **No browser found**: install any Chromium (for example
  `npx playwright install chromium`) or set `HTML_BOX_BROWSER`.
- **Long-token overflow**: add `overflow-wrap: anywhere; min-width: 0`.
  Reduce padding before font size. No viewport-width font scaling.
- **Grid or flex child overflows**: children need `min-width: 0`.
- **Screenshot looks gray**: restore clean component hues; fade only
  secondary elements. **Too saturated**: reduce glow, shadow opacity, and
  active-state contrast before desaturating everything.
