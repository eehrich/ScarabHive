#!/usr/bin/env bash
# scripts/auto_pipeline/run_feedback_sync.sh
#
# Cron entry: pull + push feedback once (writer-jobs feedback-sync).
#
# Behaviour:
#   - holds a per-script flock so concurrent ticks no-op
#   - skips on PAUSED flag
#   - alerts on any non-zero exit or transport error captured by the CLI
#
# Phase A status: USES REAL CLI. The writer-jobs feedback-sync subcommand is
# implemented by Agent 1; this script is the cron front-door.

set -euo pipefail

SCRIPT_NAME="run_feedback_sync"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

acquire_lock "$SCRIPT_NAME"

load_envrc

if paused; then
  log "PAUSED flag present at $PAUSED_FLAG; skipping feedback sync"
  exit 0
fi

if ! activate_venv; then
  alert "auritale feedback-sync: no venv" "activate_venv failed at $REPO_DIR/.venv"
  exit 0
fi

if [ -z "${AURITALE_WRITER_KEY:-}" ]; then
  alert "auritale feedback-sync: missing AURITALE_WRITER_KEY" \
        "Set AURITALE_WRITER_KEY in .envrc before enabling this cron."
  log "AURITALE_WRITER_KEY unset; aborting tick"
  exit 0
fi

log "feedback-sync tick start"

if ! out="$(writer-jobs feedback-sync 2>&1)"; then
  alert "auritale feedback-sync: command failed" "$out"
  log "writer-jobs feedback-sync failed: $out"
  exit 0
fi

log "writer-jobs feedback-sync: $out"
log "feedback-sync tick end"
