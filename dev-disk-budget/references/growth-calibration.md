# Receipt-Backed Growth Calibration

Use `scripts/growth_calibration.py` only after a conservative next-wave peak
estimate already exists. The script is a pure JSON validator and advisor: it
does not inspect paths, create manifests, mutate caches, or authorize cleanup.

## Evidence Contract

The request has `schema_version=1` and
`kind=dev-disk-budget-growth-calibration-request`, with exactly these
inputs:

- `requested_expected_growth_bytes`: the original conservative peak estimate;
- `command_class` and `target_class`: stable caller-defined workload classes;
- `cache_identity`: exact `source`, absolute `path`, `device`, `inode`, and
  `uid` of the shared cache;
- `completed_wave_receipts`: at least two prior receipts;
- `current_shared_cache`: the cache allocation observation from the last
  receipt through the current decision point, plus `active`;
- `safety_margin_bytes`: a positive additive margin; zero is valid input but
  cannot authorize calibration;
- `disk`: current `total_bytes`, `free_bytes`, `min_free_bytes`, and
  `min_free_percent`.

Each completed receipt has
`kind=dev-disk-budget-wave-growth-receipt`, a unique `wave_id`,
`status=completed`, the same command/target/cache identities, and two distinct
measurements:

```json
{
  "owned_output": {
    "baseline_allocated_bytes": 0,
    "peak_allocated_bytes": 6442450944,
    "newly_allocated_bytes": 6442450944,
    "ownership_validated": true
  },
  "shared_cache": {
    "before_allocated_bytes": 10737418240,
    "after_allocated_bytes": 12884901888,
    "delta_allocated_bytes": 2147483648,
    "growth_explanation": "cache misses compiled by this wave"
  }
}
```

Measure allocated bytes, not logical apparent size. Set
`ownership_validated=true` only when all owned-output roots were declared and
sealed before execution and the measurement excludes shared, historical, and
unknown paths. Baseline/peak arithmetic must equal newly allocated bytes.
Cache observations must form one continuous lineage from receipt to receipt
and into `current_shared_cache`. Every positive cache delta needs a nonempty
explanation; zero or negative deltas may use `null`.

## Decision

For homogeneous valid evidence, the candidate is:

```text
max(receipt owned-output newly allocated bytes)
+ max(0, every receipt and current shared-cache delta)
+ safety margin bytes
```

The candidate is used only when it is lower than the requested estimate and
the request supplies a positive safety margin and leaves both the supplied
absolute and percentage floors intact. A zero margin retains the conservative
estimate with `SAFETY_MARGIN_NOT_POSITIVE`. The active cache's current size and
any negative delta receive no headroom credit. A successful result always reports
`shared_cache_headroom_credit_bytes=0`.

Evidence that is well-formed but non-homogeneous, incomplete, insufficient, or
unexplained returns `decision=conservative_retained`; launch remains allowed
only if the original estimate passes both floors. Invalid types, arithmetic,
schema, or required fields return `decision=blocked_malformed_evidence`. Any
estimate selected for launch that fails a floor returns
`decision=blocked_disk_floor`. All outcomes include stable `reason_codes`, and
the CLI exits `0` for ready advice, `75` for a disk-floor block, and `2` for
malformed evidence.

Treat the advice as a bounded input to the live gate, not as permanent proof:
rerun `disk_budget.py check` immediately before dispatch with
`effective_expected_growth_bytes` and the same floor values.
