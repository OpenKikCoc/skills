# Proposal packet

JSON object passed to `draft_inbox.py propose`.

| Field | Required | Notes |
|---|---|---|
| `title` | yes | Short imperative description of the rule. |
| `trigger` | yes | `user_correction`, `repeated_failure`, or `retrospective`. |
| `mechanism` | yes | Why it happened, as a reusable discriminator. |
| `evidence` | yes | 1-12 redacted quotes or observations. |
| `target` | yes | `draft_only`, `project_guidance`, `existing_skill`, `casebook`, `memory`, `new_skill`. |
| `target_ref` | unless `draft_only` | Skill name, guidance file, or memory entry to change. |
| `proposed_change` | yes | The concrete edit, in a sentence or two. |
| `existing_rule_checked` | yes | What you compared against and what was missing. |
| `blocking` | no | Must be `false` if present. |
| `origin` | no | Free-form label such as the agent or session kind. |

Dedupe key: `trigger`, `target`, `target_ref`, `title`, `proposed_change`.
Re-submitting the same ask with different prose is reported as a duplicate and
changes nothing.

Draft file names are `<16 hex digest>-<title slug>.json`; decision receipts are
`<draft name>.r<N>.json` in the sibling `decisions/` directory.

Redact before submitting: the script refuses obvious secrets and absolute home
paths but cannot recognize every private identifier.
