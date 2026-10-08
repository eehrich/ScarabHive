"""The State Graph panel served against the real plugin, for the guide's screenshot (src/scripts/guide_screenshots.py).

Seeded under ``root``: the machine ``build_review`` of the guide (an agent builds a change, a second one judges it,
a person approves) in the writable machine folder, and one mock-only run of it, ``demo_run``, that went through a
rejected review and a person's approval to its final state. Auth is off: one person's instance, nobody signs in.
No agent, tool or LLM is reached: the run is mock-only, and the served process starts nothing.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from agent_system.config.models import AgentSystemConfig
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.tests.stategraph_testkit import tool_config

MACHINE = """\
stategraph: 1
id: build_review
title: Build and review
description: An agent builds a change, a second one judges it, a person approves or sends it back.
group: Reviews
params:
  task: {type: string, required: true, description: What to build}
events:
  approve: {description: A person accepts the change}
  reject: {description: A person sends it back with notes, data: {type: string}}
context: {change: null, notes: null}
initial: build
states:
  build:
    max_visits: 3
    do:
      agent: coder
      task: "{{ params.task }} -- notes from the last review: {{ ctx.notes }}"
      timeout: 10m
    transitions:
      - target: review
        effect: ctx.change = out
      - trigger: error
        target: failed
  review:
    do:
      decide: noul
      by: reviewer
      question: Is this change ready to merge?
      input: "{{ ctx.change }}"
    transitions:
      - target: approval
        guard: out["value"] >= 0.7
      - target: build
        guard: else
  approval:
    timeout: 24h
    transitions:
      - trigger: approve
        target: done
      - trigger: reject
        target: build
        effect: ctx.notes = event.data
      - trigger: error
        target: failed
  done: {type: final, output: {change: "{{ ctx.change }}"}}
  failed: {type: final, status: failed}
"""

MOCKS = {"build": {"$visits": ["Retry the fetch three times, 2 s apart", "Retry the fetch three times and log each retry"]},
         "review": {"$visits": [{"value": 0.35, "confidence": 0.8}, {"value": 0.92, "confidence": 0.9}]}}


async def _seed(server: StateGraphServer) -> None:
    await server.service.start_run("build_review", params={"task": "Add a retry to the fetcher"}, mocks=MOCKS,
                                   mock_only=True, run_id="demo_run")
    for _ in range(200):  # to the approval's wait: the first review sends it back to build once
        row = server.run_store.get_run("demo_run")
        if row["status"] == "waiting":
            break
        await asyncio.sleep(0.02)
    server.service.send_event("demo_run", "approve")
    for _ in range(200):
        if server.run_store.get_run("demo_run")["status"] == "succeeded":
            break
        await asyncio.sleep(0.02)
    await server.run_manager.shutdown()
    server.run_store.close()


def readme_app(root: Path) -> FastAPI:
    """The same, laid out left to right: the README's picture is far wider than tall, and five states top down
    filled a third of it."""
    (root / "machines").mkdir(parents=True, exist_ok=True)
    (root / "machines" / "build_review.layout.json").write_text('{"auto": "classic", "version": 1}', encoding="utf-8")
    return panel_app(root)


def panel_app(root: Path) -> FastAPI:
    (root / "machines").mkdir(parents=True, exist_ok=True)
    (root / "machines" / "build_review.yaml").write_text(MACHINE, encoding="utf-8")
    asyncio.run(_seed(StateGraphServer("stategraph", AgentSystemConfig(), tool_config(root))))
    server = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(root))  # reads the seed from disk
    app = FastAPI()
    app.state.config = SimpleNamespace(auth=SimpleNamespace(enabled=False))
    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("stategraph", server)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
