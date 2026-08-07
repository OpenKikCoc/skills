# Grounding answers in "what's on screen"

Every question automatically gets whatever the panel resolves as "the
current view" attached as context — this is what makes answers feel like
they understand the specific page you're looking at, not just the codebase
in the abstract.

## The default: the whole page

Out of the box, with no configuration, the panel uses:

```js
{ title: document.title, text: document.body.innerText }
```

truncated to 4000 characters. This is correct and sufficient for an
ordinary single-view page — a doc, a dashboard, a report. Nothing to wire up.

## Overriding it: `window.AI_COMPANION_CONTEXT`

Pages with a notion of "current section" — tabs, a multi-slide deck, a
wizard, a paginated viewer — should define this **before** the snippet's
script block runs (anywhere earlier in `<body>`, or in the snippet itself if
you're editing your own copy):

```js
window.AI_COMPANION_CONTEXT = function () {
  // Return null/undefined to fall back to the whole-page default for this
  // call. Otherwise return an object with any of these (all optional):
  return {
    title: "...",   // shown in the context chip and tagged on the turn
    text: "...",    // what the AI actually reads; keep it to what's visibly
                     // relevant -- the panel truncates at 4000 chars but
                     // sending the whole DOM as text is still noise, not signal
    index: 3,        // 1-based position, purely cosmetic ("item 3/15")
    count: 15
  };
};
```

Called on a 1s poll while the panel is open (cheap: just reads whatever the
host page's own navigation already updated) and again at send time. Errors
thrown inside it are caught and treated as "use the default."

## Worked example: a live-deck deck

live-deck slides are separate documents in `<iframe class="slide">`
elements; the currently-shown one carries `.is-current`
(`reference/conventions.md` / `reference/bridge.md` in the `live-deck`
skill). The panel doesn't reach into the deck's private state — it reads
this one public, stable DOM signal instead:

```js
window.AI_COMPANION_CONTEXT = function () {
  var frame = document.querySelector('iframe.slide.is-current');
  if (!frame) return null;
  var doc;
  try { doc = frame.contentDocument; } catch (e) { /* not expected: slides load via srcdoc, same-origin */ }
  var all = document.querySelectorAll('iframe.slide');
  var index = null;
  for (var i = 0; i < all.length; i++) { if (all[i] === frame) { index = i + 1; break; } }
  return {
    title: frame.getAttribute('title') || '',
    text: doc && doc.body ? doc.body.innerText : '',
    index: index,
    count: all.length
  };
};
```

## Why a hook instead of baking in DOM assumptions

The bridge server and the panel's chat/settings machinery don't know or care
what "current" means for your page — that's the one piece of real
page-specific knowledge in an otherwise generic widget, so it's the one
piece pulled out into an integration point instead of guessed at. Keep your
provider **read-only and cheap**: it runs on every tick the panel is open,
so it should query existing DOM state, not compute anything expensive.
