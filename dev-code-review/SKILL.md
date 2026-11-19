---
name: dev-code-review
description: "Evidence-first review of a pull request, merge request, patch, or local diff. Binds exact source/target/base revisions, treats the target branch's behavior and extension points as the authority, requires every finding to carry a concrete trigger, code mechanism, and observable effect, reproduces risky behavior with the smallest test when cheap and safe, and writes short plain comments. Use for code review, regression or design-boundary audits, and for drafting or (only when explicitly authorized) posting review comments."
---

# Code Review, Evidence First

This skill owns the review reasoning and the wording. Compose other skills for
the mechanics:

- `dev-code-navigation` for definitions, callers, and references.
- `dev-ci-fix-loop` for `gh` / `glab` access to PR/MR data and comments.
- `fleet-dispatch` when the review runs as a reviewer role under a card; the
  reviewer verdict contract lives there.

## Review contract

- Bind the exact source, target, and base revisions before reviewing. Recheck
  the source revision immediately before posting anything.
- The target branch's behavior, its design contract, and its framework
  extension points are the authority. Never judge the diff in isolation.
- Every finding connects three things: a concrete trigger, the code mechanism,
  and the observable effect (user-visible or system-visible). Missing one of
  the three means it is not yet a finding.
- Reproduce with the smallest test or build when that is cheap and safe. A
  green CI result never overrides a reproduced invariant violation.
- A grep hit, or an empty reference-search result, is a lead and not a finding.
- Keep analysis separate from external writes. A review request authorizes
  inspection and a summary only. Posting comments needs explicit authorization.
- Never approve, reject, resolve threads, or change reviewers unless asked.

## Workflow

1. **Freeze the boundary.** Record the PR/MR number and version, the three
   revisions, and the checkout state. When the active checkout has unrelated
   or dirty work, review in an isolated worktree.
2. **Recover intent.** Read the description, linked design notes, existing
   discussion, changed tests, CI state, and the target-branch implementation
   the change plugs into.
3. **Trace semantics.** Follow definitions, callers, implementations, state
   transitions, rollback paths, and tests. Use a semantic index when one is
   available; otherwise use bounded text search and say once that it is the
   limitation.
4. **Challenge the change.** Concurrency, stale snapshots, lifecycle ordering,
   error isolation, rollback, compatibility, state reconstruction, ownership
   boundaries. Compare failure behavior with the target branch.
5. **Prove the important findings.** Write the smallest failing test. Temporary
   tests live in the review worktree and are labeled as review artifacts; they
   are never committed to the change under review.
6. **Rank internally.** Severity orders the output but stays out of inline
   comments unless the user or the repository convention asks for labels. A
   generic framework layer that depends on one concrete plugin or store is a
   blocking architecture defect when an extension point should own that
   behavior.
7. **Deliver.** Default: a chat summary, merge recommendation first, then only
   actionable findings by severity, then a short validation note.

## Posting (only when explicitly authorized)

1. Recheck that the source revision still equals the reviewed one; if it moved,
   re-review the delta first.
2. Publish only the comments the user approved, each bound to the current
   revision and the exact line.
3. Read each one back and verify position, content, and that it is not marked
   outdated.
4. Leave approval state, thread resolution, and reviewers untouched.

## Writing contract

State the failure in plain words first, then the cause.

- One short paragraph, usually one to three sentences.
- Shape: `This misses X, which causes Y when Z. Suggest W.`
- No filename or line number inside the comment (the review UI shows it), no
  `P1`/`Finding` prefix, no research diary, no praise filler.
- No vague wording such as "potential risk"; name the trigger and the effect.
- One architecture comment instead of repeating the same redesign on several
  lines. Prefer one concrete direction over a long remediation design.
- Match the user's language and tone.

Read [references/comment-style-cases.md](references/comment-style-cases.md)
before drafting comments for an external system.

## Guardrails

- Do not turn a style preference or a theoretical edge case into a defect
  without a contract or an observable consequence.
- Do not hide uncertainty: gather more evidence, or phrase it as a bounded
  question rather than an assertion.
- Do not edit production code during a review-only request.
- Do not duplicate platform command syntax or symbol-tool manuals here.
