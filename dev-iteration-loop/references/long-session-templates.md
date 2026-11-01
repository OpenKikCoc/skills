# Templates

## Contents

1. Contract draft
2. State checkpoint patches
3. Instruction classification
4. Worker dispatch and receipt
5. Compact continuation receipt
6. Recovery receipt
7. Evidence and decision events

## 1. Contract draft

Pass a JSON file shaped like this to `init`. The script adds schema, session, revision, workspace authority, and timestamps.

```json
{
  "objective": "Deliver the user-visible outcome",
  "success_outcomes": [
    "Preserve the design property that must not disappear during implementation"
  ],
  "hard_constraints": [
    {"id": "C001", "text": "Do not modify production"}
  ],
  "non_goals": [
    "Build a general workflow engine"
  ],
  "authority": {
    "user_instruction": "turn-or-message-reference"
  },
  "milestones": [
    {
      "id": "M001",
      "title": "Design and validate the boundary",
      "deliverables": ["docs/design.md"],
      "acceptance": [
        {"id": "A001", "text": "The design covers recovery and failure behavior", "required": true}
      ]
    },
    {
      "id": "M002",
      "title": "Implement and test",
      "deliverables": ["src/", "tests/"],
      "acceptance": [
        {"id": "A002", "text": "Focused and integration tests pass", "required": true}
      ]
    }
  ]
}
```

## 2. State checkpoint patches

Move the foreground milestone into review:

```json
{
  "mode": "REVIEW",
  "milestone_status": {"M001": "REVIEW"},
  "next_action": "Review A001 against current evidence"
}
```

Complete one milestone and activate the next only after A001 evidence is recorded:

```json
{
  "mode": "DIRECT",
  "foreground_milestone": "M002",
  "milestone_status": {
    "M001": "COMPLETE",
    "M002": "ACTIVE"
  },
  "next_action": "Implement the smallest vertical slice"
}
```

Pause M002 for a contract-defined M003 side milestone:

```json
{
  "foreground_milestone": "M003",
  "milestone_status": {
    "M002": "PAUSED",
    "M003": "ACTIVE"
  },
  "suspension_stack": [
    {
      "milestone_id": "M002",
      "next_action": "Continue the focused implementation",
      "return_condition": "M003 acceptance is satisfied"
    }
  ],
  "next_action": "Perform the bounded M003 action"
}
```

Return by popping the stack, marking M003 complete, and restoring M002 plus its saved next action.

## 3. Instruction classification

```json
{
  "classification": "ADD",
  "user_authority": "turn-or-message-reference",
  "reason": "The new deliverable extends the existing outcome without replacing it",
  "contract_changes": ["add M003 and acceptance A004"],
  "foreground_effect": "keep M002 active",
  "requires_user_clarification": false
}
```

Use one of `CLARIFY`, `ADD`, `PAUSE`, `SUPERSEDE`, or `INDEPENDENT`. If two classifications would produce materially different scope, set `requires_user_clarification` true and disarm.

## 4. Worker dispatch and receipt

Dispatch contract:

```json
{
  "worker_id": "W003",
  "milestone_id": "M002",
  "task": "Inspect the parser boundary and report defects",
  "non_goals": ["Modify files", "Redefine the project objective"],
  "base_authority": "HEAD:<full-sha>",
  "read_scope": ["src/parser/**", "tests/parser/**"],
  "write_scope": [],
  "acceptance": ["Every claim cites a file, line, command, or official URL"],
  "result_schema": "worker-receipt-v1"
}
```

Receipt:

```json
{
  "worker_id": "W003",
  "status": "complete",
  "summary": "One bounded conclusion",
  "claims": [
    {"text": "Observed fact", "evidence": ["file:line or command output reference"]}
  ],
  "changes": [],
  "validation": [],
  "uncertainties": [],
  "conflicts": [],
  "recommended_next_action": "Root agent verifies the cited boundary"
}
```

Reject a receipt that contains only intent, a greeting, or a progress update.

## 5. Compact continuation receipt

`continue-after-compact` emits the following shape when the host goal is still
active and no anomaly exists:

```json
{
  "continue": true,
  "instruction": "execute next_action now; do not stop at this receipt",
  "objective": "Deliver the user-visible outcome",
  "success_outcomes": ["Preserve the important design property"],
  "hard_constraints": [
    {"id": "C001", "text": "Do not modify production"}
  ],
  "foreground_milestone": "M002",
  "milestone_title": "Implement and test",
  "required_acceptance": ["A002"],
  "acceptance_contract": [
    {"id": "A002", "text": "Focused and integration tests pass", "required": true}
  ],
  "next_action": "Run the focused behavior test",
  "epoch": 5,
  "state_revision": 12
}
```

This is an execution command, not a report to the user. Immediately perform
the returned `next_action`; do not convert it into a future-tense final answer.

## 6. Recovery receipt

```json
{
  "contract_revision": 3,
  "epoch": 5,
  "foreground_milestone": "M002",
  "hard_constraints_checked": ["C001", "C002"],
  "current_authority": {"kind": "git", "value": "HEAD:<full-sha>"},
  "valid_evidence": ["E014"],
  "stale_evidence": ["E010"],
  "workers": {"running": [], "awaiting_review": ["W003"], "blocking": []},
  "effects": {"unresolved": [], "unknown": []},
  "instruction_classification": "CLARIFY",
  "next_action": "Review W003 receipt against current source",
  "rearm_allowed": true
}
```

The script fills mechanically provable fields. The root agent fills semantic
checks such as hard constraints and instruction classification before rearm,
then immediately resumes the returned next action.

## 7. Evidence and decision events

Evidence event file:

```json
{
  "type": "evidence",
  "id": "E014",
  "milestone_id": "M002",
  "acceptance_ids": ["A002"],
  "claim": "Focused tests pass",
  "source": "command: python3 -m unittest ...",
  "authority": {"kind": "git", "value": "HEAD:<full-sha>"}
}
```

Decision event file:

```json
{
  "type": "decision",
  "id": "D007",
  "scope": "session",
  "decision": "Keep one root session across all milestones",
  "reason": "The user explicitly requested continuity",
  "authority": "user:turn-or-message-reference",
  "supersedes": []
}
```
