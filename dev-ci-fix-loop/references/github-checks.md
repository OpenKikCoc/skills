# GitHub Checks Evidence

Use the GitHub CLI (`gh`) as the structured client. Confirm `gh <cmd> --help`
if an installed version differs. Save structured output to files outside the
skill and search saved logs locally instead of loading unbounded logs into
context.

## Query Order

Resolve the repository, PR, and current head SHA before judging checks:

```bash
gh pr view <pr> --json number,headRefName,headRefOid,statusCheckRollup \
  > pr.json
```

`headRefOid` is the expected SHA. `statusCheckRollup` is the PR aggregate of
check runs and commit statuses for the head commit.

List the required checks and their state:

```bash
gh pr checks <pr> --required \
  --json name,state,bucket,workflow,link,startedAt,completedAt \
  > required-checks.json
```

`bucket` collapses `state` into `pass`, `fail`, `pending`, `skipping`, or
`cancel`. For commit-scoped results (the preferred source), query the exact SHA:

```bash
gh api repos/<owner>/<repo>/commits/<sha>/check-runs > commit-check-runs.json
gh api repos/<owner>/<repo>/commits/<sha>/status > commit-status.json
```

Resolve the required-check policy independently of the observed runs, from
branch protection or rulesets:

```bash
gh api repos/<owner>/<repo>/branches/<base>/protection/required_status_checks
gh api repos/<owner>/<repo>/rules/branches/<base>
```

A required check that has not created a run yet is missing/unknown, not absent
from policy. Rules:

1. Prefer a non-empty commit-scoped result for the expected SHA.
2. When it is empty, use the PR aggregate only after confirming each check
   belongs to the expected SHA.
3. Treat a mismatched or missing SHA on a required check as stale/unknown, not
   green.
4. Keep required and optional checks separate. An optional failure is
   reportable but does not block a fresh required-green result.
5. Re-read `headRefOid` after every push and before the final green claim.

## Logs and Artifacts

For a GitHub Actions check, the run ID and job ID appear in the check `link`
(`.../actions/runs/<run-id>/job/<job-id>`). Prefer artifacts for test reports
and crash dumps, then the failed-step log, then the whole job log:

```bash
gh api repos/<owner>/<repo>/actions/runs/<run-id>/artifacts
gh run download <run-id> -n <artifact-name> -D <directory>

gh run view <run-id> --log-failed > failed.log
gh run view --job <job-id> --log > job.log
```

An expired or missing artifact means fall back to the log immediately. Do not
treat an artifact URL as usable evidence until download succeeds. For checks
from external CI providers, follow the check `link` to that provider.

Retain check name, run ID, job ID, failed step, SHA, and workflow name with the
diagnostic excerpt.

## GitLab

The same rules apply to GitLab MRs. Typical equivalents with `glab`:
`glab mr view <mr> -F json` (source SHA), `glab ci status`, `glab ci view`,
`glab ci trace <job-id>`, and
`glab api projects/:id/merge_requests/<mr>/pipelines`. Required checks come
from the project's merge-check settings.

## Offline Evaluator Packet

`scripts/evaluate_pipeline.py` does not read raw `gh` or `glab` output. Normalize
the results into this packet first:

```json
{
  "expected_sha": "abc123",
  "expected_required_checks": ["unit"],
  "commit_checks": [],
  "mr_checks": [
    {
      "id": "check-1",
      "name": "unit",
      "required": true,
      "sha": "abc123",
      "status": "completed",
      "conclusion": "failure",
      "artifacts": [
        {
          "name": "junit",
          "available": false
        }
      ],
      "steps": [
        {
          "id": "step-7",
          "name": "unit-test",
          "status": "completed",
          "conclusion": "failure",
          "log_available": true
        }
      ]
    }
  ]
}
```

### Input fields

- `expected_sha`: required non-empty string; the PR `headRefOid` / MR source
  SHA, not a branch name. Checks match it by exact equality.
- `expected_required_checks`: required array of distinct non-empty names from
  the independently resolved policy, never copied from the checks that
  happened to run. An empty array does not turn optional-only or empty
  results green.
- `commit_checks`: checks from the commit-scoped query; `mr_checks`: the PR/MR
  aggregate (`statusCheckRollup`). Both default to empty; leave
  `commit_checks` empty to fall back.
- Per check: `name` (non-empty), `required` (boolean), `sha`, `status`,
  `conclusion`. GitHub check runs map `status`/`conclusion` directly; commit
  statuses map `state` to `status`. Rollup entries carry no SHA, so set `sha`
  to the `headRefOid` read in the same query. A missing SHA is unknown.
  Optional `id`, `job_run_id`, `run_seq`, `artifacts[]` (`name`, `available`,
  `locator`), `steps[]` (`id`, `name`, `status`, `conclusion`,
  `log_available`), and `check_log_available`. Availability flags must
  reflect observed evidence.

`status` accepts queued, pending, waiting, running, in_progress, completed,
success, pass, failure, fail, cancelled, timed_out, and related spellings.
Completed checks use `conclusion` to distinguish success, failure,
neutral/skipped, and cancellation; unrecognized values evaluate as `unknown`.
Conflicting terminal `status` and `conclusion` make the packet invalid.

For a failed check the evaluator selects, in order: the first available
artifact, the first failed step with `log_available: true`, `check_log` when
`check_log_available: true`, otherwise `none`. This is advisory; prove the
chosen evidence can actually be retrieved.

### Invocation and output

Import `evaluate(packet)` for a pure in-process result (`EvidenceError` on
invalid input), or run the CLI with a file or `-` for JSON on stdin:

```bash
python3 <skill-root>/scripts/evaluate_pipeline.py evidence.json --compact
```

Both forms are offline and return the same result. The CLI prints JSON on
stdout, exits `0` iff `complete` is true, `2` for valid but unresolved
evidence, and `1` for invalid or unreadable input (`error: ...` on stderr).
Callers must check the exit code and parse the result. A missing evaluator, a
timeout, or an invocation failure is unknown, never PASS. Do not change the
fields or exit semantics without a versioned migration; sibling skills may
consume them.

Result keys: `expected_sha`, `query_source` (`commit` or `mr_aggregate`),
`state`, `complete`, `next_action`, `counts`, `expected_required_checks`,
`required`, `optional`, `stale`, `missing_sha`, `failures`, `summary`.
`state` is one of `failed`, `running`, `queued`, `unknown`, `stale`, `green`,
or `no_required_checks`; only `green` is complete. Each failure carries the
selected `evidence` (kind `artifact`, `step_log`, `check_log`, or `none`).
Optional failures appear in `summary.optional_failed` without blocking;
policy-required runs that never appeared are listed in
`summary.missing_expected_required` and block completion.

## Polling and Completion

- Use bounded polling with increasing intervals and a stated deadline
  (`gh pr checks <pr> --watch --required` is acceptable when bounded).
- Refresh immediately when the head SHA changes.
- Do not re-run or cancel workflows unless the user authorized that external
  action and evidence supports it.
- Completion requires fresh required green for the latest head SHA. Report
  optional failures, skipped checks, and infrastructure warnings separately.
