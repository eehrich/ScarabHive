"""Claude must run on a path where its prompt cache actually works.

Measured 2026-09-01, same model, same prompt, twice in a row:

    provider=openai_responses   call 1: cached=0 write=0      $0.009278
                                call 2: cached=0 write=0      $0.009278
    provider=openai_httpx       call 1: cached=0 write=4617   $0.0115865
                                call 2: cached=4617 write=0   $0.0009674

OpenRouter passes `cache_control` through to Anthropic on Chat Completions,
but NOT on the Responses API — there the markers are simply dropped. Since
`openrouter-base` sets `provider: openai_responses` for the whole family,
Claude inherited the broken path and every turn re-paid the full prompt at
roughly ten times the price. A live agent-cli run after the switch: 78%
cache hits on the second turn.

The client deliberately does not recognize model names; the dialect and the
path are declared on the model, and this guard is what keeps a new Claude
profile from inheriting the broken one again.
"""
from __future__ import annotations

from agent_system.config.settings import load_settings

#: Substrings that identify an Anthropic model in a `model:` string, e.g.
#: "~anthropic/claude-sonnet-latest". Kept here, in the test, ON PURPOSE.
ANTHROPIC_MARKERS = ("claude", "anthropic")

#: The provider whose OpenRouter route drops cache_control (see the numbers
#: above). Chat Completions (openai_httpx) carries it.
CACHE_BLIND_PROVIDER = "openai_responses"


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


def test_no_claude_profile_runs_on_the_cache_blind_path():
    offenders = [f"{name} (model={cfg.model})"
                 for name, cfg in _claude_profiles()
                 if cfg.provider == CACHE_BLIND_PROVIDER]
    assert not offenders, (
        "these Claude profiles route through " + CACHE_BLIND_PROVIDER
        + ", where OpenRouter drops cache_control -- every turn re-pays the "
        "full prompt (~10x): " + "; ".join(offenders)
        + " -- set `provider: openai_httpx` on the model entry")


def test_claude_profiles_declare_the_anthropic_dialect():
    """Belt and braces: httpx derives the dialect itself, but the declaration
    is what makes the model self-describing rather than relying on a name
    check inside a generic client."""
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
    wrong reason -- the empty-set trap. It caught exactly that when the
    provider switch moved every Claude profile out of the old filter."""
    covered = [name for name, cfg in _claude_profiles()
               if cfg.base_url and "openrouter.ai" in cfg.base_url]
    assert covered, (
        "no Claude profile on OpenRouter was found at all -- the model naming "
        "or the routing changed, and this guard is measuring nothing")
