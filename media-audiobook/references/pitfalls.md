# Pitfalls

Each row is a real failure. "Enforce" names the kind of gate that stops it
recurring.

| # | Symptom | Root cause | Rule | Enforce |
|---|---|---|---|---|
| 1 | A character in their twenties sounds fifty | voice picked by pitch from a sample library whose male voices were all 46-55 | design voices from the character, check age, human listens | synthesis refuses without approval |
| 2 | A male character is synthesized as female | pitch-shifted (+1.5) reference flips gender when cloned; only the reference was checked | check **cloned** lines | audition report; QA numbers in the voice map |
| 3 | Pitch-shifted variants sound older | shift distortion is cloned | shift <= +/-2 semitones and re-check; prefer design | pick step |
| 4 | Male voice used as female | gender judged by F0 alone | use a gender classifier | voice probe |
| 5 | Correct gender "fixed" to the wrong one | inferred from a nickname, source not read | read the source before changing cast attributes | cast step note |
| 6 | Dialogue read as narration | truncated quotes, wrong-direction quote marks | leftover quotes after segmentation = 0 | text step, verify |
| 7 | Flat delivery | one emotion label per line, no context | separate delivery step with a cue per line | delivery step |
| 8 | Emotion mix out of model range | only the model's web UI normalizes; the library API does not | call the normalization yourself | synthesis |
| 9 | LLM worker returns empty / ignores long input | agent CLI tries tools; stdin silently dropped | no-tools prompt + streaming input | LLM wrapper |
| 10 | Structured output fails to parse | wrapped or concatenated JSON | one tolerant parser | LLM wrapper |
| 11 | Two synthesis processes are slower | 12 GB VRAM cannot hold two models | one GPU process; parallelize I/O | synthesis docs |
| 12 | Title pause shorter than designed | join code bug | timing and audio from one pass, compare durations | verify |
| 13 | Large outputs fill the system disk | output directory defaulted to the system volume | write to a configured data volume; fail if unset | book config |
| 14 | Two characters sound like one person (very high speaker similarity) | designed voices converge; prompts described temperament only | acoustic contrasts + multiple candidates + joint pick + similarity gate | approval refuses on similarity |
| 15 | First similarity threshold was a guess | no calibration | calibrate every automatic gate on known-same/known-different samples and record it | constants with calibration notes |
| 16 | Adult reference cloned into a child; a young voice cloned decades older | pick judged references only | judge cloned output during the pick | two-stage pick |
| 17 | Assumed a demo-track method would fix recitation tone | designed without measuring | A/B delivery schemes first; check that a metric separates known good from bad before using it | A/B script |
| 18 | Phone does not advance to the next chapter on the lock screen | network fetch before `play()` | prefetch; synchronous `play()` in `ended` | browser test asserting same-task play |
| 19 | Tapping the last lines jumps to the start or next chapter | stable audio URL served stale shorter audio | content-fingerprinted URLs, duration check, clamped seek | per-chapter seek test + stale-cache test |
| 20 | Declared fixed after desktop tests; still broken on the phone | no device evidence | diagnostic beacon first, then fix | diagnostics in player |
| 21 | Users still see a fixed bug | restored tab keeps old JS | build number + reload when paused/foregrounded | build test |
| 22 | Re-running merge nearly wiped all delivery annotations | merge rewrote every chapter's annotation file | per-chapter merge preserving unchanged delivery | merge step |
| 23 | An HTML entity read aloud | entity split across pages, reassembled after join | clean after joining; entity = FAIL | text step, verify |
| 24 | Source is EPUB/MOBI only | - | read ebooks directly in spine order | text step |
| 25 | Chapter structure in ebooks is wrong | converter artifacts, unnumbered headings | inspect first; numbered regex, drop regex, or explicit heading list | text step |
| 26 | Author's notes, placeholders, ads read aloud | text scraped from a serial site | drop rules + end marker; grep count = 0 | book config, spot check |
| 27 | Second half of a quote read as narration | nested quotes, multi-paragraph speech | nested -> secondary marks; span state across paragraphs; byte-identical regression on an earlier book | text step, verify |
| 28 | Punctuation-only segments produce noise | segmentation leaves bare punctuation | emit short silence | synthesis |
| 29 | Recurring characters across a series change voice | each book cast independently | inherit narrator, defaults, and same-name characters from the earlier book; still pass the gates | voice spec / pick |
| 30 | Batch LLM steps fail mid-book | one provider's quota window exhausted | automatic provider fallback; nonzero exit while work remains | LLM wrapper, annotate step |
| 31 | 5% unknown speakers | cast table lists only named characters | generic "other male/female" options mapped to default voices | merge step |
| 32 | Child voices never pass the gender gate | classifier has a child class; gate calibrated on adults | for targets under 16 count child probability | voice probe |
| 33 | An inherited voice collides with a new lead | joint pick ordered by line count put inherited voices last | inherited voices are fixed and go first | pick order |
| 34 | Publish fails silently on long chapter titles | 255-**byte** filename limit; thread-pool exceptions deferred | truncate titles in filenames; print failures immediately | output path helper, synthesis |
| 35 | Earlier book vanished from the shelf | site rebuild listed only the current book | build the shelf from all published books | site generator |
| 36 | Browser tests only worked for the first book | titles and counts hard-coded | read from published metadata | browser tests |
| 37 | Browser-test gate cannot run | it depended on another project's Playwright install | ship the test dependency with its own lockfile; install on demand | test command |
| 38 | A wait loop matched or killed itself | `pgrep -f` pattern appears in the waiting shell's command line | wait on a recorded PID | operating note |
