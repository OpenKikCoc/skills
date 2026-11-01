# Validation Provenance Contract

Use this contract for non-performance failure attribution. It binds baseline
and candidate results to distinct artifacts, fresh runtime epochs, the normal
default/release profile, and controller-computed raw plus normalized failure
signatures. Use the separate exact-SHA benchmark contract for performance.

## Controller Protocol

1. Pin distinct baseline and candidate artifact digests. Bind each run to
   controller-computed SHA-256 digests for the validator, fixture, exact command,
   parser, and environment. Run the candidate; run the baseline before making
   any comparative claim.
2. Activate each artifact in a fresh epoch and bind the run to that epoch. A
   reset is valid only in a test-owned runtime. When both runs reuse one
   runtime, the candidate epoch must directly follow the baseline epoch and
   carry an explicit reset receipt.
3. Inventory skips, retries, filters, exclusions, feature overrides, and other
   suppressors. Default/release validation must enable none of them, both in
   the inventory declaration and in each run.
4. Preserve every raw failure string. Normalize only through a named
   normalizer contract. Hash the raw and normalized UTF-8 bytes separately,
   then bind the normalizer ID and both hashes in a controller-computed digest.
5. Validate the packet before describing attribution:

```bash
python3 scripts/validation_provenance.py provenance.json
```

## Source-to-Runtime Lineage Closure

Before attributing a run, have the controller record full immutable object IDs
for `candidate_commit`, the resolved `target_ref_commit`, their recomputed
`merge_base`, and `runtime_commit`. Keep the human-readable target ref only as
context; pin its resolved commit at admission and start a new admission if it
moves.

Close one of these paths:

- **Direct:** `runtime_commit` equals `candidate_commit`.
- **Integrated:** declare the integration operation, independently recompute it
  from the candidate, recorded merge base, and pinned target-ref commit, and
  require the resulting tree and runtime commit to match. Candidate ancestry
  alone is insufficient because an arbitrary descendant can contain unrelated
  changes.

Bind the built artifact digest to `runtime_commit`, then bind activation,
loaded-runtime identity, and the validation epoch to that same digest. Reject
a mutable ref, display version, build label, desired-state record, or a loaded
artifact without its source receipt as lineage proof. If Git objects,
integration inputs, build receipt, activated artifact, loaded artifact, and run
epoch do not form one closed chain, classify the comparison as `unknown`.

Keep the Git and runtime receipts beside the schema-version-1 packet. The pure
validator checks declared artifact and epoch consistency; it cannot resolve
Git refs or interrogate a live runtime, so its success alone does not close
lineage.

## Manager or Control-Plane Vertical Acceptance

When the claimed behavior crosses a manager or control plane, add at least one
vertical acceptance path through real deployable components. An isolated,
test-owned environment is valid; invoking a handler directly or replacing the
claimed manager/runner boundary with a mock is not.

1. Submit the pinned candidate and target-ref tuple through the public ingress.
2. Read back the manager's authoritative persisted operation identity and
   resolved lineage tuple.
3. Let the real reconciliation or dispatch path activate the bound artifact in
   a fresh runtime epoch, then prove the runtime loaded it.
4. Exercise the behavior through the runtime boundary rather than a direct
   implementation call.
5. Observe the terminal result through the manager's public read/watch surface
   and correlate operation, runtime, epoch, artifact, and source identities
   end to end.

External dependencies outside the claimed boundary may use controlled
fixtures, but no fixture may manufacture a manager transition, runtime load,
or terminal projection being claimed. Include a fail-closed mismatch case for
stale or conflicting lineage. If the vertical path is unavailable, report the
tested lower-layer claim and the missing acceptance boundary; do not call the
manager/control-plane behavior validated.

## Classifications

| Baseline | Candidate | Classification |
|---|---|---|
| pass | fail | `regression` |
| fail, same normalized signature set | fail | `pre_existing` |
| fail | pass | `fixed` |
| fail, different normalized signature set | fail | `changed` |
| unavailable, both pass, or incomparable normalizers | any | `unknown` |

Candidate-only evidence or any comparison-surface digest mismatch is `unknown`
and cannot authorize a regression claim. Different normalizer namespaces are
incomparable rather than silently changed.
The pure validator performs no clock, process, runtime, or workspace access;
the trusted controller owns those observations. See
`scripts/test_validation_provenance.py` for packet fixtures.
