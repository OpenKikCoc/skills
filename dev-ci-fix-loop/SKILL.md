---
name: dev-ci-fix-loop
description: "Drive an MR or PR through repeated CI diagnosis, minimal fixes, validation, commits, pushes, and fresh check observation until every required check for the current source SHA passes or a real blocker is proven. Use when a user expects ownership of a failing pipeline loop, such as GitHub PR checks or GitLab MR pipelines, their step logs, and artifacts."
---

# CI Fix Loop

## Core Rule

Own the loop until every required check is green for the current PR/MR source (head) SHA
or a concrete blocker is recorded. A push, stale green run, optional green run,
or plausible diagnosis is not completion.

## Loop

1. Authenticate the CI client (`gh auth status`, `glab auth status`) from the
   same host/HOME context that will run Git. A sandbox may see the CLI's
   stored status while still lacking push credentials. Run authentication,
   Git push, and the full pipeline loop where credentials actually work; use
   sandbox status only as a diagnostic probe.
2. Confirm repository, PR/MR, branch, current `HEAD`, PR/MR source SHA, dirty files,
   protected user changes, required local gates, and whether commit/push is in
   scope.
3. Pin the expected SHA. Do not diagnose or declare success from checks for a
   different revision.
4. Fetch structured commit-scoped checks first. If that query is empty, fetch
   the PR/MR aggregate and retain only checks whose SHA matches the expected SHA.
   Never replace a non-empty commit result with a more favorable aggregate.
5. Classify required and optional checks separately. Use the bundled offline
   evaluator when the result is complex:

   ```bash
   <skill-root>/scripts/evaluate_pipeline.py evidence.json
   ```

   It exits `0` only for fresh required green, `2` for an unresolved pipeline,
   and `1` for invalid evidence.
6. For each required failure, inspect structured check details, then prefer an
   available failure artifact. If no artifact is usable, fetch the failed step
   log; if that cannot be resolved, fetch the whole check-run log.
7. Diagnose from evidence before editing. Separate product defects, tests,
   pipeline configuration, infrastructure failures, and flaky behavior.
8. Make the smallest root-cause fix. Add or update a focused regression test
   for a product bug. Preserve unrelated dirty and untracked files.
9. Run repository-documented formatting and focused verification, then broader
   relevant gates. Do not hide a remote-only failure behind unrelated local
   green tests.
10. Commit precisely and push only when requested and authorized. Follow the
   repository's commit-message and trailer rules. Never bypass required checks
   merely to end the loop.
11. Read the PR/MR source SHA again, observe checks for that exact SHA, and repeat.
    Use bounded polling with progress updates for queued or running checks.
12. Before a delivery claim, assemble a source-SHA evidence epoch covering
    local gates, remote checks, composed/generated artifacts, and release
    receipts. Validate it offline:

    ```bash
    <skill-root>/scripts/delivery_evidence_epoch.py delivery-evidence.json
    ```

    Freeze the source under test as implementation revision `I`. Prefer one
    revision. A later evidence revision `E` is valid only when it binds the
    immutable plan, repository/checkout identity, and commands from `I`,
    preserves the source tree, changes only declared evidence paths, and
    carries reproducible input/output and authority-ledger digests.

## State Actions

- `queued`: wait and poll; absence of a run is not green.
- `running`: keep observing; diagnose any already failed required sibling.
- `failed`: collect the selected artifact or log and fix the proven cause.
- `stale`: refresh PR/MR source state and query checks for the expected SHA.
- `green`: finish only when all required checks are fresh; report optional
  failures or warnings separately.
- `unknown` or `no_required_checks`: treat as blocked evidence, not success.

## Evidence Record

For each diagnosis retain the PR/MR, expected SHA, check name/id, required flag,
status/conclusion, job run, failed step, pipeline version, and selected
artifact or log locator. Record local commands and results before each push.
For a delivery claim, read
[delivery-evidence-epoch.md](references/delivery-evidence-epoch.md) and retain
the validated normalized packet. A source-dependent receipt is usable only for
its named source SHA; source-independent reuse must be explicitly typed and
justified.

Read [github-checks.md](references/github-checks.md) for the GitHub query
order (`gh pr view --json headRefOid,statusCheckRollup`, `gh pr checks
--required`, `gh run view <id> --log-failed`), GitLab equivalents, the
normalized evaluator packet, and the artifact-to-log fallback.

## Safety

- Do not let tests invoke a real network, retry, cancel, commit, or push.
- Ask before new external side effects not already authorized by the task.
- Do not revert user changes, force-push, rewrite history, or include unrelated
  files without explicit approval.
- Report the exact blocker and manual command when credentials, permissions,
  infrastructure, or approval prevent the next step.
