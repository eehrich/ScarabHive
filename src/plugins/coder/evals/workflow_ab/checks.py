"""Scores a run of the suite coder-workflow-ab: the template's tests in the run's workspace, and the hidden tests
beside this file -- the cases the task states that tests/ does not show, which a review is for.

The score is the share of all those tests that pass (``of``: their number, so the report pools by test). The
number is the task's own (TESTS), not what pytest reports: a file that cannot import -- a module the run never
wrote -- is one error however many tests it holds, and a run that wrote nothing would beat one that began; a
skip would lower the count too. A part that reports more than that counts what it reports. The
detail names both parts and how many workflow runs the agent started. A run that changed or deleted a test of
the template, or added a conftest.py, scores 0. pytest runs as agent_eval's pytest check runs it: confined to the
workspace (rootdir, confcutdir, an ini beside it), ``-P`` so the workspace cannot shadow pytest.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
#: Per task: its hidden tests, below this directory.
HIDDEN = {"currencies": "hidden/currency", "bugs": "hidden/bugs"}
#: Per task: how many tests its tests/ and its hidden tests hold, once every module they import is there.
TESTS = {"currencies": (7, 14), "bugs": (3, 7)}
TIMEOUT_S = 300


def score(workspace: Path, run: dict[str, Any], task: Any) -> dict[str, Any]:
    workspace = Path(workspace)
    template = HERE / task.workspace
    tampered = _tampered(template, workspace)
    if tampered:
        return {"score": 0.0, "passed": False, "detail": f"test files changed by the run: {', '.join(tampered)}"}
    visible = sorted(p.relative_to(template).as_posix() for p in (template / "tests").glob("test_*.py"))
    # Into the workspace, now that the run is over: below confcutdir, or the checkout's conftest.py above the
    # runs' data directory would take part. Whatever the run left under that name goes first.
    hidden_dir = workspace / "_hidden_tests"
    shutil.rmtree(hidden_dir, ignore_errors=True)
    shutil.copytree(HERE / HIDDEN[task.id], hidden_dir)
    hidden = sorted(p.relative_to(workspace).as_posix() for p in hidden_dir.glob("test_*.py"))
    (shown_passed, shown_total), shown_out = _pytest(workspace, visible, "visible")
    (secret_passed, secret_total), secret_out = _pytest(workspace, hidden, "hidden")
    shown = shown_passed, max(shown_total, TESTS[task.id][0])
    secret = secret_passed, max(secret_total, TESTS[task.id][1])
    total = shown[1] + secret[1]
    passed = shown[0] + secret[0]
    workflows = len(run.get("stategraph_runs") or [])
    detail = (f"tests/ {shown[0]}/{shown[1]}, hidden {secret[0]}/{secret[1]}; workflow runs: {workflows}\n"
              f"{shown_out[-1500:]}\n{secret_out[-1500:]}")
    if total <= 0:
        return {"score": 0.0, "passed": False, "detail": f"no tests ran\n{detail}"}
    return {"score": passed / total, "passed": passed == total, "of": total, "detail": detail}


#: Tool-name endings: a lookup that changes nothing, and an edit through the file tools.
LOOKUPS = ("_read_file", "_list_directory", "_search_files", "_grep_search", "_semantic_search")
EDITS = ("_replace_string_in_file", "_manage")


def score_with_habits(workspace: Path, run: dict[str, Any], task: Any) -> dict[str, Any]:
    """``score``, with how the run worked as the detail's first line (suite coder-prompt-ab): the steps of the
    suite's agent, how many of them were a lone lookup right after another lone lookup -- they could have gone out
    together --, and how many edits were read back in the very next step."""
    result = score(workspace, run, task)
    result["detail"] = f"{habits(_messages(run))}\n{result['detail']}"
    return result


def habits(messages: list[dict[str, Any]]) -> str:
    """The model's own steps only: a call the loop or a plugin put in (the preloaded search, ``injected_by``) is
    no step it took."""
    steps = calls = lone_after_lone = edits = read_back = 0
    lone_before = False
    edited: list[tuple[str, ...]] = []
    for message in messages:
        made = [call.get("function") or {} for call in message.get("tool_calls") or []]
        if message.get("role") != "assistant" or not made or message.get("injected_by"):
            continue
        steps += 1
        calls += len(made)
        lone = len(made) == 1 and str(made[0].get("name", "")).endswith(LOOKUPS)
        lone_after_lone += lone and lone_before
        lone_before = lone
        now_edited: list[tuple[str, ...]] = []
        for call in made:
            name, target = str(call.get("name", "")), _target(call.get("arguments"))
            if name.endswith("_read_file") and target and any(_same_file(target, done) for done in edited):
                read_back += 1
            if name.endswith(EDITS) and target:
                edits += 1
                now_edited.append(target)
        edited = now_edited
    return (f"habits: steps {steps}, calls {calls}, lone lookups after a lone lookup {lone_after_lone}, "
            f"edits {edits}, read back {read_back}")


def _target(arguments: Any) -> tuple[str, ...]:
    """The parts of the path a call names; none where it names no file."""
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
    except ValueError:
        return ()
    path = parsed.get("filePath") or parsed.get("path") or "" if isinstance(parsed, dict) else ""
    return tuple(part for part in str(path).replace("\\", "/").split("/") if part not in ("", "."))


def _same_file(one: tuple[str, ...], other: tuple[str, ...]) -> bool:
    """The same file is written with and without its folders: the shorter path is the end of the longer one."""
    short, long = sorted((one, other), key=len)
    return bool(short) and long[len(long) - len(short):] == short


def _messages(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The messages of the run's own session, as stored in its data directory; none where it left no session."""
    for path in Path(run.get("data_dir") or ".").rglob(f"{run.get('session_id')}.json"):
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(stored, dict) and isinstance(stored.get("messages"), list):
            return [message for message in stored["messages"] if isinstance(message, dict)]
    return []


def _pytest(workspace: Path, paths: list[str], name: str) -> tuple[tuple[int, int], str]:
    """(passed, total) and the output of pytest over ``paths``, run in ``workspace``."""
    report = workspace.parent / f"pytest-{name}.xml"
    report.unlink(missing_ok=True)
    ini = workspace.parent / "pytest-eval.ini"
    ini.write_text(f'[pytest]\npythonpath = "{workspace.as_posix()}"\n', encoding="utf-8")
    args = [sys.executable, "-P", "-m", "pytest", *paths, "-q", "-p", "no:cacheprovider", f"--rootdir={workspace}",
            f"--confcutdir={workspace}", "-c", str(ini), f"--junitxml={report}", "--continue-on-collection-errors"]
    try:
        done = subprocess.run(args, cwd=workspace, capture_output=True, text=True, timeout=TIMEOUT_S,
                              env={**os.environ, "AGENT_SYSTEM_TEST_NO_REAP": "1", "PYTHONIOENCODING": "utf-8"})
        output = done.stdout + done.stderr
    except subprocess.TimeoutExpired:
        return (0, len(paths)), f"{name}: timed out after {TIMEOUT_S}s"
    if not report.exists():
        return (0, len(paths)), f"{name}: no report\n{output}"
    total = failed = 0
    for element in ET.parse(report).getroot().iter("testsuite"):
        total += int(element.get("tests", 0)) - int(element.get("skipped", 0))
        failed += int(element.get("failures", 0)) + int(element.get("errors", 0))
    if total == 0:
        return (0, len(paths)), f"{name}: no test ran\n{output}"
    return (total - failed, total), output


def _tampered(template: Path, workspace: Path) -> list[str]:
    """Test material of the template the run changed or removed, and any conftest.py it added."""
    out = []
    for src in template.rglob("*"):
        rel = src.relative_to(template)
        if src.is_file() and "__pycache__" not in rel.parts and (
                rel.parts[0] == "tests" or rel.name in ("pytest.ini", "conftest.py")):
            mine = workspace / rel
            if not mine.is_file() or mine.read_bytes() != src.read_bytes():
                out.append(rel.as_posix())
    out += [p.relative_to(workspace).as_posix() for p in workspace.rglob("conftest.py")
            if not (template / p.relative_to(workspace)).is_file()]
    return sorted(set(out))
