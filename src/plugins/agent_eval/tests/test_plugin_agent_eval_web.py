"""The Evals panel's API: which suites it offers, what it starts, how it reads runs."""
from __future__ import annotations

import os
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from agent_system.paths import DATA_DIR_ENV
from plugins.agent_eval import runner, store, web
from plugins.agent_eval.plugin import AgentEvalPlugin

PLUGIN = Path(__file__).resolve().parents[1]
HELLO = str((PLUGIN / "evals" / "hello" / "suite.yaml").resolve())


@pytest.fixture
def endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "data"))
    config = SimpleNamespace(plugins=SimpleNamespace(plugin_dirs=[str(PLUGIN.parent)]),
                             _source_path=str(tmp_path / "named-on-the-command-line.yaml"))
    plugin = AgentEvalPlugin("agent_eval", config, SimpleNamespace())
    return plugin.web_endpoints


def _finished_run(tmp_path, monkeypatch, suite="s", note="") -> str:
    monkeypatch.setattr(runner, "_agent_run", lambda *a: {"status": "done", "answer": "ok", "cost": 0.5})
    monkeypatch.setattr(runner, "check_arms", lambda suite: None)
    path = tmp_path / "suite.yaml"
    path.write_text(textwrap.dedent(f"""
        # {note}
        name: {suite}
        agent: a
        repeats: 1
        arms: {{one: {{}}, two: {{}}}}
        tasks:
          - {{id: t1, input: x, check: {{expect: {{contains: [ok]}}}}}}
    """), encoding="utf-8")
    return runner.run_suite(path, progress=lambda _: None)


def test_every_route_of_the_schema_has_its_handler(endpoints):
    paths = {(r.path, m) for r in endpoints.get_web_router().routes for m in r.methods}
    assert ("/plugins/agent_eval/runs", "POST") in paths and ("/plugins/agent_eval/runs/{run_id}", "GET") in paths


async def test_offers_the_suites_below_evals(endpoints):
    suites = (await endpoints.list_suites(None))["suites"]
    hello = next(s for s in suites if s["path"] == HELLO)
    assert hello["name"] == "hello" and hello["arms"] == ["base"]


async def test_starts_only_a_suite_it_offers(endpoints, tmp_path, monkeypatch):
    started = []
    monkeypatch.delenv("AGENT_CONFIG_PATH", raising=False)

    reaped = []

    def popen(args, **kw):
        started.append((args, kw))
        return SimpleNamespace(pid=4242, wait=lambda: reaped.append(True))
    monkeypatch.setattr(web.subprocess, "Popen", popen)
    # A valid suite, only not one the discovery found: its checks would run whatever it names.
    elsewhere = tmp_path / "suite.yaml"
    elsewhere.write_text("name: s\nagent: a\ntasks:\n  - {id: t, input: x, check: {command: 'true'}}\n",
                         encoding="utf-8")
    with pytest.raises(HTTPException) as refused:
        await endpoints.start_run(None, web.StartRun(suite=str(elsewhere)))
    assert refused.value.status_code == 400 and not started
    with pytest.raises(HTTPException):
        await endpoints.start_run(None, web.StartRun(suite=HELLO, arms=["nope"]))
    run_id = (await endpoints.start_run(None, web.StartRun(suite=HELLO)))["run_id"]
    (args, kw), = started
    assert args[1:] == ["-m", "plugins.agent_eval", "run", HELLO, "--run-id", run_id]
    assert kw["cwd"] == web.PROJECT_ROOT
    # The API's own configuration reaches the runner, also one named on the API's command line.
    assert kw["env"]["AGENT_CONFIG_PATH"].endswith("named-on-the-command-line.yaml")
    # No console window: neither its own nor, through it, one per agent run.
    assert kw == {**kw, **web.GROUP}
    assert (web.store.root() / "logs" / f"{run_id}.pid").read_text() == "4242"
    import time
    deadline = time.monotonic() + 5
    while not reaped and time.monotonic() < deadline:
        time.sleep(0.01)
    assert reaped, "the runner is never waited for: a zombie would read as running for ever"


async def test_refused_runs_are_listed_as_not_counted(endpoints, tmp_path, monkeypatch):
    _finished_run(tmp_path, monkeypatch)  # writes the suite and stands in for the arm check
    monkeypatch.setattr(runner, "_agent_run", lambda *a: {"status": "refused", "answer": "", "cost": 0.0})
    refused = runner.run_suite(tmp_path / "suite.yaml", progress=lambda _: None)
    rep = await endpoints.get_run(None, refused)
    assert rep["skipped"]["refused"] == 2 and rep["arms"]["one"]["runs"] == 0


async def test_a_run_that_never_started_shows_why(endpoints):
    logs = web.store.root() / "logs"
    logs.mkdir(parents=True)
    (logs / "20261026-120000-dead.pid").write_text(str(2 ** 22 + 7), encoding="utf-8")
    (logs / "20261026-120000-dead.log").write_text("Error: arm think: unknown llm_profile ['x']\n", encoding="utf-8")
    with pytest.raises(HTTPException) as e:
        await endpoints.get_run(None, "20261026-120000-dead")
    assert e.value.status_code == 500 and "unknown llm_profile" in e.value.detail
    (logs / "20261026-120000-busy.pid").write_text(str(os.getpid()), encoding="utf-8")
    with pytest.raises(HTTPException) as e:
        await endpoints.get_run(None, "20261026-120000-busy")
    assert e.value.status_code == 404, "a runner still setting up is starting, not failed"


async def test_only_a_finished_run_becomes_the_baseline(endpoints, tmp_path, monkeypatch):
    run_id = _finished_run(tmp_path, monkeypatch)
    meta = web.store.read_meta(run_id)
    web.store.write_meta(run_id, {**meta, "status": "stopped"})
    with pytest.raises(HTTPException) as e:
        await endpoints.set_baseline(None, run_id)
    assert e.value.status_code == 409 and web.store.baseline_for("s") is None


async def test_a_baseline_of_another_suite_or_suite_file_is_told(endpoints, tmp_path, monkeypatch):
    first = _finished_run(tmp_path, monkeypatch, note="first version")
    changed = _finished_run(tmp_path, monkeypatch, note="second version")
    other = _finished_run(tmp_path, monkeypatch, suite="other")
    rep = await endpoints.get_run(None, changed, against=first)
    assert rep["warnings"] and "suite file changed" in rep["warnings"][0]
    with pytest.raises(HTTPException) as e:
        await endpoints.get_run(None, other, against=first)
    assert e.value.status_code == 400


async def test_reads_a_run_and_compares_against_its_baseline(endpoints, tmp_path, monkeypatch):
    first = _finished_run(tmp_path, monkeypatch)
    second = _finished_run(tmp_path, monkeypatch)
    rep = await endpoints.get_run(None, second)
    assert {r["arm"] for r in rep["records"]} == {"one", "two"} and "against" not in rep
    assert rep["arms"]["two"]["learn"]["tasks"] == 1
    await endpoints.set_baseline(None, first)
    rep = await endpoints.get_run(None, second, against="baseline")
    assert rep["against"] == first and rep["baseline"] == first
    # The baseline itself is read against its own reference arm, not against itself.
    assert "against" not in await endpoints.get_run(None, first, against="baseline")


async def test_a_bad_run_id_is_not_found(endpoints):
    for bad in ("../../etc", "20990101-000000-ffff"):
        with pytest.raises(HTTPException) as e:
            await endpoints.get_run(None, bad)
        assert e.value.status_code == 404


async def test_a_runner_that_died_is_not_shown_running(endpoints, tmp_path, monkeypatch):
    run_id = _finished_run(tmp_path, monkeypatch)
    meta = store.read_meta(run_id)
    store.write_meta(run_id, {**meta, "status": "running", "pid": 2 ** 22 + 7})
    (run,) = (await endpoints.list_runs(None))["runs"]
    assert run["status"] == "died" and run["scored"] == 2 and run["cost"] == 1.0


async def test_stop_marks_the_run(endpoints, tmp_path, monkeypatch):
    run_id = _finished_run(tmp_path, monkeypatch)
    await endpoints.stop_run(None, run_id)
    assert store.stop_requested(run_id)


def test_panel_renders(endpoints):
    app = FastAPI()
    app.include_router(endpoints.get_web_router())
    page = TestClient(app).get("/plugins/agent_eval/")
    assert page.status_code == 200 and "/plugins/agent_eval/static/panel.js" in page.text
