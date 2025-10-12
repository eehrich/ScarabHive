"""CLI utilities and shared runner functionality."""

from .agent_runner import create_and_register_agent, get_agent_with_llm_override
from .common import (
    supports_color, colorize, set_color_mode,
    format_status_event, status_subscriber, sse_subscriber,
    format_result_output, format_error, get_phase_color_map
)

__all__ = [
    # Agent runner functions
    "create_and_register_agent",
    "get_agent_with_llm_override",
    # Common utilities
    "supports_color",
    "colorize",
    "set_color_mode",
    "format_status_event",
    "status_subscriber",
    "sse_subscriber",
    "format_result_output",
    "format_error",
    "get_phase_color_map",
]
