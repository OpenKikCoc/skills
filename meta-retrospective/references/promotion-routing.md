# Promotion routing

Route a verified observation to the smallest target that prevents recurrence.

| Observation | Target |
|---|---|
| Run-local lesson, or not yet verified | Draft only |
| Repository-specific invariant | That repository's guidance file |
| Gap in a reusable workflow | Update the owning skill |
| Narrow domain mechanism | Existing casebook or reference file |
| Durable fact about the user or environment | Memory entry |
| Recurring cross-project workflow | New skill candidate |

Do not create a skill because one failure was memorable.

## Update an existing skill when

- its trigger already covers the task;
- the new discriminator changes a decision inside that workflow;
- a safety boundary or deterministic behavior can be tested;
- the addition does not turn the skill into an incident chronology.

## Merge into a casebook when

- the mechanism belongs to one domain and a specialist consumes it as
  hypothesis context;
- it has a distinctive signature, a falsifier, a guard against unsafe
  inference, and a recovery boundary;
- it is not yet a rule for the main workflow.

Fold new evidence into an existing case when trigger, mechanism,
discriminator, and falsifier are equivalent.

## Propose a new skill only when

- the workflow generalizes across projects;
- inputs, decision, output, and safety boundaries are stable;
- a distinctive trigger changes agent behavior;
- positive and negative test fixtures are feasible;
- there are two independent occurrences, or the user explicitly asks;
- no existing skill can own it without losing coherence.

## Generalization check before any edit

Replace rather than append: parameterize existing text instead of adding a
host-, ticket-, version-, or incident-specific path. If the rule repeats
deterministically, put it in a script with a positive and a negative test.

## Reviewing the inbox

At a natural foreground checkpoint: list drafts, merge duplicates in meaning,
retire contradicted ones, and record decisions. Record `promoted` only after
the change exists and its checks passed; until then record `deferred`.
