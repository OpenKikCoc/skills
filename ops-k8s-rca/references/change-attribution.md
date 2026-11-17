# Change attribution

Answers "what changed, when, and who or what changed it" around an incident
window. Run it after a symptom signature exists; do not start from an assumed
upgrade. Read-only throughout.

## 1. Fix the window first

Record symptom onset (first bad event/log/metric, UTC) and the last known good
time. Search a window wider than that on both sides: planned changes start
earlier than their visible effect. Keep execution time and creation/plan time
as separate fields.

## 2. Collect change sources

| Source | Command or locator | Gives |
|---|---|---|
| Workload rollout history | `kubectl -n <ns> rollout history deploy/<name>` and `--revision=<n>` | revisions and pod template per revision |
| ReplicaSet revisions | `kubectl -n <ns> get rs -l <selector> -o custom-columns=NAME:.metadata.name,REV:.metadata.annotations.deployment\\.kubernetes\\.io/revision,CREATED:.metadata.creationTimestamp,REPL:.spec.replicas` | when each template appeared and which is live |
| StatefulSet / DaemonSet | `kubectl rollout history sts/<name>`; `kubectl get controllerrevisions -n <ns>` | template revisions |
| managedFields | `kubectl get <kind> <name> -o yaml --show-managed-fields` | per field-manager: operation, apiVersion, `time`, fields owned |
| Audit logs | API server audit backend, if you can read it: filter by `objectRef` (resource, namespace, name) and verbs `create/update/patch/delete` | authenticated user, user agent, source IP, timestamp |
| GitOps history | Argo CD: `argocd app history <app>`; Flux: `kubectl -n flux-system get kustomization,helmrelease -o yaml` (last applied revision) plus the Git log of the path | commit, author, sync time |
| Helm history | `helm -n <ns> history <release>`; `helm get manifest <release> --revision <n>` | revisions, status, chart/app version |
| Node, OS, component versions | `kubectl get nodes -o custom-columns=NAME:.metadata.name,KUBELET:.status.nodeInfo.kubeletVersion,OS:.status.nodeInfo.osImage,KERNEL:.status.nodeInfo.kernelVersion,RUNTIME:.status.nodeInfo.containerRuntimeVersion`; node `creationTimestamp` and Ready transition times | node replacement or upgrade waves |
| Cluster events | `kubectl get events -A --sort-by=.lastTimestamp` | short-lived; often already expired |

Config objects count: ConfigMaps/Secrets mounted by the workload, webhook
configurations, CRDs, RBAC, NetworkPolicies, admission policies. Check
`metadata.resourceVersion` ordering and managedFields times for each.

## 3. Read managedFields correctly

- `manager` names the client identity (for example a controller, `kubectl-*`,
  a GitOps agent, a Helm client), not necessarily a human.
- `time` is the last time that manager applied or updated those fields, so
  it can only show a later change, never an earlier one that was overwritten.
- Several managers owning different fields lets you assign each field to a
  writer. A field owned by a controller was probably defaulted or reconciled,
  not authored.
- The `operation` (`Apply` vs `Update`) separates declarative sync from
  imperative edits.

## 4. Correlate, then classify

Build one timeline: change time, rollout completion time (new ReplicaSet
fully available, node Ready), symptom onset. Then label each candidate:

| Label | Requirement |
|---|---|
| nearby | change time within the window, no further link |
| plausible | change precedes onset and touches the failing component or its inputs |
| caused | plausible plus a discriminating check: the symptom appears only on objects built from the new revision, and the mechanism is verified in source or reproduced; reverting in a safe scope (if authorized) or comparing a non-changed peer resolves it |

"Changed nearby" is not "caused". Default to `nearby` or `plausible`. Check
the negative control: objects, nodes or clusters that did not change and do not
show the symptom. If the change hit them too and they are healthy, the
hypothesis is weakened.

Also check order of effect: a rollout that started after the first failure may
be the response to it.

## 5. Who changed it

Prefer, in order: audit log user identity; GitOps commit author and sync
actor; Helm release metadata; managedFields manager. Report the identity chain
as found (for example "GitOps agent applied commit X authored by Y"), without
assuming the human behind a service account. If only a manager name is
available, say "manager <name>", not a person.

## 6. When history is truncated

- `rollout history` is bounded by `revisionHistoryLimit`; old ReplicaSets are
  garbage collected. Say "history starts at revision N".
- Events usually expire within about an hour; audit retention varies; log
  backends rotate. Record each source's earliest available timestamp so
  "no change found" can be scoped to what could be seen.
- managedFields keep only the latest time per manager.
- Fall back to indirect traces: object `creationTimestamp`, Pod start times,
  node boot time, image pull events, Git log, metrics step changes.
- Emit `unresolved` for the trigger lane (see
  [rca-deliverable.md](rca-deliverable.md)) rather than filling the gap with
  the likeliest candidate.
