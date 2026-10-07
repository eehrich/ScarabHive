# Configuration-Based Agents

An agent in ScarabHive is a YAML entry: a name, a prompt, the LLM profiles it
runs on and the tools it may call. No Python is needed for agents that differ
in prompt, tools, model and limits. An agent that needs its own logic is a
plugin -- see [Plugin Authoring](plugin_authoring.md).

The complete examples load as written; fragments show only the keys in
question. Every field and command was checked against the code.

## Quick start

### 1. The agent

`config/agents/my_analyst.yaml`:

```yaml
plugins:
  servers:
    my_analyst:
      type: basic_agent
      enabled: true                       # the default is false
      description: "Researches a topic on the web and answers with sources."
      self_tool_descriptions:             # server level, not in agent_config
        my_analyst_execute_task: "Research a topic on the web and answer with sources."
      metadata:
        visibility: both                  # the default is private
      agent_config:
        llm_profile: [normal, think]      # a chain: primary, then fallbacks
        max_steps: 20
        system_template: "./prompts/my_analyst.md"   # relative to this YAML file
        template_vars:
          tone: concise
        tools:
          allowed: ["duckduckgo_search/*", "web_scraper/*", "datetime/*"]
        hooks:
          overrides:
            context_engineer.engineer_context: {enabled: true}
```

### 2. The prompt

`config/agents/prompts/my_analyst.md`:

```markdown
You are a research analyst. Keep your answers {{ tone }} and cite every source with its URL.
```

### 3. Check and run it

```bash
validate-agents config/agents/my_analyst.yaml      # the file's schema
agent-cli run --agent my_analyst "Summarise today's news on fusion power"
agent-cli chat --agent my_analyst                  # interactive; /agent lists the agents
agent-run --agent my_analyst "..."
```

`agent-cli` and `agent-run` read the configuration when they start, so a new
agent is there at once. The API (web UI) needs a restart for a new agent.
Without `--agent`, the CLIs and the web UI start `default_agent` from
`config/config.yaml`.

`validate-agents` checks each file on its own: YAML syntax and the fields of
`agent_config`. A misspelt server-level key such as `enabeld`, or a list mixing
`+` with plain entries, passes it. It does not merge the defaults, follow
`type:` inheritance or open the prompt template -- the start does that (see
[Troubleshooting](#troubleshooting)).

## Where agents live and how they load

- **Every agent is a server entry** under `plugins: servers: <name>:`. A
  top-level `agents:` key is not read -- the block is dropped without an error.
- **Files:** `config/config.yaml` includes every `config/agents*/*.yaml` and
  every `src/plugins*/<plugin>/agents/*.yaml`. A file with a YAML syntax error
  is skipped as a whole, with a line in the log.
- **Defaults:** every entry starts from `plugins.default_config` in
  `config/plugins.yaml`: `type: basic_agent`, `enabled: false`,
  `llm_profile: [normal, think]`, `max_steps: 20`,
  `system_template: config/prompts/system_prompt.md`, hooks on.
- **Typos:** `agent_config` is strict -- one unknown key in any agent stops the
  whole configuration from loading ("Extra inputs are not permitted"). At server
  level, in `metadata` and in `hooks`, unknown keys are ignored without a word: a
  misspelt `enabeld: true` leaves the agent off.
- **Changes:** a prompt template is read from disk on every render, so an edit
  applies at once, even to runs in flight. `agent-cli reload` (or
  `POST /admin/reload-config`) refreshes only `max_steps`,
  `fallback_recovery_seconds`, `inherit_parent_llm`, the escalation knobs and
  `metadata.min_role` of running agents; `llm_profile`, `llm_params`, tools,
  hooks, timeouts, `system_prompt`, `template_vars` and the `system_template`
  path need a restart.

## Server-level fields

| Field | Default | Meaning |
|-------|---------|---------|
| `type` | `basic_agent` | A plugin type (its folder name) or the name of another agent to inherit from (see [Inheritance](#inheritance-and-list-merging)). An unknown type is skipped at start with a warning. |
| `enabled` | `false` | Whether the agent is built at start -- for the API, sub-agent managers and agents called as tools. `agent-cli --agent` and `/agent` build any entry with an `agent_config` on demand, enabled or not: an agent that works in `agent-cli` can still be missing from the web UI. |
| `description` | -- | One line for people: the web UI's agent picker (`GET /agents`). No model sees it -- a calling agent reads the tool description, set with `self_tool_descriptions`. |
| `metadata` | -- | `visibility`, `min_role`, and free `author`, `version`, `tags`, `category`. Other keys are dropped. |
| `self_tool_descriptions` | -- | Descriptions for the agent's own tools, by full tool name: `<name>_execute_task`, `<name>_list_available_tools`. |
| `agent_config` | from `default_config` | Everything below. |

### `visibility` and `min_role`

- **`visibility`** decides where the agent shows: `ui` (the agent list of the
  web UI and `GET /agents`), `tool` (other agents can call it as the tool
  `<name>_execute_task`), `both`, or `private` -- the default, shown nowhere. A
  sub-agent manager offers every agent that is not `private`. It only hides; it
  does not stop a run by name. See
  [Agent Visibility](agent_visibility.md).
- **`min_role`** (`guest`, `user`, `admin`) decides who may run the agent, on
  every path: HTTP, sub-agent manager, agent called as a tool, state machines,
  wakes. It applies while authentication is on; in `agent-cli` and `agent-run`
  the local operator passes. Give it `admin` for anything with a shell, a coding
  CLI, SSH, code execution, or file access to the checkout or to `config/`.
- `metadata` is merged along `type:`: an agent based on a gated one inherits the
  gate, and `min_role: null` does not lift it -- set a lower role explicitly.

## `agent_config` fields

| Field | Default | Meaning |
|-------|---------|---------|
| `llm_profile` | `[normal, think]` | A profile name or a chain `[primary, fallback, ...]` from `llm_system.profiles`. `[]` is an error. |
| `llm_profile_advanced` | `[]` | A chain of its own for the advanced model (`use_advanced_model`). |
| `llm_params` | -- | Request parameters per profile: flat for every profile of the chains, or keyed by profile name (`"*"` for all); the exact key wins. |
| `inherit_parent_llm` | `false` | As a sub-agent, run on the model the calling run was switched to (see below). |
| `fallback_recovery_seconds` | `3600` | Longest block the agent places on a failing LLM. |
| `max_steps` | `20` | Upper bound of tool-calling steps per run. |
| `system_template` | `config/prompts/system_prompt.md` | Prompt file: `.md`, `.txt` or `.markdown`. |
| `system_prompt` | -- | Prompt text inline; it beats `system_template` (see [System prompt](#system-prompt)). |
| `template_vars` | -- | Variables for the prompt, any YAML value. |
| `skills` | -- | `always: [...]` (bodies appended to the prompt) and `on_demand: [...]` (only an index; the agent reads a body with the `skills` tool, so it needs e.g. `skills/*` in `tools.allowed`, else the index is left out with an error). A bare list means `always`. |
| `tools` | deny all | `allowed`, `blocked`, `deferred` (see [Tools](#tools)). |
| `hooks` | `enabled: true` | Per-agent hook switches (see [Hooks](#hooks)). |
| `timeouts` | from `default_config` | Internal safety limits (status queue, session lock, LLM polling, tool cleanup); there is no run timeout here. |

Further knobs -- `loop_detection`, `reasoning_loop`, `auto_escalate_on_stuck`,
`escalate_rounds`, `escalate_max_calls`, `escalate_error_streak`,
`output_cap_notes` -- are documented on `AgentConfig` in
`src/agent_system/config/models.py`.

## LLM profiles

An agent names **profiles**, not models. A profile lives under
`llm_system.profiles` (in `config/llm.yaml` or `config/llm_openrouter.yaml`)
and points at a model under `llm_system.models`:

```yaml
llm_system:
  models:
    my-model:
      provider: openai
      model: gpt-4.1-mini
      temperature: 0.7
  profiles:
    my-profile:
      model_ref: my-model
      description: "Small and fast"
```

An entry under `models` alone is not a profile: `llm_profile: my-model` fails
with "Profile 'my-model' not found in LLM system profiles". The shipped
configuration defines profiles such as `normal`, `think`, `turbo`, `chat` and
`code`; which model each one uses is up to your `config/llm.yaml`. A chain that
names an unknown profile is reported at start, and if it is the primary one the
log says the agent will not start.

### Fallbacks

If the LLM of a step fails, the agent continues on the next profile of its
chain:

```yaml
my_agent:
  type: basic_agent
  agent_config:
    llm_profile: [normal, think, turbo]   # primary, then fallbacks
    fallback_recovery_seconds: 1800       # longest block (default 3600)
```

After its own chain, the fallback runs through the other chain
(`llm_profile_advanced`, or the reverse), without repeats. If the run is on a
profile other than its own (API `llm_profile`, `/model`, inherited from the
caller), the primary profile of its own chain is the first fallback.

> `llm_profile_fallbacks` was removed. A chain lives in `llm_profile` itself and
> the advanced model has its own chain in `llm_profile_advanced`; a config that
> still sets `llm_profile_fallbacks` is rejected at load with a migration hint.

**Blocks belong to the LLM, not to the agent.** A 429, an exhausted quota or a
rejected key (401/402/403/404) blocks the LLM for every agent in the process:

1. A 429 blocks for 60 s (or the provider's `retry_after`, if longer), each
   further rejection doubles the pause up to `fallback_recovery_seconds`; an
   exhausted quota and a rejected key block for that long at once.
2. Each step takes the desired LLM (escalation, the choice in the chat,
   config) if it is free, otherwise the first free profile of the chain,
   otherwise the desired one anyway.
3. The first answer from the LLM lifts the block for everyone.
4. 5xx, connection errors and request-shaped errors (400/413/422) block
   nothing -- they only move the current request along the chain.
5. If the chain is exhausted, the error of the last attempt goes to the caller.

Details: [Agent architecture](_arch_agent_architecture.md), section "LLM
fallback and blocks".

### Inheriting the caller's LLM (`inherit_parent_llm`)

A sub-agent normally runs on its own chain. Some do the work of their caller
and should run on its model -- a skills or coding helper that would otherwise
be the only part of the job left on the weaker model:

```yaml
skills_agent:
  agent_config:
    inherit_parent_llm: true
```

1. **Only a switch is passed on.** If the calling run was set to a profile
   other than its own -- `llm_profile` of the API, the model picker in the web
   chat, `--llm` of the CLI, `/model` in the chat, `use_advanced_model` -- the
   sub-agent runs on that profile. A caller on its own chain, or with only
   different parameters on its own primary profile (`--llm-params` alone),
   passes nothing on.
2. **It stays itself.** Its `llm_params` for that profile apply; its chain
   remains the fallback, its primary profile first.
3. **A choice made for exactly this run wins:** an override the sub-agent start
   passes along, or `use_advanced_model` for an agent with an advanced chain.
4. **Further down only if every level wants it.** A grandchild inherits from
   the sub-agent, not from the grandparent run.
5. **A profile the config does not know** leaves the sub-agent on its chain,
   with a warning in the log.

The profile travels with the run (`agent_system/llm/caller_llm.py`), not
through the sub-agent manager: every plugin that starts an agent inside a tool
call passes it on. A run started later in another process (a wake, a job
worker) gets nothing and runs on its chain. An agent with `type: <other agent>`
inherits the switch; `skills_agent_multimodal` sets it back to `false` because
it needs a model that reads media.

## System prompt

**Which prompt wins:** the prompt an agent's own Python class returns from
`get_custom_system_prompt()` (plugin agents only), then `system_prompt`, then
`system_template`, then the built-in default. Since
`default_config` gives every agent a `system_template`, a `system_prompt` --
also one **inherited** from the agent named in `type:` -- silently beats your
template. Check the parent when your template seems ignored.

**Template files** (`system_template`):

- The whole file is the prompt, rendered with Jinja2. Only `.md`, `.txt` and
  `.markdown` are accepted.
- A path starting with `./` or `../` is relative to the YAML file; any other
  path is relative to the project root.
- `{% include "part.md" %}` looks in the template's own folder, then in
  `config/prompts/`.
- `<!-- ... -->` comments, in the template and in its includes, are removed
  before rendering: the place for notes to whoever edits the prompt.
- The file is read on every render.

**Inline prompts** (`system_prompt`) are rendered with Jinja2 too, but keep
their HTML comments (they reach the model) and cannot `{% include %}`.

**When something is wrong:** a missing template file fails the first render
(agents built lazily are checked at start); a template that renders empty logs
a warning and the default prompt is used; a Jinja error in a template file
sends the unrendered text with a warning; in an inline `system_prompt` -- an
`{% include %}` among them -- it replaces the whole prompt with the built-in
default ("You are an assistant agent."), with a warning; an undefined variable
renders as an empty string, without a warning.

The bodies of `skills.always` and the index of `skills.on_demand` are appended
after whichever prompt won.

## Template variables

### Built-in variables

| Variable | Content |
|----------|---------|
| `tools` | What this agent may call: server names, tool names, and `server.tool` for tools of external MCP servers |
| `max_steps` | The configured step limit |
| `current_step` | The current step -- **changes every call, see below** |
| `current_date`, `tomorrow_date` | Dates (YYYY-MM-DD) |
| `current_time`, `current_datetime`, `unix_timestamp` | **Change every call, see below** |
| `current_weekday`, `current_month`, `current_year` | Parts of today's date |
| `current_timezone`, `current_location` | From `context.timezone` and `context.location` in `config/config.yaml` |

The date and time variables exist only while `context.auto_datetime` is on.

### What is installed: `tools`, `has_tool()`, `plugins`, `mcp_servers`

| Variable | Content |
|----------|---------|
| `tools` | What **this agent** may call, by its allow and block patterns |
| `has_tool(pattern)` | Asks `tools` with an fnmatch pattern, case-sensitive. Tool names carry the instance name (`coder_sam_manage_sub_agent`), hence `has_tool('*_manage_sub_agent')`, `has_tool('github.*')` |
| `plugins` | Plugin types installed **and** enabled (one instance with `enabled: true`), whether or not this agent may use them |
| `mcp_servers` | External MCP servers with `enabled: true` in `config/mcp_servers.yaml` (when the `mcp_client` plugin runs) |

```jinja
{% if has_tool('*_manage_sub_agent') %}
You hand large subtasks off to sub-agents.
{% else %}
You work alone; split large tasks into steps.
{% endif %}
{% if 'web_scraper' in plugins %}Pages can be read in full.{% endif %}
```

The plugin id is the plugin type: its folder name, the one under `type:` in
`plugins.yaml`. If a type exists in two plugin folders, the first wins and the
start reports it.

`plugins` and `mcp_servers` come from the configuration, never from a live
state: an MCP server that is not connected right now is still listed. `tools`
is fixed for a run; the tools of an MCP server that was not connected when the
run started are missing from it. So `has_tool('github.*')` asks whether the
agent has them now, `'github' in mcp_servers` whether they are meant to be
there.

**Keep the system prompt stable.** It is rendered again before every step and
is the start of the prompt the provider caches; a value that differs between
two calls bills the whole conversation behind it again, on every call. So no
`current_step`, `current_time`, `current_datetime` or `unix_timestamp` in a
system prompt. `tests/config/test_prompts_have_no_ticking_clock.py` checks the
`.md` prompts under `config/` and `src/`; nothing stops one at runtime.
`current_date` changes once a day and is fine; an agent that needs the
exact time has the `datetime` tool.

### Your own variables: `template_vars`

```yaml
agent_config:
  system_template: "./prompts/support.md"
  template_vars:
    product: "ScarabHive"
    languages: [English, German]
    escalation:
      email: "support@example.com"
      hours: "9-17 CET"
```

```markdown
You support users of {{ product }} in {{ languages | join(' and ') }}.
Escalate to {{ escalation.email }} ({{ escalation.hours }}).
```

Any YAML value works: strings, numbers, lists, nested objects. Your variables
beat the built-in ones of the same name, and session variables (below) beat
both.

A new session takes a copy of `template_vars` into its session variables and
saves it with the session. Changing a value in the YAML therefore does not
reach existing sessions -- only new keys do.

## Session variables

Session variables are the per-session layer on top of `template_vars`. They
start as the copy described above and change while the session lives:

- `/vars` in the chat (web and `agent-cli`), and `agent-cli --vars KEY=VALUE`;
- the `set_context` tool of the `task_switch` plugin (`<instance>_set_context`);
- plugins that set them for their session.

A **sub-agent** inherits all variables of the session that starts it; when it
is continued, it gets the caller's current values again, and what it changed
itself survives. A variable that was never set renders as an empty string.

The Session panel of the web UI shows them in its "Context variables" card,
and those of sub-sessions in the "Sub-sessions" tree.

### Phase filtering in a sub-agent manager

A sub-agent manager can narrow the agents it starts to the current phase of a
workflow, read from a session variable:

```yaml
plugins:
  servers:
    project_sam:
      type: sub_agent_manager
      enabled: true
      allowed_agents: [planner, coder, reviewer]
      phase_filtering:
        enabled: true
        phase_variable: workflow_phase     # the session variable to read
        phase_agents:
          planning: [planner]
          build: [coder]
          review: [reviewer]
          _default: []                     # unknown phase: all allowed_agents
```

- The manager reads `phase_variable` from the calling agent's session
  variables, falling back to that agent's `template_vars`. No phase set means no
  narrowing.
- Starting an agent outside the phase is refused with
  `error_type: "phase_blocked"`.
- The tool description still lists all `allowed_agents`, with a note that a
  phase filter is active; the agents of the current phase are named in the
  sub-agent context the manager gives the model, and in its panel.

How to make an agent startable as a sub-agent at all: the
`sub_agent_manager` guide (`src/plugins/sub_agent_manager/sub_agent_manager.guide`).
In short: the agent is enabled, its name is in the manager's `allowed_agents`,
the caller allows the manager (`"project_sam/*"`), its `visibility` is not
`private`, and the caller passes its `min_role`.

## Tools

```yaml
tools:
  allowed:
    - "web_scraper/*"                   # every tool of the server web_scraper
    - "file_ops/file_ops_read_file"     # one tool: the FULL tool name
    - "github.*"                        # tools of the external MCP server github
    - "duckduckgo_search/*"
  blocked:
    - "web_scraper/web_scraper_download"
  deferred:
    - "duckduckgo_search/*"             # allowed above; only its schema waits until loaded
```

- Patterns name **server instances**, not plugin types: a second instance
  (`workspace_file_ops` with `type: file_ops`) has its own tool names and needs
  its own entry. A tool name carries its instance as prefix --
  `file_ops/read_file` matches nothing, `file_ops/file_ops_read_file` does.
- `"*"` allows everything; a bare `server` (no slash) means all its tools.
- Tools of external MCP servers use the dot form `server.*`; `server/*` does
  not match them.
- An empty `allowed` means **no tools**. `blocked` is applied after `allowed`.
- `deferred` takes the same patterns and changes nothing about what the agent
  may call; it only holds the schemas back until the model loads them with the
  core tool `tool_search`. See [Deferred tools](deferred_tools.md).

`/tools` in the chat shows the tools the agent really has.

## Inheritance and list merging

`type: <other agent>` makes an agent start from that agent's merged
configuration -- prompt, chains, tools, metadata. Lists are **replaced** by
default; prefixes merge:

| Prefix | Behaviour | Example |
|--------|-----------|---------|
| `+item` | Appended to the inherited list | `+media_ops/*` |
| `!pattern` | Removes matching inherited items (an fnmatch pattern; `x/*` also takes the bare `x`) | `!terminal/*` |
| `item` (no prefix) | The list replaces the inherited one | `web_scraper/*` |

From `config/agents/agents.yaml`:

```yaml
skills_agent_multimodal:
  type: skills_agent                # everything skills_agent has ...
  enabled: true
  agent_config:
    llm_profile: [or-gemini-flash, or-gemini-flash-lite, or-claude-sonnet]   # replaced
    inherit_parent_llm: false
    tools:
      allowed:
        - "+media_ops/*"            # ... plus the media tools
```

This works for any list -- `llm_profile`, `blocked`, `allowed_agents` -- and
also against `default_config` for an agent without a parent agent:
`llm_profile: ["+turbo"]` gives `[normal, think, turbo]`.

A list is either merged or a replacement, never both. Mixing prefixed and
unprefixed entries in one list of an enabled agent stops the start --
`agent-cli`, `agent-run` and the API alike -- with a `ValueError` naming the
key and the entries ("mixes list merge syntax with replacement entries"). It
is almost always a forgotten `+`, and guessing either way would be wrong half
the time.

## Hooks

```yaml
agent_config:
  hooks:
    enabled: true                      # false: no hooks at all for this agent
    overrides:
      context_engineer.engineer_context: {enabled: true}
      context_summarizer.summarize_context: {enabled: false}
```

- Keys are `<instance>.<hook>`. An agent's override beats the hook's own
  default; extra keys in it reach the hook as `context.hook_config`.
- A key that matches no registered hook (a disabled plugin, a typo) is reported
  as a warning at start.
- Timeouts are set only in the global `hooks.overrides` of `plugins.yaml`.

Context handling -- trimming, summarising, engineering the context -- is done
by such hooks, not by an agent field. See [Plugin Hooks](plugin_hooks.md).

## Seeing what loaded

| Where | What |
|-------|------|
| `agent-cli chat`: `/agent` | The agents you can switch to |
| `agent-cli chat`: `/tools` | The tools this agent really has |
| `GET /agents` | Agents with `visibility` `ui` or `both` |
| `GET /agents/{name}/tools`, `GET /agents/{name}/allowed-tools` | Its tools, and its allow patterns |
| `GET /agents/debug/{name}/system-prompt` (admin) | The rendered system prompt |
| `GET /admin/config` (admin) | The loaded configuration |

## Troubleshooting

**The agent is missing from the list or the web UI.**
`enabled` is `false` (the default), or misspelt; `visibility` is `private`
(the default); the file is not under an included path, or uses a top-level
`agents:` key; the file has a YAML error (see the log); the API was not
restarted after the agent was added.

**Nothing loads at all.** A validation error in some `agent_config` -- an
unknown key ("Extra inputs are not permitted"), `llm_profile: []`, a
`system_template` that is not `.md`/`.txt`/`.markdown`, invalid `llm_params` --
or a list in an enabled agent that mixes `+`/`!` entries with plain ones. The
error names the agent and the key.

**The agent does not start.** Its primary profile is not under
`llm_system.profiles` (the start log says so), or its `system_template` does
not exist.

**A tool is missing.** The pattern uses the plugin type instead of the
instance name, or a short tool name instead of the full one; MCP tools need
the dot form. Check with `/tools`.

**The template is ignored.** A `system_prompt` wins -- possibly one inherited
through `type:`.

**A sub-agent cannot be started.** See the list under
[Phase filtering](#phase-filtering-in-a-sub-agent-manager): enabled, in
`allowed_agents`, manager allowed by the caller, `visibility` not
`private`, `min_role` met.

## When to write code instead

A YAML agent covers prompt, model, tools, limits and hooks. An agent with its
own `execute_task`, its own tools or state of its own is a plugin with a
`SchemaBasedAgent` subclass; a whole package of agents, prompts and skills
without Python is a config-only plugin. Both: [Plugin Authoring](plugin_authoring.md).

## See also

- [Plugin Authoring](plugin_authoring.md)
- [Plugin Hooks](plugin_hooks.md)
- [Agent Visibility](agent_visibility.md)
- [Deferred tools](deferred_tools.md)
- [Agent architecture](_arch_agent_architecture.md)
