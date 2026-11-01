#!/bin/bash
# Probe remote tmux sessions running coding-agent TUIs (Codex CLI, Claude Code) and print one line per session:
#   session|state|attention|context|tasks|worked
# state: working|queued|idle|exited|gone ; attention: -|question|compact-request|low-context
#
# Usage:
#   probe.sh --host <ssh-alias> --sessions '<glob>[,<glob>...]' [--lines N]
#   probe.sh --from-capture <file>          # classify a saved capture (offline/tests)
#   probe.sh --host H --sessions G --raw    # print the raw capture instead of classifying
# Env: PROBE_HOST, PROBE_SESSIONS provide defaults; PROBE_*_RE override heuristics (see probe-lib.sh).
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/probe-lib.sh"

host=${PROBE_HOST:-}
sessions=${PROBE_SESSIONS:-}
lines=40
from=""
raw=0
while [ $# -gt 0 ]; do
  case "$1" in
    --host) host=${2:-}; shift 2 ;;
    --sessions) sessions=${2:-}; shift 2 ;;
    --lines) lines=${2:-}; shift 2 ;;
    --from-capture) from=${2:-}; shift 2 ;;
    --raw) raw=1; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) probe_die 2 "unknown argument: $1" ;;
  esac
done

if [ -n "$from" ]; then
  [ -r "$from" ] || probe_die 2 "capture file not readable: $from"
  probe_classify_capture "$from"
  exit 0
fi

probe_valid_host "$host" || probe_die 2 "--host (or PROBE_HOST) must be a plain ssh alias"
probe_valid_glob "$sessions" || probe_die 2 "--sessions (or PROBE_SESSIONS) must be a glob list of [A-Za-z0-9_.:*?,-]"
case "$lines" in ''|*[!0-9]*) probe_die 2 "--lines must be an integer" ;; esac

# The remote script is sent on stdin so no pane text or pattern is ever re-quoted by ssh.
capture=$(mktemp "${TMPDIR:-/var/tmp}/probe-capture.XXXXXX") || probe_die 1 "mktemp failed"
trap 'rm -f "$capture"' EXIT
opts=$(probe_ssh_opts)
# shellcheck disable=SC2086
ssh $opts "$host" "bash -s -- $(probe_b64 "$sessions") $lines" >"$capture" 2>"$capture.err" <<'REMOTE'
set -u
set -f
globs=$(printf '%s' "$1" | base64 -d 2>/dev/null || printf '%s' "$1" | base64 -D)
n=$2
all=$(tmux list-sessions -F '#{session_name}' 2>/dev/null)
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
    cmd=$(tmux list-panes -t "=$s:" -F '#{pane_current_command}' 2>/dev/null | head -n 1)
    echo "@@SESSION $s ${cmd:--}"
    # iconv drops invalid bytes but keeps CJK (strings(1) would drop CJK too).
    if command -v iconv >/dev/null 2>&1; then
      tmux capture-pane -p -t "=$s:" 2>/dev/null | iconv -c -f utf-8 -t utf-8 | grep -v '^[[:space:]]*$' | tail -n "$n"
    else
      tmux capture-pane -p -t "=$s:" 2>/dev/null | grep -v '^[[:space:]]*$' | tail -n "$n"
    fi
    echo "@@END"
  done
  case "$g" in *'*'*|*'?'*) ;; *) [ "$found" = 1 ] || { echo "@@SESSION $g -"; echo "@@END"; } ;; esac
  IFS=','
done
REMOTE
rc=$?
if [ "$rc" -ne 0 ]; then
  err=$(tail -n 1 "$capture.err" 2>/dev/null)
  rm -f "$capture.err"
  probe_die 3 "ssh to $host failed (rc=$rc): ${err:-no stderr}; check the ssh alias, keys, and agent"
fi
rm -f "$capture.err"
if [ "$raw" = 1 ]; then
  cat "$capture"
else
  probe_classify_capture "$capture"
fi
