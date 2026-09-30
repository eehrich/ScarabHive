# Agents, sub-agents, config-only, LLM providers

## Agent without Python (the normal case)

An agent is a server entry with `type: basic_agent` (or the name of another agent it
inherits from):

```yaml
plugins:
  servers:
    my_agent:
      type: basic_agent
      enabled: true                      # default false
      description: "One line for humans and the SAM list."
      metadata:
        visibility: tool                 # ui | tool | both | private (default); display only
        min_role: admin                  # guest | user | admin; who may RUN it; absent = no gate
      agent_config:                      # extra="forbid": a typo fails the load
        llm_profile: [primary, fallback]
        llm_profile_advanced: []         # explicit, else inherited from default_config
        max_steps: 30
        system_template: "./prompts/my_agent.md"
        template_vars: {}
        skills: {always: [], on_demand: []}
        tools:
          allowed: ["file_ops/*", "my_sam/*"]
          blocked: []
          deferred: []                   # allowed tools sent as a name until tool_search loads them
        hooks:
          overrides:
            context_engineer.engineer_context: {enabled: true}  # <instance>.<hook>, full name!
```

- File under `config/agents*/` or `src/plugins*/<plugin>/agents/` — both globs are
  included in `config/config.yaml`.
- `self_tool_descriptions` belongs at **server level**, not in `agent_config`.
- `system_template` with `./` or `../` resolves relative to the YAML; only `.md`,
  `.txt`, `.markdown`. `<!-- -->` comments are stripped before Jinja.
  `default_config` sets `config/prompts/system_prompt.md` for every agent.
- Prompt language: English outside `src/plugins_writer/`.
- Visibility: `tool`/`both` → callable as a tool by other agents; `ui`/`both` → in the UI.
  It only hides; it does not stop a run by name.
- Called as a tool, an agent runs on a session of its own per caller session
  (`Agent.tool_session` → `tool_session_id(caller, name)`), never on the caller's:
  it remembers its calls in that caller session, is saved under the call's user
  below the caller's session (hidden like a SAM sub-session), and a throwaway
  caller's is never saved (if the agent starts sub-agents, the SAM files it as the
  listed "Coordinator Session" it makes for any missing parent). A call to an
  agent that runs above it already (itself, directly or through agents called as
  tools) is refused: `error_type: "recursive_call"`; across a SAM or stategraph hop
  the nesting budget bounds it (stategraph only where a SAM above set one). A session
  that cannot be stored with the caller's budget: `"tool_session_unavailable"`. A
  deleted one is forgotten; the next call starts it afresh. A custom
  `execute_task` takes it from
  `session_id, refusal = await self.tool_session(params)` and answers a refusal
  with `{"status": "error", **refusal}` (and a `collect_final_result` whose
  `refused` is set with that error_type) -- passing `params["_session_id"]` to the
  run writes the agent's transcript into the caller's session file.
- `min_role` gates who may run the agent on every path -- HTTP (answered like an
  unknown agent, except `/run`/`/events` for the default agent with no name and
  `POST /api/sessions`: 403), SAM (`error_type: "agent_role_gate"`), agent as a tool, stategraph, wakes,
  and each `<name>_*` tool (`src/agent_system/auth/agent_access.py`).
  - No identity (no registered request owner, no session user): judged as
    `anonymous` -- refused unless anonymous access is enabled with a sufficient
    role; the SAM and the agent's tools refuse it outright. `cli_user` is the local
    operator only in `agent-cli`/`agent-run`.
  - **Inheritance trap:** `metadata` is deep-merged along `type:` -- an agent based on
    a gated one inherits the gate, and `min_role: null` does NOT lift it. Set a lower
    role explicitly (`guest`/`user`) and check with `load_settings` +
    `get_tool_server_config`.
  - Gate (admin) anything with a shell, a coding CLI, SSH, a tool that runs arbitrary
    code (`blender_execute`, `godot_script`), or file access to the checkout or above,
    to `config/`, to `data/` itself (user store, every user's sessions -- a folder of
    its own like `data/workspace` is fine) or write access to `src/`.

### Prompt traps

- **Precedence:** custom prompt → raw `system_prompt` → `system_template` → default.
  **An inherited `system_prompt` silently beats your template.** Check the parent.
- A template that renders empty → WARNING, next strategy. Missing file →
  FileNotFoundError at render. Jinja error → unrendered text + warning.
- Templates are **read from disk on every render** (run start and every step). An edit
  takes effect without a restart — including in runs already in flight.
- Date variables come via `config.context.auto_datetime`; never put them in the prompt
  (cache, see [hooks.md](hooks.md)).
- Branch on what is there: `has_tool('*_manage_sub_agent')` (fnmatch over `tools`, the
  agent's own tools; names carry the instance prefix), `'writer_pipeline_v4' in plugins`
  (types installed and enabled), `'github' in mcp_servers`. A plugin's id is its
  type = folder name. Table: `docs/config_based_agents.md`.

### llm_profile

- A list = chain `[primary, fallback…]`; `llm_profile_advanced` is its own chain.
  Fallback: own tail, then the whole other chain, deduplicated.
- `llm_profile: []` and `llm_profile_fallbacks` are errors.
- `llm_params` apply to every chain member (flat or `"*"`; the exact key wins). Keys for
  profiles outside the chains are errors.
- Only `_RELOADABLE_AGENT_FIELDS` (`servers/agent/server.py`) reload live;
  `llm_profile`, tools and timeouts need a restart.

## Agent with its own code

```python
from agent_system.servers.agent.schema_based import SchemaBasedAgent
from agent_system.plugins.factory_utils import make_agent_plugin_factory

class MyAgent(SchemaBasedAgent):
    def __init__(self, name, system_config, server_config, registry=None, **kwargs):
        super().__init__(name, system_config, server_config, registry, **kwargs)
        # Agent.__init__ is (name, system_config, server_config, registry=None, llm=None,
        # llm_factory=None, session_service=None) — pass anything beyond registry by keyword

    async def execute_task(self, params: dict) -> dict:    # tool "{name}_execute_task"
        ...

PLUGIN_FACTORY = make_agent_plugin_factory(MyAgent)
```

- **Always `make_agent_plugin_factory`.** The runtime passes `registry=` only if
  `factory._accepts_registry` is set. A hand-written factory without the flag gives
  the agent an empty private registry — the ToolExecutionManager holds on to it;
  setting `inst.registry` afterwards is too late.
- Plain `Agent` only when tools are generated at runtime.
- `server_config` is the **merged** config (default_config + `type:` chain).
- `lazy = true` in plugin.toml only if `__init__` touches nothing but config and the
  LLM client (no file, thread, socket). The runtime only checks that an `Agent` comes
  out (else TypeError) and that LLM config/template resolve — nobody checks the
  no-I/O promise. Today everything is still built at start anyway.

## Sub-agents: enabling in the SAM

There is no global registry. **All four** must hold:

1. **The sub-agent exists as a server** — YAML included, `enabled: true`, type
   resolves. Otherwise: `"Agent type 'x' not found in registry"`.
2. **Its instance name is in `allowed_agents` of the calling SAM instance** (entry with
   `type: sub_agent_manager`, e.g. `src/plugins/coder/agents/tools.yaml`).
   - Exact names or fnmatch; `blocked_agents` is checked first and matches **exactly only**.
   - With a phase filter it must also be in `phase_agents[phase]`.
   - Otherwise `error_type: "agent_blocked"` (allowed/blocked lists) or
     `"phase_blocked"` (not in the phase's list).
   - Code default without the key is `['*']`; the `config:` block in the SAM
     `schema.yaml` is **not** read.
   - The "Available" list in the SAM tool description applies the same check.
3. **The caller allows the SAM instance:** `tools.allowed: ["my_sam/*"]` (tool
   `{name}_manage_sub_agent`).
4. **`metadata.visibility` is not `private`** (the default) — a private agent is
   missing from the "Available" list, so the model never learns its name. Use `tool`
   (or `both`).
5. **The calling run's user passes its `metadata.min_role`** (if set) — otherwise
   `error_type: "agent_role_gate"`, before a sub-session exists. The caller is the
   registered owner of `_request_id`, else `_user_id`; neither known → refused.

SAM knobs are **top-level** keys on the SAM entry (not under `config:`):
`allowed_agents`, `blocked_agents`, `allow_advanced_model` (default true; false drops
the caller's `use_advanced_model`), `advanced_create_only_agents`.
`allowed_agents`, limits, `allow_advanced_model` and the injector options reload via `agent-cli reload`; a
**new** agent needs a restart (the user does restarts).

## Config-only plugin (`type = ["library"]`)

```
src/plugins/my_harness/
  plugin.toml          # type = ["library"], no entrypoint, no plugin.py
  agents/*.yaml
  agents/prompts/*.md
  skills/<name>/SKILL.md
```

Found via the include glob in `config.yaml`, the `./` template resolution and
`skills.skill_dirs: src/plugins*/*/skills`. Missing skill → ERROR once per agent,
left out of the prompt. Example `coder`; guard test
`src/plugins/amiga/tests/test_amiga_config.py` (loads settings, checks allowlist
patterns and skills, actually renders the prompt).

## LLM providers (`src/plugins/`)

```toml
[plugin]
type = ["llm-provider"]            # required — the LLM registry skips anything else
provides = ["myprovider"]          # optional provides_batch, provides_tts, provides_decisions
default_base_url = { myprovider = "https://..." }
dependencies = ["my-sdk>=1.0"]
```

- The module is always `<folder>/provider.py` with `PROVIDERS = {name: build_fn(cfg, ssl_verify)}`
  (optional `BATCH_BACKENDS`, `TTS_PROVIDERS`). Only names declared in the manifest
  count; declared but not exported → `ProviderNotFoundError`.
- Clients are built only via `create_llm_from_profile` (AST guard
  `tests/llm/test_plugin_llm_clients_full_path.py`). The core names no provider and
  stays SDK-free (`tests/llm/test_llm_provider_registry.py`,
  `tests/pluginsystem/test_plugin_deps_declared.py`).
- Shared helpers live in `llm_common`; delegation via `get_provider("openai")`.
- **No provider tables**: no `if google`, no alias dicts, no name heuristics. Mappings
  come from config or gateway data.
- **Structured output** (`agent_system/llm/structured_output.py`): a client that wires it
  lists `response_format_kinds` (the kinds its WIRE has a field for), takes
  `response_format=` on `chat_tools`/`chat_tools_streaming` and calls
  `self._require_response_format(...)` before anything goes out. Whether a model honours it
  is `capabilities.structured_output` on the model entry (`json_mode` is not read). A client without the
  kinds is never handed a format; one that has them must never drop it silently.
- Report usage in OpenAI semantics: `prompt_tokens` **including** cache, details as a
  subset (example `llm_anthropic/anthropic_utils.usage_to_openai`).
- `config/llm*.yaml` belongs to the user — don't change it on your own.
