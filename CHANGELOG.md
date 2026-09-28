# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- The log viewer and the SSH machine panel are admin-only (rules in
  `config/config.yaml`): the logs carry every user's prompts, and the SSH panel
  runs commands on the configured hosts.
- `workspace_file_ops` -- the whole checkout, `config/secrets.env` and
  `data/users.db` included -- ships disabled; no shipped agent used it.
- A terminal with a `security.whitelist` runs every command in its configured
  working directory and environment: it neither offers nor accepts `cwd` and
  `env_vars` (`error_type: ConfiguredOnly`), refuses control characters before
  it matches, and keeps the shell line to one command whatever
  `allow_command_chains` says. A whitelist checks the command string only, and
  both let the model change what the allowed command does.
- `allow_command_chains: false` now keeps the shell line to one command: a line
  break, `;`, `&&`, `||`, `|`, `&`, a backtick, `$(`, `<(` or `>(` is refused
  anywhere in the command, quoted or not. Before, only `&&`, `||` and `;` were
  looked at, and most such chains still passed. The check is lexical; the
  command stays exactly one only together with a whitelist that names the
  program.
- `state_graph_terminal` names its interpreter (`python`, `python3`, `py`, the
  checkout's `.venv` interpreter by its relative path), separates words by
  spaces only and starts in the directory the server runs from -- the checkout,
  as every relative path of the configuration assumes; before, any program
  whose name ended in `py` passed.
- A tool server's generic dispatcher no longer calls private methods
  (`<server>__<method>`); no schema names one.
- The API does not start with an empty or short JWT signing key (an unset
  `${AUTH_SECRET_KEY}` used to sign every token with an empty key), and logs an
  error for a published one; `auth.reject_default_secret_key: true` refuses it.
- The systemd unit template runs the server as its own user on
  `127.0.0.1:8000`, with secrets in an environment file; it no longer runs as
  root on a fixed network address.
- `.gitignore` covers `.env` files, private keys, credential files and local
  configuration.
- Per-agent role gate: `metadata.min_role` (`guest`/`user`/`admin`) decides who
  may run an agent -- over HTTP (`/run`, `/events`, `/chat/command`,
  `POST /api/sessions`; `GET /agents`, the tool listings and `/chat/commands`
  hide it; refusing an agent answers like an unknown one, except the default
  agent on `/run`/`/events` without a name and `POST /api/sessions`, which answer
  403), for sub-agents
  (`error_type: agent_role_gate`), agents called as tools, stategraph, woken
  sessions and every tool the agent serves. A run nobody can be named for is
  judged as `anonymous`: refused unless anonymous access is enabled with a
  sufficient role; the sub-agent manager and an agent's own tools refuse it
  outright.
- Shipped agents with a shell, `coding_cli`, `ssh_control`, checkout-wide file
  access or a tool that runs arbitrary code (`blender_execute`, `godot_script`)
  are gated at `admin`; `state_graph_agent`/`state_graph_agent_ui` at `user`.
- An agent run as a tool no longer acts as another user through a session id it
  holds for that user: tools run for the run's registered user, a run whose user
  differs from the session's stored user is refused (an admin's too, and a
  session held for `anonymous` too, as `POST /run` refuses another user's
  session), and a `session_id` in a model's tool arguments no longer picks the
  session.
- `cli_user` counts as the local operator only inside `agent-cli`/`agent-run`,
  and only while no account of that name exists.
- A session whose agent its user may not run is not woken; it used to start up
  to three refused `agent-cli` runs per message.

### Added

- `Dockerfile`, `.dockerignore` and `docker-compose.yml` to run the API server
  in a container: non-root user, CPU-only PyTorch, `config/` mounted from the
  checkout, `data/`, `logs/` and model caches in named volumes, port published
  on `127.0.0.1` only.
- GitHub Actions CI: ruff, mypy (non-blocking) and a network-free subset of the
  test suite on Python 3.11, 3.12 and 3.14, plus an image build with a health check.
- `CONTRIBUTING.md`, `SECURITY.md`, this changelog, issue forms and a pull
  request template.

### Changed

- The ruff rule set is named in `pyproject.toml` (`E4`, `E7`, `E9`, `F`, ruff's
  default before 0.16), so a newer ruff's wider default does not apply.
- `asyncio.iscoroutinefunction` (deprecated in Python 3.14) is gone from the
  code: tool dispatch and the cancellation cleanup call the method or callback
  and await what it returns when that is awaitable, the context engineer's callback
  check uses `inspect.iscoroutinefunction`. `pytest.ini` ignores the
  deprecation (and google-genai's `_UnionGenericAlias` one) only where
  fastapi, starlette, chromadb and google-genai raise it.

### Fixed

- `agent-api` loads the config `AGENT_CONFIG_PATH` names, as `agent-cli` and
  `agent-run` do; `/health` reads the config the server was started with.
- A tool method or cleanup callback that returns an awaitable without being a
  coroutine function (an object with an async `__call__`, a lambda around an
  async call) is awaited; the tool dispatch used to return the coroutine as the
  tool's result, the cleanup dropped it.
- `agent-run --list-sessions` lists the session store `AGENT_SESSION_STORAGE_PATH`
  names, the one its runs use.
- `config/secrets.env.example` has its placeholder keys commented out: a copied
  template no longer sets `sk-or-v1-...` as a key, so a key left unset is
  named in the startup warning instead of failing later with a 401.
- The API's early log (written before the config is loaded) goes to
  `logs/api.log` in the working directory -- the file the shipped config names
  -- and no longer into the source tree.

## [0.7.0]

The version this changelog starts from and the first version published as
open source. Its release date is set when the version is tagged. Changes
before it are not summarised here; the git history has them.
