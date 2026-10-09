# CLI Architecture

How `agent-cli` and `agent-run` are built. The commands themselves are in the
[CLI Reference](cli_reference.md); this document covers what happens behind
them and which pitfalls the code already knows.

As of: 2026-09-14 (cleanup round: dead commands, config writers and
duplicate implementations removed).

---

## 1. Entry Points

| Command | Module | Purpose |
|--------|-------|-------|
| `agent-cli` | `src/agent_system/agent_cli.py:main` | Agent runs (`run`, `chat`) and inspection (`plugins`, `mcp`, `hooks`), users (`users`), `reload` of the server |
| `agent-run` | `src/agent_system/agent_run.py:main` | lean one-shot run with the default agent; shares session logic and attachments with `agent-cli` |

Both are listed in `console_scripts.cfg`, which `pyproject.toml` reads.

**Principle: the CLI does not write configuration.** A plugin or tool server
is enabled by editing the YAML — `enabled` alone is not enough either, the
agent needs the tools in its allowlist. The former write commands
(`plugins enable`, `mcp enable`, `mcp tool allow/block`, `mcp feature set`)
have been removed; `allow/block` had rewritten `mcp_servers.yaml` via
`yaml.safe_dump` and lost all comments in the process.

---

## 2. Argument Parsing (`agent_cli.main`)

`agent_cli.py` holds the entry point only: it reads the line, loads the
config and hands over to the command. The two parsers and `--llm-params` are
built in `cli_utils/cli_parser.py`, next to each other because their pitfall
(below) is a pair. Three stages, each for a measured reason:

1. **Pre-parser** (`parse_known_args`): picks up the global options (`--config`,
   `-v`, `--color`, `--no-color`, `--show-tools`, `--no-status`, `--raw`) from
   *anywhere* in the line and places them before the subcommand. If the first
   remaining word is not a subcommand, `run` is inserted — so `agent-cli "Question"`
   works without `run`.
2. **`users` goes to Typer** before the main parser runs
   (`cli_utils/users.py`) — with the *original* tokens after `users`,
   because the pre-parser also reads option values (`-p -vS3cret` arrived as
   `-p -S3cret`). Typer owns arguments, help and exit codes. The
   earlier argparse copy had drifted apart: options ended up at commands that
   do not know them (traceback), `update USER EMAIL` discarded the
   email, every error ended with 0.
3. **Main parser** with subparsers for the rest.

**Pitfall — define a flag in only one place.** If a subparser defines the same
`dest` as the parent parser, its *default* overrides the parent parser's value
(Python 3.12, measured). This is how `plugins info --raw` used to read
`False`, and `mcp --format json list` produced a table. Hence:
`--raw` only globally, `--format` only on the actions.

**`--config` without a default.** If the flag is missing, `load_settings(None)`
gets to choose: `AGENT_CONFIG_PATH`, otherwise `config/config.yaml`. A default
`config/config.yaml` in the parser had masked the environment variable for all
subcommands.

---

## 3. Subcommands and What They Start Up

Each command lives in `cli_utils/commands/`: `run.py` (`run` and `chat`),
`plugins.py`, `mcp.py`, `hooks.py`, `reload.py`; `users` is the Typer app in
`cli_utils/users.py`.

| Subcommand | Bootstrap | Note |
|------------|-----------|---------|
| `plugins` | only `discover_all_plugins` over `plugins.plugin_dirs` | `enabled` raw from `plugins.servers` — as `ToolServerIntegration` does when registering; the type follows the `type:` chain down to the plugin. A plugin counts as enabled if one of its instances is |
| `mcp` | `ToolServerIntegration.initialize` → action → `shutdown` | read-only; a connection does not survive the process, hence no `connect`/`disconnect` |
| `hooks` | `ToolServerIntegration.initialize` → read registry → `shutdown` (~2 s plus connection setup of external servers) | Hooks register themselves when the plugins are loaded; without that the registry was always empty. If `initialize` fails as a whole → exit 1; a single broken plugin is missing (error on stderr), as on the server. No statistics: they live in the memory of the executing process |
| `users` | only the user database (`auth.database_path`) | no login needed, direct DB access |
| `reload` | nothing; `POST /admin/reload-config` on the running server | admin key required |
| `run`, `chat` | full: logging, `InitializationService.initialize_for_cli`, `initialize_tools`, `init_batch_system` | see 4 |

`mcp` uses `ToolServerService` (`list_servers`, `get_server_status`, `test_server`)
and `ToolService.list_tools`; the API serves both services as well.

---

## 4. Flow of `run` and `chat`

In this order, in `cli_utils/commands/run.py`: `run_agent_command` calls one
function per step, each with what it reads and what it hands on.

1. **Logging** into a role-specific file (`logging.file_cli`, otherwise
   `<logfile>-cli.log`), so that CLI and API do not write to the same file.
   Without `-v` the console shows only warnings.
2. **Bootstrap** (registry, session service, MCP, batch system).
3. **Session defaults**: if `--session` is resumed, the agent and
   LLM profile it was started with apply — `--agent`/`--llm` override them
   (`cli_utils/session_defaults.py`).
4. **Entry agent**: from the registry or built from the *resolved*
   config (`get_tool_server_config`); the raw entry would carry Pydantic defaults
   instead of inherited values. One factory for all: `servers/agent/entry.py`
   (`entry_agent`) — the API, `/agent` in the chat and `create_and_register_agent`
   (agent-run, audio in a further plugin root) build there. If the name is not an agent → the list
   of agents, exit 1.
5. `--max-steps` (copy of the `agent_config`, this process only),
   `--list-sessions` (lists and ends, before the LLM override).
6. **LLM override** from `--llm`/`--llm-params` — before the attachments, so that
   the capability check sees the model actually used. `--llm-params`
   itself is already validated by the parser (exit 2, before the bootstrap).
7. **Attachments** (`cli_utils/attachments.py`): the kind comes from the file, not
   from the flag. The message and the capability check are built by
   `message_with_attachments` (`utils/multimodal_processor.py`), the same
   place as for the API and the chat; error → exit 1.
8. **Session presence** (`core/session_presence.py`): the session is
   *held before* it is loaded. Occupied → error (exit 1), `--force`
   overrides an orphaned hold, `--woken` (set by the wake command) steps back
   silently. Ctrl+C is a stop: the session is marked released, and
   nothing wakes it up by itself afterwards.

   `chat` had to learn two things for this. First: **the prompt waits on the
   event loop, not beside it.** `PromptSession.prompt()` is synchronous — it
   starts its own loop and blocks the thread until Enter, and everything
   that sits on the REPL's loop stands still for that time. Measured on
   2026-09-20: a sub-agent's one-step LLM call lay **four minutes**
   unread and finished 0.3 s after the first user input — typing
   was what turned the loop again. So `wake_when_done` could not work in the
   chat at all: the job that sets the marker was frozen, so the marker
   never appeared. `_PromptEditor._ask` therefore runs
   `prompt_async` under `run_until_complete` (`cli_utils/chat/prompt_input.py`). The same
   applies to `/edit`: the editor runs via `run_in_executor`, because writing
   a message in vim takes minutes, and that is exactly when a
   background job would have the most time. Not affected and still blocking
   is the fallback reader `input()` — the redirected case, in which nobody sits
   at the prompt; and `/copy`, where `clip`/`xclip` take milliseconds and
   the lock would cost more than the damage.

   Second: `chat` holds its session across the **whole** REPL, and therefore has
   to pick up the wake call itself: the marker (`<session>.pending`) is otherwise taken only
   *within* a request (`_presence_step` on every LLM call),
   and no call runs at the prompt — a sub-agent finished with `wake_when_done`
   would sit there until the user happens to type something. While the prompt
   waits, a watcher thread (`_watch_for_wake` in
   `cli_utils/chat/context.py`) therefore polls `presence.pending(...)` every half second and
   cuts off the input; the REPL takes the marker (so that a turn that never
   reaches an LLM call does not trigger an endless loop) and starts a
   turn with `WAKE_TASK`, just as an input would. It takes it with
   `take_for_wake`, which also changes `<session>.woken`: `wake_session`
   repeats its ring while the session is held, and the chat holds it for the
   whole REPL — without the stamp every repeat was a woken turn of its own
   (up to 30, ten seconds apart). A woken `agent-cli run --woken` takes the
   marker the same way. What has already been typed is
   **not** cut off — `exit()` discards the buffer — and neither is it
   without a line editor: `input()` cannot be interrupted by any thread, and
   that is the redirected case anyway, in which nobody sits at the prompt.
   Attachments from `/attach` do not go along with a woken turn: they belong to
   the message the user is currently writing.
9. **Open the session** via `SessionService.open_for_run`, like `/run` and
   agent-run: a saved one is restored, a new one starts with
   the `template_vars` of the agent config; `--vars` on top.
10. **Run**: `chat` hands over to `cli_utils/chat/repl.py:run_chat_loop`. The
    one-shot run is `cli_utils/commands/one_shot.py`: `--raw` and
    stream mode both collect via `collect_final_result`. In
    stream mode, `on_event` shows tool calls (`--show-tools`),
    the thinking (gray), errors (`ERROR:`) and the answer as soon as they arrive —
    a Ctrl+C usually lands outside the event loop, and after the run
    only the abort line appears. The status lines come via
    `status_bus`.
11. Save the session (not on abort), release the hold in the `finally` and
    shut down the batch system and MCP.

**One event loop for the whole process** (`run_async`, `get_cli_loop`).
`asyncio.run` per step closed the loop afterwards — and with it the tasks of the
external MCP connections from the bootstrap. The agent silently got zero
external tools while `mcp test` worked (measured 2026-09-01). The chat
borrows the same loop; `close_cli_loop` cleans it up at process end.

**stdout carries the result.** Meta lines ("Session saved", warnings)
go to stderr. Status lines and streamed thinking are on stdout for `agent-cli`;
`--no-status` keeps stdout clean. `agent-run` writes status to
stderr.

---

## 5. `cli_utils/`

| Module | Contents |
|-------|--------|
| `common.py` | color mode (`set_color_mode`, `supports_color`), Windows VT mode, status lines, `show_answer` (answer as Markdown with colors, raw into a pipe), `render_with_rich` |
| `chat/` | the REPL, a package (below) |
| `cli_parser.py` | agent-cli's preliminary and main parser, `parse_llm_params_args` |
| `commands/run.py` | `run` and `chat`: the steps of section 4 up to the run, the session hold, open and save |
| `commands/one_shot.py` | the one-shot run: its stop (`RunControl`), the streamed display, the result printed after |
| `commands/plugins.py` | `plugins list` / `info` / `search` |
| `commands/mcp.py` | `mcp list` / `status` / `test` / `tools` |
| `commands/reload.py` | `reload` |
| `commands/table.py` | the table `plugins list` and `mcp list` print (tabulate, or a plain fallback) |
| `session_defaults.py` | agent/LLM of a resumed session |
| `session_listing.py` | `--list-sessions` |
| `attachments.py` | sort attachments by file kind |
| `agent_runner.py` | what both entry points share: running as the local operator, agent creation for `agent-run`, the woken run's task, the busy-session and cancelled lines |
| `users.py` | Typer app for `agent-cli users` |
| `commands/hooks.py` | `hooks list` / `hooks inspect`, with the plugins loaded |

`chat/` is cut by responsibility; `chat/__init__.py` re-exports
`run_chat_loop`, `run_chat_turn`, `ChatRenderer` and `display_width`:

| Module | Contents |
|-------|--------|
| `repl.py` | `run_chat_loop`: reads a line, resolves it, hands a built-in command to its handler through one table (`_COMMANDS`) or runs a turn; what Tab completion offers |
| `turn.py` | `run_chat_turn`, `_execute_turn` on the REPL's loop, the two-stage Ctrl-C (`_cancel_turn`) |
| `display.py` | `ChatRenderer` (the live region), `display_width`, muting console logging while the region is drawn |
| `prompt_input.py` | the prompt: `"""` and a trailing `\` for several lines, `_PromptEditor` (prompt_toolkit, history, completion), piped stdin, `/edit` in `$EDITOR` |
| `typeahead.py` | what is typed while a turn runs (`_KeyReader`, `_poll_typed_input`) |
| `context.py` | `_ChatContext` and the open session: its messages and the history seed, hold/release/claim (session presence), the wake watch, a fresh session, `_save_now` |
| `interruptible.py` | work on the REPL's loop that Ctrl-C cancels instead of the chat (`_run_interruptible`, `_drain`) |
| `token_usage.py` | tokens and cost per call and per chat, the footer line, `/costs` |
| `sessions.py` | `/new`, `/session`, `/sessions`, `/resume`, `/title`, `/vars`, `/attach` |
| `agent_setup.py` | `/agent`, `/model`, `/think`, `/tools`, `/skills`, `/context`, running a skill |
| `transcript.py` | `/history`, `/last`, `/undo`, `/retry`, `/rewind`, `/export`, `/copy` |

Inside the package a module calls a sibling's function through the module
(`context._save_now(...)`), so a test patches it once, where it is defined.
The commands themselves are declared elsewhere: `agent_system/chat_commands.py`
(built in: the catalogue and the parser, shared with the web UI) and
`agent_system/plugin_commands.py` (declared by plugins, always run via
`dispatch_tool_call`).

---

## 6. Output

- `--color auto` (default) writes ANSI only where it is rendered;
  `always`/`ansi` force it, `never`/`text` switch it off, `html` for
  embedded display. Windows consoles get encoding errors replaced,
  pipes get UTF-8.
- `--format table|json` exists per subcommand (`plugins`, `mcp` actions,
  `hooks`, `reload`).
- Exit codes: 0 success, 1 error (including functional ones from `plugins`, `mcp`,
  `hooks`, `reload`, `users`), 2 invalid invocation. Errors the agent
  reports during a run (`error` events) do not change the code. The `_mcp_*` helpers and
  `handle_hooks_command` report success as `bool` for this.

---

## 7. Tests

`tests/cli/` — run them in a targeted way, the group takes a good minute.
Entry points: `test_cli_subcommands.py` (hooks, users, removed commands),
`test_cli_mcp.py`, `test_cli_plugins_*.py`, `test_cli_event_loop.py`
(one loop), `test_cli_session_resume_end_to_end.py`, `test_cli_chat.py`.

---

## 8. Related Documents

- [CLI Reference](cli_reference.md) — all commands and options
- [System Architecture](_arch_agent_system_architecture.md)
- [App Architecture](_arch_app_architecture.md) — the API that `reload` calls
- [Plugin Architecture](_arch_plugin_architecture.md), [Plugin Hooks](plugin_hooks.md)
- [Session Management](session_management.md)
- [Tool server configuration](server_configuration.md)
