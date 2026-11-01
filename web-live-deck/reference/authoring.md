# Authoring — adding, reordering, and building slides

## Deck folder layout

A deck is a flat folder:

```
my-deck/
  index.html            # the shell (start from assets/deck-shell.html)
  slide-template.html   # keep around as the copy source for new slides
  example-slide.html    # delete once you have real slides
  build-single-file.py  # the bundler
  <your-slides>.html    # one file per slide
  deck.html             # generated single-file build (never edit by hand)
```

Start a new deck by copying the four files from `assets/` and renaming
`deck-shell.html` to `index.html` (the bundler accepts either name, looking
for `index.html` first).

## Adding a slide

1. **Copy `slide-template.html`** to a new file, e.g. `slide-checkout.html`.
   Never start from scratch — the template carries the stage scaffold, the
   step engine, and the bridge script, all of which the shell depends on.
2. **Author the slide**: set `<title>`, the topbar eyebrow/h1/subtitle, build
   the diagram inside `#stage` (absolute positions, stage pixels, 1600x900),
   and fill in `STEPS`. For diagrams with connectors, lift the
   `EDGES`/`box()`/`drawEdges()` pattern from `example-slide.html`.
   Follow `conventions.md` — especially the 12px type floor.
3. **Register it in the shell**, in two places that must stay in the same
   order:
   - add an entry to the `SLIDES` array (`file`, `arc`, `eyebrow`, `title`,
     `notes`);
   - add a matching `<iframe class="slide" title="...">` tag inside
     `#stage-zoom`.
   One iframe per `SLIDES` entry, same order — `build-single-file.py` errors
   out if the counts differ, but it cannot detect two lists in *different*
   orders, which would pair every slide with the wrong metadata.
4. If the slide starts a new narrative section, give it a new `arc` and add
   the arc to `ARC_COLOR` plus a `--arc-*` color in the shell's `:root`.
   Arcs drive the rail segmentation, the overview card accents, and the
   notes overlay accent.
5. Rebuild (below) and check the slide both standalone and in the deck.

## Speaker notes

Notes live in the shell's `SLIDES` entries, not in the slide files, so the
slide documents stay presentation-clean. Plain text with `\n` breaks. The
house format that works well when presenting:

```
Target time: 2 min

Opening: <the first sentence you will actually say>

Teaching points:
- <one claim per line>

Transition: <the bridge to the next slide>
```

## Reordering / removing slides

Order lives **only** in the shell's `SLIDES` array — file names carry no
ordering. To reorder: move the `SLIDES` entry *and* its `<iframe>` tag so the
two lists stay aligned. To remove: delete both, and delete the file.

## Building the single-file deck

```
python3 build-single-file.py            # writes deck.html
python3 build-single-file.py out.html   # or name the output
```

The bundler inlines each slide's whole document into its iframe's `data-doc`
attribute. The result is one file with zero external requests — safe to mail,
upload to a wiki, or open from Downloads. Re-run it after every edit; never
edit `deck.html` itself.

## Previewing

- **Single slide**: open the slide file directly in a browser. It is fully
  interactive standalone (the deck bridge is inert).
- **Whole deck during authoring**: `python3 -m http.server` in the deck
  folder, then open `http://localhost:8000/`. The shell loads the slide files
  by URL, so plain reloads pick up edits without rebuilding.
- **Whole deck for real**: build, then open `deck.html`.

## Verifying a build

Run the self-check from the deck folder:

```
python3 verify.py
```

It checks the shell, the slide list, that every slide is self-contained and
carries the bridge, that the bundle contains every slide, and that a headless
browser renders it to a non-blank page. Python 3.8+, standard library only --
nothing to install.

It finds a browser in this order: `$LIVE_DECK_BROWSER`, `$CHROME`, `$BROWSER`,
your `PATH`, then the usual Chrome / Chromium / Edge install locations on
macOS, Linux and Windows. If no browser exists the render step is skipped and
everything else still runs, so the check works on a build machine too. Point it
at a specific browser with:

```
LIVE_DECK_BROWSER="/path/to/chrome" python3 verify.py
```

To eyeball a particular slide, screenshot it yourself -- `$BROWSER` below is
any Chromium-family binary:

```
"$BROWSER" --headless=new --disable-gpu --hide-scrollbars \
  --virtual-time-budget=7000 --window-size=1600,950 --screenshot=out.png \
  "file:///absolute/path/to/deck.html#3"
```

The `#3` picks slide 3 (the shell reads the hash). Check that the slide renders
inside the shell chrome (rail at the bottom, Explore button top-right), that
connectors meet their boxes, and that no topbar/controlbar text clips.
Screenshot at least the first slide and the densest slide after any layout
change.

## Presenting

`F` for full-screen present mode, `N` for speaker notes, `O` for the all-
slides grid, `L` toggles the presenter pointer, wheel or `+`/`-` zooms the
slide (crisp — it re-lays-out), drag pans while zoomed, and **Explore Slide**
(top right) opens the interactive copy where arrow keys step through the
current slide's build. `?` shows the full key map.
