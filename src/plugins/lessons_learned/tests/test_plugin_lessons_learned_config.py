"""The configured extraction profile has to reach the extraction.

`schema.yaml` declares `llm_profile`, `config/plugins.yaml` ships it as
`chat-nostream` — and nothing read it. Extraction always ran on the hardcoded
`turbo` fallback, because the call site asked for `extraction_llm_profile`,
which is the PER-AGENT hook key (writer_agent.yaml sets it). Both levels are
legitimate; what was missing is that the plugin-level value acts as the
default for agents that set no hook key.

Nothing here is a rebuild: the real `config/plugins.yaml` goes through the
real server. A hand-built config would set exactly the keys the author has in
mind and could not have shown the gap.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest
import yaml

from agent_system.config.models import ToolServerConfig
from plugins.lessons_learned.server import LessonsLearnedServer

PLUGINS_YAML = Path("config/plugins.yaml")

FRAMEWORK_KEYS = {"type", "enabled", "description"}


def _shipped() -> dict:
    data = yaml.safe_load(PLUGINS_YAML.read_text(encoding="utf-8"))
    return data["plugins"]["servers"]["lessons_learned"]


def _server(overrides: dict | None = None) -> LessonsLearnedServer:
    cfg = {k: v for k, v in _shipped().items() if k not in FRAMEWORK_KEYS}
    cfg.update(overrides or {})
    return LessonsLearnedServer(
        "lessons_learned", types.SimpleNamespace(),
        ToolServerConfig(type="lessons_learned", enabled=True, **cfg))


@pytest.mark.skipif(not PLUGINS_YAML.exists(), reason="no config/plugins.yaml")
class TestProfilesArrive:
    def test_the_shipped_extraction_profile_is_effective(self):
        shipped = _shipped().get("llm_profile")
        assert shipped, "llm_profile is not shipped — the test would be vacuous"
        assert _server().llm_profile == shipped

    def test_the_shipped_consolidation_profile_is_effective(self):
        shipped = _shipped().get("consolidation_llm_profile")
        assert shipped, "not shipped — the test would be vacuous"
        assert _server().consolidation_llm_profile == shipped

    def test_an_override_wins_over_the_shipped_value(self):
        """Counter-check: without it, a hardcoded return of the shipped string
        would satisfy the two tests above."""
        assert _server({"llm_profile": "think"}).llm_profile == "think"

    def test_the_default_is_a_profile_that_exists(self):
        """A profile name that resolves to nothing disables the feature
        silently — the client falls back to the agent's own LLM."""
        from agent_system.config.settings import load_settings

        known = set(load_settings().llm_system.profiles)
        assert len(known) >= 20, "config did not load — test would be vacuous"
        assert _server().llm_profile in known


class TestTheCallSiteUsesIt:
    """The attribute alone proves nothing.

    A mutation that reverts the call site to a hardcoded "turbo" left the
    attribute tests green — the value arrived and was then ignored one line
    later. What has to hold is which profile the extraction is actually
    invoked with.
    """

    @staticmethod
    async def _profile_used(server, hook_config: dict) -> str:
        """Drive on_session_end and capture the profile handed to extraction."""
        # The import is local to the method (`from .extraction import ...`),
        # so the module that owns the name is `extraction`, not `server`.
        import plugins.lessons_learned.extraction as extraction

        captured = {}

        async def fake_extract(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(candidates=[], stored=0)

        messages = [types.SimpleNamespace(role="user", content="a"),
                    types.SimpleNamespace(role="assistant", content="b")] * 5
        context = types.SimpleNamespace(
            agent_name="a1", session_id="s1", messages=messages,
            hook_config=hook_config, agent=None)

        original = extraction.extract_lessons_from_conversation
        extraction.extract_lessons_from_conversation = fake_extract
        try:
            await server.on_session_end(context)
        finally:
            extraction.extract_lessons_from_conversation = original
        assert captured, "extraction was never called - test would be vacuous"
        return captured["llm_profile"]

    async def test_without_a_hook_key_the_plugin_default_is_used(self):
        used = await self._profile_used(_server({"llm_profile": "think"}), {})
        assert used == "think", "the plugin-level profile never reached extraction"

    async def test_a_per_agent_hook_key_still_wins(self):
        """writer_agent.yaml sets extraction_llm_profile - that must keep
        overriding the deployment default."""
        used = await self._profile_used(
            _server({"llm_profile": "think"}),
            {"extraction_llm_profile": "chat-nostream"})
        assert used == "chat-nostream"
