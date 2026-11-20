---
name: media-audiobook
description: "Turn a novel (TXT/EPUB/MOBI) into a multi-voice audiobook with a read-along web player, using cheap LLMs for text understanding and a local GPU TTS for synthesis. Covers text cleanup and quote segmentation, cast analysis, two-model speaker attribution with adjudication, context-derived delivery (emotion mix, pace, volume), voice design from character analysis with objective age/gender/distinctness gates measured on cloned output, human audition and approval, resumable chapter-by-chapter synthesis and publishing, and the read-along player's mobile pitfalls (lock-screen auto-advance, stale cached audio, stale single-page JS). Use when producing or re-voicing an audiobook, adding a book to an existing pipeline, or debugging voice/timing/player problems. Do not use it to pick voices by pitch from a sample library, or to declare a mobile player bug fixed from desktop browser tests alone."
---

# Multi-voice Audiobook Pipeline

The bottleneck is TTS, not the LLM: text understanding is cheap work for any
capable model, so route it to inexpensive providers and keep the expensive
agent for orchestration. Synthesis on one 12 GB consumer GPU runs at roughly
0.4x real time; a 12-hour book is an overnight job.

Every rule below comes from a real failure. A rule that lives only in a
document gets broken again on the next book, so each one should also be
enforced in code (a gate that refuses to proceed).

## Four Principles

1. **Voices come from characters, not from a voice library.** Character
   analysis (who, age, temperament) -> written voice design -> generated
   candidates -> objective checks -> human audition -> approval -> synthesis.
   Picking "a stock voice with roughly the right pitch" caused every voice
   problem in the first book.
2. **Delivery comes from context, not from one emotion label.** Fix the
   speaker first (two models plus adjudication), then read the context
   separately to decide *how* the line is said. Priority: explicit cues in the
   narration ("through gritted teeth", "whispered") > scene > personality.
   Map the decision to parameters the TTS can actually execute.
3. **Check the final artifact.** A good reference clip does not imply a good
   clone: a male reference can clone into a female voice, and a young-adult
   reference into a child. Run age/gender checks and auditions on
   *cloned lines*.
4. **Sample first, full run later; gates live in code.** Chapter one plus a
   character audition reel before the whole book. Synthesis refuses to run
   until the voice map carries an explicit approval flag.

## Pipeline and Gates

| Step | Output | Gate (stop if it fails) |
|---|---|---|
| 1 Text | chapters -> segments (narration / quote) | chapter count matches the book; leftover quote marks after segmentation = 0; no HTML entities or pagination debris; spot-check 3 chapters |
| 2 Cast | character table | two models build it independently; on gender/age disagreement **read the source** (nicknames lie) |
| 3 Speakers | speaker per quote | two models label independently, agreement >= 95% (lower means segmentation is wrong); disagreements go to the stronger model with the whole chapter; leftover unknown <= 1% |
| 4 Delivery | emotion mix, pace, volume, cue | every quote has a cue traceable to the text or scene; strongly cued lines visibly change parameters |
| 5 Voices | approved voice map + reference clips | see "Voice Design"; a human must listen before approval |
| 6 Synthesis | per-chapter audio + sentence timing | single GPU process; per-sentence speech-rate outliers are listened to; timing end vs. audio duration < 0.1 s |
| 7 Publish | static read-along site | desktop + phone-emulation journey; click-to-seek on every chapter; stale-cache test; real-phone lock-screen test |

Bundle every automatic gate into one `verify <book>` command (PASS / WARN /
FAIL, nonzero exit on FAIL) and one browser test command. Run them before
approval, after full synthesis, and after publishing. Details per step:
[references/pipeline.md](references/pipeline.md).

## Voice Design

- Write the design prompt with a concrete age, gender, timbre, temperament,
  speaking rate, and "studio quality, no background". Describe the *everyday*
  speaking voice with neutral affect; per-line emotion is added at synthesis.
- Young characters: say explicitly "young, no weathered quality". Models
  render "deep, commanding" as middle-aged by default.
- Same-gender leads must differ in **acoustic** features (register, breathy vs.
  clear, rasp, nasality, rate), not just temperament adjectives. Voices
  generated from text descriptions converge (example: same-gender designs
  showed a median speaker similarity of 0.61, a maximum of 0.91).
- Only characters with enough lines (about 8+) get a dedicated voice; the rest
  use a default male/female voice. **Design the narrator first**: it is about
  75% of the runtime, and changing it means resynthesizing the whole book.
- Two-stage pick: shortlist the top 3 candidates per character on the
  reference clip, then actually clone 3 of the character's lines with each and
  decide on the cloned result (two lines are too noisy when one is emotional).
- Generate about 6 candidates for leads and pick **jointly**, in order of line
  count: each character takes the candidate least similar to the
  already-chosen same-gender leads. Inherited voices (series books) are fixed
  and go first.
- Objective gates on cloned lines (an open age/gender classifier plus the TTS
  model's own speaker encoder):
  - target-gender probability >= 0.8;
  - perceived age within 10 years of target, judged **relatively** (models
    trained mostly on other languages are biased in absolute terms, but young
    characters must still sound younger than their parents);
  - for children (< 16) count the classifier's child probability toward the
    gender target, otherwise child voices never pass;
  - distinctness: for major characters and the narrator, same-gender pairwise
    speaker similarity < 0.65.
- **Calibrate every automatic threshold** on known-same / known-different
  samples before trusting it, and record the calibration. Example values: different
  real speakers median 0.21; the same voice on different lines 0.65-0.91.
- On failure, rewrite the design prompt and regenerate. Do not fall back to
  stock voices or loosen thresholds. After two failed rounds a human may waive
  a minor character; the reason is written into the voice map.
- Pitch-shifted variants: at most +/-2 semitones and always re-checked. The
  distortion is cloned too: small shifts can add a decade of perceived age or
  flip perceived gender.
- Audition reel: the narrator announces each character and their brief, then
  the character says two real lines (one calm, one emotional) with exactly the
  production delivery parameters. Ask: right age? fits the person?
  distinguishable from other same-gender leads?

## Delivery (Context -> Parameters)

| Field | Values | At synthesis |
|---|---|---|
| mix | 1-2 emotions with weights | scale the dominant emotion to 1.0 keeping ratios, then the model's own normalization (bias + total cap) |
| pace | slow / normal / fast | duration factor about 1.12 / 1.0 / 0.9 |
| volume | whisper / soft / normal / loud | post-gain about -9 / -4 / 0 / +3 dB (TTS does not whisper) |
| cue | short justification | not synthesized; for audit and audition |

When there is no clear cue, stay flat (low calm) rather than acting every
line. Passing the annotated weights straight through, after the model's bias
and cap, made delivery weak and recitation-like; scaling the dominant emotion
to 1.0 measurably helped. A "director's demo track" (design-voice performance
used as the emotion prompt) gave no measurable gain at +3 h cost. An
emotion-recognition model is only a coarse filter, not a gate: it scored even
purpose-acted demos poorly. Decide the delivery scheme by a human A/B on 15-20
strongly cued lines, each played after its preceding narration.

## Data Placement

Version-control what is **human-decided or would change on regeneration**:
book config (source path, encoding, cleanup rules), cast table, final speaker
and delivery annotations, voice design prompts, approved reference clips (a
few hundred KB each, lossless), and the voice map. Do not version what can be
rebuilt deterministically from those plus the source text: segments,
per-sentence audio cache, chapter audio, timing, the site. Do not version
copyrighted source text; record its location and keep a cold copy.

## Operations

- One GPU synthesis process. Two processes on 12 GB were twice as slow.
  "Parallel" means overlapping synthesis with encoding and publishing.
- Cache per sentence so an interrupted run resumes; after changing a voice or
  delivery, invalidate only the affected speaker's lines.
- Publish chapter by chapter as they finish, and rebuild the shelf from every
  published book, not just the current one.
- LLM steps: expect a provider's quota window to run out when two books run
  in parallel; fall back automatically to another provider, and make steps
  exit nonzero when work is left so chained commands stop.
- The test harness ships with the pipeline (its own lockfile, installed on
  demand). Do not depend on another project's installed tooling: a gate that
  cannot run is a gate that silently stops being run.

Full pitfall table with root causes and enforcement points:
[references/pitfalls.md](references/pitfalls.md). Read-along player rules for
mobile Safari: [references/player.md](references/player.md).
