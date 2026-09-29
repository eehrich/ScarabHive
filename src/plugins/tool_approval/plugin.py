"""tool_approval -- factory.

A hook plugin with one web route: the pre_tool_call hook decides every call of
an agent that switched it on, and asks the person watching the run where the
rules leave the decision open; the route takes that person's answer.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .hooks import ToolApprovalPlugin


def PLUGIN_FACTORY(name: Optional[str] = None, system_config: Any = None,
                   server_config: Any = None) -> ToolApprovalPlugin:
    """Called as factory(name, system_config, server_config)."""
    return ToolApprovalPlugin(Path(__file__).parent, name=name or "tool_approval",
                              server_config=server_config)
