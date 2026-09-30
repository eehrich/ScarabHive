"""CLI utilities and shared runner functionality."""

from .agent_runner import create_and_register_agent
from .common import (
    set_color_mode, supports_color, colorize,
    format_status_event, status_subscriber,
    format_error, get_phase_color_map,
    show_answer, render_with_rich, print_agent_response
)

__all__ = [
    "create_and_register_agent",
    "supports_color",
    "colorize",
    "set_color_mode",
    "format_status_event",
    "status_subscriber",
    "format_error",
    "get_phase_color_map",
    "show_answer",
    "render_with_rich",
    "print_agent_response",
]
