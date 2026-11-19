# Pending and not-running Pod drill-down

A map of layers and the evidence that separates them, for "stuck Pending",
"not running", "part of the replicas will not schedule". Not a fixed procedure:
pick the order the live evidence suggests.

## Start from the Pod, not the platform

1. Resolve any platform or dashboard link or ID to cluster, namespace and workload identity.
   If the platform API fails, list Pods cluster-side by name prefix, job ID or
   labels instead (see [entity-derivation-and-freshness.md](entity-derivation-and-freshness.md)).
2. Read the live Pod's **own events first** (`involvedObject.uid` or name), its
   annotations and conditions. Scheduler, volume controller and kubelet
   each write their decisive reason there.
3. Compare with a successful sibling of the same workload. Partial success
   routes first to per-node fit differences, but does not rule out accepted
   demand exceeding eligible physical supply.
4. If the Pod is gone, use siblings, owner events and component logs by name
   prefix to recover which nodes it landed on.

## Layer map

Classify the **last stage the exact Pod UID reached in the incident window**,
then drill into that layer. Scheduler explain output is a capture-time
simulation, not the actual attempt: a rejection from another window cannot
explain a later stuck episode. If scheduling already succeeded, follow binding,
volume and startup evidence instead of recycling an earlier filter rejection.

| Last stage reached | Typical signal | Drill into |
|---|---|---|
| Admission / quota / queue | Pod never created; quota-rejection events; queue states | quota object versus accepted demand; separate a pre-creation rejection from later physical-fit failures |
| Filter | `FailedScheduling`, `0/N nodes are available: ...` | per-reason predicate against node allocatable, topology objects, deployed scheduler version and gates |
| Assumed (scheduler cache), binding cycle pending: Permit/PreBind waiting | Pod assumed by the scheduler, no `spec.nodeName`, Permit or PreBind not finished | scheduler logs for the Pod across the window, Permit waiting state (gang), claim readiness |
| Bind failure: a PreBind or Bind plugin error event | event naming the failing plugin | the named plugin's resource on that node (free capacity of that resource) |
| Volume binding | `Failed to bind volumes: timed out`; PVC Pending with a selected-node annotation | PVC, StorageClass, provisioner, per-node driver registration, driver Pod readiness |
| Bound, not running | `ContainerCreating`, sandbox errors, probe errors | kubelet and runtime health on the node; [build pairing](mechanism-checks.md#c-kubelet-and-runtime-build-pairing) |
| Image pull | `ErrImagePull` / `ImagePullBackOff` with not-found | exact reference versus the cluster's registry mirror ([D](mechanism-checks.md#d-image-available-in-only-some-registries)) |
| Running but stuck, or fails on one node family | `Terminating` with processes alive; admission errors on some nodes | node kernel I/O ([E](mechanism-checks.md#e-node-kernel-io-hang)) |
| Running Pods evicted in a burst | many healthy nodes, one queue | pool or reservation shrink ([F](mechanism-checks.md#f-burst-eviction-when-a-node-pool-or-reservation-shrinks)) |
| Repeated start-kill-restart | restart loop; controller exit codes | which nodes replacements keep binding to and why they cannot run there |

## Filter-stage rules

- Do node arithmetic on the node's **allocatable of the exact extended resource
  name** the Pod requests, minus what is already allocated. A node can look fine
  on CPU and memory and have zero of an extended resource.
- **Request shapes depend on the deployed version.** Before concluding
  "insufficient", bind the deployed scheduler release and check it supports the
  shape (a device request shape the deployed scheduler does not support, e.g. a
  fractional or shared GPU extended resource). An unsupported shape fails the filter with an ordinary-looking
  message. Trace in order: entry predicate, request extraction, shape
  validation, per-device capacity. If the matched branch rejects before it
  consults free capacity, shortage and fragmentation do not explain it.
  Support in the default branch is not support in the deployed release; when a
  required predicate was not read, name that gap rather than assert the branch
  ran.
- Topology reasons (socket, NUMA, device topology) need the entry predicate and
  the per-device view: read the node's complete topology object, compare a
  rejected node family with a successful one for the same request shape.
  Aggregate allocatable does not settle a topology predicate.
- **Separate physical shortage from overselling.** Candidate nodes can lack the
  resource while an admission layer (quota, reservation or queue) accepted more demand
  than physical supply. Compare requested shape with eligible-node free
  capacity (including topology and reservations) in the same scope and window,
  using deterministic arithmetic; do not add incompatible device classes or
  infer a shortfall size from the word "insufficient". Attribute overselling to
  admission only when accepted demand versus physical supply is evidenced; free
  totals alone prove neither shortage nor overselling. If only later snapshots
  survive, state the historical gap.
- A failed typed read is not proof the object cannot be read: check the native
  tool's `--help`, then query by exact group, plural resource and name; inspect
  the envelope before projecting fields.

## Assumed but binding cycle pending

An assumed Pod already counts toward its assumed node in the scheduler cache, so
other Pods see that node as full; one orphaned assumed Pod can block a whole
gang (PodGroup via Coscheduling or Volcano). Read the scheduler log for the Pod
across the window: popped, waiting in Permit, rejected by a plugin, or never
received. A Pod waiting in Permit with no later re-queue is a liveness defect
candidate, not a capacity problem. With DRA ResourceClaims, read each claim's UID, allocation, reservedFor and
state, and use it as a stage witness: compare a successfully bound Pod and its
claim under the same scheduler release. Watch streams can arrive out of order, so
API timestamps do not prove informer arrival order.
