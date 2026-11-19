#!/bin/bash
# Shared helpers for fleet-tui-probe. Source this file; do not execute it.
# Compatible with macOS bash 3.2: no associative arrays, case-conversion expansions, or array-reading builtins.

# Patterns are ERE. Each is overridable through the environment so a different
# other interactive coding-agent TUI can be supervised without editing the scripts.
# Defaults are grounded in Codex CLI and Claude Code UI text:
#   working  - "Working (12s • esc to interrupt)" (Codex), "(12s · esc to interrupt)" or
#              "(esc to interrupt)" (Claude Code), compaction, goal / sub-agent status lines.
#              A bare "esc to ..." footer is NOT working.
#   paused   - a paused goal.
#   approval - permission cards, including Claude Code "Do you want to proceed?".
#   question - Codex multi-question picker hints (an idle session waiting for an answer).
#   queued   - no default (neither TUI shows a capacity queue); set PROBE_QUEUED_RE to enable.
: "${PROBE_WORKING_RE:=\\((([0-9]+[hms][^)]*)?(•|·) )?esc to interrupt|^[[:space:]]*Compacting([[:space:]]|$)|Waiting for agents|agents? running|Pursuing goal|\\([0-9]+[hms][^)]*(•|·) }"
: "${PROBE_PAUSED_RE:=^[[:space:]]*Goal paused}"
: "${PROBE_APPROVAL_RE:=^[[:space:]]*(Permission required|Approval required|Waiting for approval|Reviewing approval|Would you like to run the following command\\?|Do you want to proceed\\?)}"
: "${PROBE_QUEUED_RE:=}"
: "${PROBE_QUESTION_RE:=to navigate questions|to submit all|[0-9]+ unanswered\\)}"
# Explicit resolve / resume lines clear only their own blocker.
# Assigned separately: a literal "}" inside ${VAR:=...} would end the expansion early.
[ -n "${PROBE_APPROVAL_CLEAR_RE:-}" ] || PROBE_APPROVAL_CLEAR_RE='^[[:space:]]*(Approved[.]?|Approval resolved)[[:space:]]*$'
[ -n "${PROBE_PAUSED_CLEAR_RE:-}" ] || PROBE_PAUSED_CLEAR_RE='^[[:space:]]*Goal resumed[[:space:]]*$'
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
# state: unknown|exited|approval|paused|queued|working|idle
# attention: -|question|compact-request|low-context
probe_classify_pane() {
  cmd=$1
  file=$2
  tailtxt=$(grep -v '^[[:space:]]*$' "$file" | tail -n "$PROBE_STATUS_TAIL")
  ctx=$(grep -oE 'Context [0-9]+% left' "$file" | tail -n 1 | grep -oE '[0-9]+')
  tasks=$(grep -oE '[0-9]+ tasks \([^)]*\)' "$file" | tail -n 1)
  worked=$(grep -oE 'Worked for [^·|]+· [0-9:]+' "$file" | tail -n 1 | sed 's/ *$//')
  attention=-
  if [ -z "$tailtxt" ] || [ "$cmd" = - ] || [ -z "$cmd" ]; then
    state=unknown
  elif ! printf '%s\n' "$cmd" | grep -qE "$PROBE_AGENT_CMD_RE"; then
    state=exited
  else
    # Read status events in order. A later work card cannot resolve an approval
    # or pause without an explicit transition; contradictory evidence is unknown.
    state=$(printf '%s\n' "$tailtxt" | env PROBE_APPROVAL_RE="$PROBE_APPROVAL_RE" \
      PROBE_PAUSED_RE="$PROBE_PAUSED_RE" PROBE_QUEUED_RE="$PROBE_QUEUED_RE" \
      PROBE_APPROVAL_CLEAR_RE="$PROBE_APPROVAL_CLEAR_RE" PROBE_PAUSED_CLEAR_RE="$PROBE_PAUSED_CLEAR_RE" \
      PROBE_QUESTION_RE="$PROBE_QUESTION_RE" PROBE_WORKING_RE="$PROBE_WORKING_RE" awk '
      BEGIN {
        approval=ENVIRON["PROBE_APPROVAL_RE"]; paused=ENVIRON["PROBE_PAUSED_RE"]
        queued=ENVIRON["PROBE_QUEUED_RE"]; question=ENVIRON["PROBE_QUESTION_RE"]
        working=ENVIRON["PROBE_WORKING_RE"]; state="idle"; block=""
      }
      $0 ~ ENVIRON["PROBE_APPROVAL_CLEAR_RE"] {
        if (block == "approval") { block=""; state="idle" }; next
      }
      $0 ~ ENVIRON["PROBE_PAUSED_CLEAR_RE"] {
        if (block == "paused") { block=""; state="idle" }; next
      }
      $0 ~ approval { state="approval"; block=state; next }
      $0 ~ paused { state="paused"; block=state; next }
      $0 ~ question { state=(block != "" ? "unknown" : "question"); next }
      queued != "" && $0 ~ queued { state=(block != "" ? "unknown" : "queued"); next }
      $0 ~ working { state=(block != "" ? "unknown" : "working"); next }
      /◆ Done[.]/ { state=(block != "" ? "unknown" : "idle") }
      END { print state }
    ')
    if [ "$state" = question ]; then
      state=idle
      attention=question
    elif [ "$state" = idle ]; then
      if tail -n 30 "$file" | grep -qE "$PROBE_COMPACT_REQ_RE"; then
        attention=compact-request
      elif [ -n "$ctx" ] && [ "$ctx" -lt "$PROBE_LOW_CONTEXT" ]; then
        attention=low-context
      fi
    fi
  fi
  printf '%s|%s|%s|%s|%s\n' "$state" "$attention" "$ctx" "$tasks" "$worked"
}

# classify_capture <capture-file>
# Capture format (produced by probe.sh remotely):
#   @@SESSION <name> <pane_current_command>
#   @@DATA <base64 pane bytes>  (legacy plaintext is offline-only)
#   @@END
#   @@GONE <name>  (only successful enumeration proves absence)
# Prints one line per session: session|state|attention|context|tasks|worked
probe_classify_capture() {
  capture=$1
  umask 077
  work=$(mktemp -d "${TMPDIR:-/var/tmp}/probe-classify.XXXXXX") || probe_die 1 "mktemp failed"
  if ! python3 "$DIR/capture-records.py" "$capture" "$work" "${2:-}"; then
    rm -rf "$work"
    return 3
  fi
  i=1
  while [ -f "$work/$i.name" ]; do
    name=$(cat "$work/$i.name")
    cmd=$(cat "$work/$i.cmd")
    if [ "${PROBE_RENDER:-}" = raw ]; then
      # Decoded view for humans: pane lines are indented so they cannot pass for records.
      if [ -f "$work/$i.gone" ]; then
        printf '@@GONE %s\n' "$name"
      else
        printf '@@SESSION %s %s\n' "$name" "$cmd"
        sed 's/^/    /' "$work/$i.pane"
      fi
    elif [ -f "$work/$i.gone" ]; then
      printf '%s|gone|-|||\n' "$name"
    else
      printf '%s|%s\n' "$name" "$(probe_classify_pane "$cmd" "$work/$i.pane")"
    fi
    i=$((i + 1))
  done
  rm -rf "$work"
}
