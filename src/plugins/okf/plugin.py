"""OKF plugin entrypoint.

PLUGIN_FACTORY MUST live here — plugin discovery only checks plugin.py.
The OkfServer both serves tools and provides the pre_llm_call hook
(duck-typed; declared in schema.yaml), so it is exported directly.
"""
from __future__ import annotations

from .server import OkfServer

PLUGIN_FACTORY = OkfServer
