# Mechanism mapping with a retrieval graph

A code/doc graph (for example one built with `dev-code-graph`, or any
retrieval MCP) is a **causal-context layer**, not an observability source. It
cannot know current Pod, lease or process state.

## Required order

1. **Collect live facts first.** Exact timestamps, namespace/pod/node identity,
   versions, events, full error strings, leases, relevant logs.
2. **Write the symptom signature.** Component nouns (`kube-scheduler`,
   `kubelet`), exact action or error text, resource kind, time/version/topology.
3. **Ask for a synthesis if the tool offers one,** then drop to raw passages,
   exact symbols and relationships only where a material claim needs them.
4. **Read the returned code in the matching revision.**
5. **Derive discriminating checks** and go back to live state.
6. **Conclude only when live facts satisfy the code-derived mechanism.**

Never let a graph neighbor, community co-membership or inferred edge become a
root cause without validation. Do not ask the graph "why is cluster X broken".

## Query design

- Start with 2-4 distinctive terms: an exact error string, a component name, a
  symbol. Do not paste an incident transcript.
- Generic words (`memory`, `status`, `bind`, `lease`) match vendored libraries
  and unrelated code; add a component or file qualifier.
- Resolve a promising label to an exact node (repo, file, line, type), then
  expand that anchor with a bounded depth (2, then 3) to see callers and
  references. Prefer extracted call/reference edges over inferred ones.
- If a label is ambiguous, qualify it rather than taking the first hit.
- Community or cluster membership suggests ownership and blast radius, never a
  dependency.
- Absence of an incident-specific component from the graph is a coverage
  finding, not a reason to guess.

## Evidence labels

| Label | Meaning |
|---|---|
| OBSERVED | live fact collected in this incident |
| EXTRACTED | direct code/doc fact or graph edge with file and line |
| INFERRED | plausible mechanism or edge that still needs a check |
| SYNTHESIZED | tool-generated summary; orientation only |
| STALE-RISK | cached graph, old revision or old wiki content |
| UNKNOWN | missing evidence or coverage gap |

A root cause needs OBSERVED facts plus verified source behavior. EXTRACTED
context can explain a fact; INFERRED context can only propose a check.

## Turn graph context into falsifiable checks

| Field | Content |
|---|---|
| Live fact | exact event/log/state observed now |
| Anchor | node, file:line, edge confidence |
| Mechanism | what the code says should happen |
| Prediction | a fact that must be present if this is the cause |
| Check | specific API, log, file or source query |
| Falsifier | result that would rule it out |
| Status | confirmed / rejected / unresolved |

Example: an error says volume binding failed. The graph shows the scheduler
plugin wraps provisioner errors. Prediction: the PVC events and provisioner or
CSI state, not the retry text, identify the cause. Check those; reject the
hypothesis if the PVC is bound and healthy.

## Verify source against what is deployed

Graph paths and line numbers are routing hints.

1. Identify the deployed component version or commit (image tag/digest, build
   info, `--version`).
2. Find the graph's pinned revision (source manifest, build metadata).
3. Open the source at the deployed revision, not default-branch HEAD.
4. Grep the exact log or error string; inspect the caller and state transitions.
5. Note any difference between graph revision and deployed revision.
6. If the graph covers only upstream Kubernetes but the cluster runs a fork or
   vendor patch set, verify the fork before concluding.

Fetch private source with the current user's own credentials; do not ask a
shared retrieval service to bypass repository access control.

## Availability

If retrieval is down, report the gap and continue with live state and local
source. A timeout is not evidence that the component is absent. An explicitly
supplied offline graph snapshot may be used if labeled `STALE-RISK`; never fall
back silently. Keep the retrieval path read-only: no ingestion or refresh calls
on shared services during an incident.

## Anti-patterns

- Querying before collecting exact logs and events.
- One broad traversal presented as the answer.
- Quoting graph line numbers without matching the deployed revision.
- Presenting community membership as a dependency or INFERRED edges as fact.
- Letting retrieval override contradictory live evidence.
- Dumping graph output into the report instead of predictions and checks.
