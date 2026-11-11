"""The workflows the coder-workflows skill ships, run through the engine with scripted agents.

A finding stands when two of its three skeptics cannot refute it and falls when two refute it. One that
neither decides -- its skeptics failed, a timeout, the run's output-token cap -- is lost, and says so: counted
as refuted, it read as a clean review.
"""
from __future__ import annotations

import json
from pathlib import Path

from plugins.stategraph.kinds import ActivityError
from plugins.stategraph.tests.stategraph_testkit import FakeBackend, Harness

MACHINES = Path(__file__).resolve().parents[1] / "skills" / "coder-workflows" / "machines"
DOWN = ActivityError("agent_failed", "down")


class Scripted(dict):
    """Every activity path answered by one function; a dict answer goes out as an agent writes it, JSON."""

    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def get(self, path, default=None):
        def handler(call):
            value = self.answer(call["path"])
            return json.dumps(value) if isinstance(value, dict) else value
        return handler


def skeptics(path):
    """Per finding (verify/<finding>/<skeptic>): 0 stands, 1 falls, 2 and 3 are left undecided."""
    finding, skeptic = (int(part) for part in path.split("/")[1:3])
    votes = {0: [False, False, True], 1: [True, True, False], 2: [DOWN, DOWN, DOWN], 3: [False, True, DOWN]}
    vote = votes[finding][skeptic]
    return vote if isinstance(vote, BaseException) else {"refuted": vote, "reason": "read the code"}


FINDINGS = {"findings": [{"file": "a.py", "line": n, "claim": f"claim {n}"} for n in range(4)]}


async def _run(tmp_path, name, answer, params):
    harness = Harness(tmp_path)
    backend = FakeBackend()
    backend.handlers = Scripted(answer)  # after: its constructor copies the handlers into a plain dict
    try:
        return await harness.run({name: (MACHINES / name).read_text(encoding="utf-8")}, root=name,
                                 backend=backend, params=params, timeout=30)
    finally:
        await harness.close()


async def test_a_review_names_the_findings_its_skeptics_left_undecided(tmp_path):
    def answer(path):
        return FINDINGS if path.startswith("review/") else skeptics(path)

    row = await _run(tmp_path, "review_with_refutation.yaml", answer,
                     {"change": "x", "files": ["a.py"], "lenses": ["correctness"]})
    assert row["status"] == "succeeded", row
    out = row["output"]
    assert [f["claim"] for f in out["confirmed"]] == ["claim 0"]
    assert out["refuted"] == 1
    assert out["lost"] == ["verify: claim 2", "verify: claim 3"]


async def test_a_build_ends_with_the_findings_its_skeptics_left_undecided_as_lost(tmp_path):
    reviews = []

    def answer(path):
        if path == "build":
            return {"summary": "changed", "files": ["a.py"]}
        if path == "test":
            return {"passed": True, "output": ""}
        if path.startswith("review/"):
            reviews.append(path)
            return FINDINGS if len(reviews) == 1 else {"findings": []}
        if path.startswith("verify/"):
            return skeptics(path)
        raise AssertionError(path)

    row = await _run(tmp_path, "build_until_green.yaml", answer,
                     {"task": "do x", "tests": "pytest -q", "lenses": ["correctness"]})
    assert row["status"] == "succeeded", row
    out = row["output"]
    assert len(reviews) == 2, "the finding that stood went back to the builder, and the second review found none"
    assert out["open_findings"] == [] and out["lost"] == ["verify: claim 2", "verify: claim 3"], out
