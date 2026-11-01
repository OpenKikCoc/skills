#!/bin/bash
# Shared helpers for fleet-tui-probe. Source this file; do not execute it.
# Compatible with macOS bash 3.2: no associative arrays, case-conversion expansions, or array-reading builtins.

# Patterns are ERE. Each is overridable through the environment so another
# interactive coding-agent TUI can be supervised without editing the scripts.
# Defaults are grounded in Codex CLI (0.160) and Claude Code UI text:
#   working  - "Working (12s • esc to interrupt)" (Codex), "(12s · esc to interrupt)"
#              (Claude Code), Codex goal / sub-agent status lines.
#   question - Codex multi-question picker hints; Claude Code / Codex
#              "Do you want to proceed?" approval prompts.
#   queued   - no default (neither TUI shows a capacity queue); set PROBE_QUEUED_RE to enable.
: "${PROBE_WORKING_RE:=esc to|Waiting for agents|agents? running|Pursuing goal|\\([0-9]+[hms][^)]* • }"
: "${PROBE_QUEUED_RE:=}"
: "${PROBE_QUESTION_RE:=Do you want to proceed\\?|to navigate questions|to submit all|[0-9]+ unanswered\\)}"
# Assigned separately: a literal "}" inside ${VAR:=...} would end the expansion early.
[ -n "${PROBE_COMPACT_REQ_RE:-}" ] || PROBE_COMPACT_REQ_RE='([Pp]lease run|[Rr]un)[^/]{0,24}/compact'
# pane_current_command: native Claude Code shows "claude"; npm-installed Codex shows "node".
: "${PROBE_AGENT_CMD_RE:=^(codex|claude|node)$}"
: "${PROBE_STATUS_TAIL:=20}"
: "${PROBE_LOW_CONTEXT:=15}"

probe_die() {
  # Deterministic error: one line on stderr, fixed prefix, explicit exit code.
  printf 'error: %s\n' "$2" >&2
  exit "$1"
}

probe_valid_name() {
  # tmux session / window targets we accept: no quotes, spaces, or shell metacharacters.
  case "$1" in
    ''|*[!A-Za-z0-9_.:-]*) return 1 ;;
  esac
  return 0
}

probe_valid_glob() {
  case "$1" in
    ''|*[!A-Za-z0-9_.:*?,-]*) return 1 ;;
  esac
  return 0
}

probe_valid_host() {
  case "$1" in
    ''|-*|*[!A-Za-z0-9_.@:-]*) return 1 ;;
  esac
  return 0
}

probe_ssh_opts() {
  # ControlMaster=no avoids stale multiplexed masters after sleep/network loss.
  printf '%s\n' -o BatchMode=yes -o ConnectTimeout="${PROBE_SSH_TIMEOUT:-15}" \
    -o ControlMaster=no -o ControlPath=none
}

probe_b64() {
  printf '%s' "$1" | base64 | tr -d '\n'
}

# classify_pane <cmd> <pane-file>
# Prints: state|attention|context|tasks|worked
# state: exited|queued|working|idle ; attention: -|question|compact-request|low-context
probe_classify_pane() {
  cmd=$1
  file=$2
  tailtxt=$(tail -n "$PROBE_STATUS_TAIL" "$file")
  ctx=$(grep -oE 'Context [0-9]+% left' "$file" | tail -n 1 | grep -oE '[0-9]+')
  tasks=$(grep -oE '[0-9]+ tasks \([^)]*\)' "$file" | tail -n 1)
  worked=$(grep -oE 'Worked for [^·|]+· [0-9:]+' "$file" | tail -n 1 | sed 's/ *$//')
  attention=-
  if ! printf '%s\n' "$cmd" | grep -qE "$PROBE_AGENT_CMD_RE"; then
    state=exited
  elif [ -n "$PROBE_QUEUED_RE" ] && printf '%s\n' "$tailtxt" | grep -qE "$PROBE_QUEUED_RE"; then
    state=queued
  elif printf '%s\n' "$tailtxt" | grep -qE "$PROBE_QUESTION_RE"; then
    # A visible question card blocks the session even if its footer says "esc to ...".
    state=idle
    attention=question
  elif printf '%s\n' "$tailtxt" | grep -qE "$PROBE_WORKING_RE"; then
    state=working
  else
    state=idle
    if tail -n 30 "$file" | grep -qE "$PROBE_COMPACT_REQ_RE"; then
      attention=compact-request
    elif [ -n "$ctx" ] && [ "$ctx" -lt "$PROBE_LOW_CONTEXT" ]; then
      attention=low-context
    fi
  fi
  printf '%s|%s|%s|%s|%s\n' "$state" "$attention" "$ctx" "$tasks" "$worked"
}

# classify_capture <capture-file>
# Capture format (produced by probe.sh remotely):
#   @@SESSION <name> <pane_current_command>
#   <non-blank pane lines>
#   @@END
# Prints one line per session: session|state|attention|context|tasks|worked
probe_classify_capture() {
  capture=$1
  work=$(mktemp -d "${TMPDIR:-/var/tmp}/probe-classify.XXXXXX") || probe_die 1 "mktemp failed"
  awk -v dir="$work" '
    /^@@SESSION / { n++; name=$2; cmd=$3; f=dir "/" n ".pane"; print name > (dir "/" n ".name"); print cmd > (dir "/" n ".cmd"); printf "" > f; next }
    /^@@END/ { f=""; next }
    f != "" { print >> f }
  ' "$capture"
  i=1
  while [ -f "$work/$i.name" ]; do
    name=$(cat "$work/$i.name")
    cmd=$(cat "$work/$i.cmd")
    if [ ! -s "$work/$i.pane" ]; then
      printf '%s|gone|-|||\n' "$name"
    else
      printf '%s|%s\n' "$name" "$(probe_classify_pane "$cmd" "$work/$i.pane")"
    fi
    i=$((i + 1))
  done
  rm -rf "$work"
}
