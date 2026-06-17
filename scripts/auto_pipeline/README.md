# auto_pipeline — Operator Runbook

Shell + cron orchestration layer for the auritale writer auto-pipeline
(Phase A). All scripts here are thin wrappers around `writer-jobs` and
`writer-publish` CLIs. The real Python logic lives in
`src/plugins_writer/writer_jobs/` and `src/plugins_writer/writer_publish/`.

For the full design see
`src/plugins_writer/writer_publish/docs/auto_pipeline.md`.

## Files

| File | Purpose |
|---|---|
| `lib/common.sh` | Sourced helpers: `log`, `acquire_lock`, `release_lock`, `alert`, `activate_venv`, `load_envrc`, `paused`. |
| `envrc.example` | Operator template for `.envrc` (secrets + tunables). |
| `run_producer.sh` | Cron tick: producer enqueue loop. Phase A placeholder. |
| `run_workers.sh` | Cron tick: worker process supervisor. Phase A placeholder. |
| `run_feedback_sync.sh` | Cron tick: `writer-jobs feedback-sync` (pull + push). |
| `run_deploy_queue.sh` | Cron tick: discover & deploy publishable books. |
| `run_health_check.sh` | Cron tick: probe `/health`, pause pipeline on red. |
| `sweep_stale.sh` | Cron tick: `writer-jobs sweep-stale`. |
| `install_cron.sh` | Idempotent crontab installer. |

## Install (first time)

```bash
# 1. Copy the envrc template and fill in real values.
cp scripts/auto_pipeline/envrc.example .envrc
chmod 600 .envrc
$EDITOR .envrc

# 2. Make sure the venv exists at .venv/ and writer-jobs is on PATH.
.venv/bin/pip install -e .
.venv/bin/writer-jobs migrate

# 3. Verify wiring with a dry run.
scripts/auto_pipeline/run_producer.sh
scripts/auto_pipeline/run_feedback_sync.sh
scripts/auto_pipeline/run_health_check.sh
tail -n 50 /var/log/auritale_writer/run_*.log

# 4. Install the cron entries.
scripts/auto_pipeline/install_cron.sh           # interactive
# or
scripts/auto_pipeline/install_cron.sh --yes     # non-interactive

# 5. Confirm.
crontab -l | grep auritale-writer-pipeline
```

## Verify

After install:

```bash
# Should list six managed entries.
crontab -l | grep auritale-writer-pipeline

# Check each script can run standalone.
scripts/auto_pipeline/run_producer.sh
scripts/auto_pipeline/run_workers.sh
scripts/auto_pipeline/run_feedback_sync.sh
scripts/auto_pipeline/run_deploy_queue.sh
scripts/auto_pipeline/run_health_check.sh
scripts/auto_pipeline/sweep_stale.sh

# Logs land in $AUTO_PIPELINE_LOG_DIR (default /var/log/auritale_writer).
ls -lh /var/log/auritale_writer/
```

## Pause + resume

```bash
# Pause everything (producer/workers/deploy/feedback-sync skip on next tick).
touch /tmp/auritale_writer/PAUSED

# Resume.
rm /tmp/auritale_writer/PAUSED
```

The health check creates the PAUSED flag automatically when `/health` is
red for `AURITALE_HEALTH_FAIL_THRESHOLD` consecutive ticks (default: 3) and
removes it on the first OK response.

## Troubleshooting

### "cron is firing but nothing happens"

1. Check the per-script log: `tail -f /var/log/auritale_writer/run_producer.log`.
2. Verify `.envrc` is at the repo root and `chmod 600`.
3. Verify the venv: `ls -l .venv/bin/activate` (or `.venv/Scripts/activate` on Windows).
4. Verify `writer-jobs` is on the venv PATH: `.venv/bin/writer-jobs stats`.

### "deploy queue can't find the deploy helper"

The script logs `deploy helper import failed: ...` and alerts. Reasons:

- Agent 2's deploy.py hasn't shipped yet → wait for the next pull.
- `pip install -e .` not run after pulling → reinstall.
- Python is the system python rather than the venv → `activate_venv`
  failed; check `.venv/` exists.

### "writer-jobs feedback-sync alerts immediately"

Most common cause: `AURITALE_WRITER_KEY` empty in `.envrc`. The script
refuses to call the API without it. Fix the envrc, re-run by hand to confirm.

### "I want to disable a single cron entry temporarily"

`crontab -e` and comment the offending line (don't delete it — `install_cron.sh`
will replace it on the next run). Or remove the managed marker comment on the
line above to remove the entry from the next idempotent install.

Cleanest: `scripts/auto_pipeline/install_cron.sh --uninstall` removes ALL
managed lines. Run `install_cron.sh` again later to restore them.

### "I want to test the pause flow"

```bash
# Simulate red health.
echo 503 > /tmp/auritale_writer/health_state
# Run the health check 3 times.
scripts/auto_pipeline/run_health_check.sh
scripts/auto_pipeline/run_health_check.sh
scripts/auto_pipeline/run_health_check.sh
ls /tmp/auritale_writer/PAUSED  # should exist
```

(Note: real flow needs `/health` actually returning non-200. The above
shortcut only sets the counter; the script will still re-probe the URL.)

## Logs

- Per-script log: `$AUTO_PIPELINE_LOG_DIR/<script>.log`
- All scripts mirror to stderr so cron mail captures it too.

## Locking

Every script acquires a per-name flock at `$AUTO_PIPELINE_RUNTIME_DIR/<name>.lock`
on fd 9. A second invocation while the first is still running exits 0
silently — cron will simply try again on the next schedule.

This makes the cron schedule safe even if a tick is slow:

- `run_producer.sh` every 15m
- `run_workers.sh` every 5m
- `run_feedback_sync.sh` every 30m
- `run_deploy_queue.sh` every 15m
- `run_health_check.sh` every 5m
- `sweep_stale.sh` every 20m

## Secrets

ALL secrets live in `.envrc` at the repo root, never in the scripts.

Required:
- `AURITALE_WRITER_KEY` — X-Writer-Key header for the feedback API.

Recommended:
- `AURITALE_DEPLOY_SSH_KEY` — dedicated SSH key for `rsync` to `api1.auritale.com`.
- `AURITALE_ALERT_MAIL` — operator address for failure mail. Without it, failures
  log but don't email.

See `envrc.example` for the full list.

## Phase A vs Phase B

| Concern | Phase A (this) | Phase B |
|---|---|---|
| Producer enqueue loop | Placeholder, runs `writer-jobs stats` | Real launcher/feedback/commission triage |
| Worker process | Placeholder, logs "no worker yet" | Programmatic v4 pipeline runner |
| Feedback sync | Calls `writer-jobs feedback-sync` (Agent 1 CLI) | Same; adds richer triage routing |
| Deploy queue | Iterates `select_books_needing_deploy` → `writer-publish deploy-book` | Same; adds batch + canary policies |
| Health check | Probes `/health`; pause/resume sentinel | Same; richer health surface |
| Sweep stale | Calls `writer-jobs sweep-stale` | Same |
| Cron installer | Idempotent | Stable for Phase B |

Phase B builds on Phase A without changes to this directory.

## Quick reference: cron entry markers

`install_cron.sh` tags each managed line with:

```
# auritale-writer-pipeline:<name>
<schedule> <abs path to script>
```

Names: `producer`, `workers`, `feedback_sync`, `deploy_queue`,
`health_check`, `sweep_stale`. Re-running the installer rewrites these
in place; non-managed crontab lines are preserved untouched.
