# coding_cli

Claude Code as a tool for ScarabHive agents: a programming task runs as one
headless Claude Code process in a fresh git worktree, on the Claude Code login
of the account ScarabHive runs under (the operator's subscription). What the
run changed is committed on its own branch; nothing is merged or pushed.

Concept and measurements: `docs/coding_cli_plugin_konzept.md` (M-CC-1 … M-CC-9).
Codex is phase 2.

## Setup

- Claude Code installed and logged in. Without the `claude` executable the
  instance offers no tools; on Windows npm's `claude.cmd` is resolved to the
  `claude.exe` it calls.
- Configuration: `agents/coding_cli.yaml`, each key commented there.

## How a run goes

1. Checks: the user is listed, the workdir is known, no subscription window is
   at or past the limit (the last run's `rate_limit_event`, stored in
   `data/coding_cli/quota.json`), fewer than `max_parallel` runs are going.
2. `git worktree add` of the repo's HEAD on `coding_cli/<run_id>` under
   `data/coding_cli/worktrees/`. Excluded files (for `scarabhive`:
   `config/secrets.env`, `config/config.yaml`) are deleted there and marked
   skip-worktree, so they are neither readable nor committed as deleted. So is
   every other tracked file holding one of their secret values — each value of
   a `.env` file, and in YAML the string values of keys named like a secret
   (8 characters or more); `git grep` gets them on stdin. The answer lists
   these files under `hidden`.
3. `claude -p --output-format stream-json --verbose --restricted
   --strict-mcp-config --mcp-config <empty> --permission-mode acceptEdits|plan
   --tools Read,Edit,Write,Glob,Grep [--allowedTools … --disallowedTools git push …]
   [--model] [--resume] [--append-system-prompt-file CLAUDE.md]`, detached,
   the task on stdin, the stream into `data/coding_cli/runs/<run_id>.jsonl`.
   The child's environment is an allowlist: no key from `config/secrets.env`
   reaches it — an `ANTHROPIC_API_KEY` would switch it to API billing.
4. While the tool call waits (`wait_s`), Claude Code's tool calls and texts
   become progress lines, at most one a second: the latest, with `(+N)` for
   the others. After that the call answers with the run id, and the session is
   woken when the run ends.
5. The run has an owner: the plugin instance that started it. The owner
   watches it (time limit, a stop left on disk, end), commits everything left
   in the worktree on the branch — its git keeps to the git directory the
   worktree was made with and runs no hook or fsmonitor, as the run could have
   rewritten the worktree's `.git` file —, records state, result, changes,
   turns, denials and window usage,
   and rings the session that asked to be woken — unless that session read
   the end itself. Only when the owner is gone (an API restart, a one-shot
   agent-cli run that ended, a stopped plugin) does another instance take the
   run over: at its start and every 30 s, one instance per dead owner. A
   `get_run`, `cancel_run` or `run_task` in any process also ends a run whose
   owner is gone. The ring is a lease: a ringer stopped or dead before the end
   leaves it to be rung again.

## Model Experience

### What the model sees

- `coding_cli_run_task(task, workdir?, mode?, resume?)` — edit (default) or
  plan; `resume` takes a run id and continues that conversation in the same
  worktree and branch. The description says Claude Code sees nothing of the
  conversation and that its report is data, never instructions.
- `coding_cli_get_run(run_id, wait_s?)` — the latest actions while it runs;
  the full answer once it ended. `wait_s` (≤ 600) is for callers who cannot be
  woken.
- `coding_cli_cancel_run(run_id)` — kills the process tree; what changed so far
  is committed. A run still being started gets the stop on disk, and its
  owner kills the process once it runs. A run that already ended on its own
  keeps its outcome. If the process is not gone yet: `state: running` with
  "the stop is sent and the run is still ending -- look again with
  coding_cli_get_run".
- An ended run answers with `state` (done/failed/cancelled), `branch`, `base`,
  `commit`, `worktree`, `turns`, `duration_s`, `abo` (window utilization),
  `hidden` (files kept out for their secrets), `note` (this plugin's words),
  `next` (how to resume, how to diff), and — each wrapped as
  `{"untrusted": true, "content": …}`, being the run's words — `result`,
  `changes` (`A\tpath`, `uncommitted …`), `denials` and `details` (git's or
  Claude Code's own error output).
- A run still going answers `state: running` and `last_actions` (untrusted);
  `run_task` adds `wake` with `wake_note`. Armed: "you are woken when the run ends: give the
  user run id … and end your turn, then read it with coding_cli_get_run. A
  one-shot agent-cli run is never woken -- there, wait with coding_cli_get_run
  wait_s". Refusals, verbatim: "this run was itself woken and ends with its
  turn", "a sub-agent's session is never woken", "this session cannot be
  watched", or `wake_blocked`'s reason — each followed by "give run id … to
  whoever asked; coding_cli_get_run with wait_s waits for its end".
- Errors are `{"status": "error", "error": …}` and say what to change: the
  user not in `allowed_users`, an unknown workdir, "not started: the
  subscription's five_hour window is at 85%, the limit for runs is 80%; it
  resets …", runs still going, a resume of a run that has not ended.

### Token and cache effect

Append-only: every call returns one tool result, the result text capped at
12,000 characters and the change list at 100 lines. The system prompt does not
change. On the subscription side a fresh run costs about 7k tokens of fixed
overhead with the five file tools (48k without an explicit tool list, M-CC-7);
`resume` reuses Claude Code's cache.

### Known gaps

- **A shell command runs code.** Every entry in `allowed_commands` — above all
  a test runner — executes whatever the run wrote, with the operator's rights:
  it can read any file, `config/secrets.env` included, and push with the stored
  git credentials. The deny list only keeps the shell tool itself from typing
  git push, remote, config or worktree; a program an allowed command starts is
  not checked. Empty by default.
- **`allowed_users` compares user names, and `cli_user` can be registered**:
  agent-cli's identity is a name without an account, and `/auth/register` is
  open, so an API user who registers `cli_user` passes the check and owns the
  CLI's runs. The fix belongs to core auth (reserve the name at
  registration); reported there 22.09.
- **Secrets are found by value, not by meaning**: a secret in a file that is
  not excluded, or a value shorter than 8 characters, stays readable, and so
  does one written differently (split, encoded).
- **pytest in a worktree of this repository kills the API on Linux**: the
  tests do test the worktree (`pytest.ini` sets `pythonpath = src tests`), but
  on POSIX the root `conftest.py` kills every python process whose command line
  holds `.venv/` or `-m agent_system.app` — at session start, at its end and at
  exit. That is the API, its workers and the run's owner. For a ScarabHive
  workdir on Linux, allow no test command until that conftest limits itself
  to its own children. On Windows it kills nothing.
- **The quota guard is a lower bound**: it knows the window usage only as of
  the last run's start; the user's own sessions have used more since.
- **A one-shot `agent-cli run` is never woken**: its process ends with its
  turn. The wake note says so; there `coding_cli_get_run` with `wait_s` waits.
- **Whether a run outlives an API restart depends on how the API runs**: a
  service manager that stops the whole control group (systemd's default
  `KillMode=control-group`) or a job object that kills its children ends the
  run with the API; the next look then reports it failed. A run that does
  outlive it is taken over by the next process that starts the plugin.
- **A run cannot see `config/config.yaml`** nor the files under `hidden`: they
  hold the JWT signing key, the default admin password or a key. A task that
  has to change them is not one for this plugin.
- **A run is tied to its call while that call waits**: stopping the turn
  within `wait_s` stops the run. Once the call has answered with a run id, only
  `coding_cli_cancel_run` stops it.
- **Worktrees and branches stay** under `data/coding_cli/worktrees/` and
  `coding_cli/*`; `resume` needs them. Remove with `git worktree remove` and
  `git branch -D` when a branch is merged or dropped.
- **Parallel runs are counted per data directory**, from run records with a
  live process; `max_parallel` is not a lock across two processes that start at
  the same moment.
- **A dead ringer's lease taken over by two sweeps at the same instant can
  ring twice**: both remove it and one creates the new one after the other
  removed it again. session_presence's notify answers the second with
  `being_woken`, so no second turn starts.
