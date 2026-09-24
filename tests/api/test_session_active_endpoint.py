"""Which sessions are running: the source, the endpoint, and what a viewer sees.

The sidebar has to mark the sessions an agent is working in, and the chat has to
let a run keep going when the user looks at another session. Both need one
answer the front end cannot work out for itself: what is running right now.
"""

import asyncio
import json

import pytest

from agent_system.api import session_endpoints
from agent_system.api.session_endpoints import list_active_sessions, session_router
from agent_system.llm.message_roles import DEVELOPER
from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.components.session_tracking import SessionTracker
from agent_system.servers.agent.server import Agent
from agent_system.services.background_job_manager import BackgroundJobManager, JobStatus


class _Server:
    """An agent, as far as the union reaches into one: a name and a REAL tracker.

    The tracker is the real class, and its locks are taken the way a run takes
    them. A stand-in with just the one method the union calls was here first and
    hid a second call the union later grew -- the union asked the tracker who
    owns a session, and no test noticed, because the stand-in was written to the
    union of the day.
    """

    def __init__(self, name):
        self.name = name
        self._session_tracker = SessionTracker()

    async def hold(self, session_id, request_id, user_id=None):
        """Run a session, the way a run does: take its lock, write its metadata."""
        assert await self._session_tracker.acquire_session_lock(session_id, request_id), \
            "the fixture could not take the session lock it is built on"
        if user_id is not None:
            self._session_tracker.set_session_metadata(session_id, {"user_id": user_id})


async def _manager_with(job_specs=(), owners=None, owner_users=None):
    """A real manager: real jobs through create_job, a real tracker for the agent side."""
    manager = BackgroundJobManager()
    server = _Server("default_agent")
    for session_id, request_id in (owners or {}).items():
        await server.hold(session_id, request_id, (owner_users or {}).get(session_id))
    manager.set_agent_registry(None, server)
    release = asyncio.Event()
    for spec in job_specs:
        async def runner(_release=release):
            await _release.wait()
            if False:
                yield {}
        job = await manager.create_job(
            request_id=spec["request_id"],
            user_id=spec["user_id"],
            agent_name=spec.get("agent_name", "an_agent"),
            session_id=spec.get("session_id"),
            agent_runner=runner,
        )
        if spec.get("actual_session_id"):
            job.actual_session_id = spec["actual_session_id"]
    return manager, release


async def _stop(manager, release):
    release.set()
    for job in list(manager._jobs.values()):
        job.task.cancel()
        try:
            await job.task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# The source: the two halves of the union
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_session_the_run_created_itself_is_found_by_its_actual_id():
    """The trap this union exists for.

    A /run without a session id creates one, and the job learns it only from the
    start event -- `session_id` stays None, `actual_session_id` gets the name.
    Matching only the first would have shown a fresh session as idle for the
    whole of its first turn, which is exactly when the user is watching.
    """
    manager, release = await _manager_with([
        {"request_id": "r1", "user_id": "u1", "session_id": None, "actual_session_id": "s-new"},
    ])
    try:
        active = await manager.active_sessions()
        assert "s-new" in active, active
        assert active["s-new"]["request_id"] == "r1"
    finally:
        await _stop(manager, release)


@pytest.mark.asyncio
async def test_a_run_without_a_job_is_found_through_the_agents_session_lock():
    """A /run carrying files is no background job, and a sub-agent's session
    never was one. The session locks are the only place those show up."""
    manager, release = await _manager_with(owners={"s-sub": "r-sub"})
    try:
        active = await manager.active_sessions()
        assert active["s-sub"]["request_id"] == "r-sub"
        assert active["s-sub"]["agent_name"] == "default_agent"
        assert active["s-sub"]["user_id"] is None, "a lock knows the request, not who asked"
    finally:
        await _stop(manager, release)


@pytest.mark.asyncio
async def test_the_job_wins_over_the_lock_because_only_it_knows_the_user():
    manager, release = await _manager_with(
        [{"request_id": "r1", "user_id": "u1", "session_id": "s1"}],
        owners={"s1": "r1"},
    )
    try:
        active = await manager.active_sessions()
        assert active["s1"]["user_id"] == "u1"
    finally:
        await _stop(manager, release)


@pytest.mark.asyncio
async def test_a_finished_job_is_not_running():
    manager, release = await _manager_with(
        [{"request_id": "r1", "user_id": "u1", "session_id": "s1"}])
    try:
        manager._jobs["r1"].status = JobStatus.COMPLETED
        assert await manager.active_sessions() == {}
    finally:
        await _stop(manager, release)


@pytest.mark.asyncio
async def test_the_tracker_reports_a_session_only_while_its_lock_is_held():
    """The agent-side half, on the real SessionTracker."""
    tracker = SessionTracker()
    assert tracker.active_sessions() == {}
    assert await tracker.acquire_session_lock("s1", "r1")
    assert tracker.active_sessions() == {"s1": "r1"}
    await tracker.release_session_lock("s1", "r1")
    assert tracker.active_sessions() == {}


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

class _User:
    def __init__(self, username):
        self.username = username


class _Owns:
    """Stands in for the session manager's ownership answer."""

    def __init__(self, *owned):
        self.owned = set(owned)
        self.asked = []

    def belongs_to(self, user_id, session_id):
        self.asked.append((user_id, session_id))
        return session_id in self.owned


@pytest.mark.asyncio
async def test_the_endpoint_answers_only_about_the_ids_it_was_asked_for(monkeypatch):
    manager, release = await _manager_with([
        {"request_id": "r1", "user_id": "u1", "session_id": "asked"},
        {"request_id": "r2", "user_id": "u1", "session_id": "not-asked"},
    ])
    monkeypatch.setattr(session_endpoints, "get_background_job_manager", lambda: manager)
    try:
        answer = await list_active_sessions(ids="asked,idle", current_user=_User("u1"), session_manager=_Owns())
        assert set(answer["active"]) == {"asked"}
        assert answer["active"]["asked"]["request_id"] == "r1"
    finally:
        await _stop(manager, release)


@pytest.mark.asyncio
async def test_another_users_run_is_not_reported(monkeypatch):
    """The process-wide view knows every user's sessions. Asking by id already
    means you could see the row -- but a job carries its user, so that is
    checked too."""
    manager, release = await _manager_with([
        {"request_id": "r1", "user_id": "someone_else", "session_id": "s1"},
    ])
    monkeypatch.setattr(session_endpoints, "get_background_job_manager", lambda: manager)
    try:
        answer = await list_active_sessions(ids="s1", current_user=_User("u1"), session_manager=_Owns())
        assert answer["active"] == {}
    finally:
        await _stop(manager, release)


@pytest.mark.asyncio
async def test_no_ids_asks_nothing_of_the_manager(monkeypatch):
    def _boom():
        raise AssertionError("an empty poll must not walk the registry")
    monkeypatch.setattr(session_endpoints, "get_background_job_manager", _boom)
    assert await list_active_sessions(ids="", current_user=_User("u1"), session_manager=_Owns()) == {"active": {}}


@pytest.mark.asyncio
async def test_the_id_list_is_capped(monkeypatch):
    """Two runs, both the caller's: the one named inside the cap is answered,
    the one past it is not. Asserting an empty answer would have passed with no
    cap at all, because an id nobody runs answers empty either way."""
    limit = session_endpoints._ACTIVE_IDS_LIMIT
    manager, release = await _manager_with([
        {"request_id": "r-in", "user_id": "u1", "session_id": "s0"},
        {"request_id": "r-out", "user_id": "u1", "session_id": f"s{limit + 10}"},
    ])
    monkeypatch.setattr(session_endpoints, "get_background_job_manager", lambda: manager)
    try:
        many = ",".join(f"s{i}" for i in range(limit + 50))
        answer = await list_active_sessions(ids=many, current_user=_User("u1"), session_manager=_Owns())
        assert "s0" in answer["active"], "the first ids must still be answered"
        assert f"s{limit + 10}" not in answer["active"], "the cap did not cut"
    finally:
        await _stop(manager, release)


@pytest.mark.asyncio
async def test_the_route_is_wired_to_the_dependencies_it_reads(monkeypatch):
    """Every other test here calls the function and hands it its arguments, which
    proves nothing about what FastAPI would hand it. Driven through the app, a
    parameter wired to the wrong provider -- the job manager where the session
    manager belongs -- fails here and nowhere else.

    It also fixes the query name and the shape of the answer, which the front end
    and the browser stub are both written against.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agent_system.api.dependencies import get_session_manager
    from agent_system.auth.dependencies import get_optional_user

    manager, release = await _manager_with([
        {"request_id": "r1", "user_id": "ada", "session_id": "s-mine"},
        {"request_id": "r2", "user_id": "someone_else", "session_id": "s-theirs"},
    ], owners={"s-lock": "r-lock"})  # a lock-only run, so the session manager is actually used
    monkeypatch.setattr(session_endpoints, "get_background_job_manager", lambda: manager)
    try:
        app = FastAPI()
        app.include_router(session_router)
        owns = _Owns("s-lock")
        app.dependency_overrides[get_session_manager] = lambda: owns
        app.dependency_overrides[get_optional_user] = lambda: _User("ada")

        with TestClient(app) as client:
            answer = client.get("/api/sessions/active",
                                params={"ids": "s-mine,s-theirs,s-lock,s-idle"})
            assert answer.status_code == 200, answer.text
            active = answer.json()["active"]
            assert set(active) == {"s-mine", "s-lock"}, "another user's run, or a session that runs nowhere"
            assert active["s-mine"] == {"request_id": "r1", "agent_name": "an_agent",
                                        "attachable": True, "answered": False, "elsewhere": False}
            # the lock-only one got there through the injected session manager
            assert owns.asked == [("ada", "s-lock")], \
                "the session manager was never asked -- the parameter is wired elsewhere"

            # and "active" is the endpoint, not a session called that
            assert client.get("/api/sessions/active").json() == {"active": {}}
    finally:
        await _stop(manager, release)


def test_active_is_declared_before_the_session_id_route():
    """FastAPI matches in declaration order: declared after ``/{session_id}``,
    a GET of /api/sessions/active would load a session called "active"."""
    paths = [r.path for r in session_router.routes]
    assert "/api/sessions/active" in paths
    assert paths.index("/api/sessions/active") < paths.index("/api/sessions/{session_id}")


# ---------------------------------------------------------------------------
# What a viewer joining mid-run gets to see
# ---------------------------------------------------------------------------

def _live_run_messages():
    return [
        ChatMessage(role="system", content="You are a careful assistant."),
        ChatMessage(role="user", content="Find the number."),
        ChatMessage(role="system",
                    content='{"type": "archived_ref", "summary": "earlier turns"}'),
        ChatMessage(role="assistant", content="Looking."),
        ChatMessage(role=DEVELOPER, content="Step 9 of 10.", injected_by="agent.step_budget"),
    ]


class _AgentWithLiveState:
    get_live_conversation = Agent.get_live_conversation

    def __init__(self, messages):
        self._messages = messages

    def get_live_messages(self, session_id):
        return self._messages


def test_the_live_view_drops_the_system_prompt_and_the_runs_own_notes():
    """The run's working list is not a transcript: it opens with the rendered
    system prompt and carries the note the run wrote for this one call."""
    view = _AgentWithLiveState(_live_run_messages()).get_live_conversation("s1")
    assert [m.content for m in view] == [
        "Find the number.",
        '{"type": "archived_ref", "summary": "earlier turns"}',
        "Looking.",
    ], "the prompt or a volatile note leaked into the view"


def test_the_live_view_keeps_a_system_message_that_is_conversation():
    """An archived_ref is a compaction breadcrumb -- dropping it would make the
    history look like it starts in the middle."""
    view = _AgentWithLiveState(_live_run_messages()).get_live_conversation("s1")
    assert any('"archived_ref"' in (m.content or "") for m in view)


def test_a_deliberately_placed_developer_note_stays():
    """No marker means nobody rebuilds it: it is a fact of its turn. The rule is
    narrower than message_roles.is_injected_note on purpose -- that one also matches
    a marked USER message, which writer_pipeline_v4 reads back from the transcript."""
    messages = [ChatMessage(role=DEVELOPER, content="Read the style guide."),
                ChatMessage(role="user", content="a scripted follow-up", injected_by="v4.loop")]
    view = _AgentWithLiveState(messages).get_live_conversation("s1")
    assert [m.content for m in view] == ["Read the style guide.", "a scripted follow-up"]


def test_no_live_state_is_not_an_empty_conversation():
    """None and [] mean different things: the caller falls back to the
    persisted list on None, and must not be handed an empty history instead."""
    assert _AgentWithLiveState(None).get_live_conversation("s1") is None


def test_persistence_still_drops_volatile_notes_after_the_rule_was_extracted():
    """The predicate moved out of set_session_messages into is_volatile_note;
    the funnel must behave as before."""
    tracker = SessionTracker()
    tracker.set_session_messages("s1", [
        ChatMessage(role="user", content="keep me"),
        ChatMessage(role=DEVELOPER, content="volatile", injected_by="agent.step_budget"),
        ChatMessage(role=DEVELOPER, content="placed on purpose"),
        ChatMessage(role="user", content="a scripted follow-up", injected_by="agent_continuation"),
    ])
    kept = [m.content for m in tracker.get_session_messages("s1")]
    assert kept == ["keep me", "placed on purpose", "a scripted follow-up"], kept


# ---------------------------------------------------------------------------
# What the endpoint may hand out, and what a client may do with it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_lock_only_run_is_answered_only_for_its_owner(monkeypatch):
    """A session lock carries no user. Without asking whose session it is, this
    would be the one route here that answers about a session it never established
    belongs to the caller -- and the request_id it hands out is what
    POST /api/requests/{id}/cancel acts on."""
    manager, release = await _manager_with(owners={"s-sub": "r-sub"})
    monkeypatch.setattr(session_endpoints, "get_background_job_manager", lambda: manager)
    try:
        mine = _Owns("s-sub")
        answer = await list_active_sessions(ids="s-sub", current_user=_User("u1"), session_manager=mine)
        assert "s-sub" in answer["active"]
        assert mine.asked == [("u1", "s-sub")]

        stranger = _Owns()  # owns nothing
        answer = await list_active_sessions(ids="s-sub", current_user=_User("u2"), session_manager=stranger)
        assert answer["active"] == {}, "another user's session lock was reported"
    finally:
        await _stop(manager, release)


@pytest.mark.asyncio
async def test_only_a_job_backed_run_is_attachable(monkeypatch):
    """GET /events reconnects by finding the JOB. A run without one -- a /run
    carrying files, a sub-agent's run -- answers 409 there, which a client would
    show as a lost connection on a session that is working perfectly well. It is
    still reported, so the sidebar can mark it."""
    manager, release = await _manager_with(
        [{"request_id": "r-job", "user_id": "u1", "session_id": "s-job"}],
        owners={"s-lock": "r-lock"},
    )
    monkeypatch.setattr(session_endpoints, "get_background_job_manager", lambda: manager)
    try:
        answer = await list_active_sessions(ids="s-job,s-lock", current_user=_User("u1"),
                                            session_manager=_Owns("s-lock"))
        assert answer["active"]["s-job"]["attachable"] is True
        assert answer["active"]["s-lock"]["attachable"] is False
        assert answer["active"]["s-lock"]["request_id"] == "r-lock",             "the sidebar still needs it: deleting the session cancels that run"
    finally:
        await _stop(manager, release)


@pytest.mark.asyncio
async def test_a_job_is_not_checked_against_the_session_files(monkeypatch):
    """A job carries its user, so the answer costs no stat call."""
    manager, release = await _manager_with(
        [{"request_id": "r1", "user_id": "u1", "session_id": "s1"}])
    monkeypatch.setattr(session_endpoints, "get_background_job_manager", lambda: manager)
    try:
        owns = _Owns()  # would say "not yours" to everything
        answer = await list_active_sessions(ids="s1", current_user=_User("u1"), session_manager=owns)
        assert "s1" in answer["active"]
        assert owns.asked == []
    finally:
        await _stop(manager, release)


def test_belongs_to_answers_from_the_users_own_directory(tmp_path):
    """The real SessionManager: one session, two users."""
    from agent_system.services.session_manager import SessionManager
    manager = SessionManager(storage_path=str(tmp_path))
    (tmp_path / "ada").mkdir(parents=True)
    (tmp_path / "ada" / "s-1.json").write_text("{}", encoding="utf-8")
    assert manager.belongs_to("ada", "s-1") is True
    assert manager.belongs_to("bob", "s-1") is False
    assert manager.belongs_to("ada", "s-2") is False
    assert manager.belongs_to("ada", "../escape") is False


def test_asking_whether_a_session_is_the_users_creates_nothing(tmp_path):
    """A question is not a reason to write to disk.

    ``belongs_to`` used to go through ``_get_session_path``, which makes the user
    directory on its way to the path. The sidebar's poll asks about every row on
    screen several times a minute, and for a user who has never saved a session
    that meant a directory appearing because someone looked.
    """
    from agent_system.services.session_manager import SessionManager
    manager = SessionManager(storage_path=str(tmp_path))
    assert manager.belongs_to("nobody", "s-1") is False
    assert not (tmp_path / "nobody").exists(), "a read made a directory"


@pytest.mark.asyncio
async def test_a_job_that_ended_any_way_at_all_is_not_running():
    """RUNNING is the one status that counts, not 'anything but COMPLETED'.

    A cancelled or failed job stays in the manager until the cleanup sweep takes
    it. Reported, the sidebar would mark a dead session and the chat would attach
    to a run that is not there.
    """
    for ended in (JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.COMPLETED):
        manager, release = await _manager_with(
            [{"request_id": "r1", "user_id": "u1", "session_id": "s1"}])
        try:
            assert "s1" in await manager.active_sessions(), "fixture: the job never ran"
            manager._jobs["r1"].status = ended
            assert await manager.active_sessions() == {}, f"a {ended.value} job was reported as running"
        finally:
            await _stop(manager, release)


@pytest.mark.asyncio
async def test_the_lock_half_takes_its_owner_from_the_tracker(monkeypatch):
    """The tracker that holds the lock also holds the session's metadata, which
    every run writes -- the API layer, agent-cli, the sub-agent manager. Taking
    the owner from there answers for a session that has not reached disk yet, and
    saves the endpoint a stat per row."""
    manager, release = await _manager_with(owners={"s-sub": "r-sub"},
                                           owner_users={"s-sub": "u1"})
    monkeypatch.setattr(session_endpoints, "get_background_job_manager", lambda: manager)
    try:
        assert (await manager.active_sessions())["s-sub"]["user_id"] == "u1"

        owns = _Owns()  # owns nothing on disk: the answer may not come from there
        answer = await list_active_sessions(ids="s-sub", current_user=_User("u1"), session_manager=owns)
        assert "s-sub" in answer["active"], "a run whose owner the tracker knew was dropped"
        assert owns.asked == [], "the disk was asked although the tracker had the owner"

        answer = await list_active_sessions(ids="s-sub", current_user=_User("u2"), session_manager=owns)
        assert answer["active"] == {}, "another user's run was reported"
        assert owns.asked == [], "a known owner must settle it, either way"
    finally:
        await _stop(manager, release)


@pytest.mark.asyncio
async def test_a_run_past_its_answer_says_so(monkeypatch):
    """``cancel_session`` spares a run that has answered -- cancelling one takes
    its background sub-agents with it. A client that cancels from this answer has
    to spare it too, so the answer has to say which runs those are."""
    manager, release = await _manager_with(
        [{"request_id": "r1", "user_id": "u1", "session_id": "s1"}])
    monkeypatch.setattr(session_endpoints, "get_background_job_manager", lambda: manager)
    try:
        answer = await list_active_sessions(ids="s1", current_user=_User("u1"), session_manager=_Owns())
        assert answer["active"]["s1"]["answered"] is False, "fixture: the run had already answered"

        manager._jobs["r1"].answered = True
        answer = await list_active_sessions(ids="s1", current_user=_User("u1"), session_manager=_Owns())
        assert answer["active"]["s1"]["answered"] is True
        assert answer["active"]["s1"]["request_id"] == "r1", "it is still reported, only marked"
    finally:
        await _stop(manager, release)


# ---------------------------------------------------------------------------
# Joining a run's stream without seeing its turns twice, or missing them
# ---------------------------------------------------------------------------

def _job_with_buffer(emitted, buffered):
    """A job that has sent ``emitted`` items and still holds the last ``buffered``."""
    from agent_system.services.background_job_manager import BackgroundJob
    queue = asyncio.Queue(maxsize=1000)
    for i in range(buffered):
        queue.put_nowait({"n": i})
    return BackgroundJob(request_id="r", user_id="u", agent_name="a", session_id="s",
                         task=None, event_queue=queue, events_emitted=emitted)


def test_the_catch_up_skips_exactly_what_the_client_already_holds():
    """The whole point of the number.

    Dropping the buffer WHOLE also threw away everything the run emitted between
    the session load and this connect -- two round trips -- and if the run's final
    answer fell in there the viewer never saw it and got a "connection lost"
    notice on a run that had finished cleanly.
    """
    # 10 sent, the buffer holds all 10, the client saw the first 6: skip 6, keep 4.
    assert _job_with_buffer(emitted=10, buffered=10).catch_up_skip(6) == 6
    # Nothing seen: nothing skipped -- that is the plain replay.
    assert _job_with_buffer(emitted=10, buffered=10).catch_up_skip(0) == 0
    # Seen everything the run has sent: the whole buffer goes.
    assert _job_with_buffer(emitted=10, buffered=10).catch_up_skip(10) == 10


def test_the_catch_up_counts_from_the_run_not_from_the_buffer():
    """The buffer drops its oldest when it fills, so its first entry is not the
    run's first event. Counting within the buffer would skip from the wrong end."""
    # 100 sent, only the last 10 still held (items 91..100), client saw 95.
    # Items 91..95 are already on its screen -- five of them -- and 96..100 are not.
    assert _job_with_buffer(emitted=100, buffered=10).catch_up_skip(95) == 5


def test_a_client_the_buffer_has_outrun_skips_nothing_and_keeps_its_gap():
    """Seen 80, but the buffer starts at 91: items 81..90 were pushed out while
    nobody was reading. Nothing in the buffer is on the client's screen, so
    nothing is skipped -- and the gap stays, which only the next session load
    closes. Skipping the buffer whole here would widen it instead."""
    assert _job_with_buffer(emitted=100, buffered=10).catch_up_skip(80) == 0
    assert _job_with_buffer(emitted=100, buffered=10).catch_up_skip(3) == 0
    assert _job_with_buffer(emitted=10, buffered=10).catch_up_skip(-5) == 0


@pytest.mark.asyncio
async def test_the_count_keeps_counting_past_the_buffers_size():
    """``events_emitted`` is not the queue's length: the buffer drops its oldest
    when it fills, and the number has to keep meaning 'which event this was'."""
    manager = BackgroundJobManager()
    started, release = asyncio.Event(), asyncio.Event()
    size = BackgroundJobManager.MAX_EVENT_BUFFER

    async def runner():
        for i in range(size + 25):
            yield {"type": "status", "n": i}
        started.set()
        await release.wait()

    job = await manager.create_job(request_id="r1", user_id="u1", agent_name="a",
                                   session_id="s1", agent_runner=runner)
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        assert job.event_queue.qsize() == size, "fixture: the buffer never filled, nothing was dropped"
        assert job.events_emitted == size + 25
        # The buffer holds the last `size` of them, so its first entry is number 26.
        assert job.catch_up_skip(25) == 0, "the client is behind what the buffer still holds"
        assert job.catch_up_skip(30) == 5
    finally:
        release.set()
        job.task.cancel()
        try:
            await job.task
        except BaseException:  # noqa: BLE001
            pass


@pytest.mark.asyncio
async def test_the_end_marker_is_counted_so_the_two_numbers_line_up():
    """The marker is not an event, but it takes a place in the queue. Counted, the
    length and the count mean the same items; uncounted, the arithmetic is one out
    and the catch-up drops one real event too many."""
    manager = BackgroundJobManager()

    async def runner():
        yield {"type": "status", "n": 1}
        yield {"type": "final", "n": 2}

    job = await manager.create_job(request_id="r1", user_id="u1", agent_name="a",
                                   session_id="s1", agent_runner=runner)
    await asyncio.wait_for(job.task, timeout=10)
    assert job.event_queue.qsize() == 3, "fixture: two events and the marker"
    assert job.events_emitted == 3
    # A client that saw both events skips both -- and not the marker, which ends the stream.
    assert job.catch_up_skip(2) == 2


# ---------------------------------------------------------------------------
# GET /api/sessions/{id} while the run is still going
# ---------------------------------------------------------------------------

class _Registry:
    """The tool registry, as far as the endpoint uses it."""

    def __init__(self, **agents):
        self._agents = agents

    def get(self, name):
        return self._agents[name]  # KeyError for an unknown name, as the endpoint expects


async def _agent_running(session_id, request_id, live_messages):
    """A real Agent holding a real session lock with real live state.

    ``Agent.__init__`` builds a whole tool server, so the instance is made
    without it -- everything the endpoint then touches is the real class:
    ``_set_live_messages`` is the writer the run itself uses, the lock is taken
    through ``acquire_session_lock``, and ``get_live_conversation`` is inherited.
    """
    agent = Agent.__new__(Agent)
    agent.name = "an_agent"
    agent._session_tracker = SessionTracker()
    agent._live_state_by_session = {}
    agent._current_messages = None
    agent._live_state_max_sessions = 8
    assert await agent._session_tracker.acquire_session_lock(session_id, request_id), \
        "the fixture could not take the session lock it is built on"
    agent._set_live_messages(session_id, live_messages)
    return agent


async def _saved_session(tmp_path, user_id="ada", agent_name="an_agent"):
    from agent_system.services.session_manager import SessionManager
    manager = SessionManager(storage_path=str(tmp_path))
    session = await manager.create_session(user_id=user_id, title="t", agent_name=agent_name)
    session["messages"] = [
        {"role": "user", "content": "the turn before"},
        {"role": "assistant", "content": "answered that one"},
    ]
    await manager.save_session(session)
    return manager, session["session_id"]


@pytest.mark.asyncio
async def test_a_session_in_flight_is_served_from_the_run_not_from_disk(tmp_path, monkeypatch):
    """What the whole live branch is for.

    The persisted list ends at the last FINISHED turn -- _finalize_request writes
    at the end of a request -- so opening a session mid-run showed the history up
    to there and then live events, with the turn in between missing entirely.
    """
    manager, session_id = await _saved_session(tmp_path)
    persisted = await manager.load_session("ada", session_id)
    assert [m["content"] for m in persisted["messages"]] == ["the turn before", "answered that one"], \
        "fixture: the session was not saved"

    agent = await _agent_running(session_id, "r-1", [
        ChatMessage(role="system", content="You are a careful assistant."),
        ChatMessage(role="user", content="the turn before"),
        ChatMessage(role="assistant", content="answered that one"),
        ChatMessage(role="user", content="the turn in flight"),
        ChatMessage(role=DEVELOPER, content="Step 9 of 10.", injected_by="agent.step_budget"),
    ])
    jobs, release = await _manager_with([{"request_id": "r-1", "user_id": "ada", "session_id": session_id}])
    monkeypatch.setattr(session_endpoints, "get_background_job_manager", lambda: jobs)
    try:
        answer = await session_endpoints.get_session(
            session_id, current_user=_User("ada"), session_manager=manager,
            default_agent=None, tool_registry=_Registry(an_agent=agent))

        assert [m["content"] for m in answer["messages"]] == [
            "the turn before", "answered that one", "the turn in flight",
        ], "the in-flight turn is missing, or the prompt/the run's own note leaked in"
    finally:
        await _stop(jobs, release)


@pytest.mark.asyncio
async def test_a_session_nobody_runs_is_served_from_disk(tmp_path):
    """The lock is the gate. Live state outlives the run it belonged to, so
    reading it whenever it is there would serve a finished run's working list."""
    manager, session_id = await _saved_session(tmp_path)
    agent = await _agent_running(session_id, "r-1", [
        ChatMessage(role="user", content="left over from a run that ended"),
    ])
    await agent._session_tracker.release_session_lock(session_id, "r-1")
    assert agent.get_live_messages(session_id), "fixture: the live state was gone anyway"

    answer = await session_endpoints.get_session(
        session_id, current_user=_User("ada"), session_manager=manager,
        default_agent=None, tool_registry=_Registry(an_agent=agent))
    assert [m["content"] for m in answer["messages"]] == ["the turn before", "answered that one"]


@pytest.mark.asyncio
async def test_the_live_answer_says_how_far_the_runs_stream_has_come(tmp_path, monkeypatch):
    """The number the client hands back as ``seen`` when it joins the stream.

    Without it the reconnect can only replay the buffer whole (the same turns
    twice) or drop it whole (losing whatever the run sent in between).
    """
    manager, session_id = await _saved_session(tmp_path)
    agent = await _agent_running(session_id, "r-1", [ChatMessage(role="user", content="in flight")])
    jobs, release = await _manager_with([{"request_id": "r-1", "user_id": "ada", "session_id": session_id}])
    monkeypatch.setattr(session_endpoints, "get_background_job_manager", lambda: jobs)
    try:
        jobs._jobs["r-1"].events_emitted = 7
        answer = await session_endpoints.get_session(
            session_id, current_user=_User("ada"), session_manager=manager,
            default_agent=None, tool_registry=_Registry(an_agent=agent))
        assert answer["live_events_seen"] == 7
    finally:
        await _stop(jobs, release)


@pytest.mark.asyncio
async def test_what_the_run_sends_after_its_messages_were_read_is_not_counted_as_seen(tmp_path, monkeypatch):
    """The token estimate runs off the loop, after the messages were read; whatever
    the run sends meanwhile is not in them. Counted as seen, the client skips it."""
    from agent_system.services import session_service

    manager, session_id = await _saved_session(tmp_path)
    agent = await _agent_running(session_id, "r-1", [ChatMessage(role="user", content="in flight")])
    jobs, release = await _manager_with([{"request_id": "r-1", "user_id": "ada", "session_id": session_id}])
    monkeypatch.setattr(session_endpoints, "get_background_job_manager", lambda: jobs)

    def the_run_goes_on(messages):
        jobs._jobs["r-1"].events_emitted = 9

    monkeypatch.setattr(session_service, "_add_estimated_tokens", the_run_goes_on)
    try:
        jobs._jobs["r-1"].events_emitted = 7
        answer = await session_endpoints.get_session(
            session_id, current_user=_User("ada"), session_manager=manager,
            default_agent=None, tool_registry=_Registry(an_agent=agent))
        assert jobs._jobs["r-1"].events_emitted == 9, "fixture: the estimate never ran"
        assert answer["live_events_seen"] == 7
    finally:
        await _stop(jobs, release)


@pytest.mark.asyncio
async def test_a_run_with_no_job_names_no_number(tmp_path, monkeypatch):
    """A /run carrying files streams inline and cannot be reconnected to, so
    there is nothing for a number to be used for -- and none to be had."""
    manager, session_id = await _saved_session(tmp_path)
    agent = await _agent_running(session_id, "r-inline", [ChatMessage(role="user", content="in flight")])
    jobs, release = await _manager_with()
    monkeypatch.setattr(session_endpoints, "get_background_job_manager", lambda: jobs)
    try:
        answer = await session_endpoints.get_session(
            session_id, current_user=_User("ada"), session_manager=manager,
            default_agent=None, tool_registry=_Registry(an_agent=agent))
        assert answer["messages"][-1]["content"] == "in flight", "fixture: the live branch did not run"
        assert answer["live_events_seen"] is None
    finally:
        await _stop(jobs, release)


@pytest.mark.asyncio
async def test_the_live_messages_carry_their_token_estimate(tmp_path, monkeypatch):
    """The Session Info panel sums estimated_tokens and counts how many messages
    carried one. The persisted path fills them in; without the same step here the
    figure reads 0 for exactly the sessions worth watching."""
    manager, session_id = await _saved_session(tmp_path)
    agent = await _agent_running(session_id, "r-1", [
        ChatMessage(role="user", content="a message long enough to estimate something for"),
    ])
    jobs, release = await _manager_with([{"request_id": "r-1", "user_id": "ada", "session_id": session_id}])
    monkeypatch.setattr(session_endpoints, "get_background_job_manager", lambda: jobs)
    try:
        answer = await session_endpoints.get_session(
            session_id, current_user=_User("ada"), session_manager=manager,
            default_agent=None, tool_registry=_Registry(an_agent=agent))
        assert answer["messages"][-1]["content"].startswith("a message long enough"), \
            "fixture: the live branch did not run"
        assert answer["messages"][-1]["estimated_tokens"] > 0
    finally:
        await _stop(jobs, release)


# ---------------------------------------------------------------------------
# What opening a session costs: the sub-session tree
# ---------------------------------------------------------------------------

async def _tree_on_disk(tmp_path, user_id="ada"):
    """A real three-level tree through the real SessionManager.

    root
      kid-a         context_vars {"phase": "a"}
        grandkid    context_vars {"phase": "deep"}
      kid-b         context_vars {"phase": "b"}
    """
    from agent_system.services.session_manager import SessionManager
    manager = SessionManager(storage_path=str(tmp_path))

    async def make(sid, parent=None, phase=None):
        session = await manager.create_session(user_id=user_id, title=sid, agent_name="an_agent",
                                               session_id=sid, parent_session_id=parent)
        if phase:
            session["context_vars"] = {"phase": phase}
            await manager.save_session(session)
        return session

    await make("root")
    await make("kid-a", parent="root", phase="a")
    await make("grandkid", parent="kid-a", phase="deep")
    await make("kid-b", parent="root", phase="b")

    # The whole point of the walk is that it never asks for this. `list_sessions`
    # merges the main index with EVERY per-parent sub-index -- 51 MB across 1303
    # files on the user's store, about a second -- and the tree it produced was
    # the same shape, so every assertion about the SHAPE stays green if it comes
    # back. Only this says it is gone.
    async def refuse(*_args, **_kwargs):
        raise AssertionError("list_sessions was called: the expensive path is back")

    manager.list_sessions = refuse
    return manager


@pytest.mark.asyncio
async def test_the_sub_session_tree_is_walked_down_from_the_root(tmp_path):
    """It used to come from list_sessions, which merges the main index with EVERY
    per-parent sub-index -- on the user's store 51 MB across 1303 files, about a
    second, to find a handful of ids below one root. Walking down reads one
    sub-index per parent. Same tree, and the nesting is the point: a flat answer
    would satisfy a test that only counted."""
    manager = await _tree_on_disk(tmp_path)
    tree = await session_endpoints._build_descendants_context_vars(
        session_manager=manager, tool_registry=None, user_id="ada", root_session_id="root")

    by_id = {node["session_id"]: node for node in tree}
    assert set(by_id) == {"kid-a", "kid-b"}, "the direct children are the top level"
    assert by_id["kid-a"]["context_vars"] == {"phase": "a"}
    assert by_id["kid-b"]["children"] == [], "a leaf has no children"
    deeper = by_id["kid-a"]["children"]
    assert [n["session_id"] for n in deeper] == ["grandkid"], "the grandchild hangs under its own parent"
    assert deeper[0]["context_vars"] == {"phase": "deep"}
    assert deeper[0]["agent_name"] == "an_agent"


@pytest.mark.asyncio
async def test_a_sub_index_naming_a_session_further_up_does_not_loop(tmp_path):
    """The tree cannot loop, but a stale sub-index can name a session above it,
    and walking down from there would not end. Guarded rather than trusted: the
    walk now reads those files one at a time instead of one merged picture."""
    manager = await _tree_on_disk(tmp_path)
    # grandkid's sub-index claims kid-a, its own parent -- the shape a stale partition
    # leaves. Pointed at the middle of the tree and not at the root on purpose: a guard
    # that only refused to walk the root again would pass that and still hang here.
    path = manager._get_index_path("ada", "grandkid")
    template = json.loads(manager._get_index_path("ada", "kid-a").read_text(encoding="utf-8"))
    entry = next(iter(template.values()))
    path.write_text(json.dumps({"kid-a": {**entry, "session_id": "kid-a",
                                          "parent_session": {"session_id": "grandkid"}}}),
                    encoding="utf-8")
    # Said out loud: if this ever stops being read back as a child, the test below
    # degenerates into a second copy of the one above without anybody noticing.
    assert [c["session_id"] for c in await manager.list_child_sessions("ada", "grandkid")] == ["kid-a"], \
        "fixture: the stale entry is not read back as a child, so there is no loop to guard against"

    # Measured on which parents the walk asks for, not on how long it takes: without
    # the guard this does not merely take longer, it never ends, and a test that waits
    # for a timeout to prove that hangs the suite instead of failing it. The cap turns
    # the runaway into a stop; the duplicate is what the assertion is about.
    asked: list[str] = []
    real_children = manager.list_child_sessions

    async def counting(user_id, parent_id, **kwargs):
        asked.append(parent_id)
        if len(asked) > 20:
            raise RuntimeError("the walk is going in circles")
        return await real_children(user_id, parent_id)

    manager.list_child_sessions = counting

    tree = await session_endpoints._build_descendants_context_vars(
        session_manager=manager, tool_registry=None, user_id="ada", root_session_id="root")

    assert sorted(asked) == ["grandkid", "kid-a", "kid-b", "root"], \
        f"every parent below the root is walked once, and only once: {asked}"
    assert asked.count("kid-a") == 1, "the walk went back up to a session it had already been through"

    walked = set()

    def collect(nodes):
        for node in nodes:
            walked.add(node["session_id"])
            collect(node["children"])

    collect(tree)
    assert "root" not in walked, "the walk came back to where it started"
    assert {"kid-a", "kid-b", "grandkid"} <= walked, "and it still found everything below"


@pytest.mark.asyncio
async def test_the_sub_session_tree_is_built_only_for_whoever_asks_for_it(tmp_path):
    """The expensive half of this answer, and only the Session Info panel shows it:
    building it opens EVERY session below the one asked for. 28 % of this user's
    sessions have more than two hundred descendants and the largest has 3055 --
    half a second to two seconds, on every open, thrown away by every caller but
    one. So it is off unless asked for, which is the unusual direction for a
    field that used to be there: the alternative was for the chat to ask for
    LESS, and that gives its GET a different URL from the DELETE of the same
    session -- which the delete path relies on being the same."""
    manager = await _tree_on_disk(tmp_path)
    asked = []
    real_children = manager.list_child_sessions

    async def counting(user_id, parent_id, **kwargs):
        asked.append(parent_id)
        # The walk knows the whole subtree, so it has no use for `has_children` --
        # which is one stat per child, hundreds of them for nothing on a wide node.
        assert kwargs.get("annotate_children") is False, \
            "the walk asked for the expand-toggle flag it never reads"
        return await real_children(user_id, parent_id, **kwargs)

    manager.list_child_sessions = counting

    asked_for_it = await session_endpoints.get_session(
        "root", descendants=True, current_user=_User("ada"), session_manager=manager,
        default_agent=None, tool_registry=None)
    assert {n["session_id"] for n in asked_for_it["descendants_context_vars"]} == {"kid-a", "kid-b"}, \
        "fixture: asking for the tree did not produce one"
    assert asked, "fixture: nothing was read for the tree"

    asked.clear()
    plain = await session_endpoints.get_session(
        "root", current_user=_User("ada"), session_manager=manager,
        default_agent=None, tool_registry=None)
    assert plain["descendants_context_vars"] == [], "the default hands out the expensive half again"
    assert asked == [], "and it was built anyway"
    # the session itself still comes: the same fields the chat renders from
    assert plain["session_id"] == "root" and plain["agent_name"] == "an_agent"
    assert "messages" in plain


@pytest.mark.asyncio
async def test_a_descendants_vars_are_merged_with_what_its_run_holds(tmp_path):
    """The reason this tree exists at all: a sub-agent sets context_vars during its
    run and they are persisted only at the next checkpoint. Every other test here
    passes tool_registry=None, which skips the merge entirely -- so without this,
    handing out the persisted vars alone would pass them all."""
    manager = await _tree_on_disk(tmp_path)
    agent = await _agent_running("kid-a", "r-1", [])
    agent._session_tracker.set_session_template_vars("kid-a", {"phase": "live", "chapter": 7})

    tree = await session_endpoints._build_descendants_context_vars(
        session_manager=manager, tool_registry=_Registry(an_agent=agent),
        user_id="ada", root_session_id="root")

    by_id = {node["session_id"]: node for node in tree}
    assert by_id["kid-a"]["context_vars"] == {"phase": "live", "chapter": 7}, \
        "the run's vars did not reach the answer, or did not win over the saved ones"
    assert by_id["kid-b"]["context_vars"] == {"phase": "b"}, \
        "a session the run holds nothing for keeps what is on disk"


@pytest.mark.asyncio
async def test_no_more_than_sixteen_session_files_are_read_at_once(tmp_path):
    """A session file can be megabytes and a tree here has hundreds of nodes. The
    reads run together -- that is what makes this fast -- but one panel's request
    must not decide how much of the process's memory is JSON. The number is read
    from the module rather than written down twice."""
    from agent_system.services.session_manager import SessionManager
    manager = SessionManager(storage_path=str(tmp_path))
    await manager.create_session(user_id="ada", title="root", agent_name="an_agent", session_id="root")
    for i in range(40):  # more than the cap, or "bounded" and "there was no more" look the same
        await manager.create_session(user_id="ada", title=f"k{i}", agent_name="an_agent",
                                     session_id=f"kid-{i:02d}", parent_session_id="root")

    live = 0
    high_water = 0

    def counted(real):
        """Both kinds of read are bounded: the session files AND the sub-indexes.
        The biggest single file this walk opens on the user's store is a 943 KB
        sub-index, so counting only the session files would measure the smaller
        half and call the bound proved."""
        async def counting(*args, **kwargs):
            nonlocal live, high_water
            live += 1
            high_water = max(high_water, live)
            try:
                await asyncio.sleep(0)  # give the others a chance to pile up
                return await real(*args, **kwargs)
            finally:
                live -= 1
        return counting

    manager.load_session = counted(manager.load_session)
    manager.list_child_sessions = counted(manager.list_child_sessions)

    tree = await session_endpoints._build_descendants_context_vars(
        session_manager=manager, tool_registry=None, user_id="ada", root_session_id="root")

    assert len(tree) == 40, "fixture: the walk did not find the children it was meant to bound"
    assert high_water > 1, "fixture: the reads never overlapped, so nothing was bounded"
    assert high_water <= session_endpoints._DESCENDANT_READS_AT_ONCE


@pytest.mark.asyncio
async def test_an_unreadable_branch_costs_the_branch_not_the_session(tmp_path):
    """A sub-index that cannot be read is a branch nobody can show. Opening the
    session must still work -- the messages are what the viewer came for."""
    manager = await _tree_on_disk(tmp_path)
    real_children = manager.list_child_sessions

    async def sometimes_broken(user_id, parent_id, **kwargs):
        if parent_id == "kid-a":
            raise OSError("the sub-index is unreadable")
        return await real_children(user_id, parent_id, **kwargs)

    manager.list_child_sessions = sometimes_broken

    answer = await session_endpoints.get_session(
        "root", descendants=True, current_user=_User("ada"), session_manager=manager,
        default_agent=None, tool_registry=None)

    by_id = {n["session_id"]: n for n in answer["descendants_context_vars"]}
    assert set(by_id) == {"kid-a", "kid-b"}, "the readable siblings went with it"
    assert by_id["kid-a"]["children"] == [], "the branch below the unreadable index is empty"
    assert "messages" in answer, "the session itself did not survive a branch it could not read"


@pytest.mark.asyncio
async def test_the_query_that_asks_for_the_tree_is_the_one_the_panel_sends(tmp_path, monkeypatch):
    """Driven through the app, because the panel sends a QUERY STRING and every
    other test here hands the flag over as a keyword -- which proves nothing about
    what FastAPI would parse. Rename the parameter or change how it coerces and
    the panel silently shows no sub-sessions while every test stays green."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agent_system.api.dependencies import get_session_manager, get_agent_optional, get_tool_registry
    from agent_system.auth.dependencies import get_optional_user

    manager = await _tree_on_disk(tmp_path)
    app = FastAPI()
    app.include_router(session_router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    app.dependency_overrides[get_optional_user] = lambda: _User("ada")
    app.dependency_overrides[get_agent_optional] = lambda: None
    app.dependency_overrides[get_tool_registry] = lambda: None

    with TestClient(app) as client:
        # exactly what static/js/panels/session.js puts in the URL
        asked = client.get("/api/sessions/root", params={"descendants": "true"})
        assert asked.status_code == 200, asked.text
        assert {n["session_id"] for n in asked.json()["descendants_context_vars"]} == {"kid-a", "kid-b"}

        plain = client.get("/api/sessions/root")
        assert plain.status_code == 200, plain.text
        assert plain.json()["descendants_context_vars"] == []
