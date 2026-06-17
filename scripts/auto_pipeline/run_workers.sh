#!/usr/bin/env bash
# scripts/auto_pipeline/run_workers.sh
#
# Cron entry: ensure worker process is alive (systemd-timer-light).
#
# Phase A status: PLACEHOLDER. Real worker (claim_next + dispatch into the v4
# pipeline + complete) lands in Phase B. For now this exercises wiring +
# emits a clear "not yet implemented" log line per tick so operators see
# the cron is running.

set -euo pipefail

SCRIPT_NAME="run_workers"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

acquire_lock "$SCRIPT_NAME"

load_envrc

if paused; then
  log "PAUSED flag present at $PAUSED_FLAG; skipping worker tick"
  exit 0
fi

if ! activate_venv; then
  alert "auritale workers: no venv" "activate_venv failed at $REPO_DIR/.venv"
  exit 0
fi

log "worker tick start (phase A placeholder)"

if ! out="$(writer-jobs stats 2>&1)"; then
  alert "auritale workers: writer-jobs stats failed" "$out"
  log "writer-jobs stats failed: $out"
  exit 0
fi

log "writer-jobs stats: $out"
log "TODO(phase-b) no worker process running yet; implement claim_next + dispatch to v4 pipeline runner"
log "worker tick end"
