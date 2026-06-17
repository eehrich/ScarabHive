#!/usr/bin/env bash
# scripts/auto_pipeline/run_health_check.sh
#
# Cron entry: curl /health on the production feedback-api, persist status,
# pause the pipeline if it's red for >AURITALE_HEALTH_FAIL_THRESHOLD ticks.
#
# State file ($RUNTIME_DIR/health_state) is a single integer: the current
# consecutive-failure count. We pause by `touch`-ing $PAUSED_FLAG, which
# the other run_*.sh scripts check via the paused() helper.
#
# Recovery: on the first OK response we reset the counter and remove the
# pause flag (auto-resume). Operators can also remove it manually.

set -euo pipefail

SCRIPT_NAME="run_health_check"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

acquire_lock "$SCRIPT_NAME"

load_envrc

threshold="${AURITALE_HEALTH_FAIL_THRESHOLD:-3}"
url="${AURITALE_HEALTH_URL:-}"
if [ -z "$url" ]; then
  base="${AURITALE_FEEDBACK_API_BASE:-https://api1.auritale.com:8081}"
  url="${base%/}/health"
fi

if ! command -v curl >/dev/null 2>&1; then
  log "WARN curl not available; cannot probe ${url}"
  exit 0
fi

# Read previous counter; default 0.
prev=0
if [ -f "$HEALTH_STATE_FILE" ]; then
  prev="$(cat "$HEALTH_STATE_FILE" 2>/dev/null || echo 0)"
  # Sanitize: digits only.
  case "$prev" in
    ''|*[!0-9]*) prev=0 ;;
  esac
fi

# curl -sS: silent body, show errors; -o/dev/null: drop body; -w: print code.
# --max-time 10: don't let cron hang on a wedged TLS handshake.
status_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$url" 2>/dev/null || echo 000)"
log "health probe url=${url} status=${status_code} prev_fail=${prev}"

case "$status_code" in
  2*)
    # Healthy. Reset counter + clear pause flag if we set it.
    printf '0\n' > "$HEALTH_STATE_FILE" 2>/dev/null || true
    if [ -f "$PAUSED_FLAG" ]; then
      rm -f "$PAUSED_FLAG"
      log "RECOVERED: removed pause flag $PAUSED_FLAG"
      alert "auritale health: recovered" "Pipeline resumed after $prev consecutive bad ticks."
    fi
    ;;
  *)
    next=$((prev + 1))
    printf '%s\n' "$next" > "$HEALTH_STATE_FILE" 2>/dev/null || true
    log "health probe FAIL ${status_code} (count=${next}/${threshold})"
    if [ "$next" -ge "$threshold" ] && [ ! -f "$PAUSED_FLAG" ]; then
      # Pause the pipeline; sentinel file is read by run_producer / run_workers / run_deploy.
      mkdir -p "$RUNTIME_DIR" 2>/dev/null || true
      printf 'paused_at=%s reason=health_check_red status=%s url=%s\n' \
        "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$status_code" "$url" \
        > "$PAUSED_FLAG" 2>/dev/null || true
      alert "auritale health: PAUSED" \
            "Pipeline paused after ${next} consecutive bad ticks (status=${status_code}, url=${url})."
    fi
    ;;
esac
