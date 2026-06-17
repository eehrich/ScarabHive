#!/usr/bin/env bash
# scripts/auto_pipeline/install_cron.sh
#
# Idempotent crontab installer for the auto-pipeline scripts.
#
# How idempotency works:
#   Each managed line carries a marker comment:
#     # auritale-writer-pipeline:<name>
#   We rewrite the user's crontab by stripping every existing managed line
#   (matched by marker) and appending fresh ones. Running twice in a row
#   yields an identical crontab — no duplicates.
#
# Usage:
#   scripts/auto_pipeline/install_cron.sh             # interactive (asks)
#   scripts/auto_pipeline/install_cron.sh --yes       # non-interactive
#   scripts/auto_pipeline/install_cron.sh --uninstall # remove managed lines
#   scripts/auto_pipeline/install_cron.sh --print     # print proposed crontab
#   scripts/auto_pipeline/install_cron.sh --dry-run   # show diff only
#
# Schedules pulled from envrc if set; otherwise the spec defaults are used.

set -euo pipefail

SCRIPT_NAME="install_cron"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

load_envrc

MARKER_PREFIX="# auritale-writer-pipeline:"

# Defaults match the spec; envrc may override.
CRON_PRODUCER="${AUTO_PIPELINE_CRON_PRODUCER:-*/15 * * * *}"
CRON_WORKERS="${AUTO_PIPELINE_CRON_WORKERS:-*/5 * * * *}"
CRON_FEEDBACK_SYNC="${AUTO_PIPELINE_CRON_FEEDBACK_SYNC:-*/30 * * * *}"
CRON_DEPLOY_QUEUE="${AUTO_PIPELINE_CRON_DEPLOY_QUEUE:-*/15 * * * *}"
CRON_HEALTH_CHECK="${AUTO_PIPELINE_CRON_HEALTH_CHECK:-*/5 * * * *}"
CRON_SWEEP_STALE="${AUTO_PIPELINE_CRON_SWEEP_STALE:-*/20 * * * *}"

# (name, schedule, script_basename)
ENTRIES=(
  "producer|${CRON_PRODUCER}|run_producer.sh"
  "workers|${CRON_WORKERS}|run_workers.sh"
  "feedback_sync|${CRON_FEEDBACK_SYNC}|run_feedback_sync.sh"
  "deploy_queue|${CRON_DEPLOY_QUEUE}|run_deploy_queue.sh"
  "health_check|${CRON_HEALTH_CHECK}|run_health_check.sh"
  "sweep_stale|${CRON_SWEEP_STALE}|sweep_stale.sh"
)

MODE="interactive"
for arg in "$@"; do
  case "$arg" in
    --yes|-y) MODE="yes" ;;
    --uninstall) MODE="uninstall" ;;
    --print) MODE="print" ;;
    --dry-run) MODE="dry_run" ;;
    --help|-h)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *)
      printf 'unknown arg: %s\n' "$arg" >&2
      exit 2
      ;;
  esac
done

build_managed_block() {
  # Emits the full block of managed cron lines to stdout.
  # Each script is invoked with an absolute path so cron's empty PATH is fine.
  for entry in "${ENTRIES[@]}"; do
    local name schedule script
    name="${entry%%|*}"
    local rest="${entry#*|}"
    schedule="${rest%|*}"
    script="${rest##*|}"
    printf '%s%s\n' "$MARKER_PREFIX" "$name"
    printf '%s %s/%s\n' "$schedule" "$SCRIPT_DIR" "$script"
  done
}

current_crontab() {
  # `crontab -l` returns 1 on "no crontab" with a message on stderr; treat as empty.
  crontab -l 2>/dev/null || true
}

filter_unmanaged() {
  # Drop both the marker comment and the line immediately following it.
  awk -v prefix="$MARKER_PREFIX" '
    BEGIN { skip_next = 0 }
    {
      if (skip_next) { skip_next = 0; next }
      if (index($0, prefix) == 1) { skip_next = 1; next }
      print
    }
  '
}

if ! command -v crontab >/dev/null 2>&1; then
  printf 'crontab command not available; cannot install cron entries.\n' >&2
  printf 'On this host, install the cron service or run the scripts via systemd timers instead.\n' >&2
  exit 3
fi

# Build the proposed new crontab.
existing="$(current_crontab)"
unmanaged="$(printf '%s\n' "$existing" | filter_unmanaged)"

# Trim trailing blank lines from the unmanaged section so we don't grow forever.
unmanaged_trimmed="$(printf '%s' "$unmanaged" | awk 'BEGIN{blank=0} {if($0==""){blank++}else{for(i=0;i<blank;i++)print "";blank=0;print}}')"

if [ "$MODE" = "uninstall" ]; then
  proposed="$unmanaged_trimmed"
else
  managed_block="$(build_managed_block)"
  if [ -n "$unmanaged_trimmed" ]; then
    proposed="${unmanaged_trimmed}"$'\n\n'"${managed_block}"
  else
    proposed="${managed_block}"
  fi
fi

# Ensure single trailing newline.
proposed="$(printf '%s\n' "$proposed" | awk 'NF{f=1} f' | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}')"
proposed="${proposed}"$'\n'

if [ "$MODE" = "print" ]; then
  printf '%s' "$proposed"
  exit 0
fi

if [ "$MODE" = "dry_run" ]; then
  printf '=== current crontab ===\n%s\n' "$existing"
  printf '=== proposed crontab ===\n%s\n' "$proposed"
  exit 0
fi

if [ "$MODE" = "interactive" ]; then
  printf 'Proposed crontab:\n'
  printf '%s\n' "$proposed"
  printf 'Install this crontab? [y/N] '
  read -r answer
  case "$answer" in
    y|Y|yes|YES) ;;
    *) printf 'aborted.\n'; exit 0 ;;
  esac
fi

# Atomic-ish install: write to temp, validate, then crontab <file.
tmpfile="$(mktemp -t auritale-cron.XXXXXX)"
trap 'rm -f "$tmpfile"' EXIT
printf '%s' "$proposed" > "$tmpfile"

if ! crontab "$tmpfile"; then
  printf 'crontab install failed (see stderr above)\n' >&2
  exit 4
fi

if [ "$MODE" = "uninstall" ]; then
  log "cron entries removed"
  printf 'cron entries removed.\n'
else
  log "cron entries installed/refreshed"
  printf 'cron entries installed.\n'
fi
