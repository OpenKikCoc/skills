---
id: {{ID}}
title: {{TITLE}}
repo: {{REPO_PATH_OR_DASH}}
base: {{BASE_BRANCH}}
branch: fleet/{{id-slug}}
runtime: {{codex|claude|...}}
timebox: {{e.g. 2h}}
scope: {{small|medium|large}}
ambiguity: {{low|medium|high}}
risk: {{low|medium|high}}
verify_cost: {{cheap|costly}}
host_jobs: none
---

# Card {{ID}} · {{TITLE}}

> This card is the worker's only task input and the first section of its
> HANDOFF. The worker writes only the State, Progress, and Evidence sections
> below; it never edits the contract.

## Contract

- **Goal:** {{one sentence}}
- **Acceptance:** {{numbered, checkable criteria}}
- **Inputs:** {{named datasets, files, links}}. If an input is not named, first
  write down how the candidate set is defined and where it comes from, open a
  decision, and wait. Never substitute a similar list that happens to be at hand.
- **Preconditions:** {{what must already be true}}. If unmet, stop and say what
  is missing.
- **Scope:** repo and branch from the front matter; work only in the bound
  worktree; one item, one branch.
- **Gates:**
  1. A minutes-scale precheck before any expensive run; fail with a reason.
  2. Differential validation: baseline and candidate in the same environment;
     count only new failures, list baseline failures separately.
  3. Run gates in parallel by target, not one full serial rerun.
  4. Concurrency changes (queues, async, retries, caches) need interleaving
     tests and an adversarial review.
- **Not blockers (never write `blocked` or stop for these):**
  - live or production acceptance, canaries, deploys, and their receipts belong
    to the coordinator: deliver `awaiting-verify` and list them in `known_gaps`;
  - reading or changing code inside this card's scope needs no extra grant;
  - an optional tool is missing: fall back to plain `grep`/`git grep`/shell;
  - a shared interface is missing: add the minimum one and name it in Progress;
  - a provider capacity error (429, "capacity", "overloaded"): wait 1-2 minutes
    and retry, at most 5 times;
  - a local experiment environment this card already allows: just use it.
  Only credentials or logins, production writes, and the meaning of an
  authorization escalate to the coordinator.
- **Branch:** you may squash your own commits, rebase onto `{{BASE_BRANCH}}`
  (base wins conflicts), and `git push --force-with-lease origin <this card's
  branch>`. That is the only force-push allowed, and only for this card's
  branch. If the branch is renamed for rework (`<branch>-r2`), preflight rebinds
  it; no other rename is accepted.
- **Push each slice:** commit and push every tested slice at once. Uncommitted
  work older than 45 minutes without a push breaches this card. Do not start
  review rounds of your own; the coordinator schedules independent review.
- **Pre-push self-check:** run the project's static checker on every touched
  package; make tests pass under the default umask, not only under one your
  shell sets; compare paths after `realpath`, never as typed.
- **Prohibited:** pushing to the base branch; force-push of anything but this
  card's own branch (see Branch); merging or marking a PR ready; deploys or
  production writes; changing model or effort; spawning other sessions;
  building tunnels or proxies; editing files outside this item's scope;
  running fleet admin commands.
- **Escalation:** before the third fix of the same failure class, write a
  one-page design (invariants and a falsifiable test). If the fourth round
  still does not converge, stop; the coordinator will split or redesign.
- **Timebox:** if stuck for more than 30 minutes, write it in Progress and wait.
- **Execution batches:** after each model turn, finish one bounded batch:
  combine independent reads, make one atomic patch, run focused gates together.
  Stop only at completion, a real blocker or approval, a redesign, an external
  wait, or a context/safety limit. Never batch irreversible writes or retries of
  unknown results.
- **Evidence:** artifacts, reports, and logs cited as evidence live in the
  item's work directory with sha256 in the manifest. Code cards push the item
  branch (only that branch) and list a `commit` entry with sha and remote_ref.
- **Done means:** fill the Evidence manifest, run `fleet.py verify {{ID}}`
  until it passes, set state to `awaiting-verify`, and stop.

## State (worker-maintained, one line, overwrite)
`state: in-progress` · `updated: <ISO time>` · `blocker: none` · `doing: <plain words, ≤60 chars>`

## Progress (worker-appended; at most 3 lines per hour: progress / next / blocker)
<!-- The last line of every entry is exactly one executable `next_action: <command or edit>`;
     a relaunched session runs it first. -->

## Evidence (JSON, filled on completion; schema: evidence-manifest.schema.json)
```json
{}
```
