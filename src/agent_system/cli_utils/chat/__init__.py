"""Interactive chat mode (REPL) for agent-cli.

``agent-cli chat`` hands over to run_chat_loop; the other modules are the
parts it is made of:

- repl: run_chat_loop -- read a line, resolve it, run the command (through
  the command table) or a turn; Tab completion's knowledge of what exists.
- turn: run_chat_turn and the turn on the REPL's loop, with its two-stage
  Ctrl-C.
- display: ChatRenderer, the live region, and the column arithmetic under it.
- prompt_input: the prompt -- multi-line input, the line editor with its
  history, piped stdin, /edit in $EDITOR.
- typeahead: what is typed while a turn runs.
- context: _ChatContext and the session the chat holds -- presence, the wake
  watch, a fresh session, the save.
- interruptible: work on the REPL's loop that Ctrl-C stops, not the chat.
- token_usage: tokens and cost of the turns, the footer, /costs.
- The commands, by topic: sessions (/new /session /sessions /resume /title
  /vars /attach), agent_setup (/agent /model /think /tools /skills /context),
  transcript (/history /last /undo /retry /rewind /export /copy).

The surface-independent part of the chat -- the command catalogue, the
parser, what a message holds -- lives outside, in agent_system.chat_commands,
chat_actions and plugin_commands, which the web UI reads as well.

Inside the package a module reaches what a sibling defines through that
module (``context._save_now(...)``), never by importing the name: a test
patches a function where it is defined, and the patch reaches every caller.
What is re-exported here is the API (run_chat_loop, run_chat_turn,
ChatRenderer, display_width) and the names the tests read as
``chat.<name>``.
"""
from __future__ import annotations

from agent_system.chat_commands import PluginCommand, resolve as resolve_chat_input, suggest_command
from ...core.session_presence import WAKE_TASK
from .agent_setup import (
    _agent_for,
    _available_skills,
    _expand_skill,
    _set_thinking,
    _show_context,
    _show_skills,
    _show_tools,
    _switch_agent,
    _switch_model,
)
from .context import (
    _WAKE_POLL_S,
    _ChatContext,
    _history_seed,
    _llm_choice,
    _open_fresh_session,
    _save_now,
    _save_session,
    _take_wake_mark,
    _watch_for_wake,
    _WokenAtThePrompt,
)
from .display import (
    _ASCII_SYMBOLS,
    ChatRenderer,
    _restore_logging,
    _silence_stdout_logging,
    display_width,
)
from .interruptible import _drain, _run_interruptible
from .prompt_input import (
    _build_completer,
    _build_prompt_editor,
    _completion_word,
    _compose_in_editor,
    _editor_command,
    _editor_needs_a_terminal,
    _PromptEditor,
    _read_input,
    _skip_piped_bom,
)
from .repl import _completions_for, _help_text, _run_plugin_command, run_chat_loop
from .sessions import (
    _handle_vars,
    _load_recent_sessions,
    _resume_into,
    _resume_last_session,
    _resume_session,
    _set_session_title,
    _task_with_attachments,
)
from .token_usage import _accumulate_usage, _call_pricing_key, _format_usage, _show_costs
from .transcript import (
    _copy_last_answer,
    _copy_to_clipboard,
    _drop_last_exchange,
    _export_transcript,
    _is_real_turn,
    _message_text,
    _render_tool_call,
    _render_tool_result,
    _show_history,
    _show_last,
)
from .turn import _cancel_turn, _execute_turn, _render_answer, run_chat_turn
from .typeahead import _KeyReader, _poll_typed_input

__all__ = [
    # The API: agent_cli runs the chat, help_viewer measures with display_width.
    "run_chat_loop", "run_chat_turn", "ChatRenderer", "display_width",
    # Read by the tests as chat.<name>; each one is patched where it is defined.
    "PluginCommand", "WAKE_TASK", "resolve_chat_input", "suggest_command",
    "_agent_for", "_available_skills", "_expand_skill", "_set_thinking", "_show_context",
    "_show_skills", "_show_tools", "_switch_agent", "_switch_model",
    "_WAKE_POLL_S", "_ChatContext", "_history_seed", "_llm_choice", "_open_fresh_session",
    "_save_now", "_save_session", "_take_wake_mark", "_watch_for_wake", "_WokenAtThePrompt",
    "_ASCII_SYMBOLS", "_restore_logging", "_silence_stdout_logging",
    "_drain", "_run_interruptible",
    "_build_completer", "_build_prompt_editor", "_completion_word", "_compose_in_editor",
    "_editor_command", "_editor_needs_a_terminal", "_PromptEditor", "_read_input",
    "_skip_piped_bom",
    "_completions_for", "_help_text", "_run_plugin_command",
    "_handle_vars", "_load_recent_sessions", "_resume_into", "_resume_last_session",
    "_resume_session", "_set_session_title", "_task_with_attachments",
    "_accumulate_usage", "_call_pricing_key", "_format_usage", "_show_costs",
    "_copy_last_answer", "_copy_to_clipboard", "_drop_last_exchange", "_export_transcript",
    "_is_real_turn", "_message_text", "_render_tool_call", "_render_tool_result",
    "_show_history", "_show_last",
    "_cancel_turn", "_execute_turn", "_render_answer",
    "_KeyReader", "_poll_typed_input",
]
