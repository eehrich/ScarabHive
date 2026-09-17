"""What a chat command does to a conversation, on both surfaces.

The terminal cuts the ChatMessage list the agent holds; the browser cuts the
plain dicts a session file stores. One function serves both, so every test
here runs BOTH shapes through it -- a reader that only understands the
object is exactly how the web surface would have got its own copy.
"""
from types import SimpleNamespace

import pytest

from agent_system.chat_actions import (
    context_breakdown,
    measured_context,
    message_role,
    message_text,
    split_off_last_exchange,
    starts_a_turn,
    transcript_markdown,
)


def as_dict(role, content, tool_calls=None):
    message = {"role": role, "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def as_object(role, content, tool_calls=None):
    return SimpleNamespace(role=role, content=content, tool_calls=tool_calls)


#: Both shapes, every test. A test that only ran one of them would leave the
#: other surface's reader unmeasured -- which is the whole reason this module
#: exists.
SHAPES = pytest.mark.parametrize("build", [as_dict, as_object], ids=["dict", "object"])


class TestReadingAMessage:
    @SHAPES
    def test_the_role_and_the_text(self, build):
        assert message_role(build("user", "wie geht das?")) == "user"
        assert message_text(build("user", "wie geht das?")) == "wie geht das?"

    @SHAPES
    def test_a_part_without_text_is_named_by_its_type(self, build):
        """A question asked with a picture has to read as a question, not as
        a blank line -- /history and /undo both show it back."""
        message = build("user", [{"type": "text", "text": "was ist das?"},
                                 {"type": "image_url", "image_url": {"url": "data:x"}}])

        assert message_text(message) == "was ist das? [image_url]"

    @SHAPES
    def test_an_empty_message_is_not_a_turn(self, build):
        assert starts_a_turn(build("user", "   ")) is False
        assert starts_a_turn(build("assistant", "eine antwort")) is False
        assert starts_a_turn(build("user", "eine frage")) is True

    def test_something_unreadable_does_not_raise(self):
        """A record written by an older version, or half-parsed JSON: the
        chat must show what it can, not end the command."""
        assert message_role(object()) == "?"
        assert message_text(object()) == ""


class TestCuttingTheLastExchange:
    def _conversation(self, build):
        return [
            build("user", "erste frage"),
            build("assistant", "erste antwort"),
            build("user", "schreib die routine"),
            build("assistant", "", tool_calls=[
                {"function": {"name": "file_ops_write", "arguments": '{"path":"a.c"}'}}]),
            build("tool", "geschrieben"),
            build("assistant", "fertig"),
        ]

    @SHAPES
    def test_the_question_and_everything_after_it_goes(self, build):
        kept, dropped = split_off_last_exchange(self._conversation(build))

        assert message_text(dropped) == "schreib die routine"
        assert [message_role(m) for m in kept] == ["user", "assistant"]
        assert message_text(kept[0]) == "erste frage"

    @SHAPES
    def test_the_tail_of_the_turn_does_not_stay_behind(self, build):
        """The tool call and its result belong to the question that caused
        them. Left in, the next turn continues an answer nobody can see."""
        kept, _ = split_off_last_exchange(self._conversation(build))

        assert not any(message_role(m) == "tool" for m in kept)
        assert len(kept) == 2

    @SHAPES
    def test_the_question_comes_back_whole(self, build):
        """Not as text: asked with an image it is a multimodal message, and
        /retry has to send those parts again."""
        parts = [{"type": "text", "text": "was ist das?"},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}]

        _, dropped = split_off_last_exchange([build("user", parts),
                                              build("assistant", "ein blitter")])

        content = dropped["content"] if isinstance(dropped, dict) else dropped.content
        assert [part["type"] for part in content] == ["text", "image_url"]

    @SHAPES
    def test_a_conversation_with_no_question_is_left_alone(self, build):
        messages = [build("assistant", "eine begrüßung")]

        kept, dropped = split_off_last_exchange(messages)

        assert dropped is None
        assert len(kept) == 1, "it cut something although there was no turn"

    def test_an_empty_conversation(self):
        assert split_off_last_exchange([]) == ([], None)


class TestWritingItOut:
    @SHAPES
    def test_questions_answers_and_tool_traffic(self, build):
        markdown = transcript_markdown(
            [build("user", "was macht der blitter?"),
             build("assistant", "er kopiert speicher", tool_calls=[
                 {"function": {"name": "file_ops_read",
                               "arguments": '{"path": "blitter.c"}'}}]),
             build("tool", "int main(void)")],
            agent_name="coder", session_id="ab12", llm="fast (m)")

        assert "was macht der blitter?" in markdown
        assert "er kopiert speicher" in markdown
        assert "file_ops_read" in markdown and "blitter.c" in markdown
        assert "int main(void)" in markdown, "the tool RESULT was dropped"
        assert "coder" in markdown and "ab12" in markdown and "fast (m)" in markdown

    @SHAPES
    def test_a_long_tool_result_is_condensed(self, build):
        """A transcript is for reading; one file_ops_read result can be longer
        than the whole conversation around it."""
        markdown = transcript_markdown([build("tool", "x" * 5000)],
                                       agent_name="a", session_id="s")

        assert len(markdown) < 500
        assert "…" in markdown

    def test_without_an_llm_the_header_does_not_trail_a_dash(self):
        markdown = transcript_markdown([], agent_name="a", session_id="s")

        assert "-- LLM" not in markdown
        assert "`s`" in markdown


class TestWhatFillsTheWindow:
    """The split, not the total: "42k of 200k" says the window is filling,
    only the split says WHAT is filling it."""

    @SHAPES
    def test_every_kind_is_counted_on_its_own(self, build):
        out = context_breakdown(
            [build("user", "kurze frage"),
             build("assistant", "kurze antwort"),
             build("tool", "x" * 4000)],
            system_prompt="Du bist ein Agent. " * 40,
            tools=[{"type": "function", "function": {
                "name": "file_ops_read", "description": "read a file",
                "parameters": {"type": "object",
                               "properties": {"path": {"type": "string"}}}}}])
        parts = out["parts"]

        assert parts["tool_results"]["count"] == 1
        assert parts["questions"]["count"] == 1
        assert parts["answers"]["count"] == 1
        assert parts["tools"]["count"] == 1
        assert parts["system_prompt"]["count"] == 1
        # ...and the long tool result really is the biggest of them. This is
        # the whole reason the command exists.
        biggest = max(parts, key=lambda name: parts[name]["tokens"])
        assert biggest == "tool_results", parts

    @SHAPES
    def test_the_total_is_the_parts(self, build):
        out = context_breakdown([build("user", "frage" * 100)],
                                system_prompt="prompt" * 100)

        assert out["total"] == sum(p["tokens"] for p in out["parts"].values())
        assert out["total"] > 0

    @SHAPES
    def test_a_role_nobody_expected_is_still_counted(self, build):
        """Counted under "other", not dropped: a total that silently omits a
        message is worse than one line saying there is something else."""
        out = context_breakdown([build("system", "eine systemzeile" * 50)])

        assert out["parts"]["other"]["count"] == 1
        assert out["total"] == out["parts"]["other"]["tokens"]

    def test_nothing_unexpected_gets_no_line_of_its_own(self):
        """A line reading 0 invites the question what it is."""
        out = context_breakdown([{"role": "user", "content": "frage"}])

        assert "other" not in out["parts"]

    def test_an_empty_session_has_the_tools_and_the_prompt_in_it(self):
        """They sit in the window on every single call, before a word is
        typed -- an agent with 200 tools starts the session two thirds full."""
        out = context_breakdown([], system_prompt="p" * 4000, tools=[
            {"type": "function", "function": {"name": f"t{i}", "description": "d" * 200,
                                              "parameters": {}}} for i in range(50)])

        assert out["parts"]["system_prompt"]["tokens"] > 0
        assert out["parts"]["tools"]["tokens"] > 0
        assert out["parts"]["questions"]["tokens"] == 0


class TestWhatTheProviderCounted:
    def test_it_reads_the_last_snapshot_of_this_session(self):
        asked = []

        class _Tracker:
            def get_latest(self, session_id=None):
                asked.append(session_id)
                return {"context_window": 200000, "prompt_tokens": 42100,
                        "cached_tokens": 31000}

        agent = SimpleNamespace(registry=SimpleNamespace(
            get=lambda name: SimpleNamespace(tracker=_Tracker())))

        assert measured_context(agent, "s1") == {
            "window": 200000, "prompt_tokens": 42100, "cached": 31000,
            "is_stale": False}
        assert asked == ["s1"], "it read the whole store, not this session"

    def test_a_context_rewritten_since_is_flagged(self):
        """The number then describes a conversation that no longer exists."""
        class _Tracker:
            def get_latest(self, session_id=None):
                return {"context_window": 1, "prompt_tokens": 1, "is_stale": True}

        agent = SimpleNamespace(registry=SimpleNamespace(
            get=lambda name: SimpleNamespace(tracker=_Tracker())))

        assert measured_context(agent, "s1")["is_stale"] is True

    def test_a_session_that_never_ran_a_call_has_no_measurement(self):
        """The tracker is there and answers None -- filling that in with a
        plausible window would be a number nobody counted."""
        class _Tracker:
            def get_latest(self, session_id=None):
                return None

        agent = SimpleNamespace(registry=SimpleNamespace(
            get=lambda name: SimpleNamespace(tracker=_Tracker())))

        assert measured_context(agent, "s1") == {}

    def test_without_the_plugin_there_is_no_measurement(self):
        """An estimate is worth showing; an invented measurement is not."""
        agent = SimpleNamespace(registry=None)
        assert measured_context(agent, "s1") == {}

        class _Broken:
            def get(self, name):
                raise RuntimeError("registry gone")

        assert measured_context(SimpleNamespace(registry=_Broken()), "s1") == {}
