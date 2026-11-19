# Roles, capabilities, and generations

## Why roles, not levels

The levels L0–L3 describe distance from the work (L0 is the machinery, L3 is
farthest away). They are convenient on a dashboard and dangerous as an
authorization key: a session that "is L2" in one document and "L1" in another
silently changes what it may do. Every check therefore uses the canonical role
and the capability token issued for one session generation.

## Authority matrix

| Action | platform | worker | reviewer | coordinator | supervisor | human |
|---|---|---|---|---|---|---|
| emit events, wake, relaunch on low context | ✔ | | | | | |
| edit/commit/build in bound worktree | | ✔ (after preflight) | | | | |
| write HANDOFF state/progress/evidence | | ✔ | | | | |
| record review verdict | | | ✔ | | | |
| dispatch / launch | | | | ✔ | ✔ (request) | ✔ |
| steer / resume / rework | | | | ✔ | ✔ | ✔ |
| escalate effort (new generation) | | | | ✔ | request only | ✔ |
| land, mark done | | | | ✔ | | ✔ |
| open a decision | | ✔ (via progress) | | ✔ | ✔ | |
| answer a decision | | | | | | ✔ |

"Request" means the supervisor writes a control request that the coordinator
executes on its next batch, so a single owner still holds every state change.

## Capability binding

`launch` issues one random capability token per generation and records
`(item, generation, role, runtime, model, effort, host, branch, worktree,
session_id)`. The token reaches the session through its environment
(`FLEET_ITEM`, `FLEET_ROLE`, `FLEET_CAPABILITY`). A check succeeds only when:

- the token matches the **current** generation of that item and role;
- the host, branch, and worktree match the registration;
- the target path resolves inside the worktree (no symlink escape).

A stale generation, a copied checkout, a wrong host, or a missing token is
denied, and the denial names the control action that would be permitted.

## Generations

A generation is one capability grant for one session turn sequence, bound to
one card, role, runtime, model, and effort. A new generation is created by:

- **resume** of an idle session (same session id, model, and effort; the
  capability token rotates, because only its hash is stored);
- **relaunch** after context exhaustion or a lost process (same effort);
- **escalation** to a higher effort tier (see effort-routing.md);
- **rework**, which resumes with the reviewer's requirements.

Each relaunch starts from the evolved HANDOFF, not from the original card
alone, and fences the previous generation: its token stops working even if the
old process is still alive.

**Context relay.** On a relaunch or escalation the launch prompt also carries a
bounded tail (at most 12,000 characters in total) of the previous generation's
log: the user and assistant turns and the tool calls, with secrets redacted
(bearer and basic credentials, `token`/`secret`/`password`/`api_key` assignments,
query-string secrets, provider key prefixes, private-key blocks, and long opaque
strings), plus `git diff --stat` against the base. If the log is missing the
relay is empty and the launch proceeds. The relay is a hint for orientation; the
HANDOFF stays the only authority, and a session must re-check anything it
relies on.

## Independence

Reviewer independence comes from a different session plus evidence binding,
not from a different vendor. A different vendor for the coordinator can reduce
shared blind spots; treat it as an option, not a requirement.

## Threat model

The checks stop drift and mistakes — a confused worker landing its own branch,
a stale session writing after it was replaced, a reviewer editing what it
reviews. They do not stop a hostile process running as the same OS user: such
a process can read the database or unset its environment. Sessions launched by
the fleet carry `FLEET_ROLE`, and the CLI refuses `--human` from them, but the
human escape hatch is still a local-shell convention. For hard isolation, run
workers in the runtime's sandbox (e.g. Codex `--sandbox workspace-write`) or as
a separate OS user, so they cannot write the fleet home at all; workers then
report state only through their HANDOFF, which the platform reads.

## Offline behaviour

- Supervisor offline: the fleet proceeds normally; the platform can send the
  human a digest instead.
- Coordinator offline or out of context: the platform relaunches it from its
  card and HANDOFF; a relaunch with an unknown result is fenced and reported,
  never retried.
- Human unavailable: `human_required` decisions take their declared default at
  the deadline only if the default was declared safe; otherwise they stay open
  and the item is blocked.
