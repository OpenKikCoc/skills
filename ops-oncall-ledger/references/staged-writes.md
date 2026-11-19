# Staged writes and external-write vocabulary

Authorized reporting writes to places outside the ledger (a published RCA, a
weekly document). Map each staged write onto the vocabulary of
`dev-iteration-loop` (`references/external-write-transaction-contract.md`).

| Ledger step | Contract term |
|---|---|
| the rendered RCA or weekly body plus target and revision, hashed | **immutable intent** (one operation ID per intent; edit the record and re-render to get a new intent) |
| fetch the target before writing: existing copy, revision, identity headings | **authoritative pre-read** |
| timeout, missing or malformed response, or any ambiguity after a write was sent | **outcome_unknown** (never "failed", never retried blind) |
| re-read the exact target and compare with the intent | **reconcile** to `applied`, `absent` or `conflict` |
| the target is missing after an unknown outcome | `absent` is **not permission to replay**: confirm no concurrent writer, then issue a new, explicitly authorized attempt |
| two or more copies, or content that differs from the intent | `conflict`: preserve all copies, stop, report |
| read-back after the first write step | an **intermediate read-back is not final convergence**; the final read-back after the last step (for example after the weekly link is added) is the receipt |

Rules:

- A success receipt must be attributable to the request that produced it
  (operation ID or content digest), not to "something exists at the URL".
- Roll-up reads only records whose read-back receipt is final.
- Re-running on unchanged records stays `noop`; a reconcile that finds `applied`
  records the receipt and does not write again.

## Dependency receipt

Before the first call to an external CLI in a run, record its version, and run
its `--help` for every flag you are about to use; syntax comes from `--help`,
never from memory. If the tool's own guidance is missing, or a flag is not
listed, stop that step and report the dependency gap. Continue with the other
steps. Keep the receipt (tool, version, probed flags, time) with the run
artifacts, not in the ticket record.
