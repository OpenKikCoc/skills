# Cards and HANDOFF

## The card is the only task input

A worker receives exactly one document: its card. Everything it may rely on —
goal, acceptance, inputs, scope, gates, prohibitions, timebox — is in the card.
Chat context, earlier sessions, and "what the coordinator meant" are not
inputs. This is what makes a session replaceable: any new generation started
from the card and its HANDOFF can continue the work.

Template: [../assets/dispatch-card.template.md](../assets/dispatch-card.template.md).

### Front matter

| Key | Required | Meaning |
|---|---|---|
| `id` | yes | unique item id, e.g. `W-12` |
| `title` | yes | short title |
| `repo` | yes | path to the repository, or `-` for non-code cards |
| `base` | code cards | target branch for landing (e.g. `main`) |
| `branch` | code cards | item branch; default `fleet/<id-slug>` |
| `runtime` | yes | runtime adapter name from `fleet.json` |
| `timebox` | yes | e.g. `90m`, `2h` |
| `scope`, `ambiguity`, `risk`, `verify_cost` | yes | complexity signals, see effort-routing.md |
| `effort`, `effort_reason` | no | explicit effort override; reason mandatory |
| `tier`, `reviewer_tier` | no | one approved (model, effort) tuple from `model_tiers`, see effort-routing.md; excludes `effort` |
| `host_jobs` | no | `none` (default), `after-land`, or `explicit` |
| `depends_on` | no | comma-separated item ids that must be done first |

`fleet.py card lint` rejects a card with a missing key, an unknown signal
value, an override without reason, or a missing section.

## HANDOFF

`launch` copies the card into the work directory as `HANDOFF.md` (excluded from
Git through `.git/info/exclude`). The worker writes only three sections:

- **State** — one line, overwritten:
  `state: in-progress|blocked|awaiting-verify` · `updated: <ISO>` ·
  `blocker: <none|text>` · `doing: <plain words>` and optionally
  `next:`, `eta:`, `result:` (required for `awaiting-verify`).
  `doing` and `result` are for a human glance: no paths, hashes, or secrets.
- **Progress** — appended, at most 3 lines per hour: progress / next / blocker.
- **Evidence** — a JSON manifest, schema
  [evidence-manifest.schema.json](../assets/evidence-manifest.schema.json).

The contract part above those sections is immutable for the item's lifetime.
`verify` compares it with the registered card hash; a changed contract fails.
To change a contract, the coordinator cancels the item and writes a new card
with a new id; the reference CLI refuses to relaunch an item whose contract
changed.

## What the template adds beyond the contract basics

- **Not blockers.** A short list of things a worker must never stop for:
  live acceptance and deploys (the coordinator's; ship `awaiting-verify` with
  `known_gaps`), reading or changing in-scope code, a missing optional tool
  (fall back), a missing shared interface (add the minimum, name it), and
  provider capacity errors (wait 1-2 minutes, retry at most 5 times). Only
  credentials, production writes, and authorization semantics escalate. The
  coordinator treats a `blocked` state for a listed reason as one immediate
  steer, not a 30-minute wait.
- **Branch rule.** The worker owns its card branch: it may squash its own
  commits, rebase onto the base, and `--force-with-lease` push that branch
  only. Force-push anywhere else stays banned. If the branch was renamed for
  rework (`<registered>-rN`), `preflight --action commit|build` rebinds the
  registry to the new name under the registry lock and records a
  `branch:rebound` event; any other name is denied.
- **Push each slice.** Tested slices are committed and pushed at once;
  uncommitted work older than 45 minutes without a push breaches the card.
  Workers do not start their own review rounds.
- **Pre-push self-check** (static checker on touched packages, default umask,
  `realpath` before comparing paths).
- **Checkpoint line.** The last line of every Progress entry is exactly one
  executable `next_action:`. A relaunched session runs it first, so recovery
  does not depend on re-deriving the plan.

## Work directory

- Code cards: a dedicated Git worktree on the item branch, e.g.
  `<fleet_home>/worktrees/<slug>`.
- Non-code cards: `<fleet_home>/work/<slug>`.

Binaries, reports, and logs cited as evidence must stay inside it; temporary
directories are not evidence.

## States

```text
queued → in-progress ⇄ blocked
             ↓
       awaiting-verify → (verify) → in-review → PASS → landed → done
                                       └ REWORK → in-progress (same or new generation)
```

Only the worker writes `in-progress`, `blocked`, `awaiting-verify` — in its
HANDOFF state line, which `watch` projects into events; `fleet.py mark` is the
same with a worker capability, and refuses `awaiting-verify` unless `verify`
passes. Only the
coordinator writes `landed`, `done`, `cancelled`. A code card reaches `done`
after `land` (or `complete-verified --kind already-integrated`); a non-code
card (`repo: -`) reaches it only through `complete-verified --kind noncode`,
and plain `mark done` is refused for it. Terminal states never revert;
reopening means a new card.
