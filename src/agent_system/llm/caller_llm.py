"""The LLM profile a run was switched to, as the sub-agents it starts see it.

A run can be put on another LLM than its agent's own: the API's llm_profile,
the CLI's --llm, the chat's /model, use_advanced_model. A sub-agent runs its
own configuration -- usually the point of having one. Some are better off on
whatever their caller runs: a skills or coding helper doing the caller's work
should not be the one part of the job left on a weaker model. Those say so
with ``agent_config.inherit_parent_llm`` (opt-in).

The profile travels in a ContextVar, not through the spawner. Sub-agents are
started by plugins (sub_agent_manager today, maybe another tomorrow), and a
contract every spawner has to honour is one the next spawner forgets. The
agent loop runs each tool call as its own task, in a context holding the
run's profile (``context_for_tool``): whatever a tool starts inside it --
awaited, as a task of its own, through ``asyncio.to_thread`` -- sees that
profile, without the tool knowing there is one. ``loop.run_in_executor`` and a
bare ``threading.Thread`` copy no context: a run started there sees nothing.
Nor do tools a hook or a command dispatches outside the loop's tool calls
(tool_preload): they see whatever the surrounding run was started with.

Set per tool task, never in the run itself: ``Agent.run_events`` is an async
generator, and a value set there lives in the context of whoever iterates it.
A spawner that stops reading a sub-run early would leave the sub-run's value
behind in its caller's context, and the next sibling would inherit the wrong
model. A task's own context copy cannot leak.

Each run hands ITS OWN profile to its tools: its switch, or None when it runs
its own configuration (an override on its own primary profile is none, unless
it followed its caller onto it). A grandchild inherits from its parent, not
from a grandparent two levels up.

Not across a process: a run woken later in a new process (a wake, a job
worker) starts with nothing here and runs its own configuration.
"""
from __future__ import annotations

import contextvars
from typing import Optional

_caller_profile: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "caller_llm_profile", default=None)


def caller_llm_profile() -> Optional[str]:
    """The profile the calling run was switched to, or None."""
    return _caller_profile.get()


def context_for_tool(profile: Optional[str]) -> contextvars.Context:
    """A copy of the current context in which a tool call sees *profile*."""
    context = contextvars.copy_context()
    context.run(_caller_profile.set, profile)
    return context
