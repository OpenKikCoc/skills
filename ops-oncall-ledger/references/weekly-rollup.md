# Weekly roll-up

`ledger.py rollup LEDGER --week YYYY-Www` compiles `weekly/<week>.md` from
`tickets/*.json` and nothing else.

## Selection

A ticket is included when its `date` falls in the ISO week **and**
`mode=investigate-and-rollup`. Investigate-only tickets and other weeks are
ignored. Every selected ticket must pass `validate --for-rollup` (authorization
`apply`, RCA `published`, complete source evidence). One failing ticket aborts
the whole roll-up with a list of reasons and writes nothing, so the report never
silently omits or half-includes a ticket. A record whose `ticket_id` does not
match its file name or that fails basic validation also aborts.

## Derived values

- Total tickets, counts by priority and by status.
- **AI coverage** = tickets with `rca.agent_used` / all tickets.
- **AI accuracy** = `confirmed` / reviewed, where reviewed = agent used and
  `accuracy != unreviewed`. `partially_confirmed` and `incorrect` stay in the
  denominator but not the numerator. Zero reviewed tickets prints `0/0 (n/a)`.
- Both are reported overall, by category, and by environment.

Never hand-edit these. Fix the record and re-run.

## Idempotence

Output is deterministic: tickets are sorted by priority then ID, there are no
timestamps, and writes are atomic and skipped when content is unchanged
(`noop`). `--check` returns 1 when the stored file differs, for CI or a
pre-report gate. Concurrent writers: use one writer per ledger directory.

## Language

The report language follows the record content. If your team needs the weekly in
a specific language, translate in the record fields (title, summary, impact);
the generated headings are English.
