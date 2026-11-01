# Shell and External State Contract

Use this contract only for shell wrappers that compose the disk-budget CLI.
It does not replace a general shell style guide.

## Persistent State Root

Choose one persistent state root outside every worktree, cache, temporary, or
other disposable managed root. Establish it once in the current shell before
the first command, keep it for the full sequence, and create it before writing
state. Do not use `STATE_ROOT=... command`; that prefix does not establish
the variable for later commands.

Keep manifests, reservation status, handoffs, topology and environment
receipts, cleanup plans and receipts, and audit baselines beneath that root.
The disposable resource roots remain separate.

```zsh
STATE_ROOT="${STATE_ROOT:?set STATE_ROOT to a persistent external directory}"
MANAGED_ROOT="${MANAGED_ROOT:?set MANAGED_ROOT to the disposable resource root}"
SKILL_ROOT="${SKILL_ROOT:?set SKILL_ROOT to dev-disk-budget}"
PLAN_SHA256="${PLAN_SHA256:?set PLAN_SHA256 to the accepted plan hash}"
mkdir -p -- "${STATE_ROOT}"

task_ids=(core controller)
task_args=()
for task_id in "${task_ids[@]}"; do
  task_args+=(--task-id "${task_id}")
done

python3 "${SKILL_ROOT}/scripts/disk_budget.py" go-topology \
  --host-gocache "${MANAGED_ROOT}/go-build" \
  --wave-manifest "${STATE_ROOT}/wave-1.json" \
  --plan-sha256 "${PLAN_SHA256}" \
  "${task_args[@]}" \
  --output "${STATE_ROOT}/wave-1-go-topology.json"
```

Do not derive `STATE_ROOT` from `MANAGED_ROOT`. A cleanup plan protects its
own manifest, plan, and receipt paths, but an external state root also
preserves the wider workflow evidence across wave cleanup and recovery.

## zsh Collections and Names

In zsh, lowercase `path` is a special array tied to `PATH`. Never assign to
it or use it as a loop variable. Use names such as `resource_path`,
`receipt_file`, or `candidate_path`.

Represent repeated values as arrays and expand them with `"${items[@]}"`.
Do not put newline-delimited values in a scalar and later iterate over its
unquoted expansion. Optional zsh globs must handle no matches explicitly and
must be captured in an array:

```zsh
receipts=("${STATE_ROOT}"/wave-*-receipt.json(N))
for receipt_file in "${receipts[@]}"; do
  jq -e '.status == "completed"' "${receipt_file}"
done
```

Validate the skill's shell examples after editing them:

```bash
python3 <skill-root>/scripts/validate_shell_state_contract.py
```
