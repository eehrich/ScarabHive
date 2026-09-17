"""Tests for the media_ops plugin — both directions.

Loading: real files (PNG written by PIL, WAV written by the stdlib wave
module) go through the REAL server, dispatched through the REAL schema.yaml
tool names, and the returned ``_multimodal_content`` is fed to the REAL
``MultimodalToolContent`` model — the same line tool_execution.py runs.

Saving: real ``ChatMessage`` objects carrying media in all four inline shapes
plus a file-path item, read back out and compared byte for byte.

The security tests deliberately point at files that EXIST outside the sandbox,
so a green result cannot come from "file not found".
"""
from __future__ import annotations

import base64
import re
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from PIL import Image

from agent_system.config.models import ToolServerConfig
from agent_system.llm.models import (
    ChatMessage,
    ImageContent,
    ImageSource,
    MultimodalToolContent,
    TextContent,
)
from plugins.media_ops.server import MEDIA_TYPES, MediaOpsServer

REPO_ROOT = Path(__file__).resolve().parents[4]
PLUGINS_YAML = REPO_ROOT / "config" / "plugins.yaml"

MAX_MB = 1  # binding limit for the oversize test — deliberately small


# ── Fixtures ──────────────────────────────────────────────────────────────

def _png(path: Path, color: tuple[int, int, int], side: int = 8) -> bytes:
    Image.new("RGB", (side, side), color).save(path)
    return path.read_bytes()


def _wav(path: Path) -> bytes:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x01" * 800)
    return path.read_bytes()


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    root.mkdir()
    return root


@pytest.fixture
def image_file(media_root: Path) -> Path:
    _png(media_root / "shot.png", (10, 120, 200))
    return media_root / "shot.png"


@pytest.fixture
def audio_file(media_root: Path) -> Path:
    _wav(media_root / "clip.wav")
    return media_root / "clip.wav"


@pytest.fixture
def outside_file(tmp_path: Path) -> Path:
    """A REAL file outside the sandbox — so rejection can't be 'not found'."""
    outside = tmp_path / "outside"
    outside.mkdir()
    _png(outside / "secret.png", (200, 10, 10))
    return outside / "secret.png"


@pytest.fixture
def server(media_root: Path) -> MediaOpsServer:
    cfg = ToolServerConfig(type="media_ops", enabled=True, config={
        "allowed_directories": [str(media_root)],
        "max_file_size_mb": MAX_MB,
    })
    return MediaOpsServer("media_ops", SimpleNamespace(), cfg)


class _Agent:
    """Agent with live messages — the path a tool call really takes."""

    def __init__(self, session_id: str, messages: list) -> None:
        self._session_id = session_id
        self._messages = messages

    def get_live_messages(self, session_id):
        return self._messages if session_id == self._session_id else None


class _TrackerOnlyAgent:
    """No get_live_messages — exercises the session-tracker fallback."""

    def __init__(self, messages: list) -> None:
        self._session_tracker = SimpleNamespace(
            get_session_messages=lambda session_id: messages)


@pytest.fixture
def context_media(tmp_path: Path, audio_file: Path):
    """Three inline images (one per wire format) + one file-backed audio item.

    Distinct SIZES per format, so every entry can be traced back to the shape
    it came from — same-size payloads made the byte comparison pick whichever
    entry came first.
    """
    src = tmp_path / "src"
    src.mkdir()
    anthropic_bytes = _png(src / "a.png", (1, 2, 3), side=8)
    openai_bytes = _png(src / "b.png", (4, 5, 6), side=64)
    gemini_bytes = _png(src / "c.png", (7, 8, 9), side=256)
    video_bytes = b"\x00\x00\x00\x18ftypmp42" + b"\xab" * 500
    payloads = [anthropic_bytes, openai_bytes, gemini_bytes, video_bytes]
    assert len({len(p) for p in payloads}) == 4, \
        "fixture payloads must be distinguishable by size"
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731

    messages = [
        ChatMessage(role="user", content=[
            TextContent(type="text", text="have a look"),
            ImageContent(
                type="image", name="anthropic.png",
                source=ImageSource(type="base64", media_type="image/png",
                                   data=b64(anthropic_bytes))),
        ]),
        ChatMessage(role="user", content=[
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64(openai_bytes)}"}},
        ]),
        ChatMessage(role="user", content=[
            {"type": "image",
             "inline_data": {"mime_type": "image/png", "data": b64(gemini_bytes)}},
        ]),
        # Raw bytes instead of base64 — the shape a Gemini native part carries.
        ChatMessage(role="user", content=[
            {"type": "video",
             "inline_data": {"mime_type": "video/mp4", "data": video_bytes}},
        ]),
        ChatMessage(role="tool", tool_call_id="c1", name="audio_ops_load",
                    content="{\"status\": \"success\"}",
                    multimodal_content=[MultimodalToolContent(
                        type="audio", path=str(audio_file),
                        mime_type="audio/wav")]),
    ]
    return SimpleNamespace(messages=messages, anthropic=anthropic_bytes,
                           openai=openai_bytes, gemini=gemini_bytes,
                           video=video_bytes)


def _params(context_media, **kw):
    return {"_agent": _Agent("s1", context_media.messages),
            "_session_id": "s1", **kw}


# ══ Loading ═══════════════════════════════════════════════════════════════

class TestLoad:
    async def test_image_is_attached_as_multimodal_content(self, server, image_file):
        res = await server.call("media_ops_load", {"path": str(image_file)})

        assert res["status"] == "success", res
        items = res["_multimodal_content"]
        assert len(items) == 1
        # The exact line tool_execution.py runs on the tool result.
        attached = MultimodalToolContent(**items[0])
        assert attached.type == "image"
        assert attached.mime_type == "image/png"
        assert Path(attached.path).read_bytes() == image_file.read_bytes()

    async def test_audio_is_attached_as_multimodal_content(self, server, audio_file):
        res = await server.call("media_ops_load", {"path": str(audio_file)})

        assert res["status"] == "success", res
        attached = MultimodalToolContent(**res["_multimodal_content"][0])
        assert attached.type == "audio"
        assert attached.mime_type == "audio/wav"
        assert Path(attached.path).read_bytes() == audio_file.read_bytes()

    async def test_relative_path_resolves_against_the_project_root(
            self, server, media_root, image_file):
        # Production resolves relative paths against cwd; pin the sandbox's
        # base to the fixture so the test doesn't depend on where pytest was
        # started. The base lives IN the sandbox — an attribute set beside it
        # would have no effect, and the test would not notice.
        server.sandbox = replace(server.sandbox, base=media_root.parent.resolve())
        res = await server.call("media_ops_load",
                                {"path": f"media/{image_file.name}"})
        assert res["status"] == "success", res
        assert Path(res["path"]) == image_file.resolve()


class TestSandbox:
    async def test_absolute_path_outside_the_roots_is_refused(
            self, server, outside_file):
        assert outside_file.is_file(), "fixture file missing — test would be vacuous"

        res = await server.call("media_ops_load", {"path": str(outside_file)})

        assert res["status"] == "error"
        assert res["error_type"] == "PermissionError", res
        assert "_multimodal_content" not in res

    async def test_dotdot_traversal_is_refused(self, server, media_root, outside_file):
        escape = media_root / ".." / "outside" / outside_file.name
        assert escape.resolve().is_file(), "fixture: traversal target must exist"

        res = await server.call("media_ops_load", {"path": str(escape)})

        assert res["status"] == "error"
        assert res["error_type"] == "PermissionError", res

    async def test_symlink_out_of_the_sandbox_is_refused(
            self, server, media_root, outside_file):
        link = media_root / "link.png"
        try:
            link.symlink_to(outside_file)
        except (OSError, NotImplementedError) as e:  # Windows w/o developer mode
            pytest.skip(f"cannot create symlinks here: {e}")
        assert link.resolve() == outside_file.resolve(), "fixture: link is broken"

        res = await server.call("media_ops_load", {"path": str(link)})

        assert res["status"] == "error"
        assert res["error_type"] == "PermissionError", res

    async def test_save_target_outside_the_roots_is_refused(
            self, server, tmp_path, context_media):
        listed = await server.call("media_ops_list_context", _params(context_media))
        media_id = next(m["id"] for m in listed["media"] if m["inline"])
        target = tmp_path / "escaped.png"

        res = await server.call("media_ops_save",
                                _params(context_media, id=media_id, path=str(target)))

        assert res["status"] == "error"
        assert res["error_type"] == "PermissionError", res
        assert not target.exists(), "refused save still wrote the file"


class TestRefusedInput:
    async def test_file_above_the_size_limit_is_refused_with_its_size(
            self, server, media_root):
        big = media_root / "big.png"
        big.write_bytes(b"\0" * int(1.5 * 1024 * 1024))
        assert big.stat().st_size / (1024 * 1024) > MAX_MB, "fixture below the limit"

        res = await server.call("media_ops_load", {"path": str(big)})

        assert res["status"] == "error"
        assert res["error_type"] == "FileTooLarge", res
        assert "1.5 MB" in res["error"], res["error"]

    async def test_unsupported_extension_is_refused_and_lists_what_works(
            self, server, media_root):
        notes = media_root / "notes.txt"
        notes.write_text("not media", encoding="utf-8")

        res = await server.call("media_ops_load", {"path": str(notes)})

        assert res["status"] == "error"
        assert res["error_type"] == "UnsupportedMediaType", res
        missing = [ext for ext in MEDIA_TYPES if ext not in res["error"]]
        assert not missing, f"error message hides supported extensions: {missing}"

    async def test_missing_file_inside_the_sandbox_says_so(self, server, media_root):
        res = await server.call("media_ops_load",
                                {"path": str(media_root / "gone.png")})
        assert res["error_type"] == "FileNotFoundError", res


# ══ Saving ════════════════════════════════════════════════════════════════

class TestListContext:
    async def test_finds_media_in_both_storage_places(self, server, context_media,
                                                      audio_file):
        res = await server.call("media_ops_list_context", _params(context_media))

        assert res["status"] == "success", res
        media = res["media"]
        assert {m["origin"] for m in media} == {"content", "multimodal_content"}, media

        inline_sizes = sorted(m["size_bytes"] for m in media if m["inline"])
        assert inline_sizes == sorted(len(b) for b in (
            context_media.anthropic, context_media.openai, context_media.gemini,
            context_media.video)), "not every inline wire format was decoded"
        assert {m["type"] for m in media} == {"image", "audio", "video"}

        on_disk = [m for m in media if not m["inline"]]
        assert len(on_disk) == 1
        assert on_disk[0]["path"] == str(audio_file)
        assert on_disk[0]["type"] == "audio"
        assert on_disk[0]["size_bytes"] == audio_file.stat().st_size

    async def test_ids_are_stable_across_calls(self, server, context_media):
        first = await server.call("media_ops_list_context", _params(context_media))
        second = await server.call("media_ops_list_context", _params(context_media))

        ids = [m["id"] for m in first["media"]]
        assert ids and all(ids), "no ids handed out — save could not address anything"
        assert ids == [m["id"] for m in second["media"]]

    async def test_reads_from_the_session_tracker_when_there_are_no_live_messages(
            self, server, context_media):
        agent = _TrackerOnlyAgent(context_media.messages)

        res = await server.call("media_ops_list_context",
                                {"_agent": agent, "_session_id": "s1"})

        assert res["status"] == "success", res
        assert res["count"] == 5, res

    async def test_without_session_context_it_errors_instead_of_reporting_nothing(
            self, server):
        res = await server.call("media_ops_list_context", {})

        assert res["status"] == "error"
        assert res["error_type"] == "SessionContextMissing", res


class TestSave:
    async def _first_inline(self, server, context_media, wanted: bytes):
        listed = await server.call("media_ops_list_context", _params(context_media))
        entry = next(m for m in listed["media"]
                     if m["inline"] and m["size_bytes"] == len(wanted))
        return entry["id"]

    @pytest.mark.parametrize("payload_attr,name", [("openai", "from_context.png"),
                                                   ("video", "clip.mp4")])
    async def test_inline_media_is_written_byte_for_byte(
            self, server, media_root, context_media, payload_attr, name):
        payload = getattr(context_media, payload_attr)
        media_id = await self._first_inline(server, context_media, payload)
        target = media_root / "saved" / name

        res = await server.call("media_ops_save",
                                _params(context_media, id=media_id,
                                        path=str(target)))

        assert res["status"] == "success", res
        assert res["copied"] is True
        assert Path(res["path"]).read_bytes() == payload

    async def test_media_already_on_disk_is_not_copied(
            self, server, context_media, audio_file, media_root):
        listed = await server.call("media_ops_list_context", _params(context_media))
        entry = next(m for m in listed["media"] if not m["inline"])
        before = sorted(p.name for p in media_root.iterdir())

        res = await server.call("media_ops_save",
                                _params(context_media, id=entry["id"],
                                        path=str(media_root / "copy.wav")))

        assert res["status"] == "success", res
        assert res["copied"] is False
        assert res["path"] == str(audio_file)
        assert sorted(p.name for p in media_root.iterdir()) == before

    async def test_saved_file_can_be_loaded_back(self, server, media_root,
                                                 context_media):
        media_id = await self._first_inline(server, context_media,
                                            context_media.gemini)
        target = media_root / "roundtrip.png"
        saved = await server.call("media_ops_save",
                                  _params(context_media, id=media_id,
                                          path=str(target)))
        assert saved["status"] == "success", saved

        loaded = await server.call("media_ops_load", {"path": saved["path"]})

        assert loaded["status"] == "success", loaded
        attached = MultimodalToolContent(**loaded["_multimodal_content"][0])
        assert Path(attached.path).read_bytes() == context_media.gemini

    async def test_existing_file_is_kept_unless_overwrite_is_set(
            self, server, media_root, context_media):
        media_id = await self._first_inline(server, context_media,
                                            context_media.anthropic)
        target = media_root / "taken.png"
        target.write_bytes(b"do not lose me")

        refused = await server.call("media_ops_save",
                                    _params(context_media, id=media_id,
                                            path=str(target)))
        assert refused["error_type"] == "FileExists", refused
        assert target.read_bytes() == b"do not lose me"

        forced = await server.call("media_ops_save",
                                   _params(context_media, id=media_id,
                                           path=str(target), overwrite=True))
        assert forced["status"] == "success", forced
        assert target.read_bytes() == context_media.anthropic

    async def test_unknown_id_is_an_error(self, server, media_root, context_media):
        res = await server.call("media_ops_save",
                                _params(context_media, id="mdeadbeef0000",
                                        path=str(media_root / "x.png")))
        assert res["error_type"] == "MediaNotFound", res

    async def test_unsupported_target_extension_is_refused(
            self, server, media_root, context_media):
        media_id = await self._first_inline(server, context_media,
                                            context_media.anthropic)
        res = await server.call("media_ops_save",
                                _params(context_media, id=media_id,
                                        path=str(media_root / "dump.bin")))
        assert res["error_type"] == "UnsupportedMediaType", res
        assert not (media_root / "dump.bin").exists()


# ══ Wiring ════════════════════════════════════════════════════════════════

class TestConfigArrives:
    def test_the_configured_values_reach_the_plugin(self, server, media_root):
        assert server.allowed_roots == [media_root.resolve()]
        assert server.max_file_size_mb == MAX_MB

    @pytest.mark.skipif(not PLUGINS_YAML.exists(), reason="no config/plugins.yaml")
    def test_every_shipped_setting_reaches_the_plugin(self):
        """The shipped config through the real server — no hand-built values."""
        shipped = yaml.safe_load(PLUGINS_YAML.read_text(encoding="utf-8"))[
            "plugins"]["servers"]["media_ops"]
        assert shipped, "media_ops block is empty — test would be vacuous"
        # `type` must equal the plugin directory name or discovery never finds it.
        assert shipped["type"] == "media_ops"

        srv = MediaOpsServer("media_ops", SimpleNamespace(),
                                ToolServerConfig(**shipped))
        effective = {
            "allowed_directories": [str(p) for p in srv.allowed_roots],
            "max_file_size_mb": srv.max_file_size_mb,
        }

        for key, want in shipped.items():
            if key in ("type", "enabled", "description"):
                continue  # framework keys, not read by this plugin
            assert key in effective, f"dead config key, nothing reads it: {key}"
            if key == "allowed_directories":
                assert effective[key] == [str(Path(d).resolve()) for d in want]
            else:
                assert float(effective[key]) == float(want), key


class TestSchema:
    def test_every_declared_tool_has_an_implementation(self, server):
        names = [t["function"]["name"] for t in server.get_tools()]
        assert len(names) == 3, names
        for name in names:
            method = server._get_method_name(name)
            assert callable(getattr(server, method)), f"{name} -> {method}() missing"

    def test_descriptions_only_name_tools_that_exist(self, server):
        declared = {t["function"]["name"] for t in server.get_tools()}
        text = " ".join(t["function"]["description"] for t in server.get_tools())
        mentioned = set(re.findall(r"\bmedia_ops_\w+", text))

        assert mentioned, "no tool referenced anywhere — check would be vacuous"
        assert mentioned <= declared, f"names a tool that does not exist: " \
                                      f"{mentioned - declared}"


class TestReadOnly:
    """A read-only sandbox must not advertise `save`, and must refuse it."""

    @staticmethod
    def _server(media_root: Path) -> MediaOpsServer:
        cfg = ToolServerConfig(type="media_ops", enabled=True, config={
            "allowed_directories": [str(media_root)],
            "read_only": True,
        })
        return MediaOpsServer("media_ops", SimpleNamespace(), cfg)

    def test_save_is_not_offered(self, media_root):
        names = {t["function"]["name"] for t in self._server(media_root).get_tools()}
        assert "media_ops_save" not in names, names
        # Counter-check: reading is still offered, so an empty tool list
        # cannot make this pass.
        assert "media_ops_load" in names, names

    async def test_save_refuses_before_validating_its_arguments(self, media_root):
        """First gate, so the reason is the sandbox and not a complaint about
        the id — with no arguments at all it still says read-only."""
        res = await self._server(media_root).save({})
        assert res["status"] == "error", res
        assert res["error_type"] == "PermissionError", res
        assert "read-only" in res["error"]


class TestStatusEvents:
    """The status line must say WHAT happened.

    Shipped behaviour was the scope's defaults: every call ended as a bare
    'completed' — including refused ones, which therefore READ as successes
    in the CLI/WebUI status stream. These tests ride the REAL path: the
    events land on the real status bus via call_with_status, which is what
    injects ``_status`` in production.
    """

    async def _run(self, server, action, params, scope):
        from agent_system.tools.status import get_status_bus
        bus = get_status_bus()
        queue = await bus.subscribe(server=scope)
        try:
            res = await server.call_with_status(action, params)
        finally:
            bus.unsubscribe(queue)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert events, "no status events arrived — the subscription is vacuous"
        return res, events

    @staticmethod
    def _only(events, phase):
        from agent_system.tools.status import StatusPhase
        matching = [e for e in events if e.phase is getattr(StatusPhase, phase)]
        assert len(matching) == 1, [(e.phase, e.message) for e in events]
        return matching[0]

    async def test_load_end_names_file_and_kind(self, server, image_file):
        res, events = await self._run(server, "media_ops_load",
                                      {"path": str(image_file)},
                                      "media_ops.load()")
        assert res["status"] == "success", res
        end = self._only(events, "END")
        assert "shot.png" in end.message and "image" in end.message, end.message

    async def test_denied_load_is_an_error_event_not_completed(
            self, server, outside_file):
        res, events = await self._run(server, "media_ops_load",
                                      {"path": str(outside_file)},
                                      "media_ops.load()")
        assert res["status"] == "error", res
        error = self._only(events, "ERROR")
        assert error.message == res["error"]
        from agent_system.tools.status import StatusPhase
        assert not any(e.phase is StatusPhase.END for e in events), \
            "a refusal must not also read as 'completed'"

    async def test_list_context_end_carries_the_count(self, server, context_media):
        res, events = await self._run(server, "media_ops_list_context",
                                      _params(context_media),
                                      "media_ops.list_context()")
        assert res["count"] > 0, "empty fixture would make this vacuous"
        end = self._only(events, "END")
        assert end.message.startswith(f"{res['count']} media item"), end.message

    async def test_save_end_names_target_and_size(self, server, media_root,
                                                  context_media):
        listed = await server.call("media_ops_list_context",
                                   _params(context_media))
        entry = next(m for m in listed["media"]
                     if m["inline"] and m["type"] == "image")
        res, events = await self._run(
            server, "media_ops_save",
            _params(context_media, id=entry["id"],
                    path=str(media_root / "kept.png")),
            "media_ops.save()")
        assert res["status"] == "success", res
        end = self._only(events, "END")
        assert "kept.png" in end.message and "image" in end.message, end.message
