# Effort routing

Reasoning effort is a per-card budget, not a global default. A typo fix at
`xhigh` wastes quota and wall-clock; a concurrency redesign at `low` produces
confident wrong code that costs more review rounds than the effort it saved.
`fleet.py route` turns four declared complexity signals into a tier, and the
tier into a concrete model and effort per runtime.

## Signals (declared on the card)

| Signal | 0 | 1 | 2 |
|---|---|---|---|
| `scope` — how much changes | `small`: one file or function | `medium`: one module, a few files | `large`: several modules or an interface |
| `ambiguity` — is the solution known | `low`: known fix, clear spec | `medium`: approach known, details open | `high`: needs diagnosis or design |
| `risk` — cost of a wrong result | `low`: local, reversible | `medium`: shared code, migrations, user-visible | `high`: irreversible effects, production, credentials, data loss, concurrency |
| `verify_cost` — cost of checking | `cheap`: seconds–minutes | `costly`: long builds, benchmarks, live environments | — |

The card author (coordinator or human) fills them. Unknown is `medium`, not
`low`: an undeclared signal must not buy a cheaper tier.

## Score to tier

```text
score = scope + 2*ambiguity + risk + verify_cost      # 0..9
0–1 → low     2–3 → medium     4–6 → high     7–9 → xhigh
```

Ambiguity is weighted double because reasoning effort mainly pays off when the
model must find the approach, not when it must type more of a known one.

Floors and ceilings, applied after the score:

- `risk: high` → at least `high`.
- `ambiguity: high` → at least `high`.
- `max` is never routed; it needs an explicit card override.
- An explicit `effort:` on the card wins, but needs `effort_reason:`; `route`
  reports the override next to the computed tier so a reviewer can see it.

## Per-role effort

| Role | Effort |
|---|---|
| worker | routed tier |
| reviewer | `max(worker tier, medium)`; at least `high` when `risk` ≥ medium |
| coordinator | fixed in config (default `high`); it judges evidence, it does not write code |
| supervisor | whatever its own session uses; not routed |

A reviewer below the worker's tier tends to accept what it cannot follow.

## Tier to runtime

Canonical tiers are `low`, `medium`, `high`, `xhigh`, `max`. Each runtime maps
them in `fleet.json` (see `assets/fleet.example.json`), optionally to a
different model per tier:

```json
"tiers": {
  "low":    {"model": "<fast model>",  "effort": "low"},
  "medium": {"model": "<main model>",  "effort": "medium"},
  "high":   {"model": "<main model>",  "effort": "high"},
  "xhigh":  {"model": "<main model>",  "effort": "xhigh"}
}
```

If a runtime lacks a tier, `route` clamps to the nearest supported level **up**
for workers and reviewers and records the clamp. Verify supported levels with
the CLI's own `--help` before adding a runtime; flags differ between versions.

## Escalation and de-escalation

Effort is fixed for a generation. Changing it mid-session is forbidden for the
session itself (it would let a struggling worker buy its way out silently).
The coordinator escalates by launching a new generation one tier up from the
evolved HANDOFF:

| Trigger | Action |
|---|---|
| second REWORK for the same failure class | escalate one tier |
| reviewer verdict cites reasoning gaps (missed invariant, wrong diagnosis) | escalate one tier |
| worker blocked on diagnosis past half its timebox | escalate one tier, or re-slice |
| already at `xhigh` and still not converging | split the card or redesign; `max` only with an explicit human decision |

Each tier is used at most once per item, so escalation terminates. Never
downgrade a live item; lower the tier only on a new card, and only when the
history shows the work was easier than declared (e.g. a PASS on the first
review with spare timebox). Record every escalation in the item journal with
the trigger, so the routing table can be recalibrated from real outcomes.

## Calibration

The weights above are a starting point. After a batch of cards, compare tier
against outcome (review rounds, escalations, timebox overruns). Adjust the
thresholds in config, not the per-card signals, so cards stay comparable.
