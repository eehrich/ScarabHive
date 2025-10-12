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
import sys

logger = logging.getLogger(__name__)

# Global color mode (can be set by CLI tools)
color_mode: str = "auto"


def set_color_mode(mode: str) -> None:
    """Set global color mode.
    
    Args:
        mode: One of 'auto', 'always', 'never'
    """
    global color_mode
    color_mode = mode


def supports_color() -> bool:
    """Return whether ANSI color sequences should be used.

    Honors the global `color_mode` which can be set to 'auto',
    'always' or 'never'. In 'auto' mode this checks stdout.isatty().
    """
    if color_mode == "never":
        return False
    if color_mode == "always":
        return True
    # auto
    try:
        return sys.stdout.isatty()
    except Exception as e:
        logger.debug(f"Failed to check if stdout is a TTY: {e}")
        return False


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


def format_result_output(result: dict, show_summary: bool = True) -> str:
    """Format agent result for display.
    
    Args:
        result: Result dictionary from agent execution
        show_summary: Whether to show summary section
        
    Returns:
        Formatted result string
    """
    output = []
    
    if show_summary:
        output.append("\n" + "="*50)
        output.append("AGENT RESPONSE:")
        output.append("="*50)
    
    # Extract summary or response from result
    if isinstance(result, dict):
        summary = result.get("summary", "")
        if summary:
            output.append(summary)
        else:
            # Print the whole result if no summary
            import json
            output.append(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        output.append(str(result))
    
    if show_summary:
        output.append("="*50)
    
    return "\n".join(output)


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
