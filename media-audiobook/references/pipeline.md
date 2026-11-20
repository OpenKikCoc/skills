# Pipeline Details

A reference stack that worked: an IndexTTS-2-class zero-shot TTS for
synthesis (emotion vector + duration control), Qwen3-TTS VoiceDesign for
generating reference voices from text prompts, an open wav2vec2 age/gender
classifier and the TTS model's speaker encoder for voice QA, and two
different LLM providers for text understanding. Any equivalent stack works
if it exposes the same controls.

## 1. Text

- Inputs may be TXT (often scraped, with odd encodings), EPUB, or MOBI. For
  ebooks read the spine in order, one line per paragraph element; convert
  MOBI to EPUB first.
- **Inspect the chapter structure before configuring.** Converted ebooks may
  carry two sets of "Chapter N" (empty per-file headings plus real ones), a
  table of contents that does not match, or headings without numbers. Use a
  numbered-heading regex where possible, a drop regex for fake headings, and
  an explicit ordered heading list otherwise (fail if any is missing).
  Short isolated lines are only candidate headings: poems and one-line
  paragraphs must be removed by hand.
- Strip serial-site debris: author's notes (up to the next chapter heading),
  image placeholders, scene-break lines, stray markers, watermarks, trailing
  ads. Confirm with a count of 0 in the cleaned text.
- Scraped text: quotes truncated by line breaks (re-join paragraphs whose
  quotes do not balance), pagination debris ("...", duplicated characters at
  page joins), opening quote marks used as closing ones.
- **HTML entities can be split across pages** and reassemble after joining;
  clean entities again after joining and make any surviving entity a FAIL in
  verify (it will be read aloud).
- Quote state machine: nested quotes stay inside the quote (normalize to a
  secondary quote style); a speech spanning several paragraphs opens once and
  closes once, so the paragraphs between are quotes too. Serial "recap at the
  start of the next part" paragraphs take the same state as their first
  occurrence; keeping them verbatim is simpler than special-casing them.
- Segments that contain only punctuation produce noise or random syllables in
  TTS; emit a short silence instead (do not renumber segments).
- Segment IDs are the join key for annotations. Any segmentation change
  invalidates annotations, so regression-test it: an earlier book's
  segmentation must stay byte-identical.

## 2-3. Cast and Speakers

- Two models each read the whole book and produce a cast table. Use the
  cleaner one (walk-ons grouped into buckets) as the primary; keep the other's
  exhaustive list as a fallback for unknown speakers.
- Before correcting a character's gender or age, read the source. A character
  addressed by a male-sounding kinship nickname can still be a woman;
  "fixing" gender from a nickname is wrong.
- Speaker attribution: both models label every quote independently with the
  chapter as context. Disagreements go to the stronger model with the whole
  chapter. Unknowns get one more pass with the full name list plus generic
  "other male" / "other female" options (mapped to default voices, which
  sound better than the narrator reading dialogue). Onomatopoeia and lyrics
  go to the narrator.
- Re-running the merge step must not wipe delivery annotations stored in the
  same file: allow per-chapter merges and keep delivery for quotes whose id
  and speaker did not change.

## Headless LLM Workers

- CLI agents used as batch workers may try to run tools and get refused, or
  silently drop long stdin. Disable tools in the prompt and send long inputs
  through a streaming JSON input mode.
- Structured output may arrive wrapped (`{"text": "<json>"}`) or as several
  concatenated objects; parse every response through one tolerant parser.
- Run model calls concurrently (about 4 per provider) and make every step
  resumable at chapter granularity.

## 6. Synthesis

- Sentence-level cache; the chapter join and its timing file come from the
  same pass, then verify the timing end against the audio duration.
- Title pause and paragraph pauses are part of the join; check them after
  any change to the join code.
- Watch for runaway generation on short inputs (the model hits its token cap
  and produces long noise); flag sentences whose duration is far above what
  their length implies.
- Filesystems limit names in **bytes**: long chapter titles in CJK overflow
  255 bytes. Truncate titles in file names (keep the full title in metadata
  and the site).
- Exceptions inside a thread pool surface only when `result()` is called;
  print a loud failure line as soon as a publish task fails.

## 7. Verify Command

Collect into one command, per book: text checks (leftover quotes,
entities, debris), cast/speaker coverage, delivery coverage, voice approval
and QA numbers with any recorded waiver, synthesis completeness (every
chapter has audio and timing, durations match), and published site
consistency. List the human-only checks as TODO lines so they are not
forgotten.

## Waiting on Background Work

Do not wait with `pgrep -f <pattern>` / `pkill -f <pattern>`: the pattern also
appears in the waiting shell's own command line, so it matches (or kills)
itself. Record the PID and poll with `kill -0 "$PID"`.
