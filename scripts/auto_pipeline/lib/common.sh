#!/usr/bin/env bash
# scripts/auto_pipeline/lib/common.sh
#
# Shared shell library for the auto-pipeline runbook scripts.
# Sourced by every entry script (run_producer.sh, run_workers.sh, ...).
#
# Provides:
#   - REPO_DIR             absolute path to the AgentSystem repo root
#   - SCRIPT_NAME          basename of the calling script (sans .sh), for logs/locks
#   - LOG_DIR              directory for log files (AUTO_PIPELINE_LOG_DIR or default)
#   - RUNTIME_DIR          ephemeral directory for locks + state files
#   - PAUSED_FLAG          path to the pause sentinel (health-check creates it)
#   - load_envrc           sources .envrc from repo root if present
#   - activate_venv        activates the project venv (.venv/bin/activate or Scripts)
#   - log <msg>            timestamped line to LOG_DIR/<SCRIPT_NAME>.log and stderr
#   - acquire_lock <name>  flock-based; exits 0 (silently) if held — cron-friendly
#   - release_lock <name>  releases the named lock (also released on script exit)
#   - alert <subj> <body>  emails operator if AURITALE_ALERT_MAIL is set
#   - paused               returns 0 if PAUSED_FLAG exists, else 1
#
# Conventions:
#   - All scripts that source this file should `set -euo pipefail` themselves
#     BEFORE sourcing (we don't enforce it here to stay non-invasive).
#   - Lock fd 9 is reserved for flock. Don't reuse it.
#
# shellcheck shell=bash
# shellcheck disable=SC2155

# ---------- repo root ----------------------------------------------------
# common.sh lives at <repo>/scripts/auto_pipeline/lib/common.sh.
# Resolve repo root via three parent dirs from BASH_SOURCE.
__common_sh_dir() {
  local src="${BASH_SOURCE[0]}"
  # readlink -f is GNU; fall back to a python one-liner if missing.
  if command -v readlink >/dev/null 2>&1 && readlink -f "$src" >/dev/null 2>&1; then
    dirname "$(readlink -f "$src")"
  else
    python3 -c "import os,sys; print(os.path.dirname(os.path.realpath(sys.argv[1])))" "$src"
  fi
}

LIB_DIR="$(__common_sh_dir)"
SCRIPTS_DIR="$(dirname "$LIB_DIR")"
AUTO_PIPELINE_DIR="$SCRIPTS_DIR"
REPO_DIR="$(dirname "$(dirname "$SCRIPTS_DIR")")"
export REPO_DIR LIB_DIR AUTO_PIPELINE_DIR

# ---------- script identity ---------------------------------------------
# Best effort: callers can override SCRIPT_NAME before sourcing.
if [ -z "${SCRIPT_NAME:-}" ]; then
  # ${0} is the calling script; strip path + .sh suffix.
  __caller="$(basename "${0}")"
  SCRIPT_NAME="${__caller%.sh}"
fi
export SCRIPT_NAME

# ---------- log + runtime dirs ------------------------------------------
LOG_DIR="${AUTO_PIPELINE_LOG_DIR:-/var/log/auritale_writer}"
RUNTIME_DIR="${AUTO_PIPELINE_RUNTIME_DIR:-/tmp/auritale_writer}"
PAUSED_FLAG="$RUNTIME_DIR/PAUSED"
HEALTH_STATE_FILE="$RUNTIME_DIR/health_state"
export LOG_DIR RUNTIME_DIR PAUSED_FLAG HEALTH_STATE_FILE

# Create dirs lazily; ignore failures (logged via stderr) so cron still proceeds.
mkdir -p "$LOG_DIR" 2>/dev/null || true
mkdir -p "$RUNTIME_DIR" 2>/dev/null || true

# ---------- envrc + venv -------------------------------------------------
load_envrc() {
  # Source <REPO_DIR>/.envrc if it exists. Expects shell-compatible KEY=VALUE
  # lines (or full `export FOO=bar`). Comments + blank lines tolerated.
  local envrc="${1:-$REPO_DIR/.envrc}"
  if [ -f "$envrc" ]; then
    # `set -a` exports every variable assignment until `set +a`.
    set -a
    # shellcheck disable=SC1090
    . "$envrc"
    set +a
  fi
}

activate_venv() {
  # Try .venv/bin/activate (POSIX) then .venv/Scripts/activate (Windows Git-Bash).
  local activate_posix="$REPO_DIR/.venv/bin/activate"
  local activate_win="$REPO_DIR/.venv/Scripts/activate"
  if [ -f "$activate_posix" ]; then
    # shellcheck disable=SC1090
    . "$activate_posix"
  elif [ -f "$activate_win" ]; then
    # shellcheck disable=SC1090
    . "$activate_win"
  else
    log "WARN no venv found at $activate_posix or $activate_win; proceeding with system python"
    return 1
  fi
}

# ---------- logging ------------------------------------------------------
log() {
  # Timestamped log line. Writes to per-script log + stderr (so cron mail catches it).
  local ts msg
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  msg="${ts} [${SCRIPT_NAME}] $*"
  printf '%s\n' "$msg" >&2
  printf '%s\n' "$msg" >> "$LOG_DIR/${SCRIPT_NAME}.log" 2>/dev/null || true
}

# ---------- locking ------------------------------------------------------
# We use flock with file descriptor 9. The lock file lives in RUNTIME_DIR.
# Idempotent: release_lock is a no-op if not held. On script exit the FD is
# closed automatically by the shell, releasing the kernel lock.
acquire_lock() {
  local name="${1:?acquire_lock requires a name}"
  local lockfile="$RUNTIME_DIR/${name}.lock"

  if ! command -v flock >/dev/null 2>&1; then
    log "WARN flock not available; skipping lock for ${name}"
    return 0
  fi

  # Open lockfile on fd 9. The shell keeps fd 9 open for the duration.
  exec 9>"$lockfile"
  if ! flock -n 9; then
    log "lock ${name} held by another instance; exiting cleanly"
    exit 0
  fi
  # Record PID for debugging — non-blocking.
  printf '%s\n' "$$" >&9 2>/dev/null || true
}

release_lock() {
  local name="${1:?release_lock requires a name}"
  # Close fd 9 → kernel releases the flock. Lockfile is left on disk on purpose
  # (so operators can `ls /tmp/auritale_writer` and see which lockfiles exist).
  exec 9>&- 2>/dev/null || true
  # `name` is referenced only for log/debug parity.
  : "$name"
}

# ---------- alerts -------------------------------------------------------
alert() {
  # alert "<subject>" "<body>"
  # Sends mail to AURITALE_ALERT_MAIL if set + `mail` is available. Otherwise
  # logs only — never fails the caller.
  local subject="${1:-auritale-pipeline alert}"
  local body="${2:-}"

  log "ALERT: ${subject}"
  if [ -n "$body" ]; then
    log "ALERT body: ${body}"
  fi

  if [ -z "${AURITALE_ALERT_MAIL:-}" ]; then
    return 0
  fi
  if ! command -v mail >/dev/null 2>&1; then
    log "WARN mail command not available; alert not delivered"
    return 0
  fi

  printf '%s\n' "$body" | mail -s "$subject" "$AURITALE_ALERT_MAIL" \
    || log "WARN mail delivery failed"
}

# ---------- pause flag ---------------------------------------------------
paused() {
  [ -f "$PAUSED_FLAG" ]
}
