# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- coding_cli runs Google's Antigravity CLI (`agy`) as well as Claude Code: `cli: antigravity` on an instance.
  agy's stream is read as Claude Code's, so the tools, the branch, `resume` and `json_schema` work the same.
  A run gets a home folder of its own per user (pinned settings, nothing of the user's agy setup, no Claude
  Code variables), edits only its worktree (`--mode accept-edits`, plan mode only reads), has no shell and
  loads no MCP server. Its web search cannot be switched off, so such an instance takes only workdirs that
  say `web: true`. agy reports no subscription windows: no quota tool, no budget guard. A run records its CLI,
  and a resume stays with it.
- MCP gateway (`mcp_gateway` plugin, off by default): an outside CLI (Claude Code, Antigravity) reaches tools of
  this installation over MCP at `/plugins/mcp_gateway/mcp`. Nothing is open by configuration: a caller in the API
  process issues a token per run (`grants.issue`) naming the tools, optionally parameters fixed to one value and
  allowed values reaching into lists (read but not write), and revokes it when the run ends. Every call goes
  through the agent `mcp_gateway_runner` -- its allowlist is the ceiling, its tool hooks see every call -- with the
  run's account, session and request id. Served by the mcp SDK's own server; `config/security.yaml` opens the one
  route, which refuses any request without a live token.
- Coding CLIs as an LLM (`llm_coding_cli` plugin, providers `claude_code` and `antigravity`): a model entry names
  a coding_cli instance, and an agent's model call becomes a run of that CLI -- the conversation as one task, the
  CLI's own tool loop through its workdir's MCP gateway, its final answer as the assistant message. A second call
  in the same agent run that goes on from the last one resumes that run; cancelling the agent stops it. The
  subscription's limit blocks the link until it resets, any other failure moves the profile chain on. Agent runs
  hold their session in `current_run_session` (beside `current_run_user`), which the CLI's tool calls run in.
- Tool bindings of a run: `Agent.run_events(tool_bindings={tool pattern: {parameter: value}})` holds every call of
  a tool a pattern matches (the allowlist's patterns) to those values -- put in where the call leaves them out, the
  call refused where it asks for another, a matched tool that does not take the parameter refused. One check for
  every way a tool is called: the model's own loop, `dispatch_tool_call` (scripts, hooks) and the MCP gateway,
  whose grant `pinned` takes the same form. A model call that is a coding CLI's run hands them on as `pin`;
  `AgentCaller` passes them to the sub-agent manager (`_tool_bindings`). The bindings are the run's own: a
  sub-agent does not take over its caller's.
- coding_cli: a workdir's `gateway` (tools, `pin`, `allowed`, `read_only`) hands its runs tools of this
  installation through the MCP gateway -- a grant per run, revoked at its end; `run_task` takes the values it fixes
  as `pin`. Claude Code gets the token in its environment only, agy in its workspace's MCP file, removed before the
  commit. Beside web (for agy always) the workdir must say `read_only: true`. An instance's `env` sets values its
  runs get (the plugin's own win), and `max_result_chars` bounds the final report (default 12,000, up to 200,000)
  for a caller that takes it as its answer.
- `/events` takes `rerun_ended` (POST body or query): a job under the request_id that broke off -- failed, or
  ended without an answer, not cancelled -- runs again under that id as the same agent, instead of replaying its
  end (a finished job is kept until the process ends, so a dispatcher's retry only saw the old failure). A running
  job, one that answered and a cancelled one are joined as without it, so a dispatcher may send it every time.

### Fixed

- A model call without streaming went on running when the agent's event stream was closed or its run cancelled
  from outside: the 100 ms polls shielded the call from the cancel too. It is cancelled now -- not billed to its
  end with its answer thrown away.

## [0.7.1] - 2026-10-27

### Added

- Evals (`agent_eval` plugin, admin panel "Evals", `agent-eval` command): a suite names an agent, variants of it
  (arms: any agent_config change, another model, another prompt) and tasks with a check each -- a command,
  pytest (the share of green tests), expectations on the answer, a script or a plugin's own function. Every
  arm runs every task several times, each run a child process driving agent-run's own code in a fresh
  workspace and (by default) a fresh data directory; cost by request id from the usage tracker, also for a
  run killed at its timeout. The report compares the arms task by task: an exact sign test, how many tasks
  must move one way to decide, a t interval of the mean difference, learn and holdout tasks apart,
  regression tasks named; a complete run can be the suite's baseline. A task's setup runs once and every arm
  starts from its copy; `chain: true` runs the tasks in order on shared state. An arm that cannot run is
  refused before the first agent run. An overlay a model writes is checked separately and may only narrow
  the agent's rights. Example suite: `src/plugins/agent_eval/evals/hello/`.
- Docker in one command: `sh scripts/docker-setup.sh` (Windows:
  `scripts\docker-setup.ps1`) makes the installation's own signing key in
  `config/local.env`, names it in `config/local.yaml`, asks for an OpenRouter
  key, builds and starts the container and prints the admin's first login.
  Run again, it changes nothing that is there. In the container a published
  signing key -- the one `config/config.yaml` ships -- now stops the start,
  and the log names the script; before, `docker compose up` without the
  manual steps signed every login with it.
- Panels on phones and tablets: at 640 px and narrower (a phone, or a panel
  docked narrow beside the chat) every `.pk-table` with a head lays out as
  cards, one per row with each value beside its column's name, the head
  above them as a row of sort buttons; the page head puts its toolbar on a
  line of its own. On a touch screen wider than that a wide table scrolls in
  its own box. Before, 15 panels scrolled their whole page sideways on a
  phone, head and filters included. `data-pk-nocards` keeps a table a table.
- Forgot password: the login page mails a link to the account's address
  (`POST /auth/password-reset-request`, `POST /auth/password-reset`, until
  now placeholders that sent nothing). The link holds
  `auth.password_reset.link_minutes` (30) and works once; setting the
  password ends every login of the account. The answer is the same for any
  address and comes before the address is looked up; an account gets a mail
  every two minutes and five a day at most. The link is built on
  `auth.password_reset.public_url`, never on the request's Host, and carries
  the token in the fragment (`/login#reset=...`), out of server and proxy
  logs. New config: `auth.mail` (host, port, security, username, password,
  sender, timeout) and `auth.password_reset`; without a mail server, sender and public
  address the login page offers nothing.
- `PATCH /auth/me` asks for `current_password` to change the email too: it is
  where the reset link goes. The Settings panel asks for it once the address
  differs.
- Session folders in the sidebar. Folders sit above the sessions in none,
  and inside a folder as below it sessions group by day: Today, Yesterday
  and This week open, each month before folded, the folds kept per browser.
  The core knows no folder: a plugin declares the one its sessions belong in
  (`[session_folder]` in its plugin.toml: name, icon, the agent groups --
  `metadata.category` -- it files by itself; docs/plugin_authoring.md). A
  plugin can declare a folder such as Writer (for the agent groups Writer and
  Writer sub-agents); a stategraph machine's run session carries its `agent:`
  block's category, so a machine's runs land in its plugin's folder although
  their agent is the runner. A user makes folders of their own, changes or deletes a
  declared one (kept per user in `data/sessions/<user>/.folders.json`; one
  left as declared follows its plugin). A session moves by drag and drop or
  its folder button, without changing its day; its own choice is
  `metadata.folder` (`""` keeps it out of its group's folder). Icons come
  from the kit's sprite (26 new Lucide icons). New routes:
  `GET/PUT /api/sessions/folders`, `PUT /api/sessions/{id}/folder`.
- Metadata a session's index row carries (tags, folder, category) reaches
  the session list at once; before, a tag set through `PATCH
  /api/sessions/{id}` showed only after the session's next save.
- The header's right side is grouped by purpose: the work area first (open
  a panel, the panel area -- mirroring the sessions toggle on the left),
  then the app (help, the account last). The theme moved from its own
  header button into the account menu, as a row of its three choices with
  the one in use marked; the palette still offers it.
- Panel tables have resizable columns: every `.pk-table` with one head row
  and two columns or more gets a grip on each column's right edge but the
  last, which takes the rest (drag to set the width, double-click to fit the
  content). Once a width is set, a cell of text that does not fit ends in
  "…" and shows its whole text on hover, a cell holding blocks (prose, code)
  wraps; the widths hold through redraws and, for a table named by
  `data-pk-cols` or `data-pk-sort`, across reloads. `data-pk-noresize` opts a
  table out.
- The UI kit's look is refreshed: inputs are white fields on the light theme,
  bordered buttons, cards, number tiles and table boxes lift with a soft
  shadow, dialogs and menus with a layered one, radii are a step rounder
  (8/12 px), callouts are rounded notes and a button gives a little on press.

- `tool_preload` rules may use two placeholders taken from the user's
  message without a group of their own: `{message}` (its text) and
  `{keywords}` (the code identifiers it names -- backticked spans,
  snake_case, CamelCase, dotted names, paths -- as one escaped regex, at most
  `max_keywords`, default 8). A rule using `{keywords}` on a message that
  names none is skipped without a warning. The `coder` uses both: before its
  first LLM call of a turn it greps `coder_fs` for the identifiers and runs
  a semantic search with the message (five words or more), so the model
  starts reading instead of searching. The setting `text_from` picks the
  parts of a message the rules see, for a request that stands behind a long
  preamble the 2000-character cap would otherwise spend itself on.

### Changed

- A config reload (`agent-cli reload`, `POST /admin/reload-config`) reaches
  what an agent runs on: its whole `agent_config` (model chain, `llm_params`,
  tools, hooks, timeouts, prompt), its description and metadata, and every
  model change in `llm.yaml` -- before, it took seven scalars such as
  `max_steps`, and a changed model needed a restart. LLM clients are held by
  a hash of what they are built from and rebuilt once, only when that
  changed; a run in flight keeps its model. A profile that does not build
  fails the reload for that agent, which keeps running as it was. The report
  names a changed model (`llm: old -> new`) also when only `llm.yaml`
  changed. The same holds for machine steps with their own `llm_profile`,
  the agent a machine's `agent:` block declares, and the LLM-using hooks
  (summarizer, watchdog, continuation, tool-result summaries).
- Every agent names its group for the agent picker (`metadata.category`):
  Assistant, Development, Testing, Writer, Writer sub-agents, Video, Media,
  Research, Automation, Collaboration, Operations -- 21 of the 49 agents the
  picker lists had none and stood under "Other", the rest spelled the same
  group several ways. Set for the tool-only and private agents too;
  docs/agent_visibility.md lists the groups. A stategraph machine's `agent:`
  block takes a `category` too.
- `file_ops` and the shared path sandbox: the tools' path parameters name the
  instance's folders (`Path under data/runs, relative as written or
  absolute`) instead of asking for an "Absolute path", and refusals name them
  the same way, not as this machine's absolute paths. A read behind an
  invented root (`/workspace/data/...`, `/home/user/data/...`) reads from the
  project when the path as written cannot exist and the rest names an allowed
  folder; a change behind one is refused. Measured 7-14 Oct: 36 refusals, every one such a path.
- `file_ops`: writes to one file run one at a time (a lock per file, shared
  by the instances of a process). Parallel replace_string_in_file calls of
  one step each read the file and wrote it whole: all said success, one
  edit stayed (one-shot run: 1 of 5).
- `coding_cli`: a stored subscription state that would refuse a run and is
  over five minutes old is asked anew (one turn on haiku) before the refusal:
  only a finished run wrote it, so a weekly window that reset early refused
  every run until its old reset.
- `context_engineer`: the media passes (Pre-Layer M, duplicates, the
  event pass) are weighed with layers 1-3 under `min_reduction` and held with
  them. Measured on production agents over two weeks: 99 of 114
  partial cache hits followed a run of Pre-Layer M, ~9k tokens of images for
  a break at ~10 % of the context, each one costly. Past
  `always_compact_media_max` (20) pictures Pre-Layer M runs whatever it
  takes.
- `context_engineer` compacts only when it is worth the cache break. A run of
  layers 1-3 is tried on a copy, storing nothing, and kept when it takes
  `min_reduction` (default 0.2) of the context; a shallower one leaves the
  context as it is. Measured on a long agent run: Layer 1 ran every ~20k
  tokens of growth for 2-5 %, and each run re-billed the context at the input
  price. `/compact`, the byte limit and a context past
  `force_above_window_share` (0.85) of the model's window are kept whatever
  they take.
- `context_engineer` binds its thresholds to the window of the model a call
  goes to: `layer1_window_share` (0.55), `layer2_window_share` (0.65),
  `layer3_window_share` (0.75) and `target_window_share` (0.30) lower the
  token values where the window is small -- on a 128k model `layer1_threshold:
  140000` lay beyond the window and no layer ever ran. The token values stay
  the ceiling. An agent that overrides a token threshold is not bound by the
  plugin's share for it. `min_tokens_between_compactions` comes down with
  Layer 1, to the way from the target back up to it (32k model: 8000).

- The modules of `src/agent_system` above 1000 lines are split along their
  responsibilities; every name other code imports stays importable from where
  it was. `Agent` is built from mixins (`servers/agent/mixins/`, the step loop
  a package of phases in `mixins/llm_loop/`), the app's own routes are routers
  in `api/` sharing an `AppContext`, and `config/models`, `cli_utils/chat`,
  `core/session_presence`, `utils/vector_store` and
  `llm/batch/queue_manager` are packages; `agent_cli.py`, `config/settings.py`,
  the session manager and archive, and `tool_execution.py` hand their parts
  to modules beside them. The API's shared services moved from globals of
  `app.py` to `agent_system.app_state` (`agent_system.app` still answers a
  read of the old names). For code outside this repository: log records of
  moved code carry the new module names, and a test that patches a moved
  name patches the module that now looks it up.

- `openai_api` passes a request's own tools over instead of refusing it:
  clients with function calling on (Open WebUI's native mode, n8n, LibreChat)
  send their tools with every request, and the agent calls its own.
  `ignore_client_tools: false` in the server entry refuses them (400) as
  before. A forced call (`tool_choice: required`, a named tool) is refused
  either way: the agent never calls the client's tools.

- `forge`'s seventeen tools are four, each picking its work with
  `operation`: `forge_repo` (status, checkout, push), `forge_issue` (list,
  get, comment, update), `forge_pr` (list, get, diff, discussions, create,
  update, comment, merge) and `forge_ci` (status, log, retry); the
  parameters keep their names. The new `repo status` shows the
  repositories, those left out and why, the policy, whether the caller may
  use forge, and the webhook's work for the caller's sessions; it uses no
  token, answers every caller, and is the one tool offered without a usable
  repository (before: none). `/forge` runs it in the web chat and
  `agent-cli`. The new panel **Forge** (admins only) shows the same for
  every user's sessions, and the webhook store now records what became of
  each event -- the session it went to, or why it was dropped, not started,
  left waiting or failed. An agent's tool allowlist now allows a whole
  subject (`forge/forge_pr`), no longer single operations such as merge.

- A state machine run's cost counts the LLM calls made under its `call`
  activities too -- by the function, the code it calls, a tool it runs
  (hook `count_call_spend`) -- and with more than one state it is split
  by state (`spend.by_state`, the panel's Cost by state): a state that runs
  a submachine holds what its machine's states cost, the cost of a phase.

### Fixed

- A page of another origin on the same site -- another port on this host,
  a sibling subdomain -- could act as the signed-in user: SameSite=Lax hands
  such a page the auth cookie for every request, a form POST or a fetch
  included, so it could undo, delete or post (without reading the answer).
  A state-changing request the cookie authenticates now has to come from
  this origin's own pages, as the browser's `Sec-Fetch-Site` says -- sent
  to HTTPS and localhost addresses; otherwise `Origin` has to match `Host`
  or `X-Forwarded-Host` -- or it is answered 403. A plain-HTTP proxy that
  rewrites `Host` has to set `X-Forwarded-Host`. Requests with a Bearer value, without the cookie, or without either
  header (API clients) pass as before, and so do the origins of a
  credentialed CORS allowlist (`auth.cors_origins` with
  `auth.cors_credentials`). `src/agent_system/auth/cross_site.py`.
- The message debugger's size cap held two days of captures and kept
  every request row back to June: retention emptied a payload inside its
  request row, and the row kept its page -- on the server 590k rows in
  389k pages, 1.3 GB empty inside them, counted against the 5 GB cap and
  growing with every pruned row. Payloads now live in their own table
  `llm_payloads` (id = `llm_requests.id`), deleted whole by age together
  with the turns, so their space comes free; payloads, responses and a
  turn's messages are stored zlib-compressed (about a third), in columns
  named for it (`payload_z`, `response_z`, `turns.messages_z`: a query of
  an old name fails instead of finding only old rows). The first start
  moves an old file over on the writer (a minute or two, captures wait in
  the queue); the panel's prune then gives the freed space back to the
  disk. The move is one-way: code from before can neither write to the
  file nor start on it, so every process on it restarts together. A move
  that fails changes nothing: the retention strips the old payloads in
  place until a start succeeds, and takes no cost row while content is
  left; the cost capture waits for the move, and writer-costs, where it
  needs a payload, says so and stops. The WAL is
  cut back after a checkpoint (`journal_size_limit`; it had stayed at
  2.1 GB). `llm_requests` keeps every cost column; a reader of payloads
  joins `llm_payloads` and reads them with `debugger_text()`
  (`register_reader_functions`) -- writer-costs' resolvers do. A request
  opened without its payload says why.
- A state machine run's lease is renewed from a thread of its own: a `call`
  function holding the event loop for more than ~40 seconds let it run
  out, so a sweep marked the working run interrupted and another process
  could take it over.

- A state machine run's session in the web chat shows its agents' runs --
  task, steps and answer, read from their sessions -- each under the call
  that ran it, as a sub-agent's under the call that spawned it. The run's
  session now holds each agent activity as such a call (task and answer cut
  to 2,000 characters; the agent's session has them whole); before, the
  agents were only sub-sessions in the session list.

- A session's figures "including sub-agents" (`/costs` in the chat and the
  terminal, the Context & Cost Usage panel for one session) count the sessions
  below it in the session list: a session that makes no LLM call of its own --
  a state machine's run, an agent that only runs a machine -- showed nothing
  while its agents spent. The sub-agents of a session were found only through
  the request ids of its own calls; now through the session store
  (`SessionManager.subtree_ids`), and the request-id rule still finds calls
  filed under no session of the tree.

- The State Graph panel shows what a run cost -- in its Result card, its
  sub-agents included, and per activity with its tokens -- and `get_run`
  answers it as `spend`, summed from the journal. A fork counts only what it
  ran itself: the activities it replays name the run that spent them.

- An entry of `auth.anonymous_access.allowed_endpoints` opens only the method
  it names on plugin routes too: the plugin routes' security read the path of
  an entry and ignored its method, so `"GET /plugins/x/data"` also let an
  anonymous POST, PUT or DELETE through where the plugin layer decided (an
  `endpoint_security` rule leaving plugin routes to it). The enforcer, the
  security middleware and the plugin layer now read the list through one rule
  (`auth.enforcement.anonymous_may_reach`). The middleware, which took any
  first word of an entry as its method, now takes only GET, POST, PUT, DELETE,
  PATCH or `*` there, as the enforcer does: an entry such as `"HEAD /x"`
  opens nothing.

- A visitor without an account that `anonymous_access.allowed_endpoints` lets
  in no longer passes a route's role requirement: all three layers handed out
  the anonymous user before they looked at the role, so an entry such as
  `"GET /plugins/*"` opened admin-only plugin types (`log_viewer`,
  `ssh_control`) and `"GET /*"` admin routes to a guest. An entry waives the
  sign-in and the default role (`user`, or `plugin_security.default_min_role`);
  a stronger one that a rule, a plugin override or the plugin's type asks for
  answers 403 (`auth.enforcement.anonymous_meets_role`).

- On Windows the API no longer freezes whole when the terminal it was started
  in stops answering: starting a process with pipes, CPython asked the
  terminal's console, holding the GIL, whether a pipe is a console, and no
  request ran again. `agent-api` now runs the server on a hidden console of its
  own and passes its output on (`agent_system.own_console`; the entry point
  moved there, reinstall with `pip install -e .` to pick it up).

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

- The VS Code API tasks listen where the config says (`network.host`/`port`;
  a machine opens the API to its network in `config/local.yaml`), not on a host
  of their own: they start `app.run` as `agent-api` does. A stop of `agent-api`
  no longer waits for every open stream (5 s, as the systemd unit), and its
  access log reaches the terminal again (never `api.log`). `python -m
  agent_system.app` served from a copy of the module the rest of the code
  never saw -- a session lookup answered 503 off Windows; it starts as
  `agent-api` now.

## [0.7.0] - 2026-10-06

The first version published as open source. Changes before it are not
summarised here; the git history has them.

### Security

- Agent prompts render in Jinja's sandbox, both an inline `system_prompt` and
  a template file with its includes: a prompt reads its variables and calls
  their harmless methods, but no longer reaches Python internals --
  `{{ cycler.__init__.__globals__.os... }}` ran code in the API process
  before. `*` and `**` refuse results past 100,000 characters, items or bits;
  `{{ 9 ** 99999999 }}` froze the whole process while compiling. Every
  shipped prompt renders as before.
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

[Unreleased]: https://github.com/eehrich/ScarabHive/compare/v0.7.1...HEAD
[0.7.1]: https://github.com/eehrich/ScarabHive/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/eehrich/ScarabHive/releases/tag/v0.7.0
