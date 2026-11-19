# Evidence and numbers

Read the generic checklist first (`dev-iteration-loop/references/evidence-calibration-checklist.md`);
this file is the planning-specific layer. Prefer repairing a comparison
(correct scope, basis, bound) over deleting every quantity; an unknown
mechanism may stay as an explicit plan hypothesis with its validation step.

## Three dispositions

- **sourced**: a directly supported current fact, mechanism, or attributed
  approved plan. Bind a file:line or reader-accessible link and its snapshot or
  revision.
- **plan-proposal**: an approach derived from sourced facts. Write it as a
  proposal ("this plan proposes ..."), with dependencies and validation. Never
  present an unapproved mechanism or precise parameter as existing behavior.
- **delete**: no adequate basis. Remove from prose and figures; record the
  ruling in TRIAGE, not a placeholder. Never invent a target the owner cannot
  set.

Personal notes, seed messages, and recollections guide research but are not
sources for a published plan unless the owner permits it; authorship alone is
not permission. Distinguish deployed, merged, planned rollout, and optional
enhancement; a pending optional enhancement is not a blocker for a delivered
capability.

## NUMBERS whitelist

Columns: value, used in (section), source (file:line or link), basis
(numerator, denominator, excluded parts), snapshot date. Each source must
really support the value. Keep separate: logical and physical units, point-in-
time allocation and time-series utilization, goals and completed work,
estimated upper bounds and feasible supply. Record conversion assumptions.

Before "more than", "covered", "enough":

1. Recompute numerator and denominator from the cited artifacts.
2. Subtract every excluded pool or scenario in the same sentence and figure.
3. Check window, unit, scope, conversion, and rounding.
4. Check the binding dimension, not just one resource total.
5. Give conservative and scenario bounds; headroom is not a guarantee.
6. Have a data-adversarial lane recompute; the coordinator re-runs it.

## Lint boundary

`plan_lint.py` flags a quantity absent from NUMBERS (comma, space, `~`, `about`
normalized; full-width digits folded; units never converted). Exempt without
being evidence-backed: numbers inside URLs and code, years, quarter and fiscal
labels, ISO dates, section and figure numbers, list markers. A passing lint
says nothing about whether a whitelisted number is right.

## Supporting datasets

Aggregates only; no personal or host identifiers in data, file names, or
metadata. Record sha256 for each artifact, units, exclusions, and snapshot
basis. A dataset published as a source needs verified readability by the
audience and a link from the plan. Local work continues while publication is
blocked; dependent claims publish only after their source section exists.
