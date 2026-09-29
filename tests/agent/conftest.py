import pytest

from agent_system.llm.model_health import model_health


@pytest.fixture(autouse=True)
def _no_llm_blocks_across_tests():
    """The blocks belong to the process: a test that rate-limits a model name
    would otherwise route the next test using that name onto its fallback."""
    model_health.clear()
    yield
    model_health.clear()
