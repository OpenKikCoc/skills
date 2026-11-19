# Multi-Writer Contracts

All documents are UTF-8 JSON with `schema_version: 1`. SHA fields are full
40-character hexadecimal commit IDs. Paths are repository-relative POSIX paths.

## Plan Input

```json
{
  "schema_version": 1,
  "base_sha": "1111111111111111111111111111111111111111",
  "worker_substrate": "native-subagent",
  "tasks": [
    {
      "id": "core",
      "summary": "Implement the core behavior",
      "depends_on": [],
      "write_set": ["src/core/**", "tests/test_core.py"],
      "acceptance": [
        {
          "id": "unit",
          "kind": "command",
          "value": "python3 -m unittest tests.test_core"
        }
      ]
    },
    {
      "id": "docs",
      "summary": "Document the accepted behavior",
      "depends_on": ["core"],
      "write_set": ["docs/core.md"],
      "acceptance": [
        {
          "id": "content",
          "kind": "criterion",
          "value": "The document covers the new contract."
        }
      ]
    }
  ]
}
```

Task IDs and acceptance IDs use lowercase letters, digits, `.`, `_`, and `-`.
`worker_substrate` uses the same stable identifier syntax and binds dispatch to
one execution runtime. It is optional only so existing version-1 plan evidence
remains readable; new operational plans must set it before workers start. A
dispatch failure cannot infer or silently change this value.
Write-set entries are exact files or subtrees ending in `/**`. Other glob
syntax, absolute paths, `..`, backslashes, and overlapping ownership are
invalid.

Dependencies define dispatch and integration precedence. Every task remains
anchored to `base_sha`; `changed_files` must describe only the task's own
accepted commit. If a task must compile against a predecessor's source changes,
do not fake independence: combine the tasks or finish the predecessor and make
a new plan with a new base SHA.

Organize tasks around end-to-end acceptance slices, not component or activity
counts. A slice is the smallest independently verifiable outcome whose complete
acceptance contract can turn green, even when its implementation crosses
components. File ownership and individual validation commands are mechanics
inside that slice; they are not additional convergence units.

`plan` adds:

- `kind: "parallel-change-plan"`;
- the sealed `worker_substrate`, when supplied;
- normalized tasks;
- `parallel_stages`, where each stage is lexically sorted;
- `integration_order`, using stable topological order with lexical task-ID
  tie-breaking;
- explicit invariants.

Run the planner's `digest` action on the plan output to obtain a small receipt
with `base_sha` and `plan_sha256`. The digest identifies canonical normalized
plan semantics, not the bytes of a pretty-printed JSON file. Downstream
consumers recompute it and reject stale or tampered plan snapshots.

## Execution Input

Pass the exact plan output plus independently observed snapshots:

```json
{
  "schema_version": 1,
  "plan": {},
  "main_workspace": {
    "initial": {
      "head_sha": "1111111111111111111111111111111111111111",
      "clean": true,
      "changed_files": []
    },
    "current": {
      "head_sha": "1111111111111111111111111111111111111111",
      "clean": true,
      "changed_files": []
    }
  },
  "workers": [
    {
      "task_id": "core",
      "base_sha": "1111111111111111111111111111111111111111",
      "head_sha": "2222222222222222222222222222222222222222",
      "parent_sha": "1111111111111111111111111111111111111111",
      "merge_base_sha": "1111111111111111111111111111111111111111",
      "commit_shas": [
        "2222222222222222222222222222222222222222"
      ],
      "state": "completed",
      "clean": true,
      "changed_files": ["src/core/engine.py", "tests/test_core.py"],
      "acceptance_evidence": [
        {
          "id": "unit",
          "passed": true,
          "detail": "exit 0"
        }
      ],
      "preservation": null,
      "unique_artifacts": false
    }
  ]
}
```

Provide exactly one worker for every task. Allowed states are `completed`,
`interrupted`, and `failed`. Evidence IDs must exactly match the task's
acceptance IDs.

Each worker is a single-commit result: independently observe `parent_sha`,
`merge_base_sha`, and the ordered `commit_shas` range. Acceptance requires:

- `parent_sha == base_sha`;
- `merge_base_sha == base_sha`;
- `commit_shas == [head_sha]`.

This is a pure validator; the controller gathers those values with Git before
constructing the JSON. A SHA-shaped string without topology evidence is not an
accepted result.

`preservation` is either `null` or an independently observed proof:

```json
{
  "kind": "integrated",
  "locator": "3333333333333333333333333333333333333333",
  "contains_head": true,
  "observed_at": "2030-01-01T00:00:00Z"
}
```

For `integrated`, `locator` is the full controller commit proven to contain the
worker HEAD. For `archived`, it is a durable ref or archive locator. The
controller sets `contains_head` only after an independent reachability check.
An absent or false proof always blocks cleanup.

The main workspace is contaminated if it was initially dirty, its initial HEAD
did not equal the base SHA, its current HEAD changed, or its current snapshot is
dirty. Contradictory snapshots such as `clean: true` with changed files are
also invalid.

## Validation Output

`validate` emits:

```json
{
  "schema_version": 1,
  "kind": "parallel-change-validation",
  "valid": true,
  "base_sha": "...",
  "integration_order": ["core", "docs"],
  "main_workspace": {
    "clean_at_start": true,
    "contaminated": false,
    "initial_head_sha": "...",
    "current_head_sha": "...",
    "errors": []
  },
  "workers": [
    {
      "task_id": "core",
      "accepted": true,
      "errors": [],
      "cleanup_allowed": false,
      "cleanup_refusal_reasons": [
        "worker commit is neither integrated nor archived"
      ]
    }
  ],
  "errors": []
}
```

A worker is accepted only when it completed, stayed clean, has a single
non-base result commit whose parent and merge base equal the plan base, changed
at least one allowlisted file and no other file, and passed every required
acceptance item.

Cleanup is a separate decision. It is refused unless the worker is accepted,
clean, has a verified integration or archive preservation proof, and has no
unique artifacts. A boolean claim is not preservation evidence.

Even `cleanup_allowed: true` is not filesystem deletion authority. It proves
only that this handoff observed no orchestration-level blocker. A cleanup
executor must use a resource manifest sealed before workers started, map the
same task and commit, and revalidate live path identity, complete Git status
including ignored content, preservation reachability, and process activity.
`dev-disk-budget` supplies that companion lifecycle without changing
this version-1 handoff schema.

## Handoff Output

`handoff` always renders the schema, including blocked runs:

```json
{
  "schema_version": 1,
  "kind": "parallel-change-handoff",
  "status": "ready",
  "base_sha": "...",
  "integration_order": ["core", "docs"],
  "integration_steps": [
    {
      "sequence": 1,
      "task_id": "core",
      "commit_sha": "...",
      "parent_sha": "...",
      "merge_base_sha": "...",
      "commit_shas": ["..."],
      "depends_on": [],
      "action": "integrate"
    }
  ],
  "workers": [
    {
      "task_id": "core",
      "commit_sha": "...",
      "depends_on": [],
      "write_set": ["src/core/**", "tests/test_core.py"],
      "acceptance_contract": [
        {
          "id": "unit",
          "kind": "command",
          "value": "python3 -m unittest tests.test_core"
        }
      ],
      "changed_files": [],
      "acceptance_evidence": [],
      "accepted": true,
      "preservation": null,
      "cleanup": {
        "allowed": false,
        "reasons": []
      }
    }
  ],
  "main_workspace": {},
  "blockers": [],
  "next_actions": []
}
```

`status` is `ready` only when validation succeeds. Handoff generation does not
integrate commits or remove worktrees. The controller must recheck the real
workspace before each integration and cleanup action.

Create worktree/cache ownership evidence before dispatch, not from this
after-the-fact handoff. Historical or preexisting paths remain unmanaged even
when the worker result itself is accepted.

For multi-wave changes, keep lifecycle scopes separate: a session-level manifest owns shared caches, and a per-wave manifest owns that wave's worktrees and task temp roots. Finish and clean a wave once it is accepted and preserved, before dispatching the next large stage.

This is a resource-orchestration rule, not a worker-count limit. The handoff
schema remains unchanged.

## CLI Behavior

```bash
python3 scripts/parallel_merge.py plan|validate|handoff|digest --input in.json [--output out.json]
python3 scripts/worktree_context_attestation.py --input context.json
python3 scripts/worker_liveness_advisor.py --input worker-liveness.json
python3 scripts/restart_checkpoint.py checkpoint --input checkpoint.json
python3 scripts/restart_checkpoint.py resume-validate --input resume.json
```

Use `--output file.json` to write a file; otherwise output goes to stdout (`-`
for stdin input). Invalid or blocked documents return a non-zero status after
emitting structured JSON. The scripts never import or invoke Git, shells, agent
runtimes, or process-spawning APIs. Their outputs authorize neither filesystem
deletion nor production mutation.

The validators keep separate authorities:

- **plan/validate/handoff** decide structure, ownership, acceptance, and order;
- **context attestation** proves a supplied command snapshot belongs to one
  assigned worktree and path anchor;
- **worker liveness** distinguishes host status from task progress and admits at
  most one unchanged-intent replacement (see [worker-liveness.md](worker-liveness.md));
- **restart checkpoint** owns Git/resource recovery only: `cursor_owner` names
  `iteration-root-packet` and `recovery_entry` names the sole `resume_validate`
  entry for that checkpoint path. The root references its exact bytes and owns
  user outcome and foreground progression. Resume receipts carry canonical
  `checkpoint_sha256` and `cursor_owner`; only `resume_existing` without
  blockers completes root wave recovery. A fresh reconstruction requires a
  successor checkpoint (same base, plan and current gate) carrying
  `predecessor_checkpoint_sha256`, followed by fresh validation. Older
  checkpoints without these fields fail closed and are rebuilt, never upgraded;
  stale worker state is never completion. See
  [Restart recovery](multi-writer-protocol.md#restart-recovery).

### Worker Context Attestation Input

Use the exact flat top-level schema below. Do not invent nested `observed`,
`controller`, or `command` objects; the validator rejects unsupported fields.
The controller derives every digest and observation before giving the packet to
the pure validator.

```json
{
  "schema_version": 1,
  "task_id": "package-a",
  "plan_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "assigned_worktree_root": "/worktrees/package-a",
  "controller_worktree_root": "/workspace/controller",
  "other_worker_roots": ["/worktrees/package-b"],
  "observed_cwd": "/worktrees/package-a/subdir",
  "observed_git_top_level": "/worktrees/package-a",
  "expected_head_sha": "1111111111111111111111111111111111111111",
  "observed_head_sha": "1111111111111111111111111111111111111111",
  "write_set_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "observed_write_set_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "command_root": "/worktrees/package-a",
  "path_operands": [
    "pkg/api",
    "scripts/test_api.py"
  ]
}
```

Require `kind: worker-context-attestation`, `status: ready`, and no blockers
before the corresponding mutation or validation follow-up.
