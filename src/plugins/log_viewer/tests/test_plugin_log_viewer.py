"""Log Viewer without a browser: discovery, routes, and what the panel's two calls answer, against log files in tmp_path."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.config.models import AgentConfig, AgentSystemConfig, ToolServerConfig
from agent_system.plugins.web_adapter import PluginWebRegistry
from plugins.log_viewer import endpoints
from plugins.log_viewer.plugin import PLUGIN_FACTORY, LogViewerHybridPlugin

APP_LOG = """\
2026-01-01 10:00:00,000 INFO agent_system.api Server started
2026-01-01 10:00:01,000 DEBUG agent_system.api GET /plugins/log_viewer/logs/list 200
2026-01-01 10:00:02,000 ERROR agent_system.core Tool failed
Traceback (most recent call last):
  File "core.py", line 7, in run
ValueError: Bad Budget
2026-01-01 10:00:03,000 WARNING agent_system.llm Retrying request
2026-01-01 10:00:04,123 CRITICAL agent_system.app Out of memory
"""


def make_plugin(names):
    config = ToolServerConfig(type="log_viewer", enabled=True, agent_config=AgentConfig())
    if names is not None:
        config.log_files = names
    return LogViewerHybridPlugin("log_viewer", AgentSystemConfig(), config)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "app.log").write_bytes(APP_LOG.encode())
    registry = PluginWebRegistry()
    registry.register_web_plugin("log_viewer", make_plugin(["logs/app.log", "logs/missing.log"]))
    app = FastAPI()
    registry.apply_to_app(app)
    return TestClient(app)


def content(client, name="logs/app.log", **params):
    return client.get(f"/plugins/log_viewer/logs/content/{name}", params=params)


def test_log_viewer_plugin_discovered():
    from agent_system.plugins import discover_all_plugins

    plugins = discover_all_plugins([Path(__file__).resolve().parents[2]])
    assert plugins["log_viewer"] is PLUGIN_FACTORY


def test_router_serves_the_panel_its_two_calls_and_its_static_files(client):
    router = make_plugin([]).get_web_router()
    assert sorted(route.path for route in router.routes) == [
        "/plugins/log_viewer/", "/plugins/log_viewer/logs/content/{log_name:path}", "/plugins/log_viewer/logs/list"]
    page = client.get("/plugins/log_viewer/")
    assert page.status_code == 200 and "/static/kit/kit.css" in page.text and "/plugins/log_viewer/static/panel.js" in page.text
    assert client.get("/plugins/log_viewer/static/panel.js").status_code == 200
    assert client.get("/plugins/log_viewer/static/panel.css").status_code == 200


def test_list_names_every_configured_file(client):
    logs = client.get("/plugins/log_viewer/logs/list").json()["logs"]
    assert [(log["name"], log["exists"]) for log in logs] == [("logs/app.log", True), ("logs/missing.log", False)]
    assert logs[0]["size"] == len(APP_LOG.encode()) and logs[0]["rotation_count"] == 1


# slashes encoded: the client would resolve the dot segments before the server sees them
@pytest.mark.parametrize("name", ["logs%2Fother.log", "..%2Flogs%2Fapp.log", "logs%2F..%2Flogs%2Fapp.log", "logs%2Fapp.log.1",
                                  "%2Fetc%2Fpasswd"])
def test_a_name_that_is_not_configured_is_refused_with_404(client, name, tmp_path):
    (tmp_path / "logs" / "other.log").write_bytes(APP_LOG.encode())
    (tmp_path / "logs" / "app.log.1").write_bytes(APP_LOG.encode())
    answer = content(client, name)
    assert answer.status_code == 404 and "not a configured log file" in answer.json()["detail"]


def test_a_configured_file_that_does_not_exist_is_404(client):
    answer = content(client, "logs/missing.log")
    assert answer.status_code == 404 and "does not exist" in answer.json()["detail"]


@pytest.mark.parametrize("params", [{"lines": 0}, {"lines": 5001}, {"levels": "error,loud"}])
def test_bad_parameters_are_refused_with_422(client, params):
    assert content(client, **params).status_code == 422


def test_entries_keep_their_continuation_lines_and_skip_the_panels_own_requests(client):
    entries = content(client).json()["entries"]
    assert [entry["level"] for entry in entries] == ["info", "error", "warning", "critical"]
    assert entries[1] == {"timestamp": "2026-01-01 10:00:02,000", "level": "error", "message":
                          'agent_system.core Tool failed\nTraceback (most recent call last):\n  File "core.py", line 7, in run\nValueError: Bad Budget'}
    assert entries[3]["timestamp"] == "2026-01-01 10:00:04,123" and entries[3]["message"] == "agent_system.app Out of memory"


def test_the_panels_own_requests_are_left_out_only_as_routine(client, tmp_path):
    (tmp_path / "logs" / "app.log").write_bytes(
        b"2026-09-14 16:53:40 | INFO | ALLOWED | GET /plugins/log_viewer/logs/list | user=admin\n"
        b"2026-09-14 16:53:41 | WARNING | DENIED | GET /plugins/log_viewer/logs/list | user=bob\n"
        b"2026-09-14 16:53:42 ERROR agent_system.api GET /plugins/log_viewer/logs/content/x failed\n"
        b"2026-09-14 16:53:43 DEBUG uvicorn.access GET /plugins/log_viewer/ 200\n")
    assert [e["level"] for e in content(client).json()["entries"]] == ["warning", "error"]
    assert [e["level"] for e in content(client, search="denied").json()["entries"]] == ["warning"]


def test_without_configured_files_tools_and_panel_share_one_allowlist():
    from plugins.log_viewer.tool_server import DEFAULT_LOG_FILES

    plugin = make_plugin(None)
    assert plugin.tool_server.log_files == DEFAULT_LOG_FILES == ["logs/api.log", "logs/cli.log", "logs/profiling.log", "logs/security.log"]
    assert plugin.web_endpoints.log_files is plugin.tool_server.log_files and plugin.log_files is plugin.tool_server.log_files


def test_levels_search_and_count_filter_the_newest_entries(client):
    assert [e["level"] for e in content(client, levels="error,critical").json()["entries"]] == ["error", "critical"]
    assert [e["level"] for e in content(client, search="bad budget").json()["entries"]] == ["error"]  # found in the traceback
    assert [e["level"] for e in content(client, lines=2).json()["entries"]] == ["warning", "critical"]
    assert content(client, levels="debug").json()["entries"] == []  # the only debug entry is the panel's own request


@pytest.mark.parametrize("line, expected", [
    ("2026-01-05 20:47:21 INFO [agent_system.profiling] Profiling started",
     {"timestamp": "2026-01-05 20:47:21", "level": "info", "message": "[agent_system.profiling] Profiling started"}),
    ("2025-09-25 00:23:32,790 - agent_system.api - WARNING - Slow answer",
     {"timestamp": "2025-09-25 00:23:32,790", "level": "warning", "message": "agent_system.api Slow answer"}),
    ("2026-09-14 16:53:40 | ERROR | DENIED | GET /admin | user=bob",
     {"timestamp": "2026-09-14 16:53:40", "level": "error", "message": "DENIED | GET /admin | user=bob"}),
    ("2026-09-14 16:53:40 WARN worker Busy", {"timestamp": "2026-09-14 16:53:40", "level": "warning", "message": "worker Busy"}),
    ("2026-09-14 16:53:40 FATAL worker Gone", {"timestamp": "2026-09-14 16:53:40", "level": "critical", "message": "worker Gone"}),
    ("2026-09-14 16:53:40 Information without a level",
     {"timestamp": "2026-09-14 16:53:40", "level": None, "message": "Information without a level"}),
])
def test_every_known_line_format_yields_timestamp_level_and_message(line, expected):
    assert endpoints.parse_entry(line) == expected


def test_rotations_are_read_newest_first_and_returned_oldest_first(client, tmp_path):
    logs = tmp_path / "logs"
    (logs / "app.log.2").write_bytes(b"started before logging\n2025-12-31 09:00:00,000 INFO a Oldest\nwithout a timestamp\n")
    (logs / "app.log.1").write_bytes(b"2025-12-31 10:00:00,000 INFO a Older\n")
    entries = content(client, lines=100).json()["entries"]
    assert [e["message"] for e in entries[:3]] == ["started before logging", "a Oldest\nwithout a timestamp", "a Older"]
    assert len(entries) == 7 and entries[0]["timestamp"] is None
    assert [e["message"] for e in content(client, lines=5).json()["entries"]][0] == "a Older"
    assert "started before logging" not in str(content(client, lines=100, levels="info").json())
    assert client.get("/plugins/log_viewer/logs/list").json()["logs"][0]["rotation_count"] == 3


def test_lines_are_read_backwards_across_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(endpoints, "CHUNK_BYTES", 7)
    path = tmp_path / "x.log"
    path.write_bytes("first line\r\nsecond ünïcode line\r\n\r\nlast without newline".encode())
    assert list(endpoints.lines_backwards(path)) == ["last without newline", "", "second ünïcode line", "first line"]


# ------------------------------------------------------------------------- the tools

def tool(plugin, name, **params):
    import asyncio

    return asyncio.run(plugin.call_tool(f"log_viewer_{name}", params))


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "a.log").write_text("".join(f"line {n} ERROR x\n" for n in range(10)))
    (tmp_path / "logs" / "b.log").write_text("ERROR in b\n")
    (tmp_path / "logs" / "secret.log").write_text("ERROR secret\n")
    return make_plugin(["logs/a.log", "logs/missing.log", "logs/b.log"])


def test_tool_descriptions_name_the_instance(tools):
    described = str(tools.get_tools())
    assert "log_viewer_list names it" in described and "{{" not in described


def test_tail_returns_the_newest_lines_and_refuses_a_count_below_one(tools):
    answer = tool(tools, "tail", log_file="logs/a.log", lines=3)
    assert answer["lines"] == ["line 7 ERROR x", "line 8 ERROR x", "line 9 ERROR x"] and answer["total_lines"] == 10
    assert tool(tools, "tail", log_file="logs/a.log", lines=50)["returned_lines"] == 10
    for lines in (0, -3, "3", True):
        assert "lines must be a whole number" in tool(tools, "tail", log_file="logs/a.log", lines=lines)["error"]


def test_tail_refuses_an_unconfigured_or_missing_file(tools):
    assert "not in allowed list" in tool(tools, "tail", log_file="logs/secret.log")["error"]
    assert "not found" in tool(tools, "tail", log_file="logs/missing.log")["error"]
    assert tool(tools, "tail")["error"] == "Missing required parameter: log_file"


def test_search_refuses_a_named_file_it_would_not_read(tools):
    assert "not in allowed list" in tool(tools, "search", pattern="error", log_file="logs/secret.log")["error"]
    assert "not found" in tool(tools, "search", pattern="error", log_file="logs/missing.log")["error"]


def test_search_stops_at_max_results_and_counts_the_files_it_read(tools):
    everything = tool(tools, "search", pattern="ERROR")
    assert (everything["total_matches"], everything["files_searched"], everything["truncated"]) == (11, 2, False)
    first = tool(tools, "search", pattern="error", max_results=3)
    assert [r["line_number"] for r in first["results"]] == [1, 2, 3]
    assert (first["files_searched"], first["truncated"]) == (1, True)  # b.log was never opened
    for max_results in (0, -1):
        assert "max_results must be" in tool(tools, "search", pattern="x", max_results=max_results)["error"]


def test_search_an_unreadable_file_uses_up_no_result(tools, monkeypatch):
    real_open = open

    def failing_open(path, *args, **kwargs):
        if str(path).endswith("a.log"):
            raise PermissionError("locked")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", failing_open)
    answer = tool(tools, "search", pattern="error", max_results=1)
    assert answer["results"][0]["error"] == "Failed to read file: locked"
    assert answer["results"][1]["line"] == "ERROR in b" and answer["total_matches"] == 1
    assert answer["files_searched"] == 1  # only b.log was read
