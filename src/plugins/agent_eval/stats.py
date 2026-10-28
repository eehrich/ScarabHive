"""Comparing two arms the way the measurement can carry it.

The unit is the TASK, not the run: per task the difference of the mean scores, ``d_i = arm - reference``.
Runs of one task are not independent evidence about the agent -- the task decides most of the outcome. The
decision is an exact two-sided sign test over the tasks whose difference is not zero; its direction is the
direction of the count, not of the mean, so a verdict never contradicts the "better / worse" it reports. With
few tasks the test cannot decide at all; the report says how many tasks must move one way (``needed``), so
"no difference" is never read as "the same".
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, Iterable, List, Optional

ALPHA = 0.05
#: Two-sided 97.5% quantiles of Student's t for 1..30 degrees of freedom.
_T975 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
         2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042)


def task_means(records: Iterable[Dict[str, Any]], arm: str) -> Dict[str, Dict[str, Any]]:
    """Per task of *arm*: runs, passes, mean score, cost, and the task's kind. Scored runs only: a run that
    timed out or crashed scores 0 and counts (the arm did that); one never started (budget, stop) does not."""
    out: Dict[str, Dict[str, Any]] = {}
    for r in records:
        if r.get("arm") != arm or not r.get("scored"):
            continue
        t = out.setdefault(r["task"], {"kind": r.get("kind", "learn"), "scores": [], "passes": 0})
        t["scores"].append(float(r.get("score", 0.0)))
        t["passes"] += 1 if r.get("passed") else 0
    for t in out.values():
        t["runs"] = len(t["scores"])
        t["mean"] = sum(t["scores"]) / t["runs"]
    return out


def sign_test(positive: int, negative: int) -> float:
    """Exact two-sided p of a sign test with *positive* and *negative* non-zero differences."""
    n = positive + negative
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(positive, negative) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def needed(moved: int) -> Optional[int]:
    """How many of *moved* tasks -- those the arms did not tie on; the sign test ignores ties -- must move the
    same way for it to decide; None when no outcome of that many can (five or fewer)."""
    return next((m for m in range((moved + 1) // 2, moved + 1) if sign_test(m, moved - m) < ALPHA), None)


def t_interval(values: List[float]) -> Optional[List[float]]:
    """95% interval of the mean difference over tasks (Student's t). None for fewer than two values or no spread
    -- every task moved alike, which says nothing about how far the next would."""
    if len(values) < 2:
        return None
    sd = statistics.stdev(values)
    if sd == 0:
        return None
    df = len(values) - 1
    t = _T975[df - 1] if df <= 30 else _t975_beyond_table(df)
    mean = statistics.fmean(values)
    half = t * sd / math.sqrt(len(values))
    return [mean - half, mean + half]


def _t975_beyond_table(df: int) -> float:
    """t(0.975, df) for df > 30, by the Cornish-Fisher expansion around the normal quantile (error < 1e-4)."""
    z = 1.959964
    return z + (z ** 3 + z) / (4 * df) + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96 * df ** 2)


def compare(reference: Dict[str, Dict[str, Any]], arm: Dict[str, Dict[str, Any]],
            kinds: Iterable[str] = ("learn", "regression", "holdout")) -> Dict[str, Any]:
    """*arm* against *reference*, paired by task, over the tasks of the given kinds both have."""
    kinds = set(kinds)
    tasks = sorted(t for t in reference if t in arm and reference[t]["kind"] in kinds)
    diffs = {t: arm[t]["mean"] - reference[t]["mean"] for t in tasks}
    values = list(diffs.values())
    positive = sum(1 for d in values if d > 0)
    negative = sum(1 for d in values if d < 0)
    p = sign_test(positive, negative)
    if not values:
        verdict = "no common tasks"
    elif needed(len(tasks)) is None:
        verdict = "not decidable with so few tasks"
    elif p < ALPHA:
        verdict = "better" if positive > negative else "worse"
    else:
        verdict = "no detectable difference"
    interval = t_interval(values)
    return {
        "tasks": len(tasks), "better_tasks": positive, "worse_tasks": negative,
        # The reference's tasks of these kinds: none means nothing to judge, some but none shared is a mismatch.
        "reference_tasks": sum(1 for t in reference.values() if t["kind"] in kinds),
        "mean_diff": round(statistics.fmean(values), 4) if values else 0.0,
        "ci95": [round(v, 4) for v in interval] if interval else None,
        "p": round(p, 4), "moved": positive + negative, "needed": needed(positive + negative),
        "verdict": verdict,
        "per_task": {t: round(d, 4) for t, d in diffs.items()},
    }


def regressions(reference: Dict[str, Dict[str, Any]], arm: Dict[str, Dict[str, Any]]) -> List[str]:
    """Regression tasks the reference passed every time and the arm passes at most a third of the time."""
    return sorted(t for t, ref in reference.items()
                  if ref["kind"] == "regression" and t in arm and ref["passes"] == ref["runs"] > 0
                  and arm[t]["passes"] * 3 <= arm[t]["runs"])


def _p90(values: List[float]) -> Optional[float]:
    """The 90th percentile by nearest rank."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def _models(records) -> Dict[str, Dict[str, int]]:
    total: Dict[str, Dict[str, int]] = {}
    for record in records:
        for agent, used in (record.get("models") or {}).items():
            for model, calls in used.items():
                total.setdefault(agent, {})[model] = total.get(agent, {}).get(model, 0) + calls
    return total


def _off_model(records: List[Dict[str, Any]], expected: Dict[str, Any]) -> tuple:
    """({agent: models it fell back to}, {agent: its advanced model it escalated to}) over *records* of one arm.
    The suite's agent is held to the model its run's agent was built with (``model``, after the task's overlay
    too); a run that left no result (timed out, crashed) to the one its task's overlay gives (``tasks``)."""
    fell: Dict[str, set] = {}
    escalated: Dict[str, set] = {}
    for record in records:
        wanted = dict(expected.get("models") or {})
        own = expected.get("agent")
        built = record.get("model") or (expected.get("tasks") or {}).get(record.get("task"))
        if own in wanted and built:
            wanted[own] = {**wanted[own], "model": built}
        for agent, used in (record.get("models") or {}).items():
            runs = wanted.get(agent)
            if runs is None:
                continue
            advanced = runs.get("advanced") if runs.get("advanced") != runs.get("model") else None
            for model in set(used) - {runs.get("model")}:
                (escalated if model == advanced else fell).setdefault(agent, set()).add(model)
    return ({agent: sorted(models) for agent, models in fell.items()},
            {agent: sorted(models) for agent, models in escalated.items()})


def report(records: List[Dict[str, Any]], reference_arm: str, arms: List[str],
           reference_records: Optional[List[Dict[str, Any]]] = None,
           expected: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Every arm against the reference arm -- of the same records, or of *reference_records* (a baseline).

    Learn and holdout tasks are judged apart: only the holdout is evidence once someone has tuned on the
    learn tasks. Regression tasks are checked one by one. Cost counts every run that started; a run whose cost
    could not be read is counted in ``cost_unknown_runs`` instead of as free. ``models``: each agent's calls per
    model it ran on. *expected* (runner.expected_models): per arm, the model each agent it names runs on first and,
    if it escalates by itself, its advanced one; ``fallbacks`` names the agents that also ran on another -- those
    runs did not measure the arm's choice alone --, ``escalated`` the ones that also ran on their advanced model
    (an escalation; with a one-profile chain also what a fallback would look like).
    """
    reference = task_means(reference_records if reference_records is not None else records, reference_arm)
    out: Dict[str, Any] = {"reference_arm": reference_arm, "arms": {}}
    for name in arms:
        means = task_means(records, name)
        ran = [r for r in records if r.get("arm") == name and r.get("scored")]
        costs = [float(r["cost"]) for r in ran if r.get("cost") is not None]
        runs = sum(m["runs"] for m in means.values())
        entry: Dict[str, Any] = {
            "tasks": {t: {k: v for k, v in m.items() if k != "scores"} for t, m in sorted(means.items())},
            "runs": runs,
            "pass_rate": round(sum(m["passes"] for m in means.values()) / runs, 4) if runs else None,
            "mean_score": round(statistics.fmean([m["mean"] for m in means.values()]), 4) if means else None,
            "cost": round(sum(costs), 4),
            "cost_unknown_runs": len(ran) - len(costs),
            "cost_p90": round(_p90(costs), 4) if costs else None,
            "models": _models(r for r in records if r.get("arm") == name),
        }
        entry["fallbacks"], entry["escalated"] = _off_model([r for r in records if r.get("arm") == name],
                                                            (expected or {}).get(name) or {})
        if reference_records is not None or name != reference_arm:
            entry["learn"] = compare(reference, means, ("learn",))
            entry["holdout"] = compare(reference, means, ("holdout",))
            entry["regressions"] = regressions(reference, means)
        out["arms"][name] = entry
    return out
