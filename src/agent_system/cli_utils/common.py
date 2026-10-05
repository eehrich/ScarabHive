"""
Shared CLI utilities for agent-run and agent-cli.

This module provides:
- Color output utilities
- Status event subscriber and display
- Common formatting functions
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from functools import lru_cache

logger = logging.getLogger(__name__)

# STD_OUTPUT_HANDLE / ENABLE_VIRTUAL_TERMINAL_PROCESSING (Windows console API)
_STD_OUTPUT_HANDLE = -11
_STD_INPUT_HANDLE = -10
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


def reassert_vt() -> None:
    """Re-enable VT processing if a child process switched it off.

    Console modes are per-console, not per-process: a spawned shell (cmd.exe,
    MSYS bash) inherits the console and resets its mode on startup. After the
    first terminal.execute() the escapes we keep emitting render literally --
    so anyone painting ANSI after subprocesses ran has to re-assert the flag.
    Unlike _enable_windows_vt this is deliberately uncached.
    """
    if not _IS_WINDOWS:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        if not (mode.value & _ENABLE_VT_PROCESSING):
            kernel32.SetConsoleMode(handle, mode.value | _ENABLE_VT_PROCESSING)
    except Exception as e:
        logger.debug(f"Could not re-assert ANSI processing: {e}")


def snapshot_console_input_mode() -> int | None:
    """Capture the console INPUT mode while it is known-good.

    Child shells don't only reset the output mode -- they also switch the
    input mode (line input, echo, processed Ctrl-C off). At that point every
    keystroke at a prompt lands raw: Enter and Backspace stop working and
    Ctrl-C arrives as a character instead of a signal. Snapshot at REPL start,
    restore via restore_console_input_mode() before each prompt read.
    """
    if not _IS_WINDOWS:
        return None
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return None
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        return int(mode.value)
    except Exception as e:
        logger.debug(f"Could not snapshot console input mode: {e}")
        return None


def restore_console_input_mode(mode: int | None) -> None:
    """Put the console input mode back to its snapshot (no-op for None)."""
    if mode is None or not _IS_WINDOWS:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return
        current = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(current)):
            return
        if current.value != mode:
            kernel32.SetConsoleMode(handle, mode)
    except Exception as e:
        logger.debug(f"Could not restore console input mode: {e}")


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


def dumb_terminal() -> bool:
    """Whether TERM says the terminal shows no escape codes (as Rich reads it)."""
    return os.environ.get("TERM", "").lower() in ("dumb", "unknown")


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
    
    # Auto mode: ANSI only where it will actually render as colour, and not
    # when NO_COLOR is set (no-color.org) or the terminal says it is dumb
    # (an explicit --color still wins).
    if mode == "auto":
        if os.environ.get("NO_COLOR") or dumb_terminal():
            return "text"
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


_LINE_BREAK_TAG = re.compile(r"<br\s*/?>", re.IGNORECASE)
_URL_SCHEME = re.compile(r"([a-z][a-z0-9+.-]*):", re.IGNORECASE)


def render_with_rich(markdown_content: str, code_theme: str = "monokai") -> None:
    """Render markdown content with Rich Console (for ANSI terminal display)."""
    try:
        from markdown_it import MarkdownIt
        from rich.console import Console
        from rich.markdown import Markdown as RichMarkdown

        # Only show_answer calls this, when --color asks for colours (auto: on a terminal, VT switched on):
        # ANSI codes then, into a pipe on Windows too (Rich would take the console API there, which a pipe
        # ignores) and under NO_COLOR or TERM=dumb, which only the automatic choice honours (get_output_format).
        # Under TERM=dumb Rich would detect no colours at all.
        console = Console(force_terminal=True, legacy_windows=False, no_color=False,
                          color_system="standard" if dumb_terminal() else "auto")
        # A dumb terminal shows a link's OSC 8 escape as junk: "text (url)" there.
        dumb = dumb_terminal()
        md = RichMarkdown(markdown_content, code_theme=code_theme, hyperlinks=not dumb)
        # As the chat shows it: a single line break stays one, the model meant it (Rich joins the lines);
        # <br> is one too (a table cell's only line break); other HTML is text -- Rich drops it, and with it
        # "--agent <name>" or "List<T>". Rich's own parser without block HTML and images, which the chat has
        # none of: a line that starts with a tag stays a paragraph, its tags reach the loop below, and an
        # image is "!" and its link. Links as the chat allows them (a terminal's are clickable too): http(s),
        # mailto or relative; any other stays text.
        parser = MarkdownIt().enable("strikethrough").enable("table").disable(["html_block", "image"])
        parser.validateLink = lambda url: (not (scheme := _URL_SCHEME.match(url))
                                           or scheme[1].lower() in ("http", "https", "mailto"))
        md.parsed = parser.parse(markdown_content)
        for token in md.parsed:
            children = token.children or []
            for i, child in enumerate(children):
                if child.type == "softbreak":
                    child.type = "hardbreak"
                elif child.type == "html_inline":
                    child.type = "hardbreak" if _LINE_BREAK_TAG.fullmatch(child.content) else "text"
                elif (dumb and child.type == "link_open" and child.markup == "autolink"
                      and children[i + 1].content == child.attrs["href"]):  # its text is its url: once
                    for mark in (child, children[i + 2]):
                        mark.type, mark.content = "text", ""
            # A <br> that ends a paragraph or a cell, behind closing marks too, draws no line in the chat.
            end = len(children)
            while end and (children[end - 1].type.endswith("_close")
                           or children[end - 1].type == "text" and not children[end - 1].content):
                end -= 1
            if end and children[end - 1].type == "hardbreak":
                del children[end - 1]
        console.print(md)
    except ImportError:
        print(markdown_content)
    except Exception as e:
        logger.warning(f"Failed to render with Rich: {e}")
        print(markdown_content)


def _is_json(text: str) -> bool:
    """A structured answer, or one a prompt asked to be JSON: Markdown drawing would join its lines."""
    stripped = text.strip()
    if stripped[:1] not in ("{", "["):
        return False
    try:
        json.loads(stripped)
    except ValueError:
        return False
    return True


def show_answer(text: str) -> None:
    """An agent's answer on stdout, as --color asks: its Markdown drawn with colours where they show
    (a terminal), HTML for ``--color html``, and the text as the model wrote it otherwise -- into a pipe,
    a file, under NO_COLOR or TERM=dumb. JSON is always printed as it is."""
    output_format = get_output_format()
    if _is_json(text) or output_format not in ("ansi", "html"):
        print(text, flush=True)
        return
    from agent_system.utils.markdown_render import extract_markdown_content, markdown_to_html
    try:
        if output_format == "html":
            print(markdown_to_html(text) or text, flush=True)
        else:
            render_with_rich(extract_markdown_content(text))
    except Exception:
        logger.debug("Drawing the answer failed, printing it as it is", exc_info=True)
        print(text, flush=True)


def print_agent_response(text: str) -> None:
    """An agent's answer between rules, for agent-run (see show_answer)."""
    print("\n" + "="*50)
    print("AGENT RESPONSE:")
    print("="*50)
    show_answer(text)
    print("="*50)
