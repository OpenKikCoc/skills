---
name: meta-skills-repo
description: "Maintain a canonical skills repository that is mirrored into several coding agents' skill directories (Claude Code, Codex, Gemini, others): validate skills, run their tests, sync mirrors without clobbering unknown files, verify byte equality, and run a publish gate with a configurable sensitive-term scan before pushing a public repo. Use when adding or editing skills, fixing mirror drift, or preparing a release. Not for writing the content of an individual skill or for managing non-skill repositories."
---

# Skills Repository

The repository checkout is the only source of truth. Installed skill
directories (`~/.claude/skills`, `~/.codex/skills`, `~/.gemini/skills`, ...)
are mirrors: never edit them in place, and never delete things in them that
this tool did not put there.

## Command surface

One standard-library script, run from the repository root (or `--repo PATH`):

```bash
python3 <skill-root>/scripts/skills_repo.py validate          # static checks
python3 <skill-root>/scripts/skills_repo.py test              # each skill's scripts/test_*.py
python3 <skill-root>/scripts/skills_repo.py sync --dry-run    # preview mirror changes
python3 <skill-root>/scripts/skills_repo.py sync              # apply
python3 <skill-root>/scripts/skills_repo.py verify            # mirrors == source
python3 <skill-root>/scripts/skills_repo.py gate              # validate + test + sensitive scan
python3 <skill-root>/scripts/skills_repo.py reproduce         # validate, test, sync, verify
```

Global options go before the action: `--repo`, `--manifest`, `--home`,
`--target ID` (repeatable), `--skill NAME` (repeatable).

## Required order for a change

1. Edit only the repository checkout.
2. `validate`, then `test --skill <name>` for content-only changes, or `test`
   for changes to shared tooling.
3. `sync --dry-run`, read the plan (conflicts, kept unknown files), then `sync`
   and `verify`.
4. Before publishing a public repository run `gate` (below); it must pass.
5. Review `git diff` for secrets, absolute paths, and generated files, then
   commit and push. Confirm the remote and mirrors afterward.

Reuse a passing result while source bytes and command are unchanged; always
rerun `validate` after the final edit.

## What validate enforces

- `SKILL.md` has front matter with `name` equal to the directory name
  (lowercase-hyphen) and a non-empty `description` (at most 1024 characters).
- Relative Markdown links resolve and stay inside the skill, reported as
  `file:line`. `?query` and `#fragment` are stripped before resolving. Fenced
  code (``` or ~~~, any length of three or more) is skipped without shifting
  line numbers; URLs and pure anchors are skipped. Single-line reference
  definitions (`[id]: path`, up to three leading spaces) are checked even when
  unused. Limits: this is not a full Markdown parser; definitions inside a
  blockquote or list item, or with the destination on the next line, are not
  checked.
- No symlinks or stray `._*` files inside a skill, and no special files (FIFO,
  socket, device): these are errors, and sync/gate fail closed on them instead
  of opening them.
- A `scripts/` directory with Python sources must contain `test_*.py`.
- No duplicate skill names.

## Sync safety

Each mirrored skill directory carries a small marker listing the files this
tool wrote. Sync then:

- copies only changed files and preserves the full executable mask (owner,
  group, other); files are opened without following links and without
  blocking, and checked to be regular files;
- prunes only files recorded in the marker that left the source;
- keeps unknown files and reports them;
- leaves other skills in the target untouched, and skips targets whose root
  directory does not exist (unless the manifest sets `"create": true`);
- refuses a symlinked destination and any non-regular entry in the mirror
  (`--force` never writes through one);
- refuses to overwrite a pre-existing differing file it did not write, unless
  `--force`.

Copy success is not verification: `verify` compares content and executable
bits and reports stale managed files.

## Publish gate

`gate` runs `validate`, `test`, and a sensitive-term scan over every file in
the working tree (and file names). Findings print `path:line` and the
deny-list entry number, never the term, so CI logs stay clean.

- Deny list: `.sensitive-terms.txt` in the repo root (or `--deny-list FILE`,
  or `deny_list` in the manifest). One term per line, case-insensitive; prefix
  `re:` for a regex; `#` comments. Keep it out of git (the gate fails if it is
  tracked). Start from
  [sensitive-terms.example.txt](references/sensitive-terms.example.txt), which
  is empty.
- `--require-deny-list` fails when no list exists; use it in publish
  pipelines so an absent list cannot pass silently.
- `--history` also scans git author names and commit messages.
- Built-in checks regardless of the list: absolute home-directory paths,
  private-key blocks, quoted secret assignments, unquoted assignments with a
  token-like value, `Bearer`/`Basic` credentials, and
  secret query parameters (`?token=`, `access_token=`, `refresh_token=`,
  `key=`). Placeholders (`${VAR}`, `<value>`) are allowed. Findings name the
  line and class, never the matched text. A same-line `skills-repo: allow-test-secret`
  comment exempts an obviously synthetic fixture, only in `test_*.py` and never
  for private keys or quoted literals.
- Optional `expected_origin` in the manifest (a remote URL or canonical
  `host/owner/repo`): `gate`, `sync`, and `reproduce` refuse to run unless
  `git rev-parse --show-toplevel` equals `--repo` and `origin` normalizes to the
  same identity. Host case is ignored, path case is not; `.git` and a trailing
  `/` are stripped; local paths, whitespace, `?`, `#`, backslashes, embedded
  passwords, bad ports, `.`/`..`/`%` parts, and fewer than two path segments are
  rejected. Without a git checkout or `origin`, it fails closed.

A clean gate is necessary, not sufficient: the deny list only knows what you
told it. Also read the diff.

## Vendor guidance reference

When a skill depends on guidance shipped inside a CLI (a vendor guidance
reference), never copy that text into this repository. Name the exact read
command, record its version (`<cli> --version`) where the skill depends on it,
and probe `--help` for the flags in use. If the guidance, a referenced file, or
the command is missing, stop that dependent step, keep local work, and report
the dependency gap instead of reconstructing the guidance from memory.

## Configuration

Optional `skills-manifest.json` at the repo root; with none, skills are
auto-discovered under `skills/` (or the root) and the default targets are
`claude`, `codex`, and `gemini`. See
[manifest-example.md](references/manifest-example.md).

## Validation

```bash
python3 -I <skill-root>/scripts/test_skills_repo.py
```
