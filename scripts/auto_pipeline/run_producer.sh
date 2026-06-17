#!/usr/bin/env bash
# scripts/auto_pipeline/run_producer.sh
#
# Cron entry: tick the producer.
#
# Phase A status: PLACEHOLDER. The real producer (enqueue loop for launcher,
# feedback-triage, commission) lands in Phase B. For now this script verifies
# the orchestration wiring is sound:
#   - env loads, venv activates
#   - writer-jobs stats runs and returns valid JSON
#   - lock + pause flag + log + alert wiring is exercised
#
# This script is intentionally cron-tolerant: it exits 0 on inner failures
# (alerts are sent via lib/common.sh::alert).

set -euo pipefail

SCRIPT_NAME="run_producer"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

acquire_lock "$SCRIPT_NAME"

load_envrc

if paused; then
  log "PAUSED flag present at $PAUSED_FLAG; skipping producer tick"
  exit 0
fi

if ! activate_venv; then
  alert "auritale producer: no venv" "activate_venv failed at $REPO_DIR/.venv"
  exit 0
fi

log "producer tick start (phase A placeholder)"

if ! out="$(writer-jobs stats 2>&1)"; then
  alert "auritale producer: writer-jobs stats failed" "$out"
  log "writer-jobs stats failed: $out"
  exit 0
fi

log "writer-jobs stats: $out"
log "TODO(phase-b) implement producer enqueue logic: launcher tick, feedback triage, commission vetter"
log "producer tick end"
