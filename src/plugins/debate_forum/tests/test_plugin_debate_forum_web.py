"""The panel's JSON API on the real plugin and database: what it answers, and how it refuses."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from plugins.debate_forum import web_endpoints
from plugins.debate_forum.tests.test_plugin_debate_forum_panel import panel_app

API = "/plugins/debate_forum/api"


@pytest.fixture
def client(tmp_path):
    return TestClient(panel_app(tmp_path))


def test_the_list_counts_every_match_of_status_group_and_search(client, monkeypatch):
    monkeypatch.setattr(web_endpoints, "CHANNELS_SHOWN", 1)
    listed = client.get(f"{API}/channels", params={"search": "i"}).json()  # synopsis, titles, <img ...>
    assert len(listed["channels"]) == 1
    assert listed["total"] == 3
    assert client.get(f"{API}/channels", params={"search": "Which"}).json()["total"] == 2  # topics
    assert client.get(f"{API}/channels", params={"search": "i", "group_id": 1, "status": "concluded"}).json()["total"] == 1


def test_messages_come_rendered_and_raw_and_as_text_without_a_converter(client, monkeypatch):
    messages = client.get(f"{API}/channels/1/messages").json()["messages"]
    assert messages[0]["content"] == "Synopsis A drags in the **middle**."
    assert messages[0]["content_html"] == "<p>Synopsis A drags in the <strong>middle</strong>.</p>"
    monkeypatch.setattr(web_endpoints, "markdown_to_html", lambda text: None)
    assert client.get(f"{API}/channels/3/messages").json()["messages"][0]["content_html"].startswith("&lt;script&gt;")


def test_a_verdict_summary_comes_rendered_from_the_summary_or_the_verdict(client):
    assert client.get(f"{API}/channels/2").json()["verdict_summary_html"] == "<p><strong>B</strong> wins</p>"
    assert client.get(f"{API}/channels/1").json()["verdict_summary_html"] is None
    client.post("/__stub/conclude", params={"channel": 1, "summary": ""})
    assert client.get(f"{API}/channels/1").json()["verdict_summary_html"] == "<p><em>From the verdict</em></p>"


def test_a_post_goes_into_the_latest_round_and_is_refused_when_it_cannot(client):
    answer = client.post(f"{API}/channels/1/messages", json={"agent_name": " Ann ", "content": " hi ", "agent_role": " "})
    assert answer.status_code == 200 and answer.json()["round"] == 2
    saved = client.get(f"{API}/channels/1/messages").json()["messages"][-1]
    assert (saved["agent_name"], saved["agent_role"], saved["content"], saved["round"]) == ("Ann", "user", "hi", 2)
    assert client.post(f"{API}/channels/2/messages", json={"agent_name": "A", "content": "x"}).status_code == 409
    assert client.post(f"{API}/channels/1/messages", json={"agent_name": " ", "content": "x"}).status_code == 422
    assert client.post(f"{API}/channels/99/messages", json={"agent_name": "A", "content": "x"}).status_code == 404


def test_channel_actions_refuse_what_they_cannot_do(client):
    assert client.post(f"{API}/channels/4/archive").status_code == 409
    assert client.post(f"{API}/channels/1/reopen").status_code == 409
    assert client.post(f"{API}/channels/1/archive").json() == {"status": "archived", "channel_id": 1}
    assert client.post(f"{API}/channels/1/reopen").json() == {"status": "active", "channel_id": 1}
    for method, path in (("post", "channels/99/archive"), ("post", "channels/99/reopen"), ("delete", "channels/99"),
                         ("get", "channels/99"), ("get", "channels/99/messages")):
        assert getattr(client, method)(f"{API}/{path}").status_code == 404, path
    assert client.post(f"{API}/messages/99/pin", json={"pinned": False}).status_code == 404
    assert client.post(f"{API}/channels", json={"name": "  "}).status_code == 422
    assert client.delete(f"{API}/channels/1").json() == {"status": "deleted", "channel_id": 1}
    assert client.get(f"{API}/channels/1").status_code == 404
