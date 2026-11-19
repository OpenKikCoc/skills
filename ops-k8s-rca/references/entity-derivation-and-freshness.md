# Entity derivation, freshness and evidence grades

## Derive entities before asking

Pod, namespace, cluster, node, workload and time window come first from the
conversation, then from reads. Never ask for a Pod name while a read path
remains.

| Source | What it yields |
|---|---|
| user text, earlier ticket or alert cards, pasted links and log lines (query strings often carry cluster, namespace, name) | first-pass identity and window |
| events by `involvedObject` (name or UID) | Pod, node, controller |
| workload name to Pods | name prefix, labels, `ownerReferences` (use `scripts/owner_chain.py`) |
| group members of a gang or job | sibling Pods and nodes |
| API audit | objects already gone: who created, patched, deleted, when |
| federation or management cluster | which member cluster holds the object |

Normalize entity text first: strip list bullets, Markdown, link wrappers and
trailing punctuation (`- node-a1` is the node `node-a1`).

A historical incident is not answered by current state ("nothing Pending now");
read the history. One failing read path is not a stop: if the object API
returns auth failure or 5xx, pivot to another path for the same fact (audit,
node read, component logs, component record) and report which path failed.
When the exact Pod is gone, recover its nodes from audit, siblings and node names
given in the conversation. If every path fails, list the reads tried and their
results, give the best-supported layer, and only then ask. Ask the user only for
facts no system holds: business intent, a never-stated window, or which of
several identical workloads they mean.

## Freshness before diagnosis

Before reading a state as "now", align the clocks that bound it: node and Lease
heartbeat, Pod UID and deletion time, process start time, latest log timestamp.

- A stale, timed-out or empty read is `UNKNOWN` or `STALE`, never zero, never
  healthy, never a green dashboard cell. A faulted device that vanishes from a
  metric series is missing data, not zero capacity.
- A Ready node with an old heartbeat has an unknown current state; check the
  Lease, the process and log freshness first.
- A taint's `timeAdded` is when it was added, not proof of ongoing pressure; a
  taint that persists after the condition cleared needs the controller's next
  reconcile time and transition history before calling it stuck.
- Independent samplers (API object, agent self-report, `df`, `du`) are taken at
  different instants and with different accounting; keep each with its own
  `observedAt` and never merge them into one number.

## Evidence grades

| Grade | Meaning |
|---|---|
| OBSERVED | read from the live system for this incident, with source and time |
| EXTRACTED | direct fact from code, docs or a retrieval edge, with file and line |
| INFERRED | plausible link that still needs a check |
| STALE-RISK | cached graph, old revision, old wiki or old snapshot |
| UNKNOWN | missing, failed or uncovered |

A log-text match is inference about the cause, not an observation of it. An image
tag is not a git revision: resolve the digest or build metadata before treating
it as a source revision. A root cause needs OBSERVED facts plus verified source
behaviour.
