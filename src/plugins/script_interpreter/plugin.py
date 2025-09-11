"""Script Interpreter plugin entrypoint (standardized)."""

from __future__ import annotations

from .server import ScriptInterpreterServer


PLUGIN_FACTORY = ScriptInterpreterServer