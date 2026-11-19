# Remote runtime host

Lessons for running agent CLIs on a separate machine (an always-on workstation,
a machine with access the fleet host lacks (a network, a device, a licensed tool), a host that holds the credentials) and driving
them from a controller over SSH. Nothing here is tied to a particular CLI; it
applies to any runtime listed in [runtime-adapters.md](runtime-adapters.md).

## 1. Execution and credentials stay on the host

The controller (laptop, coordinator host) is only a terminal. The agent CLI,
its tools, workspaces, session store, and credentials live on the runtime
host; the controller reaches them through plain `ssh` (`ssh -tt` for a TUI,
stdin-piped prompts for batch runs).

- Freeze the **execution tuple** before running anything: controller, SSH
  alias, host OS/arch, network, persona, runtime version, workspace path,
  required credential realms, and mode (interactive, batch, resume, auth
  maintenance). A credential that is ready in another tuple is not ready here.
- Do not forward the SSH agent (`ssh -A`), run the workload locally "because
  it can start", or sync session stores between machines.
- Pass prompts on stdin, not as shell-quoted remote arguments, so prompt text
  cannot become a remote command.
- Resolve and inspect the workspace on the host (repo root, revision, dirty
  state, project instructions) and use the host path; a similarly named
  controller path is not the same tree.
- Resume through the same host, persona, and workspace that created the
  session, by explicit session ID rather than "last". After reconnecting,
  re-check cwd, revision, last completed effect, and auth: a resumable
  transcript proves neither that credentials are still valid nor that an
  interrupted tool call finished.
- Bring back only what the task authorizes (a final message file, a JSONL
  event stream), after inspecting it for sensitive content. Never copy auth
  files, tokens, cookies, credential caches, or agent sockets to the controller.

Default to plain SSH. Add an app-server tunnel, relay, forced-command broker,
or VM only when the threat model or durability requirement cannot be met
without it.

## 2. Persona and profile isolation

When several operators or roles share one host user, give each a private
persona so state cannot leak between them or into the real home.

- A persona is a launcher that sets every config/state/cache location to a
  persona-private root and then `exec`s its arguments: `HOME`, the runtime's
  own home variables (including legacy aliases), `XDG_CONFIG_HOME`,
  `XDG_STATE_HOME`, `XDG_CACHE_HOME`, and `TMPDIR`. Use `umask 077`, a minimal
  reviewed `PATH`, and disable auto-upgrade of dependent CLIs.
- A missed legacy variable silently writes helper state under the operator's
  real home. Test for that rather than trusting the list.
- Install an immutable, pinned release as a complete closure (binary plus the
  helper binaries and metadata it expects); a lone binary may start but fail
  closed on features.
- Acceptance before calling a persona usable: persona A authenticates while a
  fresh persona B stays unauthenticated; the real home gains no new runtime
  state; one interactive turn completes and leaves no stray server process;
  the agent's shell sees the persona environment; one harmless read succeeds
  per required tool; logout affects only that persona; temporary challenges,
  test credentials, and staging roots are removed.
- State the boundary honestly: same-UID personas are cooperative isolation,
  not protection against a hostile peer with a shell under the same user.

## 3. Loopback-only exposure

Dashboards, terminals, and app-servers are bearer-capability surfaces.

- Persist their bind addresses as loopback in the product's own configuration;
  do not patch generated process-manager files, and do not open a wildcard
  listener to make a link reachable. A bind change applies only to a new
  process, so inspect active sessions and get interruption authorization
  before restarting.
- Reach them remotely through a reviewed tunnel: a dedicated key used with
  `IdentitiesOnly` and batch mode, no password or keyboard-interactive
  fallback, host-key verification kept on, no shared ControlMaster,
  `ExitOnForwardFailure`, bounded server-alive probes, and one explicit
  `127.0.0.1:<local> -> 127.0.0.1:<remote>` forward. That key authenticates
  only the tunnel; it is not a general SSH identity.
- Health means the expected HTTP response through the forward, not an open TCP
  port.
- Treat a dashboard URL as a short-lived bearer credential: never print it in
  audit output, plist/unit arguments, or logs. Audit output may carry port
  numbers and policy booleans, not hostnames, usernames, key paths, or tokens.
- Loopback limits reach; it does not make public data authenticated, and it
  does not replace reviewing the agent's own filesystem sandbox and working
  directory (do not grant a broad home or codebase root for one repository).
- Status checks must be read-only. Anything that can change app permissions,
  subscriptions, or release state is a side-effect workflow, not a probe.
- Service managers: enabling autostart may only write the unit; verify it is
  loaded separately, and do not mistake a normal "not running" state of a
  fire-and-forget launcher for failure.

## 4. Separate failure domains

The ingress or dashboard layer (notification bridge, web UI, tunnel) is a convenience
around the runtime. It may be down without blocking unrelated work.

Keep these recoverable independently, so none requires the orchestrator or
dashboard to be healthy:

1. the dashboard/daemon and its tunnel;
2. the agent runtime and its sessions;
3. credential maintenance (renewal, login-approval reminders);
4. skill/instruction release;
5. notification of the human.

Rules that follow:

- Release skills from a canonical repository; the runtime's skill store is a
  consumer. Never edit the consumer copy as the source change. Detect drift by
  comparing manifest, source revision, content, and executable modes. For a
  remote host, local state says nothing about the remote store; verify on the
  remote host or through a read-only status contract, and do not infer
  freshness from a healthy tunnel.
- One component owns each incident's state and its cooldown/idempotency
  policy; other surfaces may render it but must not start their own retry
  loops. Use the ingress layer's messaging only for the current session; use an
  independent channel for recipients outside it, for delivery that must work
  while the ingress is stopped, and for auth/host-recovery alerts.
- Repository validation, auth recovery, alerts, and already-running work must
  not wait on the dashboard being online.

## 5. Credential preflight without touching secrets

- Credentials are per tool context: the OS keychain, credential caches, and
  GUI-session stores may be visible to the host user but not to a sandbox with
  the same `HOME`, and vice versa. Probe with a read-only status command and
  run login/refresh and the dependent command in the same context. If sandbox
  and host disagree, record `context_mismatch` and pick the context that will
  run the dependent command; never shuttle raw tokens between them.
- Read semantic fields, not exit codes: a status command can exit 0 while
  reporting "not authenticated", or wrap a login-needed state in a success
  envelope. Treat a malformed response as a protocol error, not a login
  request. A local-file-only status view is not proof of a usable session; use
  the network-validated check. Readiness of one tool or realm never implies
  another; check each independently, and keep one realm's protocol error from
  failing the whole report.
- Automate status, silent refresh, one recoverable interactive login approval, a
  reminder to the current user only, and bounded polling. Never automate the
  approval step, passwords, OTPs, cookies, keychain prompts, approval under
  another identity, or silent scope expansion; never message anyone but the
  current user. Keep pending approval handles private, reuse one pending
  approval across retries, and expose only the verification link to the
  trusted user.
- Never read, print, copy, or transport secrets: no env dumps (inspect an
  allowlist of variables), no credentials via argv, stdin, pipes, or
  environment variables, no copying stores between personas or machines. Keep
  each credential store separate (app credentials, dashboard capabilities,
  user tokens, per-service credentials, OS secret store); one does not prove or
  replace another.
- When qualifying a CLI upgrade or new artifact against an existing
  host-owned credential, stage the exact candidate bytes, run it in the
  credential's owning context, and record only digests of evidence. An
  unreadable credential blocks qualification; it never authorizes deleting or
  recreating the credential. Diagnose the context, signature, or provider
  integration instead.
- Fail closed: unknown identity, context, or result means stop and ask, not
  retry or substitute.

## 6. A durable interactive session over SSH

A TUI run directly under `ssh -tt` dies with the connection. Put it in a
detached terminal-multiplexer session on the host so a dropped link costs
nothing, and make the entry point idempotent.

- **One named detached tmux session per case.** The case label is free text the
  operator chooses (`<group>-<label>`); it never selects a cluster, repository,
  or workspace, and it is never sent to the agent as a prompt.
- **One command creates or attaches.** The same helper creates the session when
  it is absent and attaches the existing one otherwise, so it is also the
  reconnect command. Pass agent arguments only when creating; if the session
  already exists, refuse them, so a requested `resume` or workspace is never
  silently ignored.
- **Label with a name prefix plus a session option**
  (`set-option -t <session> @group <group>`), and list the group by filtering on
  those. Do not use tmux session groups: they share one window set, which is the
  opposite of independent parallel cases.
- **Prevent idle sleep only while the agent runs.** Wrap the agent in the host's
  keep-awake utility (for example `caffeinate -i <agent>` on macOS) inside the
  session, so the machine sleeps normally once the agent exits. Neither tmux nor
  the keep-awake survives a host reboot; after one, confirm the session is gone
  and resume the persisted agent session ID into a new, differently named tmux
  session.
- **Resolve tmux by absolute fallback paths.** A non-login SSH command often has
  a minimal `PATH`; look in `PATH` first, then the usual absolute locations
  (`/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin`), and fail clearly if none
  works.
- **State is read-only.** Check a session with a read-only probe (see
  `fleet-tui-probe`): `working`/`queued` is still running, `idle` with attention
  is waiting for the operator, `gone` means no such session. After losing the
  connection never launch a second session "to be safe"; re-attach or probe.

## 7. Verify the identity a login actually completed with

A successful login is not proof of whose login it is.

- On a host where several operators share one OS user, a credential helper or
  keychain may complete a login for another operator's identity. Read the
  authenticated identity back from the status command and compare it with the
  intended one; do not accept "login succeeded".
- Prefer a device-code single sign-on flow that the intended person approves
  under their own identity; the code and any completion handle stay private, only
  the verification link goes to the trusted user.
- If the identity is wrong, log out only that persona (never a shared store),
  verify the other personas still report their original status, and start over.

## 8. Parallel personas on one host: qualification

Several personas may run at once on one OS user only after a small matrix
passes. The claim afterwards is `cooperative_same_uid_parallel=true`, never
hostile isolation, confidentiality, or hard resource quotas (those need
separate OS users, a VM, or a container).

| Surface | Rule |
|---|---|
| Release | share immutable version directories; each persona pins an absolute path, never `current` |
| State | home, config, cache, temp, sessions, logs, sockets live under that persona's root |
| Workspace | a neutral read-only workspace by default; separate clones or worktrees for writes; never one writing checkout shared |
| Processes | prefer embedded/no-daemon sessions; no fixed port or global socket |
| Shared CLIs | record each one's resolved path, version, and sha256; any change invalidates the qualification until reviewed |

Qualify two fresh personas from the same release before real work:

1. A and B report different homes, temp dirs, workspaces, session stores, and
   socket paths, each below its own root.
2. A authenticates while B stays unauthenticated (with the verified identity).
3. A's logout leaves B untouched, and the reverse.
4. Run two simultaneous harmless sessions; interrupt A and show B continues.
5. No state appears under the host user's real home or another persona.
6. Writable worktrees stay clean with no lock, index, branch, or generated-file
   crossover.
7. No shared fixed port, forwarded agent, credential environment variable, or
   shared temp path appears during either session.

Record names, resolved non-secret paths, versions, digests, pass/fail results,
and unverified surfaces; never tokens, cookies, device codes, or environments.

## 9. Auth preflight bound to the exact binary

Compatibility facts about a CLI belong to the bytes you probed, not to its name.

- **Receipt.** Keep a small private receipt per tool: resolved absolute path,
  sha256 of the executable, and its version. Re-probe `--help` (for every flag
  the preflight will use) only when those bytes change; never cache current
  status, only the read-only help facts.
- **Missing guidance stops that step.** If a flag, subcommand, or status field
  is not in the installed `--help`, the step is a protocol gap: stop it and
  report. Do not guess syntax from memory.
- **Protocol gaps are not consent requests.** A malformed, unknown, or
  contradictory status (exit 0 with "not authenticated", a missing field, two
  fields that disagree) is a protocol gap for the operator to inspect. It never
  starts a login challenge and never asks anyone to approve anything.
- **Stable operation IDs.** Derive the logical operation or challenge ID from
  the requirement (tool, site/region, scopes), not from the binary version or
  path, so an upgrade between a begin and a resume does not orphan a pending
  approval or start a second one.

## 10. Authenticated outer transport as the only trust boundary

Reusing an already-authenticated outer channel (for example an SSH tunnel) so an
inner service needs no second password is acceptable only when **all** of these
hold; otherwise keep the inner authentication.

- the operator explicitly chose the outer transport as the sole credential
  boundary;
- the inner listener is loopback-only on every enabled address family (IPv4 and
  IPv6) with no alternate route, proxy, socket, or published port;
- the outer authentication was recovered and verified in the same host/context
  that opens the tunnel;
- the session maps to one fixed non-root account with an exact authorization
  gate;
- weaker outer-auth fallbacks (passwords, keyboard-interactive, unrelated keys)
  are disabled;
- exit, crash, and normal close all reclaim the tunnel and the local listener,
  and that cleanup has been verified.

If the inner client still insists on a password-shaped field, use a documented
non-secret placeholder only after proving it is not an authentication factor;
otherwise stop with a client-contract blocker instead of reintroducing a real
inner secret. Moving secrets through the clipboard is never a design.
