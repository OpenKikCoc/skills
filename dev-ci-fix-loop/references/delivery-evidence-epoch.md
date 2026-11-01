# Delivery Evidence Epoch

Use one normalized packet when the delivery claim extends beyond CI status to
local validation, remote checks, composed or generated outputs, and release
publication. The validator is pure: it checks consistency and provenance but
does not execute commands, inspect Git, or contact a remote service.

## Revision Model

- `implementation_revision` is the exact source revision `I` measured by every
  source-dependent receipt.
- `current_source_sha` equals `evidence_revision`: normally `I`, or a later
  commit `E` that contains evidence only.
- For `E != I`, prove that `I` is an ancestor, both revisions have the same
  source-tree digest, and every `I..E` path is allowed by the immutable plan's
  `evidence_paths`. Any source change invalidates the epoch.
- Establish the plan at `I`. Bind its canonical digest, its command-list
  digest, and separate environment and sample-policy digests. Each receipt or
  revision proof must name exactly one command from that plan.
- Freeze a canonical repository identifier and checkout fingerprint in that
  plan. The Git proof must repeat both exactly and bind their canonical digest
  as its `repository-identity` input; self-consistent proof hashes from another
  repository or checkout are still invalid. Its target and profile must also
  equal the selected immutable Git command.

Never combine measurements from different source revisions. Do not reinterpret
a stale receipt as current merely because the check name, target, artifact, or
branch matches. A source-independent receipt may be reused only with
`source_dependency: source_independent`, `source_sha: null`,
`reuse_class: source_independent`, and a concrete `independence_basis`. It does
not replace a source-dependent receipt in any delivery lane.

## Required Matrix and Receipt Binding

Populate all four matrix lanes: `local_gates`, `remote_checks`,
`composition_generated_artifacts`, and `release_receipts`. Each lane needs at
least one passing source-dependent receipt. Every receipt binds:

- source dependency and source SHA;
- the digest of its normalized `input_hashes`;
- target and profile;
- one bounded claim and reuse class;
- an authority kind/locator and immutable plan command;
- named input and output SHA-256 hashes plus pass state.

The command's authority, target, and profile must equal the receipt. Use
authority kinds `local_runner`, `remote_ci`, `artifact_generator`, and
`release_authority` for their corresponding lanes. Represent commands as argv
arrays so the plan digest does not depend on shell parsing.

## Evidence-Only Revision Proof

For a later `E`, use a `git` authority command and record the `I..E` diff
digest, normalized changed paths, ancestry result, and source-tree digests at
both revisions. Bind the two source-tree digests as named proof inputs and the
diff digest as the named `diff` output. Record evidence artifacts under paths
declared by `evidence_paths`; an empty evidence-only diff is invalid when
`E != I`.

The packet also carries aggregate input and output digests. Its authority
ledger is derived deterministically from normalized receipts and the Git proof,
not supplied as an unrelated narrative. The validator recomputes every digest,
sorts unordered receipt/hash collections, and rejects a non-reproducible
ledger. Run it before a final delivery claim:

```bash
python3 scripts/delivery_evidence_epoch.py delivery-evidence.json
```

Exit `0` means the packet is internally ready, `2` means it is well-formed but
blocked, and `1` means the schema is invalid. Review `blockers` and preserve the
normalized output; internal readiness does not substitute for independently
checking that each authority locator and recorded hash came from the claimed
system.
