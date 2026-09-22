"""Claude must declare the cache dialect it is billed by.

Claude caches with Anthropic's `cache_control`, not with the OpenAI
breakpoints the rest of the OpenRouter family uses. Without the declaration
the marker style falls back to the OpenAI one or to none, and the prompt is re-paid in
full every turn (measured 2026-09-01: ~10x the price of a cached turn).

Both OpenRouter routes carry it, in different places (measured 2026-09-22):
Chat Completions (`openai_httpx`) takes it on the content parts, the
Responses API (`openai_responses`) only as one top-level key -- see
`OpenAIResponsesClient._apply_top_level_cache_control`. The clients do not
recognize model names; the dialect is declared on the model, and this guard
keeps a new Claude profile from forgetting it.
"""
from __future__ import annotations

from agent_system.config.settings import load_settings

#: Substrings that identify an Anthropic model in a `model:` string, e.g.
#: "~anthropic/claude-sonnet-latest". Kept here, in the test, ON PURPOSE.
ANTHROPIC_MARKERS = ("claude", "anthropic")


def _models():
    """Only entries that can actually be called.

    A template like `openrouter-claude` carries no `model:` — it exists to be
    extended, and that is where the path is declared once for all its
    children. Judging it by its own (absent) model name would report the
    template as the offender.
    """
    return [(name, cfg) for name, cfg in load_settings().llm_system.models.items()
            if cfg.model]


def _is_anthropic(model) -> bool:
    return any(m in (model or "").lower() for m in ANTHROPIC_MARKERS)


def _claude_profiles():
    return [(name, cfg) for name, cfg in _models() if _is_anthropic(cfg.model)]


def test_claude_profiles_declare_the_anthropic_dialect():
    """The declaration is what makes the model self-describing rather than
    relying on a name check inside a generic client."""
    offenders = [
        f"{name} (style={cfg.prompt_cache_marker_style})"
        for name, cfg in _claude_profiles()
        if cfg.base_url and "openrouter.ai" in cfg.base_url
        and cfg.prompt_cache_marker_style != "anthropic"
    ]
    assert not offenders, offenders


def test_no_other_model_claims_the_anthropic_dialect():
    """Counter-check: cache_control on an OpenAI request is a foreign key in
    the payload."""
    offenders = [f"{name} (model={cfg.model})"
                 for name, cfg in _models()
                 if cfg.prompt_cache_marker_style == "anthropic"
                 and not _is_anthropic(cfg.model)]
    assert not offenders, offenders


def test_the_guard_actually_sees_the_claude_profiles():
    """A filter that matches nothing would make the tests above pass for the
    wrong reason -- the empty-set trap. It caught exactly that when a
    provider switch moved every Claude profile out of an older filter."""
    covered = [name for name, cfg in _claude_profiles()
               if cfg.base_url and "openrouter.ai" in cfg.base_url]
    assert covered, (
        "no Claude profile on OpenRouter was found at all -- the model naming "
        "or the routing changed, and this guard is measuring nothing")
