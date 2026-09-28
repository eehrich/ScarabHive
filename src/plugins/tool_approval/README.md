# tool_approval

Approvals before tool calls (#091). A `pre_tool_call` hook decides every call
the model makes, before it runs:

- **deny rules** block it,
- **allow rules** let it run,
- and in `ask` mode every other call is **put to the person watching the
  run**, who answers *allow once*, *allow for this session* or *deny*, with an
  optional reason for the model.

Off for every agent until the agent switches it on. The shipped
`config/plugins.yaml` loads the plugin (its answer route exists) and enables the
hook for nobody; `test_plugin_tool_approval_config.py` checks that against the
real configuration.

## Switching it on

Per agent, in its YAML:

```yaml
agent_config:
  hooks:
    overrides:
      tool_approval.check_tool_call:
        enabled: true
        mode: ask                      # ask | auto | "off" (a bare off reads as false -- also off)
        allow:
          - "file_ops/*_read_*"
          - "web_scraper"
        deny:
          - tool: "terminal/*"
            arguments:
              command: "\\brm\\b.*\\s-{1,2}[a-zA-Z-]*[rR]|--recursive|\\bfind\\b.*-delete|\\bsudo\\b"
        ask_timeout: 300               # seconds a question waits
        unattended: block              # block | allow
```

The key is the hook's full name, `<instance>.check_tool_call`. Every key but
`enabled`, `timeout` and `order` reaches the hook (those three are the hook
system's; `timeout` and `order` take effect only in the global `hooks.overrides`).

| Key | Default | Meaning |
|---|---|---|
| `mode` | `ask` | `ask`: deny blocks, allow runs, the rest is asked. `auto`: deny blocks, the rest runs. `off` on an agent: its own rules and questions are off -- the instance's deny rules (and those of a run above it) still block. `off` on the instance: the default for agents that set no mode, and for them nothing is checked; an agent that sets `ask` or `auto` has the instance's deny rules too. |
| `allow` | `[]` | Rules for calls that run without a question (`ask`). |
| `deny` | `[]` | Rules for calls that never run: an agent's own in `ask` and `auto`, the instance's also for an agent in `off` (unless the instance is off too). Deny wins over allow and over a session grant. |
| `ask_timeout` | `300` | Seconds a question waits; unanswered, the call is blocked. |
| `unattended` | `block` | What happens to a call that would be asked while nobody can answer. |
| `gone_after_seconds` | `15` | A run nobody has read this long (tab closed) is not asked any more; a waiting question stops, and the call gets the `unattended` answer. |
| `reask_seconds` | `20` | A waiting question is sent again this often (see *Where the person is reached*). |

The instance's `config:` in `plugins.yaml` holds the same keys. Every key of
the agent's replaces the instance's, except `allow` and `deny`: the agent's
rules are **added** to the instance's, so an agent cannot drop a deny rule the
operator set for everyone -- not even with `mode: off`.

### Rules

A rule is a tool pattern exactly as in `tools.allowed` — `server/tool`,
`server/*`, `server`, fnmatch wildcards; the tool name carries the instance
prefix (`terminal/terminal_execute`). The same function matches both
(`tool_matches_patterns`), so a pattern names the same calls here and there,
external MCP tools (`weather.*`) included.

A rule may add argument patterns: `{tool: <pattern>, arguments: {<name>: <regex>}}`.
Every regex must match (`re.search`) the argument's value; a value that is no
string is matched as its JSON text; a call without the argument does not match.

A rule that cannot be read (bad regex, unknown key, `arguments` not a mapping)
**blocks** every call the hook decides for that agent and is logged — a typo in
a deny rule must not open what it was written to close.

**An argument rule on a shell is no boundary.** A regex on a command catches
the spellings it was written for, not `rm -R`, `find -delete`, a script file, an
alias or an interpreter that does the same. Use it to stop the obvious; to keep
an agent out of the shell, deny the tool (`terminal/*`).

## Where the person is reached

The question is a **status line of the run**, under a row of its own
(`<request_id>_approval_<id>`), with `meta.tool_approval`:
`{id, tool, server, arguments, arguments_cut, request_id, session_id, agent,
asked_at, expires_at, answer_url, decisions}`. The web chat draws the arguments,
a field for the reason and three buttons on that row (`syncQuestionActions` in
`static/js/chat_module.js`) and posts the answer. The reason reaches the model
with *deny* only.

`arguments` names every argument, in the order the model sent them -- nested
ones by their leaves (`opts.cmd`, `files[2]`), names that are not plain quoted
and escaped; short values stand whole, long ones lose their middle (never their
head or tail), past 300 leaves the rest are counted, and `arguments_cut` makes
the chat say that something was shortened. A long value or a strange name
cannot push the path of a file write or a command out of view.

```
POST /plugins/tool_approval/answer   {"question_id", "decision", "reason"}
GET  /plugins/tool_approval/pending  [?session_id=…&request_id=…]
```

`decision` is `allow_once`, `allow_session` or `deny`. The row's last line
(allowed / denied / no answer / cancelled) takes the buttons down, in every tab
showing the run. While it waits the hook sends the question again every
`reask_seconds`: a page reloaded mid-question skips the lines it had read, and
gets the buttons back with the next one.

**Who may answer:** the user the run belongs to, or an admin — the rule the app
applies to acting on a run (stopping it, writing into it). With authentication
off there is one user. Only a person's sign-in counts: an access token (Bearer or
the chat's cookie), never an API key — a key belongs to a program. **With
authentication off, anyone who reaches the API can list and answer the
questions** (`/pending`, `/answer`) -- another page (CORS allows every origin
by default) and a model's own HTTP tool (`web_scraper`, `terminal` with curl)
included. Approvals that must hold against the model need authentication on.

**"Allow for this session"** is kept for the owner, in the session of every run
of the chain that asks (a sub-agent's question: its session and its parent's),
and holds only where it was given for all of them -- a sub-agent's grant from a
run where its parent did not ask does not reach a parent that asks. It is never
written into another user's session: a chain with a level of another user's
(an ended run's id taken as a prefix) holds no grant and asks every time. A **script**
(tool_script) is never allowed for the session: it is asked with its code, call
by call -- one click would otherwise let every later script, and every call
inside one, run unread. The answer is refused, and the chat offers no button.

**Who can be asked at all.** A question is put only where a person can answer
it: a run started by a client that shows its questions to the person who
started it, and that person is signed in (or authentication is off -- an
anonymous visitor could not answer). The web chat says so when it starts a run
(`"attended": true` on POST `/events`, form field `attended` on `/run` with
files); the run carries the mark for as long as it runs
(`core.request_context.set_run_attended`). The hook asks when a run stream that
is **live, marked and read** carries the call's status lines -- the run itself,
or a run above it (`status_forwarding.attended_stream_of`). "Read" means the
run's job has a stream open on it: a closed tab leaves the job running with
none: a call made after `gone_after_seconds` without a reader is not asked, and
a question already waiting stops then (the job counts from the moment its last
reader left, so a reload -- which reads again within moments -- is allowed for).
Everything else is *unattended*:

| Started by | Asked? |
|---|---|
| web chat (`/events`, `/run` with files), signed in or auth off, while a tab reads the run | yes |
| web chat, anonymous visitor; or the tab was closed | no |
| a sub-agent, an agent called as a tool, a stategraph activity under such a run | yes — their status lines reach the stream of the run above them, the chat shows them in the sub-run's box |
| an async sub-agent after the run above it ended | no — that stream is gone |
| openai_api, agent-run, agent-cli, JSON `/run`, the writer's `/events` dispatches | no |
| a call inside a tool_script script | never -- see *Scripts* |

## Sub-runs: the rules go along

A run cannot leave its rules behind by starting other work.

**A sub-run whose agent has this hook on inherits** (`policy.py`). A sub-agent,
an agent called as a tool, a stategraph activity: each runs under a request id
that extends the id of the run that started it (`<run>_003_sub_x1`,
`<run>_004`). The spawn call passes the parent's hook first, which records the
parent's effective policy under its run id; the sub-run looks up its nearest
recorded ancestor -- every recorded run above it, in fact: a stale entry under
a reused id can only add rules -- and is held to all of them, never looser than
any run above it:

- **mode:** the stricter one (off < auto < ask). A parent in `ask` makes the
  sub-run ask -- in the parent's stream, as for the parent's own calls.
- **deny:** every rule of every level.
- **allow:** a call runs unasked only when an allow rule of *every* asking
  level names it; a sub-agent cannot widen what its parent lets through.
- **unattended:** `block` when any asking level says `block`.

The policies live in the process, one per run under approval, kept while the
run or a run under it streams and for a minute after its last call (an async
sub-agent may outlive its parent, and start after it), then swept; at most
10000.

**Starting work no approval reaches** (`spawns.py`) cannot inherit anything, so
it is decided on its own, before allow rules and session grants (neither can
reach the calls it would make):

| Mode | A spawn without approvals |
|---|---|
| `auto` | blocked; the model reads which work lacks approvals and to use an agent that has them |
| `ask` | asked, with a warning above the buttons: *… runs WITHOUT tool approvals: the rules of this run do not apply to its calls.* "Allow for this session" is kept per target, not per tool |
| `ask`, nobody can answer | blocked, whatever `unattended` says |
| inside a script | blocked: a script cannot ask |

What counts, by the server's plugin *type* (an instance may be named anything):

| Spawn | Target | Without approvals when |
|---|---|---|
| `sub_agent_manager` `manage_sub_agent` `create` | the agent `agent_type` | the config it runs with (the registry's instance, its merged `agent_config`) does not run `<instance>.check_tool_call`; an agent of type `stategraph_machine` always |
| `sub_agent_manager` `manage_sub_agent` `continue` | the agent of the sub-session `instance_id`, read as the manager reads it | the same; a sub-session that cannot be read counts as without |
| an agent called as a tool (the server is an Agent), any tool but a schema agent's `list_available_tools` | that agent | the same |
| `coding_cli` `run_task` | Claude Code | always: a process of its own, no hook of ours |
| `stategraph` `run_machine`, `send_event`, `control_run` continue/step/run_to/resume/fork/terminate | the machine | always: its `tool:` activities pass no hook (terminate runs its `finally` activities) |

The manager's operation is read as the manager reads it (inferred when the
model left it out). A target the registry does not have is left to the tool,
which refuses it. The preview escapes every invisible character (control,
format -- bidi overrides, zero-width -- and separators): code shown for
approval reads as it runs.

Not covered, and why:

- `agent_continuation` starts nothing: it continues the same run.
- `terminal`, `ssh_control`, `n8n` and the like can start anything, an agent
  CLI included; they are not spawn paths of the framework. Deny rules on them
  are the way to close that.
- A sub-agent started without a request id of its caller (`sub_<id>`: a slash
  command or a panel button) inherits nothing; those callers pass no hook at all.

## Scripts (tool_script)

A script is one call of the model: it passes this hook like any other, asked in
`ask` with its code in the preview (one line per line of code, long code cut in
the middle by whole lines), let through by an allow rule, or run in `auto`. The
calls it makes inside **never ask a person**: deny rules -- the run's and the
inherited ones -- block them (the script gets a catchable `ToolCallError`), and
so does a spawn without approvals; everything else runs. So no question waits
inside a script, and tool_script's `per_call_timeout` has nothing to cut off.

## Model Experience

### What the model sees

Nothing while a call is allowed — the call runs as sent. A blocked call's result
is the framework's `{"status": "error", "error": <text>, "type": "ToolCallBlocked"}`
with one of these texts (`<tool>` is the name the model called):

- deny rule: *The call to '<tool>' is not allowed for this agent (rule on
  <tool pattern>); it did not run. Do not send it again, and do not try to get
  the same effect with another call. Tell the user what you would need.* The
  rule's argument patterns are logged, not shown: a regex in the result is a
  recipe for the call that slips past it.
- the person denied: *The user denied the call to '<tool>'; it did not run.
  Their reason: <reason> Do not send it again, and do not try to get the same
  effect with another call. Take their answer into account, or ask the user how
  to proceed.* (without a reason the middle sentence is left out)
- no answer: *The call to '<tool>' needs the user's approval, and nobody
  answered within <n> seconds; it did not run. Do not send it again right away:
  go on without it, or finish and tell the user which call is waiting for their
  approval.*
- nobody watching (also when the reader left while it waited): *The call to
  '<tool>' needs a person's approval, and nobody can give it in this run: nobody
  is watching it. It did not run. Go on without it, or finish and tell the user
  which call needs their approval.*
- rules unreadable: *The call to '<tool>' did not run: the approval rules of
  this agent cannot be read (<error>). Tell the user; do not retry it.*
- a spawn without approvals, `auto`: *The call to '<tool>' would start <target>,
  which runs without tool approvals: the rules this run is held to would not
  apply to its calls. It did not run. Use an agent that has approvals switched
  on, or tell the user that <target> needs them.* (inside a script: *The call
  to '<tool>' inside the script would start …*)
- a spawn without approvals, nobody to ask: *The call to '<tool>' would start
  <target> without tool approvals; only a person may allow that, and nobody is
  watching this run. It did not run. Go on without it, or finish and tell the
  user which call needs their approval.*
- a spawn without approvals inside a script, `ask`: *The call to '<tool>'
  inside the script would start <target> without tool approvals, and only a
  person may allow that -- a script cannot ask. It did not run. Make this call
  on its own, outside the script, so the user can be asked.*
- cancelled while asking: the framework reports the call cancelled, not blocked.

`<target>` reads *agent 'name'*, *Claude Code (coding_cli)*, *the state machine
'id'* or *the sub-agent 'instance'*.

The hook changes no tool description and injects no message.

### Token and cache effect

None. The history keeps the model's call and the blocked result in its place,
append-only; nothing earlier is rewritten.

### Known gaps

- **Nothing is persisted.** Open questions and session grants live in the
  process; a restart ends the runs that waited and forgets the grants. The grant
  set keeps the last 1000 sessions.
- **"Allow for this session" is per tool, not per argument.** A deny rule still
  wins over it. A script is never allowed for the session.
- **The hook's own timeout bounds the wait** (schema: 3600 s, lower only through
  the global `hooks.overrides`). A question never waits past it: `ask_timeout`
  is kept a little short of it (logged), so the call ends with this hook's
  text. Cut off from outside (a torn-down run) the call does not run either
  (`on_error: block`), and the row ends with *cut off*.
- **`auto` has no reviewer.** Unmatched calls run; a reviewer agent that
  decides them is not built.
- **Other clients** that show a person the stream (a custom UI) must send
  `attended` themselves and render `meta.tool_approval`; the pending list
  (`GET …/pending`) serves one that missed the line.
