# Extending stategraph: a new activity kind

Activity kinds (the key of a state's `do:` mapping) are a registry. A plugin adds a
kind without touching the engine (docs/stategraph_design.md, D9). Everything that makes
an activity durable -- journal, replay, mocks, retries, timeouts, cancellation -- is
done by the engine around your kind, so a kind only has to describe its fields and do
its work once.

Code: `kinds/base.py` (registry, `KindSpec`, `ActivityKind`, `ActivityError`),
`kinds/builtin.py` (the seven built-in kinds -- the best examples),
`engine/activity.py` (`ActivityRun`, what `run` receives).

## A complete kind

```python
# src/plugins/my_plugin/stategraph_kinds.py
from typing import Any

from pydantic import Field

from plugins.stategraph.kinds.base import ActivityError, ActivityKind, KindSpec, register


class NotifySpec(KindSpec):
    """The fields of `do: {notify: ..., text: ...}`; unknown keys are refused (extra="forbid")."""

    notify: str = Field(description="flat name of a messaging tool, e.g. telegram_send_message")
    text: Any = Field(description="the message (template)")


@register
class NotifyKind(ActivityKind):
    key = "notify"                     # the kind key in do:
    spec_model = NotifySpec
    template_fields = ("text",)        # rendered, and covered by the journal's input hash
    default_idempotent = False         # a repeat after a crash would send the message twice
    title = "Notify"                   # palette
    icon = "send-horizontal"           # an icon of static/kit/icons.svg
    summary = "Send a message through a messaging tool; out = the tool's result"

    def references(self, spec: NotifySpec) -> dict[str, str]:
        return {"tool": spec.notify}   # SG007: the runner must be allowed to call it

    async def run(self, spec: NotifySpec, act) -> Any:
        text = act.text(act.render(spec.text, "text"))
        if not text.strip():
            raise ActivityError("call_failed", "notify: the message is empty")
        result = await act.backend.call_tool(act, tool=spec.notify, args={"text": text})
        act.meta["chars"] = len(text)  # journaled with the outcome, shown in the panel
        return result
```

Used in a machine:

```yaml
  tell_user:
    do:
      notify: telegram_send_message
      text: "Your book {{ ctx.title }} is ready."
    transitions:
      - target: done
```

**Registration.** `@register` adds the kind to the process-wide registry when the
module is imported. Import the module from your plugin's entry point (`plugin.py`) so
it is registered before machines load. A second kind with the same key raises
`ValueError` at import.

## The class attributes and hooks

| Name | Meaning |
|---|---|
| `key` | The kind key in `do:`. Names like state names. |
| `spec_model` | A `KindSpec` subclass: your fields. It inherits the common keys `retry`, `timeout`, `idempotent`, `description`. Its JSON schema feeds the panel's inspector and `stategraph_catalog`. |
| `title`, `icon`, `summary` | Palette and catalog. |
| `template_fields` | Fields that are templates (§2.6 of the design). The engine renders them **before** the activity runs to compute the input hash; `run` renders them again with `act.render`. Every field that is not listed is literal. |
| `code_fields` | Fields holding a Python expression (validated as code, not as a template), e.g. `map`, `error_if`. Evaluate them with `act.evaluate`. |
| `default_idempotent` | `True` (default): a resumed run may start the activity again if it was in flight at a crash. Set `False` when a second execution does harm (sends, creates, pays); the engine then raises `interrupted` instead. A machine can override it per activity with `idempotent:`. |
| `nested_one`, `nested_map` | Fields holding one nested activity (`each`) or a map of them (`parallel`); the validator checks them recursively. |
| `child_scope_names(spec)` | Extra names bound inside nested activities (`map`: the item variable and `index`). |
| `references(spec)` | Literal names for the configuration check (SG007): `agent`, `tool`, `profile`. |
| `submachine(spec)` | The import alias this activity runs, if any (SG006 checks its params). |
| `extra_inputs(spec, act)` | Inputs besides the template fields that the input hash must cover (pure; e.g. `map`'s items). |
| `label(spec)` | Subtitle on the canvas. |

## `run(spec, act)`: the contract

`run` is a coroutine. It receives the validated spec and an `ActivityRun`, and
returns the activity's result -- `out` for the machine -- or raises `ActivityError`.

- **Never touch `ctx`.** An activity's only effect on the machine is its result, which
  the engine journals before the machine sees it. That is what lets a resumed run
  replay the result instead of running the activity again. Read what you need from
  the scope (`act.render`, `act.evaluate`); let the machine's effects write `ctx`.
- **Return JSON data.** The result goes through a canonical JSON round trip (dataclasses
  and pydantic models are dumped first; anything else non-JSON becomes a string).
- **Fail with `ActivityError(type, message, data=None)`.** Use a type from the design's
  list (§3.5) so machines can handle it: `call_failed` for "the work failed", `config`
  for a misconfiguration, `tool_failed` / `agent_failed` when you wrap those. `data`
  becomes `error.data`. Any other exception is caught and turned into an error too, but
  with a generic type -- raise your own.
- **Leave cancellation alone.** Do not catch `asyncio.CancelledError`; terminate and
  per-attempt timeouts cancel `run`. A timeout becomes error `timeout`. A `CancelledError`
  the run did not cause (a library cancelled a future you awaited) fails the activity with
  `activity_failed`, and its error transition fires.
- **No retries of your own.** `retry:` on the activity is the only retry layer.
- **Reach the outside only through `act.backend`.** It is the ScarabHive backend in a
  live run and a backend that refuses everything (`no_backend`) in a mock-only run.
  A kind that opens its own connections runs for real in a mock-only run.
- **Pure rendering.** Template fields and code fields follow the machine's purity rules;
  your `run` may do I/O, `extra_inputs` may not.

## The `ActivityRun` API (`engine/activity.py`)

| Member | What |
|---|---|
| `act.render(value, where)` | Render a template value against the activity's scope; `where` names the field in error messages. |
| `act.evaluate(source, where, **bind)` | Evaluate a Python expression (read-only); `bind` adds names, e.g. `out=result` for an `error_if`. |
| `act.text(value)` | A rendered value as text (JSON for dicts and lists). |
| `act.plain(value)` | A deep, plain JSON-like copy. |
| `act.backend` | `agent_create`, `agent_continue`, `call_tool`, `decide` (see `engine/backend.py`); every call takes `act` first. |
| `act.meta` | A dict journaled with the outcome (`instance_id`, counts, cost, …). |
| `act.child(label, raw, extra_scope=None)` | Run a nested activity (a raw `do` mapping) as a child: its own journal key `<key>/<label>`, its own mock path, replay and retries. `extra_scope` binds names for its templates. |
| `act.run_submachine(alias, params)` | Run an imported machine in a nested frame; returns its final output or raises `submachine_failed`. |
| `act.recorded_error(label)` | A child's recorded failure (for fail-fast joins during replay). |
| `act.frozen_scope()` | The read-only scope as an object (`.ctx`, `.params`, `.run`, …). |
| `act.frame_vars()` | The frame's effective agent template vars. |
| `act.request_id()` | The next request id of the run (`<run id>_NNN`), for calls that need one. |
| `act.namespace` | The machine's companion-module namespace (`namespace.function(name, where)`). |
| `act.run_id`, `act.machine_id`, `act.state`, `act.path`, `act.key`, `act.visit` | Where this activity runs: run, machine, state, mock/display path, journal key, visit number. |
| `act.cancellation_token` | The run's cancellation token -- `None` in a `finally` or `close` activity, which runs on after a terminate. |

Nested activities show how composites are built: `parallel` and `map` call
`act.child` per branch or item and join the results (`kinds/builtin.py::join`).

## Checklist

- [ ] `spec_model` lists every field with a description; nothing is `extra`.
- [ ] Every template field is in `template_fields`, every code field in `code_fields`.
- [ ] `default_idempotent` is `False` if a repeat does harm.
- [ ] `references` names what SG007 must check.
- [ ] `run` returns JSON, raises `ActivityError` with a documented type, never touches `ctx`.
- [ ] External calls go through `act.backend`.
- [ ] A test runs the kind through the engine: a mocked run (the mock answers instead of
      `run`), a live run with a stub backend, and a replay (resume) that does not call
      `run` again.
