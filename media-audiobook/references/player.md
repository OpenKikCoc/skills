# Read-along Player

A static site: shelf -> table of contents -> chapter page that highlights the
current sentence from the timing file and seeks when a sentence is tapped.
The hard part is mobile Safari.

## Rules

1. **Lock-screen auto-advance.** If the player fetches the next chapter's
   text over the network *before* calling `play()`, iOS suspends the page as
   soon as audio stops, and the later `play()` counts as an autoplay without
   a user gesture and is blocked. Prefetch the next chapter; inside the
   `ended` handler swap `src` and call `play()` **synchronously**, render
   afterwards. Test it: instrument `play()` and assert it runs in the same
   task as `ended` (the old code must fail this test).
2. **Content-addressed audio URLs.** After a chapter is resynthesized, a
   stable URL with a long cache lifetime lets the phone play the old, shorter
   audio against the new timing. Taps past the old length jump to the start
   or to the next chapter. Put a content fingerprint in the audio URL, cache
   it long, check the loaded duration against the expected one and reload
   bypassing the cache on mismatch, and clamp seek targets to the duration.
3. **Stale single-page JS.** Safari restores tabs from memory and keeps
   running old JavaScript, including old audio URLs. Publish a build number;
   when the page returns to the foreground or is paused and the build has
   changed, reload. Never reload while playing or at a chapter boundary.
4. **Real-device evidence before "fixed".** Desktop Chromium passing does not
   mean the phone passes. Add a tiny diagnostic beacon (an image or fetch to
   a logged path carrying the event, positions, and the front-end build), ask
   the user to reproduce once, and read the server log. Confirm the build
   number in the log before trusting a report. Device logs can show that
   the suspected cause (for example a duration mismatch) is not happening at
   all and the real cause is elsewhere (often rule 3).
5. **Route every seek through one function** that validates, clamps, and
   retries; keep protection against spurious `ended` events as a defense.

## Browser Tests

- Full journey on desktop and on phone emulation: shelf -> book -> chapter ->
  play -> sentence highlight -> tap to seek -> auto-advance at chapter end.
- Seek test on **every** chapter: first, second-to-last, and last sentence
  land where tapped without a chapter jump.
- End test: tap the last sentence of several chapters and let it play out; it
  must advance, never loop.
- Stale-cache test: serve a truncated old copy of one chapter's audio; the
  player must detect and recover.
- Build test: rebuild the front end while a page is open; no reload during
  playback, one reload once paused.
- Read titles and chapter counts from the published book metadata; never
  hard-code the first book. Tests that need several chapters run after
  synthesis.
- Pin the browser-test dependency in the pipeline's own lockfile and install
  it on demand; do not borrow another project's install.
- Real-phone checks remain a human step: lock-screen continuous play and
  tapping the last lines of a chapter.
