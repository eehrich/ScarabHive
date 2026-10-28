"""agent-eval: run a suite, list runs, compare them.

    agent-eval run <suite.yaml> [--arm a --arm b] [--baseline]
    agent-eval list
    agent-eval show <run_id> [--against <run_id>|--against-baseline] [--json]
    agent-eval baseline <run_id>
    agent-eval stop <run_id>
    agent-eval export-run <stategraph run_id> <out dir> [--data <dir>] [--include <data path> ...]
    agent-eval judge <run_id>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any, Dict, Optional

from . import store
from .stats import ALPHA, judge_shares, report


def build_report(run_id: str, against: Optional[str] = None) -> Dict[str, Any]:
    """The run's arms against its reference arm, or against the reference arm of run *against* -- a run of the
    same suite (ValueError otherwise; a suite file changed in between is reported in ``warnings``)."""
    meta = store.read_meta(run_id)
    records = store.read_records(run_id)
    warnings = []
    if against == run_id:
        against = None  # a run against itself compares nothing
    if against:
        ref_meta = store.read_meta(against)
        if ref_meta["suite"] != meta["suite"]:
            raise ValueError(f"{against} is a run of suite '{ref_meta['suite']}', not '{meta['suite']}'")
        if ref_meta.get("suite_sha") != meta.get("suite_sha"):
            warnings.append(f"The suite file changed between {against} and this run: tasks of the same name "
                            "may ask or check something else.")
        out = report(records, ref_meta["reference_arm"], meta["arms"], store.read_records(against),
                     meta.get("expected_models"))
        out["against"] = against
        # the reference is the baseline's: an agent of it that fell back makes it measure something else too
        base = report(store.read_records(against), ref_meta["reference_arm"], [ref_meta["reference_arm"]],
                      expected=ref_meta.get("expected_models"))["arms"][ref_meta["reference_arm"]]
        for agent, models in base["fallbacks"].items():
            warnings.append(f"In the baseline {against}, {agent} fell back to {', '.join(models)}.")
    else:
        out = report(records, meta["reference_arm"], meta["arms"], expected=meta.get("expected_models"))
    calls = (meta.get("judge") or {}).get("calls") or []
    warnings += judge_shares(calls, records)
    for name, arm in out["arms"].items():
        length = arm.get("length")
        if length and length["p"] < ALPHA and length["longer_higher"] > length["longer_lower"]:
            warnings.append(f"In arm {name} the judge scored the longer of two products higher "
                            f"{length['longer_higher']} times, lower {length['longer_lower']} (p={length['p']}): "
                            "it may be judging length.")
    if calls:
        costs = [c["cost"] for c in calls if c.get("cost") is not None]
        out["judge"] = {"calls": len(calls), "products": sum(c["products"] for c in calls),
                        "judged": sum(c["judged"] for c in calls), "cost": round(sum(costs), 4) if costs else None,
                        "problems": [f"{c['task']}: {c['problem']}" for c in calls if c.get("problem")]}
    out["meta"] = meta
    out["warnings"] = warnings
    out["skipped"] = {s: sum(1 for r in records if r.get("status") == s) for s in store.NOT_COUNTED}
    # judging that never ended, its runner did: agent-eval judge does it (while it runs, the judge is still to come)
    if store.live_status(meta) != "running":
        out["skipped"]["judge_pending"] = sum(1 for r in records if r.get("judge") == "pending")
    return out


def _print_report(rep: Dict[str, Any]) -> None:
    meta = rep["meta"]
    print(f"{meta['run_id']}  suite {meta['suite']}  agent {meta['agent']}  {meta['status']}")
    ref = f"{rep['reference_arm']}" + (f" of {rep['against']}" if rep.get("against") else "")
    print(f"reference: {ref}")
    for warning in rep.get("warnings", []):
        print(f"WARNING: {warning}")
    for name, arm in rep["arms"].items():
        unknown = f" + {arm['cost_unknown_runs']} run(s) unknown" if arm.get("cost_unknown_runs") else ""
        p90 = f"${arm['cost_p90']:.4f}" if arm.get("cost_p90") is not None else "n/a"
        rate = f"{arm['pass_rate']:.0%}" if arm.get("pass_rate") is not None else "n/a"
        pooled = f" (pooled {arm['pooled_score']})" if arm.get("pooled_score") is not None else ""
        print(f"\n[{name}] runs {arm['runs']}  pass {rate}  mean score {arm['mean_score']}{pooled}"
              f"  cost ${arm['cost']:.4f}{unknown} (p90/run {p90})")
        for agent, models in (arm.get("fallbacks") or {}).items():
            print(f"  WARNING: {agent} fell back to {', '.join(models)} -- those runs did not measure this arm's model")
        for agent, models in (arm.get("escalated") or {}).items():
            print(f"  note: {agent} also ran on its advanced model {', '.join(models)} -- an escalation, "
                  "or a fallback past its chain")
        for task, t in arm["tasks"].items():
            print(f"  {task:<30} {t['kind']:<10} {t['passes']}/{t['runs']}  score {t['mean']:.2f}")
        length = arm.get("length")
        if length:
            print(f"  length: products {length['mean_chars']} chars on average; of two, the longer scored higher "
                  f"{length['longer_higher']}x, lower {length['longer_lower']}x (p={length['p']})")
        for kind in ("learn", "holdout"):
            c = arm.get(kind)
            if not c or not c["reference_tasks"]:
                continue
            if not c["tasks"]:
                print(f"  {kind}: no common tasks -- the reference has {c['reference_tasks']}, this arm ran none")
                continue
            needs = (f"of the {c['moved']} that moved, deciding takes {c['needed']} one way"
                     if c["needed"] is not None
                     else f"{c['moved']} moved task(s) can never decide, it takes six or more (ties do not count)")
            print(f"  {kind}: {c['verdict']} -- {c['better_tasks']} better / {c['worse_tasks']} worse of "
                  f"{c['tasks']}, p={c['p']}; mean diff {c['mean_diff']:+.3f}, CI {c['ci95'] or 'none (every task alike)'}; "
                  f"{needs}")
        if arm.get("regressions"):
            print(f"  REGRESSIONS: {', '.join(arm['regressions'])}")
    judge = rep.get("judge")
    if judge:
        cost = f"${judge['cost']:.4f}" if judge.get("cost") is not None else "cost unknown"
        print(f"\njudge: {judge['judged']} of {judge['products']} product(s) judged in {judge['calls']} call(s), {cost}")
        for problem in judge["problems"]:
            print(f"  {problem}")
    skipped = {k: v for k, v in rep["skipped"].items() if v}
    if skipped:
        print(f"\nnot counted: {skipped}")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-eval", description="Measure agents against task suites.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="run a suite")
    p_run.add_argument("suite")
    p_run.add_argument("--arm", action="append", help="run only this arm (repeatable)")
    p_run.add_argument("--run-id", help=argparse.SUPPRESS)  # the panel names the run it starts
    p_run.add_argument("--baseline", action="store_true", help="make the finished run the suite's baseline")
    sub.add_parser("list", help="list runs")
    p_show = sub.add_parser("show", help="show a run's report")
    p_show.add_argument("run_id")
    group = p_show.add_mutually_exclusive_group()
    group.add_argument("--against", help="compare against another run's reference arm")
    group.add_argument("--against-baseline", action="store_true", help="compare against the suite's baseline")
    p_show.add_argument("--json", action="store_true")
    p_base = sub.add_parser("baseline", help="make a run its suite's baseline")
    p_base.add_argument("run_id")
    p_stop = sub.add_parser("stop", help="start no further runs of a running suite")
    p_stop.add_argument("run_id")
    p_judge = sub.add_parser("judge", help="judge again what a run left unjudged")
    p_judge.add_argument("run_id")
    p_export = sub.add_parser("export-run", help="freeze a stategraph run for fork tasks")
    p_export.add_argument("run_id")
    p_export.add_argument("out", help="the export directory (empty or new), usually next to the suite")
    p_export.add_argument("--data", help="the data directory that holds the run (default the installation's)")
    p_export.add_argument("--include", action="append", default=[],
                          help="a path in the data directory to take along, what the fork hooks read (repeatable)")
    args = parser.parse_args(argv)

    from agent_system.paths import enter_project
    enter_project()
    try:
        if args.command == "run":
            from .runner import run_suite
            from agent_system.paths import user_path
            run_id = run_suite(user_path(args.suite), run_id=args.run_id, arms=args.arm)
            refused = None
            if args.baseline:
                try:
                    store.set_baseline(store.read_meta(run_id)["suite"], run_id)
                except ValueError as e:  # an incomplete run: its report is still worth reading
                    refused = e
            meta = store.read_meta(run_id)
            baseline = store.baseline_for(meta["suite"])
            # One arm: measured against the baseline. Several: against their own reference arm, which ran
            # beside them -- same hours, same provider routing -- unlike a baseline from another day.
            against = baseline if baseline and baseline != run_id and len(meta["arms"]) == 1 else None
            _print_report(build_report(run_id, against))
            if refused:
                print(f"Error: not made the baseline: {refused}", file=sys.stderr)
                return 1
        elif args.command == "list":
            base = store.baselines()
            for m in store.list_runs():
                mark = " (baseline)" if base.get(m["suite"]) == m["run_id"] else ""
                print(f"{m['run_id']}  {m['status']:<8} {m['suite']:<24} {m['agent']:<24} arms {','.join(m['arms'])}{mark}")
        elif args.command == "show":
            against = args.against
            if args.against_baseline:
                against = store.baseline_for(store.read_meta(args.run_id)["suite"])
                if not against:
                    print("this suite has no baseline yet (agent-eval baseline <run_id>)", file=sys.stderr)
                    return 1
            rep = build_report(args.run_id, against)
            if args.json:
                print(json.dumps(rep, indent=2, ensure_ascii=False))
            else:
                _print_report(rep)
        elif args.command == "baseline":
            store.set_baseline(store.read_meta(args.run_id)["suite"], args.run_id)
            print(f"{args.run_id} is now the baseline of its suite")
        elif args.command == "stop":
            store.request_stop(args.run_id)
            print(f"{args.run_id}: no further runs start; running ones finish")
        elif args.command == "judge":
            from .runner import rejudge
            rejudge(args.run_id)
            _print_report(build_report(args.run_id))
        elif args.command == "export-run":
            from agent_system.paths import user_path
            from .runner import export_run
            out = export_run(args.run_id, user_path(args.out), user_path(args.data) if args.data else None,
                             args.include)
            print(f"{args.run_id} exported to {out}")
    except (ValueError, OSError, sqlite3.Error) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
