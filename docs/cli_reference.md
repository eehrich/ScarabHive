# Agent CLI Reference

Complete command-line interface reference for AgentSystem.

## Installation

After installing the package (`pip install -e .`), the `agent-cli` command is available globally:

```bash
agent-cli --help
```

## Global Options

Available for all commands:

```bash
agent-cli [OPTIONS] COMMAND [ARGS]...

Options:
  --config PATH              Path to config file (default: AGENT_CONFIG_PATH, else config/config.yaml)
  -v, --verbose             Enable verbose logging
  --color {auto,always,never,ansi,html,text}  Color output mode (default: auto)
  --no-color                Disable colored output
  --show-tools                Show tool calls and their results
  --no-status               Disable status event output
  --raw                     Output raw JSON (machine-readable)
  -h, --help                Show help message
```

## Commands Overview

| Command | Description |
|---------|-------------|
| `run` | Execute an agent task (default command) |
| `chat` | Interactive chat with an agent (stays in the session) |
| `plugins` | Inspect discovered plugins (read-only) |
| `mcp` | Inspect external MCP servers (read-only) |
| `hooks` | Inspect registered hooks |
| `users` | User management (direct database access) |
| `reload` | Reload the running server's config |

---

## `agent-cli run` - Execute Agent Tasks

Run an agent with a given prompt.

### Usage

```bash
agent-cli run [OPTIONS] PROMPT
agent-cli PROMPT                     # `run` may be left out

# Default agent (default_agent in config.yaml)
agent-cli run "What is the weather in Berlin?"

# Specify agent by name
agent-cli run "Check system status" --agent sysadmin_agent

# Override LLM profile
agent-cli run --llm turbo "Fast question about Python"

# Multimodal: the task comes FIRST -- --attach takes every path that
# follows it, so a task behind the flag is read as a file name
agent-cli run "What's in this image?" --attach screenshot.png
```

### Options

```bash
--agent TEXT                    Agent name to use (plugin or config-based)
--llm TEXT                      LLM profile to use (overrides agent's default)
--llm-params KEY=VALUE ...      Override LLM parameters for this run, e.g.
                                thinking_level=max max_tokens=16384
--attach PATH ...               File(s) to attach -- images, audio or text;
                                the kind is read from the file, like /attach
                                in the chat (--images/--audio/--text still
                                work and are sorted the same way). Put the
                                request FIRST (see above); behind the flag it
                                is read as one more path, and the CLI says so
                                instead of reporting a missing request
--max-steps N                   Step budget for this run (overrides the
                                agent's max_steps; this process only)
--session ID|TITLE              Continue an existing session -- its ID or the
                                title given to it with `/title` (unknown:
                                creates a session with this ID)
--session-title TEXT            Title for the new session
--list-sessions [COUNT|all]     List this user's sessions, one line each
                                (default 20, 0 = no limit; no sub-agent sessions).
                                Only agents meant for chat
                                (visibility ui/both) -- plus always the agent
                                of this call and the session named with
                                --session. What pipelines started under the
                                same user is counted in a footer; `all`
                                lists everything. `agent-run --list-sessions`
                                reads no config and therefore always lists
                                everything.
                                A task that follows the flag is ignored, as
                                before -- the listing runs and nothing else
--list-archived [COUNT]         List this user's archived conversations
                                (default 20, 0 = all)
--restore-session ID            Restore an archived conversation and its
                                sub-agent sessions
--archive-sessions [DAYS]       Archive conversations older than DAYS (default:
                                session_archive.retention_days); --dry-run
                                reports without changing anything
--vars KEY=VALUE ...            Template variables for the agent's prompt
```

These are global and come BEFORE the subcommand:

```bash
--no-status                     Disable status event streaming
--raw                           Output raw JSON instead of human-readable
--color auto|always|never|ansi|html|text
--config PATH                   Path to config
```

`--max-steps` changes the budget for THIS process only — nothing is written to
the YAML, and the agent keeps its configured value everywhere else. Without
the flag the budget comes from `max_steps` in the agent's YAML, as before.
It works on `chat` too, which shares the resolved agent.

### Examples

```bash
# Quick query with default agent
agent-cli run "What time is it in Tokyo?"

# Use a specialized agent
agent-cli run "Review this PR: https://github.com/..." --agent coder_reviewer

# Override the agent and its model for one task
agent-cli run --agent research_agent --llm or-gpt-full \
  "Research the latest AI developments in 2026"

# Vision task with image (task first -- see above)
agent-cli run "Explain this architecture diagram" --attach diagram.png

# Machine-readable output for scripting
agent-cli run --raw "List top 3 tech stocks" | jq '.summary'
```

---

## `agent-cli chat` - Interactive Chat

REPL mode: stay in the session and keep talking to the agent, like `ollama run`.
The session is saved after every turn and can be resumed later (`--session`).

```bash
# Chat with the default agent
agent-cli chat

# Chat with a specific agent and LLM profile
agent-cli chat --agent coder --llm deepseek-chat

# Send a first message immediately
agent-cli chat "What is the status?" --agent sysadmin_agent

# Resume an earlier session (/sessions and /session print this line for you)
agent-cli chat --session a1b2c3d4 --agent coder

# List sessions without entering the chat
agent-cli chat --list-sessions
```

Takes the same options as `run`: `--agent`, `--llm`, `--llm-params`,
`--attach`, `--max-steps`, `--session`, `--session-user`, `--session-title`,
`--force`, `--list-sessions`, `--list-archived`, `--restore-session`,
`--archive-sessions`/`--dry-run` and `--vars`, plus the global `--color` and
`--no-status`. In the chat this means:

- `--no-status`, `--color never`/`text` (also `NO_COLOR`, `TERM=dumb`) or
  input that is not a terminal: nobody is asked what a run would ask the
  person (`ask_user`, `tool_approval`) -- the run decides without asking, as
  under agent-run.

- `--attach` attaches the files to the first message, as `/attach` does,
  including a check whether the model can read them. Without a first
  message passed along, they wait for the first one typed.
- `--llm-params` also apply to every profile that `/model` switches to.
- `--session-title` names only the session the chat starts with —
  not the one after `/new` or `/resume`.
- `--raw` and `--show-tools` have no effect in the chat; `/last` shows the
  tool traffic.

**In-chat commands:**

| Command | Effect |
|---------|--------|
| `/exit`, `/quit`, `/q`, `/bye` | End the chat (Ctrl-D / Ctrl-Z+Enter work too) |
| `/new` | Start a fresh session (the current one stays saved) |
| `/session` | Show the current session and the command that resumes it |
| `/sessions [count\|all]` | List this user's sessions, one line each (default 20, `0` = no limit). Sub-agent sessions are left out — they outnumber the real ones ten to one. So are the runs of agents that are not meant for chat (visibility neither `ui` nor `both`; measured 25.09.: 4562 of 5054, almost all pipeline evaluators) — the agent of this chat and the running session always stay in. A footer counts the rest, `/sessions all` shows it. The browser chat reads the same list via `GET /api/sessions/listing?count=&agent=&current=` — the server computes count, `all`, filter and footer with the same functions as the terminal |
| `/resume [id\|titel]` | Continue an earlier session without leaving the chat; without an argument the last one this user left (in the browser the most recent one from `/sessions` except the open one, from any chat agent: the browser switches the agent along with the session, the terminal stays with its own). The title works instead of the ID: IDs are machine-generated (`2332j2kj22k`) and **cannot** be renamed — they are the key under which usage tracker, message debugger, context store, sub-session indexes and presence locks keep their rows. Several sessions with the same title: the most recently used; a prefix suffices. Like `--session <id>`: the session continues on its own LLM. In the terminal a session of another agent is rejected, with the command that resumes it (the browser switches the agent instead) — in this chat it would run with foreign tools and a foreign prompt, and the next save would write this agent into its record. The same applies if its LLM cannot be started here (missing key): otherwise it would run on this chat's profile, and saving would overwrite its own choice |
| `/title [text]` | Give the session a name — the one `/sessions` shows, and under which `/resume` and `--session` find it again; without text it shows the current one. Titles are searched only among top-level sessions (sub-agent titles are their task text); an ID reaches any session. If several sessions carry the same title, `/resume` takes the most recent and says that there are others. A session without a first turn has no record yet; there the title goes along with the first save. The same in the browser: there is not even a session ID before the first message, the title goes along with the first message as `session_title` to `/run` or `/events` (only for a session the run creates), a rejected first message hands it back to the next one, and switching the session beforehand drops it with a notice. A `/title` during the first run, before it has saved, is remembered by the server for its first save (as in the terminal, which writes it after the turn) |
| `/agent [name]` | Agent of this chat — without an argument it lists the agents of the configuration, with an argument it switches. The switch **always starts a new session**: a session carries the agent it ran with, and under another one it would run with foreign tools and a foreign prompt. The new agent runs on its own LLM, a `/model` before it does not apply to it |
| `/vars [KEY=VALUE ...]` | Template variables of this session — bare lists them, `unset KEY` removes one, `clear` empties. The same variables `--vars` fills. A change reaches the agent on its next step and is written to the session file at once, so a removal survives `/resume` |
| `/model [profile]`, `/llm` | LLM of this session — without an argument it lists the profiles and marks the current one, with an argument it switches. Applies from the next message and is written to the session immediately, so a later `--session <id>` starts on it — even if the chat ends right afterwards. A session without a first message has no record yet; there the choice lands with the first save. `--llm-params` go along |
| `/tools [filter]` | Tools the agent really has, grouped by server (optionally filtered); deferred ones (`tools.deferred`) are listed too, a run sends their schema once loaded |
| `/skills` | Skill bundles it loads, `always` vs `on_demand` |
| `/costs` | Session cost so far **including sub-agents** (needs `context_usage_tracker`) |
| `/context`, `/ctx` | What fills the context window. Two blocks that are never mixed: what the provider **counted at the last call** (from `context_usage_tracker`, with the window it was counted against — and the note "stale" if the context has been compacted since), and what the conversation contains **now**, estimated and broken down by kind: tool results, answers, your messages, system prompt, tool schemas. Largest first, because that is the answer to "why is my window full" — in a long session it is almost always the tool results. No category is computed as "measurement minus estimate": that would look exact and carry the error of both |
| `/history [n]` | Last `n` exchanges (default 6); tool traffic condensed to one line each |
| `/last` | The last turn's tool calls and results in full, formatted |
| `/attach [<path> \| clear]` | Attach a file to the **next** message (repeat for several); without an argument the list, `clear` empties it. The rest of the line is ONE path — Windows paths contain spaces. The kind (image, audio, text) is read from the file, as with `--attach`; a file that cannot go along is rejected right away. The list is sent with the next message and then emptied |
| `/copy` | The last answer to the clipboard — the **text** as the model wrote it, not what the terminal made of it (the live region wraps to the window width and truncates tool lines). On Windows via `clip` in UTF-16LE, otherwise `wl-copy`, `xclip`, `xsel` in this order; one that is installed and still fails does not stop the next |
| `/undo [files]` | Take the last question and everything that answered it out of the session. The record is trimmed immediately, otherwise `--session <id>` brings the turn back — even if the session is empty afterwards. If the trimmed state cannot be written, the chat says so instead of presenting the turn as gone. The **files** the turn changed stay as they are; `/undo files` restores them **beforehand** (plugin `file_checkpoints`, see below) and only then takes the turn out. If one of them has been changed outside the agent since, nothing is touched and the turn stays — `/undo files overwrite` restores them anyway |
| `/retry [files]` | The same, and ask the question again right away — with the attachments it was asked with. The model no longer sees its first attempt, which is exactly why it is cut instead of appended. `files` as with `/undo` |
| `/rewind [n]` | Only the files, the conversation stays: without an argument the **checkpoints** of this session (one per turn that changed something via the file tools, with the files; the number is the fixed sequence number of the entry, it does not shift when old checkpoints drop out, and therefore has gaps), `/rewind <n>` restores every file the agent has changed since checkpoint n to how it was before — created files are deleted, deleted and moved ones come back. A turn that `/undo` without `files` took out appears as "dropped turn" between its neighbours and is reverted with a checkpoint before it. `/rewind <n> overwrite` also for files that have been changed outside the agent since. Shell commands are **not** recorded, only mentioned with a count |
| `/export [path]` | Write the conversation as Markdown: questions, answers, the tool calls and their results truncated (`/last` shows them in full). Without a path `chat-<session>.md` in the current directory; an existing file is never overwritten |
| `/edit [text]` | Write the next message in `$VISUAL`/`$EDITOR` (without either: `notepad` or `vi`), the argument is already in it. For what a prompt line is the wrong shape for — a specification, a pasted diff with a paragraph around it. An empty file sends nothing, and neither does an editor that ends with an error: whoever aborts does not want to pay for the turn |
| `/help`, `/h`, `/?` | List the commands. `/help manual` opens the manual, `/help plugins` the plugin list, `/help <guide>/<node>` that node, anything else searches every guide (an exact match opens directly, otherwise the hits are numbered). In the viewer: a number opens that link, `b` goes back, `n`/`p` browse, `c` contents, `i` index, `h` help, `/text` searches, `q` or Enter returns to the chat. Long pages pause a screen at a time. Nothing of it reaches the agent; without a terminal the page is printed and nothing is asked |
| ↑ / ↓ | Walk the input history; Ctrl-R searches it |
| Tab | Completes what fits the line: at the `/` the commands, plugin commands and skills, after `/model` the profiles, after `/agent` the agents, after `/vars` the variables of this session, after `/attach` paths (also with backslash). After `/resume` the sessions the process has already seen — `/sessions` or an empty `/resume` fill the list. Within a message nothing is offered |
| Ctrl-C | Cancel the **running turn**; twice at the prompt exits. Also aborts a running command (`/sessions`, `/resume`, `/vars`, `/tools`, a plugin command) without ending the chat; a running save is finished first, a second Ctrl-C drops it. After Ctrl-C, queued lines never run as new turns — not even if the answer was faster |

**The chat wakes up by itself.** While it waits at the prompt, its
event loop keeps running — a sub-agent started in the background (`blocking=false`)
therefore works even when nobody is typing. If input arrives for
its session — a sub-agent that has finished with `wake_when_done` —,
it aborts the wait and starts the turn itself, instead of waiting for
someone to happen to type something. What has already been typed stays untouched;
the wake call then waits for the next half second, and by then the
line itself has started a turn anyway. Attachments from `/attach` do **not** go
along with a woken turn: they belong to the message currently being
written. Redirected input (`… | agent-cli chat`) does not wake — there is
no line editor to interrupt and nobody waiting.

Plugins add their own, listed under *Plugin commands* in `/help` — but only
those whose tool this agent may call, so the list differs per agent. They run
the plugin directly, without an LLM turn: `/compact` (context_engineer) shrinks
the conversation on the spot. Two plugins claiming the same name are both
reachable as `/<plugin>:<command>`.

**In the browser** the same commands run, from the same catalogue and the same
parser — `/sessions`, `/resume`, `/tools`, `/costs`, `/history`, `/last`,
`/vars`, `/title`, `/agent`, `/undo`, `/retry`, `/rewind`, `/export`, `/copy` and
`/context` answer from the API (`/agents/<name>/tools`, `/api/sessions`,
`/chat/vars`, `/chat/undo`, `/chat/checkpoints`, `/chat/rewind`,
`/chat/transcript`, `/chat/last_answer`, `/chat/context`) instead of from the
local agent. `/vars`
sends the line as typed, so the grammar is read by the same parser the
terminal uses; it needs a session, which in the browser exists from the first
message on. It reads the persisted variables merged with the live ones — the
browser can open a session the running process has never loaded, and listing
only the live half would report "none" for a session whose file is full, then
overwrite it.

Only `/exit` is terminal-specific: a browser tab has no terminal to
leave. When typed, the browser says so instead of rejecting it. Everything else exists
in **both** surfaces — a command the user finds in the terminal
and not in the browser reads like a defect. Four do the
equivalent there:

- `/model` goes through the profile selector, as `/agent` goes through the
  agent selector, and applies from the next run that writes it into the session
  — a message to a running run stays on that run's model.
  Unlike in the terminal not immediately: whoever reloads beforehand is back on the
  session's profile — as with the button next to it. `/undo` and `/retry` also reload,
  but keep the choice.
- `/copy` fetches the text of the last answer from the server (`GET /chat/last_answer`,
  the same reading as in the terminal: `chat_actions.last_answer`) and puts it on
  the clipboard. The browser gives a page the clipboard only over https or on
  localhost — over http on another machine `/copy` uses the browser's old
  copy command, and if that fails too, it says so. While a
  run of this tab is answering, it refuses itself; if another run is working on
  the session, the server refuses (409): the record may then hold an
  intermediate state. With session presence (on in `config.yaml`) it sees
  every process, but cannot tell a run from an `agent-cli chat`
  that merely has the session open — the message names both.
  Without presence it knows only the runs of this server.
- `/attach` lists and clears as in the terminal. The page cannot read a path —
  it opens the paperclip's file picker instead; if the keypress for it is
  too long ago, it points to the paperclip.
- `/edit [text]` puts the text into the input field without sending: the field is
  already the editor there, which the terminal has to fetch via `$EDITOR` first.

Where both do the same, they also do it through the same place: the cut of
`/undo` and `/retry` is `chat_actions.split_off_last_exchange`, the Markdown
of `/export` is `chat_actions.transcript_markdown`. The only difference
is what they are applied to: the terminal trims the agent's message list
and lets the next save follow, the browser has the server trim the **record**
(`POST /chat/undo`) and reloads it — it shows the record, after all. A session
in which a run is currently working is rejected (409), not cut away from under
it — in the browser `/undo force`, for the case that the lock is the corpse of
a crashed process.

**Restoring files** (`/undo files`, `/rewind`) is possible only for whoever has recorded
them beforehand: the plugin `file_checkpoints` remembers per turn how each
file looked before the agent changed it via `file_ops` or `media_ops`
— for agents that enable its two hooks (the coder harness does).
`chat_commands.parse_undo` reads the words for both surfaces, the plugin
writes the lines for both. The browser goes via `POST /chat/undo` with
`"files": true`, `GET /chat/checkpoints` and `POST /chat/rewind`; a rejection
(files changed outside the agent) is a 409 with the list, and
`/undo files` then leaves the turn in place. Without the plugin both
surfaces say that file checkpoints are off. Details:
`src/plugins/file_checkpoints/README.md`.

`/title` and `/agent` go through the widgets the browser already has —
the session list and the agent selector — so that the command and the
button next to it do not diverge. `/export` downloads there instead of
writing: the browser cannot mean a path on the server's disk,
and it says so instead of silently ignoring it. `/retry` puts the
question back into the input field instead of sending it immediately — a file that
went along lies on the viewer's disk, and only they can attach it
again.

Plugin commands work there as well, and stay per-agent: the browser asks
`/chat/commands?agent=<name>` for the list and `POST /chat/command` runs one.
Both resolve the command against what THAT agent may dispatch, so the browser
names a command and never a tool — a command whose tool the agent may not call
does not exist for it, exactly as in the terminal. Switching the agent in the
selector re-fetches the list.

**A session brings its agent and its LLM along.** Both are in its
record, and a bare `--session <id>` reads them back — the same
conversation therefore continues with the agent and the model it was
started with, instead of the config defaults. `--agent` and `--llm` still
override that. **`agent-run` behaves identically**, and that is not a
convenience but a necessity: both entry points *write* the same
record, and as long as they answered the question differently, every
`agent-run` call deleted what `agent-cli` had stored there. The
decision therefore lives in exactly one place
(`cli_utils/session_defaults.py`). Two restrictions with reasons: a stored profile is
adopted only if the agent is the stored one too (a profile chosen for
another agent does not belong in that agent's chain), and if
it is the agent's default anyway, nothing happens — a second client
for the same value would be pure work.

**Input history.** Arrow up recalls what was asked in *this session*. It is not
stored anywhere additionally: the session itself is the
log, its user messages are the history. `/resume` and `/new`
therefore swap it as well, and both surfaces show the same one.

Two things are deliberately left out. **Slash commands** run in the REPL and
never reach the session — they can be recalled until the process ends, also across
`/new` and `/resume`, and are gone
afterwards. And **very long messages** (over 2000 characters) are skipped:
a `/skill` call lands in the session as the fully *unpacked* skill text,
and nobody wants 30 kB of SKILL.md above their prompt. A
message that was sent with `//` also comes back with `//`
— otherwise Enter on it would *execute* the command instead of sending it.

In the browser the input field is multi-line, so the arrow keys belong
to the cursor first: they reach for the history only when the cursor
*cannot* move any more — that is, at the top or bottom edge of the
text. After a recall the cursor is at the **start**, so that paging
further back costs one keypress per step; the first arrow down
therefore still belongs to the cursor, only the second goes forward again. Nothing
is lost in the process: a draft in progress comes back, and what one writes into
a recalled entry is kept when paging on.
Escape cancels and restores the draft.

**Multi-line input.** A plain Enter sends the message, so pasting a block
needs one of:

```
"""
move.w  d0,d1
rts
"""
```

or a trailing backslash to continue on the next line. A message that has to
*start* with a command word is escaped with a doubled slash — `//new ...`
reaches the agent as `/new ...`; anything else beginning with `/` that is not
a known command — a path like `/etc/nginx/nginx.conf`, for instance — is sent
as an ordinary message. The escape only fires where it is needed: a pasted
`// TODO: fix` or `//192.168.1.1/share` keeps both slashes.

While a turn is running, a typed line goes to the agent at the next step --
unless a question of the run (`ask_user`, `tool_approval`) was shown when you
began typing it: then the line is its answer, see
`src/plugins/ask_user/ask_user.guide`.
The same rule as at the prompt applies, from the same function: a
**single** line that is a known command word is rejected
(commands exist only at the prompt), everything else is a message — a
pasted block is therefore **one** message, with indentation and blank lines, and
a path like `/etc/nginx/nginx.conf` goes through. `/history` and `/last`
show every message the agent received, including one sent
with `//`.

**Display:** tool activity is rendered like the WebUI front panel -- one line
per operation that updates in place and collapses into its `✓`/`✗` end state,
instead of a chronological log. Thinking tokens appear as a live counter
(`✻ Thinking… (~120 tokens · 4s)`), intermediate agent narration between tool
calls is shown dimmed, and the final answer is rendered as markdown. Each turn
ends with a dim usage footer (`↑1.2k ↓830 · $0.0213 · 3m41s`) and the session
total is printed on exit. On a non-ANSI terminal (or when piped) the display
falls back to plain chronological lines.

---

## `agent-cli plugins` — Inspect plugins

Read-only. A plugin is enabled in `config/plugins.yaml`, not by
command: `enabled: true` on the server entry, and for an agent to
get the tools too, they belong in its allowlist (`agent_config.tools.allowed`). The
former commands `enable`/`disable`/`status` no longer exist.

```bash
agent-cli plugins list [--format table|json] [--show-metadata]
agent-cli plugins info NAME [--format table|json]
agent-cli plugins search TERM
agent-cli --raw plugins info NAME --format json    # additionally factory details
```

`list` shows every plugin **type** and below it its instances, as soon as there is more
than one. For the type, ENABLED means: at least one instance is
enabled. The same rule applies to `info` — `extra_audio_ops` is an
instance of type `audio_ops`, not a type of its own. If `type:` names another
server (`child: {type: base}`), the instance counts toward the plugin at the end of this
chain.

```
| NAME                              | ENABLED   | DESCRIPTION                                          | VERSION   |
|-----------------------------------|-----------|------------------------------------------------------|-----------|
| audio_ops                         | YES       | Audio file manipulation - cut, merge, mix, and co... | 1.1.0     |
| ├─ audio_ops                      | YES       | Audio file manipulation - cut segments from FLAC/... |           |
| ├─ extra_audio_ops                | YES       | Audio manipulation for a further plugin root         |           |
```

JSON form of an entry (`instances` stays empty as long as there is only one):

```json
{"name": "audio_ops", "description": "...", "version": "1.1.0", "enabled": true,
 "instances": [{"instance_name": "audio_ops", "enabled": true, "description": "..."},
               {"instance_name": "extra_audio_ops", "enabled": true, "description": "..."}]}
```

With `--format json`, `--show-metadata` appends the raw plugin metadata.

---

## `agent-cli mcp` — Inspect external MCP servers

Read-only. Each call connects the servers enabled in `config/mcp_servers.yaml`,
performs the action and disconnects again — a connection does not
survive the process. That is why there is no `connect`/`disconnect`; whether a
server is reachable is answered by `test`. Blocked tools are listed in
`mcp_servers.yaml` under `tools.blocked` — only this list takes effect for an
external server; which tools an agent may call is governed by its allowlist.
`tool allow/block`,
`enable`/`disable` and `feature` have been dropped (`allow/block` had
rewritten the file and deleted all comments in the process).

```bash
agent-cli mcp                                 # help
agent-cli mcp list [--format table|json]      # configured servers
agent-cli mcp status [SERVER] [--format ...]  # without SERVER like list
agent-cli mcp test SERVER                     # connection + basic function, JSON
agent-cli mcp tools SERVER [--format ...]     # tools, blocked ones marked
```

`--format` goes **after** the action (`mcp list --format json`).

---

## `agent-cli hooks` — Inspect hooks

Loads the plugins so that their hooks register (about two seconds, plus
the connection setup to enabled external MCP servers), and then shows
the registry. If loading fails as a whole, the command ends with exit code
1. A single plugin that does not load is missing from the list — as on the server —
and appears as an error on stderr.

```bash
agent-cli hooks list [--type pre_llm_call] [--format table|json]
agent-cli hooks inspect PLUGIN.HOOK                # all details as JSON
```

ENABLED is the state of the registry after `schema.yaml`, `hooks.overrides` and
the instance's `hook_config` — not the override of individual agents.
`hooks.overrides` comes from the loaded config (`--config`).
There are no execution statistics here: they live in the memory of the
process that executes the hooks (API server), which a CLI call never sees.

---

## `agent-cli users` — Manage users

Works directly on the user database (`auth.database_path`, default
`data/users.db`) and therefore needs no login. Help with
`agent-cli users -h` or `agent-cli users COMMAND -h`. Without a command
`list` runs (also `users --limit 5`). Errors end with exit code 1. Global options
such as `--config` go **before** `users`.

```bash
agent-cli users list [--limit 100] [--skip 0]
agent-cli users info USERNAME
agent-cli users create USERNAME EMAIL [-p PASSWORD] [-n "Full Name"] [-r user|admin|guest] [--admin] [--inactive]
agent-cli users update USERNAME [-e EMAIL] [-n NAME] [-p PASSWORD] [-r ROLE] [--activate | --deactivate]
agent-cli users delete USERNAME [-f]
agent-cli users generate-api-key USERNAME
agent-cli users revoke-api-key USERNAME [-f]
```

Without `-p`, `create` prompts for the password; `-f` skips the confirmation prompt.

---

## `agent-cli reload` — Reload the running server's config

```bash
agent-cli reload [--url http://127.0.0.1:8000] [--api-key KEY] [--format table|json] [--timeout 30]
```

Calls `POST /admin/reload-config` on the running server (no restart). URL
and key otherwise come from `AGENT_SERVER_URL` and `AGENT_ADMIN_API_KEY` /
`AGENT_API_KEY` respectively; the key must belong to an admin. A password change
revokes it — generate a new one afterwards.

The `auth` section takes effect only after a restart: the server keeps the one it
was started with, and reports a change on disk in the report
(`report.auth`: "changed on disk: takes effect on a restart").

---

## Environment variables

| Variable | Effect |
|----------|--------|
| `AGENT_CONFIG_PATH` | Config file if `--config` is missing (otherwise `config/config.yaml`) |
| `AGENT_SERVER_URL` | Server for `reload` |
| `AGENT_ADMIN_API_KEY`, `AGENT_API_KEY` | Key for `reload` |
| `NO_COLOR`, `TERM=dumb` | no colors, as long as `--color` is `auto` (also in log lines) |

API keys of the LLM providers are in `config/local.env` (this machine) or `config/secrets.env` next to the config.

What applies to one machine only (network, log retention, its own signing key) is in `config/local.yaml` next to it: never in the repo, read by the loader after all includes — it wins over all other files and, alone besides `config.yaml`, may also set `auth` and `paths`. This machine's keys are in `config/local.env` (likewise never in the repo); the loader reads it before `config/secrets.env`, so its value wins there. The setup panel and the installation scripts write only to these two files.

---

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error: unknown plugin/hook/server, `mcp test` failed, `reload` without success (also a single server), unknown LLM profile, unreadable attachment, session busy or not loadable, `users` command failed |
| 2 | Wrong invocation: unknown command or parameter, invalid `--llm-params` |

Errors reported by the **agent** during a run (`ERROR:` lines) do not change
the exit code.

---

## Configuration

- `config/config.yaml` — among others `default_agent` (agent without `--agent`),
  `logging.file_cli` (log file of the CLI; without the key `<logging.file>-cli.log`,
  i.e. `logs/agent-cli.log`).
- `config/plugins.yaml` and the files loaded via `includes` — plugins and
  agents under `plugins.servers`; an agent is a server entry with
  `agent_config`. See [Configuration-Based Agents](config_based_agents.md).
- `config/mcp_servers.yaml` — external MCP servers under
  `external_servers.remote_servers`. See [Tool server configuration](server_configuration.md).

---

## Troubleshooting

**"Agent not found"** — the error message lists the available agents; the
name belongs after `--agent`, not as the first word of the task.

**ANSI codes in redirected output** — `--no-color` or `NO_COLOR=1`. With
`--color auto` (default) no escape sequences are produced in pipes.

**A tool is missing for the agent** — plugin enabled in `plugins.yaml`
(`plugins list`)? Tool in the agent's allowlist? For external servers:
`mcp test SERVER` and `mcp tools SERVER`.

---

## Further Reading

- [Configuration-Based Agents](config_based_agents.md) - Deep dive into agent definitions
- [Tool server configuration](server_configuration.md) - External server setup
- [Plugin Authoring](plugin_authoring.md) - Create custom plugins
- [Authentication Guide](multi_user_authentication.md) - Security and user management
- [Context Management](context_management.md) - Token budget strategies

---

## Quick reference

```bash
agent-cli "task"                                # run with the default agent
agent-cli run "task" --agent NAME --llm PROFILE
agent-cli chat --agent NAME                     # interactive
agent-cli run --list-sessions                   # this user's sessions

agent-cli plugins list | info NAME | search TERM
agent-cli mcp list | status [SERVER] | test SERVER | tools SERVER
agent-cli hooks list | inspect NAME
agent-cli users list | info | create | update | delete | generate-api-key | revoke-api-key
agent-cli reload

agent-cli --verbose run "task"                  # progress
agent-cli --show-tools run "task"               # tool calls in detail
agent-cli --raw run "task"                      # result as JSON
```
