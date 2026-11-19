---
name: dev-code-navigation
description: "Use when a coding agent (Claude Code, Codex CLI, ...) needs symbol definitions, callers, references, implementations or targeted function bodies and a Serena MCP server is (or can be) configured, including Go/gopls and Git worktrees. Prefer Serena before grep-based symbol navigation; keep text, config and generated-code searches on ordinary tools, and explain evidence-backed fallbacks. Also covers installing and checking the bundled session-isolated Serena launcher."
---

# Code Navigation (Serena)

Prefer Serena for symbol navigation, not every file read or text search.
Do not run onboarding or generate memories before working. Repository discovery
does not start language servers; warm symbol caches only for explicit projects.

## Choose the Next Retrieval

- For definitions, callers/references, implementations, file symbol structure,
  or targeted symbol bodies in supported source, use Serena before grep-based
  symbol navigation or broad file reads. Once a symbol or file is known, switch
  to a bounded symbol query rather than continuing repository-wide searches.
- Use ordinary search for unknown names or paths, string literals, configuration,
  documentation, generated/vendor sources, or exact historical revisions not
  active in Serena. A text match is not a semantic reference or implementation.
- Keep edits, tests, plain-text reads, and tiny known-file fixes on existing tools.
  Do not repeat a useful symbol result with an equivalent broad text search.

## Use the Current Agent's Tools

Use an exposed Serena tool directly. In Claude Code the tools appear as
`mcp__<server-name>__find_symbol` and so on; `/mcp` shows whether the server is
connected. If you are unsure whether Serena is available, check this session's
own tool list once; do not use a shell wrapper as an MCP substitute. A server
inventory, loaded skill, or another agent's successful call does not prove this
agent can call Serena. Each child uses its own tool surface and working tree.

Do not stop at `get_current_config`, `activate_project`, or a `ready` status:
complete the task-relevant symbol query. Skip redundant health checks when
queries already work. If a required tool is absent or a query fails, use the
bounded fallback below; do not keep probing the same unchanged state.

## Bind the Actual Working Tree

- The launcher detects the nearest Git root from the client's startup directory,
  including `.git` pointer files in worktrees outside the usual code directory.
- After creating or entering another worktree, call `activate_project` with its
  absolute working-tree root before symbol queries. Shell `cd` does not retarget
  an already running MCP server. Never pass the Git common directory instead.
- If no project is active, resolve the task's actual repository and activate it.
  Do not activate HOME, a directory containing unrelated repositories, or a
  different checkout merely because it has the same branch/project name.
- Treat `--check` status `unbound`, `wrong_worktree`, or `different_project` as
  actionable. Use `--check --expect-project /absolute/worktree` when exact
  binding matters; a later shell `cd` never retargets an existing connection.
- Separate client sessions must launch separate stdio instances. Do not switch
  projects on a server concurrently shared with another agent; use a dedicated
  connection or the existing source-reading tools instead.
- The launcher discovers stable Git repositories below the path-separated
  `SERENA_CODE_ROOTS` (default `~/src`; set it to wherever your checkouts
  live, e.g. `SERENA_CODE_ROOTS=~/src:~/work`). A root that is itself a Git
  repository is terminal; list nested repositories as separate roots when needed.
  Only selected projects enter the private connection registry. Discovery skips
  `tmp`, `.tmp`, `.cache`, `__pycache__`, `.git`, `vendor`, and `node_modules`
  below each root; an explicit root is always scanned even if its own path
  contains one of those names. Add more with `SERENA_DISCOVERY_EXCLUDES`, a JSON
  list of relative directory paths (for example `["build/out", "scratch"]`);
  absolute, `..`, empty, or non-JSON values are rejected. `--check` and
  `--discover` report every exclusion as `discovery_exclusions` (path, pattern,
  reason).

## Navigate by Symbol

1. Use `get_symbols_overview` for a known file or `find_symbol` with a narrow
   path and `include_body=false` to find the definition.
2. Read only relevant function/type bodies with `find_symbol(include_body=true)`.
3. Use `find_referencing_symbols` for callers/usages, `find_implementations` for
   interface implementations, and `find_declaration` from an exact use site.
   Reuse the returned `name_path` verbatim; Go receiver methods may be flat names,
   not `Type/Method`. Serena `body_location` lines are zero-based; add one for
   user-facing `file:line` citations.
4. Inspect the returned source and relevant tests before editing with existing
   tools. References describe the analyzed build, not a complete runtime call graph.

Keep responses bounded with `max_answer_chars`. Refine an ambiguous name/path
before increasing limits; fetch bodies only for symbols needed by the task.

## Fall Back with Evidence

- If symbol navigation cannot proceed, state the observed reason once: required
  tool absent from this agent, no active language server, failed query, or source
  outside the analyzed scope. Then use a bounded ordinary search/read and keep
  the unsupported semantic claim explicit. Normal text/config searches need no
  fallback warning.
- Activation with no active language server is not symbol readiness. For an
  empty or contradictory result, check the actual worktree and query path plus
  relevant language/build scope before concluding there is no symbol or caller.
  An empty result alone does not prove Serena is unavailable.
- Reuse successful queries; retry only after correcting a concrete path/query
  mismatch or an observed capability/state change. Do not auto-reconnect MCP,
  restart servers, change project/build configuration, or interrupt other agents
  to force adoption. Resume symbol navigation when the limitation is resolved.

## Freshness and Go Scope

- Queries must reflect saved files in the active worktree, including external
  edits, additions, deletions and renames. Do not reuse a prior body or location
  after its file changes; unsaved editor buffers are outside this integration.
- If a result contradicts current source, verify the active project and read the
  file directly. Restart only this connection's language server after user
  confirmation; do not reset shared caches or restart another session.
- gopls coverage depends on the Go toolchain, modules/workspaces, build tags,
  GOOS/GOARCH, and resolved dependencies. An empty reference result is not proof
  of no callers when that scope is incomplete. Do not rewrite `go.mod`, `go.work`
  or project flags merely to make navigation succeed.
- Remote-only code requires a server on that host; a local checkout is not a live
  view of changes made on a remote development machine.

## Register the Launcher

Install [Serena](https://github.com/oraios/serena) so `serena` is on `PATH`
(or point `SERENA_EXECUTABLE` at it), plus the language servers your projects
need (for example `gopls` for Go). Then register the bundled launcher as a
stdio MCP server. It binds to the Git root of the client's startup directory,
so register it at user scope and start the client inside a repository:

```bash
claude mcp add --scope user serena -e SERENA_CODE_ROOTS="$HOME/src" -- \
  python3 /abs/path/to/dev-code-navigation/scripts/start_serena.py
# Codex CLI equivalent:
codex mcp add --env SERENA_CODE_ROOTS="$HOME/src" serena -- \
  python3 /abs/path/to/dev-code-navigation/scripts/start_serena.py
```

Run without arguments, the launcher starts Serena's MCP server with the
read-only navigation context in `assets/code-navigation.yml`. Note that
`assets/serena-config.json` is not a client MCP config: it is the Serena
server-side settings template the launcher copies into each connection's private
`SERENA_HOME`.

## Installation Contract

`python3 scripts/start_serena.py --check` reports exact startup binding, workspace
roots, discovered/configured counts, runtime identity, and missing executables
for the current project's inferred languages without writing state. The launcher
requires `serena` and fails before semantic use when required host executables
such as `gopls` are missing. It does not install them.

Each connection keeps private config, logs, registry, lease, active-project state,
and full-path-keyed project symbol caches under
`~/.serena/code-intelligence/sessions`. SolidLSP runtime downloads use an immutable
shared seed generation keyed by Serena version, platform and architecture. Cold
preparation is serialized and atomically published with a checksummed manifest.
Each connection receives a writable overlay whose prepared entries point to that
immutable seed, so an unprepared lazy activation can download only into its own
session and cannot race or mutate another connection. Override the state root with
`SERENA_CODE_STATE_ROOT`. The current Git root is prepared eagerly; other projects
remain lazy and are not registered until explicitly selected. The launcher forces
Serena's configured project-state path through a dedicated Serena CLI bootstrap,
without replacing `PYTHONPATH` or normal Python startup. Dynamic absolute-path
activation therefore never uses repository-local mutable state: immediately
before Serena adds an unregistered path, the bootstrap applies the same private
normalization. When a repository has `.serena/project.yml`, the launcher validates
that it is a non-symlink within the project and copies only supported language,
ignore, encoding, workspace, and language-server settings into the private
connection state; commands and tool or mode overrides are not imported.

`--warm` publishes a separate immutable project-state seed keyed by the full
project path, Git revision, saved worktree content, and generated project config.
A later matching connection copies that seed into its private writable project
state before startup. Live connections therefore reuse warm symbols without ever
sharing a mutable project configuration or symbol cache.

Run `--cleanup-plan` to classify leased session directories as `active`,
`abandoned_candidate`, or `unknown`. It is read-only: it never deletes by age,
never removes active or unknown state, and leaves every abandoned candidate for
explicit review. The connection exposes navigation only, with no code-editing,
shell, onboarding or memory tools. Templates live in
[serena-config.json](assets/serena-config.json). A new MCP configuration alone
does not prove that an existing agent's tools changed (Claude Code and Codex
load MCP servers at session start). Verify in the
affected agent; coordinate client refresh with the user rather than disrupting
ongoing development sessions.

Use `--discover` to validate and report discoverable project configurations
without starting MCP. Use `--warm [PROJECT ...]` to build document-symbol caches and
`--doctor [PROJECT ...]` to run one real language-server symbol query per target.
Doctor chooses a tracked source that matches the normalized project languages and
skips paths covered by the normalized `ignored_paths` configuration using Serena's
gitignore-style matcher. Unknown configured language IDs fall back to the
launcher's broad supported-source suffix set instead of producing no candidate.
Omitting project selectors targets every discovered stable repository, so prefer
an explicit bounded list for routine warming and diagnosis. Duplicate directory
names require absolute-path selectors. These commands never run repository builds
or keep language servers resident after completion. An explicit target list does
not configure or preflight unrelated discoveries; they are reported separately as
`not_evaluated` and do not fail successful targets. Target configuration,
preflight, or query failures still return nonzero.
