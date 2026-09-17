"""What a chat command does to a conversation, on both surfaces.

The terminal cuts the ChatMessage list the agent holds; the browser cuts the
plain dicts a session file stores. One function serves both, so every test
here runs BOTH shapes through it -- a reader that only understands the
object is exactly how the web surface would have got its own copy.
"""
from types import SimpleNamespace

import pytest

from agent_system.chat_actions import (
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
