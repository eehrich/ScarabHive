"""Batch Monitor without a browser: discovery, routes, and what the two endpoints answer (the panel test seeds the manager)."""

from pathlib import Path

import pytest
from fastapi import HTTPException

from agent_system.llm.batch import initialization
from plugins.batch_monitor.server import BatchMonitorWebFactory
from plugins.batch_monitor.tests.test_plugin_batch_monitor_panel import requests, seeded_manager


@pytest.fixture
def factory():
    return BatchMonitorWebFactory("batch_monitor", {}, {})


@pytest.fixture
def manager(monkeypatch, tmp_path):
    manager = seeded_manager(tmp_path)
    monkeypatch.setattr(initialization, "_batch_queue_manager", manager)
    return manager


def test_batch_monitor_plugin_discovered():
    from agent_system.plugins import discover_all_plugins

    plugins = discover_all_plugins([Path(__file__).resolve().parents[2]])
    assert isinstance(plugins["batch_monitor"]("batch_monitor", {}, {}), BatchMonitorWebFactory)


def test_router_serves_the_panel_and_its_two_calls(factory):
    router = factory.get_web_router()
    assert router.prefix == "/plugins/batch_monitor"
    assert sorted(route.path for route in router.routes) == ["/plugins/batch_monitor/", "/plugins/batch_monitor/metrics",
                                                             "/plugins/batch_monitor/queues"]
    assert factory.get_static_assets() == Path(factory.plugin_dir, "static")


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["get_queues", "get_metrics"])
async def test_without_a_batch_manager_the_endpoints_answer_503(factory, monkeypatch, endpoint):
    monkeypatch.setattr(initialization, "get_batch_queue_manager", lambda: None)
    with pytest.raises(HTTPException) as refused:
        await getattr(factory, endpoint)(None)
    assert refused.value.status_code == 503


@pytest.mark.asyncio
async def test_queues_list_pending_running_and_recently_finished(factory, manager):
    answer = await factory.get_queues(None)
    assert answer["collection_window_seconds"] == 30.0
    queues = {queue["queue_key"]: queue for queue in answer["queues"]}
    assert list(queues) == ["anthropic:claude-x", "gemini:flash", "mistral:large", "openai:gpt-4o"]
    assert [job["job_id"] for job in queues["anthropic:claude-x"]["jobs"]] == ["job-rec"]
    assert [job["job_id"] for job in queues["gemini:flash"]["jobs"]] == ["job-done"]
    assert queues["anthropic:claude-x"]["pending_requests"] == 1 and queues["anthropic:claude-x"]["pending_estimated_tokens"] == 1216
    running, unsubmitted = queues["openai:gpt-4o"]["jobs"]
    assert running["elapsed_seconds"] in (125, 126) and running["total_requests"] == 5 and running["failed_count"] == 1
    assert unsubmitted["status"] == "pending" and unsubmitted["elapsed_seconds"] is None
    assert queues["gemini:flash"]["jobs"][0]["elapsed_seconds"] == 100
    assert queues["anthropic:claude-x"]["jobs"][0]["total_requests"] == 0


@pytest.mark.asyncio
async def test_metrics_keep_a_processing_time_of_zero(factory, manager):
    answer = await factory.get_metrics(None)
    assert answer == {"completed_jobs": 3, "failed_jobs": 1, "total_completed_requests": 1200, "total_failed_requests": 7,
                      "processing_time": {"min": 0.0, "max": 3599.7, "mean": 1229.9}}


@pytest.mark.asyncio
async def test_queues_come_sorted_by_key(factory, monkeypatch, tmp_path):
    manager = seeded_manager(tmp_path)
    keys = [f"provider-{number}:model-{number * 7 % 13}" for number in range(13)]
    for key in keys:
        manager._queues[key] = requests(1, "m")
    monkeypatch.setattr(initialization, "_batch_queue_manager", manager)
    listed = [queue["queue_key"] for queue in (await factory.get_queues(None))["queues"]]
    assert listed == sorted(listed) and set(keys) <= set(listed)
