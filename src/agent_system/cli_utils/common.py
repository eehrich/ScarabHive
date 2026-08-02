"""
Shared CLI utilities for agent-run and agent-cli.

This module provides:
- Color output utilities
- Status event subscriber and display
- Common formatting functions
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from functools import lru_cache

logger = logging.getLogger(__name__)

# STD_OUTPUT_HANDLE / ENABLE_VIRTUAL_TERMINAL_PROCESSING (Windows console API)
_STD_OUTPUT_HANDLE = -11
_ENABLE_VT_PROCESSING = 0x0004
# Module-level so tests can flip the platform without patching os.name globally
_IS_WINDOWS = os.name == "nt"


@lru_cache(maxsize=1)
def _enable_windows_vt() -> bool:
    """Switch ANSI processing on for the attached Windows console, once.

    Only the SetConsoleMode side effect is cached -- not the decision, which
    still has to re-read sys.stdout (colorama replaces it during startup).
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return False
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & _ENABLE_VT_PROCESSING:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | _ENABLE_VT_PROCESSING))
    except Exception as e:
        logger.debug(f"Could not enable ANSI processing on this console: {e}")
        return False


def ansi_capable_stdout() -> bool:
    """Whether stdout can actually render ANSI -- switching it on if it can.

    isatty() alone is not enough on Windows: a console IS a tty but prints
    escape sequences literally until ENABLE_VIRTUAL_TERMINAL_PROCESSING is set
    on it. That gap is how raw ESC[90m ended up in the output instead of colour.
    """
    try:
        if not sys.stdout.isatty():
            return False
    except Exception as e:
        logger.debug(f"Failed to check if stdout is a TTY: {e}")
        return False

    if not _IS_WINDOWS:
        return True
    return _enable_windows_vt()


# Global color mode (can be set by CLI tools)
# Supports: 'auto', 'always', 'never', 'ansi', 'html', 'text'
color_mode: str = "auto"


def set_color_mode(mode: str) -> None:
    """Set global color mode.
    
    Args:
        mode: One of 'auto', 'always', 'never', 'ansi', 'html', 'text'
              - auto: ANSI if TTY, otherwise text
              - always/ansi: Force ANSI colors
              - never/text: Plain text only
              - html: HTML output
    """
    global color_mode
    color_mode = mode


def get_output_format() -> str:
    """Get the desired output format based on color_mode.
    
    Returns:
        One of 'ansi', 'html', 'text', 'markdown'
        - 'ansi': ANSI colored terminal output
        - 'html': HTML formatted output
        - 'text': Plain text (no formatting)
        - 'markdown': Raw markdown (fallback)
    """
    # Normalize aliases
    mode = color_mode
    if mode == "always":
        mode = "ansi"
    elif mode == "never":
        mode = "text"
    
    # Handle explicit formats
    if mode in ("ansi", "html", "text", "markdown"):
        return mode
    
    # Auto mode: ANSI only where it will actually render as colour
    if mode == "auto":
        return "ansi" if ansi_capable_stdout() else "text"
    
    # Default fallback
    return "text"


def supports_color() -> bool:
    """Return whether ANSI color sequences should be used.

    Honors the global `color_mode` which can be set to 'auto',
    'always', 'never', 'ansi', 'html', 'text'.
    
    Note: This returns True for both 'ansi' and legacy 'always' modes.
          For explicit format detection, use get_output_format().
    """
    format_mode = get_output_format()
    return format_mode == "ansi"


def colorize(text: str, color_code: str) -> str:
    """Wrap text in ANSI color codes when supported.
    
    Args:
        text: Text to colorize
        color_code: ANSI color code (e.g., '31' for red, '32' for green)
        
    Returns:
        Colorized text if colors are supported, otherwise plain text
    """
    if not supports_color():
        return text
    return f"\x1b[{color_code}m{text}\x1b[0m"


def get_phase_color_map() -> dict[str, str]:
    """Get standard color mapping for status event phases.
    
    Returns:
        Dictionary mapping phase names to ANSI color codes
    """
    return {
        "start": "36",      # cyan
        "progress": "34",   # blue
        "end": "32",        # green
        "error": "31",      # red
    }


def format_status_event(event, use_color: bool = True) -> str:
    """Format a status event for display.
    
    Args:
        event: Status event object with phase, server, message attributes
        use_color: Whether to use color formatting
        
    Returns:
        Formatted status line string
    """
    # Extract phase
    phase = event.phase.value if hasattr(event.phase, 'value') else str(event.phase)
    phase_disp = phase
    
    # Colorize phase if enabled
    if use_color and supports_color():
        phase_color_map = get_phase_color_map()
        c = phase_color_map.get(phase, "34")
        phase_disp = colorize(phase, c)
    
    # Build status line
    server_col = event.server
    txt = event.message
    status_line = f"[{phase_disp}] {server_col}: {txt}"
    
    # Error phase should be fully red
    if phase == "error" and use_color and supports_color():
        status_line = colorize(status_line, "31")
    
    # Warning level should be yellow
    if hasattr(event, 'level') and event.level == "warning" and use_color and supports_color():
        status_line = colorize(status_line, "33")
    
    return status_line


async def status_subscriber(
    status_queue,
    output_stream=sys.stderr,
    use_color: bool = True,
    verbose: bool = False
) -> None:
    """Subscribe to status events and display them.
    
    This is a reusable async function for displaying status events from a queue.
    
    Args:
        status_queue: AsyncIO queue containing status events
        output_stream: Stream to write status messages to (default: stderr)
        use_color: Whether to use color formatting
        verbose: Whether to show debug messages
        
    Usage:
        status_queue = await status_bus.subscribe()
        task = asyncio.create_task(status_subscriber(status_queue))
        # ... do work ...
        task.cancel()
    """
    if not status_queue:
        return
    
    try:
        while True:
            event = await status_queue.get()
            
            # Format and display the event
            status_line = format_status_event(event, use_color=use_color)
            print(status_line, file=output_stream)
            
    except asyncio.CancelledError:
        if verbose:
            logger.debug("Status subscriber cancelled")
        return
    except Exception as e:
        logger.debug(f"Status subscriber loop error: {e}")
        return


async def sse_subscriber(
    url: str,
    verbose: bool = False,
    use_color: bool = True
) -> None:
    """Subscribe to SSE (Server-Sent Events) status stream.
    
    This is an optional feature for environments where status events
    are streamed via HTTP SSE.
    
    Args:
        url: SSE stream URL
        verbose: Whether to show debug messages
        use_color: Whether to use color formatting
    """
    try:
        try:
            import aiohttp
        except ImportError:
            logger.debug("aiohttp not available, SSE subscriber disabled")
            return
            
        timeout = aiohttp.ClientTimeout(total=None)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url) as resp:
                if resp.status != 200:
                    logger.debug(f"SSE stream returned status {resp.status}")
                    return
                    
                async for line in resp.content:
                    try:
                        text = line.decode("utf-8").strip()
                    except Exception:
                        continue
                        
                    if not text or not text.startswith("data:"):
                        continue
                        
                    payload = text[len("data:"):].strip()
                    try:
                        import json
                        obj = json.loads(payload)
                    except Exception as e:
                        if verbose:
                            logger.debug(f"Failed to parse SSE payload: {e}")
                        obj = {"raw": payload}
                    
                    # Print SSE messages
                    msg = f"[SSE] {obj.get('server','?')}: {obj.get('message','')}"
                    if use_color and supports_color():
                        msg = colorize(msg, "34")
                    print(msg)
                    
    except asyncio.CancelledError:
        if verbose:
            logger.debug("SSE subscriber cancelled")
        return
    except Exception as e:
        if verbose:
            logger.debug(f"SSE subscriber error: {e}")
        return


def format_error(error_msg: str, use_color: bool = True) -> str:
    """Format error message for display.
    
    Args:
        error_msg: Error message text
        use_color: Whether to colorize the error
        
    Returns:
        Formatted error string
    """
    if not error_msg.startswith("ERROR:"):
        error_msg = f"ERROR: {error_msg}"
    
    if use_color and supports_color():
        return colorize(error_msg, "31")  # Red
    return error_msg


def render_with_rich(markdown_content: str, code_theme: str = "monokai") -> None:
    """Render markdown content with Rich Console (for ANSI terminal display)."""
    try:
        from rich.console import Console
        from rich.markdown import Markdown as RichMarkdown
        
        console = Console()
        md = RichMarkdown(markdown_content, code_theme=code_theme)
        console.print(md)
    except ImportError:
        print(markdown_content)
    except Exception as e:
        logger.warning(f"Failed to render with Rich: {e}")
        print(markdown_content)


def print_agent_response(formatted_content: str, content_format: str) -> None:
    """Print agent response with appropriate formatting and headers.
    
    Args:
        formatted_content: The formatted content to display
        content_format: Format type ('ansi', 'html', 'text', 'markdown')
    """
    print("\n" + "="*50)
    print("AGENT RESPONSE:")
    print("="*50)
    
    if content_format == 'ansi':
        render_with_rich(formatted_content)
    else:
        print(formatted_content)
    
    print("="*50)


async def format_output_with_hooks(
    output: str,
    agent_instance,
    session_id: str = "unknown",
    request_id: str = "cli_display",
    output_format: str | None = None
) -> tuple[str, str]:
    """Format output using FORMAT_OUTPUT hooks based on --color setting.
    
    This is a central function used by both agent-cli and agent-run to convert
    agent output (markdown or HTML) to the desired format using FORMAT_OUTPUT hooks.
    
    Args:
        output: The output string to format (can be markdown or HTML)
        agent_instance: Agent instance with hook manager
        session_id: Session ID for context
        request_id: Request ID for context
        output_format: Override format ('ansi', 'html', 'text'). If None, uses get_output_format()
        
    Returns:
        Tuple of (formatted_output, content_format)
        - formatted_output: Formatted string in requested format
        - content_format: Actual format type ('ansi', 'html', 'text', 'markdown')
        
    Note:
        - Honors --color flag via get_output_format()
        - Falls back to original output if hooks not available
        - Handles both markdown and HTML input (auto-detects and converts)
    """
    # Determine desired output format
    if output_format is None:
        output_format = get_output_format()
    
    # Check if agent has hook manager
    if not agent_instance or not hasattr(agent_instance, '_hook_manager') or not agent_instance._hook_manager:
        logger.debug("No hook manager available, returning original output")
        return output, 'text'
    
    try:
        logger.debug(f"Formatting output with format='{output_format}' (length: {len(output)})")
        
        # Execute format hooks with requested output format
        formatted_output, content_format = await agent_instance._hook_manager.execute_format_output_hooks(
            output=output,
            request_id=request_id,
            session_id=session_id,
            output_format=output_format
        )
        
        logger.debug(f"Formatting complete: format={content_format}, length={len(formatted_output)}")
        return formatted_output, content_format
        
    except Exception as e:
        logger.warning(f"Failed to format output: {e}", exc_info=True)
        return output, 'text'
