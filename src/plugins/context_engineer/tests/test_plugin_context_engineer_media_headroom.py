"""Pre-Layer M's window, and the readings the gates after the media passes use.

Each eviction of the always-compact-media pass rewrites an old message and
breaks the provider prompt cache from there. Without headroom that happened on
every call once keep_last media messages were reached; always_compact_media_headroom
evicts below keep_last, so `headroom` new media messages follow each break quietly.

Pre-Layer B read the byte reading taken before the media passes: when those had
already fixed the size, B still stripped all media outside the last two
messages. Layer 1 reads the tokens from before them, and rides the break a media
pass makes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_system.hooks import HookContext, HookType
from agent_system.llm.models import ChatMessage
from plugins.context_engineer import hooks as hooks_mod
from plugins.context_engineer.archival_memory import ArchivalMemory
from plugins.context_engineer.compaction import CompactionConfig, LayeredCompactionStrategy
from plugins.context_engineer.core_memory import CoreMemory
from plugins.context_engineer.hooks import ContextEngineerPlugin
from plugins.context_engineer.tool_result_store import ToolResultStore


def _strategy(tmp_path, **config):
    defaults = dict(layer1_threshold=10**9, layer2_threshold=10**9, layer3_threshold=10**9,
                    target_tokens=10**9, max_messages=0, deduplicate_media=False,
                    compact_media_after_user_message=False,
                    compact_media_after_final_response=False)
    defaults.update(config)
    return LayeredCompactionStrategy(
        tool_store=ToolResultStore(tmp_path / "tools.db", session_id="t"),
        core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
        archival_memory=ArchivalMemory(tmp_path / "archive.db", session_id="t"),
        config=CompactionConfig(**defaults),
    )


def _image_step(n: int, path: str) -> list[dict]:
    """One tool call whose result carries an image file, as image tools attach it."""
    call_id = f"call_{n}"
    return [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": "render", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "name": "render", "content": '{"status": "ok"}',
         "multimodal_content": [{"type": "image", "path": path, "mime_type": "image/png"}]},
    ]


def _run(tmp_path, images: int, size: int = 1000) -> list[dict]:
    messages = [{"role": "user", "content": "Render the cover."}]
    for n in range(images):
        path = tmp_path / f"render_{n}.png"
        path.write_bytes(bytes([n]) * size)
        messages += _image_step(n, str(path))
    return messages


def _with_media(messages: list[dict]) -> list[int]:
    return [i for i, msg in enumerate(messages) if msg.get("multimodal_content")]


class TestTheWindow:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("keep_last, headroom, images, kept", [
        (4, 2, 4, 4),   # at the limit: nothing goes
        (4, 2, 5, 2),   # past it: down to keep_last - headroom
        (2, 2, 3, 1),   # never below one
        (2, 0, 2, 2),   # no headroom: today's window
        (2, 0, 3, 2),
    ])
    async def test_evicts_past_keep_last_down_to_below_it(self, tmp_path, keep_last, headroom,
                                                          images, kept):
        messages = _run(tmp_path, images)
        with_media = _with_media(messages)
        result = await _strategy(tmp_path, always_compact_media_keep_last=keep_last,
                                 always_compact_media_headroom=headroom).compact(
            messages, current_tokens=10)

        assert _with_media(result.modified_messages) == with_media[len(with_media) - kept:]
        assert result.media_always_compacted == images - kept

    @pytest.mark.asyncio
    @pytest.mark.parametrize("headroom, breaks", [(0, 10), (1, 5)])
    async def test_one_break_buys_headroom_quiet_calls(self, tmp_path, headroom, breaks):
        """An image loop of 12 steps with keep_last 2: without headroom every call
        from the third on rewrites an old message, with headroom 1 every other."""
        strategy = _strategy(tmp_path, always_compact_media_keep_last=2,
                             always_compact_media_headroom=headroom)
        messages = [{"role": "user", "content": "Render the cover."}]
        rewrites = 0
        for n in range(12):
            path = tmp_path / f"render_{n}.png"
            path.write_bytes(bytes([n]) * 1000)
            messages = messages + _image_step(n, str(path))
            result = await strategy.compact(messages, current_tokens=10)
            messages = result.modified_messages
            rewrites += result.media_always_compacted > 0
            assert len(_with_media(messages)) <= 2, "the context carried more than keep_last"

        assert rewrites == breaks

    @pytest.mark.asyncio
    async def test_the_media_event_keeps_a_remote_images_address(self, tmp_path):
        """The other callers of the eviction reach it with every media item."""
        strategy = _strategy(tmp_path, compact_media_after_user_message=True)
        url = "https://example.org/reference.png"
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "match this"},
                                         {"type": "image_url", "image_url": {"url": url}}]},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [{"type": "image_url", "image_url": {
                "url": "data:image/png;base64,iVBORw0KGgo" + "A" * 4000}}]},
            {"role": "assistant", "content": "seen"},
            {"role": "user", "content": "and now?"},
        ]

        result = await strategy.compact(messages, current_tokens=100, force=True,
                                        trigger_event="user_message")

        assert result.media_compacted_after_event >= 1, "fixture: the event evicted nothing"
        assert url in str(result.modified_messages[0]), result.modified_messages[0]

    @pytest.mark.asyncio
    async def test_an_image_it_cannot_evict_takes_no_slot(self, tmp_path):
        """A remote reference image in the request is never evicted; counted as
        media, it held the window one over the limit and every step broke again."""
        strategy = _strategy(tmp_path, always_compact_media_keep_last=2,
                             always_compact_media_headroom=1)
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "Render the cover in this style."},
            {"type": "image_url", "image_url": {"url": "https://example.org/style.png"}}]}]
        rewrites = 0
        for n in range(12):
            path = tmp_path / f"render_{n}.png"
            path.write_bytes(bytes([n]) * 1000)
            messages = messages + _image_step(n, str(path))
            result = await strategy.compact(messages, current_tokens=10)
            messages = result.modified_messages
            rewrites += result.media_always_compacted > 0

        assert "https://example.org/style.png" in str(messages[0]), "fixture: the reference went"
        assert rewrites == 5, "not the break count of the same loop without the reference"

    @pytest.mark.asyncio
    async def test_an_image_by_remote_url_keeps_its_address(self, tmp_path):
        """Nothing to store and no payload to save: the hint replaced the only
        address the image had, and the model could never see it again."""
        strategy = _strategy(tmp_path, always_compact_media_keep_last=1)
        urls = [f"https://example.org/cover_{n}.png" for n in range(3)]
        messages = [{"role": "user", "content": [
            {"type": "text", "text": f"look at cover {n}"},
            {"type": "image_url", "image_url": {"url": url}}]} for n, url in enumerate(urls)]
        payload = "iVBORw0KGgo" + "A" * 4000
        for filler in ("B", "A"):   # two evictable images: the older one goes
            messages.insert(0, {"role": "user", "content": [{"type": "image_url", "image_url": {
                "url": f"data:image/png;base64,iVBORw0KGgo{filler * 4000}"}}]})

        result = await strategy.compact(messages, current_tokens=100, force=True)

        kept = str(result.modified_messages)
        assert payload not in kept, "fixture: the window evicted nothing"
        assert all(url in kept for url in urls), kept


class TestReadingsAfterTheMediaPasses:

    @pytest.mark.asyncio
    async def test_the_byte_limit_is_read_after_pre_layer_m(self, tmp_path):
        """Four 300 KB images against a 1.5 MB limit: Pre-Layer M keeps three and
        the request is under it. Read from before, B left only the newest image
        and Layer 1 ran on top."""
        messages = _run(tmp_path, 4, size=300_000)
        with_media = _with_media(messages)
        strategy = _strategy(tmp_path, always_compact_media_keep_last=3,
                             max_request_bytes=1_500_000)
        assert strategy._estimate_request_bytes(messages) > 1_500_000, "fixture is not over the limit"

        result = await strategy.compact(messages, current_tokens=10)

        assert strategy._estimate_request_bytes(result.modified_messages) <= 1_500_000
        assert result.layers_applied == ["M"]
        assert _with_media(result.modified_messages) == with_media[1:]


class TestTheHook:

    @pytest.fixture
    def plugin(self, tmp_path):
        impl = ContextEngineerPlugin(Path(hooks_mod.__file__).parent, stats_history=[])
        impl._storage_base = tmp_path
        yield impl
        for sid in list(impl._session_components):
            impl.cleanup_session(sid)

    @pytest.mark.asyncio
    async def test_layer_one_rides_a_media_break(self, plugin, tmp_path):
        """An image loop that grows, keep_last 2 with headroom 1, sent back as the
        history each call. The reading peaks on the calls where a third image is
        in, which are the calls Pre-Layer M evicts on. Gated on the reading after
        M, Layer 1 waited for a quiet call and broke the cache once more there."""
        config = {"layer1_threshold": 5000, "layer2_threshold": 10**9, "layer3_threshold": 10**9,
                  "min_tokens_between_compactions": 10**6,
                  "always_compact_media_keep_last": 2, "always_compact_media_headroom": 1,
                  "tool_result_keep_last": 3, "tool_result_min_size": 200,
                  "tool_result_max_window_share": 0, "deduplicate_media": False}
        history = [ChatMessage(role="system", content="You render covers."),
                   ChatMessage(role="user", content="Render the cover.")]
        sent_before = None
        lone_breaks, layer_one_calls = [], []
        for n in range(20):
            path = tmp_path / f"render_{n}.png"
            path.write_bytes(bytes([n]) * 2000)
            step = _image_step(n, str(path))
            step[1]["content"] = f"render log line {n} " * 50
            history = history + [ChatMessage(**msg) for msg in step]
            result = await plugin.engineer_context(HookContext(
                hook_type=HookType.PRE_LLM_CALL, request_id=f"r{n}", session_id="loop",
                messages=history, hook_config=config))
            sent = [msg.model_dump(exclude_none=True) for msg in result.context.messages]
            layers = (result.metadata or {}).get("layers_applied") or []
            broke = sent_before is not None and any(a != b for a, b in zip(sent_before, sent))
            if broke and "M" not in layers:
                lone_breaks.append((n, layers))
            if 1 in layers:
                layer_one_calls.append(n)
            sent_before = sent
            history = [msg for msg in result.context.messages
                       if msg.role != "system" or msg.injected_by is None]

        assert layer_one_calls, "fixture: the loop never reached Layer 1"
        assert lone_breaks == []
