"""The app's static files and the plugins' come with one cache rule: a page never mixes a fresh kit with a panel
script the browser kept on its own."""
import os
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.app import build_app
from agent_system.plugins.web_adapter import PluginWebRegistry

PLUGIN_STATIC = Path(__file__).resolve().parents[2] / "src" / "plugins" / "message_debugger" / "static"


def app_with(tmp_path, monkeypatch, disable_cache):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"network": {"host": "127.0.0.1", "port": 8000, "disable_cache": disable_cache}}),
                      encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    app = build_app(str(config))  # not started: /static is mounted while the app is built
    registry = PluginWebRegistry()  # as the start mounts a plugin's static folder: the rule comes from the app
    registry.static_mounts["message_debugger"] = PLUGIN_STATIC
    registry.apply_to_app(app)
    return TestClient(app)


@pytest.mark.parametrize("disable_cache, rule", [(True, "no-cache"), (False, None)])
def test_the_kit_and_a_panel_script_come_with_the_same_cache_rule(tmp_path, monkeypatch, disable_cache, rule):
    client = app_with(tmp_path, monkeypatch, disable_cache)
    for path in ("/static/kit/panel-kit.js", "/plugins/message_debugger/static/panel.js"):
        answer = client.get(path)
        assert (answer.status_code, answer.headers.get("cache-control")) == (200, rule), path
        # asked again, an unchanged file is a short 304, not the file once more
        again = client.get(path, headers={"If-None-Match": answer.headers["etag"]})
        assert (again.status_code, again.content) == (304, b""), path


def test_a_file_replaced_within_the_same_second_or_by_an_older_one_is_sent_again(tmp_path, monkeypatch):
    """The browser sends its ETag and the time it has: a changed ETag decides, whatever the time says."""
    served = tmp_path / "served"
    served.mkdir()
    script = served / "panel.js"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"network": {"disable_cache": True}}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    app = build_app(str(config))
    registry = PluginWebRegistry()
    registry.static_mounts["probe"] = served
    registry.apply_to_app(app)
    client = TestClient(app)

    def write(text, when):
        script.write_text(text, encoding="utf-8")
        os.utime(script, (when, when))

    write("first", 1_700_000_000.2)
    first = client.get("/plugins/probe/static/panel.js")
    asked = {"If-None-Match": first.headers["etag"], "If-Modified-Since": first.headers["last-modified"]}
    # same second; an older copy -- each of another size, so the ETag differs where a file system keeps whole seconds
    for text, when in (("again!", 1_700_000_000.6), ("old", 1_600_000_000.0)):
        write(text, when)
        answer = client.get("/plugins/probe/static/panel.js", headers=asked)
        assert (answer.status_code, answer.text) == (200, text)
    write("first", 1_700_000_000.2)  # unchanged against what the browser has: still a 304
    assert client.get("/plugins/probe/static/panel.js", headers=asked).status_code == 304


def test_an_app_that_names_no_rule_leaves_the_plugin_files_as_they_were():
    app = FastAPI()
    registry = PluginWebRegistry()
    registry.static_mounts["message_debugger"] = PLUGIN_STATIC
    registry.apply_to_app(app)
    assert "cache-control" not in TestClient(app).get("/plugins/message_debugger/static/panel.js").headers
