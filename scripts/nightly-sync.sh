#!/bin/sh
# Nightly Memento sync: pulls new Claude/Cursor sessions, plus any
# Confluence pages listed in confluence-urls.txt, into the shared artifact
# store + index. Idempotent — dedups by content hash, so a re-run over
# unchanged content costs nothing (no Cursor calls, no index churn).
#
# second-brain (RAW/Wiki knowledge bases) is retired as of 2026-09-14 — its
# lvcore and beacon content was fully captured into Memento and the live
# directory archived to ~/src/personal/second-brain-archive, no longer fed
# with new sources. No import step for it here: nothing new will ever land
# there. If a similar KB is started again elsewhere, add its
# import-markdown-kb / import-raw-sources steps back into this script.
#
# Not Slack-integrated on purpose: an unattended cron job must never
# auto-send a message on Ian's behalf. Problems surface as a local macOS
# notification plus a full log, for him to read and act on himself.

set -u

# launchd runs jobs with a minimal environment — it does not inherit the
# interactive shell's PATH. memento itself is invoked by absolute path below,
# but memento in turn shells out to `cursor-agent` by bare name, which needs
# this on PATH to resolve under launchd (confirmed failing 2026-09-14:
# FileNotFoundError: 'cursor-agent' even when triggered via `launchctl start`
# from an interactive terminal — launchd's env applies regardless of trigger).
export PATH="$HOME/.local/bin:$PATH"
export MEMENTO_ATLASSIAN_EMAIL="ian.handley@lvt.com"

MEMENTO="$HOME/.local/bin/memento"
LOG_DIR="$HOME/.local/share/memento/logs"
LOG_FILE="$LOG_DIR/nightly-sync-$(date +%Y-%m-%d).log"
CONFLUENCE_URLS_FILE="$HOME/.config/memento/confluence-urls.txt"
mkdir -p "$LOG_DIR"

# Resolved once so log_sync_run doesn't re-invoke `memento config show` per
# step. Empty if config resolution fails; log_sync_run then no-ops rather
# than write anywhere unexpected.
ARTIFACTS_ROOT=$("$MEMENTO" config show 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('artifacts') or '')" 2>/dev/null)

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG_FILE"
}

run_step() {
  description="$1"
  shift
  log "START: $description"
  output=$("$@" 2>&1)
  status=$?
  printf '%s\n' "$output" >> "$LOG_FILE"
  log "END ($status): $description"
  printf '%s' "$output"
  return "$status"
}

notify() {
  osascript -e "display notification \"$1\" with title \"Memento nightly sync\"" >/dev/null 2>&1 || true
}

# Appends one structured entry per run_step call to sync_history.jsonl in the
# artifact store, for `memento report`'s nightly-sync-yield section. Best
# effort: a non-JSON blob (e.g. a dry run's human-readable output) or a
# missing ARTIFACTS_ROOT just skips the append, never fails the sync.
log_sync_run() {
  label="$1"
  blob="$2"
  [ -n "$ARTIFACTS_ROOT" ] || return 0
  printf '%s' "$blob" | python3 -c "
import json, sys
from datetime import datetime, timezone

label, path = sys.argv[1], sys.argv[2]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
entry = {
    'label': label,
    'logged_at': datetime.now(timezone.utc).isoformat(),
    'discovered': data.get('discovered', 0),
    'activated': data.get('activated', 0),
    'blocked': data.get('blocked', 0),
    'failed': data.get('failed', 0),
    'pending_retry': data.get('pending_retry', 0),
}
with open(path, 'a') as handle:
    handle.write(json.dumps(entry) + '\n')
" "$label" "$ARTIFACTS_ROOT/sync_history.jsonl" 2>/dev/null || true
}

# Appends to $problems (global) if a batch-import JSON blob reports any
# blocked/failed source. Each blob is multi-line (indented JSON), so this is
# called once per blob directly rather than folded into a `for` loop over
# several blobs — word-splitting a dynamic list of multi-line strings isn't
# safe in POSIX sh.
count_problems() {
  blocked=$(printf '%s' "$1" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('blocked',0)+d.get('failed',0))" 2>/dev/null || echo 0)
  if [ "${blocked:-0}" != "0" ] && [ "${blocked:-0}" != "" ]; then
    problems="${problems}${blocked} blocked/failed source(s); "
  fi
}

log "=== nightly sync starting ==="

# Optional: limit Claude discovery to sessions updated in the last N days.
# Set MEMENTO_SYNC_SINCE_DAYS while backfilling newly discovered projects
# (MEMENTO_PROJECT_ROOTS / [discovery] roots) in stages; unset it to widen.
SINCE_ARGS=""
if [ -n "${MEMENTO_SYNC_SINCE_DAYS:-}" ]; then
  SINCE_ARGS="--since $(date -v-"${MEMENTO_SYNC_SINCE_DAYS}"d +%Y-%m-%d)"
fi

# shellcheck disable=SC2086 # SINCE_ARGS is intentionally word-split.
sessions_claude=$(run_step "import Claude sessions (all projects)" "$MEMENTO" import-sessions --source claude --all-projects $SINCE_ARGS)
log_sync_run "claude_sessions" "$sessions_claude"
sessions_cursor=$(run_step "import Cursor conversations" "$MEMENTO" import-sessions --source cursor)
log_sync_run "cursor_sessions" "$sessions_cursor"

# Surface anything that needs a human: real failures immediately, and
# pending_retry only once it's stale (younger than 48h self-heals on its own
# on tomorrow's run — see run_extraction's hash-based re-candidacy, no code
# needed for that case).
problems=""
count_problems "$sessions_claude"
count_problems "$sessions_cursor"

# Confluence pages: format is "PROJECT_ID URL", one per line, "#"-prefixed
# lines and blank lines ignored. Grouped by project_id so each project gets
# one import-url call (one index rebuild) instead of one per URL. Add/remove
# a page by editing this file — nothing else needs touching.
if [ -f "$CONFLUENCE_URLS_FILE" ]; then
  projects=$(awk 'NF && $1 !~ /^#/ {print $1}' "$CONFLUENCE_URLS_FILE" | sort -u)
  for project_id in $projects; do
    urls=$(awk -v p="$project_id" 'NF && $1 !~ /^#/ && $1 == p {print $2}' "$CONFLUENCE_URLS_FILE")
    confluence_output=$(run_step "import Confluence pages ($project_id)" "$MEMENTO" import-url $urls --project-id "$project_id")
    log_sync_run "confluence:$project_id" "$confluence_output"
    count_problems "$confluence_output"
  done
fi

stale_pending=$(find "$HOME/.local/share/memento/artifacts" -name job_status.json -mtime +2 2>/dev/null | wc -l | tr -d ' ')
if [ "$stale_pending" != "0" ]; then
  problems="${problems}${stale_pending} source(s) stuck pending_retry/blocked for 48h+; "
fi

if [ -n "$problems" ]; then
  log "PROBLEMS: $problems"
  notify "$problems See $LOG_FILE"
else
  log "no problems"
fi

log "=== nightly sync finished ==="
