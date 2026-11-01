---
name: ops-k8s-rca
description: "Evidence-driven root-cause analysis for Kubernetes incidents using plain kubectl, optionally backed by a code/doc knowledge graph (e.g. dev-code-graph) or any retrieval MCP. Use for Pending/ContainerCreating backlogs, sandbox or startup failures, controller or rollout stalls, workloads with zero instances, or any case where live objects, events and logs must be reconciled with deployed source. Includes a Pod-startup snapshot classifier. Not for remediation, capacity planning, or incidents with no cluster access."
---

# Kubernetes Incident RCA

Build the diagnosis from live evidence first, use retrieval only to explain it,
and verify against the source that is actually deployed. Never let a favored
cause, a past case, or a graph neighbor stand in for an observation.

## Operating model

Keep three planes separate:

1. **State plane** - live objects, events, metrics, component logs, node files,
   versions, leases, change history. Collected with `kubectl` (read-only first).
2. **Knowledge plane** - a code/doc graph or retrieval service. Proposes
   mechanisms and code anchors; never observes the cluster.
3. **Verification plane** - the deployed source revision plus checks that
   distinguish competing mechanisms.

Compose specialists rather than copying their commands: use this skill for
orchestration and the evidence discipline, and bring in domain tooling (node
access, log backends, graph retrieval such as `dev-code-graph`) as needed.
If a plane is unavailable, continue with the others and record the gap.

## Experience is hypothesis routing, not truth

Past incidents, runbooks and casebooks may suggest competing hypotheses and
discriminating checks. They cannot confirm the current incident, authorize
remediation, or replace live evidence and deployed-source verification. A
single past case is an example, not a rule.

## Execution loop

1. **Frame.** Record cluster, namespace, workload, nodes, UTC window, impact and
   requested action. Separate diagnosis from remediation. Start a hypothesis
   ledger with several candidates, not one.
2. **Collect state.** Read-only `kubectl` first: `get`, `describe`, `events`,
   `logs --previous`, node conditions. Bound every query (namespace, field
   selector, time window). Run independent collectors in parallel; do not
   parallelize producer-consumer chains. For startup stalls use
   [pod-startup-audit.md](references/pod-startup-audit.md).
3. **Build an exact symptom signature.** Component/stage, exact error phrase,
   object type, time/version/topology, healthy versus unhealthy. When later
   reasoning depends on flags, feature gates or defaults, capture the effective
   runtime contract: live process start time, image digest, full argv, config
   actually mounted. Absence from argv is not a value; a template states intent,
   the live process proves consumption.
4. **Trace realization for "accepted but nothing running".** Walk the chain
   request -> controller object -> conditions/events -> generated
   ReplicaSet/Pod -> readiness -> projections. An empty dashboard or `0
   instances` is a symptom, not a stage. A `404`, empty list, 2xx or zero-hit log
   query is evidence only for that exact endpoint, identifier and window.
5. **Retrieve mechanism context** only after the signature exists; see
   [mechanism-mapping.md](references/mechanism-mapping.md).
6. **Verify and falsify.** For each candidate mechanism state the observation,
   the source fact, a prediction and a falsifier, then run the check that best
   separates two hypotheses. Confirm the deployed version first.
7. **Attribute changes** (rollout, config, node change) only when temporally
   plausible; do not force every incident into an upgrade narrative.
8. **Report** per [rca-deliverable.md](references/rca-deliverable.md). Publish a
   provisional brief at the time-box rather than waiting for every lane.

## Decision rules

- A confirmed root cause needs matching OBSERVED facts, verified source
  behavior, a satisfied prediction, and no surviving high-probability
  contradiction.
- Bind every claim to an episode: the exact object generation or rollout. A
  replacement object starts a new episode; old conclusions become historical.
- Current health does not disprove a historical failure; missing events may be
  retention, not absence.
- Do not mutate the cluster (restart, cordon, drain, delete, scale) without
  explicit authorization. Record the before-state for any approved action and
  verify recovery afterwards. Never delay a safe, authorized mitigation waiting
  for retrieval.
- Identifiers are typed. A deployment name, ticket number, UID and cluster ID
  are different namespaces even when strings look alike; do not join by name
  similarity.

## Bundled tool

`scripts/pod_startup_classify.py` classifies stuck Pods from files you already
collected (`kubectl get pods,events -A -o json`), offline and read-only:

```bash
python3 scripts/pod_startup_classify.py --first first.json --second second.json
python3 scripts/pod_startup_classify.py --second now.json --format json
```

Test: `python3 -I scripts/test_pod_startup_classify.py`.

## References

- [pod-startup-audit.md](references/pod-startup-audit.md) - snapshots, event
  correlation, classes, decision gates.
- [mechanism-mapping.md](references/mechanism-mapping.md) - using a retrieval
  graph, evidence labels, version verification.
- [rca-deliverable.md](references/rca-deliverable.md) - report structure and
  state vocabulary.
