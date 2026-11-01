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
coordinator writes `landed`, `done`, `cancelled`. Terminal states never revert;
reopening means a new card.
