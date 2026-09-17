# Agent Editor

The **Agent Editor** panel: create and edit agent definitions — the `plugins.servers.<name>` entries in the config
tree — and write them back into the YAML file each one came from, comments and layout kept. Web-only plugin, no MCP
tools.

## Requirements

- `auth.enabled: true`. With authentication off the panel shows an empty state and the API answers `403`.
- An active admin account.
- The server entry `agent_editor: {type: agent_editor, enabled: true}` (in `config/plugins.yaml`) and a route rule
  `/plugins/agent_editor/*` → admin in `auth.plugin_security`.
- For **New agent**: `config/config.yaml` must include `agents*/*.yaml` (it does by default); new agents are written to
  `config/agents/<name>.yaml`.

## The panel

Opened from the panel launcher (category *Agents & tools*) or at `/plugins/agent_editor/`.

**Which entries are agents:** for an entry the running app declared, the app's own answer (its instance, or the lazy
declaration). For every other entry its final type decides: `agent`, the lazy plugin types, and the types all of
whose built instances are agents. A type that built agents and plain servers (`writer_issues`) is no agent class.

**Left** (the kit's side pane: its corner sets the width, which is kept for the next visit): search, **New** (blank,
a copy, or a child that inherits from another agent), a filter (all / enabled / disabled / has problems / needs
restart / read-only) and the agents grouped by where they live — `config` for everything under the config
directory, the plugin name for `src/plugins*/<plugin>/agents/`. Each row shows the description and badges for *off*,
*new*, *restart*, *read-only* and *N problems* (the problems in its tooltip). A problem is what the editor warns
about, read from the files as saved: an entry that does not resolve, a profile of a chain that does not exist (any
profile, when the config defines none), a
missing prompt template, an unknown skill, an allowed or blocked pattern that matches no tool of the running app,
a pattern for an external MCP server that is off. A pattern for an external server is not judged otherwise: the
editor lists no external tools (they come from a live connection), and the Tools tab marks it *external, not
checked*. The servers come from `external_servers.remote_servers` of the loaded config, whichever file declares
them; a pattern reaches them the way the runtime lets it — the exact dotted name (`everything.echo`) or a pattern
ending in `*` (`everything.*`, `ever*`), never a bare `everything`, `everything/*`, `*.echo` or `everything.ech?`
(those pass discovery and die in the tool filter).
**Reload config** in the toolbar calls the core `POST /admin/reload-config`.

**Right:** the selected agent. The head stays in place while a tab scrolls: the name, its base (the parent agent or
the plugin class), its file, how it relates to the running app (*Active*, *Active with older settings*, *Not
active yet*, *Disabled*, *Still active, gone from disk* — active: started and ready, not necessarily busy), and
Revert / Save. A banner says what a restart or a
config reload would apply, with a **Reload config** button when a reload applies something. Tabs:

- **General** — enabled, base, description; visibility, category, tags.
- **Model** — the model chain (primary and fallbacks) and the advanced chain; `fallback_recovery_seconds`,
  `llm_params`.
- **Run** — the core's run settings, key by key: `max_steps`; auto-escalation (`auto_escalate_on_stuck`,
  `escalate_error_streak`, `escalate_rounds`, `escalate_max_calls`, with a note when it cannot run: no advanced chain,
  one that starts with the model chain's profile, steps or calls at 0, or chains of a parent that does not resolve);
  `loop_detection`; `reasoning_loop`; the `timeouts` the agent reads (`status_queue_put_timeout` is read nowhere and
  has no field). The models take any number; the fields keep to what the runtime can use (a negative history size
  fails every request, a threshold of 0 turns a detector off unseen), mark anything else and hold Save back; such a
  typed number counts as an unsaved change. Known gap: switching loop detection off still leaves the sequence check
  running (core, `Agent.__init__`; reported).
- **Tools** (the tab shows how many tools reach the agent) — allowed and blocked lists, each with a source:
  inherited, extended (`+`/`!` entries on top of the inherited list) or an own list. The tree can be searched (server
  names, plugin types, tool names and descriptions) and narrowed to the servers in the list or not in it. It offers
  the tools an agent can be given — the servers tool discovery finds: every plugin server, and every other server
  of the app that is visible as a tool. A live summary shows the tools the agent gets, which patterns grant each of
  them, which blocked patterns name a tool (granted or not, so a blocked tool shows as locked), how many tools each
  pattern names, and patterns that name nothing. It follows the agent's own passes: a server is considered when the
  whole allowed list matches it (an exact `server/tool` entry counts), its tools are kept when the whole list matches
  them, then the blocked ones are dropped. So `web/web_*` alone
  grants nothing — the server pass does not admit `web` — but next to `web/web_search` it grants every `web_*` tool.
- **Sub-agents** (only when the `sub_agent_manager` plugin is installed) — an agent starts sub-agents through the
  managers its allowed tools grant, and each manager's own lists decide which agents it can start (there is no
  per-agent list of sub-agents). The tab shows every enabled manager with a switch for the grant (written into this
  agent's tools, saved with the agent; locked, with the reason as its title, while a pattern the switch does not
  write grants the manager or a block stops it — the Tools tab edits those), which agents it can start, and whether
  it runs yet. **Configure** opens a
  manager's own entry: the agents it can start (a checklist with search and filter; a list with patterns, or none,
  can be replaced by picked agents), and its limits (`max_sub_agents_per_session`, `max_sub_agents_per_type`,
  `max_nesting_depth`, `default_wait_timeout`, `auto_archive_on_limit`, `allow_advanced_model`); **Save manager**
  writes that entry after its own diff, other keys of it stay (a limit is a whole number of at least 0; an empty
  field falls back to the plugin default). Unsaved manager edits are asked about before another agent, another
  manager, a new manager or a delete drops them. **New manager** creates `config/agents/<name>.yaml`
  (starting no agent) and grants it to this agent. Below: one switch per manager, whether it may start this agent,
  written into that manager's lists right away.
- **Prompt** — a template file (with preview) or an inline prompt, `template_vars`, skills (always / on demand; a
  search and a filter for the selected ones).
- **Hooks** — hooks on or off for the agent, and per hook an override: on/off, and its own settings as YAML. The
  table can be searched and narrowed by hook type and by state for this agent (set here, on, off).
- **YAML** — the agent's own entry as YAML, for every key without a form control (`self_tool_descriptions`,
  plugin-specific keys).

The YAML fields are coloured with the kit's `yamlCode` (a block scalar keeps its colour over blank lines). An error
under a YAML field is about the text it was shown for: typing on hides it, leaving or saving parses again. An
unquoted `- !server/*` is a YAML tag to any parser; typed here it is read as the removal entry it looks like, and
written back quoted (`'!server/*'`). A
save stopped by YAML that does not parse opens the tab and the field with the error; a hook filter that hides the row
is cleared.

A field the entry does not set shows the inherited value, marked as such; setting it writes it into the entry,
resetting removes it again. `enabled` is never inherited: the app reads it from the entry itself.

**Resolved views** (effective, inherited, removed agents, `/inherited`, the tool lists and descriptions) show what
the loader made of the files, with two things put back:

- **Secrets:** values the config expands from `${NAME}` show as `${NAME}` — for every variable a config file
  references and every name in `config/secrets.env`. A value of 8 characters or more is replaced wherever it appears
  in a string; a shorter one only where it is the whole string. The tool preview expands only these variables in the
  lists it is sent; any other `${NAME}` stays as typed. Error details show the values of these variables, and of
  those a sent entry names, as `${NAME}` as well: they may quote what the loader expanded.
- **Templates:** the loader makes `./` template paths absolute; under the repository they show relative to it
  (`config/agents/prompts/x.md`), which is also how the loader reads any other template. An override that copies
  such a value into an entry writes a valid path, and the preview can read it.

## How saving works

**Only the agent's own entry is edited**, exactly as written in its file: `${VAR}` stays unexpanded, `+`/`!` list
prefixes and `./` paths stay as they are. Inherited values are never copied in.

**Save** first shows the change as a diff (a dry run, nothing written), then writes on confirmation:

1. **Conflict check** — the panel sends the file version it loaded: the hash of exactly the bytes the shown entry was
   read from. If the file changed on disk meanwhile, the save is refused (`409`, "changed on disk; reload") and
   nothing is written — also when the list itself is up to a second old. The file is read once more right before the
   write, so a save by someone else while the edit was rendered is not overwritten either.
2. **In place, comments kept** — the edited file is rendered with ruamel.yaml, and only the lines the edit changed are
   taken from that rendering; every other line is copied from the file byte for byte. So a file that ruamel would
   otherwise reformat (trailing blanks, `null`, spacing inside `[a,  b]`) keeps its layout, and CRLF files stay CRLF.
   Keys the entry no longer has are removed, new keys go to the end of their block, a changed list keeps its flow
   style, the quoting and comments of the items it still holds, and the comment block that followed it. Values are
   written so the loader's YAML 1.1 parser reads them back unchanged: `1e-05` as `1.0e-05`, keys and strings like
   `on`, `yes`, `null`, `y` quoted. The result is parsed with the loader's parser and must read back as exactly the
   intended data, else nothing is written (`400`, naming the key and value that cannot be written, or saying the
   file's layout does not allow an in-place edit).
3. **Atomic write** — a temp file in the same directory, then a rename; file permissions are kept. A replace Windows
   refuses because another process holds the file is retried a few times. A read-only file is not editable.
4. **Check and rollback** — the whole config is loaded again, and the agent plus every agent that inherits from it is
   checked: a type chain that ends in an unknown type, an inheritance cycle, the inheritance merge, every profile of
   both chains, the effective `system_template` (unless an inline `system_prompt` wins). An entry that does not
   resolve is checked on the fields it sets itself. A problem that was not there before the write restores the old
   file and answers `422`, one `location: message` per line; problems an entry already had neither block a save nor
   hide a new one.
   - If the file changed again while it was checked, it is left alone and the answer (`409`, "changed on disk")
     says so.
   - If the old content cannot be put back, the answer (`500`) names the file and says it holds the rejected
     content; the same goes to the log as an error.

**New agent** writes `config/agents/<name>.yaml` (header comment *Created with the Agent Editor.*). The name must match
`^[a-z][a-z0-9_]{1,63}$` and must not be a server, a plugin type or an existing file; the file is created exclusively
(a file made meanwhile is a `409`), and only under the repository root. A copy re-bases `./` and `../`
template paths from the source's file to the new one. If the new file is not picked up by the config includes, or the
agent does not load, the file is removed again.

**Delete** removes the entry — refused while another entry inherits from it (the children are named). The entry's
lines go together with the comment block directly above its key (comments at the key's own indentation; a deeper
one ends the entry before and stays), every comment inside it, and one separating blank
line. When it was the file's last server, the file keeps `servers: {}`; it is deleted only if nothing but blank
lines, the structure and the editor's own header would remain (a comment elsewhere in the file keeps it). The answer
names sub-agent managers whose own `allowed_agents` still list the agent.

**Sub-agent access** edits the manager's own lists as little as possible and keeps each list's form. A list the
manager does not have, or has with `+`/`!` entries, gets `+name` (or `!name` to lift a block it inherits), so the
inherited list keeps applying; a plain own list gets the bare name. Allowing takes the agent off `blocked_agents` and,
if still not allowed, adds it to `allowed_agents`; blocking takes it off `allowed_agents` and, if still allowed, adds
it to `blocked_agents`. A merge list that becomes empty is removed, not left as `[]` — that would replace the
inherited one. The rule shown per manager is the list entry that decides, in the manager's order: blocked, `*`,
exact name, pattern.

## What needs a restart

Agents are built once when the app starts. The panel compares each resolved entry on disk with what the app runs —
the whole entry except `enabled`: type, description, metadata, self tool descriptions, plugin keys and
`agent_config`. The running side is the app's declaration, with the `agent_config` of the built instance when there
is one (a config reload changes that one).

| State | Meaning |
|---|---|
| `in_sync` | disk and app agree |
| `changed` | they differ; the changed keys are listed — `agent_config` keys by name (`max_steps`, with `tools.allowed`, `hooks.overrides`, `skills.always` one level down), other keys by name, mappings one level down (`metadata.visibility`, `config.depth`) |
| `new` | enabled on disk, not started — needs a restart |
| `removed` | started, but disabled or gone on disk — needs a restart |
| `off` | disabled and not started |

**Reload config** (`POST /admin/reload-config`) applies only `max_steps`, `fallback_recovery_seconds`,
`auto_escalate_on_stuck`, `escalate_rounds`, `escalate_max_calls`, `escalate_error_streak`, and only to agents that
are built and registered as plugin servers (the reload walks that registry), plus a manager's
`allowed_agents`/`blocked_agents`. For those agents the panel lists the reloadable changes separately. Everything
else — model chain, tools, prompt, hooks, visibility, and any change to an agent that is not built yet — takes a
restart. The panel only says so; the restart is the operator's.

## Access

Every endpoint, reads included, checks for an **active admin in the user database** itself, on top of the route
rules; the role written in a token does not count, API keys are refused. Writes (`POST`, `PUT`, `DELETE`) take
`Content-Type: application/json` only, otherwise `415`. Prompt previews read only relative paths to `.md`, `.txt`,
`.markdown` files inside the repository, and at most 200 000 characters of them. Absolute, UNC (`//host`, `\\host`),
drive (`C:`) and stream (`file:name`) paths are refused as text, before the path is touched — on Windows even
resolving a UNC path opens a connection to that host. `/yaml/parse` refuses anchors and aliases, and anything that
would not come back as the same JSON (dates, sets, binary, NaN, keys that are not text): quote such values.

## Endpoints

All under `/plugins/agent_editor/`, JSON in and out. Error `detail` is always a string.

| Method | Path | Does |
|---|---|---|
| `GET` | `/` | the panel |
| `GET` | `/agents` | `{agents: [row], errors}` — name, type, base, enabled, description, visibility, category, tags, group, file(s), editable, readonly_reason, state, changed, restart, problems; a failed tool catalogue leaves the patterns unchecked and says so in `errors` |
| `GET` | `/agents/{name}` | `own`, `inherited` (default_config ⊕ parent chain, `enabled` false), `effective`, parent, children, version, live state with `reload_fields`, sub-agent access, prompt file, `form_reason` (why the form cannot save the entry, or null); `404` for an entry that is no agent |
| `GET` | `/inherited?type=` | `{inherited}` — what an entry of that type gets without keys of its own: a server's resolved entry, or default_config with a plugin type; without `type`, `basic_agent` (the loader's default); `enabled` false |
| `POST` | `/agents` | create: `{name, entry, source?, dry_run}` → `{name, file, diff, version?}` |
| `PUT` | `/agents/{name}` | save: `{entry, version, dry_run}` → `{diff, version?}` |
| `DELETE` | `/agents/{name}` | delete: `{version, dry_run}` → `{diff, deleted_file, notes}` |
| `GET` | `/managers` | `{managers: [{name, file, version, editable, readonly_reason, own, allowed, blocked, spawns}], agents}` — `spawns`: the agents the manager's lists let it start |
| `POST` | `/managers` | `{name, allowed_agents, dry_run}` → `{name, file, diff, version?}`; a new manager in `config/agents/<name>.yaml` |
| `PUT` | `/managers/{name}` | `{entry, version, dry_run}` → `{diff, version?}`; an enabled manager's own entry, `type` and `enabled` unchanged |
| `PUT` | `/agents/{name}/spawnable` | `{sam, allowed, version, dry_run}` → `{diff, version?}`; `version` is the manager file's |
| `GET` | `/meta` | agent classes, LLM profiles, skills, hooks (one row per name, with its `types`), prompt files, visibility values, reloadable fields, `sub_agents` (the manager plugin is installed) |
| `GET` | `/tools` | `{servers: [{server, type, tools: [{name, description}]}]}` — the servers tool discovery finds |
| `POST` | `/tools/effective` | `{name?, type?, allowed, blocked}` → `{allowed, blocked, tools, per_tool, counts, unmatched, external}`: the merged lists, the granted `server/<tool>` paths, per named tool the patterns that grant (`allowed_by`) and block (`blocked_by`) it, tools per pattern, the patterns (allowed or blocked) that name nothing, and the patterns for external MCP servers with whether their server is enabled (not judged against the catalogue). Sent lists are read as the loader reads a file (`${VAR}` expanded), so a masked list copied into the entry still matches. A list sent as null is the inherited one; a missing `type` is the entry's type on disk, `"type": null` means no own type (`basic_agent`) |
| `GET` | `/prompt?path=&agent=` | a template file; `./` paths relative to the agent's file |
| `POST` | `/yaml` | `{entry}` → `{yaml}` |
| `POST` | `/yaml/parse` | `{yaml}` → `{entry}` |

Answers: `400` the write is not possible (entry read-only, name taken, entry still inherited from, a list the
manager cannot change that way, a value or layout that cannot be written in place, a refused prompt path), `401` not
signed in, `403` not an active admin or authentication off, `404` unknown agent, type or manager, `409` only when the
file changed on disk (the detail says "changed on disk"), `415` not JSON, `422` invalid request or an entry that does
not load (the file is back as it was), `500` the old content could not be put back.

For tests, `config_path` and `root` in the server entry point the plugin at another config tree; without them it uses
the app's config and the working directory.

## Model Experience

Not applicable: the plugin gives agents no tools and injects nothing.

## Known gaps

- **Read-only entries.** An entry defined in more than one file (the loader merges them) is shown but not editable,
  and so is a read-only file, a file outside the repository, a file with mixed CRLF/LF line endings, or one ruamel
  cannot read (for example a duplicate key, which the loader silently accepts). An entry holding a value JSON cannot
  carry (a date, a number as a key, `.inf`) is shown with that value as text, and neither its form nor a copy of it
  can be saved: the form would send it back changed. Deleting it and the sub-agent switches still work; they edit the
  parsed file. A file included twice counts once.
- **Manager lists must be lists.** A sub-agent manager whose own `allowed_agents` or `blocked_agents` is a single
  value (`"*"`) is not changed by the switches; the answer says so.
- **Rare layouts are refused.** When the in-place result would not read back as the intended data, nothing is written
  and the save says so. Measured on the real tree (122 agents, a key added to each): 3 refusals — an entry whose keys
  are indented irregularly, and a file that ends inside a block scalar without a final newline.
- **Rollback window.** Between the write and the check the new file is on disk for about a tenth of a second; a config
  reload or agent start in exactly that moment would read it.
- **Comments inside a replaced list.** Comments on items the new list still holds are kept, the comment block after
  the list stays below it. A list of mappings that changes loses the comments inside the mappings it replaces. A list
  written as `[]` becomes a block list when it gets its first item.
- **Tool catalog.** Only internal servers the app has built: external MCP servers and agents not built yet (lazy
  start) are not in the picker; their patterns show as unmatched.
- **Secret masking is textual.** A secret shorter than 8 characters is shown only where it is a whole value; inside
  a longer string it stays visible (masking it there would rewrite unrelated words). A removed agent's variables are
  masked only when a file or `secrets.env` still names them.
- **Quiet checks.** The editor's own config loads (after a file change, and the check after a write) do not log
  the loader's report again; the app logged it at its start and logs it on every reload.
- **Live state without a runtime.** In an app without a runtime in its state (not the case for `agent-api`), every
  state is empty.
- **Other editors.** An edit made by someone else shows up in the list within a second; the version check keeps a
  save from overwriting it.
