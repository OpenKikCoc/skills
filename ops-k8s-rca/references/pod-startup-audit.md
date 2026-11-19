# Pod startup audit

Use when many Pods sit in `Pending` / `ContainerCreating`, or Pods are bound to
a node but never become ready. Goal: split the backlog into causes without
promoting a symptom to a root cause.

## Startup stages (keep them distinct)

1. scheduler selects a node
2. the assignment is persisted (`spec.nodeName`)
3. kubelet observes and starts processing the Pod
4. the runtime creates the Pod sandbox
5. the network plugin provides an address
6. images are pulled, volumes mounted, containers start
7. probes pass and the Pod becomes Ready

An empty `status.hostIP` or `podIP` says the path is incomplete, not which stage
failed.

## Bounded collection

```bash
# snapshot 1, then a short gap, then snapshot 2 with events
kubectl get pods -A --field-selector status.phase=Pending -o json > first.json
sleep 20
kubectl get pods,events -A --field-selector status.phase=Pending -o json > second.json
```

If your kubectl rejects mixed resources with a field selector, fetch events in
a separate call and merge the `items` arrays. Prefer field selectors and a named
namespace over unbounded `-A -o json` on a large cluster. Pending Pods are few
relative to Running ones; do not dump every Running Pod.

For deep checks on survivors only:

```bash
kubectl describe pod -n NS NAME
kubectl get events -n NS --field-selector involvedObject.name=NAME
kubectl get node NODE -o json   # Ready, heartbeat, unschedulable, allocatable
```

## Classify

Run `scripts/pod_startup_classify.py --first first.json --second second.json`.
Classes and the evidence behind each:

| Class | Required observation | Next check |
|---|---|---|
| `scheduler_or_policy` | no `spec.nodeName`, `PodScheduled=False` | scheduling message: taints, affinity, quota, requests |
| `image` | waiting `ErrImagePull`/`ImagePullBackOff` | pull error text, registry reachability, credentials |
| `volume_mount` | `FailedMount`/`FailedAttachVolume` events | PVC/PV, attach state, CSI node plugin |
| `cni_ipam` | `FailedCreatePodSandBox` whose text names CNI/IPAM/address allocation | CNI daemon, IP pool, routes |
| `kubelet_cri_sandbox` | `FailedCreatePodSandBox` without CNI text (timeouts, name reserved) | runtime and kubelet logs, node I/O |
| `container_config` | `CreateContainerConfigError` / `CreateContainerError` | referenced Secret/ConfigMap/env |
| `app_runtime_or_probe` | crash loop or `Unhealthy` events | container logs; not a node-startup problem |
| `bound_no_signal` | bound, no IP, no events | kubelet watch/sync backlog; node health |
| `historical_orphan` | old, non-deleting, no retained events | owner/controller cleanup; report separately |
| `terminating_residue` | `deletionTimestamp` set | exclude from active impact |
| `too_young` | age below the stuck threshold | re-snapshot |

## Decision gates

Call a Pod a **persistent stall** only if all hold: the same UID is Pending in
both snapshots; its age exceeds the threshold; it is not deleting. Call a
**post-bind startup stall** additionally only if `spec.nodeName` is set and no
host or pod IP is present.

- Do not call it an IPAM failure from a missing IP alone; require an explicit
  CNI/IPAM message.
- Sandbox timeouts concentrated on one node are stronger than a global count;
  read `node_concentration` in the output.
- `context canceled` can come from workload deletion; it does not alone prove a
  runtime outage.
- Successful binding disproves "unscheduled" but does not prove kubelet success.
- A Ready node can still have a blocked kubelet or runtime.
- A large Pending count can be healthy churn (short-lived batch/serverless
  Pods); look at persistence and age distribution first.
- Current node health does not disprove an incident-window event.
- Missing events can be retention. Event TTL is often about an hour.

## Escalate

Go to node evidence only for candidates that survive the gates: node Ready and
heartbeat, pod density, comparison with healthy control nodes, kubelet and
runtime logs for the incident window. Restart, cordon, drain, delete and runtime
cleanup stay user-authorized.

## Report

UTC window and exact target coverage; per-cluster first/second Pending counts;
persistent counts by class; candidate age, node and events; node concentration;
historical orphans and unscheduled Pods in separate sections; access, timeout,
retention and namespace coverage gaps; next discriminating host checks.

See also [pending-pod-drilldown.md](pending-pod-drilldown.md) for the layer map keyed by the last stage the exact UID reached.
