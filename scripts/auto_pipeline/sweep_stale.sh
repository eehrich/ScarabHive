#!/usr/bin/env bash
# scripts/auto_pipeline/sweep_stale.sh
#
# Cron entry: writer-jobs sweep-stale. Re-queues jobs whose worker heartbeat
# is older than the configured threshold (worker crashed mid-run).

set -euo pipefail

SCRIPT_NAME="sweep_stale"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

acquire_lock "$SCRIPT_NAME"

load_envrc

# Sweep runs even while paused — we want recovery state to be ready when
# we un-pause. If you disagree, add `if paused; then exit 0; fi` here.

if ! activate_venv; then
  alert "auritale sweep-stale: no venv" "activate_venv failed at $REPO_DIR/.venv"
  exit 0
fi

log "sweep-stale tick start"

if ! out="$(writer-jobs sweep-stale 2>&1)"; then
  alert "auritale sweep-stale: command failed" "$out"
  log "writer-jobs sweep-stale failed: $out"
  exit 0
fi

log "writer-jobs sweep-stale: $out"
log "sweep-stale tick end"
