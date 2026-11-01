# State Contract

## Contents

1. Authority model
2. Control directory
3. Contract schema
4. State schema and invariants
5. Journal events
6. Goal liveness and compact continuation
7. Recovery and rearm
8. Command reference
9. Failure handling

## 1. Authority model

Keep intent and facts on separate precedence chains.

Intent precedence:

```text
current system/developer constraints
  > explicit new user instruction
  > confirmed contract projection
  > root plan/checkpoint
  > worker or fork recommendation
  > compact summary or memory
```

Fact precedence:

```text
fresh repo/runtime/API read
  > evidence bound to a still-current authority
  > worker receipt
  > plan
  > transcript, compact summary, or memory
```

The contract persists intent but never overrides a newer user instruction. A transcript can help navigation but cannot prove current runtime state. Bind evidence to the narrowest rereadable authority that proves its claim, such as a file hash, scoped Git tree/blob SHA, runtime identity, or API revision. Repository `HEAD` is intentionally coarse and will invalidate every same-kind evidence item bound to the previous HEAD.

## 2. Control directory

The script creates this directory under the selected workspace:

```text
.claude/long-session/
├── contract.json
├── state.json
├── journal.jsonl
└── .lock
```

`contract.json` and `state.json` are atomically replaced under an exclusive file lock. Journal entries are appended and fsynced under the same lock. The lock coordinates processes on one filesystem; it is not a distributed lease.

Decide explicitly whether the control directory is repository-tracked or local-only. Never add it to Git or `.gitignore` without user authorization. Redact secrets before persisting values.

## 3. Contract schema

Required top-level fields after initialization:

| Field | Contract |
| --- | --- |
| `schema_version` | Integer `1` |
| `session_id` | Non-empty opaque ID |
| `revision` | Positive integer |
| `objective` | User outcome, not an implementation checklist |
| `success_outcomes` | Non-empty string list preserving important design intent |
| `hard_constraints` | Objects with unique `id` and non-empty `text` |
| `non_goals` | String list |
| `milestones` | Non-empty ordered milestone list |
| `authority` | User/source reference plus resolved workspace root |
| `updated_at` | RFC3339 timestamp |

Each milestone contains a unique `id`, a `title`, a string-list `deliverables`, and a non-empty `acceptance` list. Each acceptance has a globally unique `id`, non-empty `text`, and Boolean `required`.

Do not mutate the contract through `checkpoint`. To change user intent, edit a candidate contract under root control, increment `revision`, preserve the same `session_id`, append a decision event naming the user authority, validate, and disarm until state is reconciled to the new revision.

## 4. State schema and invariants

Required fields:

| Field | Contract |
| --- | --- |
| `schema_version` | Integer `1` |
| `state_revision` | Positive CAS revision |
| `contract_revision` | Must equal the active contract revision before rearm |
| `session_status` | `ACTIVE`, `DISARMED`, `RECOVERING`, `WAITING_USER`, or `COMPLETE` |
| `armed` | Boolean; true only for `ACTIVE` |
| `root_session_id` | Root controller established by `init` |
| `armed_by` | Root ID while armed, otherwise null |
| `mode` | `DIRECT`, `ORCHESTRATE`, `REVIEW`, or `RECOVERY` |
| `epoch` | Non-negative integer; increments at every ordinary compact continuation and anomaly recovery |
| `foreground_milestone` | Exactly one contract milestone unless session complete |
| `milestone_status` | Status for every contract milestone |
| `next_action` | One non-empty action unless session complete |
| `suspension_stack` | Ordered saved parent milestones |
| `recovery_reason` | Non-empty while disarmed/recovering |
| `workspace_authority` | A rereadable `kind` and `value`, or null |

Milestone statuses are `PENDING`, `ACTIVE`, `REVIEW`, `PAUSED`, `BLOCKED`, `COMPLETE`, and `SUPERSEDED`. The foreground milestone must be `ACTIVE`, `REVIEW`, or `BLOCKED`. Every item in the suspension stack names a `PAUSED` milestone and stores its prior `next_action` plus a concrete `return_condition`.

Every `COMPLETE` milestone must have non-invalidated evidence covering all its required acceptance IDs before normal work can be armed. Evidence freshness is determined from the latest journal projection, not only from the original evidence object's `freshness` field. Recovery may temporarily invalidate evidence for an already completed milestone; that missing coverage is reported as a blocker while reads and replacement evidence remain allowed.

Allowed `checkpoint` patch fields are deliberately narrow:

```text
mode, foreground_milestone, milestone_status, next_action,
suspension_stack, session_status
```

The script owns `state_revision`, `armed`, `armed_by`, `epoch`, `contract_revision`, and timestamps. `checkpoint` cannot arm a session or enter recovery states.

## 5. Journal events

Each line is one JSON object. The script adds a monotonic `seq`, `event_id`, and timestamp. Supported user-provided event types are:

### `decision`

Required: `id`, `decision`, `authority`. Optional: `scope`, `reason`, `supersedes`. Use only when future interpretation changes.

### `evidence`

Required: `id`, `milestone_id`, non-empty `acceptance_ids`, `claim`, `source`, and `authority` with `kind`/`value`. Optional: artifact hash or locator. Acceptance IDs must belong to the named milestone.

### `evidence_invalidated`

Required: `evidence_id`, `reason`. Append this instead of changing history. A later evidence event with a new ID may restore coverage.

### `worker`

Required: `id`, `milestone_id`, `status`. Status is one of `registered`, `running`, `awaiting_review`, `reviewed`, `lost`, or `conflicted`. Include task/base authority/read scope/write scope on registration and a receipt reference when awaiting review. `lost` and `conflicted` block rearm until a later event resolves the worker.

### `continuation`, `recovery`, and `checkpoint`

The script emits these events. A `continuation` records a normal compact
boundary with an active host goal and unchanged authority. It advances the
epoch while keeping the session armed. `recovery` records an anomalous
reconciliation. `checkpoint` records a bounded execution transition. None
reproduces the transcript.

### Effects

Use `effect prepare` rather than generic `record` for an `effect_intent`. It writes and fsyncs a stable `call_id` before the external action. Use `effect finish` for exactly one terminal outcome: `confirmed`, `failed`, or `unknown`. An unclosed intent blocks rearm. `unknown` remains blocking until a later authoritative reconciliation records `confirmed` or `failed`.

## 6. Goal liveness and compact continuation

The control packet is an execution cursor, not a scheduler. When the host
provides a durable goal, its active state is the liveness authority:

```text
goal active + actionable next_action  => keep producing work turns
goal complete                         => terminal answer is allowed
goal blocked                          => wait for the stated external condition
```

Keep one goal for the whole objective across all milestones. Plan items and
milestones refine the work but do not independently decide whether the session
should stop. A process that only reads the control packet cannot make a host
schedule another turn after termination.

For an ordinary compact, `continue-after-compact` succeeds only when the caller
is the root, the host goal is explicitly `active`, the session remains
`ACTIVE` and armed, current authority exactly matches saved authority, and no
operational blocker exists. Under the packet lock it appends a continuation
event, increments `epoch`, and returns:

- objective and success outcomes;
- hard constraints;
- foreground milestone and its required acceptance contract;
- the one executable `next_action`;
- an explicit instruction to execute it now.

Success is not a stopping point. The caller must execute the returned action in
the same continued turn. Failure does not mutate state; classify the failure
and enter explicit recovery when appropriate.

## 7. Recovery and rearm

`disarm` moves the session to `DISARMED`, clears `armed_by`, sets `RECOVERY`
mode, and records a real anomaly reason. Ordinary compact alone is not such a
reason.

`recover` performs deterministic checks that a local script can prove:

- validates both JSON files and the journal;
- compares current authority with the saved authority;
- invalidates evidence bound to a changed authority;
- projects the latest worker statuses;
- detects unclosed or unknown effects;
- increments the epoch and emits a recovery receipt;
- remains `RECOVERING`.

The root agent must still reread user intent, inspect changed artifacts, classify new messages, and resolve reported blockers. The script cannot infer semantic goal changes.

`rearm` succeeds only when:

- the caller matches `root_session_id`;
- contract and state revisions agree;
- no unresolved or unknown effect exists;
- no worker remains `lost` or `conflicted`;
- the supplied current authority is explicit;
- the packet passes all invariants.

After rearm, use `assert-ready` immediately before a write boundary and execute
the recovered `next_action`. A recovery receipt or successful rearm is not a
user outcome. `assert-ready` is a deterministic guard, not identity
authentication; a malicious process can edit local files.

## 8. Command reference

All commands emit one JSON object to stdout. Contract violations return exit code `2`; unexpected I/O failures return `1`.

```bash
# Initialize
session_state.py --workspace <root> init \
  --contract-file contract-draft.json \
  --root-session-id <id> --next-action <action> \
  --authority-kind git --authority-value HEAD:<sha>

# Read-only checks
session_state.py --workspace <root> validate
session_state.py --workspace <root> status
session_state.py --workspace <root> assert-ready --actor <root-id>

# Append a validated decision/evidence/worker event
session_state.py --workspace <root> record --event-file event.json

# CAS state update
session_state.py --workspace <root> checkpoint \
  --expected-revision <n> --patch-file state-patch.json

# Recovery
session_state.py --workspace <root> disarm \
  --expected-revision <n> --reason <resume-or-real-anomaly>

# Normal compact fast path; execute returned next_action immediately
session_state.py --workspace <root> continue-after-compact \
  --expected-revision <n> --actor <root-id> \
  --goal-status active \
  --authority-kind git --authority-value HEAD:<sha>

session_state.py --workspace <root> recover \
  --expected-revision <n> --actor <root-id> \
  --authority-kind git --authority-value HEAD:<sha>
session_state.py --workspace <root> rearm \
  --expected-revision <n> --actor <root-id> \
  --authority-kind git --authority-value HEAD:<sha> \
  --instruction-classification CLARIFY \
  --checked-constraint C001 --checked-constraint C002

# Update intent; this disarms and requires recovery
session_state.py --workspace <root> contract-update \
  --expected-contract-revision <n> --expected-state-revision <n> \
  --actor <root-id> --contract-file revised-contract.json \
  --decision-file decision.json

# External side effect
session_state.py --workspace <root> effect prepare \
  --actor <root-id> --effect-id X001 \
  --operation <text> --authority-before <revision>
session_state.py --workspace <root> assert-ready \
  --actor <root-id> --effect-id X001
session_state.py --workspace <root> effect finish \
  --actor <root-id> --effect-id X001 \
  --status confirmed --authority-after <revision>
```

Use `--json-output <path>` only when another process needs an atomic receipt file; stdout remains the primary interface.

## 9. Failure handling

- If JSON is corrupt, stay disarmed and preserve the files. Build a candidate repair separately; never overwrite silently.
- If CAS fails, reread state and reapply the intended semantic change.
- If authority changes, replace stale evidence through new observations rather than relabeling old evidence as fresh.
- If a worker is lost, inspect its artifacts and record a resolving worker event before rearm.
- If an effect is unknown, query the target using its stable external identity or idempotency key. Do not infer failure from a missing receipt.
- If an ordinary compact continuation check fails, preserve state, identify the
  failed predicate, and use disarm/recover/rearm only for a real anomaly.
- If hooks are unavailable, run the same continuation or anomaly-recovery path
  manually.
- If multiple hosts need to write state, this file-lock protocol is insufficient; use a dedicated orchestrator.
