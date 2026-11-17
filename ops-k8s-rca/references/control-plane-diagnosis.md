# Control-plane component diagnosis

Use when the symptom points at a control-plane or cluster-level component
(scheduler, controller manager, a custom controller, an admission webhook
server) rather than at one workload. Terms are upstream Kubernetes; substitute
the component and namespace you actually run.

## 1. Identity gate: which component, which replica, which version

Before attributing anything to a component, prove all three:

- **Component**: the Pod/process that produced the log line or decision is the
  component you name (labels, container name, command, not just Deployment name).
- **Replica**: with several replicas only the leader acts. Names of replicas
  that merely exist are not evidence of who decided.
- **Version**: image tag and digest of the running container, and the source
  revision they map to.

```bash
kubectl -n <ns> get pods -l <selector> -o wide
kubectl -n <ns> get pod <pod> -o jsonpath='{range .status.containerStatuses[*]}{.name}{" "}{.image}{" "}{.imageID}{"\n"}{end}'
```

An image tag is a claim; `imageID` (digest) is the observation. Tags can be
re-pushed. Map digest to commit through your registry or build metadata, and
record the gap if you cannot.

## 2. Find the active leader (Lease)

Components using leader election hold a `coordination.k8s.io` Lease. List all
Leases first; do not assume a single well-known name (sharded or renamed
components exist).

```bash
kubectl get lease -A
kubectl -n <ns> get lease <name> -o jsonpath='{.spec.holderIdentity}{"|"}{.spec.renewTime}{"|"}{.spec.leaseDurationSeconds}{"|"}{.spec.leaseTransitions}{"\n"}'
```

Read:

- `holderIdentity`: usually `<pod-or-host-name>_<uuid>`; strip only the trailing
  suffix and match the result to a Pod name or node. Formats are
  component-specific; confirm against the component's election code.
- `renewTime`: compare to now (UTC). If older than `leaseDurationSeconds`, the
  holder is stale and nobody may currently lead (OBSERVED gap, not a guess).
- `leaseTransitions`: a count that keeps rising signals flapping leadership.
  Take two readings a few minutes apart.

Falsifier: if the named holder Pod has no log lines inside the window, the
Lease read was stale or mis-parsed; re-read before continuing.

## 3. Read every replica

The leader holds the decisions, but followers hold the history of earlier
leaders and election messages. For the window of interest:

```bash
for p in $(kubectl -n <ns> get pods -l <selector> -o name); do
  echo "== $p"; kubectl -n <ns> logs "$p" --all-containers --since-time=<UTC> --timestamps | grep -E '<signature>' | head -50
done
```

Record per replica: whether it ever held the lock in the window, first/last
timestamps, and the exact signature count. A signature seen on only one replica
is a statement about that replica. Absence in a replica that was not leading is
expected, not exculpatory.

## 4. Panic and restart forensics

```bash
kubectl -n <ns> get pods -l <selector> -o custom-columns=NAME:.metadata.name,RESTARTS:.status.containerStatuses[*].restartCount,NODE:.spec.nodeName
kubectl -n <ns> get pod <pod> -o jsonpath='{.status.containerStatuses[*].lastState}'
kubectl -n <ns> logs <pod> -c <container> --previous --timestamps | tail -200
kubectl -n <ns> get events --field-selector involvedObject.name=<pod> --sort-by=.lastTimestamp
```

- `lastState.terminated` gives reason (`Error`, `OOMKilled`), exit code and
  `finishedAt`; match `finishedAt` to the Lease `leaseTransitions` increment.
- `--previous` covers only the immediately prior container; earlier crashes are
  gone from the API. Say so rather than claiming "no earlier panic".
- Look for the first panic frame (goroutine trace top) and the object it was
  handling; a panic loop on one poison object differs from random crashes.
- Where node access is permitted (read-only), check kubelet and runtime logs
  and the container log files on the node, plus kernel OOM messages, for
  crashes older than the API retains. Mark node-level evidence as unavailable
  when you lack access.
- Restart count alone is not a cause: pair it with exit reason and time.

## 5. "Why can't this pod be scheduled"

1. Capture the Pod's `PodScheduled` condition and `FailedScheduling` events
   (exact message, count, first/last time). The event names the profile and
   counts nodes rejected per reason, e.g. `0/N nodes are available: ...`.
2. Map each reason to the filter or score plugin that emits it (taints and
   tolerations, node affinity, resource fit, pod topology spread, inter-pod
   affinity, volume binding, node ports). The mapping is a hypothesis until the
   source confirms it.
3. Check the Pod spec against live nodes for the named reason:
   ```bash
   kubectl get pod <pod> -o yaml
   kubectl describe node <node> | sed -n '/Taints/,/Events/p'
   kubectl get nodes --show-labels
   ```
4. If events are absent, the scheduler may never have seen the Pod. Check
   `spec.schedulerName` matches a running scheduler, then the leader's logs
   (sections 2-3). No event plus no log mention is "not attempted", a different
   branch from "attempted and rejected".
5. Read the plugin source at the deployed version (section 1). Do not cite
   upstream `master` for a pinned older build. Quote file, function and line
   at the revision you verified.

Prediction/falsifier example: if the cause is a taint, then removing the
node from the candidate set (in a dry analysis) or adding the toleration in a
copy of the spec should flip the per-node verdict; if every node still
rejects with the same reason count, the hypothesis is wrong.

## 6. Causality gate

Attribute a symptom to a component only when all hold:

- identity proven (section 1), replica was leader or handled the object;
- timeline fits: component misbehavior precedes or coincides with the first
  affected object, not after it;
- a discriminating check separates it from the next-best candidate
  (e.g. API server latency, node pressure, admission webhooks);
- no surviving contradiction (another replica or version behaved the same way
  while healthy).

Otherwise report `suspected` with the missing gate named. Pair with
[change-attribution.md](change-attribution.md) when a rollout of the component
itself is plausible.
