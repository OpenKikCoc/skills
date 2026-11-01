# External-Write Transaction Contract

Use this contract for a provider mutation whose transport or UI can lose the
result after the provider may already have committed it. The trusted
controller collects an ordered event ledger; the pure validator performs no
network or provider access.

## Protocol

1. Define one immutable JSON intent. Derive its SHA-256 with sorted keys and
   compact UTF-8 JSON, and assign a stable operation ID.
2. Record whether the provider supports idempotency keys, guarantees same-key
   replay for the same operation, permits retry after authoritative absence,
   and exposes an authoritative read. Do not infer these guarantees from a
   client wrapper.
3. Rotate both operation ID and retry key for a new logical operation. Reuse
   the operation ID, intent digest, and retry key for every retry of the same
   operation.
4. Freeze the canonical authoritative query and its SHA-256. Before the first
   write, perform that query through the declared authority locator and provider
   schema revision. Bind the observation to attempt zero. Zero matches permits
   one write; one exact match means it is already applied; conflicting or
   multiple matches block.
5. Record transport or UI ambiguity as `outcome_unknown`, never as failure.
   Perform an authoritative reconciliation read after the ambiguous attempt,
   bound to that attempt number, before deciding what follows. Cached, stale,
   schema-mismatched, or differently queried reads have no authority.
6. Accept one exact late success. Block conflicting or multiple matches. Retry
   only after authoritative absence and only when the provider explicitly
   guarantees retry-safe same-key replay.

Validate the complete ledger with:

```bash
python3 scripts/external_write_transaction.py transaction.json
```

The validator returns the derived state and allowed next action. It rejects
events after a terminal state and any ledger whose claim disagrees with the
derived state. See `scripts/test_external_write_transaction.py` for complete
packet fixtures.
