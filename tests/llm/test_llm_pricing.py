"""The central cost layer: one usage shape, one pricing rule.

Every consumer used to re-derive both, and they disagreed -- on whether a
provider ``cost`` of exactly 0.0 counts, on whether cache writes are priced, on
what an unknown model costs, and on which key holds the cached tokens. These
tests pin the shared behaviour so a fifth copy is never needed.
"""
import pytest
import yaml

from agent_system.llm import pricing
from agent_system.llm.pricing import (
    CallUsage,
    estimate_cost,
    normalize_usage,
    resolve_call_cost,
)


@pytest.fixture
def table(tmp_path, monkeypatch):
    """A pricing table on disk, with the module cache reset around it."""
    path = tmp_path / "llm_pricing.yaml"
    path.write_text(yaml.safe_dump({
        "cheap": {"input": 1.0, "output": 2.0, "cached_input": 0.1},
        "with_writes": {"input": 1.0, "output": 2.0, "cached_input": 0.1,
                        "cache_write": 1.25},
        "batchy": {"input": 10.0, "output": 20.0, "batch_discount": 0.5},
        "partial": {"input": 3.0},          # no output rate at all
    }), encoding="utf-8")
    monkeypatch.setattr(pricing, "_cache", {"path": None, "mtime": None, "table": {}})
    return path


class TestNormalizeUsage:
    """One canonical shape out of every client's dialect."""

    def test_openai_and_httpx_shape(self):
        call = normalize_usage({"prompt_tokens": 1000, "completion_tokens": 50,
                                "prompt_tokens_details": {"cached_tokens": 800}})
        assert (call.prompt_tokens, call.completion_tokens, call.cached_tokens) == (
            1000, 50, 800)

    def test_responses_api_shape(self):
        call = normalize_usage({"input_tokens": 500, "output_tokens": 20,
                                "input_tokens_details": {"cached_tokens": 400}})
        assert (call.prompt_tokens, call.completion_tokens, call.cached_tokens) == (
            500, 20, 400)

    def test_anthropic_cache_names(self):
        call = normalize_usage({"prompt_tokens": 100, "completion_tokens": 50,
                                "prompt_tokens_details": {
                                    "cached_tokens": 80,
                                    "cache_creation_tokens": 20}})
        assert call.cached_tokens == 80
        assert call.cache_write_tokens == 20

    def test_anthropic_raw_field_names(self):
        call = normalize_usage({"input_tokens": 7, "output_tokens": 3,
                                "cache_read_input_tokens": 5,
                                "cache_creation_input_tokens": 2})
        assert (call.prompt_tokens, call.cached_tokens, call.cache_write_tokens) == (
            7, 5, 2)

    def test_gemini_camel_case(self):
        call = normalize_usage({"promptTokenCount": 300, "candidatesTokenCount": 10,
                                "cachedContentTokenCount": 250})
        assert (call.prompt_tokens, call.completion_tokens, call.cached_tokens) == (
            300, 10, 250)

    def test_ollama_without_cache_or_cost(self):
        call = normalize_usage({"prompt_tokens": 10, "completion_tokens": 2})
        assert call.cached_tokens == 0 and call.provider_cost is None

    def test_total_tokens_is_derived_not_trusted(self):
        """A provider's own total is ignored -- prompt+completion is what the
        rates are applied to, so the two must never disagree."""
        call = normalize_usage({"prompt_tokens": 3, "completion_tokens": 4,
                                "total_tokens": 999})
        assert call.total_tokens == 7

    def test_zero_provider_cost_is_a_real_figure(self):
        """A free/BYOK model bills 0.0 -- that is billing, not 'unknown'."""
        assert normalize_usage({"cost": 0.0}).provider_cost == 0.0

    def test_garbage_input_is_survivable(self):
        for junk in (None, "nonsense", 42, [], {"prompt_tokens": "viele"}):
            assert normalize_usage(junk) == CallUsage()


class TestResolveCallCost:
    def test_provider_cost_wins_and_is_not_an_estimate(self, table):
        cost, estimated = resolve_call_cost(
            {"prompt_tokens": 10_000, "completion_tokens": 10_000, "cost": 0.25},
            "cheap", path=table)
        assert cost == 0.25 and estimated is False

    def test_zero_provider_cost_is_not_replaced_by_an_estimate(self, table):
        cost, estimated = resolve_call_cost(
            {"prompt_tokens": 1_000_000, "completion_tokens": 0, "cost": 0.0},
            "cheap", path=table)
        assert cost == 0.0 and estimated is False

    def test_estimate_is_flagged(self, table):
        cost, estimated = resolve_call_cost(
            {"prompt_tokens": 1_000_000, "completion_tokens": 0}, "cheap", path=table)
        assert cost == pytest.approx(1.0) and estimated is True

    def test_cached_tokens_use_the_cheaper_rate(self, table):
        full, _ = resolve_call_cost({"prompt_tokens": 1_000_000}, "cheap", path=table)
        cached, _ = resolve_call_cost(
            {"prompt_tokens": 1_000_000,
             "prompt_tokens_details": {"cached_tokens": 1_000_000}},
            "cheap", path=table)
        assert cached == pytest.approx(0.1) and cached < full

    def test_cache_writes_are_billed_on_top(self, table):
        """They were tracked everywhere and priced nowhere -- a systematic
        undercount for Anthropic and Gemini explicit caches."""
        without, _ = resolve_call_cost({"prompt_tokens": 1_000_000},
                                       "with_writes", path=table)
        with_writes, _ = resolve_call_cost(
            {"prompt_tokens": 1_000_000,
             "prompt_tokens_details": {"cache_write_tokens": 1_000_000}},
            "with_writes", path=table)
        assert with_writes == pytest.approx(without + 1.25)

    def test_no_cache_write_rate_means_no_extra_charge(self, table):
        """Models without the rate must cost exactly what they did before."""
        plain, _ = resolve_call_cost({"prompt_tokens": 1_000_000}, "cheap", path=table)
        writes, _ = resolve_call_cost(
            {"prompt_tokens": 1_000_000,
             "prompt_tokens_details": {"cache_write_tokens": 5_000_000}},
            "cheap", path=table)
        assert writes == plain

    def test_batch_discount_applies(self, table):
        sync, _ = resolve_call_cost({"prompt_tokens": 1_000_000}, "batchy", path=table)
        batch, _ = resolve_call_cost({"prompt_tokens": 1_000_000}, "batchy",
                                     is_batch=True, path=table)
        assert batch == pytest.approx(sync * 0.5)

    def test_unknown_model_is_unknown_not_zero(self, table):
        """Silently pricing an unknown model at 0.0 makes a run look free."""
        assert resolve_call_cost({"prompt_tokens": 1_000_000}, "nope", path=table) == (
            None, False)

    def test_missing_model_name_is_unknown(self, table):
        assert resolve_call_cost({"prompt_tokens": 10}, None, path=table) == (None, False)

    def test_incomplete_table_entry_does_not_raise(self, table):
        """A yaml entry without an `output` rate must not KeyError."""
        cost, estimated = resolve_call_cost(
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            "partial", path=table)
        assert cost == pytest.approx(3.0) and estimated is True


class TestEstimateCostGuards:
    def test_cached_larger_than_prompt_never_subtracts(self, table):
        """Anthropic can report more cache reads than prompt tokens; without
        the clamp the uncached term goes negative and REMOVES money."""
        cost = estimate_cost("cheap", prompt_tokens=100, completion_tokens=0,
                             cached_tokens=5000, path=table)
        assert cost is not None and cost >= 0

    def test_none_counts_are_treated_as_zero(self, table):
        assert estimate_cost("cheap", None, None, None, path=table) == 0.0


class TestPricingPath:
    def test_default_path_is_absolute_and_present(self):
        """A CWD-relative default made the estimate vanish silently whenever a
        tool ran from another directory."""
        assert pricing.DEFAULT_PRICING_PATH.is_absolute()
        assert pricing.DEFAULT_PRICING_PATH.exists()

    def test_real_table_loads(self):
        assert pricing.load_pricing()  # not empty
