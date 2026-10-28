"""Scoring a finished run. Every check returns ``{"score": 0..1, "passed": bool, "detail": str}``, and ``of`` where
the score is a share of countable things (tests, expectations): how many -- the report pools by it.

A score is the continuous measure (share of green tests, share of expectations met): a task's difference between
two arms shows in it long before a pass/fail check flips.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List

from .proc import run_bounded, tail
from .suite import Check, Suite, Task

#: The tail of a check's output kept in its detail.
_DETAIL_CHARS = 2000


def run_check(check: Check, suite: Suite, task: Task, workspace: Path, run: Dict[str, Any]) -> Dict[str, Any]:
    """The check's verdict on one run. A check that fails to run, or runs past its timeout, scores 0 and says why."""
    try:
        verdict = _CHECKS[check.kind](check, suite, task, workspace, run)
        score = float(verdict.get("score", 1.0 if verdict.get("passed") else 0.0))
    except Exception as e:  # noqa: BLE001 - a broken check is a failed check, with its reason
        return {"score": 0.0, "passed": False, "detail": f"check failed: {type(e).__name__}: {e}"}
    score = min(1.0, max(0.0, score))
    of = verdict.get("of")
    return {"score": score, "passed": bool(verdict.get("passed", score >= 1.0)),
            "detail": str(verdict.get("detail", ""))[-_DETAIL_CHARS:],
            **({"of": of} if isinstance(of, int) and not isinstance(of, bool) and of > 0 else {})}


def _bounded(check: Check, args, workspace: Path, shell: bool = False, env=None) -> tuple:
    """(exit code or None at the timeout, output) of a check's process; output in a file beside the workspace."""
    log = workspace.parent / "check.log"
    code = run_bounded(args, cwd=workspace, log=log, timeout=check.timeout_s, env=env, shell=shell)
    return code, tail(log, _DETAIL_CHARS)


def _timed_out(check: Check, output: str) -> Dict[str, Any]:
    return {"score": 0.0, "passed": False, "detail": f"check timed out after {check.timeout_s:.0f}s\n{output}"}


def _command(check: Check, suite: Suite, task: Task, workspace: Path, run: Dict[str, Any]) -> Dict[str, Any]:
    code, output = _bounded(check, check.command, workspace, shell=True)
    if code is None:
        return _timed_out(check, output)
    return {"passed": code == 0, "detail": f"exit {code}\n{output}"}


#: pytest's default test file names: a path given explicitly would be collected whatever its name.
_TEST_FILE = re.compile(r"(test_.*|.*_test)\.py")
#: Directories whose every file belongs to the tests (fixtures, expected output).
_TEST_DIR = re.compile(r"tests?|tests?_.*|.*_tests?")


def _pytest(check: Check, suite: Suite, task: Task, workspace: Path, run: Dict[str, Any]) -> Dict[str, Any]:
    """pytest in the workspace, confined to it: rootdir, confcutdir and an ini beside it (not in it, so the
    agent cannot plant one), so no conftest or pytest.ini above the workspace -- the repository's, when runs
    live under its data directory -- takes part.

    What the run cannot do to its own score: change or delete a test of the template, add a conftest.py (both
    refused), add test files (only the template's are collected), shadow pytest itself with a module in the
    workspace (``-P``: the working directory is not put on sys.path first), or leave a report behind from
    before (it is deleted first). What it still can: the code under test runs inside this pytest process, and
    code there can do anything that process can. A check of an agent that is not trusted belongs in a process
    the code under test never runs in.
    """
    tampered = _tampered_tests(check, suite, task, workspace)
    if tampered:
        return {"score": 0.0, "passed": False, "detail": f"test files changed by the run: {', '.join(tampered)}"}
    report = workspace.parent / "pytest-report.xml"
    report.unlink(missing_ok=True)
    ini = workspace.parent / "pytest-eval.ini"
    # The workspace on sys.path, as when pytest runs from a project root: a test imports the module under test.
    # pytest splits the value like a shell line: backslashes would vanish, a space would split it.
    ini.write_text(f'[pytest]\npythonpath = "{workspace.as_posix()}"\n', encoding="utf-8")
    args = [sys.executable, "-P", "-m", "pytest", *_test_paths(check, suite, task), "-q", "-p", "no:cacheprovider",
            f"--rootdir={workspace}", f"--confcutdir={workspace}", "-c", str(ini), f"--junitxml={report}"]
    code, output = _bounded(check, args, workspace, env={**os.environ, "AGENT_SYSTEM_TEST_NO_REAP": "1"})
    if code is None:
        return _timed_out(check, output)
    if not report.exists():
        return {"score": 0.0, "passed": False, "detail": f"no report (exit {code})\n{output}"}
    total = failed = 0
    for suite_el in ET.parse(report).getroot().iter("testsuite"):
        total += int(suite_el.get("tests", 0)) - int(suite_el.get("skipped", 0))
        failed += int(suite_el.get("failures", 0)) + int(suite_el.get("errors", 0))
    if total <= 0:
        return {"score": 0.0, "passed": False, "detail": f"no tests ran (exit {code})\n{output}"}
    passed = total - failed
    return {"score": passed / total, "passed": failed == 0 and code == 0, "of": total,
            "detail": f"{passed}/{total} passed\n{output}"}


def _test_paths(check: Check, suite: Suite, task: Task) -> List[str]:
    """What pytest is given: each path of the check that is a directory of the template becomes the template's
    test files below it, so a test file the run adds is never collected. Files, node ids and paths the template
    does not have (a suite without a workspace) are passed as written."""
    out: List[str] = []
    template = suite.path(task.workspace) if task.workspace else None
    for arg in check.pytest.split():
        base = template / arg if template is not None and not arg.startswith("-") and "::" not in arg else None
        if base is not None and base.is_dir():
            out += sorted(p.relative_to(template).as_posix() for p in base.rglob("*.py")
                          if _TEST_FILE.fullmatch(p.name) and "__pycache__" not in p.parts)
        else:
            out.append(arg)
    return out


def _tampered_tests(check: Check, suite: Suite, task: Task, workspace: Path) -> list:
    """What the run changed of the tests the check runs: a test file of the workspace template under one of the
    pytest paths -- test_*.py, conftest.py, anything in a tests directory, a file the check names -- that differs
    or is gone, and any conftest.py the template does not have. The code under test is the run's to change.
    Without a template nothing to compare."""
    if not task.workspace:
        return []
    template = suite.path(task.workspace)
    roots = [arg for arg in check.pytest.split() if not arg.startswith("-") and (template / arg.split("::")[0]).exists()]
    out = []
    for arg in roots:
        base = template / arg.split("::")[0]
        for src in ([base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()
                                                   and _is_test_material(p.relative_to(template))]):
            rel = src.relative_to(template)
            mine = workspace / rel
            if "__pycache__" not in rel.parts and (not mine.is_file() or mine.read_bytes() != src.read_bytes()):
                out.append(rel.as_posix())
    out += [p.relative_to(workspace).as_posix() for p in workspace.rglob("conftest.py")
            if not (template / p.relative_to(workspace)).is_file()]
    return sorted(set(out))


def _is_test_material(rel: Path) -> bool:
    return bool(_TEST_FILE.fullmatch(rel.name) or rel.name == "conftest.py"
                or any(_TEST_DIR.fullmatch(part) for part in rel.parts[:-1]))


def _script(check: Check, suite: Suite, task: Task, workspace: Path, run: Dict[str, Any]) -> Dict[str, Any]:
    run_file = workspace.parent / "run.json"
    run_file.write_text(json.dumps(run, ensure_ascii=False, default=str), encoding="utf-8")
    log = workspace.parent / "check.log"
    code = run_bounded([sys.executable, str(suite.path(check.script)), str(workspace), str(run_file)],
                       cwd=workspace, log=log, timeout=check.timeout_s, env=_utf8_env())
    output = log.read_text(encoding="utf-8", errors="replace")
    if code is None:
        return _timed_out(check, output[-_DETAIL_CHARS:])
    lines = [line for line in output.splitlines() if line.strip()]
    if code != 0 or not lines:
        return {"score": 0.0, "passed": False, "detail": f"script exit {code}\n{output[-_DETAIL_CHARS:]}"}
    return _verdict(json.loads(lines[-1]))


def _python(check: Check, suite: Suite, task: Task, workspace: Path, run: Dict[str, Any]) -> Dict[str, Any]:
    module, _, name = check.python.partition(":")
    function = getattr(importlib.import_module(module), name)
    return _verdict(function(workspace, run, task))


def _expect(check: Check, suite: Suite, task: Task, workspace: Path, run: Dict[str, Any]) -> Dict[str, Any]:
    answer = run.get("answer") or ""
    wanted = [("contains", s, s in answer) for s in check.expect.get("contains", [])]
    wanted += [("regex", r, re.search(r, answer) is not None) for r in check.expect.get("regex", [])]
    missing = [f"{kind} {value!r}" for kind, value, hit in wanted if not hit]
    met = len(wanted) - len(missing)
    return {"score": met / len(wanted) if wanted else 0.0, "passed": not missing and bool(wanted),
            **({"of": len(wanted)} if wanted else {}),
            "detail": f"{met}/{len(wanted)} met" + (f"; missing: {', '.join(missing)}" if missing else "")}


def product_of(suite: Suite, task: Task, workspace: Path, run: Dict[str, Any]) -> str:
    """What the judge reads of a finished run: the suite judge's product of it, or the final answer. Raises when the
    product cannot be read (a script that fails, runs past its timeout or prints nothing)."""
    product = suite.judge.product
    if product is None:
        return run.get("answer") or ""
    if product.python:
        module, _, name = product.python.partition(":")
        text = getattr(importlib.import_module(module), name)(workspace, run, task)
        if not isinstance(text, str):
            raise ValueError(f"a product must be a str, got {type(text).__name__}")
        return text
    run_file = workspace.parent / "run.json"
    run_file.write_text(json.dumps(run, ensure_ascii=False, default=str), encoding="utf-8")
    out, err = workspace.parent / "product.txt", workspace.parent / "product.log"
    code = run_bounded([sys.executable, str(suite.path(product.script)), str(workspace), str(run_file)],
                       cwd=workspace, log=out, errors=err, timeout=product.timeout_s, env=_utf8_env())
    text = out.read_text(encoding="utf-8", errors="replace")
    if code != 0 or not text.strip():
        raise ValueError(f"product script {'timed out' if code is None else f'exit {code}'}"
                         f"{'' if text.strip() else ', printed nothing'}\n{tail(err, _DETAIL_CHARS)}")
    return text


def _utf8_env() -> Dict[str, str]:
    """A script's environment: its output read back as UTF-8, also where the console's code page is another."""
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def _verdict(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or not ("score" in value or "passed" in value):
        raise ValueError(f"a check must return a dict with 'score' and/or 'passed', got {value!r:.200}")
    return value


_CHECKS = {"command": _command, "pytest": _pytest, "script": _script, "python": _python, "expect": _expect}
