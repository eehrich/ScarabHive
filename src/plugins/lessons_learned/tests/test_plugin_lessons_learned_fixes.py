"""Bugs found while writing the plugin's guide, each with the test that goes red without its fix."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_system.llm.models import ChatMessage
from plugins.lessons_learned.extraction import _parse_extraction_response, extract_lessons_from_conversation
from plugins.lessons_learned.prompt_builder import build_lesson_prompt
from plugins.lessons_learned.server import LessonsLearnedServer, _limit


def make_server(tmp_path, **settings) -> LessonsLearnedServer:
    config = SimpleNamespace(database_path=str(tmp_path / "lessons.db"), **settings)
    return LessonsLearnedServer("lessons_learned", SimpleNamespace(), config)


@pytest.fixture
def server(tmp_path) -> LessonsLearnedServer:
    return make_server(tmp_path)


async def test_two_servers_on_one_database_hand_out_distinct_ids(tmp_path):
    """The API and each agent-cli run a server on the same database; a counter
    cached per process handed out an id the other had taken."""
    first, second = make_server(tmp_path), make_server(tmp_path)

    results = [await first.store_lesson("a", "One", "C"), await second.store_lesson("a", "Two", "C"),
               await first.store_lesson("a", "Three", "C")]

    assert [result.get("lesson_id") for result in results] == ["a_les_001", "a_les_002", "a_les_003"], results


async def test_a_new_lesson_does_not_take_the_id_of_one_moved_away(tmp_path, server):
    """A lesson moved to another agent keeps its id; counting only the agent's
    own rows handed that id out again, and the insert failed. In a database
    from before the counter rows, the rows are all there is to count."""
    await server.store_lesson("writer", "One", "C")
    moved = await server.store_lesson("writer", "Two", "C")
    await server.update_lesson(moved["lesson_id"], agent_name="coder")
    conn = server._get_connection()
    try:
        conn.execute("DELETE FROM lesson_id_counters")
        conn.commit()
    finally:
        conn.close()

    result = await make_server(tmp_path).store_lesson("writer", "Three", "C")

    assert result.get("lesson_id") == "writer_les_003", result


async def test_a_filtered_search_finds_matches_behind_the_closest(server):
    """The status filter ran on the `limit` closest only: drafts closer to the
    query hid the active lesson, and the tool answered "0 found"."""
    for title in ("Pin the data directory in tests", "Pin the data directory in every test",
                  "Tests pin the data directory"):
        await server.store_lesson("a", title, "Relative to the test's tmp_path.")
    active = await server.store_lesson("a", "Keep the storage location configurable", "Never spell it out.",
                                       status="active")

    result = await server.execute({"operation": "search", "query": "pin the data directory in tests", "limit": 2,
                                   "_agent_name": "a", "_session_id": "s"})

    assert [hit["lesson_id"] for hit in result["results"]] == [active["lesson_id"]], result


async def test_the_tool_limit_is_held_to_the_schema(server):
    """The schema promises 1 to 100; SQLite reads a negative LIMIT as none at all."""
    for number in range(3):
        await server.store_lesson("a", f"Lesson {number}", "C")

    listed = await server.execute({"operation": "list", "limit": -1, "_agent_name": "a", "_session_id": "s"})

    assert len(listed["lessons"]) == 1
    assert (_limit({"limit": 500}, 20), _limit({"limit": 0}, 20), _limit({}, 20)) == (100, 1, 20)


@pytest.mark.parametrize("operation", ["store", "teach"])
async def test_a_duplicate_is_not_reported_as_stored(server, operation):
    """The status line said "Lesson stored" where only evidence was added to the existing lesson."""
    call = {"operation": operation, "title": "Pin the data dir", "content": "Relative in tests.",
            "target_agent": "a", "_agent_name": "a", "_session_id": "s"}
    first = await server.execute(call)
    status = MagicMock()
    status.progress, status.end, status.error = AsyncMock(), AsyncMock(), AsyncMock()

    result = await server.execute({**call, "_status": status})

    assert result["dedup_action"] == "confirm"
    assert status.end.call_args.args[0] == f"Not stored: similar to {first['lesson_id']}, evidence added"


def lessons_of(count: int, category: str = "a") -> list[dict]:
    return [{"lesson_id": f"x_les_{i:03d}", "title": f"Lesson {i}", "content": "X" * 200, "category": category,
             "priority": 5, "confidence": 0.5, "tags": []} for i in range(count)]


def test_the_block_fills_its_budget():
    """Each entry's cost counted the category's earlier entries again: half the budget stayed empty."""
    text = build_lesson_prompt(lessons_of(100), max_tokens=1500)

    assert 5500 < len(text) <= 6000, len(text)


async def test_only_the_lessons_shown_count_as_applied(server):
    """The budget leaves lessons out of the block; they were still counted as applied."""
    ids = [(await server.store_lesson("a", f"Lesson {n}", "X" * 1000, status="active", confidence=0.9))["lesson_id"]
           for n in range(3)]
    context = SimpleNamespace(agent_name="a", session_id="s", hook_config={"max_tokens": 150},
                              messages=[ChatMessage(role="user", content="Hello")])

    result = await server.on_pre_llm_call(context)

    counts = [(await server.get_lesson(lesson_id))["application_count"] for lesson_id in ids]
    assert result.metadata["injected_lessons"] == 1 and sorted(counts) == [0, 0, 1], (result.metadata, counts)


def test_one_malformed_extracted_lesson_leaves_the_others():
    """int("high") raised out of the parse and took every lesson of the session with it."""
    answer = json.dumps({"lessons": [{"title": "Bad", "content": "C", "priority": "high"},
                                     {"title": "Good", "content": "C", "priority": 6}]})

    assert [candidate.title for candidate in _parse_extraction_response(answer)] == ["Good"]


async def test_an_extracted_lesson_the_store_refused_is_not_counted_as_created(tmp_path, monkeypatch):
    """A full agent refused every store, and the extraction still reported them created."""
    from agent_system.llm import factory

    server = make_server(tmp_path, max_lessons_per_agent=0)
    llm = MagicMock()
    llm.chat = AsyncMock(return_value=json.dumps({"lessons": [{"title": "Pin the data dir", "content": "Relative."}]}))
    monkeypatch.setattr(factory, "create_llm_from_profile", lambda **kwargs: llm)
    messages = [{"role": "user", "content": "x" * 200}, {"role": "assistant", "content": "y" * 200}]

    result = await extract_lessons_from_conversation(messages, "a", "s-1", server)

    assert (result.created_count, result.skipped_count) == (0, 1)


async def test_a_merge_keeps_the_evidence_of_its_duplicates(server):
    """The duplicates' evidence was deleted while the primary's count became their
    sum: the next confirm counted the rows and the count went down."""
    primary = (await server.store_lesson("a", "One", "C"))["lesson_id"]
    duplicate = (await server.store_lesson("a", "Two", "C"))["lesson_id"]
    await server.add_evidence(primary, "s0", "a")
    for session_id in ("s1", "s2"):
        await server.add_evidence(duplicate, session_id, "a")
    lessons = [await server.get_lesson(primary), await server.get_lesson(duplicate)]

    merged = await server._execute_merge(agent="a", lessons=lessons, merge_decision={"primary_id": primary})
    after_merge = (await server.get_lesson(primary))["evidence_count"]
    confirmed = await server.add_evidence(primary, "s3", "a")

    # one of its own, two moved over, one noting the merge
    assert (merged["total_evidence"], after_merge, confirmed["evidence_count"]) == (4, 4, 5)


# ---------------------------------------------------------------------------- second round (review)


def test_two_servers_storing_at_once_hand_out_distinct_ids(tmp_path):
    """Two processes storing at the same moment: without one write transaction
    around count, id and insert, both read the same last number."""
    import asyncio
    import threading

    servers = [make_server(tmp_path) for _ in range(2)]
    for one in servers:
        one.vector_store.add = lambda **kwargs: None  # only the ids are measured here
    results: list = []
    start = threading.Barrier(len(servers))

    def store_many(one):
        start.wait()
        for number in range(40):
            results.append(asyncio.run(one.store_lesson("a", f"Lesson {number}", "C")))

    threads = [threading.Thread(target=store_many, args=(one,)) for one in servers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    errors = [result["error"] for result in results if "error" in result]
    assert not errors and len({result["lesson_id"] for result in results}) == 80, errors[:3]


async def test_the_id_of_a_deleted_lesson_is_not_handed_out_again(tmp_path, server):
    """An agent still holding the old id would act on another lesson."""
    await server.store_lesson("a", "One", "C")
    gone = await server.store_lesson("a", "Two", "C")
    await server.delete_lesson(gone["lesson_id"])

    result = await make_server(tmp_path).store_lesson("a", "Three", "C")

    assert result["lesson_id"] == "a_les_003"


async def test_a_merge_keeps_the_applications_recorded_meanwhile(server):
    """The count was the sum read before the LLM call; applications the hook recorded meanwhile were lost."""
    primary = (await server.store_lesson("a", "One", "C"))["lesson_id"]
    duplicate = (await server.store_lesson("a", "Two", "C"))["lesson_id"]
    await server.record_application(primary, "s", "a")
    for _ in range(2):
        await server.record_application(duplicate, "s", "a")
    lessons = [await server.get_lesson(primary), await server.get_lesson(duplicate)]
    await server.record_application(primary, "s", "a")  # while the LLM judges

    await server._execute_merge(agent="a", lessons=lessons, merge_decision={"primary_id": primary})

    conn = server._get_connection()
    try:
        rows = conn.execute("SELECT COUNT(*) FROM lesson_applications WHERE lesson_id = ?", (primary,)).fetchone()[0]
    finally:
        conn.close()
    assert ((await server.get_lesson(primary))["application_count"], rows) == (4, 4)


@pytest.fixture
def extraction_llm(monkeypatch):
    from agent_system.llm import factory

    llm = MagicMock()
    llm.chat = AsyncMock(return_value=json.dumps({"lessons": []}))
    monkeypatch.setattr(factory, "create_llm_from_profile", lambda **kwargs: llm)
    return llm


class Requests:
    """A session's requests as the agent loop hands them to the session end hooks.

    Each request's list is the system prompt, the history as persisted (through
    the real SessionTracker, which drops the notes a request appended), the new
    turns, and ``notes`` developer notes hooks append in this request. Built by
    hand as plain appends, the lists had no system prompt and no notes that are
    gone next time, and a position-based reading point passed."""

    def __init__(self, server, notes: int = 2, min_turns: int = 2):
        from agent_system.servers.agent.components.session_tracking import SessionTracker

        self.server, self.notes, self.min_turns = server, notes, min_turns
        self.tracker = SessionTracker()
        self.turn = 0

    async def request(self, words: int = 8, answer: str | None = None, stamped: bool = False) -> None:
        """``answer``: the same assistant text every time; ``stamped``: the messages carry timestamps."""
        from datetime import datetime, timedelta

        from agent_system.llm.message_roles import DEVELOPER

        live = [ChatMessage(role="system", content="You are an agent.")]
        live += list(self.tracker.get_session_messages("s-1"))
        stamp = (lambda n: datetime(2026, 9, 1) + timedelta(seconds=n)) if stamped else (lambda n: None)
        user = "go" if stamped else f"Turn {self.turn:03d} user " + "text " * words
        live.append(ChatMessage(role="user", content=user, timestamp=stamp(self.turn)))
        live += [ChatMessage(role=DEVELOPER, content=f"note {n}", injected_by=f"plugin{n}") for n in range(self.notes)]
        reply = answer or f"Turn {self.turn + 1:03d} assistant " + "text " * words
        live.append(ChatMessage(role="assistant", content=reply, timestamp=stamp(self.turn + 1)))
        self.turn += 2
        self.tracker.set_session_messages("s-1", [message for message in live if message.role != "system"])
        await self.server.on_session_end(SimpleNamespace(agent_name="a", session_id="s-1", messages=live,
                                                         hook_config={"min_turns": self.min_turns}, agent=None))

    def compact(self) -> None:
        """What a compaction leaves: context_summarizer's summary, marked as injected, in place of the history."""
        self.tracker.set_session_messages("s-1", [ChatMessage(role="user", content="Summary of the turns so far",
                                                              injected_by="context_summarizer")])


def turns_read(llm) -> list[str]:
    """Every turn number the extraction LLM was shown, in order, with repeats."""
    import re

    return [number for call in llm.chat.await_args_list
            for number in re.findall(r"Turn (\d{3})", call.kwargs["messages"][-1].content)]


@pytest.mark.parametrize("notes", [0, 1, 2])
async def test_every_turn_is_read_once_across_requests(server, extraction_llm, notes):
    """The list of a request holds the system prompt and this request's notes;
    both are gone from the next. A position logged from it pointed past turns
    no extraction ever read (measured: with 2 notes, 4 of 16)."""
    requests = Requests(server, notes=notes)
    for _ in range(8):
        await requests.request()

    assert turns_read(extraction_llm) == [f"{n:03d}" for n in range(16)]


async def test_a_repeated_answer_does_not_match_the_wrong_message(server, extraction_llm):
    """The same "ok" after every user turn: named by itself alone, the anchor
    matched the newest "ok", and the turns before it were never read."""
    requests = Requests(server)
    for _ in range(4):
        await requests.request(answer="ok, noted")

    assert turns_read(extraction_llm) == ["000", "002", "004", "006"]


async def test_the_same_words_at_other_times_are_other_messages(server, extraction_llm, monkeypatch):
    """"go" and "ok" every request: only their timestamps tell them apart."""
    from plugins.lessons_learned import extraction

    monkeypatch.setattr(extraction, "read_conversation",
                        lambda messages, max_chars=8000: ("x" * 200, [m for m in messages if extraction.written(m)]))
    requests = Requests(server)
    for _ in range(4):
        await requests.request(answer="ok", stamped=True)

    assert extraction_llm.chat.await_count == 4


async def test_extraction_does_not_read_injected_messages(server, extraction_llm):
    """The lessons block of inject_lessons, read back in, confirmed the lessons against themselves."""
    from agent_system.llm.message_roles import DEVELOPER

    conversation = [ChatMessage(role="user" if n % 2 == 0 else "assistant", content=f"Turn {n:03d} " + "text " * 8)
                    for n in range(6)] + [
        ChatMessage(role=DEVELOPER, content="## LESSONS LEARNED injected block", injected_by="lessons_learned"),
        ChatMessage(role="user", content="A notice the loop injected", injected_by="agent_continuation")]

    await server.on_session_end(SimpleNamespace(agent_name="a", session_id="s-1", messages=conversation,
                                                hook_config={}, agent=None))

    shown = extraction_llm.chat.await_args.kwargs["messages"][-1].content
    assert "Turn 005" in shown and "LESSONS LEARNED" not in shown and "notice" not in shown


async def test_after_a_compaction_the_new_turns_are_read(server, extraction_llm):
    """The anchor is compacted away: every written message left is read -- the
    summary is injected and is not -- and none of the turns after it is skipped."""
    requests = Requests(server)
    for _ in range(3):
        await requests.request()
    requests.compact()
    for _ in range(3):
        await requests.request()

    assert turns_read(extraction_llm) == [f"{n:03d}" for n in range(12)]
    assert not any("Summary" in call.kwargs["messages"][-1].content for call in extraction_llm.chat.await_args_list)


async def test_the_part_behind_the_cut_is_read_next_time(server, extraction_llm):
    """The 8000 characters and the reading point disagreed: what was cut off was logged as read."""
    requests = Requests(server, min_turns=2)
    await requests.request(words=1000)  # two messages of 5000 characters: the second is cut
    await requests.request()

    assert turns_read(extraction_llm) == ["000", "001", "002", "003"]


async def test_a_message_longer_than_the_budget_is_shortened_not_dropped(server, extraction_llm):
    """Dropped, nothing was read or logged, and extraction of the session stood still for good."""
    requests = Requests(server, min_turns=2)
    await requests.request(words=2000)  # 10000 characters each: one a reading
    await requests.request()
    await requests.request()

    assert turns_read(extraction_llm) == ["000", "001", "002", "003", "004", "005"]


async def test_one_evidence_per_lesson_session_and_kind(server):
    """A session read twice (a failed duplicate check, an anchor compacted away)
    confirmed the lessons it had confirmed already."""
    lesson = (await server.store_lesson("a", "One", "C"))["lesson_id"]
    await server.add_evidence(lesson, "s-1", "a")
    again = await server.add_evidence(lesson, "s-1", "a")
    await server.add_evidence(lesson, "s-1", "a", "contradict")
    await server.add_evidence(lesson, "s-2", "a")

    assert again["status"] == "evidence_exists"
    assert (await server.get_lesson(lesson))["evidence_count"] == 3


def test_a_second_process_starting_on_an_old_database_loads(tmp_path):
    """Both saw the new column missing; the second ALTER failed on "duplicate
    column name", and the plugin did not load. Two servers in a row add it
    twice, as the race does."""
    import sqlite3

    first = make_server(tmp_path)
    conn = sqlite3.connect(first.db_path)
    conn.execute("ALTER TABLE extraction_log DROP COLUMN anchor")
    conn.commit()
    conn.close()

    make_server(tmp_path)
    make_server(tmp_path)


# ---------------------------------------------------------------------------- fourth round (review)


@pytest.mark.parametrize("answer", ["", "Sorry, I cannot help with that.", '{"lessons": [{"title": "Pin the', None,
                                    "null", '{"result": "nothing to extract"}',
                                    '{"lessons": [{"title": "Pin the data dir", "content": "Relative."}'])
async def test_an_answer_that_could_not_be_read_whole_logs_nothing(server, extraction_llm, answer):
    """Empty, a refusal, cut off, JSON without a lessons list, or mended by
    repair_json: logged as read, those messages were never extracted again."""
    extraction_llm.chat = AsyncMock(return_value=answer)
    requests = Requests(server)
    await requests.request()
    await requests.request()

    assert server.last_anchor("s-1", "a") is None
    assert turns_read(extraction_llm)[:2] == ["000", "001"] and turns_read(extraction_llm)[2:4] == ["000", "001"]


async def test_a_request_reads_until_its_backlog_is_used_up(server, extraction_llm):
    """One reading of 8000 characters a request: a session writing more fell
    further behind at every turn, and a compaction deleted what was not read."""
    requests = Requests(server, min_turns=2)
    for _ in range(6):
        await requests.request(words=1800)  # 9000 characters a message: one message a reading

    assert turns_read(extraction_llm) == [f"{n:03d}" for n in range(11)]  # 011 waits for a second message


async def test_the_session_a_lesson_came_from_does_not_confirm_it(server):
    """Read again, a session vouched for the lesson it had produced itself (0.4 -> 0.47)."""
    lesson = (await server.store_lesson("a", "Pin the data dir", "Relative.", source_type="auto",
                                        source_session="s-1"))["lesson_id"]

    own = await server.add_evidence(lesson, "s-1", "a")
    against = await server.add_evidence(lesson, "s-1", "a", "contradict")
    other = await server.add_evidence(lesson, "s-2", "a")

    assert (own["status"], against["status"], other["status"]) == ("evidence_exists", "evidence_added",
                                                                  "evidence_added")


async def test_without_the_duplicate_check_the_llm_is_not_asked(server, extraction_llm):
    """Nothing was logged while the check failed, and every request sent the same messages again."""
    working = server.vector_store.query

    def broken(**kwargs):
        raise RuntimeError("no embedding model")

    server.vector_store.query = broken
    requests = Requests(server)
    for _ in range(3):
        await requests.request()
    assert extraction_llm.chat.await_count == 0

    server.vector_store.query = working
    await requests.request()
    assert turns_read(extraction_llm) == [f"{n:03d}" for n in range(8)]


async def test_a_merge_keeps_one_evidence_of_a_kind_per_session(server):
    """Both lessons confirmed from one session: moved over, the primary had it
    twice. The merge's own notes, one per lesson merged away, all stay."""
    primary = (await server.store_lesson("a", "One", "C"))["lesson_id"]
    duplicate = (await server.store_lesson("a", "Two", "C2"))["lesson_id"]
    third = (await server.store_lesson("a", "Three", "C3"))["lesson_id"]
    await server.add_evidence(primary, "s-1", "a")
    await server.add_evidence(duplicate, "s-1", "a")
    await server.add_evidence(third, "s-2", "a")
    lessons = [await server.get_lesson(lesson) for lesson in (primary, duplicate, third)]

    merged = await server._execute_merge(agent="a", lessons=lessons, merge_decision={"primary_id": primary})

    conn = server._get_connection()
    try:
        rows = conn.execute("SELECT session_id, evidence_type, COUNT(*) FROM lesson_evidence WHERE lesson_id = ? "
                            "GROUP BY 1, 2", (primary,)).fetchall()
    finally:
        conn.close()
    assert sorted(tuple(row) for row in rows) == [("consolidation", "confirm", 2), ("s-1", "confirm", 1),
                                                  ("s-2", "confirm", 1)]
    assert merged["total_evidence"] == 4


async def test_an_anchor_that_is_not_found_again_stops_the_readings(server, extraction_llm, monkeypatch):
    """Readings follow each other within a request. An anchor the next look does
    not find -- a fingerprint that changed -- read the same messages again and
    again until the time budget was spent."""
    from plugins.lessons_learned import extraction

    import itertools

    ticks = itertools.count()
    monkeypatch.setattr(extraction, "fingerprint", lambda previous, last: str(next(ticks)))  # never the same twice
    await Requests(server).request()

    assert extraction_llm.chat.await_count == 1


# ---------------------------------------------------------------------------- fifth round (review)


@pytest.mark.parametrize("answer, found", [
    ('Here are the lessons:\n{"lessons": [{"title": "A", "content": "B"}]}', 1),
    ('```json\n{"lessons": [{"title": "A", "content": "B"}]}\n```\nLet me know if you need more.', 1),
    ('{"lessons": [{"title": "A", "content": "B"},]}', 1),
    ('No lessons here: {"lessons": []}', 0),
])
def test_text_around_a_whole_answer_leaves_it_whole(answer, found):
    """Counted as not whole, these everyday answers were never logged: the same
    first messages went to the LLM at every request, for good."""
    from plugins.lessons_learned.extraction import _parse

    candidates, whole = _parse(answer)

    assert (len(candidates), whole) == (found, True)


def test_an_answer_cut_off_stays_not_whole():
    from plugins.lessons_learned.extraction import _parse

    assert _parse('{"lessons": [{"title": "A", "content": "B"}, {"title": "C", "cont')[1] is False


def long_session(count: int, chars: int) -> list:
    return [ChatMessage(role="user" if n % 2 == 0 else "assistant", content=f"Turn {n:03d} " + "x" * chars)
            for n in range(count)]


async def test_no_reading_starts_that_would_pass_the_budget(server, extraction_llm, monkeypatch):
    """Checked only before a reading started, one begun at 19.9 s ran to the hook's timeout."""
    import asyncio

    from plugins.lessons_learned import server as server_module

    async def slow(**kwargs):
        await asyncio.sleep(0.4)
        return json.dumps({"lessons": []})

    extraction_llm.chat = AsyncMock(side_effect=slow)
    monkeypatch.setattr(server_module, "EXTRACTION_BUDGET", 1.0)
    context = SimpleNamespace(agent_name="a", session_id="s-1", messages=long_session(10, 9000),
                              hook_config={"min_turns": 1}, agent=None)
    await server.check_duplicate("a", "warm", "up")  # the embedding model loads before the clock runs
    loop = asyncio.get_running_loop()
    began = loop.time()

    result = await server.on_session_end(context)

    assert result.metadata["readings"] == 2 and loop.time() - began <= 1.0


async def test_a_second_request_of_the_session_leaves_the_readings_to_the_first(server, extraction_llm):
    """Two requests of one session in one process read the same messages in lockstep: every LLM call twice."""
    import asyncio

    async def slow(**kwargs):
        await asyncio.sleep(0.2)
        return json.dumps({"lessons": []})

    extraction_llm.chat = AsyncMock(side_effect=slow)
    messages = long_session(6, 3000)

    await asyncio.gather(*(server.on_session_end(SimpleNamespace(
        agent_name="a", session_id="s-1", messages=messages, hook_config={"min_turns": 2}, agent=None))
        for _ in range(2)))

    assert turns_read(extraction_llm) == [f"{n:03d}" for n in range(6)]


async def test_a_reading_goes_on_from_where_another_process_got_to(server, extraction_llm):
    """The anchor is read afresh before each reading: another process may have read on meanwhile."""
    from plugins.lessons_learned.extraction import fingerprint

    messages = long_session(6, 3000)  # two messages a reading
    real_log = server.log_extraction
    others = []

    def log_and_let_another_read_on(*args, **kwargs):
        real_log(*args, **kwargs)
        if not others:  # after the first reading, another process reads messages 2 and 3
            others.append(1)
            real_log("s-1", "a", 0, fingerprint(messages[2], messages[3]), "other")

    server.log_extraction = log_and_let_another_read_on

    await server.on_session_end(SimpleNamespace(agent_name="a", session_id="s-1", messages=messages,
                                                hook_config={"min_turns": 2}, agent=None))

    assert turns_read(extraction_llm) == ["000", "001", "004", "005"]


async def test_a_merge_drops_a_confirm_from_the_kept_lessons_own_session(server):
    """Moved over from a duplicate, it confirmed the kept lesson with the session that made it."""
    primary = (await server.store_lesson("a", "One", "C", source_type="auto", source_session="s-1"))["lesson_id"]
    duplicate = (await server.store_lesson("a", "Two", "C2"))["lesson_id"]
    await server.add_evidence(duplicate, "s-1", "a")
    await server.add_evidence(duplicate, "s-2", "a")
    lessons = [await server.get_lesson(primary), await server.get_lesson(duplicate)]

    await server._execute_merge(agent="a", lessons=lessons, merge_decision={"primary_id": primary})

    conn = server._get_connection()
    try:
        sessions = sorted(row[0] for row in conn.execute(
            "SELECT session_id FROM lesson_evidence WHERE lesson_id = ?", (primary,)))
    finally:
        conn.close()
    assert sessions == ["consolidation", "s-2"]


async def test_evidence_not_added_is_not_counted_as_confirmed(server, extraction_llm):
    """A session read again found its own lesson: nothing was added, yet the totals said "confirmed"."""
    await server.store_lesson("a", "Pin the data dir", "Relative in tests.", source_type="auto", source_session="s-1")
    extraction_llm.chat = AsyncMock(return_value=json.dumps(
        {"lessons": [{"title": "Pin the data dir", "content": "Relative in tests."}]}))

    result = await extract_lessons_from_conversation(long_session(2, 200), "a", "s-1", server)

    assert (result.confirmed_count, result.merged_count, result.skipped_count) == (0, 0, 1)


async def test_the_status_line_of_evidence_not_counted_fits_its_own_session(server):
    lesson = (await server.store_lesson("a", "One", "C", source_type="auto", source_session="s-1"))["lesson_id"]
    status = MagicMock()
    status.progress, status.end, status.error = AsyncMock(), AsyncMock(), AsyncMock()

    await server.execute({"operation": "confirm", "lesson_id": lesson, "_agent_name": "a", "_session_id": "s-1",
                          "_status": status})

    assert status.end.call_args.args[0].startswith("Not counted, this session gave or made it")
