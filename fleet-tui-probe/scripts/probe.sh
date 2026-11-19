#!/bin/bash
# Probe remote tmux sessions running coding-agent TUIs (Codex CLI, Claude Code) and print one line per session:
#   session|state|attention|context|tasks|worked
# state: working|queued|paused|approval|idle|exited|gone|unknown|probe_error
# attention: -|question|compact-request|low-context
#
# Usage:
#   probe.sh --host <ssh-alias> --sessions '<glob>[,<glob>...]' [--lines N]  [--timeout-seconds S]
#   probe.sh --from-capture <file>          # classify a saved capture (offline/tests)
#   probe.sh --host H --sessions G --raw    # print the raw capture instead of classifying
# Env: PROBE_HOST, PROBE_SESSIONS provide defaults; PROBE_COLLECTION_TIMEOUT is the whole-collection
#   deadline in seconds (default 30); PROBE_*_RE override heuristics (see probe-lib.sh).
# Exit: 0 ok, 2 invalid input, 3 failed or timed-out collection (probe_error rows, no gone).
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/probe-lib.sh"

host=${PROBE_HOST:-}
sessions=${PROBE_SESSIONS:-}
lines=40
from=""
raw=0
timeout_seconds=${PROBE_COLLECTION_TIMEOUT:-30}
original_args=("$@")
while [ $# -gt 0 ]; do
  case "$1" in
    --host) host=${2:-}; shift 2 ;;
    --sessions) sessions=${2:-}; shift 2 ;;
    --lines) lines=${2:-}; shift 2 ;;
    --timeout-seconds) [ $# -ge 2 ] || probe_die 2 "--timeout-seconds requires a value"; timeout_seconds=$2; shift 2 ;;
    --from-capture) from=${2:-}; shift 2 ;;
    --raw) raw=1; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) probe_die 2 "unknown argument: $1" ;;
  esac
done

if [ -n "$from" ]; then
  [ -r "$from" ] || probe_die 2 "capture file not readable: $from"
  probe_classify_capture "$from"
  exit $?
fi

probe_valid_host "$host" || probe_die 2 "--host (or PROBE_HOST) must be a plain ssh alias"
probe_valid_glob "$sessions" || probe_die 2 "--sessions (or PROBE_SESSIONS) must be a glob list of [A-Za-z0-9_.:*?,-]"
case "$lines" in ''|*[!0-9]*) probe_die 2 "--lines must be an integer" ;; esac

if [ "${PROBE_DEADLINE_WORKER:-}" != "$PPID" ]; then
  exec python3 "$DIR/ssh-deadline.py" "$timeout_seconds" "$sessions" bash "$0" "${original_args[@]}"
fi

# The remote script is sent on stdin so no pane text or pattern is ever re-quoted by ssh.
umask 077
private=$(mktemp -d "${TMPDIR:-/var/tmp}/probe-capture.XXXXXX") || probe_die 1 "mktemp failed"
capture=$private/capture
trap 'rm -rf "$private"' EXIT
opts=$(probe_ssh_opts)
# shellcheck disable=SC2086
ssh $opts "$host" "bash -s -- $(probe_b64 "$sessions") $lines" >"$capture" 2>"$capture.err" <<'REMOTE'
set -u
set -f
globs=$(printf '%s' "$1" | base64 -d 2>/dev/null || printf '%s' "$1" | base64 -D)
n=$2
tmux_bin=$(command -v tmux 2>/dev/null || true)
if [ -z "$tmux_bin" ]; then
  for candidate in /opt/homebrew/bin/tmux /usr/local/bin/tmux /usr/bin/tmux; do
    if [ -x "$candidate" ]; then
      tmux_bin=$candidate
      break
    fi
  done
fi
[ -n "$tmux_bin" ] || { echo 'remote tmux executable not found' >&2; exit 127; }
all=$("$tmux_bin" list-sessions -F '#{session_name}') || { printf 'tmux enumeration failed\n' >&2; exit 1; }
seen=' '
IFS=','
for g in $globs; do
  unset IFS
  found=0
  for s in $all; do
    case "$s" in
      $g) ;;
      *) continue ;;
    esac
    found=1
    case "$seen" in *" $s "*) continue ;; esac
    seen="$seen$s "
    cmds=$("$tmux_bin" list-panes -t "=$s:" -F '#{pane_current_command}') || { printf 'tmux pane enumeration failed\n' >&2; exit 1; }
    cmd=$(printf '%s\n' "$cmds" | head -n 1)
    [ -n "$cmd" ] || { printf 'tmux pane command unavailable\n' >&2; exit 1; }
    pane=$("$tmux_bin" capture-pane -p -t "=$s:") || { printf 'tmux capture failed\n' >&2; exit 1; }
    echo "@@SESSION $s $cmd"
    # Encode body bytes so reserved control records cannot be forged by pane text.
    printf '@@DATA '
    if command -v iconv >/dev/null 2>&1; then
      printf '%s\n' "$pane" | iconv -c -f utf-8 -t utf-8 | grep -v '^[[:space:]]*$' | tail -n "$n" | base64 | tr -d '\n'
    else
      printf '%s\n' "$pane" | grep -v '^[[:space:]]*$' | tail -n "$n" | base64 | tr -d '\n'
    fi
    printf '\n@@END\n'
  done
  case "$g" in *'*'*|*'?'*) ;; *) [ "$found" = 1 ] || echo "@@GONE $g" ;; esac
  IFS=','
done
REMOTE
rc=$?
if [ "$rc" -ne 0 ]; then
  # Discard partial captures: a failed collection proves no session absent.
  saved_ifs=$IFS
  IFS=','
  set -f
  for g in $sessions; do printf '%s|probe_error|-|||\n' "$g"; done
  IFS=$saved_ifs
  probe_die 3 "collection from $host failed (rc=$rc); remote work is unchanged"
fi
rm -f "$capture.err"
if [ "$raw" = 1 ]; then PROBE_RENDER=raw; else PROBE_RENDER=; fi
export PROBE_RENDER
probe_classify_capture "$capture" "$sessions" >"$private/classified" || {
  for g in ${sessions//,/ }; do printf '%s|probe_error|-|||\n' "$g"; done
  exit 3
}
cat "$private/classified"
