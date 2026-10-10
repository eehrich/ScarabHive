"""Inheritance for LLM model entries (``extends``).

Mechanics only — no configuration VALUES are pinned here. What the entries in
config/ say is the operator's call and changes with price and measurement; that
the merge does what it promises is not.

The one thing this must never do is degrade quietly: a stale or misspelled
``extends`` has to be an error, because the alternative is an entry that runs
on framework defaults while looking like it runs on its base.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_system.config.settings import _resolve_model_inheritance, load_settings


def _resolve(models: dict) -> dict:
    data = {"llm_system": {"models": models}}
    _resolve_model_inheritance(data)
    return data["llm_system"]["models"]


class TestVariantsExtendConcreteEntries:
    """The 39 -unlimited/-nostream/-batch entries: same model, one knob turned."""

    def test_the_child_inherits_everything_it_does_not_say(self):
        out = _resolve({
            "parent": {"provider": "openai_httpx", "model": "m", "max_tokens": 16384,
                       "context_window": 400000, "service_tier": "flex"},
            "child": {"extends": "parent", "max_tokens": 0},
        })
        assert out["child"] == {"provider": "openai_httpx", "model": "m", "max_tokens": 0,
                                "context_window": 400000, "service_tier": "flex"}
        assert "extends" not in out["child"]

    def test_the_parent_is_not_modified_by_its_child(self):
        out = _resolve({
            "parent": {"model": "m", "max_tokens": 16384},
            "child": {"extends": "parent", "max_tokens": 0},
        })
        assert out["parent"]["max_tokens"] == 16384

    def test_chains_resolve(self):
        out = _resolve({
            "base": {"model": "m", "context_window": 100, "service_tier": "flex"},
            "mid": {"extends": "base", "context_window": 200},
            "leaf": {"extends": "mid", "max_tokens": 0},
        })
        assert out["leaf"] == {"model": "m", "context_window": 200,
                               "service_tier": "flex", "max_tokens": 0}

    def test_order_in_the_file_does_not_matter(self):
        """The child may stand above its parent, and in another include file."""
        out = _resolve({
            "child": {"extends": "parent", "max_tokens": 0},
            "parent": {"model": "m", "max_tokens": 16384, "service_tier": "flex"},
        })
        assert out["child"]["service_tier"] == "flex"


class TestDeepMerge:
    """Without a field-wise merge the variants are worth half: twelve of them
    differ in a single capabilities flag."""

    def test_a_single_capability_flag_keeps_its_siblings(self):
        out = _resolve({
            "parent": {"model": "m", "capabilities": {"tools": True, "json_mode": True,
                                                      "streaming": True}},
            "child": {"extends": "parent", "capabilities": {"streaming": False}},
        })
        assert out["child"]["capabilities"] == {"tools": True, "json_mode": True,
                                                "streaming": False}

    def test_a_list_replaces_by_default(self):
        out = _resolve({
            "parent": {"model": "m", "provider_routing": {"order": ["a", "b", "c"]}},
            "child": {"extends": "parent", "provider_routing": {"order": ["b"]}},
        })
        assert out["child"]["provider_routing"]["order"] == ["b"]

    def test_the_agent_list_syntax_works_here_too(self):
        """Same +item/!pattern mechanics as the agent type: chains."""
        out = _resolve({
            "parent": {"model": "m", "provider_routing": {"order": ["a", "b", "c"]}},
            "drop": {"extends": "parent", "provider_routing": {"order": ["!b"]}},
            "add": {"extends": "parent", "provider_routing": {"order": ["+d"]}},
        })
        assert out["drop"]["provider_routing"]["order"] == ["a", "c"]
        assert out["add"]["provider_routing"]["order"] == ["a", "b", "c", "d"]


class TestAnInheritedFieldCanBeSwitchedOff:
    """``key: null`` means different things in the two inheritance worlds.

    For an agent yaml an empty key is "nothing said" — the inherited value has
    to survive. A model entry has no other way to say "I want the provider
    default": leaving ``max_tokens`` out means inheriting the parent's cap,
    which is exactly what the -unlimited entries must not do.
    """

    def test_a_model_child_removes_it(self):
        out = _resolve({
            "head": {"model": "a", "max_tokens": 16384, "thinking_level": "high"},
            "child": {"extends": "head", "model": "b", "max_tokens": None},
        })
        assert out["child"]["max_tokens"] is None, "the cap was inherited anyway"
        assert out["child"]["thinking_level"] == "high", "sibling field lost"

    def test_the_agent_chains_keep_their_meaning(self):
        from agent_system.config.settings import _deep_merge_dict

        assert _deep_merge_dict({"a": 1}, {"a": None}) == {"a": 1}
        assert _deep_merge_dict({"a": 1}, {"a": None}, explicit_none=True) == {"a": None}

    def test_it_reaches_into_nested_blocks(self):
        out = _resolve({
            "head": {"model": "a", "capabilities": {"tools": True, "json_mode": True}},
            "child": {"extends": "head", "capabilities": {"json_mode": None}},
        })
        assert out["child"]["capabilities"] == {"tools": True, "json_mode": None}


class TestFamilyMembers:
    """No separate base section: a family member carries the shared knobs and
    the others extend it — exactly like the agent chains, where beat_writer
    extends writer_agent."""

    def test_a_sibling_supplies_the_shared_fields(self):
        out = _resolve({
            "family-head": {"provider": "openai_responses", "model": "vendor/a",
                            "service_tier": "flex",
                            "safety_settings": {"HARM_CATEGORY_HATE_SPEECH": "BLOCK_ONLY_HIGH"}},
            "sibling": {"extends": "family-head", "model": "vendor/b"},
        })
        assert out["sibling"]["model"] == "vendor/b", "own identity overridden away"
        assert out["sibling"]["provider"] == "openai_responses"
        assert out["sibling"]["safety_settings"] == {"HARM_CATEGORY_HATE_SPEECH": "BLOCK_ONLY_HIGH"}


class TestItFailsLoudly:
    def test_a_cycle_is_named(self):
        with pytest.raises(ValueError, match="cycle"):
            _resolve({"a": {"extends": "b", "model": "x"},
                      "b": {"extends": "a", "model": "y"}})

    def test_a_self_reference_is_a_cycle(self):
        with pytest.raises(ValueError, match="cycle"):
            _resolve({"a": {"extends": "a", "model": "x"}})

    def test_an_unknown_target_is_named(self):
        with pytest.raises(ValueError, match="not a model entry"):
            _resolve({"a": {"extends": "nope", "model": "x"}})

    def test_an_unresolved_extends_reaches_no_model(self):
        """Belt and braces: if the resolver were skipped, LLMModelConfig must
        refuse the leftover key instead of dropping it and running on defaults."""
        from pydantic import ValidationError

        from agent_system.config.models import LLMModelConfig
        with pytest.raises(ValidationError):
            LLMModelConfig.model_validate({"model": "x", "extends": "base"})


class TestTheCapabilitiesRegistryGoesThroughTheIncludes:
    """config.yaml is the only file read directly; everything else arrives
    through its includes. The registry used to read llm.yaml on its own — which
    missed the 36 models of llm_openrouter.yaml AND every unresolved extends."""

    MASTER = "includes:\n  - models.yaml\n"
    INCLUDED = """
llm_system:
  models:
    head-model:
      provider: openai_httpx
      model: vendor/head
      capabilities:
        tools: true
        json_mode: true
        streaming: true
    child-model:
      extends: head-model
      model: vendor/child
      capabilities:
        streaming: false
"""

    def test_a_master_config_brings_its_includes_resolved(self, tmp_path):
        from agent_system.llm.capabilities import load_capabilities_from_config

        (tmp_path / "config.yaml").write_text(self.MASTER, encoding="utf-8")
        (tmp_path / "models.yaml").write_text(self.INCLUDED, encoding="utf-8")

        caps = load_capabilities_from_config(config_path=str(tmp_path / "config.yaml"))

        assert "child-model" in caps, "the include never arrived"
        assert caps["child-model"].streaming is False, "own value lost"
        assert caps["child-model"].json_mode is True,             "inherited capability missing — the entry runs on defaults"


class TestTheRealConfigStillResolves:
    def test_every_model_entry_is_complete_after_resolution(self):
        """Grounds the suite: a resolver that produced nothing would leave every
        test above true and this one empty."""
        cfg = load_settings()
        models = cfg.llm_system.models
        assert len(models) >= 50, f"only {len(models)} models — config did not arrive"
        # Entries without `model:` are base classes. Allowed as long as no
        # profile points at them -- otherwise a None ends up in the client.
        abstract = {n for n, m in models.items() if not m.model}
        referenced = {p.model_ref for p in cfg.llm_system.profiles.values()}
        assert not (abstract & referenced),             f"Profiles point at base classes: {sorted(abstract & referenced)}"
        assert len(models) - len(abstract) >= 50

    def test_no_model_entry_carries_extends(self):
        raw = {}
        for f in ("config/llm.yaml", "config/llm_openrouter.yaml"):
            data = yaml.safe_load((REPO_ROOT / f).read_text(encoding="utf-8"))
            raw.update((data.get("llm_system") or {}).get("models") or {})
        assert raw, "no raw model entries found — the check would be vacuous"
        resolved = load_settings().llm_system.models
        for name in raw:
            assert name in resolved


class TestABaseClassMayNotBeDriven:
    """An entry without `model:` exists to be inherited from. Without a
    guard its None ends up in the client and the call only fails at the provider."""

    def _cfg(self, models, profiles):
        from agent_system.config.models import LLMSystemConfig
        return LLMSystemConfig.model_validate(
            {"models": models, "profiles": profiles})

    def test_a_profile_pointing_at_one_is_refused(self):
        import pytest
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="base classes"):
            self._cfg({"base": {"provider": "openai"}},
                      {"p": {"model_ref": "base"}})

    def test_the_base_class_itself_is_fine(self):
        cfg = self._cfg({"base": {"provider": "openai"},
                         "child": {"provider": "openai", "model": "gpt-5"}},
                        {"p": {"model_ref": "child"}})
        assert cfg.models["base"].model is None
        assert cfg.models["child"].model == "gpt-5"
