#!/usr/bin/env bash
# scripts/auto_pipeline/run_deploy_queue.sh
#
# Cron entry: deploy publishable+updated books.
#
# Strategy:
#   1. Use a Python one-liner against plugins_writer.writer_publish.deploy
#      to list books needing deploy (select_books_needing_deploy).
#   2. For each book_id, call: writer-publish deploy-book --book-id N
#   3. Per-book failures alert + log; the loop continues to the next book so
#      one bad bundle doesn't stall the queue.
#
# Phase A status: USES REAL CLI. The deploy-book subcommand is added by
# Agent 2; if it isn't available yet at cron-time, this script alerts once
# and exits cleanly.

set -euo pipefail

SCRIPT_NAME="run_deploy_queue"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

acquire_lock "$SCRIPT_NAME"

load_envrc

if paused; then
  log "PAUSED flag present at $PAUSED_FLAG; skipping deploy queue"
  exit 0
fi

if ! activate_venv; then
  alert "auritale deploy-queue: no venv" "activate_venv failed at $REPO_DIR/.venv"
  exit 0
fi

if [ -z "${AURITALE_DEPLOY_HOST:-}" ]; then
  log "AURITALE_DEPLOY_HOST unset; falling back to deploy.py default"
fi

log "deploy-queue tick start"

# Discover candidate book_ids via the deploy helper. Stdout is one id per line.
# We capture stderr separately so transient SELECT errors land in the log.
candidates_script='
import sys, sqlite3
from pathlib import Path
try:
    from plugins_writer.writer_publish.deploy import select_books_needing_deploy
except Exception as exc:  # pragma: no cover - early-Phase-A fallback
    sys.stderr.write(f"deploy helper import failed: {exc}\n")
    sys.exit(2)
db = Path("data/writer/books.db")
if not db.exists():
    sys.stderr.write(f"books.db not found at {db}\n")
    sys.exit(3)
try:
    rows = select_books_needing_deploy(str(db))
except Exception as exc:
    sys.stderr.write(f"select_books_needing_deploy failed: {exc}\n")
    sys.exit(4)
for row in rows:
    # row may be a dataclass or a dict; tolerate both.
    bid = getattr(row, "id", None) or getattr(row, "book_id", None)
    if bid is None and isinstance(row, dict):
        bid = row.get("id") or row.get("book_id")
    if bid is not None:
        print(int(bid))
'

if ! candidates="$(python -c "$candidates_script" 2>>"$LOG_DIR/${SCRIPT_NAME}.log")"; then
  alert "auritale deploy-queue: candidate discovery failed" \
        "see $LOG_DIR/${SCRIPT_NAME}.log for traceback"
  log "candidate discovery failed; aborting tick"
  exit 0
fi

if [ -z "$candidates" ]; then
  log "no books need deploy; tick end"
  exit 0
fi

count=0
failures=0
while IFS= read -r book_id; do
  [ -z "$book_id" ] && continue
  count=$((count + 1))
  log "deploying book_id=$book_id"
  if ! out="$(writer-publish deploy-book --book-id "$book_id" 2>&1)"; then
    failures=$((failures + 1))
    alert "auritale deploy-queue: deploy failed book_id=$book_id" "$out"
    log "deploy-book failed for book_id=$book_id: $out"
    continue
  fi
  log "deploy-book ok book_id=$book_id: $out"
done <<< "$candidates"

log "deploy-queue tick end (attempted=$count failures=$failures)"
