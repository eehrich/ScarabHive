"""The block of an LLM belongs to the LLM: every agent sees it, an answer lifts it
for every agent, and it grows while the LLM keeps failing."""
import pytest

from agent_system.llm.model_health import (
    FIRST_RATE_LIMIT_PAUSE,
    PROBE_LEASE,
    ModelHealth,
    model_key,
)

HOUR = 3600.0


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _Client:
    def __init__(self, model="deepseek/deepseek-v4.1-flash", base_url="https://openrouter.ai/api/v1"):
        self.model = model
        self.base_url = base_url


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def health(clock):
    return ModelHealth(clock=clock)


def test_an_llm_nobody_blocked_is_available(health):
    assert health.available(_Client(), "r-1")


def test_a_block_holds_for_every_request_and_client_of_that_llm(health, clock):
    health.block(_Client(), max_pause=HOUR, rate_limit=True)

    # another agent's request, its own client object for the same LLM
    assert not health.available(_Client(), "r-2")
    assert not health.available(_Client(), "r-1")
    clock.now += FIRST_RATE_LIMIT_PAUSE - 1
    assert not health.available(_Client(), "r-2")
    clock.now += 1
    assert health.available(_Client(), "r-2")


def test_another_llm_is_not_held_back(health):
    health.block(_Client("deepseek/deepseek-v4.1-flash"), max_pause=HOUR, rate_limit=True)

    assert health.available(_Client("~deepseek/deepseek-v4-flash-latest"), "r-1")
    # same model name at another endpoint: another LLM
    assert health.available(_Client(base_url="https://api.deepseek.com"), "r-1")


def test_a_client_keeping_its_url_privately_is_the_same_llm_as_one_at_that_url(health):
    class _OpenAIAsyncLike:
        model = "deepseek/deepseek-v4.1-flash"
        _base_url = "https://openrouter.ai/api/v1/"

    health.block(_Client(), max_pause=HOUR, rate_limit=True)

    assert not health.available(_OpenAIAsyncLike(), "r-1")


def test_clients_without_an_endpoint_and_other_keys_are_other_llms(health):
    class _BatchClient:
        model = "gemini-3.1-pro-preview"

    class _SdkClient:
        model = "gemini-3.1-pro-preview"

    health.block(_BatchClient(), max_pause=HOUR, rate_limit=False)
    assert health.available(_SdkClient(), "r-1"), "a batch quota blocked the sync client of the same model"

    paid, free = _Client(), _Client()
    paid.api_key, free.api_key = "sk-paid", "sk-free"
    health.block(free, max_pause=HOUR, rate_limit=False)
    assert health.available(paid, "r-1"), "a refused key blocked another key"
    same_key = _Client()
    same_key.api_key = "sk-free"
    assert not health.available(same_key, "r-1")
    assert "sk-free" not in repr(model_key(free)), "the key itself went into the block's key"


def test_a_burst_of_429s_from_calls_already_in_flight_is_one_failure(health, clock):
    asked_at = health.now()                          # seven calls go out together
    clock.now += 1
    pauses = [health.block(_Client(), max_pause=HOUR, rate_limit=True, asked_at=asked_at) for _ in range(7)]

    assert pauses[0] == FIRST_RATE_LIMIT_PAUSE
    assert health.remaining(_Client()) == FIRST_RATE_LIMIT_PAUSE, "the burst doubled the block towards the cap"
    # a call that went out after the block and fails again does double it
    clock.now += 1
    assert health.block(_Client(), max_pause=HOUR, rate_limit=True, asked_at=health.now()) == 2 * FIRST_RATE_LIMIT_PAUSE


def test_a_straggler_of_a_burst_does_not_undo_a_probe_finding_the_llm_healthy(health, clock):
    asked_at = health.now()                          # the straggler's call goes out
    clock.now += 10
    health.block(_Client(), max_pause=HOUR, rate_limit=True)          # the burst's block
    clock.now += FIRST_RATE_LIMIT_PAUSE + 10
    probe_asked_at = health.now()
    assert health.available(_Client(), "prober")

    # the client's own retries delivered the straggler's 429 only now
    health.block(_Client(), max_pause=HOUR, rate_limit=True, asked_at=asked_at)
    clock.now += 1
    health.release(_Client(), asked_at=probe_asked_at)                 # the probe answers

    assert health.available(_Client(), "r-2"), "the straggler re-blocked the LLM the probe found healthy"


def test_a_zero_cap_blocks_nothing(health):
    assert health.block(_Client(), max_pause=0.0, rate_limit=True) == 0.0
    assert health.available(_Client(), "r-1") and health.available(_Client(), "r-2")


def test_a_block_never_shortens_a_longer_one_still_running(health, clock):
    health.block(_Client(), max_pause=HOUR, rate_limit=False)          # a quota: an hour
    clock.now += 10

    health.block(_Client(), max_pause=300.0, rate_limit=True)          # another agent's 429, short cap

    assert health.remaining(_Client()) == HOUR - 10


def test_a_prober_asking_again_does_not_renew_its_lease(health, clock):
    health.block(_Client(), max_pause=HOUR, rate_limit=True)
    clock.now += FIRST_RATE_LIMIT_PAUSE
    assert health.available(_Client(), "prober")
    clock.now += PROBE_LEASE - 1
    assert health.available(_Client(), "prober")      # still its probe ...
    clock.now += 1
    assert health.available(_Client(), "r-2"), "... but asking again kept the LLM from everyone else"


def test_a_dropped_probe_frees_the_llm_for_the_next_request_at_once(health, clock):
    health.block(_Client(), max_pause=HOUR, rate_limit=True)
    clock.now += FIRST_RATE_LIMIT_PAUSE
    assert health.available(_Client(), "prober")

    health.drop_probe(_Client(), "someone-else")
    assert not health.available(_Client(), "r-2"), "another request dropped a probe it does not hold"
    health.drop_probe(_Client(), "prober")
    assert health.available(_Client(), "r-2")


def test_a_rate_limit_block_doubles_while_the_llm_keeps_failing_up_to_the_cap(health, clock):
    pauses = []
    for _ in range(8):
        pauses.append(health.block(_Client(), max_pause=900.0, rate_limit=True))
        clock.now += pauses[-1]

    assert pauses == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0, 900.0, 900.0]
    # a cap below the first pause caps that one too
    assert health.block(_Client("m-2"), max_pause=30.0, rate_limit=True) == 30.0


def test_an_answer_frees_the_llm_for_everyone_and_the_next_block_starts_short(health, clock):
    health.block(_Client(), max_pause=HOUR, rate_limit=True)
    clock.now += FIRST_RATE_LIMIT_PAUSE
    health.block(_Client(), max_pause=HOUR, rate_limit=True)   # the probe failed: 120 s

    health.release(_Client())

    assert health.available(_Client(), "r-9")
    assert health.remaining(_Client()) == 0.0
    assert health.block(_Client(), max_pause=HOUR, rate_limit=True) == FIRST_RATE_LIMIT_PAUSE


def test_an_answer_to_a_call_older_than_the_block_leaves_it(health, clock):
    asked_at = health.now()
    clock.now += 5
    health.block(_Client(), max_pause=HOUR, rate_limit=True)   # another request's 429 meanwhile

    health.release(_Client(), asked_at=asked_at)
    assert not health.available(_Client(), "r-2"), "a call that went out before the 429 lifted it"

    # in the same clock tick the block stays as well
    health.release(_Client(), asked_at=health.now())
    assert not health.available(_Client(), "r-2")

    clock.now += 1
    health.release(_Client(), asked_at=health.now())
    assert health.available(_Client(), "r-2"), "a call that went out after the block did not lift it"


def test_quota_and_refusals_block_the_full_pause_at_once(health):
    assert health.block(_Client(), max_pause=HOUR, rate_limit=False) == HOUR


def test_the_providers_retry_after_is_the_lower_bound(health):
    assert health.block(_Client(), max_pause=HOUR, rate_limit=True, retry_after=300.0) == 300.0
    assert health.block(_Client("m-2"), max_pause=HOUR, rate_limit=True, retry_after=5.0) == FIRST_RATE_LIMIT_PAUSE


def test_a_run_out_block_is_probed_by_one_request_while_the_others_wait(health, clock):
    health.block(_Client(), max_pause=HOUR, rate_limit=True)
    clock.now += FIRST_RATE_LIMIT_PAUSE

    assert health.available(_Client(), "prober")
    assert not health.available(_Client(), "r-2"), "a second request walked into the probe's 429"
    assert health.available(_Client(), "prober"), "the prober lost its own probe on asking again"
    clock.now += PROBE_LEASE
    assert health.available(_Client(), "r-2"), "a probe that never reported back kept the LLM blocked"


def test_a_failed_probe_blocks_everyone_again(health, clock):
    health.block(_Client(), max_pause=HOUR, rate_limit=True)
    clock.now += FIRST_RATE_LIMIT_PAUSE
    assert health.available(_Client(), "prober")

    assert health.block(_Client(), max_pause=HOUR, rate_limit=True) == 2 * FIRST_RATE_LIMIT_PAUSE
    assert not health.available(_Client(), "prober")


def test_a_client_without_a_model_name_is_never_blocked(health):
    anonymous = object()

    assert model_key(anonymous) is None
    assert health.block(anonymous, max_pause=HOUR, rate_limit=True) is None
    assert health.available(anonymous, "r-1")


def test_blocks_and_releases_move_the_version(health):
    start = health.version
    health.release(_Client())
    assert health.version == start, "releasing an LLM that was not blocked changed nothing"
    health.block(_Client(), max_pause=HOUR, rate_limit=True)
    assert health.version == start + 1
    health.release(_Client())
    assert health.version == start + 2
