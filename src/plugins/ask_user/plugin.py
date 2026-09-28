"""ask_user -- factory: the tool server, which also serves the answer route."""
from __future__ import annotations

from .server import AskUserServer

PLUGIN_FACTORY = AskUserServer
