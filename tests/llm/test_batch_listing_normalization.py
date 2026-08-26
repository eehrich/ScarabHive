"""Each batch plugin describes its OWN listing shape.

Job recovery after a restart reads `list_batches()`, which returns raw
provider JSON. The queue manager used to switch on the provider NAME and pull
the fields itself — with an `else` branch that assumed OpenAI's keys. So
Anthropic listings (`id` + `processing_status`, not `status`) were parsed with
the wrong keys: every open batch came back with an empty status string and the
model name "gpt-4o", and finished ones were never skipped.

The normalization now lives with each provider's client; the core only asks.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_system.llm.batch.base import BatchProviderClient
from agent_system.llm.batch.models import BatchStatus


@pytest.fixture
def gemini():
    from plugins_llm.llm_gemini.gemini_batch import GeminiBatchClient
    with patch("google.genai.Client"):
        return GeminiBatchClient(api_key="k")


@pytest.fixture
def openai():
    from plugins_llm.llm_openai_compat.openai_batch import OpenAIBatchClient
    with patch("httpx.AsyncClient"):
        return OpenAIBatchClient(api_key="k")


@pytest.fixture
def anthropic():
    from plugins_llm.llm_anthropic.anthropic_batch import AnthropicBatchClient
    with patch("httpx.AsyncClient"):
        return AnthropicBatchClient(api_key="k", default_model="claude-sonnet-4-6")


class TestOpenRunningJobsAreRecoverable:
    def test_gemini(self, gemini):
        out = gemini.describe_listed_batch(
            {"name": "batches/abc", "state": "JOB_STATE_RUNNING"})
        assert out == {"job_id": "batches/abc",
                       "status": BatchStatus.IN_PROGRESS, "model": "unknown"}

    def test_openai(self, openai):
        out = openai.describe_listed_batch(
            {"id": "batch_1", "status": "in_progress",
             "metadata": {"model": "gpt-5.6"}})
        assert out == {"job_id": "batch_1",
                       "status": BatchStatus.IN_PROGRESS, "model": "gpt-5.6"}

    def test_anthropic_reads_processing_status_not_status(self, anthropic):
        """The regression this test exists for: `status` does not exist on an
        Anthropic batch object."""
        out = anthropic.describe_listed_batch(
            {"id": "msgbatch_1", "processing_status": "in_progress"})
        assert out["job_id"] == "msgbatch_1"
        assert out["status"] == BatchStatus.IN_PROGRESS
        assert out["model"] == "claude-sonnet-4-6"


class TestFinishedJobsAreSkipped:
    @pytest.mark.parametrize("state", [
        "JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED",
        # EXPIRED is the expensive omission: recovered as SUBMITTED, it would
        # be polled forever for results that will never come.
        "JOB_STATE_EXPIRED",
    ])
    def test_gemini(self, gemini, state):
        assert gemini.describe_listed_batch(
            {"name": "batches/x", "state": state}) is None

    @pytest.mark.parametrize("status", ["completed", "failed", "expired",
                                        "cancelled"])
    def test_openai(self, openai, status):
        assert openai.describe_listed_batch({"id": "b", "status": status}) is None

    def test_anthropic(self, anthropic):
        assert anthropic.describe_listed_batch(
            {"id": "msgbatch_1", "processing_status": "ended"}) is None


class TestEveryMappedStateIsCarried:
    """The other half of each mapping table.

    Only one running state per provider was exercised, so dropping the rest
    (and letting them fall to the default) stayed green — while a
    `validating` batch would be recovered as PENDING and a `canceling` one
    as SUBMITTED.
    """

    @pytest.mark.parametrize("state,expected", [
        ("JOB_STATE_PENDING", BatchStatus.SUBMITTED),
        ("JOB_STATE_RUNNING", BatchStatus.IN_PROGRESS),
    ])
    def test_gemini(self, gemini, state, expected):
        out = gemini.describe_listed_batch({"name": "b", "state": state})
        assert out["status"] == expected

    @pytest.mark.parametrize("status,expected", [
        ("validating", BatchStatus.VALIDATING),
        ("in_progress", BatchStatus.IN_PROGRESS),
        ("finalizing", BatchStatus.FINALIZING),
        ("cancelling", BatchStatus.CANCELLING),
    ])
    def test_openai(self, openai, status, expected):
        out = openai.describe_listed_batch({"id": "b", "status": status})
        assert out["status"] == expected

    @pytest.mark.parametrize("processing,expected", [
        ("in_progress", BatchStatus.IN_PROGRESS),
        ("canceling", BatchStatus.CANCELLING),
    ])
    def test_anthropic(self, anthropic, processing, expected):
        out = anthropic.describe_listed_batch(
            {"id": "b", "processing_status": processing})
        assert out["status"] == expected

    def test_anthropic_falls_back_to_its_default_model(self, anthropic):
        """`model` must never come back None — the queue manager puts it into
        a BatchJob and logs it."""
        anthropic.default_model = None
        out = anthropic.describe_listed_batch(
            {"id": "b", "processing_status": "in_progress"})
        assert out["model"] == "unknown"


class TestUnusableEntries:
    """An entry without an id cannot be recovered — better skipped than
    tracked as a job with an empty provider id."""

    def test_gemini_without_name(self, gemini):
        assert gemini.describe_listed_batch({"state": "JOB_STATE_RUNNING"}) is None

    def test_openai_without_id(self, openai):
        assert openai.describe_listed_batch({"status": "in_progress"}) is None

    def test_anthropic_without_id(self, anthropic):
        assert anthropic.describe_listed_batch(
            {"processing_status": "in_progress"}) is None

    def test_an_unknown_shape_is_not_guessed(self, anthropic):
        """A foreign payload must not be force-fitted into some shape."""
        assert anthropic.describe_listed_batch({"totally": "different"}) is None


def test_the_base_class_skips_instead_of_guessing():
    """A provider that lists batches but does not describe them loses
    recovery — it does not get parsed by guesswork."""
    class Bare(BatchProviderClient):
        async def submit_batch(self, *a, **k): ...
        async def get_batch_status(self, *a, **k): ...
        async def get_batch_results(self, *a, **k): ...
        async def cancel_batch(self, *a, **k): ...

    assert Bare().describe_listed_batch({"id": "x", "status": "in_progress"}) is None
