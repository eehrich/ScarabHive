# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- A fresh install no longer needs a C compiler: `install.sh` stopped in
  `pip install -e .` on a Mac without cairo, because pycairo (reportlab's
  cairo backend, which draws SVG layers in `image_compose`) has wheels for
  Windows only. It moved to `requirements/optional.txt`, fed by a plugin's
  new `optional_dependencies` and installed best effort after the core.
  `install.sh` checks for cairo, a compiler and Python's headers and installs
  what is missing (cairo with Homebrew on macOS, everything with `apt-get`
  through sudo on Debian/Ubuntu; `SCARABHIVE_NO_SYSTEM_PACKAGES=1` leaves the
  system alone) and otherwise names the command and goes on
  without SVG layers. An svg layer on a host without the backend answers with
  that fix instead of reportlab's "cannot import desired renderPM backend",
  and the plugin warns at start. `install.sh` is executable in the
  repository.

## [0.7.0] - 2026-10-06

The first version published as open source. Changes before it are not
summarised here; the git history has them.

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
  403), on the OpenAI-compatible API (`openai_api`: a model the caller may not
  run is not listed and answers 404 `model_not_found`), for sub-agents
  (`error_type: agent_role_gate`), agents called as tools, stategraph, woken
  sessions and every tool the agent serves. The run's own refusal carries an
  `error_type` too (`agent_role_gate`, `foreign_session`): nothing of a refused
  run is saved, and the OpenAI API answers it as 404/403, not as a server
  error. A run nobody can be named for is
  judged as `anonymous`: refused unless anonymous access is enabled with a
  sufficient role; the sub-agent manager and an agent's own tools refuse it
  outright.
- Shipped agents with a shell, `coding_cli`, `ssh_control`, checkout-wide file
  access or a tool that runs arbitrary code (`blender_execute`, `godot_script`)
  are gated at `admin`; `state_graph_agent`/`state_graph_agent_ui` at `user`.
  **Operator-visible:** after the next restart, accounts below `admin` no
  longer see or run these agents (`blender_agent`,
  `claude_code_agent`, `coder`, `coder_explorer`, `coder_reviewer`,
  `coder_tester`, `file_ops_test_agent`, `gamedev`, `gamedev_tester`,
  `godot_agent`, `skills_agent`, `skills_agent_multimodal`, `sysadmin_agent`).
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
- `auth.registration` decides what `POST /auth/register` -- reachable without
  login -- may do: `enabled: false` refuses it (403), `require_approval: true`
  creates the account inactive until an admin activates it, `default_role` is
  `user` or `guest`. The defaults keep what the endpoint did: open, active at
  once, role `user`.
- The shipped `config/config.yaml` holds a self-registered account for an
  admin's approval (`auth.registration.require_approval: true`).
  **Operator-visible:** after the next restart, an account created through
  `POST /auth/register` starts inactive and cannot log in until an admin
  activates it; existing accounts are not touched.
- bubblewrap (the Linux process sandbox) runs a command with `--new-session`
  (a confined process can no longer push keystrokes into the terminal it was
  started from), `--die-with-parent` and `--unshare-pid`; in `workspace-write`
  the git hooks and config of the workspace's repository are read-only on both
  backends, so the next unconfined `git` does not run what a confined process
  put there.
- `godot_setup` refuses a quote, backslash or control character in `name` and
  `main_scene`, which are written into `project.godot` as quoted strings; a line
  break used to add lines of its own.

### Added

- `Dockerfile`, `.dockerignore` and `docker-compose.yml` to run the API server
  in a container: non-root user, CPU-only PyTorch, `config/` mounted from the
  checkout, `data/`, `logs/` and model caches in named volumes, port published
  on `127.0.0.1` only.
- GitHub Actions CI: ruff, mypy (non-blocking) and a network-free subset of the
  test suite on Python 3.11, 3.12 and 3.14, plus an image build with a health check.
- `CONTRIBUTING.md`, `SECURITY.md`, this changelog, issue forms and a pull
  request template.
- `openai_api` plugin (enabled in the shipped `config/plugins.yaml`): every
  agent the caller may run is a model under `/plugins/openai_api/v1` --
  Responses API (`previous_response_id` continues the conversation,
  `store: false` keeps none), stateless Chat Completions, `GET /v1/models`,
  streaming for both.
- An API key is also accepted as `Authorization: Bearer <key>`, as OpenAI
  clients send it; on plugin routes only for plugins that declare
  `accept_api_keys`.
- `tool_approval` plugin: approvals before tool calls -- `ask` (the person
  watching the run answers in the web chat), `auto` or `off`, with allow and
  deny rules on tool patterns and arguments. Off for every agent until it
  switches it on.
- `ask_user` plugin: the agent asks the person watching the run a question
  (2-4 options, multiple choice or free text) and waits for the answer; a run
  nobody watches is told at once to decide itself. Granted to `coder` (and
  `gamedev`, which merges the coder's tools), `sysadmin_agent` and
  `research_agent`.
- `file_checkpoints` plugin: what `file_ops` and `media_ops` change is
  recorded per turn; `/undo files`, `/retry files` and `/rewind [n]` put the
  files back in agent-cli and the browser. On for the coder harness.
- `otel` plugin: OpenTelemetry traces for agent runs, LLM calls and tool calls
  with the GenAI semantic conventions, optionally the GenAI client metrics.
  Off by default.
- `project_instructions` plugin: a project's `AGENTS.md` is read once per
  session from the root the agent's file tools work in and put in front of the
  model. On for the coder harness.
- A Seatbelt backend for the process sandbox (`utils/process_sandbox.py`): on
  macOS the terminal's `read-only` and `workspace-write` modes confine the
  command with `sandbox-exec` (enforcement `partial`: the user's temp directory
  stays shared). The default stays `danger-full-access`.

### Changed

- The ruff rule set is named in `pyproject.toml` (`E4`, `E7`, `E9`, `F`, ruff's
  default before 0.16), so a newer ruff's wider default does not apply.
- `asyncio.iscoroutinefunction` (deprecated in Python 3.14) is gone from the
  code: tool dispatch and the cancellation cleanup call the method or callback
  and await what it returns when that is awaitable, the context engineer's callback
  check uses `inspect.iscoroutinefunction`. `pytest.ini` ignores the
  deprecation (and google-genai's `_UnionGenericAlias` one) only where
  fastapi, starlette, chromadb and google-genai raise it.
- **Behaviour change for hook authors:** `pre_tool_call` and `post_tool_call`
  hooks now fire. They were declared and documented but never called; a
  plugin that registers one now runs around every tool call of the model and
  of a `tool_script` script (slash commands, web buttons, `tool_preload`,
  stategraph and `Agent.call_tool` stay unhooked). A pre hook may change the
  arguments or block the call (`metadata={"block": ...}`: the model reads a
  `ToolCallBlocked` error and the run goes on; `on_error: block` makes a failing
  hook block too); a post hook may change the result before it joins the
  history. See `docs/plugin_hooks.md`.
- `POST /run` without a `session_id` creates a real session, as `/events` does;
  it used to run on a throwaway one.

### Fixed

- A file rewind takes back again what an agent called as a tool changed during
  the turn: `file_checkpoints` finds the tool agent's own session below its
  caller's through the running agents, not only through the stored record, and
  a lookup made before that session was stored no longer keeps its miss.
- `otel`: the spans of an agent called as a tool carry its caller's
  conversation again as `gen_ai.conversation.id` (the top of the caller chain),
  with its own session as `session.id`; a backend grouping by conversation had
  split one conversation into pieces.
- An agent called as a tool (`Agent.call`, `<name>_execute_task`) no longer runs
  on its caller's session: it saved its own transcript into the caller's session
  file (creating a new one with its own agent name and a title from the
  sub-task, or replacing a stored one's history until the caller's next save),
  and kept every caller session in memory for the life of the process,
  throwaway ones included. It now runs on a session of its own per caller session
  and agent -- it still remembers its earlier calls there -- saved under the
  call's user below the caller's session (hidden from the session list, like a
  sub-agent's), refused to another user, and gone from memory with the caller's
  session; a throwaway caller's is never saved (if the agent starts sub-agents,
  the sub-agent manager files it as the listed "Coordinator Session" it makes
  for any parent it does not find). A person's "allow for this session"
  (tool_approval) in the caller's session still covers it, and
  `/pending?session_id=<session>` lists the questions asked at every level
  below. Sub-agents it starts hang below its session, not the caller's: the
  caller no longer sees them in its injected sub-agent list or through `poll`,
  and their wake goes to nobody. Its session carries the caller's sub-agent
  nesting budget from its first write; a call whose session cannot be stored
  with it does not run (`error_type: "tool_session_unavailable"`). One the
  person deleted is forgotten: the next call starts it afresh (it was never
  stored again until a restart). A second call while the first still runs on
  the session is answered as an error (`session_locked`), not as a "success"
  with the refusal inside. A call to an agent that runs above it already --
  itself, directly or through other agents called as tools -- is refused with
  `error_type: "recursive_call"` (on the caller's session it was refused at
  that session's lock); across a SAM or stategraph hop the sub-agent nesting
  budget bounds it (a stategraph agent activity only where a SAM above set
  one, as before). The ids stay short enough for a file name at any depth. An
  openai_api turn that is put back puts these sessions back with the
  conversation where only its own runs wrote them: a run of another request
  -- of this process or another, an agent-cli run too -- leaves one as it is
  (every run names itself in the session), and so does an append made in this
  process. Writes that name no run are not told apart and are put back with
  the rest: an /undo, a rename or a variables write, and an append made in
  another process. A run of another process still going on the session when
  the turn is put back writes it again afterwards.
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
- Session saves no longer lose a run's last exchange or an appended message:
  a run lets go of its session lock only once its last save is on disk; the
  save after `/run`, `/events`, an `openai_api` turn or a sub-agent's run
  leaves a session somebody else holds by then; an append and `/chat/undo`
  hold the agent's session lock as a writer from reading the session to their
  save, and what meets that lock waits instead of being refused; a sub-agent
  manager `continue` takes its sub-agent's session lock before it touches the
  session. A run refused at the lock says so (`error_type: "session_locked"`)
  and nothing of it is saved. Throwaway sessions (`ephemeral-*`) are never
  written.
- Per-session locks, the request-to-session mappings of ended requests (the
  last 1000 per agent are kept) and finished background sub-agent jobs (an
  hour, a day when their result could not be stored) no longer stay in memory
  for the life of the process.
- Answers to tool calls the framework rejects (malformed arguments, unknown
  tool) keep the order of the model's calls, as Gemini requires.
- Two plugin directories with a plugin or shared module of the same name no
  longer load as one module: the first directory that has the name claims it,
  a later one's is skipped with a warning.
- `file_ops` writes through a temp file of its own (`.<name>.<random>.tmp`,
  created exclusively, removed on cancel) instead of `<name>.tmp`, which could
  be a file of the user's that was overwritten and deleted; an edit keeps the
  file's mode, as a create over an existing file already did.
- `file_ops` semantic index: a document names its file relative to the indexed
  directory, not by its absolute path, which made the ranking depend on where
  the tree lies. The index state carries a document format now; an index from
  before the update is rebuilt in full once, on the first background pass.
- `coding_cli` reads the secret values of an excluded YAML file through
  `yaml_io` (libyaml when present) instead of the pure-Python `yaml.safe_load`.
- `blender` and `godot` refuse a Windows drive or share name (`C:/...`,
  `\\server\share`) on POSIX as well; it used to be read as a relative name.
  In `godot` a leading `/` is the project's root on every platform.
- The Memory Profile panel keeps the allocations of the last snapshot asked
  for; ten periodic snapshots later (under an hour at the default interval) it
  said none were recorded.

[Unreleased]: https://github.com/eehrich/ScarabHive/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/eehrich/ScarabHive/releases/tag/v0.7.0
