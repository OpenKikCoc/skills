# Exact-SHA Benchmark Evidence

Use this contract only when the acceptance claim concerns performance. It does
not apply to ordinary L0 implementation or non-performance tests.

## Controller Protocol

1. Pin distinct full baseline and candidate Git object IDs before running.
2. Declare the controller checkout read-only. It may already be dirty; capture
   canonical status bytes before and after and require identical controller-
   computed hashes plus zero controller write operations. Do not clean it.
3. Give each SHA an isolated worktree with one owner, and give caches and
   other resources one lifecycle owner. Use distinct baseline
   and candidate worktrees and exactly one shared cache ID when a cache applies.
   This skill validates their receipts; it does not prescribe their mechanics.
4. In the actual shell used for each run, resolve `argv[0]` through that shell's
   PATH and prove the resolved absolute path is executable before the benchmark.
5. Declare every discovery root explicitly. Each root must be an absolute,
   non-glob path inside that SHA's isolated worktree; never search `/`, a home
   directory, the controller checkout, a sibling worktree, or an implicit CWD.
6. Send noisy progress and multiline diagnostics to a named text log channel.
   Send exactly one structured metric record to a distinct named JSON result
   channel. Never scrape a scalar from human logs.
7. Record controller-computed SHA-256 fingerprints for harness, parser, fixture,
   command, and metric semantics as executed at each SHA.

## Ordered Candidate Closure

For every measured role, prove this causal chain in order. Baseline and
candidate use separate chains; a candidate-only run still needs the candidate
chain.

1. **Requested:** bind the full source object ID, controller-computed artifact
   digest, target identity, and immutable request ID. A branch, tag, version
   label, or requested path is not artifact identity.
2. **Activated:** obtain an authoritative installer, manager, or runtime receipt
   for that exact source object ID and artifact digest. Bind a new activation
   generation or runtime epoch and the request receipt.
3. **Loaded:** after activation, query the process or runtime that will serve
   the benchmark. Bind its process/container identity, activation generation,
   loaded source object ID, and loaded artifact digest. Desired state, a file on
   disk, or command success does not prove what the runtime loaded.
4. **Measured:** bind measurement start and completion to the same loaded
   runtime identity, generation, source object ID, and artifact digest. Hash the
   loaded receipt into the machine result and reject an intervening restart,
   reload, or activation.

Link phases with controller-verified predecessor receipt hashes or an
authoritative monotonic generation. Wall-clock order alone is insufficient.
One subsystem may emit adjacent receipts only when it proves the distinct
phase facts. All four phases must resolve to the same immutable source object
ID and artifact digest; otherwise the result is unbound and cannot support
either a cross-SHA or candidate-only performance claim.

Keep these ordered receipts beside the schema-version-1 packet. The pure
validator checks packet consistency but cannot observe an installer or live
runtime, so validator success alone is not candidate closure.

Run the pure validator after evidence collection:

```bash
python3 scripts/benchmark_evidence_validator.py evidence.json
```

The validator does not invoke Git, discover files, execute a shell, or mutate a
workspace. The trusted controller owns those observations and passes evidence.

## Attribution Boundary

Label evidence `portable_cross_sha` only when the identical harness, parser,
fixture, command, and metric semantics executed successfully at both pinned
SHAs and both ordered artifact chains close. A numeric A/B delta is optional,
but when present it is recomputed from the two machine records.

If any semantic fingerprint differs, label the run `candidate_only`, set
`ab_delta` to `null`, and describe only the candidate's observed behavior. The
validator reports the differing semantic dimensions and rejects any A/B delta
or cross-SHA attribution. Rebuild a genuinely portable harness before making a
comparative claim.

## Packet Shape

The packet is strict schema version 1. Required performance fields are:

- `baseline_sha`, `candidate_sha`;
- `controller_checkout` with read-only declaration and status hashes;
- `worktrees.baseline` and `worktrees.candidate`;
- one conditional shared-cache declaration;
- `runs` with actual-shell executable preflight, bounded roots, separate log
  and result channels, semantic fingerprints, and zero exit status;
- `claim.attribution` and nullable `claim.ab_delta`.

For non-performance work, the entire valid packet is only:

```json
{"schema_version": 1, "performance_work": false}
```

See `scripts/test_benchmark_evidence_validator.py` for complete positive and
negative fixtures.
