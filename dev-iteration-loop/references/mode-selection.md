# Mode Selection

## Contents

- Principle
- L0, L1, and L2 signals
- Escalation and de-escalation
- Examples
- Advisor packet

## Principle

The mode controls how much attribution evidence to require, not how much the
model is allowed to reason. Start with the lowest-cost loop that can safely
answer the question.

Do not turn typed artifact closure or composition validation into default
L0/L1 gates. Require them only when the acceptance claim is specifically about
the composed release artifact or its authoritative inputs.

Hard safety, production, credential, irreversible, and evidence-truth
boundaries remain mandatory in every mode.

## Signals

### L0

- clear goal and acceptance;
- local, reversible change;
- one control plane;
- deterministic focused test;
- no production, credential, or data mutation;
- no unresolved previous attempt.

### L1

- multiple plausible designs;
- partially clear goal;
- one earlier attempt did not settle the issue;
- costly rollback or validation;
- external API or protocol;
- baseline not yet pinned;
- multiple writers that need ownership.

Keep L1 lightweight. A short plan or a failing test is usually enough.

Heavy bugfix machinery remains a lightweight L1 admission question, not an L2
default. Before adding a helper state machine, shadow state, fallback, or an
unconditional full scan, review canonical lifecycle hooks, consider the
cheapest representation, and run one focused falsifier. If those checks do not
support the machinery, retain one source of truth and fix the lifecycle point.
The advisor emits this admission record only when a corresponding optional
`bugfix_*` signal is true; all ordinary defaults remain L0 and unburdened.

Provider contract uncertainty normally remains L1. Resolve it from
authoritative documentation for the exact provider version, then the
registered API or tool schema, then one minimal direct probe. Record whether a
field or capability is optional, explicitly `null`, deferred, or unsupported.
After the first mismatch, stop speculative compatibility patches until this
sequence discriminates the contract.

### L2

- repeated attempts plus unresolved uncertainty;
- three or more interacting control planes;
- stochastic oracle coupled to expensive validation;
- irreversible change or data migration;
- production behavior plus security or credentials;
- unclear goal spanning multiple control planes.

L2 needs a frozen baseline, a falsifiable hypothesis, bounded diagnostics, and
separation between deterministic proof and live conformance.

For an expensive stochastic live gate, declare one owner and freeze the
artifact, provider configuration, and acceptance claim. Run the gate once for
that tuple and persist its receipt. Post-install verification checks only
runtime facts: process identity, loaded artifact, active configuration, health,
and delivery. Reuse the receipt unless the validated surface changed, the
receipt mismatches, or deterministic runtime evidence falsifies the claim.

## Escalation

Escalate only when new evidence increases uncertainty or risk:

```text
L0 focused result diverges
  -> L1 name uncertainty and discriminating check
  -> L2 only after recurrence, ambiguity, or hard-risk expansion
```

Do not escalate merely because a task is large. A large mechanical migration
with a deterministic compiler/test oracle can remain L1.

An edit-placement guard also remains L0/L1. Use it only for repeated local
contexts or after a contextual patch lands under the wrong symbol: name the
target function/test, include one unique local invariant, then inspect the
scoped diff. It does not require a formal experiment ledger or whole-file
revalidation.

## De-escalation

After the first divergence is identified and reproduced deterministically,
return to L0 or L1 for implementation. Do not keep requiring a formal
experiment ledger for every follow-up edit.

## Examples

| Task | Mode | Reason |
|---|---|---|
| Rename a local API with compiler coverage | L0 | clear and deterministic |
| Choose between two cache ownership designs | L1 | architectural uncertainty |
| Second flaky fix involving prompt, provider, runtime, and live canary | L2 | repeated, stochastic, multi-plane |
| Three workers editing disjoint generated adapters | L1 | ownership matters; oracle is deterministic |
| Production credential rotation | L2 | production and security boundary |

## Advisor Packet

```json
{
  "schema_version": 1,
  "signals": {
    "goal_clarity": "clear",
    "reversibility": "easy",
    "oracle": "deterministic",
    "attempts": 0,
    "control_planes": 1,
    "production_effect": false,
    "security_or_credentials": false,
    "data_migration": false,
    "external_protocol": false,
    "expensive_validation": false,
    "parallel_writers": 1,
    "baseline_pinned": true,
    "failure_visibility": "clear"
  }
}
```

The script deliberately omits a numeric risk score. A score would imply a
precision the input signals do not support and can encourage threshold gaming.

For the conditional complexity check, set one or more of
`bugfix_helper_state_machine`, `bugfix_shadow_state`, `bugfix_fallback`, or
`bugfix_unconditional_full_scan`. Then record
`canonical_lifecycle_hooks_reviewed`, `cheapest_representation_reviewed`, and
`focused_falsifier_run`. Without any `bugfix_*` signal, the admission is a
no-op and does not change L0 routing.

Set `failure_visibility` to `opaque` only when the parent cannot distinguish a
child exit, launch failure, sandbox denial, transport error, or parse failure.
After at least two attempts, the advisor returns a bounded five-layer control
matrix. Keep it `clear` when direct stderr, exit status, or protocol error
already identifies the failing boundary.

For any mode, completion language must remain inside the oracle's tested
surface. List untested surfaces explicitly. Model-authored integrity metadata
does not become authoritative until the owning controller recomputes it from
canonical source bytes.

When repeated code appears to enforce one invariant mechanism, the optional
advisor convergence fields distinguish reuse from accidental duplication:

- set `invariant_mechanism_relation` to `same`, name exactly one
  `canonical_invariant_owner`, and choose `invariant_owner_strategy`;
- use `reuse_existing` without a removal count when the current owner can absorb
  the invariant, or `new_abstraction` only when it removes at least two duplicate
  owners;
- set the relation to `different` and keep separate owners when the mechanisms
  only look similar;
- leave the default `none` for ordinary work so the rule adds no admission tax.
