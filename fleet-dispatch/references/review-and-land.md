# Verify, review, land

Three different checks by three different actors. None replaces another.

## 1. Mechanical verify (`fleet.py verify ITEM`)

Run by the worker before it claims `awaiting-verify`, and again by the
coordinator before starting a reviewer. It checks only facts:

- the HANDOFF contract hash equals the registered card hash;
- the Evidence section parses and satisfies the manifest schema; `item` matches;
- every `artifact`/`report`/`test` path exists inside the work directory and
  its sha256 matches;
- every `commit` entry's SHA equals `git ls-remote` of its `remote_ref`, and
  that ref is the item branch;
- the candidate is one commit on top of the current base (the worker squashes);
- the state line is `awaiting-verify` with a `result`.

Output is JSON with `ok` and a list of failures. `ok` means the evidence is
consistent, not that the work is correct.

## 2. Independent review

The coordinator launches a reviewer — a different session, at the effort from
effort-routing.md — with the card, the manifest, and the candidate SHA.

Reviewer steps:

1. Re-read acceptance; map each criterion to evidence it can check itself.
2. Re-run the cheapest discriminating gate on the exact SHA; do not trust
   reported results for anything the verdict depends on.
3. Read the diff for what the tests cannot see: invariants, error paths,
   concurrency, security, data loss, scope creep.
4. Record the verdict:

   ```bash
   fleet.py review-record ITEM PASS|REWORK \
     --candidate-sha <40-hex> --manifest-sha256 <64-hex> \
     --notes-file review.md
   ```

A REWORK lists concrete requirements, each with evidence. A PASS with open
concerns is a REWORK. The reviewer never edits the candidate and never changes
the item's state. The verdict is bound to the SHA and manifest hash: any new
commit or manifest edit invalidates it.

Converge: review for real defects (wrong results, data loss, security, broken
functionality), not style. A second review round looks only at the rework.

## 3. Land (coordinator only)

```bash
fleet.py land ITEM --sha <40-hex>
```

A transaction with persisted phases; each step is checked, none is retried
blindly:

1. the latest verdict is PASS for exactly this SHA and the current manifest hash;
2. `verify` still passes;
3. the base has not moved past the candidate's parent, i.e. the push is a
   fast-forward (otherwise: rework to rebase, then re-verify and re-review);
4. `git push origin <sha>:refs/heads/<base>` — never force;
5. record `landed` with the pushed SHA. Post-land work (deploy, a host job)
   runs from that exact SHA and returns a receipt; the reference CLI leaves it
   to the coordinator (`host-job open` or `fleet-remote-runner`).

A phase whose result is unknown (e.g. the push timed out) becomes
`result_unknown`, and further `land` calls are refused. `land --reconcile`
only inspects the remote: it records `landed` if the base now equals the SHA,
or `not_landed` (after which a new `land` is allowed). It never re-runs the
push. Then `mark ITEM done`.
