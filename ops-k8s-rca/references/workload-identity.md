# Workload identity resolution

Turn an entry point (console URL, dashboard link, alert label, ticket text, a
bare name or ID) into an evidence-bounded identity before running any query that
depends on it or any action that targets it. If the identity does not resolve,
stop and report; do not act on a best guess.

## 1. Classify the input

Names, UIDs, ticket numbers, deployment IDs and cluster IDs are different
namespaces. Join them only through an explicit edge (an annotation, label,
ownerReference or API field), never by string similarity. Record what the input
literally says (URL path, query keys) separately from what you infer.

## 2. Confirm the cluster first

```bash
kubectl config current-context
kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}{"\n"}'
kubectl get ns kube-system -o jsonpath='{.metadata.uid}{"\n"}'
```

The `kube-system` namespace UID is a stable cluster fingerprint. Confirm that
the context matches the cluster the input refers to, using something the
input carries (API server address, cluster ID label or annotation), not only
the context name.

## 3. Resolve object identity: namespace + UID

A name is unique only within kind and namespace, and is reused after delete and
recreate. Bind to `namespace/kind/name` plus `metadata.uid`, and re-read the UID
before any later step:

```bash
kubectl -n <ns> get <kind> <name> -o jsonpath='{.metadata.uid}{" "}{.metadata.generation}{" "}{.metadata.creationTimestamp}{"\n"}'
```

If the UID differs from one captured earlier, it is a new object and a new
episode.

## 4. Walk the owner chain

Pod -> ReplicaSet -> Deployment; Pod -> StatefulSet or DaemonSet; Pod -> Job ->
CronJob; Pod -> any custom controller resource. Use `metadata.ownerReferences`
(`kind`, `name`, `uid`, `controller: true`) at each hop and verify each owner
exists with that UID.

```bash
kubectl -n <ns> get pod <pod> -o jsonpath='{range .metadata.ownerReferences[*]}{.kind}{" "}{.name}{" "}{.uid}{" "}{.controller}{"\n"}{end}'
```

Offline helper for collected JSON: `scripts/owner_chain.py` (see the tool
section in SKILL.md). It flags missing owners, UID mismatches and cycles. A
dangling reference (owner absent) is a finding, not a gap to fill by name.

For a custom controller kind, read its CRD to learn what it generates, and
record that the downstream chain came from that definition (INFERRED until you
see generated children carrying the owner UID).

## 5. Labels versus selectors

Labels group objects; selectors match them. Mismatches cause "owner exists but
shows no instances":

```bash
kubectl -n <ns> get deploy <name> -o jsonpath='{.spec.selector.matchLabels}{"\n"}{.spec.template.metadata.labels}{"\n"}'
kubectl -n <ns> get pods -l <k=v,...> --show-labels
kubectl -n <ns> get svc <name> -o jsonpath='{.spec.selector}{"\n"}'; kubectl -n <ns> get endpointslices -l kubernetes.io/service-name=<name>
```

Use ownerReferences for "which objects belong to this controller" and selectors
for "which Pods does this Service or policy reach"; they can disagree. Never
identify Pods by label alone when another workload could share the labels.

## 6. Zero-instance workloads

Zero Pods can mean several different things; separate them:

| Case | Observation |
|---|---|
| scaled to zero | `spec.replicas: 0` (or an autoscaler minimum of 0); check manager and time |
| never scheduled or created | replicas > 0, ReplicaSet exists, desired > current; look at ReplicaSet conditions and events (quota, admission, `FailedCreate`) |
| pods exist but not ready | `status.availableReplicas` < desired |
| deleted and recreated | UID or `creationTimestamp` newer than the alert; old ReplicaSets/Pods gone |
| wrong namespace/cluster/context | object absent here, present elsewhere |
| selector mismatch | Pods exist with matching name but not owned |

A `NotFound`, an empty list or no log lines is evidence only for that exact
query. Say what was queried (cluster, namespace, kind, name, time).

## 7. Output: identity packet

Keep it small; each field has a value, evidence and a state.

| Field | Evidence |
|---|---|
| input as given | verbatim, with its type |
| cluster | context, server, `kube-system` UID |
| namespace | read from object or input |
| kind / name | the resolved object |
| UID, generation, creationTimestamp | `kubectl get` output, observation time |
| owner chain | one line per hop with UID |
| selector and sampled Pod names | the selector used and counts |
| instance state | replicas desired / current / ready |
| state | `resolved`, `ambiguous` or `unresolved` |
| open questions | what would resolve it |

Mark each field OBSERVED, INFERRED or unavailable (see
[mechanism-mapping.md](mechanism-mapping.md)).

## 8. Refuse when unresolved

Do not restart, scale, delete, patch, cordon or even query logs for an object
whose packet is `ambiguous` or `unresolved`. Ambiguity examples: the name
exists in several namespaces or clusters, multiple owners claim the Pod, the
UID changed during the investigation, the context does not match the input.
Report the candidates and the one check that would disambiguate, and ask.
