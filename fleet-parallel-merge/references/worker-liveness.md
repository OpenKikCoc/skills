# Worker Liveness And Bounded Convergence

Use `scripts/worker_liveness_advisor.py` only after a worker request was
accepted but repeated follow-ups do not establish task progress. The script is
a pure JSON advisor. It does not inspect a host, Git, files, receipts, workers,
or processes and does not preserve, stop, create, or dispatch anything.

## Evidence boundary

The controller supplies the exact validated plan and canonical `plan_sha256`,
the task assignment, the old execution identity and assigned worktree, and a
baseline snapshot. Each accepted follow-up supplies a unique follow-up ID, host
status for context only, an optional acknowledgement bound to the old execution
and task, and a fresh controller observation bound to that execution and
worktree.

The observation contains `head_sha`, a digest of the complete relevant file
state, and an optional digest of task receipts. Derive these independently and
consistently across the review window. A host label such as `running`,
`accepted`, or `completed` is never a progress signal.

Treat the assigned task and its complete acceptance contract as one end-to-end
acceptance slice. Component edits, command count, status churn, and other
activity do not establish slice progress. The canonical `intent_sha256` binds
the plan digest, task, base, write set, and sealed substrate.

Set `accepted_followup_limit` to exactly `2`. Before the boundary, unchanged
evidence yields `await_bounded_followup`. A bound acknowledgement or any
observed HEAD, file-state, or receipt-state change yields
`continue_existing_execution`. After two unchanged accepted follow-ups, the
advisor reports the old lineage as `lost`; do not send it further work.

## Recovery and replacement

After lineage loss, preserve and inspect the old assigned worktree. Supply a
receipt for each action bound to the old execution ID and worktree root. Supply
an independently observed writer state bound to the old execution. Unknown or
running state blocks replacement; only `stopped` passes that gate. Preservation
protects recoverable work but is not cleanup or integration authority.

Review the replacement before starting it. Its packet must retain the old
task, canonical plan digest, base SHA, exact write set, and sealed substrate,
while using a new execution ID and a separate, non-overlapping worktree root.
The candidate must state `writer_started: false`; only the returned
`dispatch_fresh_execution` action admits dispatch. Activity from that new
execution never establishes progress or acceptance for the lost execution.

The unchanged-intent replacement budget is one. Legacy packets omit
`unchanged_intent_replacement_count` and therefore describe the initial
execution. When the current old execution is the admitted replacement, set the
count to `1`. A second replacement candidate is rejected.

If that replacement also reaches two unchanged accepted follow-ups, preserve
and inspect its worktree and prove its writer stopped exactly as above. Then
either provide one `single_owner_fallback` or omit it:

```json
{
  "owner_id": "controller",
  "intent_sha256": "<digest returned by the advisor>",
  "authority_receipt_sha256": "<digest of independently retained authority evidence>"
}
```

A matching owner yields `collapse_slice_to_single_owner`. The owner takes the
whole acceptance slice, not a component or a validation activity. An absent
owner yields `block_no_authorized_owner`. Both outcomes stop re-spawning; the
authority receipt is assignment evidence, not permission to exceed the plan's
write set or perform unrelated mutations.

Run the advisor with:

```bash
python3 scripts/worker_liveness_advisor.py \
  --input worker-liveness.json --output worker-liveness-advice.json
```

Malformed, stale, cross-task, cross-plan, cross-substrate, wrong-worktree, or
already-started replacement packets, exhausted replacement attempts, and owner
bindings for a different intent fail closed with exit code `2` and a structured
`worker-liveness-error`. A successful decision exits `0`.
