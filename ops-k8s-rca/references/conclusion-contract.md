# Conclusion contract

How an RCA answer concludes, final or provisional. It applies to every answer
that names a root cause. It does not lower the evidence bar of any mechanism;
it stops the answer from hedging after that bar is met.

## Contents

- [1. Evidence binding](#1-evidence-binding)
- [2. Mechanism match: state the root cause](#2-mechanism-match-state-the-root-cause)
- [3. Excluded hypotheses](#3-excluded-hypotheses)
- [4. Gaps are follow-ups](#4-gaps-are-follow-ups)
- [5. "Cannot confirm" needs no mechanism match](#5-cannot-confirm-needs-no-mechanism-match)
- [6. Version boundaries](#6-version-boundaries)
- [7. Interim conclusions](#7-interim-conclusions)
- [Answer shape](#answer-shape)
- [Self-check](#self-check)

## 1. Evidence binding

Bind every fact to the exact object UID (and the related Node, claim, volume
or component instance identity), the incident window **with timezone**, and the
lifecycle phase. Keep observation time distinct from event time. A reused name,
a replacement UID, a later rollout or a different process start time is a
different episode. If the UID or historical placement cannot be recovered,
label the binding unknown; never join by name similarity or promote a control
object into target proof.

- **Current state never refutes past state.** Current Ready, idle threads, free
  disk or a fixed version cannot exclude a failure that happened before the
  recovery, restart or upgrade. Recover fault-window logs, events, audit and
  placement history before excluding a historical mechanism.
- **Controls stay separate.** Prefer same-batch healthy and unhealthy controls
  matched by time, release, node family and request shape. Record the
  differences. A control is context, not proof about the affected object.
- **Scheduler explain or simulation output is capture-time only.** A simulated
  rejection from another window or phase cannot explain a later stall. If the
  affected UID already passed scheduling, investigate its actual binding,
  storage or startup phase; an earlier filter result is not a falsifier.
- **Missing or truncated logs are coverage gaps**, not evidence of success or
  of "no error". Say which window or component is uncovered.

## 2. Mechanism match: state the root cause

A mechanism match exists when live evidence from the affected objects satisfies
the decisive discriminator of a known mechanism: a section of
[mechanism-checks.md](mechanism-checks.md), a hypothesis prediction, or a code
path proven in the deployed source. A past-incident note or runbook entry alone is not a
match; the discriminator observed on this incident's objects is.

When the deployed source (the commit the component actually runs) shows the
defective or limiting path **and** the live objects show the state that path
predicts (object order and timing, the event it emits, the request shape it
rejects), conclude on that mechanism. Do not call it "candidate" or "suspected";
a missing corroborating log is a follow-up.

Open the answer with a root-cause statement, not a candidate:

- **Root cause:** the mechanism, named at the layer where it acts (node kernel,
  runtime pairing, image distribution, resource publication, scheduler binding cycle) and its
  scope (one node family, one cluster, all clusters). A symptom such as
  "resources insufficient", "Pod not ready" or "scheduling failed" is not a
  root cause.
- **Confidence:**
  - `high`: the decisive discriminator is observed on the affected objects and
    every competing hypothesis that was checked is falsified.
  - `medium`: the signature and at least one discriminating fact match, one
    decisive read failed or was unavailable, and nothing contradicts the
    mechanism. A symptom-only match is a leading hypothesis (section 5), not a
    `medium` root cause.
- **Evidence chain:** ordered cause to effect, each link a fact with its source
  (command and object, log line, source file and line at the deployed commit,
  version value).
- **Remedy:** the mitigation the mechanism implies, its owner, and the recovery
  evidence that would show it worked. Production changes still need the owner's
  or user's authorization.

Once the discriminator holds, words like "possibly", "suspected", "to be
confirmed" do not belong in the root-cause line. Residual uncertainty goes into
the confidence level and the follow-ups.

## 3. Excluded hypotheses

List every hypothesis a read falsified under **Excluded**, each with the one
fact that excluded it. Do not carry a falsified hypothesis forward as an
alternative next to the root cause. A hypothesis never checked is neither
excluded nor alive: name it only if a specific read could still change the
conclusion, and then as a follow-up.

## 4. Gaps are follow-ups

Missing evidence that cannot change the conclusion is a follow-up: an owner's
confirmation of an exact release number, a corroborating log read that failed
while object state, timing and discriminator agree, recovery verification,
blast-radius sizing elsewhere, prevention. List each with the exact read or
person that settles it. If a gap could falsify the root cause, say which result
would falsify it; that keeps confidence at `medium`, not "unknown".

## 5. "Cannot confirm" needs no mechanism match

"Root cause not confirmed" is allowed only when no known mechanism's
discriminator holds and no deployed-source path explains the evidence. A failed
read path, an object that is already gone, or an auth error is not "no match":
try the other paths first (see
[entity-derivation-and-freshness.md](entity-derivation-and-freshness.md)).

Even then the answer gives the best-supported hypothesis with its evidence and
layer, the hypotheses already excluded and why, and the **single read** that
would confirm or falsify the leader. Never end with "cannot confirm, please
provide more material" alone, and never ask the user for an entity a read path
can still derive.

A hand-off or owner list is never the answer. Read the live objects that decide
the mechanism, conclude from them, and name an owner only for the remedy or a
follow-up.

## 6. Version boundaries

When a mechanism names a version boundary (a kernel release that fixed a hang,
an agent release that began publishing a resource, a scheduler release
that supports a request shape):

- read the live version on the affected objects (node info, image tag or
  digest, component record and live process);
- compare by version order (`sort -V`), never as strings or floats;
- state it as evidence: "running `<v>`, below the fixed release `<b>`
  (boundary source: `<where it came from>`)".

The comparison is the evidence; re-confirming the exact boundary with the owner
is a follow-up. A version on the fixed side falsifies a version-bounded
mechanism only when it belongs to the affected instance during the incident
window. A current post-upgrade version cannot falsify a pre-upgrade failure.

## 7. Interim conclusions

In a long investigation, post the current best answer as soon as one exists (a
mechanism match, or a leading hypothesis with evidence), then keep going and
post again when it changes or firms up. Use the session's ordinary progress
channel, one compact line:

`Current conclusion: <mechanism> on <object/scope> (confidence medium); basis <key fact>; excluded <H>; next <read>`

Progress narration ("checking...", "continuing...") is not an interim
conclusion. Interim updates never replace the final answer.

## Answer shape

1. **Root cause** with confidence.
2. **Evidence chain**, cause to effect, with sources.
3. **Excluded** hypotheses with the excluding fact.
4. **Remedy and owner**, including a generic workaround when one exists.
5. **Follow-ups**: each gap with the exact read or owner.

Keep the request's exact identifiers (cluster, namespace, workload, Pod, node)
in the answer.

## Self-check

- Does every evidence link bind UID, incident window with timezone and phase,
  without substituting current state or a mismatched simulation?
- Are healthy/unhealthy controls separate from affected-object proof?
- Does the first line name a mechanism, not a symptom?
- If the discriminator held, is the root cause free of hedge words, with
  `high` or `medium`?
- Is every falsified hypothesis under Excluded and absent from the conclusion?
- Is every unmet read a follow-up with its exact command or owner?
- If "not confirmed", is there truly no mechanism match, and does the answer
  still give the best hypothesis and the settling read?
- Does any deployed-source path the live objects agree with still say
  "candidate"?
- Does the answer conclude from live objects rather than only naming owners?
- In a long investigation, was an interim conclusion posted and refreshed?
