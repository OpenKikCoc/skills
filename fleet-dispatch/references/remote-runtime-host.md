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
