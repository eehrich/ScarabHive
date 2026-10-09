"""The API process's shared services: set by ``build_app`` and its lifespan.

They were module globals of ``app.py``, and every module outside it that
needed one -- the session and admin routers, the session access check, the
tool integration, the sub-agent manager's panel, a stategraph run -- imported
``app.py`` lazily inside a function for it, because ``app.py`` imports those
modules and a top-level import was a cycle. This module imports nothing of
the application, so every reader imports it at the top.

Read an attribute at the moment it is needed (``app_state.session_service``),
never bind it at import time (``from agent_system.app_state import
session_service`` keeps the value of that moment): ``build_app`` replaces the
services, the conftest resets them between tests, and a test that swaps one in
must reach every reader.

All of them belong to whichever ``build_app`` ran last in the process -- the
same as before; the per-application state is ``app.state``. That is why the
configuration is not here: a reload writes ``app.state.config``, which is the
one source (``AppContext.live_config()``, api/app_context.py). The global it once had
answered from process start to its one remaining reader.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .services import AgentService, ConfigService, ToolServerService, ToolService
    from .services.initialization_service import InitializationService
    from .services.session_archive import SessionArchive
    from .services.session_manager import SessionManager
    from .services.session_service import SessionService
    from .tools.base import ToolServerRegistry

#: The registry the API's routes look agents and tool servers up in.
app_registry: Optional["ToolServerRegistry"] = None
#: The ToolServerIntegration the lifespan initialized (tools.integration prefers it).
tool_integration: Any = None
config_service: Optional["ConfigService"] = None
tool_server_service: Optional["ToolServerService"] = None
tool_service: Optional["ToolService"] = None
agent_service: Optional["AgentService"] = None
initialization_service: Optional["InitializationService"] = None
session_manager: Optional["SessionManager"] = None
session_service: Optional["SessionService"] = None
session_archive: Optional["SessionArchive"] = None
#: Set on shutdown so open streams end gracefully.
shutdown_event: Optional[asyncio.Event] = None
#: When the lifespan started (``/health`` reports the uptime).
app_start_time: Optional[float] = None

#: The names these services had as globals of ``app.py``. ``agent_system.app``
#: still answers a read of them (module ``__getattr__``) for code outside this
#: repository -- a plugin of another plugin root -- that imports them from there.
LEGACY_APP_NAMES = {
    "_app_registry": "app_registry",
    "_tool_integration": "tool_integration",
    "_config_service": "config_service",
    "_tool_server_service": "tool_server_service",
    "_tool_service": "tool_service",
    "_agent_service": "agent_service",
    "_initialization_service": "initialization_service",
    "_session_manager": "session_manager",
    "_session_service": "session_service",
    "_session_archive": "session_archive",
    "_shutdown_event": "shutdown_event",
    "_app_start_time": "app_start_time",
}


def reset() -> None:
    """Forget every service (between tests: the next ``build_app`` sets them anew)."""
    for name in LEGACY_APP_NAMES.values():
        globals()[name] = None
