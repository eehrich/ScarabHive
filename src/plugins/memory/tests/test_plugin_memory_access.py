"""Who may read and delete a session's memories: a user her own sessions', an admin all, everyone while
authentication is off. The panel read, searched and deleted any session whose id it was given."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.session_owners import admin, two_users, user, viewed_by

BASE = "/plugins/memory"


class Vectors:
    """The vector store's add/delete/query: a query finds every memory of the collection."""

    def __init__(self):
        self.collections: dict[str, dict[str, tuple[str, dict]]] = {}

    def add(self, collection, ids, documents, metadatas):
        self.collections.setdefault(collection, {}).update(zip(ids, zip(documents, metadatas)))

    def delete(self, collection, ids):
        for item in ids:
            self.collections.get(collection, {}).pop(item, None)

    def query(self, collection, query_text, n_results, include):
        hits = list(self.collections.get(collection, {}).items())[:n_results]
        return {"ids": [[item for item, _ in hits]], "documents": [[document for _, (document, _) in hits]],
                "metadatas": [[metadata for _, (_, metadata) in hits]], "distances": [[0.1 for _ in hits]]}


@pytest.fixture
def served(tmp_path, monkeypatch):
    from plugins.memory.plugin import PLUGIN_FACTORY

    two_users(tmp_path, monkeypatch)
    plugin = PLUGIN_FACTORY("memory", {}, SimpleNamespace(storage_path=str(tmp_path / "memories"),
                                                          auto_extract_keywords=False))
    plugin.server.vector_store = Vectors()
    for session_id in ("s-alice", "s-bob"):
        asyncio.run(plugin.server._operation_store(session_id=session_id, title=f"a memory of {session_id}",
                                                   content=f"what {session_id} keeps", importance=5))
    app = FastAPI()
    app.include_router(plugin.get_web_router())
    return TestClient(app), viewed_by(app), app


def _titles(client, session_id):
    return [memory["title"] for memory in client.get(f"{BASE}/memories?session_id={session_id}").json()["memories"]]


def _answers(client, session_id):
    """Everything the panel reads of a session."""
    return (client.get(f"{BASE}/memories?session_id={session_id}").json(),
            client.get(f"{BASE}/stats?session_id={session_id}").json(),
            client.post(f"{BASE}/memories/search?session_id={session_id}", json={"query": "keeps"}).json())


def test_her_own_session_s_memories_and_not_another_users(served):
    client, viewer, _app = served
    viewer["user"] = user("alice")

    assert _titles(client, "s-alice") == ["a memory of s-alice"]
    assert _titles(client, "s-bob") == [], "another user's memories were listed"
    assert client.get(f"{BASE}/stats?session_id=s-bob").json()["total_memories"] == 0
    assert client.post(f"{BASE}/memories/search?session_id=s-bob", json={"query": "keeps"}).json()["results"] == []
    # answered as a session that does not exist is, so the answer does not say whether it does
    foreign = _answers(client, "s-bob")
    viewer["user"] = admin()
    assert foreign == _answers(client, "s-nobody")


def test_another_users_memory_is_not_deleted(served):
    client, viewer, _app = served
    viewer["user"] = user("alice")

    refused = client.delete(f"{BASE}/memories/mem_001?session_id=s-bob")

    assert refused.status_code == 404
    viewer["user"] = admin()
    assert refused.json()["detail"] == client.delete(f"{BASE}/memories/mem_999?session_id=s-bob").json()[
        "detail"].replace("mem_999", "mem_001"), "answered otherwise than a memory that is not there"
    assert _titles(client, "s-bob") == ["a memory of s-bob"], "bob's memory was deleted"


def test_her_own_memory_is_deleted(served):
    client, viewer, _app = served
    viewer["user"] = user("alice")

    assert client.delete(f"{BASE}/memories/mem_001?session_id=s-alice").status_code == 200
    assert _titles(client, "s-alice") == []


def test_an_admin_reads_and_deletes_every_session_s(served):
    client, viewer, _app = served
    viewer["user"] = admin()

    assert _titles(client, "s-bob") == ["a memory of s-bob"]
    assert client.delete(f"{BASE}/memories/mem_001?session_id=s-bob").status_code == 200
    assert _titles(client, "s-bob") == []


def test_with_authentication_off_every_session_is_the_one_person_s(served):
    client, viewer, app = served
    app.state.config = SimpleNamespace(auth=SimpleNamespace(enabled=False))
    viewer["user"] = None

    assert _titles(client, "s-bob") == ["a memory of s-bob"]
    assert client.delete(f"{BASE}/memories/mem_001?session_id=s-bob").status_code == 200


def test_nobody_signed_in_sees_no_users_memories(served):
    """With authentication on, a request nobody signed in to is the viewer "anonymous": not alice."""
    client, viewer, _app = served
    viewer["user"] = None

    assert _titles(client, "s-alice") == []
    assert client.delete(f"{BASE}/memories/mem_001?session_id=s-alice").status_code == 404
