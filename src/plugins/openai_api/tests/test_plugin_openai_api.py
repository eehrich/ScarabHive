"""openai_api through the real routes -- and through the real ``openai`` SDK, which parses what the routes send.

Behind the routes: a registry of ``ScriptedAgent``s (``run_events`` is the one
seam) with a REAL SessionTracker, SessionService and SessionManager under
tmp_path. The app runs without auth (every caller is "anonymous") except where a
test gives it users; the API-key side of auth is tested in tests/auth and
tests/pluginsystem. A client that leaves is played at the ASGI level
(``leave_during``): an ``http.disconnect`` once the agent runs, as uvicorn sends it.

Mutation checks -- the complete list of all rounds, run again on round 7's code (scratchpad/round7/mutate.py,
list in scratchpad/round7/mutations.py: one at a time, each file restored from a copy and compared byte for
byte). Every one turned the named tests red. Rounds 2-6's entries are restated where later rounds rewrote their
code (_taken became the check of what was written since and the opening mark). Two lines survive their own
removal, by design: in _streamed, `stream.aclose()` alone (the turn's close after it covers the turn; it frees
the generator's frames at once), and the `"logprobs": None` of a chat completion's choice (the SDK has it
optional; it stays for the wire OpenAI sends).
- protocol: turn_from_messages: history not kept
      -> test_chat_history_reaches_the_agent_as_earlier_turns
- protocol: turn_from_messages: system messages dropped
      -> test_system_messages_and_instructions_reach_the_turn_marked
- protocol: _text: other parts accepted
      -> test_what_the_agent_cannot_take_is_refused
- protocol: cache_write_tokens left out
      -> test_what_is_sent_passes_the_sdks_own_validation
- plugin: logprobs left out (the Responses text delta, where the SDK requires them)
      -> test_what_is_sent_passes_the_sdks_own_validation
- plugin: _agent_names: every agent offered
      -> test_models_are_the_agents_the_web_ui_lists
- plugin: _agent_names: blocked_agents ignored
      -> test_models_are_the_agents_the_web_ui_lists
- plugin: _agent_names: only built agents offered
      -> test_models_are_the_agents_the_web_ui_lists
- plugin: create_response: previous_response_id ignored
      -> test_a_response_continues_its_conversation
- plugin: create_response: the latest check removed
      -> test_only_the_latest_response_can_be_continued
- plugin: create_response: store ignored
      -> test_store_false_keeps_nothing
- plugin: create_response: continuation not checked against the models offered
      -> test_a_conversation_ends_with_its_model_s_offer
- plugin: create_response: instructions repeated every turn
      -> test_instructions_join_the_conversation_once
- plugin: malformed: the n check removed
      -> test_a_malformed_request_is_an_openai_error
- plugin: malformed: the stream_options check removed
      -> test_a_malformed_request_is_an_openai_error
- plugin: malformed: the instructions check removed
      -> test_a_malformed_request_is_an_openai_error
- plugin: malformed: the content-type check removed
      -> test_a_malformed_request_is_an_openai_error
- plugin: _user: every caller anonymous
      -> test_a_conversation_is_its_user_s
- store: store.find: the user not checked
      -> test_a_conversation_is_its_user_s
- plugin: _run_to_end: the connection not asked
      -> test_a_client_that_leaves_stops_the_agent
- session_service: save_session: the ephemeral guard removed
      -> test_session_service.py::test_an_ephemeral_session_is_never_written
- turns: open: _hold() not called
      -> test_a_conversation_another_run_has_is_refused
      -> test_the_conversation_is_held_through_its_turn
- turns: _hold: held_here not asked
      -> test_a_conversation_another_run_has_is_refused
- turns: _settle: _let_go() not called
      -> test_the_conversation_is_held_through_its_turn
- turns: _settle: kept without deliver (no-op deliver)
      -> test_a_turn_whose_answer_never_reached_the_client_is_not_kept
- turns: _settle: kept without deliver (TypeError path)
      -> test_a_turn_whose_answer_never_reached_the_client_is_not_kept
- turns: _stop: _ended left out
      -> test_a_turn_whose_answer_never_reached_the_client_is_not_kept
- turns: _keep: no put back when the save or deliver fails
      -> test_a_turn_is_kept_only_once_saved_and_recorded
- turns: _drop: cancel_sub_requests removed
      -> test_chat_completions_keep_no_session
- turns: _delete_record: the belongs_to check removed
      -> test_chat_completions_keep_no_session
- plugin: stop_plugin: returns at once
      -> test_a_turn_still_stopping_at_shutdown_is_settled_before_the_loop_ends
- plugin: Responses stream: the joined deltas as the final text
      -> test_the_answer_is_the_final_message_streamed_or_not
- session_manager: _atomic_write_async: no wait for the thread after a cancel
      -> test_session_service.py::test_a_checkpoint_cancelled_mid_write_lands_before_the_save_after_it
- middleware: middleware: the first access_token cookie
      -> test_endpoint_security_middleware.py::TestBearerApiKey::test_both_layers_decide_alike
- server: SAM _wake_parent: the throwaway check removed
      -> test_plugin_sub_agent_manager_server.py::TestTheCallerIsWokenWhenItsJobIsDone::test_a_throwaway_session_is_told_it_is_not_woken_and_is_not
- server: SAM _never_woken: the throwaway check removed
      -> test_plugin_sub_agent_manager_server.py::TestTheCallerIsWokenWhenItsJobIsDone::test_a_throwaway_session_is_told_it_is_not_woken_and_is_not
- manager: SAM _write_sub_agent: always a warning
      -> test_plugin_sub_agent_manager_manager.py::test_a_throwaway_parent_that_is_gone_is_no_warning
- manager: SAM _write_sub_agent: always debug
      -> test_plugin_sub_agent_manager_manager.py::test_a_throwaway_parent_that_is_gone_is_no_warning
- turns: _stop: the wait not shielded
      -> test_a_client_that_leaves_stops_the_agent
- turns: _stop: the run's token not cancelled
      -> test_a_stopped_turn_cancels_the_runs_token
- turns: _drop: the throwaway session not discarded
      -> test_chat_completions_keep_no_session
- turns: _drop: the coordinator record left
      -> test_chat_completions_keep_no_session
- turns: _keep: deliver() before the save
      -> test_a_response_is_stored_only_with_its_conversation
- turns: events: an error event taken as an answer
      -> test_an_agent_error_is_an_error
- plugin: _deltas: the blank line between calls dropped
      -> test_the_answer_is_the_final_message_streamed_or_not
      -> test_a_call_asked_again_leaves_its_text_out
- turns: _put_back: nothing restored
      -> test_a_failed_turn_leaves_the_conversation_as_it_was
      -> test_a_client_that_leaves_stops_the_agent
- turns: _restore: the store_vars call removed
      -> test_a_turn_put_back_takes_back_the_variables_it_set
- turns: _settle: _forget() not called
      -> test_a_settled_conversation_leaves_the_agents_memory
      -> test_a_new_conversation_that_never_ran_leaves_nothing_behind
- turns: open: a continuation not opening as stored runs anyway
      -> test_a_conversation_no_longer_stored_is_not_continued
- turns: open: no title carried
      -> test_a_new_conversation_is_named_by_the_user_s_own_text
- protocol: the last user text as the title
      -> test_a_new_conversation_is_named_by_the_user_s_own_text
- plugin: create_response: no disconnect check before finish()
      -> test_a_json_client_that_left_as_the_run_ended_keeps_no_turn
- plugin: a refused stream answered as a 200 with response.failed
      -> test_a_conversation_takes_one_turn_at_a_time
      -> test_a_conversation_no_longer_stored_is_not_continued
      -> test_a_conversation_another_run_has_is_refused
- plugin: _streamed: the turn not closed after the response
      -> test_a_stream_that_never_started_still_closes_its_turn
- plugin: _streamed: no background close at all
      -> test_a_stream_whose_client_leaves_during_a_send_is_closed
      -> test_a_stream_that_never_started_still_closes_its_turn
- plugin: stop_plugin: the store not closed
      -> test_stopping_the_plugin_closes_its_store
- turns: _forget: the opening mark not asked (was: _taken)
      -> test_a_conversation_a_web_chat_opened_as_the_turn_settled_stays_in_the_tracker
      -> test_a_web_chat_that_opened_under_the_turn_runs_on_the_whole_conversation
- turns: _put_back: what was written since not asked (was: _taken)
      -> test_a_web_chat_turn_after_the_api_turn_is_not_put_back
      -> test_a_run_that_opened_under_the_turn_and_finished_keeps_its_turn
- turns: _put_back: always taken as written over (was: _taken always True)
      -> test_a_failed_turn_leaves_the_conversation_as_it_was
- turns: _forget: always taken as opened (was: _taken always True)
      -> test_a_settled_conversation_leaves_the_agents_memory
- turns: _put_back: not under the agent's session lock
      -> test_the_put_back_leaves_a_conversation_another_run_has_taken
- turns: _forget: the lock's owner not asked
      -> test_the_put_back_leaves_a_conversation_another_run_has_taken
- turns: open: the agent's lock only checked, not taken
      -> test_a_web_chat_that_starts_as_the_turn_opens_is_refused_not_the_turn
- turns: open: another request's agent lock not refused
      -> test_a_conversation_another_run_has_is_refused
- turns: _settle: the turn's lock not let go (_unlock)
      -> test_a_conversation_no_longer_stored_is_not_continued
- plugin: _starts_over: the length rule only
      -> test_what_a_chunk_stream_carries_when_calls_break_off_think_or_salvage
- plugin: _starts_over: no length rule
      -> test_what_a_chunk_stream_carries_when_calls_break_off_think_or_salvage
- plugin: _starts_over: no thought-in-front branch
      -> test_what_a_chunk_stream_carries_when_calls_break_off_think_or_salvage
- plugin: _starts_over: no thinking-grew branch
      -> test_thinking_in_front_of_the_text_is_the_same_call
- plugin: _deltas: restarted always False
      -> test_a_call_that_broke_off_and_is_asked_again_is_set_apart
      -> test_what_a_chunk_stream_carries_when_calls_break_off_think_or_salvage
- plugin: _rest: no rest, the whole answer again
      -> test_what_a_chunk_stream_carries_when_calls_break_off_think_or_salvage
- plugin: _rest: content that did not go out counts as out
      -> test_a_call_asked_again_on_a_model_that_does_not_stream_still_ends_on_its_answer
- plugin: _deltas: no final piece at all
      -> test_a_model_that_does_not_stream_still_streams
      -> test_a_final_call_that_does_not_stream_still_reaches_a_chunk_stream
- plugin: _deltas: the final piece only when nothing streamed
      -> test_a_final_call_that_does_not_stream_still_reaches_a_chunk_stream
- plugin: store: opened again after stop_plugin
      -> test_a_stopped_plugin_records_nothing
- plugin: create_response: a stopped plugin not refused before the run
      -> test_a_stopped_plugin_records_nothing
- plugin: start_plugin: stays stopped
      -> test_stopping_the_plugin_closes_its_store
- plugin: _open_turn: the busy check removed
      -> test_a_conversation_is_busy_until_its_turn_is_settled
- session_service: open_for_run: the in-use branch removed
      -> test_session_open_for_run.py::test_a_session_a_run_of_this_agent_holds_is_left_to_that_run
      -> test_app_run_opens_its_session.py::test_a_session_a_run_of_this_agent_holds_is_not_read_back_under_it
      -> test_a_web_chat_refused_beside_the_turn_leaves_the_turn_as_it_was
- session_service: open_for_run: in_use alone decides
      -> test_session_open_for_run.py::test_a_session_no_run_of_this_agent_holds_for_another_is_read_as_usual
- session_service: open_for_run: the lock alone decides
      -> test_session_open_for_run.py::test_a_session_no_run_of_this_agent_holds_for_another_is_read_as_usual
- session_service: open_for_run: whose it is not asked
      -> test_session_open_for_run.py::test_another_user_is_refused_a_session_a_run_of_this_agent_holds
- session_service: open_for_run: exists from disk only (was: `exists` always True)
      -> test_session_open_for_run.py::test_a_session_a_run_of_this_agent_holds_is_left_to_that_run
- app: app._bring_the_copy_up_to_date: the lock guard removed
      -> test_app_run_opens_its_session.py::test_a_session_a_run_of_this_agent_holds_is_not_read_back_under_it
- turns: events: the read-back guard removed
      -> test_a_turn_whose_conversation_was_opened_under_it_does_not_run
- plugin: _failed: ConversationBusy not answered as 409
      -> test_a_turn_whose_conversation_was_opened_under_it_does_not_run
- server: _finalize_request: the lock let go before the last save
      -> test_agent_session_lock_order.py::test_the_last_save_happens_while_the_run_holds_its_session_lock
- server: _finalize_request: the lock let go only when the save came through
      -> test_agent_session_lock_order.py::test_a_run_cancelled_in_its_last_save_lets_go_of_the_lock
- server: run_events: the refusal without its error_type
      -> test_agent_session_lock_order.py::test_a_run_refused_at_the_lock_says_so
- session_tracking: SessionTracker.mark_opened: a no-op
      -> test_a_web_chat_that_opened_under_the_turn_runs_on_the_whole_conversation
- session_service: open_for_run: the in-use opening not marked
      -> test_a_web_chat_that_opened_under_the_turn_runs_on_the_whole_conversation
- session_service: open_for_run: a full opening not marked
      -> test_a_conversation_a_web_chat_opened_as_the_turn_settled_stays_in_the_tracker
- app: /run: saved after a refusal
      -> test_app_run_opens_its_session.py::test_a_run_refused_before_it_started_saves_nothing
- app: /run with files: saved after a refusal
      -> test_app_run_opens_its_session.py::test_a_run_refused_before_it_started_saves_nothing
- app: /events: saved after a refusal
      -> test_app_run_opens_its_session.py::test_a_run_refused_before_it_started_saves_nothing
- app: app._refused_before_the_run (was _refused_at_the_lock): never
      -> test_app_run_opens_its_session.py::test_a_run_refused_before_it_started_saves_nothing
- plugin: Responses stream: the conflict answered as response.failed
      -> test_a_stream_whose_conversation_was_read_back_under_it_ends_on_a_conflict
- turns: events: what the run left not taken at its end
      -> test_a_failed_turn_leaves_the_conversation_as_it_was
- session_tracking: append_to_session: the watchers not told
      -> test_a_message_appended_as_a_failed_turn_ends_stays_and_the_turn_goes
      -> test_a_new_conversation_whose_first_turn_fails_keeps_what_was_appended
- session_tracking: append_user_message: the watchers not told
      -> test_a_message_handed_to_the_run_of_a_failed_turn_stays
- turns: open: the appends not watched
      -> test_a_message_appended_as_a_failed_turn_ends_stays_and_the_turn_goes
      -> test_a_message_handed_to_the_run_of_a_failed_turn_stays
- turns: _settle: never unwatched
      -> test_a_message_appended_as_a_failed_turn_ends_stays_and_the_turn_goes
- turns: _put_back: compared with the appends in
      -> test_a_message_appended_after_what_the_run_left_was_taken_stays_and_the_turn_goes
- turns: _restore: the appends not put back
      -> test_a_message_appended_as_a_failed_turn_ends_stays_and_the_turn_goes
      -> test_a_message_handed_to_the_run_of_a_failed_turn_stays
- turns: _restore: a new conversation deleted with its appends
      -> test_a_new_conversation_whose_first_turn_fails_keeps_what_was_appended

Round M1 (the agents' role gate and the refusals before a run), fenced, one at a time, each file restored
and compared by hash. Every one turned the named tests red:
- plugin: _agent_names: the role gate not asked
      -> test_a_model_the_caller_may_not_run_is_not_offered
- plugin: _user: the account not kept for the gate
      -> test_a_model_the_caller_may_not_run_is_not_offered
- plugin: _failed: a run the agent refused answered as a 500
      -> test_a_run_the_agent_refuses_is_an_openai_error_not_a_server_error
- plugin: Responses stream: the refusal answered as response.failed
      -> test_a_run_the_agent_refuses_is_an_openai_error_not_a_server_error
- plugin: Chat Completions stream: the refusal answered as a server error
      -> test_a_run_the_agent_refuses_is_an_openai_error_not_a_server_error
- plugin: _refusal: the 404 without the model's name
      -> test_a_run_the_agent_refuses_is_an_openai_error_not_a_server_error
- plugin: _foreign_conversation: the 403 without its code
      -> test_a_run_the_agent_refuses_is_an_openai_error_not_a_server_error
- turns: events: TurnError without the error_type
      -> test_a_run_the_agent_refuses_is_an_openai_error_not_a_server_error
- turns: _ran: a refused run taken as run (the put back saved)
      -> test_a_refused_continued_turn_leaves_the_stored_conversation_untouched
- server: _refusal_event: without its error_type
      -> test_agent_role_gate.py::test_a_plain_users_run_is_refused_before_anything_of_it_exists
- server: _refusal_event: the two error types swapped
      -> test_agent_role_gate.py::test_a_plain_users_run_is_refused_before_anything_of_it_exists
- facade: MachineAgent.run_events: the refusal without its error_type
      -> test_plugin_stategraph_facade.py::test_a_run_in_a_session_held_for_another_user_starts_no_machine_run
- app: app._refused_before_the_run: SESSION_LOCKED only
      -> test_app_run_opens_its_session.py::test_a_run_refused_before_it_started_saves_nothing
- SAM: continue: a refused run reported as a completed instance
      -> test_plugin_sub_agent_manager_role_gate.py::test_a_continue_the_agent_refuses_after_the_sams_gate_passed_is_an_error
- SAM: _consume_run: the error_type not recorded
      -> test_plugin_sub_agent_manager_role_gate.py::test_a_continue_the_agent_refuses_after_the_sams_gate_passed_is_an_error
- SAM: create: a refused run reported as a completed instance
      -> test_plugin_sub_agent_manager_role_gate.py::test_a_create_the_agent_refuses_after_the_sams_gate_passed_is_an_error
- SAM: the background job without the error_type (and without the run's record)
      -> test_plugin_sub_agent_manager_role_gate.py::test_a_background_create_the_agent_refuses_ends_its_job_with_the_error_type
- SAM: the refusal stored with the "Error: " prefix
      -> test_plugin_sub_agent_manager_role_gate.py::test_a_continue_the_agent_refuses_after_the_sams_gate_passed_is_an_error
- agent_cli: the one-shot run saved after a refusal
      -> test_cli_saves_nothing_after_a_refused_run.py::test_a_one_shot_run_refused_before_it_ran_leaves_the_session_file_alone
- result_utils: collect_final_result: the refusal not marked
      -> test_cli_saves_nothing_after_a_refused_run.py::test_a_one_shot_run_refused_before_it_ran_leaves_the_session_file_alone
- chat: the turn saved after a refusal
      -> test_cli_saves_nothing_after_a_refused_run.py::test_a_chat_turn_refused_before_it_ran_saves_nothing
- agent_run: saved after a refusal
      -> test_agent_run_session_defaults.py::test_a_run_refused_before_it_ran_is_not_saved
- llm_openai: Realtime: the server's error type passed through unprefixed (error event, response.done)
      -> test_llm_openai_realtime.py::test_a_server_error_type_never_passes_for_one_of_the_frameworks_own
      -> test_llm_openai_realtime.py::test_a_failed_response_is_an_error_the_server_can_read
- plugin: get_model: a 404 text of its own
      -> test_a_gated_model_is_answered_exactly_as_an_unknown_one
- server: Agent.call: the refusal without its error_type
      -> test_agent_role_gate.py::test_an_agent_called_as_a_tool_is_refused_with_a_tool_error
- facade: MachineAgent.run_events: the lock refusal without SESSION_LOCKED
      -> test_plugin_stategraph_facade.py::test_a_run_refused_at_the_session_lock_says_so
- SAM: the background job's stored ending without the error_type; the stored-state answer without it
      -> test_plugin_sub_agent_manager_role_gate.py::test_a_background_create_the_agent_refuses_ends_its_job_with_the_error_type
- SAM: _store_blocking_abort without the error_type
      -> test_plugin_sub_agent_manager_role_gate.py::test_a_continue_the_agent_refuses_after_the_sams_gate_passed_is_an_error
- manager: reopen_sub_session keeps an earlier refusal's error_type
      -> test_plugin_sub_agent_manager_role_gate.py::test_a_later_failure_does_not_report_an_earlier_refusal
- SAM: a refused create keeps its instance; manager: discard_sub_session keeps the record / the file
      -> test_plugin_sub_agent_manager_role_gate.py::test_a_create_the_agent_refuses_after_the_sams_gate_passed_is_an_error
- chat: a refused turn cancelled by Ctrl-C saved; the refusal lost with the cancel's result
      -> test_cli_saves_nothing_after_a_refused_run.py::test_a_ctrl_c_in_a_refused_chat_turn_saves_nothing
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any, Callable, Optional

import httpx
import pytest
from fastapi import FastAPI

from plugins.openai_api.plugin import OpenAIApiPlugin
from plugins.openai_api.protocol import INSTRUCTIONS_HEADER

pytestmark = pytest.mark.filterwarnings("ignore:'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning")


class ScriptedAgent:
    """A registry agent: streams ``answer`` word by word as the real loop does (thinking_delta per step with the
    call's ``accumulated`` text, thinking_complete with usage, final, end), keeps the turn in a real
    SessionTracker and -- as ``Agent._finalize_request`` does for every run, a failed or stopped one too --
    saves the session at the run's end. Its token is let go before "end", as the real loop's finalize does. It
    takes the agent's session lock at its start as Agent.run_events does (another request's lock: an error and
    "end", with its ``error_type``), and lets go of it after its last save, as _finalize_request does.

    ``answer`` is a string, an exception instance (an "error" event), or an async callable taking the call.
    ``streams=False`` sends no deltas, as a model that does not stream. ``void_first``: the first LLM call
    streams a little and fails, and is asked again (its thinking_complete has usage and no content).
    ``restarts``: the first call streams a little and breaks off by an exception, and is asked again without a
    thinking_complete in between (a rate limit, a dropped connection). ``final_streams=False``: the last step's
    call sends no deltas (a fallback model that does not stream). ``thinks_midway``: the last step thinks
    before its text and again after its first word (reasoning_delta), and its client keeps the thinking in
    front of the text in ``accumulated``, as the Anthropic client does. ``late_thought``: after the last step's first word a thought
    comes as a content delta, and ``accumulated`` puts the thoughts in front of the text, as the Gemini clients
    do. ``broken`` is what a restarted first call streamed; ``retry_thinking`` stands in front of the text in the
    ``accumulated`` of the call asked again (a client that keeps its thinking there, and thought longer).
    ``drops_tail``: the last step streams its text without its last characters, and its content has them (a
    client that salvages a rest without a delta). ``wrap_up``, if set, is awaited when the run's generator
    closes, before its save; ``let_go`` after it let go of the lock, before its generator has ended.
    """

    def __init__(self, name: str, answer: Any = None, *, streams: bool = True, steps: int = 1,
                 void_first: bool = False, restarts: bool = False, final_streams: bool = True,
                 thinks_midway: bool = False, late_thought: bool = False, broken: str = "broken",
                 retry_thinking: str = "", drops_tail: int = 0):
        from agent_system.servers.agent.components.session_tracking import SessionTracker

        self.name = name
        self.answer = answer if answer is not None else f"{name} says hi"
        self.streams, self.steps, self.void_first, self.restarts = streams, steps, void_first, restarts
        self.final_streams, self.thinks_midway, self.late_thought = final_streams, thinks_midway, late_thought
        self.broken, self.retry_thinking, self.drops_tail = broken, retry_thinking, drops_tail
        self.refused: list[str] = []  # the runs refused at the agent's session lock
        self.agent_config = SimpleNamespace(default_llm_profile="normal", template_vars={})
        self._session_tracker = SessionTracker()
        self._session_service: Any = None
        self.calls: list[dict[str, Any]] = []
        self.running = 0
        self.at_end = False  # the last run said "end"
        self.wrap_up: Optional[Callable[[], Any]] = None
        self.let_go: Optional[Callable[[], Any]] = None
        self.refuses: Optional[str] = None  # an error_type: every run refused before it starts, as the backstop does

    async def run_events(self, task: str, request_id: Optional[str] = None, session_id: Optional[str] = None,
                         **_: Any):
        from agent_system.core.cancellation import get_cancellation_manager
        from agent_system.llm.models import ChatMessage

        tracker = self._session_tracker
        if self.refuses:  # Agent.run_events' backstop: before the lock, nothing of the run written
            yield {"type": "error", "message": f"refused ({self.refuses})", "error_type": self.refuses}
            yield {"type": "end"}
            return
        if not await tracker.acquire_session_lock(session_id, request_id):
            from agent_system.servers.agent.server import SESSION_LOCKED

            self.refused.append(request_id)
            yield {"type": "error", "message": f"Session {session_id} is currently locked by another request",
                   "error_type": SESSION_LOCKED}
            yield {"type": "end"}
            return
        history = list(tracker.get_session_messages(session_id))
        token = get_cancellation_manager().create_token(request_id)
        call = {"task": task, "session": session_id, "request_id": request_id, "token": token,
                "history": [(m.role, m.content) for m in history]}
        self.calls.append(call)
        self.running += 1
        self.at_end = False
        turn = [*history, ChatMessage(role="user", content=task)]
        try:
            yield {"type": "start"}
            answer = self.answer
            if callable(answer):
                answer = await answer(call)
            if isinstance(answer, BaseException):
                yield {"type": "error", "message": str(answer)}
                return
            if (self.void_first or self.restarts) and self.streams:
                yield {"type": "thinking_delta", "step": 1, "delta": self.broken, "accumulated": self.broken}
                if self.void_first:
                    yield {"type": "thinking_complete", "step": 1, "assistant": {},
                           "usage": {"prompt_tokens": 10, "completion_tokens": 1}}
            for step in range(1, self.steps + 1):
                text = answer if step == self.steps else f"step {step} note"
                if self.streams and (step < self.steps or self.final_streams):
                    last = step == self.steps
                    sent = text[:-self.drops_tail] if last and self.drops_tail else text
                    words, accumulated, thinking = sent.split(" "), "", ""
                    prefix = self.retry_thinking if self.restarts and step == 1 else ""
                    if self.thinks_midway and last:  # thinking comes before the text, and stays in front of it
                        thinking = "first thoughts "
                        yield {"type": "reasoning_delta", "step": step, "delta": thinking}
                    for index, word in enumerate(words):
                        delta = word if index == len(words) - 1 else word + " "
                        accumulated += delta
                        yield {"type": "thinking_delta", "step": step, "delta": delta,
                               "accumulated": prefix + thinking + accumulated}
                        if self.thinks_midway and last and index == 0:
                            thinking += "let me think about the rest "
                            yield {"type": "reasoning_delta", "step": step, "delta": "let me think about the rest "}
                        if self.late_thought and last and index == 0:
                            thinking = "hmm "
                            yield {"type": "thinking_delta", "step": step, "delta": thinking,
                                   "accumulated": prefix + thinking + accumulated}
                yield {"type": "thinking_complete", "step": step, "assistant": {"content": text},
                       "usage": {"prompt_tokens": 10, "completion_tokens": 3}}
            turn.append(ChatMessage(role="assistant", content=answer))
            yield {"type": "final", "summary": f"<p>{answer}</p>", "content_format": "html"}
            get_cancellation_manager().unregister_request(request_id)
            self.at_end = True
            yield {"type": "end"}
        finally:
            tracker.set_session_messages(session_id, turn)
            if self.wrap_up is not None:
                await self.wrap_up()
            meta = tracker.get_session_metadata(session_id) or {}
            try:
                await self._session_service.save_session(self, meta.get("user_id", "anonymous"), session_id,
                                                         self.name, "normal", was_new_session=False)
            finally:
                await tracker.release_session_lock(session_id, request_id)
            if self.let_go is not None:
                await self.let_go()
            get_cancellation_manager().unregister_request(request_id)
            self.running -= 1


class Registry:
    """What the app keeps in app.state.tool_registry: list(), get(), describe()."""

    def __init__(self, agents: list[Any], hidden: tuple[str, ...] = (), unbuilt: tuple[str, ...] = ()):
        self.agents = {agent.name: agent for agent in agents}
        self.hidden, self.unbuilt = set(hidden), set(unbuilt)

    def list(self) -> list[str]:
        return [*self.agents, "some_tool_server"]

    def get(self, name: str) -> Any:
        if name == "some_tool_server":
            return object()
        return self.agents[name]

    def describe(self, name: str) -> Any:
        from agent_system.runtime import ServerView

        is_agent = name in self.agents
        return ServerView(name=name, is_agent=is_agent, tool_public=is_agent and name not in self.hidden,
                          tool_visible=False, built=name not in self.unbuilt,
                          min_role=getattr(self.agents.get(name), "min_role", None))


def build(tmp_path, *agents: Any, hidden: tuple[str, ...] = (), unbuilt: tuple[str, ...] = (),
          **config: Any) -> tuple[FastAPI, OpenAIApiPlugin]:
    from agent_system.services.session_manager import SessionManager
    from agent_system.services.session_service import SessionService

    service = SessionService(SessionManager(str(tmp_path / "sessions")), checkpoint_interval_seconds=0)
    for agent in agents:
        agent._session_service = service
    plugin = OpenAIApiPlugin("openai_api", None, SimpleNamespace(responses_db=str(tmp_path / "responses.db"),
                                                                 **config))
    app = FastAPI()
    app.state.tool_registry = Registry(list(agents), hidden, unbuilt)
    app.state.config = None  # no auth: every caller is "anonymous"
    app.include_router(plugin.get_web_router())
    return app, plugin


def client(app: FastAPI, key: str = "unused"):
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=key, base_url="http://test/plugins/openai_api/v1", max_retries=0,
                       http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app)))


def raw(app: FastAPI, key: Optional[str] = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/plugins/openai_api/v1",
                             headers=headers)


def stored(tmp_path) -> list[str]:
    """The session ids on disk, whoever's."""
    return sorted(path.stem for path in (tmp_path / "sessions").rglob("*.json")
                  if not path.name.startswith(".") and "index" not in path.name)


async def leave_during(app: FastAPI, path: str, body: dict[str, Any], leave: Callable[[], bool],
                       stall: Optional[Callable[[dict], bool]] = None) -> list[dict]:
    """One request whose client leaves once ``leave()`` holds: ``http.disconnect``, as uvicorn tells an app.
    A send ``stall(message)`` holds does not return -- a client that stopped reading, uvicorn waiting to write."""
    messages = [{"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}]

    async def receive() -> dict[str, Any]:
        if messages:
            return messages.pop(0)
        while not leave():
            await asyncio.sleep(0.01)
        return {"type": "http.disconnect"}

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)
        if stall is not None and stall(message):
            await asyncio.Event().wait()  # until the response is cancelled

    route = f"/plugins/openai_api/v1{path}"
    await app({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
               "method": "POST", "scheme": "http", "path": route, "raw_path": route.encode(), "root_path": "",
               "query_string": b"", "headers": [(b"host", b"test"), (b"content-type", b"application/json")],
               "client": ("127.0.0.1", 1), "server": ("test", 80)}, receive, send)
    return sent


def presence_on(agent: ScriptedAgent, tmp_path, monkeypatch) -> Any:
    """Session presence as the shipped config has it (core/session_presence.py), its lock files beside the
    test's sessions -- and waking off (max_wake_depth 0), so a session let go never starts an agent-cli run.
    Returns this process's presence store."""
    from agent_system.config.models import SessionPresenceConfig
    from agent_system.core.session_presence import presence_for

    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path / "sessions"))
    agent.system_config = SimpleNamespace(session_presence=SessionPresenceConfig(enabled=True, max_wake_depth=0))
    return presence_for(agent.system_config)


async def a_web_chat_opens(agent: ScriptedAgent, session_id: str) -> None:
    """What /events and /run do with the conversation before their own run (app._open_session_for_run): the session
    counts as in use when this process lists it as running -- its jobs and the agents' session-lock owners
    (BackgroundJobManager.active_sessions, with the agent as the app's default one) -- and
    SessionService.open_for_run opens it so."""
    from agent_system.services.background_job_manager import BackgroundJobManager

    jobs = BackgroundJobManager()
    jobs.set_agent_registry(None, agent)
    running = (await jobs.active_sessions()).get(session_id)
    await agent._session_service.open_for_run(agent, "anonymous", session_id, "normal", in_use=running is not None)


async def its_run(agent: ScriptedAgent, session_id: str, request_id: str = "web_run") -> list[dict]:
    """The opener's run, which Agent.run_events refuses at a lock another request holds. Returns its events."""
    return [event async for event in agent.run_events(task="web message", request_id=request_id,
                                                      session_id=session_id)]


async def the_web_chat_opens_and_runs(agent: ScriptedAgent, session_id: str) -> list[dict]:
    await a_web_chat_opens(agent, session_id)
    return await its_run(agent, session_id)


async def settled(plugin: OpenAIApiPlugin) -> None:
    """Until no turn of the plugin is left to settle (2 s at most)."""
    for _ in range(200):
        if not plugin._busy:
            return
        await asyncio.sleep(0.01)


# ------------------------------------------------------------------ models

async def test_models_are_the_agents_the_web_ui_lists(tmp_path):
    app, _ = build(tmp_path, ScriptedAgent("chat_agent"), ScriptedAgent("writer"), ScriptedAgent("secret"),
                   ScriptedAgent("internal"), ScriptedAgent("lazy"), hidden=("internal",), unbuilt=("lazy",),
                   blocked_agents=["sec*"])
    models = await client(app).models.list()

    assert [m.id for m in models.data] == ["chat_agent", "lazy", "writer"]
    narrowed, _ = build(tmp_path, ScriptedAgent("chat_agent"), ScriptedAgent("writer"), agents=["wri*"])
    assert [m.id for m in (await client(narrowed).models.list()).data] == ["writer"]


# ------------------------------------------------------------------ chat completions

async def test_chat_history_reaches_the_agent_as_earlier_turns(tmp_path):
    agent = ScriptedAgent("chat_agent", "four")
    app, _ = build(tmp_path, agent)
    answer = await client(app).chat.completions.create(model="chat_agent", messages=[
        {"role": "user", "content": "two plus two?"},
        {"role": "assistant", "content": "four"},
        {"role": "user", "content": [{"type": "text", "text": "and times "}, {"type": "text", "text": "two?"}]}])

    assert answer.choices[0].message.content == "four"
    assert answer.usage.prompt_tokens == 10 and answer.usage.completion_tokens == 3
    [call] = agent.calls
    assert call["task"] == "and times two?"
    assert call["history"] == [("user", "two plus two?"), ("assistant", "four")]


@pytest.mark.parametrize("sub_agents", [False, True], ids=["plain", "sub-agents"])
async def test_chat_completions_keep_no_session(tmp_path, monkeypatch, sub_agents):
    """Stateless: nothing in the tracker, nothing on disk -- also not the parent record the sub-agent manager
    writes itself (through SessionManager) when the agent starts sub-agents on a session it does not find. The
    sub-agents it left running in the background are stopped: nobody can continue the session to read what they
    answer. And a call that wrote nothing deletes nothing (a delete takes the manager's global lock and reads
    every user's directory)."""
    from agent_system.core.cancellation import get_cancellation_manager

    agent = ScriptedAgent("chat_agent")
    background = []

    async def starts_a_sub_agent(call: dict) -> str:
        await agent._session_service.session_manager.create_session(
            user_id="anonymous", session_id=call["session"], title="Coordinator Session", agent_name="chat_agent",
            llm_profile="normal")
        # a background job of the sub-agent manager runs under the run's request id (``<request>_async_...``)
        background.append(get_cancellation_manager().create_token(f"{call['request_id']}_async_job1"))
        return "delegated"

    if sub_agents:
        agent.answer = starts_a_sub_agent
    app, _ = build(tmp_path, agent)
    manager = agent._session_service.session_manager
    deletes, delete = [], manager.delete_session

    async def counted(*args: Any, **kwargs: Any) -> None:
        deletes.append(args)
        await delete(*args, **kwargs)

    monkeypatch.setattr(manager, "delete_session", counted)
    await client(app).chat.completions.create(model="chat_agent", messages=[{"role": "user", "content": "hi"}])

    [call] = agent.calls
    assert agent._session_tracker.get_session_messages(call["session"]) == [], "the throwaway session stays"
    assert stored(tmp_path) == [], "a stateless call left a session"
    if sub_agents:
        get_cancellation_manager().unregister_request(f"{call['request_id']}_async_job1")
        assert background[0].is_cancelled, "a background sub-agent of a throwaway turn runs on"
    else:
        assert deletes == [], "a call that wrote nothing went through a delete"


async def test_chat_streams_as_chunks(tmp_path):
    app, _ = build(tmp_path, ScriptedAgent("chat_agent", "all is well"))
    stream = await client(app).chat.completions.create(model="chat_agent", stream=True,
                                                       stream_options={"include_usage": True},
                                                       messages=[{"role": "user", "content": "status?"}])
    chunks = [chunk async for chunk in stream]

    text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    assert text == "all is well"
    assert [c.choices[0].finish_reason for c in chunks if c.choices][-1] == "stop"
    assert chunks[-1].usage.total_tokens == 13 and chunks[-1].choices == []


async def test_system_messages_and_instructions_reach_the_turn_marked(tmp_path):
    agent = ScriptedAgent("chat_agent")
    app, _ = build(tmp_path, agent)
    await client(app).chat.completions.create(model="chat_agent", messages=[
        {"role": "system", "content": "Answer in French."}, {"role": "user", "content": "hello"}])
    await client(app).responses.create(model="chat_agent", instructions="Be brief.", input="hello")

    assert agent.calls[0]["task"] == f"{INSTRUCTIONS_HEADER}\nAnswer in French.\n\nhello"
    assert agent.calls[1]["task"] == f"{INSTRUCTIONS_HEADER}\nBe brief.\n\nhello"


@pytest.mark.parametrize("case", ["image", "tools", "assistant last", "unknown model", "tool message"])
async def test_what_the_agent_cannot_take_is_refused(tmp_path, case):
    agent = ScriptedAgent("chat_agent")
    app, _ = build(tmp_path, agent)
    body: dict[str, Any] = {"model": "chat_agent", "messages": [{"role": "user", "content": "hi"}]}
    if case == "image":
        body["messages"][0]["content"] = [{"type": "image_url", "image_url": {"url": "data:,"}}]
    elif case == "tools":
        body["tools"] = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    elif case == "assistant last":
        body["messages"].append({"role": "assistant", "content": "hello"})
    elif case == "unknown model":
        body["model"] = "ghost"
    else:
        body["messages"].append({"role": "tool", "content": "x", "tool_call_id": "1"})
    async with raw(app) as web:
        answer = await web.post("/chat/completions", json=body)

    assert answer.status_code == (404 if case == "unknown model" else 400), answer.text
    assert answer.json()["error"]["message"]
    assert agent.calls == []


@pytest.mark.parametrize("case", ["n", "stream_options", "instructions", "content type"])
async def test_a_malformed_request_is_an_openai_error(tmp_path, case):
    """Refused before the agent runs, as a JSON error the SDK reads -- not a plain-text 500 it retries."""
    agent = ScriptedAgent("chat_agent")
    app, _ = build(tmp_path, agent)
    chat = {"model": "chat_agent", "messages": [{"role": "user", "content": "hi"}]}
    async with raw(app) as web:
        if case == "n":
            answer = await web.post("/chat/completions", json={**chat, "n": "abc"})
        elif case == "stream_options":
            answer = await web.post("/chat/completions", json={**chat, "stream": True, "stream_options": "x"})
        elif case == "instructions":
            answer = await web.post("/responses", json={"model": "chat_agent", "input": "hi",
                                                        "instructions": ["be", "brief"]})
        else:  # what a page of another origin can post with the user's cookie and without asking first
            answer = await web.post("/responses", content=json.dumps({"model": "chat_agent", "input": "hi"}),
                                    headers={"Content-Type": "text/plain"})

    assert 400 <= answer.status_code < 500, answer.text
    assert answer.json()["error"]["message"]
    assert agent.calls == []


# ------------------------------------------------------------------ responses

async def test_a_response_continues_its_conversation(tmp_path):
    agent = ScriptedAgent("chat_agent", "noted")
    app, _ = build(tmp_path, agent)
    api = client(app)
    first = await api.responses.create(model="chat_agent", input="remember 42")
    second = await api.responses.create(model="chat_agent", input="what number?", previous_response_id=first.id)

    assert first.output_text == "noted" and second.previous_response_id == first.id
    assert agent.calls[1]["session"] == agent.calls[0]["session"]
    assert agent.calls[1]["history"] == [("user", "remember 42"), ("assistant", "noted")]
    assert stored(tmp_path) == [agent.calls[0]["session"]], "the conversation is stored"


async def test_only_the_latest_response_can_be_continued(tmp_path):
    app, _ = build(tmp_path, ScriptedAgent("chat_agent"))
    api = client(app)
    first = await api.responses.create(model="chat_agent", input="one")
    await api.responses.create(model="chat_agent", input="two", previous_response_id=first.id)
    async with raw(app) as web:
        branch = await web.post("/responses", json={"model": "chat_agent", "input": "three",
                                                    "previous_response_id": first.id})
        unknown = await web.post("/responses", json={"model": "chat_agent", "input": "x",
                                                     "previous_response_id": "resp_nothing"})

    assert branch.status_code == 409, branch.text
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "previous_response_not_found"


async def test_a_conversation_ends_with_its_model_s_offer(tmp_path):
    """An agent blocked (or hidden) after the conversation began is not reached through it either."""
    agent = ScriptedAgent("chat_agent")
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="hi")
    plugin.blocked_patterns = ["chat_*"]
    async with raw(app) as web:
        answer = await web.post("/responses", json={"input": "again", "previous_response_id": first.id})

    assert answer.status_code == 404 and answer.json()["error"]["code"] == "model_not_found", answer.text
    assert len(agent.calls) == 1


@pytest.mark.parametrize("gone", ["deleted", "unreadable"])
async def test_a_conversation_no_longer_stored_is_not_continued(tmp_path, gone):
    """Deleted in the web UI (or archived), or a file that does not read any more: the turn would start from an
    empty history and save over what is there -- and a failed one deleted it without a backup. Refused before the
    agent runs; the file stays as it is. A stream is refused with the status too, before it starts."""
    agent = ScriptedAgent("chat_agent")
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session, manager = agent.calls[0]["session"], agent._session_service.session_manager
    path = tmp_path / "sessions" / "anonymous" / f"{session}.json"
    if gone == "deleted":
        await manager.delete_session("anonymous", session)
    else:
        path.write_text("{ half a session", encoding="utf-8")
        manager.clear_cache()
    before = path.read_bytes() if path.exists() else None
    continued = {"model": "chat_agent", "input": "again", "previous_response_id": first.id}

    async with raw(app) as web:
        answer = await web.post("/responses", json=continued)
        streamed = await web.post("/responses", json={**continued, "stream": True})

    assert answer.status_code == 404 and answer.json()["error"]["code"] == "previous_response_not_found", answer.text
    assert streamed.status_code == 404, streamed.text  # a status, not a response.failed after a 200
    assert len(agent.calls) == 1, "the agent ran on a conversation that is not there"
    assert (path.read_bytes() if path.exists() else None) == before, "the stored conversation was touched"
    assert plugin._busy == set()
    assert agent._session_tracker.check_session_locked(session) == (False, None), "a refused turn kept the lock"


@pytest.mark.parametrize("where", ["another process", "this process", "the agent's lock"])
async def test_a_conversation_another_run_has_is_refused(tmp_path, monkeypatch, where):
    """As /run and /events refuse it: two runs would both write the conversation, and the last save would win.
    Another process says so by its lock file. In this one holds nest, so a hold would keep that run out of
    nothing -- and opening the session under it would undo what it has not saved yet; a run of this process
    also holds the agent's session lock, which says so with session presence off too. A stream is refused
    with the status, before it starts."""
    from agent_system.core.session_presence import SessionPresence

    agent = ScriptedAgent("chat_agent", "noted")
    presence = presence_on(agent, tmp_path, monkeypatch) if where != "the agent's lock" else None
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session, tracker = agent.calls[0]["session"], agent._session_tracker
    continued = {"model": "chat_agent", "input": "again", "previous_response_id": first.id}
    if where == "the agent's lock":  # a web-chat run of this process has it (Agent.run_events takes the lock)
        assert await tracker.acquire_session_lock(session, "web_run"), "fixture: the lock was not taken"
    else:
        # Another process: a handle of its own on the lock file, which the OS keeps apart from this process's.
        holder = SessionPresence(tmp_path / "sessions") if where == "another process" else presence
        assert holder.hold(session, "anonymous", "chat_agent"), "fixture: the session could not be held"
    try:
        async with raw(app) as web:
            refused = await web.post("/responses", json=continued)
            streamed = await web.post("/responses", json={**continued, "stream": True})
    finally:
        if where == "the agent's lock":
            await tracker.release_session_lock(session, "web_run")
        else:
            holder.release(session, "anonymous")

    assert refused.status_code == 409 and streamed.status_code == 409, (refused.text, streamed.text)
    assert ("another process" in refused.json()["error"]["message"]) == (where == "another process"), refused.text
    assert len(agent.calls) == 1 and plugin._busy == set()
    again = await client(app).responses.create(model="chat_agent", input="again", previous_response_id=first.id)
    assert again.output_text == "noted", "let go, the conversation goes on"


async def test_the_conversation_is_held_through_its_turn(tmp_path, monkeypatch):
    """From before its session is read until the turn is settled -- a new conversation too -- and let go then."""
    agent = ScriptedAgent("chat_agent")
    presence = presence_on(agent, tmp_path, monkeypatch)
    during = []

    async def looks(call: dict) -> str:
        during.append(presence.held_here(call["session"], "anonymous"))
        return "noted"

    agent.answer = looks
    app, _ = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="one")
    await client(app).responses.create(model="chat_agent", input="two", previous_response_id=first.id)
    session = agent.calls[0]["session"]

    assert during == [True, True], "a turn ran on a conversation it did not hold"
    assert not presence.held_here(session, "anonymous"), "the turn kept its hold"
    assert not (tmp_path / "sessions" / "anonymous" / f"{session}.lock").exists()


async def test_instructions_join_the_conversation_once(tmp_path):
    """The turn is stored with them: the same instructions sent again (as the Agents SDK does every turn) are
    in the conversation already -- other ones reach the agent again."""
    agent = ScriptedAgent("chat_agent")
    app, _ = build(tmp_path, agent)
    api = client(app)
    first = await api.responses.create(model="chat_agent", instructions="Be brief.", input="one")
    second = await api.responses.create(model="chat_agent", instructions="Be brief.", input="two",
                                        previous_response_id=first.id)
    await api.responses.create(model="chat_agent", instructions="Be thorough.", input="three",
                               previous_response_id=second.id)

    assert [call["task"] for call in agent.calls] == [
        f"{INSTRUCTIONS_HEADER}\nBe brief.\n\none", "two", f"{INSTRUCTIONS_HEADER}\nBe thorough.\n\nthree"]


async def test_store_false_keeps_nothing(tmp_path):
    agent = ScriptedAgent("chat_agent")
    app, _ = build(tmp_path, agent)
    passing = await client(app).responses.create(model="chat_agent", input="once", store=False)
    async with raw(app) as web:
        again = await web.post("/responses", json={"model": "chat_agent", "input": "again",
                                                   "previous_response_id": passing.id})

    assert passing.output_text == "chat_agent says hi"
    assert again.status_code == 404
    assert stored(tmp_path) == []


async def test_a_response_is_stored_only_with_its_conversation(tmp_path, monkeypatch):
    agent = ScriptedAgent("chat_agent")
    app, plugin = build(tmp_path, agent)

    async def not_saved(*_: Any, **__: Any) -> bool:
        return False

    monkeypatch.setattr(agent._session_service, "save_session", not_saved)
    async with raw(app) as web:
        answer = await web.post("/responses", json={"model": "chat_agent", "input": "hi"})

    assert answer.status_code == 500 and "could not be saved" in answer.json()["error"]["message"]
    assert plugin.store.latest(agent.calls[0]["session"]) is None, "an id for a conversation that is not stored"


@pytest.mark.parametrize("streamed", [False, True], ids=["plain", "stream"])
async def test_a_failed_turn_leaves_the_conversation_as_it_was(tmp_path, streamed):
    """The agent saves a failed run too; the client's retry from its last response must not build on it."""
    async def answer(call: dict) -> Any:
        return RuntimeError("model down") if call["task"] == "boom" else "noted"

    agent = ScriptedAgent("chat_agent", answer)
    app, _ = build(tmp_path, agent)
    api = client(app)
    first = await api.responses.create(model="chat_agent", input="remember 42")
    async with raw(app) as web:
        await web.post("/responses", json={"model": "chat_agent", "input": "boom", "stream": streamed,
                                           "previous_response_id": first.id})
        await web.post("/responses", json={"model": "chat_agent", "input": "boom", "stream": streamed})
    await api.responses.create(model="chat_agent", input="again", previous_response_id=first.id)

    assert agent.calls[-1]["history"] == [("user", "remember 42"), ("assistant", "noted")]
    assert stored(tmp_path) == [agent.calls[0]["session"]], "the failed new conversation was kept"


async def test_a_turn_put_back_takes_back_the_variables_it_set(tmp_path):
    """A failed turn whose tools changed the session's variables (a workflow phase, say): the agent saved them
    with the run, and a save only merges variables -- put back, the conversation has the ones it had before."""
    seen = []

    async def answer(call: dict) -> Any:
        tracker = agent._session_tracker
        seen.append(dict(tracker.get_session_template_vars(call["session"])))
        if call["task"] == "boom":
            tracker.set_session_template_vars(call["session"], {"phase": "done", "note": "set by the failed turn"})
            return RuntimeError("model down")
        if call["task"] == "remember 42":
            tracker.set_session_template_vars(call["session"], {"phase": "planning"})
        return "noted"

    agent = ScriptedAgent("chat_agent", answer)
    app, _ = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    async with raw(app) as web:
        failed = await web.post("/responses", json={"model": "chat_agent", "input": "boom",
                                                    "previous_response_id": first.id})
    await client(app).responses.create(model="chat_agent", input="again", previous_response_id=first.id)
    stored_vars = (await agent._session_service.session_manager.load_session(
        "anonymous", agent.calls[0]["session"], bypass_cache=True))["context_vars"]

    assert failed.status_code == 500 and seen[1] == {"phase": "planning"}, f"fixture: {failed.text} {seen}"
    assert seen[2] == {"phase": "planning"}, "the next turn runs with the failed turn's variables"
    assert stored_vars == {"phase": "planning"}


@pytest.mark.parametrize("fails", ["save", "record"])
@pytest.mark.parametrize("continued", [False, True], ids=["new", "continued"])
async def test_a_turn_is_kept_only_once_saved_and_recorded(tmp_path, monkeypatch, fails, continued):
    """The turn is saved, then its response id recorded; when either fails the client gets an error and no id,
    so the conversation is put back -- the agent had saved the run already, and a retry from the last response
    must not build on it."""
    import sqlite3

    agent = ScriptedAgent("chat_agent", "noted")
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    service = agent._session_service
    with monkeypatch.context() as patched:
        if fails == "save":
            save, saves = service.save_session, []

            async def the_turns_save_fails(*args: Any, **kwargs: Any) -> bool:
                # 1: the run's own at its end (the agent saves every run), 2: the turn's -- refused, 3: the put back
                saves.append(args)
                return False if len(saves) == 2 else await save(*args, **kwargs)

            patched.setattr(service, "save_session", the_turns_save_fails)
        else:
            def locked(*_: Any, **__: Any) -> None:
                raise sqlite3.OperationalError("database is locked")

            patched.setattr(plugin.store, "add", locked)
        body = {"model": "chat_agent", "input": "boom", **({"previous_response_id": first.id} if continued else {})}
        async with raw(app) as web:
            answer = await web.post("/responses", json=body)

    assert answer.status_code == 500, answer.text
    assert agent.calls[1]["task"] == "boom" and plugin._busy == set()
    assert stored(tmp_path) == [agent.calls[0]["session"]], "a new conversation nobody has an id of stays"
    await client(app).responses.create(model="chat_agent", input="again", previous_response_id=first.id)
    assert agent.calls[-1]["history"] == [("user", "remember 42"), ("assistant", "noted")]


async def test_a_turn_whose_answer_never_reached_the_client_is_not_kept(tmp_path, caplog):
    """A stream whose client leaves after the run said "end" but before the answer went out (measured under
    uvicorn: the cancel lands while the run's generator closes). The run is over and has saved its turn, but the
    client has no id for it -- its retry from its last response must not build on it. And the run is not stopped
    again: its token went with it, and a second cancel only logs that it found none."""
    agent = ScriptedAgent("chat_agent", "noted")
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")

    async def closing_takes_a_moment() -> None:
        """The run's generator closes after "end", and a cancel meanwhile does not cut its save short."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 0.2
        while loop.time() < deadline:
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass

    agent.wrap_up = closing_takes_a_moment
    with caplog.at_level(logging.WARNING):
        await leave_during(app, "/responses", {"model": "chat_agent", "input": "unseen", "stream": True,
                                               "previous_response_id": first.id},
                           leave=lambda: any(turn.completed for turn in plugin._turns))
        agent.wrap_up = None
        await settled(plugin)

    assert agent.calls[1]["task"] == "unseen" and agent.at_end, "fixture: the run did not reach its end"
    assert plugin._busy == set(), "the turn was never settled"
    assert plugin.store.latest(agent.calls[0]["session"]) == first.id
    assert not [r for r in caplog.records if "No cancellation tokens" in r.getMessage()], \
        "a run that was over was stopped again"
    # nothing went wrong on the way either: a settle that failed and happened to put the turn back is no pass
    assert [r.getMessage() for r in caplog.records if r.name.startswith("plugins.openai_api")] == []
    await client(app).responses.create(model="chat_agent", input="again", previous_response_id=first.id)
    assert agent.calls[-1]["history"] == [("user", "remember 42"), ("assistant", "noted")]


@pytest.mark.parametrize("api", ["responses", "chat"])
async def test_a_stream_whose_client_leaves_during_a_send_is_closed(tmp_path, api):
    """Starlette cancels a response whose client left but never closes its body generator. Caught at a yield --
    in the middle of a send: a client that stopped reading, uvicorn waiting to write -- the stream waited for the
    garbage collector, and its turn with it: the conversation busy and held, the turn neither kept nor put back."""
    agent = ScriptedAgent("chat_agent", "noted")
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    stalled: list[dict] = []

    def stall(message: dict) -> bool:
        if b"noted" in message.get("body", b""):  # the first delta
            stalled.append(message)
        return bool(stalled)

    if api == "responses":
        body = {"model": "chat_agent", "input": "cut off", "stream": True, "previous_response_id": first.id}
        await leave_during(app, "/responses", body, leave=lambda: bool(stalled), stall=stall)
    else:
        body = {"model": "chat_agent", "stream": True, "messages": [{"role": "user", "content": "cut off"}]}
        await leave_during(app, "/chat/completions", body, leave=lambda: bool(stalled), stall=stall)

    # asked before anything else runs: a generator nobody holds any more is closed by the loop soon after
    assert stalled, "fixture: no delta was sent"
    assert plugin._busy == set(), "the stream's turn was left open"
    if api == "responses":
        await client(app).responses.create(model="chat_agent", input="again", previous_response_id=first.id)
        assert agent.calls[-1]["history"] == [("user", "remember 42"), ("assistant", "noted")]
    else:
        assert agent._session_tracker.get_session_messages(agent.calls[1]["session"]) == []


async def test_a_new_conversation_that_never_ran_leaves_nothing_behind(tmp_path):
    """Opened and closed without a run -- a stream whose client left before the agent started: nothing of it
    stays in the tracker (a new conversation's entry stayed there for the life of the process)."""
    from agent_system.llm.models import ChatMessage
    from plugins.openai_api import turns

    agent = ScriptedAgent("chat_agent")
    build(tmp_path, agent)
    turn = turns.AgentTurn(agent, agent._session_service, user="anonymous", session_id="fresh1",
                           request_id="oai_never", persist=True)
    await turn.open([ChatMessage(role="user", content="earlier"), ChatMessage(role="assistant", content="turn")])
    assert agent._session_tracker.get_session_metadata("fresh1"), "fixture: open() left nothing to clean up"
    await turn.close()

    assert agent._session_tracker.get_session_metadata("fresh1") is None
    assert not agent._session_tracker.has_session("fresh1")
    assert agent._session_tracker.check_session_locked("fresh1") == (False, None), "the turn kept the agent's lock"
    assert stored(tmp_path) == [] and agent.calls == []


async def test_the_put_back_leaves_a_conversation_another_run_has_taken(tmp_path):
    """The agent lets go of its session lock after its run's last save, and a web-chat run of the same
    conversation can take it before the turn is settled: it has loaded the conversation -- the failed turn with
    it -- and owns it now. Put back under it, its messages and variables were replaced from under it (and its saves brought the turn
    back anyway): the turn stays, and the conversation stays in the tracker for that run."""
    async def answer(call: dict) -> Any:
        return RuntimeError("model down") if call["task"] == "boom" else "noted"

    agent = ScriptedAgent("chat_agent", answer)
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session, tracker = agent.calls[0]["session"], agent._session_tracker

    async def a_web_chat_takes_it() -> None:  # after the run let go of the lock, before the turn is settled
        if agent.calls[-1]["task"] == "boom":
            assert await tracker.acquire_session_lock(session, "web_run"), "fixture: the lock was not taken"

    agent.let_go = a_web_chat_takes_it
    try:
        async with raw(app) as web:
            failed = await web.post("/responses", json={"model": "chat_agent", "input": "boom",
                                                        "previous_response_id": first.id})
        stored_messages = (await agent._session_service.session_manager.load_session(
            "anonymous", session, bypass_cache=True))["messages"]

        assert failed.status_code == 500 and plugin._busy == set(), failed.text
        assert tracker.check_session_locked(session) == (True, "web_run"), "the web run lost its lock"
        assert [m["content"] for m in stored_messages][-1] == "boom", "put back under the run that has it"
        assert tracker.has_session(session), "the conversation left the tracker under the run that has it"
    finally:
        await tracker.release_session_lock(session, "web_run")


async def test_a_json_client_that_left_as_the_run_ended_keeps_no_turn(tmp_path):
    """A JSON answer asks the connection only while the run goes (every DISCONNECT_POLL): a client that left in
    the last moments -- an SDK timeout -- got its turn kept and its id recorded, never saw the id, and every
    retry from its last response met a 409 (not the latest)."""
    agent = ScriptedAgent("chat_agent", "noted")
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    sent = await leave_during(app, "/responses", {"model": "chat_agent", "input": "unseen",
                                                  "previous_response_id": first.id},
                              leave=lambda: len(agent.calls) == 2 and agent.at_end)

    assert agent.calls[1]["task"] == "unseen" and agent.at_end, "fixture: the run did not reach its end"
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [499]
    assert plugin.store.latest(agent.calls[0]["session"]) == first.id
    await client(app).responses.create(model="chat_agent", input="again", previous_response_id=first.id)
    assert agent.calls[-1]["history"] == [("user", "remember 42"), ("assistant", "noted")]


@pytest.mark.parametrize("ends", ["kept", "put back"])
async def test_a_settled_conversation_leaves_the_agents_memory(tmp_path, ends):
    """The agent's tracker never lets go of a session by itself: kept there, every stored conversation stayed in
    memory for the life of the process -- a client starting one per call grew it without bound. Settled, it
    goes; the next turn reads it from disk."""
    async def answer(call: dict) -> Any:
        return RuntimeError("model down") if call["task"] == "boom" else "noted"

    agent = ScriptedAgent("chat_agent", answer)
    app, _ = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    if ends == "put back":
        async with raw(app) as web:
            await web.post("/responses", json={"model": "chat_agent", "input": "boom",
                                               "previous_response_id": first.id})
    session, tracker = agent.calls[0]["session"], agent._session_tracker

    assert not tracker.has_session(session) and tracker.get_session_metadata(session) is None
    await client(app).responses.create(model="chat_agent", input="again", previous_response_id=first.id)
    assert agent.calls[-1]["history"] == [("user", "remember 42"), ("assistant", "noted")], "read back from disk"


@pytest.mark.parametrize("api", ["responses", "chat"])
async def test_a_stream_that_never_started_still_closes_its_turn(tmp_path, api):
    """The turn is opened before the stream (so a refusal is a status). A client gone before the first chunk --
    the response's start never sent -- leaves a body generator that never ran, so never reaches its own close:
    the response closes the turn after it."""
    agent = ScriptedAgent("chat_agent", "noted")
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    stalled: list[dict] = []

    def stall(message: dict) -> bool:
        if message["type"] == "http.response.start":
            stalled.append(message)
        return bool(stalled)

    if api == "responses":
        body = {"model": "chat_agent", "input": "never", "stream": True, "previous_response_id": first.id}
        await leave_during(app, "/responses", body, leave=lambda: bool(stalled), stall=stall)
    else:
        body = {"model": "chat_agent", "stream": True, "messages": [{"role": "user", "content": "never"}]}
        await leave_during(app, "/chat/completions", body, leave=lambda: bool(stalled), stall=stall)

    assert stalled and len(agent.calls) == 1, "fixture: the stream started"
    assert plugin._busy == set() and plugin._turns == set(), "the opened turn was left open"


@pytest.mark.parametrize("earlier", [False, True], ids=["one message", "earlier turns"])
async def test_a_new_conversation_is_named_by_the_user_s_own_text(tmp_path, earlier):
    """The session is named by its first user message, as the web UI names one -- and in front of the user's text
    in the turn stand the client's instructions: the web UI listed "Instructions from the client application:"
    for every conversation that came with instructions. With earlier turns in the input, the first of them."""
    agent = ScriptedAgent("chat_agent")
    app, _ = build(tmp_path, agent)
    question = "What is the capital of France?"
    turns = ([{"role": "user", "content": question}, {"role": "assistant", "content": "Paris."},
              {"role": "user", "content": "And of Italy?"}] if earlier else question)
    await client(app).responses.create(model="chat_agent", instructions="Be brief.", input=turns)
    stored = await agent._session_service.session_manager.load_session("anonymous", agent.calls[0]["session"],
                                                                       bypass_cache=True)

    assert agent.calls[0]["task"].startswith(INSTRUCTIONS_HEADER), "fixture: no instructions in front of the turn"
    assert stored["title"] == question


async def test_stopping_the_plugin_closes_its_store(tmp_path):
    import sqlite3

    app, plugin = build(tmp_path, ScriptedAgent("chat_agent"))
    await client(app).responses.create(model="chat_agent", input="hi")
    store = plugin.store
    await plugin.stop_plugin()

    with pytest.raises(sqlite3.ProgrammingError):
        store.latest("anything")
    assert plugin._store is None, "a plugin started again would find a closed store"
    await plugin.start_plugin()  # started again (the plugin registry starts what it stopped)
    again = await client(app).responses.create(model="chat_agent", input="hi again")
    assert plugin.store.find(again.id, "anonymous"), "a plugin started again recorded nothing"


async def test_a_conversation_a_web_chat_opened_as_the_turn_settled_stays_in_the_tracker(tmp_path):
    """/events opens the conversation -- reads it into the agent's tracker -- before its run takes the agent's
    session lock. A turn settling in between took it out of the tracker again, and the web chat's run started from
    an empty history and saved over the conversation: /events does not read it again (_bring_the_copy_up_to_date),
    this process wrote it last."""
    agent = ScriptedAgent("chat_agent", "noted")
    app, _ = build(tmp_path, agent)
    service = agent._session_service
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session = agent.calls[0]["session"]
    save, saves = service.save_session, []

    async def the_web_chat_opens_after_the_turns_save(*args: Any, **kwargs: Any) -> bool:
        saved = await save(*args, **kwargs)
        saves.append(args)
        if len(saves) == 2:  # 1: the run's own save at its end, 2: the turn's (kept) -- then /events opens it
            await service.open_for_run(agent, "anonymous", session, "normal")
        return saved

    service.save_session = the_web_chat_opens_after_the_turns_save
    try:
        await client(app).responses.create(model="chat_agent", input="and 43", previous_response_id=first.id)
    finally:
        service.save_session = save
    assert service.session_manager.changed_on_disk("anonymous", session) is False, \
        "fixture: /events would read the conversation again"
    async for _ in agent.run_events(task="web message", request_id="web_run", session_id=session):
        pass  # the web chat's run, on what its opening left in the tracker

    assert agent.calls[-1]["history"] == [("user", "remember 42"), ("assistant", "noted"),
                                          ("user", "and 43"), ("assistant", "noted")]


async def boom_fails(call: dict) -> Any:
    return RuntimeError("model down") if call["task"] == "boom" else "noted"


async def stored_contents(service: Any, session: str) -> list[str]:
    record = await service.session_manager.load_session("anonymous", session, bypass_cache=True)
    return [m["content"] for m in record["messages"]]


async def test_a_message_appended_as_a_failed_turn_ends_stays_and_the_turn_goes(tmp_path):
    """/sessions/{id}/append writes a session no run has (app._append_and_persist: Agent.append_to_session, then a
    save) -- and the turn's run lets go of the agent's session lock before its generator ends (the session-end hooks
    come after, Agent._finalize_request). Appended there, the message was part of what the turn took for what its
    run left, and the put back restored the conversation over it: answered "appended", and gone. It is the user's,
    not the turn's: the turn the client never got goes, the message stays."""
    agent = ScriptedAgent("chat_agent", boom_fails)
    app, _ = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session, service = agent.calls[0]["session"], agent._session_service

    async def appended_meanwhile() -> None:
        agent.let_go = None
        assert await agent._session_tracker.append_to_session(session, "appended meanwhile")
        assert await service.save_session(agent, "anonymous", session, agent.name, "normal", was_new_session=False)

    agent.let_go = appended_meanwhile
    async with raw(app) as web:
        await web.post("/responses", json={"model": "chat_agent", "input": "boom", "previous_response_id": first.id})

    assert await stored_contents(service, session) == ["remember 42", "noted", "appended meanwhile"]
    assert agent._session_tracker._append_watchers == {}, "the settled turn still collects appends"


async def test_a_message_appended_after_what_the_run_left_was_taken_stays_and_the_turn_goes(tmp_path):
    """Appended once what the run left was taken, the message made the conversation differ from it, and the put
    back left it as written by another run: the turn its client never got stayed."""
    agent = ScriptedAgent("chat_agent", boom_fails)
    app, _ = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session, service, tracker = agent.calls[0]["session"], agent._session_service, agent._session_tracker
    acquire, appended = tracker.acquire_session_lock, []

    async def appended_before_the_put_back(session_id: str, request_id: str, timeout: float = 5.0,
                                           writer: bool = False) -> bool:
        ended = agent.running == 0 and request_id == agent.calls[-1]["request_id"]
        if ended and not appended:  # the put back asks for the lock: the run is over, what it left taken
            appended.append(request_id)
            assert await acquire(session_id, "write_1")  # as app._append_and_persist writes
            try:
                assert await tracker.append_to_session(session_id, "appended meanwhile")
                assert await service.save_session(agent, "anonymous", session_id, agent.name, "normal",
                                                  was_new_session=False)
            finally:
                await tracker.release_session_lock(session_id, "write_1")
        return await acquire(session_id, request_id, timeout=timeout, writer=writer)

    tracker.acquire_session_lock = appended_before_the_put_back
    async with raw(app) as web:
        await web.post("/responses", json={"model": "chat_agent", "input": "boom", "previous_response_id": first.id})

    assert appended, "fixture: the put back never asked for the lock"
    assert await stored_contents(service, session) == ["remember 42", "noted", "appended meanwhile"]


async def test_the_put_back_waits_for_an_append_still_saving(tmp_path):
    """The append holds the agent's session lock until its save is done. Asked for it meanwhile, the put back was
    refused as if a run had the conversation, and the turn its client never got stayed."""
    agent = ScriptedAgent("chat_agent", boom_fails)
    app, _ = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session, service, tracker = agent.calls[0]["session"], agent._session_service, agent._session_tracker
    saving: list[Any] = []

    async def still_saving() -> None:
        try:
            await asyncio.sleep(1.2)  # the put back asks for the lock meanwhile -- and waits past a second
            assert await service.save_session(agent, "anonymous", session, agent.name, "normal",
                                              was_new_session=False)
        finally:
            await tracker.release_session_lock(session, "write_1")

    async def appended_meanwhile() -> None:
        agent.let_go = None
        assert await tracker.acquire_session_lock(session, "write_1", writer=True)  # as app._beside_the_runs
        assert await tracker.append_to_session(session, "appended meanwhile")
        saving.append(asyncio.ensure_future(still_saving()))

    agent.let_go = appended_meanwhile
    async with raw(app) as web:
        await web.post("/responses", json={"model": "chat_agent", "input": "boom", "previous_response_id": first.id})
    await saving[0]

    assert await stored_contents(service, session) == ["remember 42", "noted", "appended meanwhile"]


async def test_a_kept_turn_does_not_save_over_a_run_that_took_the_conversation(tmp_path):
    """The turn's run lets go of the agent's session lock after its last save, and the turn saves once more before
    it records its response. A web chat that took the conversation in between has its live state in the tracker --
    saved there, an assistant tool call without its result went to disk. The turn's run saved the turn already,
    and the run that has the conversation now runs on it: kept as it is, and delivered."""
    from agent_system.llm.models import ChatMessage

    agent = ScriptedAgent("chat_agent", "noted")
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session, service, tracker = agent.calls[0]["session"], agent._session_service, agent._session_tracker

    async def a_web_chat_takes_it() -> None:
        agent.let_go = None
        assert await tracker.acquire_session_lock(session, "web_run")
        tracker.set_session_messages(session, [*tracker.get_session_messages(session),
                                               ChatMessage(role="user", content="web message"),
                                               ChatMessage(role="assistant", content="a tool call still running")])

    agent.let_go = a_web_chat_takes_it
    try:
        second = await client(app).responses.create(model="chat_agent", input="and 43",
                                                    previous_response_id=first.id)
    finally:
        await tracker.release_session_lock(session, "web_run")

    assert plugin.store.latest(session) == second.id, "the delivered turn was not recorded"
    assert await stored_contents(service, session) == ["remember 42", "noted", "and 43", "noted"]


async def test_a_message_appended_as_the_turn_is_put_back_waits_for_it_and_stays(tmp_path):
    """The put back holds the agent's session lock while it restores and saves the conversation. An append that met
    it was refused as if a run had the conversation (409); it waits for it now, and lands after it."""
    agent = ScriptedAgent("chat_agent", boom_fails)
    app, _ = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session, service, tracker = agent.calls[0]["session"], agent._session_service, agent._session_tracker
    save, saves, appending = service.save_session, [], []

    async def the_append() -> bool:  # what app._append_and_persist does under _beside_the_runs
        if not await tracker.acquire_session_lock(session, "write_1", timeout=5.0, writer=True):
            return False
        try:
            if not tracker.has_session(session):
                await service.load_and_restore_session(agent, "anonymous", session)
            assert await tracker.append_to_session(session, "appended meanwhile")
            return await save(agent, "anonymous", session, agent.name, "normal", was_new_session=False)
        finally:
            await tracker.release_session_lock(session, "write_1")

    async def an_append_meets_the_put_back(*args: Any, **kwargs: Any) -> bool:
        saves.append(args)
        if len(saves) == 2:  # 1: the failed run's own save at its end, 2: the put back's -- the append comes in now
            appending.append(asyncio.ensure_future(the_append()))
            await asyncio.sleep(0.01)
        return await save(*args, **kwargs)

    service.save_session = an_append_meets_the_put_back
    try:
        async with raw(app) as web:
            await web.post("/responses", json={"model": "chat_agent", "input": "boom",
                                               "previous_response_id": first.id})
    finally:
        service.save_session = save

    assert appending and await appending[0], "the append was refused by the put back"
    assert await stored_contents(service, session) == ["remember 42", "noted", "appended meanwhile"]


async def test_a_new_conversation_whose_first_turn_fails_keeps_what_was_appended(tmp_path):
    """Put back, a new conversation is deleted -- with the message the user appended to it after its run had saved
    it (the web UI lists it from then on)."""
    agent = ScriptedAgent("chat_agent", boom_fails)
    app, _ = build(tmp_path, agent)
    service = agent._session_service

    async def appended_meanwhile() -> None:
        agent.let_go = None
        session = agent.calls[-1]["session"]
        assert await agent._session_tracker.append_to_session(session, "appended meanwhile")
        assert await service.save_session(agent, "anonymous", session, agent.name, "normal", was_new_session=False)

    agent.let_go = appended_meanwhile
    async with raw(app) as web:
        await web.post("/responses", json={"model": "chat_agent", "input": "boom"})

    assert await stored_contents(service, agent.calls[-1]["session"]) == ["appended meanwhile"]


async def test_a_message_handed_to_the_run_of_a_failed_turn_stays(tmp_path):
    """The append endpoint hands a message to a run that has the session (Agent.append_user_message), and the run
    takes it in -- into what it left. The put back of a failed turn took it with the turn."""
    tracker_of: dict[str, Any] = {}

    async def answer(call: dict) -> Any:
        if call["task"] != "boom":
            return "noted"
        tracker = tracker_of["agent"]._session_tracker
        # as Agent.run_events registers its request: the queue an append to the run goes to
        tracker.register_request(call["request_id"], call["session"],
                                 {"appended": [], "message_event": asyncio.Event()})
        assert await tracker.append_user_message(call["request_id"], "handed to the run")
        return RuntimeError("model down")

    agent = ScriptedAgent("chat_agent", answer)
    tracker_of["agent"] = agent
    app, _ = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session, service, tracker = agent.calls[0]["session"], agent._session_service, agent._session_tracker

    async def takes_in_late_messages() -> None:  # Agent._finalize_request, before its last save
        request_id = agent.calls[-1]["request_id"]
        tracker.set_session_messages(session, await tracker.drain_appended_messages(
            request_id, list(tracker.get_session_messages(session))))
        tracker.unregister_request(request_id)

    agent.wrap_up = takes_in_late_messages
    async with raw(app) as web:
        await web.post("/responses", json={"model": "chat_agent", "input": "boom", "previous_response_id": first.id})

    assert await stored_contents(service, session) == ["remember 42", "noted", "handed to the run"]


async def test_a_message_appended_before_the_turn_goes_with_nothing(tmp_path):
    """Only what is appended after the turn opened the conversation is set apart: one before is part of what it
    opened, and the put back keeps it as that."""
    agent = ScriptedAgent("chat_agent", boom_fails)
    app, _ = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session, service = agent.calls[0]["session"], agent._session_service
    await service.load_and_restore_session(agent, "anonymous", session)  # what the append does with a settled one
    assert await agent._session_tracker.append_to_session(session, "appended before")
    assert await service.save_session(agent, "anonymous", session, agent.name, "normal", was_new_session=False)

    async with raw(app) as web:
        await web.post("/responses", json={"model": "chat_agent", "input": "boom", "previous_response_id": first.id})

    assert await stored_contents(service, session) == ["remember 42", "noted", "appended before"]
    assert agent.calls[-1]["history"][-1] == ("user", "appended before")


async def test_a_message_appended_as_the_turn_is_kept_waits_for_it_and_is_saved(tmp_path):
    """The append saved a few awaits after its message went in, and a turn settling in between took the
    conversation out of the agent's tracker: the save found nothing to write. The turn keeps it under the agent's
    session lock now, as a writer: the append waits for that, finds the conversation gone from the tracker, reads
    it back (app._append_and_persist) and saves it with its message."""
    agent = ScriptedAgent("chat_agent", "noted")
    app, _ = build(tmp_path, agent)
    service, tracker = agent._session_service, agent._session_tracker
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session = agent.calls[0]["session"]
    save, saves, appending = service.save_session, [], []

    async def the_append() -> bool:  # what app._append_and_persist does under _beside_the_runs
        if not await tracker.acquire_session_lock(session, "write_1", timeout=5.0, writer=True):
            return False
        try:
            if not tracker.has_session(session):
                await service.load_and_restore_session(agent, "anonymous", session)
            assert await tracker.append_to_session(session, "appended meanwhile")
            return await save(agent, "anonymous", session, agent.name, "normal", was_new_session=False)
        finally:
            await tracker.release_session_lock(session, "write_1")

    async def an_append_meets_the_turns_save(*args: Any, **kwargs: Any) -> bool:
        saves.append(args)
        if len(saves) == 2:  # 1: the run's own save at its end, 2: the turn's (kept) -- the append comes in now
            appending.append(asyncio.ensure_future(the_append()))
            await asyncio.sleep(0.01)
        return await save(*args, **kwargs)

    service.save_session = an_append_meets_the_turns_save
    try:
        await client(app).responses.create(model="chat_agent", input="and 43", previous_response_id=first.id)
    finally:
        service.save_session = save

    assert appending and await appending[0], "the append was refused, or its save found no conversation"
    assert await stored_contents(service, session) == ["remember 42", "noted", "and 43", "noted",
                                                       "appended meanwhile"]


@pytest.mark.parametrize("when", ["during the run", "before the run"])
async def test_a_web_chat_refused_beside_the_turn_leaves_the_turn_as_it_was(tmp_path, when):
    """A web chat on the conversation while an API turn has it: /events opens the session before its own run is
    refused at the agent's session lock. Opening it read the session back from disk under the turn and set new
    metadata -- the client's earlier input items were gone before the turn's run read them, and a failed turn
    counted as taken by another request and stayed in the conversation. Opened as in use now, it is left alone."""
    web: list[dict] = []

    async def answer(call: dict) -> Any:
        if call["task"] == "boom":
            if when == "during the run":
                web.extend(await the_web_chat_opens_and_runs(agent, call["session"]))
            return RuntimeError("model down")
        return "noted"

    agent = ScriptedAgent("chat_agent", answer)
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session = agent.calls[0]["session"]
    if when == "before the run":
        run_events = agent.run_events

        async def the_web_chat_first(task: str, request_id: Optional[str] = None,
                                     session_id: Optional[str] = None, **kwargs: Any):
            if task == "boom" and not web:
                web.extend(await the_web_chat_opens_and_runs(agent, session_id))
            async for event in run_events(task, request_id=request_id, session_id=session_id, **kwargs):
                yield event

        agent.run_events = the_web_chat_first
    async with raw(app) as api:
        failed = await api.post("/responses", json={"model": "chat_agent", "previous_response_id": first.id,
                                                    "input": [{"role": "user", "content": "A"},
                                                              {"role": "assistant", "content": "B"},
                                                              {"role": "user", "content": "boom"}]})
    await settled(plugin)
    stored = await agent._session_service.session_manager.load_session("anonymous", session, bypass_cache=True)

    assert failed.status_code == 500 and agent.refused == ["web_run"], (failed.text, agent.refused, web)
    assert agent.calls[1]["history"] == [("user", "remember 42"), ("assistant", "noted"), ("user", "A"),
                                         ("assistant", "B")], "the turn ran without the client's earlier input"
    assert [m["content"] for m in stored["messages"]] == ["remember 42", "noted"], "the failed turn stayed"


async def test_a_web_chat_that_opened_under_the_turn_runs_on_the_whole_conversation(tmp_path):
    """/events opens the conversation while the API turn's run has it -- in use, the opening leaves the tracker as it
    is -- and its run gets the session once the turn's run has let go of it. The turn, settled in between, took the
    conversation out of the tracker (_forget): the web chat's run started from nothing and saved over the whole
    conversation, since /events does not read it again (this process wrote it last)."""
    async def answer(call: dict) -> str:
        if call["task"] == "and 43":
            await a_web_chat_opens(agent, call["session"])
        return "noted"

    agent = ScriptedAgent("chat_agent", answer)
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    await client(app).responses.create(model="chat_agent", input="and 43", previous_response_id=first.id)
    await settled(plugin)
    session, manager = agent.calls[0]["session"], agent._session_service.session_manager
    assert manager.changed_on_disk("anonymous", session) is False, "fixture: /events would read it again"
    events = await its_run(agent, session)
    stored = await manager.load_session("anonymous", session, bypass_cache=True)

    assert events[-1]["type"] == "end" and agent.refused == [], "fixture: the web chat's run was refused"
    assert agent.calls[-1]["history"] == [("user", "remember 42"), ("assistant", "noted"), ("user", "and 43"),
                                          ("assistant", "noted")], "the web chat ran without the conversation"
    assert [m["content"] for m in stored["messages"]][:4] == ["remember 42", "noted", "and 43", "noted"]


async def test_a_run_that_opened_under_the_turn_and_finished_keeps_its_turn(tmp_path):
    """A /run on the conversation opens it while the API turn's run has it, runs once that run has let go of the
    lock, and finishes -- all while the API turn's stream waits for a client that stopped reading. When that client
    leaves, the turn is put back: over the /run's finished turn, which went with it. Written over since the turn's
    run, the conversation is left as it is."""
    agent = ScriptedAgent("chat_agent", "noted")
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session = agent.calls[0]["session"]
    opened, done = asyncio.Event(), []

    async def the_run_opens(call: dict) -> str:
        if call["task"] == "api turn":
            await a_web_chat_opens(agent, call["session"])  # /run: open as in use, while the API run has it
            opened.set()
        return "noted"

    agent.answer = the_run_opens
    stalled: list[dict] = []

    def stall(message: dict) -> bool:
        if b"noted" in message.get("body", b""):
            stalled.append(message)
        return bool(stalled)

    async def then_it_runs() -> None:
        await opened.wait()
        while not (len(agent.calls) == 2 and agent.at_end and agent.running == 0):
            await asyncio.sleep(0.01)
        done.extend(await its_run(agent, session, request_id="run_req"))

    later = asyncio.ensure_future(then_it_runs())
    body = {"model": "chat_agent", "input": "api turn", "stream": True, "previous_response_id": first.id}
    await leave_during(app, "/responses", body, leave=lambda: bool(done), stall=stall)
    await later
    await settled(plugin)
    stored = await agent._session_service.session_manager.load_session("anonymous", session, bypass_cache=True)

    assert stalled and agent.refused == [], "fixture: the stream did not stall, or the /run was refused"
    assert [m["content"] for m in stored["messages"]] == ["remember 42", "noted", "api turn", "noted",
                                                          "web message", "noted"]


async def test_a_stream_whose_conversation_was_read_back_under_it_ends_on_a_conflict(tmp_path, monkeypatch):
    """Refused after its stream began (AgentTurn.events): a Responses stream ends on an "error" event with the
    code "conflict" -- response.failed takes only OpenAI's own codes, and "server_error" invites a retry."""
    from openai.types.responses import ResponseStreamEvent
    from pydantic import TypeAdapter

    from plugins.openai_api import turns

    agent = ScriptedAgent("chat_agent", "noted")
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    opened = turns.AgentTurn.open

    async def an_opener_reads_it_back(self: Any, history: list[Any]) -> None:
        await opened(self, history)
        if self.continues:
            # open_for_run asks (the lock, as a writer) and leaves it alone: read back without asking
            await agent._session_service.load_and_restore_session(agent, "anonymous", self.session_id)

    monkeypatch.setattr(turns.AgentTurn, "open", an_opener_reads_it_back)
    async with raw(app) as web:
        answer = await web.post("/responses", json={"model": "chat_agent", "previous_response_id": first.id,
                                                    "stream": True, "input": [{"role": "user", "content": "A"},
                                                                              {"role": "assistant", "content": "B"},
                                                                              {"role": "user", "content": "C"}]})
    await settled(plugin)
    events = [json.loads(line[6:]) for line in answer.text.splitlines() if line.startswith("data: ")]

    assert (events[-1]["type"], events[-1]["code"]) == ("error", "conflict"), events[-1]
    for event in events:
        TypeAdapter(ResponseStreamEvent).validate_python(event)
    assert len(agent.calls) == 1 and plugin._busy == set()


async def test_a_turn_whose_conversation_was_opened_under_it_does_not_run(tmp_path, monkeypatch):
    """An opener that does not ask whether the session runs here (none of the app's, and open_for_run asks too
    -- this is the guard for any other) reads the conversation back between the turn's opening and its run: the
    client's earlier input items are gone from the tracker. The turn does not run on that (409), and leaves the
    conversation to the opener."""
    from plugins.openai_api import turns

    agent = ScriptedAgent("chat_agent", "noted")
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session = agent.calls[0]["session"]
    opened = turns.AgentTurn.open

    async def an_opener_reads_it_back(self: Any, history: list[Any]) -> None:
        await opened(self, history)
        if self.continues:
            # open_for_run asks (the lock, as a writer) and leaves it alone: read back without asking
            await agent._session_service.load_and_restore_session(agent, "anonymous", self.session_id)

    monkeypatch.setattr(turns.AgentTurn, "open", an_opener_reads_it_back)
    async with raw(app) as web:
        refused = await web.post("/responses", json={"model": "chat_agent", "previous_response_id": first.id,
                                                     "input": [{"role": "user", "content": "A"},
                                                               {"role": "assistant", "content": "B"},
                                                               {"role": "user", "content": "C"}]})
    await settled(plugin)

    assert refused.status_code == 409 and refused.json()["error"]["type"] == "conflict", refused.text
    assert len(agent.calls) == 1, "the turn ran on a history read back under it"
    assert plugin._busy == set()
    assert agent._session_tracker.check_session_locked(session) == (False, None), "the turn kept the lock"


async def test_a_web_chat_turn_after_the_api_turn_is_not_put_back(tmp_path):
    """A stream whose client stopped reading (a laptop that sleeps): the API run ends and lets go of the agent's
    session lock, a web-chat run of the same conversation starts and finishes -- and then the API client leaves.
    Put back then, under a lock that is free again, the conversation went back to before the API turn, and the
    web chat's finished turn with it. Opened by another request since, it is that one's: nothing is put back."""
    agent = ScriptedAgent("chat_agent", "noted")
    app, plugin = build(tmp_path, agent)
    service = agent._session_service
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session = agent.calls[0]["session"]
    stalled: list[dict] = []
    web_done: list[bool] = []

    def stall(message: dict) -> bool:
        if b"noted" in message.get("body", b""):
            stalled.append(message)
        return bool(stalled)

    async def web_chat() -> None:
        while not (len(agent.calls) == 2 and agent.at_end and agent.running == 0):
            await asyncio.sleep(0.01)
        await service.open_for_run(agent, "anonymous", session, "normal")  # /events opens it ...
        async for _ in agent.run_events(task="web message", request_id="web_run", session_id=session):
            pass  # ... and runs it (under the agent's lock, which the run takes and lets go of)
        web_done.append(True)

    web = asyncio.ensure_future(web_chat())
    body = {"model": "chat_agent", "input": "api turn", "stream": True, "previous_response_id": first.id}
    await leave_during(app, "/responses", body, leave=lambda: bool(web_done), stall=stall)
    await web
    await settled(plugin)
    stored = await service.session_manager.load_session("anonymous", session, bypass_cache=True)

    assert stalled and plugin._busy == set(), "fixture: the stream did not stall, or the turn was never settled"
    assert [m["content"] for m in stored["messages"]] == ["remember 42", "noted", "api turn", "noted",
                                                          "web message", "noted"]


async def test_a_web_chat_that_starts_as_the_turn_opens_is_refused_not_the_turn(tmp_path, monkeypatch):
    """Between a turn's opening and its run taking the agent's session lock, a web-chat run of the conversation
    could start: it took the lock, and the API run failed at it -- a 500 (or a response.failed) where a 409 was
    due. The turn holds the lock from its opening on, by its run's request id: the run takes it again, the web
    chat is refused, and the turn lets go of it however it ends."""
    agent = ScriptedAgent("chat_agent", "noted")
    app, _ = build(tmp_path, agent)
    service, tracker = agent._session_service, agent._session_tracker
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    session = agent.calls[0]["session"]
    open_for_run, web_got_it = service.open_for_run, []

    async def a_web_chat_starts_meanwhile(*args: Any, **kwargs: Any) -> bool:
        existed = await open_for_run(*args, **kwargs)
        web_got_it.append(await tracker.acquire_session_lock(session, "web_run", timeout=0.1))
        return existed

    monkeypatch.setattr(service, "open_for_run", a_web_chat_starts_meanwhile)
    try:
        async with raw(app) as web:
            answer = await web.post("/responses", json={"model": "chat_agent", "input": "again",
                                                        "previous_response_id": first.id})
    finally:
        if web_got_it == [True]:
            await tracker.release_session_lock(session, "web_run")

    assert web_got_it == [False], "the web chat got the lock"
    assert answer.status_code == 200 and agent.refused == [], answer.text
    assert tracker.check_session_locked(session) == (False, None), "the turn kept the lock"


async def test_a_stopped_plugin_records_nothing(tmp_path, monkeypatch):
    """After stop_plugin the response store stays closed: a turn still running then (it outlasted the wait, or its
    plugin was unregistered at runtime while its routes stay mounted) fails at its record and is put back,
    instead of opening a store nobody closes -- and a new request is refused before it runs."""
    from plugins.openai_api import turns

    monkeypatch.setattr(turns, "STOP_GRACE", 0.05)
    gate = asyncio.Event()

    async def answer(call: dict) -> str:
        if call["task"] == "slow":
            await gate.wait()
        return "noted"

    agent = ScriptedAgent("chat_agent", answer)
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    async with raw(app) as web:
        slow = asyncio.ensure_future(web.post("/responses", json={"model": "chat_agent", "input": "slow",
                                                                 "previous_response_id": first.id}))
        while len(agent.calls) < 2:
            await asyncio.sleep(0.01)
        await plugin.stop_plugin()  # its wait for the running turn gives up after STOP_GRACE
        gate.set()
        late = await slow
        refused = await web.post("/responses", json={"model": "chat_agent", "input": "new"})
    stored = await agent._session_service.session_manager.load_session("anonymous", agent.calls[0]["session"],
                                                                       bypass_cache=True)

    assert late.status_code == 503 and refused.status_code == 503, (late.text, refused.text)
    assert refused.json()["error"]["message"], refused.text
    assert plugin._store is None, "a stopped plugin opened its store again"
    assert len(agent.calls) == 2, "a new request ran on a stopped plugin"
    assert [m["content"] for m in stored["messages"]] == ["remember 42", "noted"], "the late turn was kept"


async def test_responses_stream_in_order(tmp_path):
    app, _ = build(tmp_path, ScriptedAgent("chat_agent", "all is well"))
    stream = await client(app).responses.create(model="chat_agent", input="status?", stream=True)
    events = [event async for event in stream]

    assert [e.type for e in events] == [
        "response.created", "response.in_progress", "response.output_item.added", "response.content_part.added",
        "response.output_text.delta", "response.output_text.delta", "response.output_text.delta",
        "response.output_text.done", "response.content_part.done", "response.output_item.done",
        "response.completed"]
    assert [e.sequence_number for e in events] == list(range(1, 12))
    assert events[-1].response.output_text == "all is well" and events[-1].response.status == "completed"


async def test_what_is_sent_passes_the_sdks_own_validation(tmp_path):
    """The SDK parses leniently; a strictly typed client validates. Every object and event against the SDK's
    own models."""
    from openai.types.chat import ChatCompletion, ChatCompletionChunk
    from openai.types.responses import Response, ResponseStreamEvent
    from pydantic import TypeAdapter

    app, _ = build(tmp_path, ScriptedAgent("chat_agent", "all is well"))
    chat = {"model": "chat_agent", "messages": [{"role": "user", "content": "hi"}]}
    async with raw(app) as web:
        response = (await web.post("/responses", json={"model": "chat_agent", "input": "hi"})).json()
        events = await web.post("/responses", json={"model": "chat_agent", "input": "hi", "stream": True})
        completion = (await web.post("/chat/completions", json=chat)).json()
        chunks = await web.post("/chat/completions", json={**chat, "stream": True,
                                                           "stream_options": {"include_usage": True}})

    def data(answer: httpx.Response) -> list[dict]:
        return [json.loads(line[6:]) for line in answer.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"]

    Response.model_validate(response)
    ChatCompletion.model_validate(completion)
    for event in data(events):
        TypeAdapter(ResponseStreamEvent).validate_python(event)
    for chunk in data(chunks):
        ChatCompletionChunk.model_validate(chunk)


async def test_a_model_that_does_not_stream_still_streams(tmp_path):
    app, _ = build(tmp_path, ScriptedAgent("chat_agent", "in one piece", streams=False))
    stream = await client(app).chat.completions.create(model="chat_agent", stream=True,
                                                       messages=[{"role": "user", "content": "hi"}])

    assert "".join([c.choices[0].delta.content or "" async for c in stream if c.choices]) == "in one piece"


async def test_the_answer_is_the_final_message_streamed_or_not(tmp_path):
    """A multi-step run: the JSON answer and every final text of the stream are the agent's final message; the
    deltas are the work as it happens, the LLM calls set apart."""
    app, _ = build(tmp_path, ScriptedAgent("chat_agent", "done", steps=2))
    api = client(app)
    plain = await api.responses.create(model="chat_agent", input="go")
    stream = await api.responses.create(model="chat_agent", input="go", stream=True)
    events = [event async for event in stream]
    last = {event.type: event for event in events}

    assert plain.output_text == "done"
    assert last["response.output_text.done"].text == "done"
    assert last["response.content_part.done"].part.text == "done"
    assert last["response.output_item.done"].item.content[0].text == "done"
    assert last["response.completed"].response.output_text == "done"
    assert "".join(e.delta for e in events if e.type == "response.output_text.delta") == "step 1 note\n\ndone"


async def test_a_call_asked_again_leaves_its_text_out(tmp_path):
    """An LLM call that failed after it streamed a little is asked again in the same step: what it streamed is
    out, set apart -- the answer is the agent's final message. Its usage counts."""
    app, _ = build(tmp_path, ScriptedAgent("chat_agent", "all is well", void_first=True))
    stream = await client(app).responses.create(model="chat_agent", input="status?", stream=True)
    events = [event async for event in stream]

    assert "".join(e.delta for e in events if e.type == "response.output_text.delta") == "broken\n\nall is well"
    assert events[-1].response.output_text == "all is well"
    assert events[-1].response.usage.output_tokens == 4


async def test_a_call_that_broke_off_and_is_asked_again_is_set_apart(tmp_path):
    """A call that fails mid-stream by an exception (a rate limit, a dropped connection) is asked again without a
    thinking_complete in between: its ``accumulated`` starts over, and that sets the new call apart."""
    app, _ = build(tmp_path, ScriptedAgent("chat_agent", "all is well", restarts=True))
    stream = await client(app).chat.completions.create(model="chat_agent", stream=True,
                                                       messages=[{"role": "user", "content": "status?"}])

    assert "".join([c.choices[0].delta.content or "" async for c in stream if c.choices]) == "broken\n\nall is well"


async def test_a_final_call_that_does_not_stream_still_reaches_a_chunk_stream(tmp_path):
    """A turn whose last call runs on a model that does not stream (a fallback profile, say) after an earlier
    call streamed: the final message comes in one piece after the deltas before it -- a chunk stream has nothing
    else to carry it."""
    app, _ = build(tmp_path, ScriptedAgent("chat_agent", "done", steps=2, final_streams=False))
    stream = await client(app).chat.completions.create(model="chat_agent", stream=True,
                                                       messages=[{"role": "user", "content": "go"}])

    assert "".join([c.choices[0].delta.content or "" async for c in stream if c.choices]) == "step 1 note\n\ndone"


async def test_a_call_asked_again_on_a_model_that_does_not_stream_still_ends_on_its_answer(tmp_path):
    """A call breaks off mid-stream and is asked again on a fallback that does not stream: no thinking_complete
    between the two, no delta of the second. What went out is the broken text -- the answer has to follow it."""
    app, _ = build(tmp_path, ScriptedAgent("chat_agent", "the real answer", restarts=True, final_streams=False))
    api = client(app)
    messages = [{"role": "user", "content": "status?"}]
    stream = await api.chat.completions.create(model="chat_agent", stream=True, messages=messages)
    streamed = "".join([c.choices[0].delta.content or "" async for c in stream if c.choices])
    plain = await api.chat.completions.create(model="chat_agent", messages=messages)

    assert plain.choices[0].message.content == "the real answer"
    assert streamed == "broken\n\nthe real answer"


async def test_thinking_in_front_of_the_text_is_the_same_call(tmp_path):
    """Anthropic's client keeps the call's thinking in front of its text in ``accumulated`` (Gemini's its
    thoughts): thinking after the first words makes it grow by more than the delta -- the same call going on,
    no new one set apart, and no final piece after it either."""
    app, _ = build(tmp_path, ScriptedAgent("chat_agent", "all is well", thinks_midway=True))
    stream = await client(app).chat.completions.create(model="chat_agent", stream=True,
                                                       messages=[{"role": "user", "content": "status?"}])

    assert "".join([c.choices[0].delta.content or "" async for c in stream if c.choices]) == "all is well"


@pytest.mark.parametrize("case", ["thought longer", "same text again", "late thought", "rest without a delta"])
async def test_what_a_chunk_stream_carries_when_calls_break_off_think_or_salvage(tmp_path, case):
    """- thought longer: a call asked again after it broke off, on a client that keeps its thinking in front of
      the text (Anthropic resets its accumulators on a mid-stream overload), thinks longer the second time: its
      ``accumulated`` grows by more than the delta and is still a new call, set apart.
    - same text again: a call asked again whose first chunk is what the broken one had sent: a new call too.
    - late thought: Gemini puts a thought that comes after the first words in front of the text in
      ``accumulated``: the same call going on (the thought itself goes out -- its clients stream it as text).
    - rest without a delta: the content of a call goes on after its last delta (a client that salvages a rest of
      the stream): the rest follows, not the whole answer again."""
    agent = {"thought longer": ScriptedAgent("chat_agent", "all is well", restarts=True,
                                             retry_thinking="a much longer second round of thinking about it. "),
             "same text again": ScriptedAgent("chat_agent", "all is well", restarts=True, broken="all "),
             "late thought": ScriptedAgent("chat_agent", "all is well", late_thought=True),
             "rest without a delta": ScriptedAgent("chat_agent", "hello world", drops_tail=2)}[case]
    app, _ = build(tmp_path, agent)
    stream = await client(app).chat.completions.create(model="chat_agent", stream=True,
                                                       messages=[{"role": "user", "content": "status?"}])

    assert "".join([c.choices[0].delta.content or "" async for c in stream if c.choices]) == {
        "thought longer": "broken\n\nall is well", "same text again": "all \n\nall is well",
        "late thought": "all hmm is well", "rest without a delta": "hello world"}[case]


@pytest.mark.parametrize("streamed", [False, True], ids=["plain", "stream"])
async def test_an_agent_error_is_an_error(tmp_path, streamed):
    app, _ = build(tmp_path, ScriptedAgent("chat_agent", RuntimeError("model down")))
    async with raw(app) as web:
        answer = await web.post("/responses", json={"model": "chat_agent", "input": "hi", "stream": streamed})

    if not streamed:
        assert answer.status_code == 500 and "model down" in answer.json()["error"]["message"]
    else:
        last = [json.loads(line[6:]) for line in answer.text.splitlines() if line.startswith("data: ")][-1]
        assert last["type"] == "response.failed" and "model down" in last["response"]["error"]["message"]


async def test_a_conversation_takes_one_turn_at_a_time(tmp_path):
    """A second turn meanwhile is a 409 -- streamed too, as a status before the stream starts, not as a
    response.failed after a 200 (which a client takes for a server error and retries)."""
    gate = asyncio.Event()

    async def answer(call: dict) -> str:
        if call["task"] == "slow":
            await gate.wait()
        return "ok"

    agent = ScriptedAgent("chat_agent", answer)
    app, _ = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="hi")
    async with raw(app) as web:
        slow = asyncio.ensure_future(web.post("/responses", json={"model": "chat_agent", "input": "slow",
                                                                 "previous_response_id": first.id}))
        while len(agent.calls) < 2:
            await asyncio.sleep(0.01)
        meanwhile = await web.post("/responses", json={"model": "chat_agent", "input": "meanwhile",
                                                       "previous_response_id": first.id})
        streamed = await web.post("/responses", json={"model": "chat_agent", "input": "meanwhile", "stream": True,
                                                      "previous_response_id": first.id})
        gate.set()
        assert (await slow).status_code == 200

    assert meanwhile.status_code == 409, meanwhile.text
    assert streamed.status_code == 409 and streamed.json()["error"]["type"] == "conflict", streamed.text


async def test_a_conversation_is_busy_until_its_turn_is_settled(tmp_path):
    """The run lets go of the agent's session lock after its last save, and the turn is settled after that. A
    next turn opening in between would take the lock -- and the first turn, finding the conversation run by
    another request, would leave its failed turn in it. So the conversation stays busy until the turn is
    settled."""
    async def answer(call: dict) -> Any:
        return RuntimeError("model down") if call["task"] == "boom" else "noted"

    agent = ScriptedAgent("chat_agent", answer)
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    closing, proceed = asyncio.Event(), asyncio.Event()

    async def the_run_closes_slowly() -> None:  # after it let go of the lock, before the turn is settled
        closing.set()
        await proceed.wait()

    agent.let_go = the_run_closes_slowly
    async with raw(app) as web:
        boom = asyncio.ensure_future(web.post("/responses", json={"model": "chat_agent", "input": "boom",
                                                                 "previous_response_id": first.id}))
        await asyncio.wait_for(closing.wait(), 5)
        assert agent._session_tracker.check_session_locked(agent.calls[0]["session"]) == (False, None), \
            "fixture: the run still holds the lock"
        agent.let_go = None
        meanwhile = await web.post("/responses", json={"model": "chat_agent", "input": "meanwhile",
                                                       "previous_response_id": first.id})
        proceed.set()
        assert (await boom).status_code == 500
    await settled(plugin)

    assert meanwhile.status_code == 409, meanwhile.text
    await client(app).responses.create(model="chat_agent", input="again", previous_response_id=first.id)
    assert agent.calls[-1]["history"] == [("user", "remember 42"), ("assistant", "noted")]


async def inside_a_tool_call(call: dict) -> str:
    """A run that stops only at its token: a cancel inside a tool call becomes a "cancelled" tool result."""
    if call["task"] != "long":
        return "noted"
    for _ in range(150):  # gives up after 3 s, so a broken stop fails instead of hanging
        if call["token"].is_cancelled:
            break
        try:
            await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            pass
    return "late"


@pytest.mark.parametrize("streamed", [False, True], ids=["json", "stream"])
async def test_a_client_that_leaves_stops_the_agent(tmp_path, monkeypatch, streamed):
    """A JSON answer hears nothing of it by itself, a stream hears it from Starlette -- which then cancels the
    whole scope, so the wait for the run must be shielded. Either way: the run's token is cancelled, the answer
    waits until the run has stopped, and the conversation is as it was (the agent saved the stopped turn)."""
    from plugins.openai_api import plugin as plugin_module

    monkeypatch.setattr(plugin_module, "DISCONNECT_POLL", 0.02)
    agent = ScriptedAgent("chat_agent", inside_a_tool_call)
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")

    sent = await leave_during(app, "/responses", {"model": "chat_agent", "input": "long", "stream": streamed,
                                                  "previous_response_id": first.id},
                              leave=lambda: len(agent.calls) == 2)

    assert agent.calls[1]["token"].is_cancelled, "the run was not stopped"
    assert agent.running == 0, "the answer went before the run had stopped"
    assert plugin._busy == set()
    assert plugin.store.latest(agent.calls[0]["session"]) == first.id
    if not streamed:
        assert sent[0]["status"] == 499
    await client(app).responses.create(model="chat_agent", input="again", previous_response_id=first.id)
    assert agent.calls[-1]["history"] == [("user", "remember 42"), ("assistant", "noted")]


async def test_a_stopped_turn_cancels_the_runs_token(tmp_path, monkeypatch):
    """The run's token is cancelled -- what stops a real agent inside a tool call, which swallows a cancel."""
    from plugins.openai_api import turns

    monkeypatch.setattr(turns, "STOP_GRACE", 1.0)
    agent = ScriptedAgent("chat_agent", inside_a_tool_call)
    app, _ = build(tmp_path, agent)
    service = agent._session_service
    turn = turns.AgentTurn(agent, service, user="anonymous", session_id="s1", request_id="oai_leave",
                           persist=False)
    await turn.open([])
    reader = asyncio.ensure_future(_drain(turn.events("long")))
    while not agent.calls:
        await asyncio.sleep(0.01)
    started = asyncio.get_running_loop().time()
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader

    assert agent.calls[0]["token"].is_cancelled
    assert asyncio.get_running_loop().time() - started < 0.9, "the run was not stopped, only waited out"
    await turn.close()


async def test_a_turn_closed_while_its_reader_waits_stops_the_run(tmp_path):
    """A stream closed at one of its yields never reaches events() (nothing is awaited there): close() stops
    the run itself."""
    from plugins.openai_api import turns

    agent = ScriptedAgent("chat_agent", inside_a_tool_call)
    app, _ = build(tmp_path, agent)
    turn = turns.AgentTurn(agent, agent._session_service, user="anonymous", session_id="s1",
                           request_id="oai_closed", persist=False)
    await turn.open([])
    events = turn.events("long")
    await events.__anext__()  # the start event: events() waits at its yield now
    await turn.close()

    assert agent.calls[0]["token"].is_cancelled and agent.running == 0
    await events.aclose()


def stops_late(finish: asyncio.Event) -> Callable[[dict], Any]:
    """A "long" run that stops only once ``finish`` is set -- past STOP_GRACE, however often it is cancelled."""

    async def slow_to_stop(call: dict) -> str:
        if call["task"] != "long":
            return "noted"
        while not call["token"].is_cancelled:
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass  # inside a tool call
        while not finish.is_set():
            try:
                await finish.wait()
            except asyncio.CancelledError:
                pass
        return "late"

    return slow_to_stop


async def test_a_run_that_stops_late_keeps_its_conversation_busy(tmp_path, monkeypatch):
    """Past STOP_GRACE the answer goes, but the conversation is settled -- and free for the next turn -- only
    once the run has stopped: before, the next turn would meet the old run's session lock or its save."""
    from plugins.openai_api import plugin as plugin_module, turns

    monkeypatch.setattr(plugin_module, "DISCONNECT_POLL", 0.02)
    monkeypatch.setattr(turns, "STOP_GRACE", 0.05)
    finish = asyncio.Event()
    agent = ScriptedAgent("chat_agent", stops_late(finish))
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    await leave_during(app, "/responses", {"model": "chat_agent", "input": "long", "previous_response_id": first.id},
                       leave=lambda: len(agent.calls) == 2)
    async with raw(app) as web:
        meanwhile = await web.post("/responses", json={"model": "chat_agent", "input": "meanwhile",
                                                       "previous_response_id": first.id})
    finish.set()
    await settled(plugin)
    again = await client(app).responses.create(model="chat_agent", input="again", previous_response_id=first.id)

    assert meanwhile.status_code == 409, meanwhile.text
    assert agent.calls[-1]["history"] == [("user", "remember 42"), ("assistant", "noted")] and again.output_text


async def test_a_turn_still_stopping_at_shutdown_is_settled_before_the_loop_ends(tmp_path, monkeypatch):
    """Past STOP_GRACE a turn is settled by a task of its own once its run stops. At shutdown the app stops its
    plugins (stop_plugin, in its lifespan) before the loop cancels every task left: the plugin waits for such
    turns there. Cancelled unsettled, the conversation kept the stopped turn."""
    from plugins.openai_api import plugin as plugin_module, turns

    monkeypatch.setattr(plugin_module, "DISCONNECT_POLL", 0.02)
    monkeypatch.setattr(turns, "STOP_GRACE", 0.05)
    finish = asyncio.Event()
    agent = ScriptedAgent("chat_agent", stops_late(finish))
    app, plugin = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    await leave_during(app, "/responses", {"model": "chat_agent", "input": "long", "previous_response_id": first.id},
                       leave=lambda: len(agent.calls) == 2)
    [turn] = plugin._turns
    assert turn._settling is not None and not turn._settling.done(), "fixture: the run stopped within the grace"

    monkeypatch.setattr(turns, "STOP_GRACE", 2.0)  # what the shutdown gives it: longer than the run needs now
    stopping = asyncio.ensure_future(plugin.stop_plugin())
    await asyncio.sleep(0.05)
    finish.set()  # the run stops while the shutdown waits
    await stopping
    turn._settling.cancel()  # what the loop does at its end to every task left
    await asyncio.sleep(0.05)

    assert plugin._busy == set(), "the turn was not settled before the loop's end"
    stored = await agent._session_service.session_manager.load_session("anonymous", agent.calls[0]["session"],
                                                                       bypass_cache=True)
    assert [(m["role"], m["content"]) for m in stored["messages"]] == [("user", "remember 42"), ("assistant", "noted")]


async def _drain(events: Any) -> None:
    async for _ in events:
        pass


# ------------------------------------------------------------------ users

async def test_a_session_of_another_user_is_refused(tmp_path, monkeypatch):
    """SessionService refuses to open it (SessionPermissionError): a 403 the SDK reads, not a plain 500."""
    from agent_system.services.session_manager import SessionPermissionError

    agent = ScriptedAgent("chat_agent")
    app, plugin = build(tmp_path, agent)

    async def not_yours(*_: Any, **__: Any) -> bool:
        raise SessionPermissionError("someone else's")

    monkeypatch.setattr(agent._session_service, "open_for_run", not_yours)
    async with raw(app) as web:
        answer = await web.post("/responses", json={"model": "chat_agent", "input": "hi"})

    assert answer.status_code == 403 and answer.json()["error"]["type"] == "permission_error", answer.text
    assert agent.calls == [] and plugin._busy == set()


async def test_a_model_the_caller_may_not_run_is_not_offered(tmp_path, monkeypatch):
    """The agents' role gate (metadata.min_role): an agent the caller's role may not run is no model for them --
    not listed, and asked for by name it is a model_not_found, as an unknown model; it does not run."""
    from agent_system.auth import database, dependencies

    chat_agent, coder = ScriptedAgent("chat_agent"), ScriptedAgent("coder")
    coder.min_role = "admin"
    app, _ = build(tmp_path, chat_agent, coder)
    app.state.config = SimpleNamespace(auth=SimpleNamespace(enabled=True))
    accounts = {"bob-key": SimpleNamespace(username="bob", role="user", is_active=True),
                "root-key": SimpleNamespace(username="root", role="admin", is_active=True)}

    async def optional_user(request: Any, credentials: Any, x_api_key: Any, db: Any) -> Any:
        return accounts.get(credentials.credentials if credentials else x_api_key)

    monkeypatch.setattr(dependencies, "get_optional_user", optional_user)
    monkeypatch.setattr(database, "get_db", lambda: None)
    async with raw(app, "bob-key") as bob, raw(app, "root-key") as root:
        bobs = [model["id"] for model in (await bob.get("/models")).json()["data"]]
        roots = [model["id"] for model in (await root.get("/models")).json()["data"]]
        refused = [await bob.get("/models/coder"),
                   await bob.post("/responses", json={"model": "coder", "input": "hi"}),
                   await bob.post("/chat/completions",
                                  json={"model": "coder", "messages": [{"role": "user", "content": "hi"}]})]
        ran = await root.post("/responses", json={"model": "coder", "input": "hi"})

    assert bobs == ["chat_agent"] and roots == ["chat_agent", "coder"], (bobs, roots)
    for answer in refused:
        assert answer.status_code == 404 and answer.json()["error"]["code"] == "model_not_found", answer.text
    assert ran.status_code == 200, ran.text
    assert [call["task"] for call in coder.calls] == ["hi"], "coder ran for bob, or not for root"


async def test_a_gated_model_is_answered_exactly_as_an_unknown_one(tmp_path, monkeypatch):
    """Whether a model does not exist or its role gate keeps the caller out, every route answers the same, and
    the routes answer alike: nothing tells the two apart but the name the caller sent."""
    from agent_system.auth import database, dependencies

    coder = ScriptedAgent("coder")
    coder.min_role = "admin"
    app, _ = build(tmp_path, ScriptedAgent("chat_agent"), coder)
    app.state.config = SimpleNamespace(auth=SimpleNamespace(enabled=True))
    bob = SimpleNamespace(username="bob", role="user", is_active=True)

    async def optional_user(request: Any, credentials: Any, x_api_key: Any, db: Any) -> Any:
        return bob

    monkeypatch.setattr(dependencies, "get_optional_user", optional_user)
    monkeypatch.setattr(database, "get_db", lambda: None)

    async def answers(model: str) -> list[list[Any]]:
        async with raw(app, "bob-key") as web:
            return [[r.status_code, r.json()] for r in (
                await web.get(f"/models/{model}"),
                await web.post("/responses", json={"model": model, "input": "hi"}),
                await web.post("/chat/completions",
                               json={"model": model, "messages": [{"role": "user", "content": "hi"}]}))]

    gated, unknown = await answers("coder"), await answers("nope")

    assert json.loads(json.dumps(unknown).replace("'nope'", "'coder'")) == gated, (unknown, gated)
    assert [answer for answer in gated if answer != gated[0]] == [], f"the routes answer differently: {gated}"
    assert gated[0][0] == 404, gated
    assert coder.calls == []


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("path", ["/responses", "/chat/completions"])
@pytest.mark.parametrize("error_type, status, code", [
    ("agent_role_gate", 404, "model_not_found"),
    ("foreign_session", 403, "permission_error"),
])
async def test_a_run_the_agent_refuses_is_an_openai_error_not_a_server_error(tmp_path, path, streamed, error_type,
                                                                              status, code):
    """The agent's backstop refuses a run before it starts (Agent.run_events: its role gate, another user's
    session). Answered as the 500 of a failed run, an SDK retries it; the role gate is a model the caller may not
    run -- unknown to them, as /models has it -- and another user's conversation is theirs."""
    agent = ScriptedAgent("chat_agent")
    agent.refuses = error_type
    app, _ = build(tmp_path, agent)
    body = ({"model": "chat_agent", "input": "hi"} if path == "/responses"
            else {"model": "chat_agent", "messages": [{"role": "user", "content": "hi"}]})
    async with raw(app) as web:
        answer = await web.post(path, json={**body, "stream": streamed})

    if not streamed:
        error = answer.json()["error"]
        assert answer.status_code == status, answer.text
    else:
        last = [json.loads(line[6:]) for line in answer.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"][-1]
        error = last.get("error") or last
    # One code wherever it goes out; the 404 words it as for any model not offered, with its name
    assert error.get("code") == code, answer.text
    if status == 404:
        assert error["message"] == "The model 'chat_agent' does not exist or you do not have access to it", error
    assert agent.calls == [], "the refused run ran"


async def test_a_refused_continued_turn_leaves_the_stored_conversation_untouched(tmp_path):
    """Refused before it started, the run wrote nothing; the put back saved the conversation anyway -- its
    updated_at moved, its variables written as they were read (none became {})."""
    agent = ScriptedAgent("chat_agent")
    app, _ = build(tmp_path, agent)
    first = await client(app).responses.create(model="chat_agent", input="remember 42")
    [path] = [p for p in (tmp_path / "sessions").rglob(f"{agent.calls[0]['session']}.json")]
    before = path.read_bytes()
    agent.refuses = "agent_role_gate"
    async with raw(app) as web:
        refused = await web.post("/responses", json={"model": "chat_agent", "input": "again",
                                                     "previous_response_id": first.id})

    assert refused.status_code == 404, refused.text
    assert path.read_bytes() == before, "the refused turn wrote the stored conversation"


async def test_a_conversation_is_its_user_s(tmp_path, monkeypatch):
    """With auth on, the key names the user: another user's response id is unknown, no key is a 401."""
    from agent_system.auth import database, dependencies

    agent = ScriptedAgent("chat_agent")
    app, _ = build(tmp_path, agent)
    app.state.config = SimpleNamespace(auth=SimpleNamespace(enabled=True))
    keys = {"alice-key": "alice", "bob-key": "bob"}

    async def optional_user(request: Any, credentials: Any, x_api_key: Any, db: Any) -> Any:
        name = keys.get(credentials.credentials if credentials else x_api_key)
        return SimpleNamespace(username=name, is_active=True) if name else None

    monkeypatch.setattr(dependencies, "get_optional_user", optional_user)
    monkeypatch.setattr(database, "get_db", lambda: None)
    first = await client(app, "alice-key").responses.create(model="chat_agent", input="mine")
    continued = {"model": "chat_agent", "input": "again", "previous_response_id": first.id}
    async with raw(app, "bob-key") as bob, raw(app) as nobody, raw(app, "alice-key") as alice:
        by_bob = await bob.post("/responses", json=continued)
        by_nobody = await nobody.post("/responses", json=continued)
        by_alice = await alice.post("/responses", json=continued)

    assert by_bob.status_code == 404, by_bob.text
    assert by_nobody.status_code == 401 and by_nobody.json()["error"]["code"] == "invalid_api_key"
    assert by_alice.status_code == 200, by_alice.text
    assert [call["session"] for call in agent.calls] == [agent.calls[0]["session"]] * 2


def test_the_shipped_config_enables_the_plugin():
    """The activation chain on the resolved config: an entry of the plugin's type, enabled."""
    from agent_system.config.settings import get_tool_server_config, load_settings

    config = get_tool_server_config("openai_api", load_settings())
    assert config is not None and config.enabled and config.type == "openai_api"
