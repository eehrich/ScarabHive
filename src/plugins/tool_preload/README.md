# Tool Preload Plugin

Executes the **predictable opening tool calls** after a user turn, before the
first LLM call — and appends the results as if the model had made the calls
itself.

An agent gets *"ändere X in Dokument Y"*. Its first LLM turn is — always — the
tool call that loads Y. That turn costs a full LLM round trip (they have grown
slow) plus the prompt tokens of the whole context, and its outcome is known in
advance. This plugin removes it.

Hook-only, no tools of its own. Registered globally, but it does **nothing**
without per-agent rules.

## How it works

1. `pre_llm_call` fires. The hook acts only when the **last message is a user
   message** — that is what makes it once per user turn, since afterwards the
   last message is a tool result.
2. The user text (capped, see below) is matched against the agent's rules.
3. Matching rules dispatch their tool calls through `Agent.dispatch_tool_call`,
   sequentially, in the configured order.
4. Each result is appended as an `assistant(tool_calls)` + `tool` pair with a
   matching `preload_<hex>` id — byte-identical in shape to what the LLM's own
   tool calls produce.

The pair is appended **after** the user message, so the existing prefix stays
byte-stable and provider prompt caching is unaffected: the pair only extends it.

## Configuration

Per agent, in the agent YAML. There is no global rule set — a rule only makes
sense next to the agent whose phrasing it matches.

```yaml
hooks:
  enabled: true
  overrides:
    tool_preload.preload:
      enabled: true
      max_calls_per_turn: 5      # default 5
      dedup: true                # default true
      rules:
        # A CHAIN — strictly in this order. "First set the context var,
        # then load the content" is a real dependency.
        - match: "Szene\\s+(?P<sid>\\d+)"
          calls:
            - tool: writer_session_set_var
              params: {name: current_scene, value: "{sid}"}
            - tool: writer_content_get_scene
              params: {scene_id: "{sid}"}

        # Shorthand: one rule, one call.
        - match: "Dokument\\s+(?P<doc>[\\w-]+)"
          tool: json_store_manage_json
          params: {operation: read, doc: "{doc}"}
```

**Parameters** are templated from two sources:

| Placeholder | Source |
|---|---|
| `{group}` | a **named group of the rule's regex** — something the user typed |
| `{{ var }}` | a **session context variable** — the same value the prompt template renders |

A value that is exactly one placeholder may become an `int` (`scene_id: 42`,
not `"42"`) — tool handlers validate ids as integers; see the lossless rule
below for when it does not. A mixed string (`"kapitel_{n}"`) stays a string. A
placeholder with no matching group *or variable* skips the whole rule with a
warning rather than calling a tool with a literal `{doc}` as the document name.

Context variables exist for state the user never types. v6 keeps its store
namespace in `json_namespace`, set once at bootstrap and inherited by every
sub-agent whose whole task text is *"Aufgabe: World"* — without this source a
rule could not name the store, and the read would hit the default namespace:
someone else's document, silently. Precedence matches the prompt rendering:
the agent's static `template_vars` as the base, `set_context` on top.

```yaml
- match: "Aufgabe:\\s*(?P<aufgabe>\\w+)"
  calls:
    - tool: v6_story_json_manage_json
      params: {operation: read, doc: synopsis, namespace: "{{ json_namespace }}"}
```

Both syntaxes resolve in **one** pass, and that matters twice over. The
context alternative comes first in the pattern, so `{{name}}` is not read as a
group `{name}` in literal braces. And a resolved value is never scanned again:
the v6 coordinator puts the whole user task into `brief`, so with two
sequential passes a brief containing `{sid}` was read as a group reference and
killed the rule with a warning naming a group that appears in no YAML.

Templating recurses into **dicts and lists**. Top-level-only meant a nested
`{filter: {namespace: "{{ ns }}"}}` reached the tool verbatim — `json_store`
then falls back to the DEFAULT namespace and returns a foreign document,
silently.

The int conversion only fires when it is **lossless** (`str(int(v)) == v`):
`"42"` becomes `42`, `"007"` stays a string. Otherwise preload and the agent's
own call would name two different namespaces — the prompt still renders `007`.
`str.isdigit()` is not the test for this: `"²"` passes it and `int("²")` raises
a ValueError, which is not a KeyError and therefore used to discard the whole
turn's preload instead of the one rule.

Values are normalised to JSON-native types first. YAML resolves an unquoted
`2026-01-01` to a `datetime.date`, and that used to reach the serializer *after*
the tool had already run — see *Invariants* below.

## Rules are all-or-nothing

Both `dedup` and `max_calls_per_turn` apply to the **rule**, never to a single
call of it:

- **dedup** skips a rule only when *every* one of its calls is already in the
  conversation. Skipping just the already-seen first link would run the rest
  against whatever state that link last set — silently, on the wrong scene.
- **the cap** skips a rule whole when it would not fit. Cutting a chain in half
  leaves the context var set and the content never loaded, which is worse than
  not preloading at all. The agent then makes those calls itself.

## Authorization

Dispatch goes through `Agent.dispatch_tool_call` — the same core helper the
`tool_script` plugin uses. Same server resolution, same `tools.allowed` and
`tools.blocked` semantics as schema build: **what the LLM may not call, a
preload may not call either.** `session_id`, `request_id` and `user_id` are
forwarded so runtime params (`_session_id`, `_user_id`, …) are injected exactly
as on the LLM path.

Nothing restricts rules to read-only tools. That is deliberate — the chain case
needs a state-setting call — but it means a rule author can preload a mutating
tool. Write rules whose match is specific enough that firing it is always
correct.

## Invariants

Each of these cost a measured failure:

- **A tool that ran is always recorded.** A serialization failure while building
  the pair used to discard every pair built so far: the tools had run, the
  conversation said they had not, and the model called them again — a double
  execution for a state-changing tool. Params are normalised up front, and the
  pair construction has its own guard that keeps earlier pairs.
- **The regex input is bounded** (`MATCH_TEXT_LIMIT`, 2000 chars). A rule with a
  nested quantifier against a long message stalled the event loop for 4.8 s while
  the hook's declared 1 s timeout never fired — `asyncio.wait_for` can only
  cancel at an await point and `re.search` has none. A rule slower than 100 ms is
  logged with its pattern so the operator can find it.
- **A failing preload never sinks the request.** A dispatch rejection aborts the
  chain (later calls may depend on the rejected one) and keeps the pairs that
  completed. A bug in the mechanism itself is caught and the run continues
  unpreloaded.
- **A tool ERROR result is appended like any result.** Error answers carry
  recovery context, and the agent would have seen exactly this calling the tool
  itself.

## Ordering

Declared `before` several categories — `context_engineering`,
`prompt_injection`, `message_validation`, `message_debugging` — not one. An
unresolvable ordering reference is dropped with a DEBUG log, so anchoring to a
single plugin meant disabling *that* plugin silently moved this hook to last.
An oversized preloaded result should be externalised by compaction's layer 1 in
the same pre-LLM pass, and the hooks that read the tail of the message list must
see it before this hook changes what the tail is.

## Operating notes

- Every executed preload logs at INFO with tool and params.
- The calls do **not** appear in the run result's `calls` list or the event
  stream — they were not made by the model. The session messages are the record.
- `config/agents/tool_preload_test_agent.yaml` is a runnable end-to-end harness
  (disabled by default; the file documents how to run it).
