#!/bin/sh
# Nightly Memento sync. Runs `memento import-sources`, which imports every
# adapter named in ~/.config/memento/config.toml `sources` (Claude, Cursor,
# and Confluence when that key is omitted). Confluence pages still come from
# confluence-urls.txt. Markdown and raw files stay off until their names are
# added. Idempotent — dedups by content hash, so a re-run over unchanged
# content costs nothing.
#
# second-brain (RAW/Wiki knowledge bases) is retired as of 2026-09-14 — its
# lvcore and beacon content was fully captured into Memento and the live
# directory archived to ~/src/personal/second-brain-archive. To import a new
# knowledge base, register its path on the project and add `markdown` or
# `raw` to `sources`.
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
adapters = data.get("adapters")
rows = adapters.items() if isinstance(adapters, dict) else [(label, data)]
with open(path, 'a') as handle:
    for name, row in rows:
        if not isinstance(row, dict):
            continue
        entry = {
            'label': name,
            'logged_at': datetime.now(timezone.utc).isoformat(),
            'discovered': row.get('discovered', 0),
            'activated': row.get('activated', 0),
            'blocked': row.get('blocked', 0),
            'failed': row.get('failed', 0),
            'pending_retry': row.get('pending_retry', 0),
        }
        handle.write(json.dumps(entry) + '\n')
" "$label" "$ARTIFACTS_ROOT/sync_history.jsonl" 2>/dev/null || true
}

# Appends to $problems (global) if a batch-import JSON blob reports any
# blocked/failed source. Each blob is multi-line (indented JSON), so this is
# called once per blob directly rather than folded into a `for` loop over
# several blobs — word-splitting a dynamic list of multi-line strings isn't
# safe in POSIX sh.
count_problems() {
  blocked=$(printf '%s' "$1" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print(0)
    raise SystemExit
adapters = data.get('adapters')
if isinstance(adapters, dict):
    print(sum(row.get('blocked', 0) + row.get('failed', 0) for row in adapters.values() if isinstance(row, dict)))
else:
    print(data.get('blocked', 0) + data.get('failed', 0))
" 2>/dev/null || echo 0)
  if [ "${blocked:-0}" != "0" ] && [ "${blocked:-0}" != "" ]; then
    problems="${problems}${blocked} blocked/failed source(s); "
  fi
}

log "=== nightly sync starting ==="

# Optional: limit discovery to sources updated in the last N days.
# Set MEMENTO_SYNC_SINCE_DAYS while backfilling newly discovered projects
# in stages; unset it to widen. The window applies to every adapter.
# Sources with no timestamp, including Confluence pages, stay in the run.
SINCE_ARGS=""
if [ -n "${MEMENTO_SYNC_SINCE_DAYS:-}" ]; then
  SINCE_ARGS="--since $(date -v-"${MEMENTO_SYNC_SINCE_DAYS}"d +%Y-%m-%d)"
fi

# shellcheck disable=SC2086 # SINCE_ARGS is intentionally word-split.
import_output=$(run_step "import configured sources" "$MEMENTO" import-sources $SINCE_ARGS)
log_sync_run "import-sources" "$import_output"

# Surface anything that needs a human: real failures immediately, and
# pending_retry only once it's stale (younger than 48h self-heals on its own
# on tomorrow's run — see run_extraction's hash-based re-candidacy, no code
# needed for that case).
problems=""
count_problems "$import_output"

# pending_retry alone looks self-healing, but a permanently broken engine
# also lands there and retries forever. Say so the first night.
if printf '%s' "$import_output" | grep -q 'Bifrost unavailable'; then
  problems="${problems}Bifrost extraction failing (check the gateway, model, or quota); "
elif printf '%s' "$import_output" | grep -q 'Cursor unavailable'; then
  problems="${problems}Cursor extraction failing (check extractor model or quota); "
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
