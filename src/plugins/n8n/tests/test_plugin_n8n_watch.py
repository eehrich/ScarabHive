"""wait_for_end: when a production run counts as over, and how long n8n is waited for."""
import pytest

from plugins.n8n.client import N8nError, N8nNotFound, N8nUnavailable
from plugins.n8n.watch import MAX_DELAY_S, wait_for_end


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def reader(*answers):
    """Answers in turn, the last one for ever."""
    queue = list(answers)

    async def read():
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return {"status": answer}
    return read


async def test_new_running_and_waiting_are_no_end_and_the_pause_grows_to_its_cap():
    clock = Clock()
    outcome = await wait_for_end(reader("new", "running", "waiting", *["running"] * 10, "success"),
                                 max_s=3600, sleep=clock.sleep, clock=clock)
    assert outcome == {"status": "success"}
    assert clock.sleeps[:3] == [2.0, 3.0, 4.5] and max(clock.sleeps) == MAX_DELAY_S


@pytest.mark.parametrize("state", ["success", "error", "crashed", "canceled", "unknown"])
async def test_every_end_state_ends_it_at_once(state):
    clock = Clock()
    assert await wait_for_end(reader(state), max_s=60, sleep=clock.sleep, clock=clock) == {"status": state}
    assert not clock.sleeps


async def test_n8n_away_is_waited_out_then_unknown():
    clock = Clock()
    outcome = await wait_for_end(reader(N8nUnavailable("down")), max_s=600, sleep=clock.sleep, clock=clock)
    assert outcome["status"] == "unknown" and "not reachable" in outcome["note"] and clock.now <= 600


async def test_a_refused_key_ends_the_wait_at_once():
    """Waiting cannot fix a rotated key; a day of polling would only hide it."""
    clock = Clock()
    outcome = await wait_for_end(reader(N8nError("the n8n public API refused the key -- check N8N_API_KEY")),
                                 max_s=86_400, sleep=clock.sleep, clock=clock)
    assert outcome["status"] == "unknown" and "N8N_API_KEY" in outcome["note"] and not clock.sleeps


async def test_a_run_that_does_not_end_is_unknown_after_the_limit():
    clock = Clock()
    outcome = await wait_for_end(reader("running"), max_s=3600, sleep=clock.sleep, clock=clock)
    assert outcome["status"] == "unknown" and "not finished after 1 h" in outcome["note"]


async def test_a_404_first_means_not_stored_later_means_gone():
    clock = Clock()
    never = await wait_for_end(reader(N8nNotFound("x")), max_s=60, sleep=clock.sleep, clock=clock)
    gone = await wait_for_end(reader("running", N8nNotFound("x")), max_s=60, sleep=clock.sleep, clock=clock)
    assert never["status"] == gone["status"] == "not_found"
    assert "not stored" in never["note"] and "gone" in gone["note"]
