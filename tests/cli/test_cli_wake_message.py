"""``wake_message`` -- what a woken run says, and who it says it as.

The sentence is the same either way; the ROLE is the whole difference. Two
things read this message that never see each other: the funnel that decides
what a session keeps, and the searches that walk back for the last thing a
person wrote. Both are asserted here against the real ones, because the fields
they read (``role``, ``injected_by``) are two lines in one constructor and a
change to either is silent.
"""
from __future__ import annotations

from agent_system.cli_utils.agent_runner import wake_message
from agent_system.core.session_presence import WAKE_TASK
from agent_system.llm.message_roles import DEVELOPER
from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.components.session_tracking import SessionTracker


def test_the_run_is_speaking_not_a_person():
    """A woken run's only input is this sentence. As a ``user`` turn nothing
    told it apart from something somebody typed -- and the first thing the
    agent has to report is which of the two happened."""
    message = wake_message()

    assert message.content == WAKE_TASK
    assert message.role == DEVELOPER


def test_it_stays_in_the_transcript():
    """A developer note CARRYING ``injected_by`` is bound to the one call it
    was built for, and the save funnel drops it. This one is the reason the
    turn exists at all: resumed without it, an answer beginning "woken" stands
    over nothing."""
    volatile = ChatMessage(role=DEVELOPER, content="2 steps left",
                           injected_by="agent.step_budget")
    tracker = SessionTracker()

    tracker.set_session_messages("s1", [wake_message(), volatile])

    kept = [msg.content for msg in tracker.get_session_messages("s1")]
    assert kept == [WAKE_TASK], "the marker would have taken the wake with it"


async def test_it_survives_the_session_file(tmp_path):
    """The role has to come back OFF DISK. A session is stored as JSON and
    rebuilt message by message (`ChatMessage(**msg)`); a role the writer or
    the validator did not know would be dropped on the way, and every resumed
    transcript would read as if a person had typed the wake."""
    from agent_system.services.session_manager import SessionManager

    manager = SessionManager(storage_path=str(tmp_path))
    await manager.create_session(user_id="u1", session_id="s1", agent_name="a",
                                 llm_profile="p")
    session = await manager.load_session("u1", "s1")
    session["messages"] = [wake_message().model_dump(mode="json")]
    await manager.save_session(session)
    manager.clear_cache()

    reloaded = await manager.load_session("u1", "s1")
    message = ChatMessage(**reloaded["messages"][0])
    assert message.role == DEVELOPER
    assert message.content == WAKE_TASK
