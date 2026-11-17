# Agent skills

Skills for coding agents (Claude Code, Codex CLI, and others that read
`SKILL.md` directories). Agents discover skills only at the top level, so the
category is the name prefix:

| Prefix | Category |
|---|---|
| `fleet-` | running and supervising several agent sessions |
| `dev-` | day-to-day engineering workflow for one agent |
| `meta-` | improving the agent and its skills from experience |
| `ops-` | production operations and incident work |
| `web-` | HTML deliverables |
| `hr-` | hiring |

Each directory is self-contained: `SKILL.md` is the entry point, `references/`
holds depth, `scripts/` holds standard-library tools with their own `test_*.py`.

## fleet — multi-agent orchestration

| Skill | What it does |
|---|---|
| [fleet-dispatch](fleet-dispatch/SKILL.md) | Run a supervised fleet from dispatch cards: platform (L0), workers/reviewers (L1), event-driven coordinator (L2), remote supervisor (L3); complexity-routed reasoning effort; verify → independent review → exact-SHA land |
| [fleet-parallel-merge](fleet-parallel-merge/SKILL.md) | Several writers on one change: fixed base SHA, disjoint write sets, ordered integration, restart recovery |
| [fleet-remote-runner](fleet-remote-runner/SKILL.md) | Run a predeclared multi-step workflow on a remote host in detached tmux, with heartbeats and hashed receipts |
| [fleet-tui-probe](fleet-tui-probe/SKILL.md) | Read-only state probe for an interactive agent TUI in remote tmux |

## dev — engineering workflow

| Skill | What it does |
|---|---|
| [dev-iteration-loop](dev-iteration-loop/SKILL.md) | Pick the smallest loop (L0/L1/L2) and keep long sessions recoverable across compaction |
| [dev-ci-fix-loop](dev-ci-fix-loop/SKILL.md) | Drive a PR/MR to green required checks on the current SHA (GitHub `gh`, GitLab `glab`) |
| [dev-code-navigation](dev-code-navigation/SKILL.md) | Symbol definitions, callers, and references through Serena MCP, bound to the real worktree |
| [dev-code-graph](dev-code-graph/SKILL.md) | Graphify knowledge graphs for one or many repositories, with any OpenAI-compatible backend |
| [dev-disk-budget](dev-disk-budget/SKILL.md) | Budgeted build caches and owned temp roots so parallel sessions do not fill the disk |

## meta — learning from experience

| Skill | What it does |
|---|---|
| [meta-retrospective](meta-retrospective/SKILL.md) | Turn a material correction or repeated failure into an evidence-backed improvement proposal |
| [meta-session-mining](meta-session-mining/SKILL.md) | Mine Codex / Claude Code session logs into redacted request inventories |
| [meta-skills-repo](meta-skills-repo/SKILL.md) | Validate, test, mirror, and publish-gate a skills repository (including a sensitive-term scan) |

## ops — operations

| Skill | What it does |
|---|---|
| [ops-k8s-rca](ops-k8s-rca/SKILL.md) | Evidence-driven Kubernetes incident RCA with plain kubectl: pod-startup classifier, control-plane diagnosis, change attribution, workload identity |
| [ops-oncall-ledger](ops-oncall-ledger/SKILL.md) | Oncall tickets as a local JSON ledger with per-ticket RCA and an idempotent weekly roll-up |

## web — HTML deliverables

| Skill | What it does |
|---|---|
| [web-live-deck](web-live-deck/SKILL.md) | Author presentation decks as HTML |
| [web-ai-companion](web-ai-companion/SKILL.md) | Embed a local-AI Q&A panel in any HTML page |
| [web-diagram](web-diagram/SKILL.md) | Architecture, flow, and sequence diagrams as HTML cards with auto-routed SVG connectors; verifier checks overlaps and edge crossings, then renders headless |
| [web-html-box](web-html-box/SKILL.md) | Self-contained interactive HTML block at a fixed viewport for docs and pages; exact-viewport overflow check and hash-verified publish |

## hr — hiring

| Skill | What it does |
|---|---|
| [hr-interview-feedback](hr-interview-feedback/SKILL.md) | Evidence-graded Chinese hiring feedback from interview transcripts |

## Checks

```bash
python3 meta-skills-repo/scripts/skills_repo.py validate
python3 meta-skills-repo/scripts/skills_repo.py test
python3 meta-skills-repo/scripts/skills_repo.py gate   # before publishing
```
