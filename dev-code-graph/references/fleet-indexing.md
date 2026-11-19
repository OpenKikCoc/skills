# Portable Fleet Indexing

Use `scripts/graphify-fleet fleet` to index many independent repositories
without hard-coded machine paths. The implementation uses only the Python
standard library plus the Graphify installation selected by the launcher.

## Workflow

1. Inventory repositories and inspect the generated manifest.
2. Run `build --dry-run` to verify selected targets, resolved paths, resume
   decisions, launcher, and Graphify arguments.
3. Build with a bounded `ThreadPoolExecutor` and an explicit disk floor.
4. Treat a zero-exit Graphify run as `partial` if its log reports failed
   semantic chunks.
5. Run `repair` to replay only failed token-packed chunks into separate repair
   fragments.
6. Merge target graphs and repair fragments into archival and compact graphs.
7. Run `audit`; retain `audit.json`, `summary.json`, `merge-audit.json`, and
   per-target status files with the graphs.

HTTP 429 and other transient-error retries belong to the backend adapter
(`graphify_fleet.py`), not the Fleet orchestrator. Its tests are the source of
truth for those network retries.

## Portable Inputs

Every Fleet subcommand accepts:

| Option | Meaning |
|---|---|
| `--root` | Fleet root; defaults to the current directory |
| `--output` | Output root; defaults to `<root>/.graphify-fleet` |
| `--launcher` | Executable Graphify launcher; defaults to the sibling `scripts/graphify-fleet` |
| `--detect-manifest` | Inventory manifest; defaults to `<output>/inventory.json` |
| `--exclude` | Additional Graphify exclusion; repeatable |
| `--no-default-excludes` | Disable built-in exclusions only after reviewing secret/cache risk |

Paths stored by inventory are relative to `--root`. Build and repair reject a
manifest entry that resolves outside that root. Target names are limited to
letters, digits, dot, dash, and underscore so they cannot escape the output
layout.

To use an existing detection manifest, provide it explicitly:

```bash
scripts/graphify-fleet fleet build \
  --root /path/to/fleet \
  --output /managed/graph-output \
  --detect-manifest /managed/manifests/fleet.json \
  --launcher /managed/bin/graphify-fleet \
  --dry-run
```

The manifest may be either a target list or an object with a `targets` list.
Each row needs:

```json
{
  "name": "repository-name",
  "path": "relative/path/from/root",
  "status": "success",
  "total_files": 120
}
```

Inventory adds word counts, per-kind counts, sensitive-skip counts, and
Graphify warnings. Empty or failed inventory targets are not built.

## Inventory and Dry Run

Without `--target`, inventory scans each immediate child directory under the
root. Select explicit roots with repeatable `[NAME=]PATH` values:

```bash
scripts/graphify-fleet fleet inventory \
  --root /path/to/fleet \
  --output /managed/graph-output \
  --target api=services/api \
  --target worker=services/worker \
  --parallel 4
```

Inventory calls Graphify detection only. It does not perform semantic
extraction and does not load credentials.

Always inspect a build plan before a large run:

```bash
scripts/graphify-fleet fleet build \
  --root /path/to/fleet \
  --output /managed/graph-output \
  --parallel 4 \
  --disk-floor-bytes 5368709120 \
  --dry-run
```

The plan includes each resolved target path, exact launcher command, and
whether the target would resume.

## Build Safety and Resume

`--parallel` is bounded to 32 and uses `ThreadPoolExecutor`; it does not use
`ProcessPoolExecutor` or platform semaphores. Each Graphify process defaults to
one AST worker and one semantic request:

```bash
scripts/graphify-fleet fleet build \
  --root /path/to/fleet \
  --output /managed/graph-output \
  --parallel 3 \
  --graphify-workers 1 \
  --graphify-concurrency 1 \
  --token-budget 12000 \
  --api-timeout 600 \
  --disk-floor-bytes 5368709120
```

The free-space check runs before every target launch. A target below the floor
gets status `blocked_low_disk`, return code `75`, and is not launched.

Status meanings:

| Status | Meaning |
|---|---|
| `success` | Exit zero, valid `graph.json`, and no failed semantic chunks in the log |
| `partial` | Valid graph exists, but the log reports one or more failed semantic chunks |
| `failed` | Nonzero exit, missing graph, or invalid graph JSON |
| `blocked_low_disk` | Free bytes were below `--disk-floor-bytes` before launch |
| `timed_out` | The per-target deadline expired; the owned process group was stopped |
| `cancelled` | The coordinator received SIGINT/SIGTERM while the target ran |
| `unknown_attempt` | A start receipt exists with no terminal receipt; see below |
| `repaired` | A partial target has a persisted repair fragment and repair audit |

Any status outside `success` and `repaired` is a failure for the exit code,
the summary, and `audit`.

### Per-target deadline

Every Graphify subprocess (build, and the repair worker) runs in its own
session with a deadline:

| Option | Default | Meaning |
|---|---|---|
| `--target-timeout` | 3600 | seconds per target, launcher startup included |
| `--kill-grace` | 2 | seconds between SIGTERM and SIGKILL |

On expiry the coordinator sends SIGTERM to the process group it created,
polls until the group is gone or the grace ends, then SIGKILLs that group only;
unrelated processes are never signalled. The target ends as `timed_out`
(return code 124). SIGINT/SIGTERM to the coordinator stops running targets the
same way and records `cancelled`. A deadline or cancellation is never followed
by an automatic relaunch in the same run; rerun the command deliberately.

Each phase writes receipts under `status/`:

- `<name>.<phase>-start.json`: operation id, process group id, start time,
  timeout, written before and updated after launch;
- `<name>.<phase>-terminal.json`: the same operation id plus outcome, return
  code, finish time, and elapsed seconds;
- `<name>.<phase>-graphify.json`: the Graphify compatibility receipt for that
  operation (see Compatibility guard).

A start receipt without a terminal receipt means the coordinator died and the
attempt's outcome is unknown: its process group may still be running. Build
refuses such a target (`unknown_attempt`) and `audit` reports it. Check that
nothing is left in the recorded group, then rerun with `--force`.

Only one coordinator may write an output root: `build` (except `--dry-run`) and
`repair` hold an exclusive lock on `<output>/.coordinator.lock` and fail fast
when another coordinator holds it. The lock is released when the process exits.

Build skips `success` and `repaired` targets when their required artifacts
still exist. It retries `partial`, `failed`, and blocked targets. Use `--force`
to rebuild completed targets. Every actual rebuild removes the previous repair
fragment and repair audit before launching so stale additions cannot contaminate
the new graph.

Successful builds remove runtime cache directories. Partial builds retain
cache for incremental retry; repair removes cache after persisting the
fragment.

## Failed-Chunk Repair

Graphify may print:

```text
[graphify] chunk 2/7 failed: ...
```

and still exit zero. Fleet records one-based failed chunk indexes in the
target status. Repair reruns only those chunks:

```bash
scripts/graphify-fleet fleet repair \
  --root /path/to/fleet \
  --output /managed/graph-output \
  repository-name
```

To override log discovery for one target:

```bash
scripts/graphify-fleet fleet repair \
  --root /path/to/fleet \
  --output /managed/graph-output \
  repository-name \
  --chunk 2 \
  --chunk 5
```

Repair uses the same `--token-budget` as the original build when reconstructing
chunks. Changing file contents, Graphify detection behavior, excludes, or the
token budget can change chunk boundaries; in that case rebuild instead of
replaying stale indexes.

Repair runs each target in a dedicated worker subprocess (the hidden
`fleet _repair-target` subcommand, started through the launcher) under the same
`--target-timeout` / `--kill-grace` deadline. After the worker exits zero the
coordinator verifies, bound to that operation id, the compatibility receipt,
the fragment, the repair audit, the target status, that every requested chunk is
covered, and that the audit's added node/edge/hyperedge counts equal the
fragment. Anything missing, stale, or inconsistent is a failure; exit zero alone
is never accepted.

Repair does not rewrite `graph.json`. It accumulates only unique additions
across repeated repair invocations:

```text
targets/<name>/graphify-out/repair-fragment.json
targets/<name>/graphify-out/repair-audit.json
```

Merge includes both the base graph and fragment. This preserves the original
graph and makes repair provenance auditable. Existing fragment nodes, edges,
hyperedges, token totals, chunk indexes, and repaired-file receipts remain in
the cumulative fragment/audit when another failed chunk is repaired.

## Merge and Compact Limit

Merge prefixes node and edge endpoints with `<repository>::` to prevent
cross-repository identifier collisions:

```bash
scripts/graphify-fleet fleet merge \
  --root /path/to/fleet \
  --output /managed/graph-output \
  --archive-out /managed/graphs/fleet-graph.json \
  --compact-out /managed/graphs/fleet-query-graph.json \
  --compact-limit-bytes 536870912
```

The archival graph retains complete node and edge objects. The compact graph
keeps identifiers, labels, source files, repository tags, relations, and
confidence while dropping large non-query metadata. The command succeeds only
when the compact file size is strictly less than the configured limit.

Both outputs use a temporary file plus `fsync` and publish only through atomic
`os.replace`. If the platform denies atomic replacement, Fleet fails closed and
keeps the old destination unchanged; it never truncates an existing graph for
a copy fallback. A failed compact-size check does not replace an existing valid
compact graph.

Archive output, compact output, and `merge-audit.json` must be distinct and
must not overwrite the inventory, status, base graph, repair fragment, or
repair audit. `merge-audit.json` records the selected target set and SHA-256
plus size of every input and both outputs.

## Output Layout

```text
<output>/
├── inventory.json
├── summary.json
├── repair-summary.json
├── merge-audit.json
├── audit.json
├── .coordinator.lock
├── fleet-graph.json
├── fleet-query-graph.json
├── logs/
│   └── <name>.log
├── status/
│   ├── <name>.json
│   ├── <name>.<phase>-start.json
│   ├── <name>.<phase>-terminal.json
│   └── <name>.<phase>-graphify.json
└── targets/
    └── <name>/
        └── graphify-out/
            ├── graph.json
            ├── repair-fragment.json
            └── repair-audit.json
```

Paths for custom archive, compact, or audit outputs may be outside this
layout, but should stay in a managed artifact directory.

## Audit

Run audit after merge:

```bash
scripts/graphify-fleet fleet audit \
  --root /path/to/fleet \
  --output /managed/graph-output \
  --compact-limit-bytes 536870912
```

Audit fails when:

- a selected target lacks status or `graph.json`;
- a target remains failed, partial, timed out, cancelled, unknown, or disk-blocked;
- a start receipt has no terminal receipt;
- failed chunk log entries have no repaired status;
- a repaired target lacks its fragment;
- the compact graph is absent or not strictly below its configured limit;
- `merge-audit.json` is missing or its target set, input paths/hashes/sizes,
  archival graph, compact path/hash/size, or configured limit differs from the
  current artifacts;
- Fleet output contains a copied `secrets/`, `cache/`, `.env`, or `.env.*`
  path.

The final JSON records target metrics, failed chunks, free space, compact
size, and every issue. Do not report a fleet as complete until audit passes.

## Validation

The portable test is:

```bash
python3 -I scripts/test_fleet_index.py
```

It uses a fake Graphify launcher and covers zero-exit failed chunks, resume,
low disk, cumulative repair fragments, force-rebuild cleanup, output-path
collisions, per-target deadlines and owned-group kill, start/terminal receipts,
cancellation, single-coordinator lock, repair result verification, stale compact rejection, compact limits, and fail-closed atomic
publication.
It performs no network calls and does not read real credentials.
