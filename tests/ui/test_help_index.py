"""The help's search by meaning (agent_system/ui/help_index.py), on a fake embedding model.

The fake embeds a bag of words, so a node ranks by the words it shares with the query -- enough to tell the
ranking, the index on disk and the fallback apart without the real model. What the real model finds is measured,
not tested (the 16 queries of 06.10.2026).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import math
import re
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.ui import help as help_module
from agent_system.ui import help_index
from agent_system.ui.help import Library
from agent_system.ui.routes import router
from agent_system.utils import vector_store

MANUAL = '@database "Probe manual"\n@node main "Probe manual"\nWelcome to the probe manual.\n@endnode\n'


class BagOfWords:
    """compute_embeddings' stand-in: one dimension per word, unit length; it keeps every text it was given."""

    def __init__(self, delay: float = 0.0):
        self.texts: list[str] = []
        self.delay = delay

    def __call__(self, texts, batch_size=128, normalize=True):
        time.sleep(self.delay)  # long enough for a second caller to arrive while the first embeds
        self.texts.extend(texts)
        rows = []
        for text in texts:
            row = [0.0] * vector_store.EMBEDDING_DIM
            for word in re.findall(r"\w+", text.lower()):
                row[int(hashlib.md5(word.encode()).hexdigest(), 16) % len(row)] += 1.0
            length = math.sqrt(sum(value * value for value in row)) or 1.0
            rows.append([value / length for value in row])
        return rows


def use_fake_model(monkeypatch, file, model=None):
    """A fresh index in ``file`` (another process, as far as it knows) on ``model``, a BagOfWords by default."""
    model = model or BagOfWords()
    monkeypatch.setattr(vector_store, "compute_embeddings", model)
    monkeypatch.setattr(help_index, "_index", help_index._Index(file))
    return model


@pytest.fixture
def plugins(tmp_path, monkeypatch):
    manual = tmp_path / "guides"
    manual.mkdir()
    (manual / "scarabhive.guide").write_text(MANUAL, encoding="utf-8")
    monkeypatch.setattr(help_module, "GUIDES_DIR", manual)
    root = tmp_path / "plugins"

    def plugin(name, guide):
        folder = root / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "plugin.toml").write_text(f'[plugin]\nname = "{name}"\ndescription = "Probe {name}"\n',
                                            encoding="utf-8")
        (folder / f"{name}.guide").write_text(guide, encoding="utf-8")
        return folder

    return root, plugin


def guide(*nodes: tuple[str, str, str]) -> str:
    return "".join(f'@node {name} "{title}"\n{text}\n@endnode\n' for name, title, text in nodes)


def search(root, query, **options):
    return asyncio.run(help_index.find_help(Library([root]), query, **options))


def test_the_closest_nodes_come_first_even_without_every_word(plugins, monkeypatch, tmp_path):
    root, plugin = plugins
    # the quokka far down the node: the snippet shows it, not the node's start
    plugin("zoo", guide(("main", "Zoo", "zebra " * 40 + "quokka"), ("cats", "Cats", "lion tiger"),
                        *[(f"n{n}", f"Filler {n}", f"filler words number {n}") for n in range(10)]))
    use_fake_model(monkeypatch, tmp_path / "index.json")

    result = search(root, "quokka xylophone", limit=3)

    # no node holds both words: the lexical search finds nothing, the ranking still finds the quokka
    assert Library([root]).search("quokka xylophone") == []
    assert "fallback" not in result
    assert [(hit["guide"], hit["node"]) for hit in result["hits"]][0] == ("zoo", "main")
    assert len(result["hits"]) == 3  # 13 nodes and more: the limit is what cuts
    scores = [hit["score"] for hit in result["hits"]]
    assert scores == sorted(scores, reverse=True) and scores[0] > scores[1]
    first = result["hits"][0]
    assert first["title"] == "Zoo" and first["database"] == "zoo"
    assert "quokka" in first["snippet"] and first["snippet"].startswith("…")
    assert result["query"] == "quokka xylophone" and result["exact"] is None


@pytest.mark.parametrize("query, exact", [
    ("zoo", ("zoo", "main")),                         # a guide's id
    ("Sub-Agent manager", ("sub_agent_manager", "main")),  # spaces, hyphens and underscores alike
    ("zoo/cats", ("zoo", "cats")),                    # guide/node
    ("big CATS", None),                               # a title only ranks: "Export" opened godot's node
    ("Setup", None),                                  # a title two guides share names neither
    ("zebra", None),
])
def test_a_query_naming_a_node_finds_it_exactly_and_still_ranks(plugins, monkeypatch, tmp_path, query, exact):
    root, plugin = plugins
    plugin("zoo", guide(("main", "Zoo", "zebra"), ("cats", "Big cats", "lion"), ("setup", "Setup", "keys")))
    plugin("sub_agent_manager", guide(("main", "SAM", "agents"), ("setup", "Setup", "spawn")))
    use_fake_model(monkeypatch, tmp_path / "index.json")

    result = search(root, query)

    assert (None if result["exact"] is None else (result["exact"]["guide"], result["exact"]["node"])) == exact
    if exact:
        assert result["exact"]["title"] == Library([root]).guides[exact[0]].nodes[exact[1]].title
    assert result["hits"]  # an exact node does not stop the ranking


def test_the_index_is_kept_on_disk_and_only_a_changed_guide_is_embedded_again(plugins, monkeypatch, tmp_path):
    root, plugin = plugins
    plugin("zoo", guide(("main", "Zoo", "zebra"), ("cats", "Cats", "lion")))
    folder = plugin("farm", guide(("main", "Farm", "cow"),))
    gone = plugin("gone", guide(("main", "Gone", "ghost"),))
    file = tmp_path / "index.json"
    first = use_fake_model(monkeypatch, file)
    search(root, "zebra")
    assert {text.split("\n")[0] for text in first.texts} >= {"Zoo", "Cats", "Farm", "Gone"}, "nothing embedded"

    again = use_fake_model(monkeypatch, file)  # a new process: the vectors come from the file
    top = search(root, "cow")["hits"][0]
    assert (top["guide"], top["node"]) == ("farm", "main")
    assert again.texts == ["cow"]  # the query, and not one node

    (folder / "farm.guide").write_text(guide(("main", "Farm", "cow sheep"), ("barn", "Barn", "hay")),
                                       encoding="utf-8")
    changed = use_fake_model(monkeypatch, file)
    assert search(root, "hay")["hits"][0]["guide"] == "farm"
    assert changed.texts[:-1] == ["Farm\ncow sheep", "Barn\nhay"]  # that guide, then the query

    for name in ("plugin.toml", "gone.guide"):
        (gone / name).unlink()
    search(root, "zebra")
    assert "gone" not in json.loads(file.read_text(encoding="utf-8"))["guides"]


def test_a_corrupt_index_file_is_built_again(plugins, monkeypatch, tmp_path):
    root, plugin = plugins
    plugin("zoo", guide(("main", "Zoo", "zebra"),))
    file = tmp_path / "index.json"
    file.write_text('{"format": 1, "guides": {"zoo": {"key": "x", "nodes": ["main"], "vectors": "AAAA"}}}',
                    encoding="utf-8")  # four bytes where a row of 384 floats belongs
    model = use_fake_model(monkeypatch, file)

    assert search(root, "zebra")["hits"][0]["guide"] == "zoo"
    assert "Zoo\nzebra" in model.texts
    stored = json.loads(file.read_text(encoding="utf-8"))
    assert stored["guides"]["zoo"]["nodes"] == ["main"] and len(stored["guides"]["zoo"]["vectors"]) > 4


def test_a_model_that_cannot_load_falls_back_on_the_words_and_says_so(plugins, monkeypatch, tmp_path):
    root, plugin = plugins
    plugin("zoo", guide(("main", "Zoo", "zebra quokka"), ("cats", "Cats", "lion")))
    file = tmp_path / "index.json"

    def offline(texts, batch_size=128, normalize=True):
        raise RuntimeError("The embedding model all-MiniLM-L6-v2 could not be readied: no network")

    use_fake_model(monkeypatch, file, offline)
    result = search(root, "Cats")

    assert result["fallback"] == "lexical"
    assert [(hit["guide"], hit["node"], hit["score"]) for hit in result["hits"]] == [("zoo", "cats", None)]
    assert result["exact"] is None and result["total"] == 1
    assert not file.exists()  # nothing half-built was kept


def test_after_a_failure_the_model_rests_before_it_is_tried_again(plugins, monkeypatch, tmp_path):
    """Offline, each try is a download attempt that holds the search for seconds."""
    root, plugin = plugins
    plugin("zoo", guide(("main", "Zoo", "zebra"), ("cats", "Cats", "lion")))
    tries = []

    def offline(texts, batch_size=128, normalize=True):
        tries.append(texts)
        raise RuntimeError("no network")

    use_fake_model(monkeypatch, tmp_path / "index.json", offline)
    clock = [1000.0]
    monkeypatch.setattr(help_index.time, "monotonic", lambda: clock[0])

    assert search(root, "lion")["fallback"] == "lexical"
    assert search(root, "zebra")["fallback"] == "lexical"
    assert len(tries) == 1  # the second search did not wait for the model

    clock[0] += help_index.RETRY_MODEL_AFTER
    monkeypatch.setattr(vector_store, "compute_embeddings", BagOfWords())  # back online
    assert "fallback" not in search(root, "lion")


def test_concurrent_first_searches_embed_the_guides_once(plugins, monkeypatch, tmp_path):
    root, plugin = plugins
    plugin("zoo", guide(("main", "Zoo", "zebra"), ("cats", "Cats", "lion")))
    model = use_fake_model(monkeypatch, tmp_path / "index.json", BagOfWords(delay=0.2))
    library = Library([root])

    async def both():
        return await asyncio.gather(help_index.find_help(library, "zebra"), help_index.find_help(library, "lion"))

    results = asyncio.run(both())

    assert [result["hits"][0]["node"] for result in results] == ["main", "cats"]
    assert model.texts.count("Zoo\nzebra") == 1


def test_the_search_runs_off_the_event_loop(plugins, monkeypatch, tmp_path):
    root, plugin = plugins
    plugin("zoo", guide(("main", "Zoo", "zebra"),))
    seen = []
    model = BagOfWords()

    def recording(texts, batch_size=128, normalize=True):
        seen.append(threading.current_thread() is threading.main_thread())
        return model(texts)

    use_fake_model(monkeypatch, tmp_path / "index.json", recording)
    search(root, "zebra")

    assert seen and not any(seen)


def test_the_words_fallback_counts_every_node_it_found(plugins, monkeypatch, tmp_path):
    """The panel said "8 nodes contain" where 20 did: it counted the hits it was given."""
    root, plugin = plugins
    plugin("zoo", guide(*[(f"n{n}", f"Node {n}", "agent") for n in range(12)]))

    def offline(texts, batch_size=128, normalize=True):
        raise RuntimeError("no network")

    use_fake_model(monkeypatch, tmp_path / "index.json", offline)
    result = search(root, "agent", limit=3)

    assert len(result["hits"]) == 3 and result["total"] == 12


def test_an_index_whose_stamps_hide_renamed_nodes_is_not_served(plugins, monkeypatch, tmp_path):
    """Same stamps, other node names (a copy keeping its mtime): the stored rows named nodes that are gone."""
    root, plugin = plugins
    folder = plugin("zoo", guide(("main", "Zoo", "zebra"), ("cats", "Cats", "lion")))
    file = tmp_path / "index.json"
    use_fake_model(monkeypatch, file)
    search(root, "zebra")
    path = folder / "zoo.guide"
    before = path.stat()
    path.write_text(guide(("main", "Zoo", "zebra"), ("dogs", "Dogs", "lion")), encoding="utf-8")  # same size
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    use_fake_model(monkeypatch, file)  # a new process: the stored index, the guide parsed afresh
    monkeypatch.setattr(help_module, "_cache", {})
    hits = search(root, "lion")["hits"]

    assert ("zoo", "dogs") in [(hit["guide"], hit["node"]) for hit in hits]


def test_the_route_answers_with_the_ranking_and_the_exact_node(plugins, monkeypatch, tmp_path):
    root, plugin = plugins
    plugin("zoo", guide(("main", "Zoo", "zebra"), ("cats", "Cats", "lion")))
    use_fake_model(monkeypatch, tmp_path / "index.json")
    app = FastAPI()
    app.include_router(router)
    app.state.config = SimpleNamespace(plugins=SimpleNamespace(plugin_dirs=[str(root)]))

    found = TestClient(app).get("/api/help/search", params={"q": "zoo/cats"}).json()

    assert found["exact"] == {"guide": "zoo", "node": "cats", "title": "Cats"}
    assert found["hits"] and found["total"] == len(found["hits"])


def test_a_file_another_process_is_replacing_is_tried_again(monkeypatch):
    """Windows refuses the index while the API or agent-cli replaces it: a PermissionError, for moments."""
    monkeypatch.setattr(help_index, "SHARING_PAUSE", 0)
    attempts = []

    def busy_twice():
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError(5, "Access is denied")
        return "read"

    assert help_index._shared(busy_twice) == "read" and len(attempts) == 3

    def always_busy():
        raise PermissionError(5, "Access is denied")

    with pytest.raises(PermissionError):
        help_index._shared(always_busy)
