"""message_with_attachments: the one path from sorted attachments to a message.

The HTTP API, agent-cli, agent-run and the chat each checked the model and
built the message by hand; this is the shared step, each surface keeps only
its own answer to AttachmentRejected.
"""
from types import SimpleNamespace

import pytest
from PIL import Image

from agent_system.llm.models import ChatMessage
from agent_system.utils import multimodal_processor as mp
from agent_system.utils.multimodal_processor import AttachmentRejected, message_with_attachments

AGENT = SimpleNamespace(llm=SimpleNamespace(model="agent-default-model"))


def test_without_attachments_the_text_goes_as_it_is():
    assert message_with_attachments("hello", {"image": [], "audio": [], "text": []}, None, AGENT) == "hello"
    assert message_with_attachments("hello", {}, None, AGENT) == "hello"


def test_the_model_the_run_uses_is_asked_and_can_refuse(monkeypatch, tmp_path):
    asked = []

    def ensure(model, *, images=0, audio=0, video=0):
        asked.append((model, images, audio))
        return f"{model} cannot see images"
    monkeypatch.setattr("agent_system.llm.capabilities.ensure_model_supports", ensure)
    built = []
    monkeypatch.setattr(mp, "create_multimodal_message_extended", lambda **k: built.append(k))

    with pytest.raises(AttachmentRejected, match="override-model cannot see images"):
        message_with_attachments("look", {"image": ["a.png", "b.png"], "audio": ["c.mp3"]},
                                 SimpleNamespace(model="override-model"), AGENT)

    assert asked == [("override-model", 2, 1)], "not the override's model, or wrong counts"
    assert not built, "a refused attachment was still processed"


def test_a_file_that_cannot_be_read_is_rejected_not_raised_raw(tmp_path):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")

    with pytest.raises(AttachmentRejected, match="Cannot open image"):
        message_with_attachments("look", {"image": [str(broken)]}, None, SimpleNamespace(llm=None))


def test_every_kind_reaches_the_message(tmp_path):
    image = tmp_path / "pic.png"
    Image.new("RGB", (4, 4)).save(image)
    notes = tmp_path / "notes.md"
    notes.write_text("the notes body", encoding="utf-8")

    message = message_with_attachments("read both", {"image": [str(image)], "text": [str(notes)]},
                                       None, SimpleNamespace(llm=None))

    assert isinstance(message, ChatMessage) and message.role == "user"
    kinds = [type(part).__name__ for part in message.content]
    assert "ImageContent" in kinds, kinds
    assert any("the notes body" in (getattr(part, "content", "") or getattr(part, "text", "") or "")
               for part in message.content), "the text file never reached the message"
    assert any(getattr(part, "text", None) == "read both" for part in message.content), "the task text is gone"
