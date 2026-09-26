"""Request -> user ownership mapping (process-wide).

Single owner of the ``request_id -> user_id`` map used for:

- **Status-stream authorization**: the app layer registers ownership when a
  request starts (``/run``, ``/events``) and releases it when the request
  completes, so status streams can be restricted to the owning user.
- **Session metadata**: the agent server resolves the user for a request when
  creating session metadata (needed by sub-agents and multi-user tools).
- **Sub-agent propagation**: tool execution and the sub_agent_manager plugin
  register sub-request ids under the parent's user.

Lives in ``core`` so that BOTH the app layer and the agent/server layer can
use it without upward imports (``servers/agent`` previously imported
``agent_system.app`` at use-site to break the resulting import cycle).
``app.py`` re-exports the dict as ``_request_user_map`` for existing importers.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

#: The user of the run this code runs in: set where a run begins, next to
#: current_request_id (Agent, a stategraph run). A call that reaches the hooks
#: without an agent -- a decision, TTS -- reads its owner here first
#: (llm/hook_notify.py): its run's id may be one nobody registered (a
#: stategraph run's own id), and the map lets go of a run's whole tree when
#: the request that started it ends, while its background sub-agents run on.
current_run_user: ContextVar[Optional[str]] = ContextVar("current_run_user", default=None)

# The shared map. Mutating module-level state is intentional here: the map is
# process-wide by design (one HTTP request may be served by app code, agent
# code and plugins, all needing the same view).
request_user_map: dict[str, str] = {}

# FIFO backstop against unbounded growth: tool calls and sub-agents register
# SUFFIXED ids (``<parent>_001``, ``<parent>_sub_...``) under the parent's
# user, and not every such id reaches a teardown path. The app layer releases
# whole trees on request completion (release_request_user_tree); this cap only
# catches ids whose parent never completes cleanly. Insertion order == dict
# order, so the oldest (= longest-finished) entries are evicted first. An
# evicted-but-still-active id degrades to the "anonymous" default on lookup.
_MAX_ENTRIES = 10000


def register_request_user(request_id: str, user_id: str) -> None:
    """Register (or overwrite) the owning user for a request id."""
    if request_id not in request_user_map and len(request_user_map) >= _MAX_ENTRIES:
        request_user_map.pop(next(iter(request_user_map)), None)
    request_user_map[request_id] = user_id


def get_request_user(request_id: str, default: str = "anonymous") -> str:
    """Resolve the owning user for a request id (``default`` if unknown)."""
    return request_user_map.get(request_id, default)


def release_request_user(request_id: str) -> None:
    """Remove a single request id from the map (idempotent)."""
    request_user_map.pop(request_id, None)


def release_request_user_tree(request_id: str) -> None:
    """Remove a request id AND all ids derived from it (``<id>_...``).

    Tool execution suffixes ids per tool call and sub-agent spawns derive
    their ids from the parent — releasing only the top-level id would leak
    one entry per tool call / sub-agent for the process lifetime. Called by
    the app layer at request teardown.
    """
    request_user_map.pop(request_id, None)
    prefix = request_id + "_"
    for rid in [k for k in request_user_map if k.startswith(prefix)]:
        request_user_map.pop(rid, None)
