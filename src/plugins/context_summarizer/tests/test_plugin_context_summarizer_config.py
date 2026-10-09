"""Does what is in the configuration arrive — and is the prompt non-empty?

The server kept a SECOND default table and afterwards copied every value onto
the hook implementation, i.e. AFTER the schema.yaml defaults were resolved. Two
of its fallbacks were wrong, and because ``plugins.yaml`` sets neither key, the
wrong fallback was always the effective value:

* ``llm_profile`` fell back to ``'fast'`` — a profile ``config/llm.yaml`` does
  not know at all and ``schema.yaml`` does not even list in its enum. Client
  creation failed silently because of it, and the summarizer used the agent's
  LLM instead.
* ``summary_prompt_template`` fell back to ``''``. The prompt is built as
  ``template.replace('{messages}', …)`` — with an empty template the result
  is empty, the messages are not even inserted. Measured on the production
  path: length 0. The summarizer called the LLM with an empty user message and
  put the answer in the place of the real conversation.

The tests therefore run the REAL ``config/plugins.yaml`` through the REAL
server. With a hand-built config both gaps would have stayed invisible —
such a test sets exactly the keys the author is thinking of at that moment.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from plugins.context_summarizer.server import ContextSummarizerServer

PLUGINS_YAML = Path("config/plugins.yaml")
PLUGIN_DIR = Path(__file__).parent.parent

#: yaml key -> attribute on the hook. Only where the names differ.
_ALIASES = {
    "summarization_trigger_percentage": "trigger_percentage",
    "summarization_chunk_size": "chunk_size",
    "preserve_recent_count": "preserve_recent",
    "preserve_system_messages": "preserve_system",
    "summary_prompt_template": "prompt_template",
    "min_summary_reduction": "min_reduction",
    "max_message_preview_length": "max_preview_length",
    "min_time_between_summarizations": "min_time_between",
    "max_tracked_sessions": "_max_tracked_sessions",
}


def _shipped() -> dict:
    data = yaml.safe_load(PLUGINS_YAML.read_text(encoding="utf-8"))
    return data["plugins"]["servers"]["context_summarizer"].get("config", {})


def _hook(config: dict):
    mcp = SimpleNamespace(config=dict(config), hook_config={},
                          name="context_summarizer")
    return ContextSummarizerServer(
        "context_summarizer", SimpleNamespace(), mcp)._hooks_impl


@pytest.mark.skipif(not PLUGINS_YAML.exists(), reason="no config/plugins.yaml")
def test_every_shipped_setting_reaches_the_plugin():
    """The test that would have found the gap."""
    shipped = _shipped()
    assert shipped, "the context_summarizer block is empty — test would be vacuous"

    hook = _hook(shipped)
    ignored = []
    for key, want in shipped.items():
        attr = _ALIASES.get(key, key)
        if not hasattr(hook, attr):
            ignored.append(f"{key}: no attribute '{attr}' on the plugin")
            continue
        got = getattr(hook, attr)
        same = (float(got) == float(want)
                if isinstance(want, (int, float)) and not isinstance(want, bool)
                else got == want)
        if not same:
            ignored.append(f"{key}: set {want!r}, effective {got!r}")

    assert not ignored, (
        "Values from config/plugins.yaml do not reach the plugin:\n  "
        + "\n  ".join(ignored))


def test_the_summarisation_prompt_is_never_empty():
    """An empty template makes the WHOLE prompt empty.

    ``template.replace('{messages}', msgs)`` runs ON the template — if it is
    empty, nothing is left after the replacement either. This is checked on the
    real server with the real configuration, because that is where the damage
    happened.
    """
    hook = _hook(_shipped())
    assert hook.prompt_template.strip(), (
        "prompt_template is empty — the summarizer would call the LLM with an "
        "empty message and put the answer in the place of the real "
        "conversation")
    assert "{messages}" in hook.prompt_template, (
        "without the placeholder the messages never reach the prompt")


def test_the_configured_profile_exists():
    """A profile that does not exist silently switches the feature off.

    With ``'fast'`` client creation failed and the summarizer used the agent's
    LLM instead — without anything turning red.
    """
    hook = _hook(_shipped())
    declared = yaml.safe_load((PLUGIN_DIR / "schema.yaml").read_text(encoding="utf-8"))
    allowed = declared.get("config", {}).get("llm_profile", {}).get("enum")
    assert allowed, "schema.yaml lists no enum for llm_profile — test would be vacuous"
    assert hook.llm_profile in allowed, (
        f"llm_profile={hook.llm_profile!r} is not in the enum {allowed}")

    # Via load_settings, not config/llm.yaml directly: the profiles live in
    # SEVERAL files (llm.yaml + llm_openrouter.yaml), and exactly the merged
    # view decides whether client creation succeeds. The direct-read approach
    # turned red when the profile switched to or-deepseek-flash — a false
    # alarm of the test, not a config error.
    from agent_system.config.settings import load_settings
    known = set(load_settings().llm_system.profiles)
    assert len(known) >= 20, "config did not arrive — test would be vacuous"
    assert hook.llm_profile in known, (
        f"llm_profile={hook.llm_profile!r} exists in no profile file "
        f"— client creation silently falls back to the agent's LLM")
