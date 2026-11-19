# Mechanism checks

Mechanisms that investigations repeatedly miss although every deciding fact was
readable. Each section has the same shape: **signature**, **mechanism**,
**ordered reads**, **falsifiers**, **conclude**. They are hypotheses to test
against live objects; none is an answer for a new incident until the reads
agree. Conclude per [conclusion-contract.md](conclusion-contract.md).

Commands use plain `kubectl ... -o json` and `jq`. Placeholders: `<ns>`,
`<pod>`, `<node>`, `<ds>`.

## Contents

- [Version boundary check](#version-boundary-check)
- [A. Fixed upstream, not in the deployed release](#a-fixed-upstream-not-in-the-deployed-release)
- [B. Configured version versus running version](#b-configured-version-versus-running-version)
- [C. Kubelet and runtime build pairing](#c-kubelet-and-runtime-build-pairing)
- [D. Image available in only some registries](#d-image-available-in-only-some-registries)
- [E. Node kernel I/O hang](#e-node-kernel-io-hang)
- [F. Burst eviction when a node pool or reservation shrinks](#f-burst-eviction-when-a-node-pool-or-reservation-shrinks)
- [G. One resource published by two sources disagrees](#g-one-resource-published-by-two-sources-disagrees)

## Version boundary check

Kernel builds (`6.1.0-12`), agent images, kubelet builds and scheduler
releases compare by version order, not as strings or floats (`9` < `12`).

```bash
# Fleet values from Node objects (no node access needed)
kubectl get nodes -o json | jq -r '.items[] | [.metadata.name,
  .status.nodeInfo.kernelVersion, .status.nodeInfo.kubeletVersion,
  .status.nodeInfo.containerRuntimeVersion, .status.nodeInfo.osImage] | @tsv'

# true when $1 sorts strictly before the boundary $2
below() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$1" ]; }
below 6.1.0-9 6.1.0-12 && echo "below fixed release"
```

Group failing and healthy members by version. The mechanism predicts failing
members on the bad side and healthy ones on the other side (or lacking another
named condition). A failing member on the fixed side falsifies the boundary for
this incident. Quote where the boundary came from (release notes, the fix
commit's first tag, an owner). With no known boundary, the split itself is the
finding: "fails on `<v1>` (n nodes), runs on `<v2>` (m nodes)"; the fixed
release is a follow-up question for the owner.

## A. Fixed upstream, not in the deployed release

**Signature.** The leading mechanism is a defect in component code (a race, a
missing requeue, an unsupported request shape, a wrong default); the deployed
source shows the defective path; the component runs a pinned release older than
its upstream default branch.

**Mechanism.** Production runs a pinned build. Many defects found in the
deployed source are already fixed upstream. The decisive fact is containment:
fix commit `F` is in a branch or tag but is not an ancestor of deployed commit
`D`.

**Ordered reads.**

1. Establish `D` for the exact component from the live image or process
   version, not a config record alone. Map a version-with-short-sha to a commit
   with `git rev-parse <sha>^{commit}` or a tag with `git rev-list -n1 <tag>`.
2. Find candidates by mechanism, not symptom text:

   ```bash
   git fetch origin --tags
   git log --oneline D..origin/<default-branch> -- <file-with-the-defect>
   git log --oneline -i --grep '<mechanism keyword>' origin/<default-branch> -- <dir>
   git log --oneline -S '<identifier the fix adds or removes>' origin/<default-branch> -- <dir>
   git log --oneline -G '<regex of the fixed code>' origin/<default-branch> -- <dir>
   ```

   The first command lists every upstream change to the defective file that the
   deployed build lacks. Read `F`'s diff and check it changes the exact path the
   incident reached.
3. Containment:

   ```bash
   git merge-base --is-ancestor F D && echo contains || echo lacks
   git tag --contains F | sort -V | head -5
   ```

   Release branches may carry a cherry-pick under another SHA. Before concluding
   absence, search `D`'s history for the same change
   (`git log --oneline -i --grep '<F subject>' D -- <file>`) and compare
   `git show F | git patch-id` with each candidate.

**Falsifiers.** `F` or a patch-equivalent commit is an ancestor of `D`; `F`
changes a different path than the one the incident shows; `D` is not
established. Never compare against the default branch as if it were deployed.

**Conclude.** A source-proven mechanism plus object-state timing plus a
"lacks the fix" containment result is a conclusion even when a corroborating
log read failed. Report: fix `F` (subject, earliest tag) is not in deployed
`D`; recommend an upgrade to a release containing `F` and the supported
mitigation until then (owner decisions).

## B. Configured version versus running version

**Signature.** The answer depends on which version runs (feature support, a
defect, a default), and the evidence so far is desired state: a Helm value, a
manifest, a rollout ticket, a pinned checkout.

**Mechanism.** Desired state is not running state: paused or partial rollout,
canary, Pods not restarted after a config change, image override, or a
DaemonSet that updated only some nodes.

**Ordered reads.**

1. Live Pods of the component with image, image digest, version labels and
   start time:

   ```bash
   kubectl -n <ns> get pods -l <selector> -o json | jq -r '.items[] |
     [.metadata.name, .spec.nodeName, .status.startTime,
      ([.spec.containers[] | "\(.name)=\(.image)"] | join(" ")),
      ([.status.containerStatuses[]? | .imageID] | join(" "))] | @tsv'
   ```

2. Rollout state: template image versus Pod images; for a DaemonSet,
   `status.updatedNumberScheduled` against `status.desiredNumberScheduled`; for
   a Deployment, `status.updatedReplicas` against `spec.replicas`.
3. Process facts on the node when the image tag is not enough: process start
   time and full argv, mounted config.

**Falsifiers.** Every live Pod or process runs the configured version and
started after the last config change: no drift.

**Conclude.** Report "configured `X`, running `Y` on `n/m` Pods or nodes" and
base source reasoning (section A) on `Y`, per node when versions are mixed.
An image tag is not a git revision; resolve the digest or build metadata.

## C. Kubelet and runtime build pairing

**Signature.** Pods are bound but not Running, or Running with probe errors,
and users report "scheduling failed". Events such as runtime socket
`connection refused`, `ContainerGCFailed`, `ImageGCFailed`, `NodeNotReady`, on
some nodes but not others.

**Mechanism.** kubelet speaks CRI to the runtime and supports only the runtime
builds of its own node-image line. A cluster built or upgraded in pieces can
hold nodes whose kubelet and runtime come from different lines; the runtime
crashes or refuses CRI calls there. Rules: one node's kubelet and runtime share
a line; one `(kubeletVersion, containerRuntimeVersion)` tuple per node pool;
kubelet is never newer than the API server.

**Ordered reads.**

1. Group tuples (no node access needed), then list which tuple each failing
   node is in:

   ```bash
   kubectl get nodes -o json | jq -r '.items[] |
     [.status.nodeInfo.kubeletVersion, .status.nodeInfo.containerRuntimeVersion] | @tsv' \
     | sort | uniq -c
   ```

2. On one failing and one healthy node: installed package builds and runtime
   health (restart count, start time, journal panics or CRI errors).

**Falsifiers.** All nodes share one tuple, or failing nodes spread evenly
across tuples; a single node fails while the fleet is on one tuple.

**Conclude.** `high` when the failing set equals the mismatched tuple and the
runtime journal shows crashes or CRI errors; `medium` when the tuple split
matches and the node read failed. Remedy: rebuild the mismatched nodes on one
supported line (restarting the runtime only delays the next crash).

## D. Image available in only some registries

**Signature.** A job "stuck on resources" or "queued", yet its Pods exist, are
bound, and sit in `ContainerCreating`, `ErrImagePull` or `ImagePullBackOff`;
the pull error is `not found` or `manifest unknown`, not `unauthorized` and not
a timeout; the same template works in another region or cluster.

**Mechanism.** Each cluster or region pulls from its own registry mirror, and
derived or rebuilt images are published per mirror. A reference published only
elsewhere fails with not-found on every Pod, which dashboards show as waiting.

**Ordered reads.**

1. Pod events and container waiting state, with the exact reference (registry
   host, repository, tag) and digest if any.
2. Resolve the exact reference against each mirror's registry using the registry
   API (read-only); `404` here and `200` elsewhere is the discriminator. A `401`
   means a token is needed, not that the tag is missing; use the registry
   tooling you are authorised to use.
3. Contrast: Pods of the same repository in the same cluster or region and which tags they
   run; those tags exist there.

**Falsifiers.** The tag resolves in this cluster's registry; another Pod here runs
the same digest; the message is auth or timeout rather than not-found.

**Conclude.** `high` with a not-found pull error and absence here but presence
elsewhere; `medium` when the not-found error is confirmed and the per-mirror
lookup could not be made. Workaround: pin a version published to that mirror;
durable fix: publish the build there.

## E. Node kernel I/O hang

**Signature.** A Pod never reaches Running, keeps failing admission, times out
after placement, or will not terminate, but only on one node pool or hardware SKU while the same Pod runs elsewhere. The Pod may be
recycled already.

**Mechanism.** A filesystem journal thread blocks in uninterruptible sleep
(D state) because a block device stops completing I/O. Everything touching that
filesystem (kubelet cgroup, volume and sandbox setup, container teardown) blocks
behind it. Some kernel builds hang on particular storage devices; the fix lands in
a later build of the same base kernel, so the decisive fact is which side of
the fix boundary the node's kernel is on.

**Ordered reads.**

1. Recover the node set with the object's own history: events, audit, sibling
   Pods of the same job, sourced node names from the conversation. Bind Pod UID
   and historical node placement to the failure window.
2. Fleet discriminator from Node objects: group `kernelVersion` and condition
   types, compare with the failing set ([version boundary](#version-boundary-check)).
   Node objects describe now; recover fault-window versions when the failure
   predates a restart or upgrade.
3. A bounded read-only node read on two or three failing nodes and one healthy
   contrast node:

   ```bash
   uname -r
   ps -eo pid,stat,lstart,wchan:32,comm | awk '$2 ~ /D/'
   for p in $(pgrep jbd2); do echo "== $p"; head -5 /proc/$p/stack 2>/dev/null; done
   dmesg -T | grep -E 'blocked for more than|hung_task|jbd2' | tail -20
   lsblk -o NAME,MODEL,MOUNTPOINT; df -h
   ```

4. Kubelet admission errors from the node journal if admission failed.

**Falsifiers.** Complete fault-window evidence shows normal I/O and no D-state
journal threads on the affected node UID (a later normal read is not a
falsifier); the exact Pod UID was bound and failed after for another reason;
disk usage was normal (an I/O hang is not "disk full").

**Conclude.** `high` with D-state journal threads and journal-commit stacks on
failing nodes only, and the kernel in the known-bad range for that storage device.
`medium` when the node read failed but the failing family sits on the bad side
of the boundary, the healthy family on the fixed side, and the symptom matches.
Remedy: cordon and reinstall or upgrade to the fixed kernel; retrying deletes on
a hung node does not help.

## F. Burst eviction when a node pool or reservation shrinks

**Signature.** Many running Pods of one queue or node pool are evicted in a
burst (often SIGTERM), users suspect bad machines, but the nodes are healthy.

**Mechanism.** A controller shrinks a node pool or reservation (for example
cluster-autoscaler scale-down, a Kueue or Volcano-style queue, or a custom
operator) and removes nodes that still run Pods. If node selection does not
prefer idle nodes, the removed nodes' Pods are evicted. A shrink below the
allocated amount guarantees some running Pods lose their nodes.

**Ordered reads.**

1. Node labels, annotations and taints (which pool or reservation each node
   belonged to).
2. API audit for the node around the eviction time: who removed the marks or
   deleted the node, with what patch, when.
3. API audit for one evicted Pod: who issued the delete or eviction; compare
   times with step 2.
4. The controlling object (autoscaler status, queue or reservation): desired
   versus allocated in `status`. If the object no longer shows the change,
   read its update history from audit.
5. Repeat 2 and 3 on two or three other evicted nodes: the same order
   (desired size drops, then nodes are released, then Pods are deleted) on all
   of them.

**Falsifiers.** Desired size unchanged in the window; node marks unchanged;
evictions span many healthy nodes at once with no controller actor; node
conditions or health taints precede the evictions on a few nodes (bad
machines); preemption events name a preemptor.

**Conclude.** `high` with the ordered chain on several nodes and the actor
identified. Remedy: restore the pool size or change the scale-down or release
policy (prefer idle nodes); owned by the controller owners.

## G. One resource published by two sources disagrees

**Signature.** A scheduler message says a resource or pool was "not found" (or
insufficient) on nodes whose disks, devices or capacity look healthy; often
right after a scheduler rollout or a feature-gate change.

**Mechanism.** One resource is published by two sources (for example a node
label or annotation and a CRD, or device-plugin capacity and a DRA
ResourceSlice), and the consumer's feature gate or config selects one of them.
The publisher may write the old source in all versions but the new one only
from some release on. Enabling the gate while publishers are older makes every
request for that resource fail while the old source looks healthy.

**Ordered reads.**

1. Pod events: which message, which component.
2. Both sources for one failing node and one working node, projected with `jq`
   (look for the pool or device name in each; inspect the full envelope before
   projecting, since a summary omitting `spec` is not an empty object).
3. Effective gate or config on the consumer (and any other component that
   must agree with it): live argv and mounted config of the running process.
   Absence from argv means the default unless a config file sets it.
4. When the gate changed: rollout history compared with the first event.
5. Publisher versions on failing nodes ([version boundary](#version-boundary-check));
   group all nodes to size the blast radius.

**Falsifiers.** The selected source carries the resource for the node; the gate
is off; the message is a size shortfall rather than "not found" (separate
predicate: capacity, not a missing pool); both sources lack it (then the
publisher or configuration is the problem, not the source choice).

**Conclude.** `high` when the source split, the gate state and the publisher
version agree; `medium` when the split is shown and one of the other two reads
failed. Remedy: roll the gate back on all components that share it, or upgrade
publishers before re-enabling; then list other clusters with the gate on and
old publishers (latent).
