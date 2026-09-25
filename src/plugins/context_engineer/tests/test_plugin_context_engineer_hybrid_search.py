"""The archive search asks the text index too, not only the vector index.

A row is in the vector index only once its embedding landed. A large prune
embeds in the background, a refused or cancelled batch never does, and a session
archived before semantic search was switched on has no vectors at all. Asked
alone, the vector index answered over the part it held -- non-empty, so the text
fallback never fired -- and an exact identifier from an unembedded message was
simply not found. That is the "the agent finds only half" case, measured on the
path `list(filter=...)` takes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.context_engineer.archival_memory import ArchivalMemory, _fused


class PartialIndex:
    """A vector store that knows only SOME of the archive, in a fixed order --
    what the shared store holds after a batch was refused or cancelled."""

    def __init__(self, known_ids: list[str], flat: bool = False):
        self.known_ids = known_ids
        self.flat = flat            # sqlite-vec answers flat, ChromaDB per query
        self.queries: list[str] = []

    def query(self, collection, query_text=None, n_results=10, where=None, **_):
        self.queries.append(query_text)
        hits = self.known_ids[:n_results]
        return {"ids": hits if self.flat else [hits]}


def _archive(tmp_path: Path, contents: list[str]) -> tuple[ArchivalMemory, list[str]]:
    archive = ArchivalMemory(tmp_path / "archive.db", session_id="s")
    ids, _, _ = archive.store_many_unindexed([{"role": "user", "content": c} for c in contents])
    archive.enable_semantic_search = True
    return archive, ids


FILLER = "Zwischenstand: die Baugruppe wurde vermessen und abgezeichnet."
MARKER = "Das Pruefstueck traegt die Kennung QS-4711 und liegt im Regal B."


def test_an_unembedded_row_is_found_by_its_keyword(tmp_path):
    """The index knows ten filler rows and answers with them -- a full,
    plausible answer that happens not to contain what was asked for."""
    archive, ids = _archive(tmp_path, [FILLER] * 10 + [MARKER])
    archive._vector_store = PartialIndex(known_ids=ids[:10])       # the marker is not embedded

    found = archive.search("QS-4711", session_id="s", limit=5)

    assert any("QS-4711" in m.content for m in found), (
        "an exact identifier from an unembedded row was not found: "
        f"{[m.content[:30] for m in found]}")
    assert archive._vector_store.queries == ["QS-4711"], "the vector index was not asked"


def test_a_single_slot_goes_to_the_row_that_holds_every_word(tmp_path):
    """The top of each list scores the same, and fusion breaks the tie by list
    order. Vector first, limit=1 answered with the nearest filler row and never
    with the unembedded one that holds the identifier asked for."""
    archive, ids = _archive(tmp_path, [FILLER] * 10 + [MARKER])
    archive._vector_store = PartialIndex(known_ids=ids[:10])

    found = archive.search("QS-4711", session_id="s", limit=1)

    assert [m.id for m in found] == [ids[10]], [m.content[:25] for m in found]


def test_the_vector_answer_is_kept_where_the_text_index_has_nothing(tmp_path):
    """A paraphrase shares no word with the stored text. That is what the
    vector index is for, and fusing must not lose it."""
    archive, ids = _archive(tmp_path, [MARKER, FILLER])
    archive._vector_store = PartialIndex(known_ids=[ids[0]])

    found = archive.search("Probenkoerper Dichtring Material", session_id="s", limit=5)

    assert [m.id for m in found] == [ids[0]]


def test_a_row_both_indexes_agree_on_ranks_first(tmp_path):
    """Agreement is the strongest signal either index has. Here the vector
    index ranks the marker LAST of three; the text index ranks it first."""
    archive, ids = _archive(tmp_path, [FILLER, FILLER, MARKER])
    archive._vector_store = PartialIndex(known_ids=[ids[0], ids[1], ids[2]])

    found = archive.search("QS-4711", session_id="s", limit=3)

    assert found[0].id == ids[2], [m.content[:20] for m in found]


def test_the_limit_holds_across_both_lists(tmp_path):
    archive, ids = _archive(tmp_path, [f"{FILLER} QS-4711 {i}" for i in range(8)])
    archive._vector_store = PartialIndex(known_ids=ids[4:])

    assert len(archive.search("QS-4711", session_id="s", limit=3)) == 3


def test_a_common_word_does_not_buy_a_slot(tmp_path):
    """The text half ORed every word, and fusion weighs by rank alone: rows that
    share nothing with the question but "die" took slots at the same weight as
    the vector index's real answer. Demanding every word, the text half adds
    only what holds all of them -- here nothing, so the answer is the vector's."""
    archive, ids = _archive(tmp_path, [
        "Der Grafikchip kopiert Bildbereiche ohne Hilfe des Prozessors.",
        "Die Rechnung fuer die Werkstatt ist noch offen.",
        "Die Figur betritt das Kapitel durch die Hintertuer.",
    ])
    archive._vector_store = PartialIndex(known_ids=[ids[0]])

    found = archive.search("Hardware die Speicher bewegt", session_id="s", limit=3)

    assert [m.id for m in found] == [ids[0]], [m.content[:25] for m in found]


def test_an_empty_index_gets_the_broad_text_search(tmp_path):
    """A session archived before semantic search was on has no vectors at all.
    Demanding every word there would find less than without semantic search;
    the empty index gets the same broad search the text-only mode has."""
    archive, ids = _archive(tmp_path, [MARKER, FILLER])
    archive._vector_store = PartialIndex(known_ids=[])

    found = archive.search("QS-4711 Werkstatt", session_id="s", limit=5)

    assert [m.id for m in found] == [ids[0]], "one of two words must be enough here"


@pytest.mark.parametrize("flat", [False, True], ids=["chromadb", "sqlite-vec"])
def test_an_empty_index_asks_the_text_index_once(tmp_path, monkeypatch, flat):
    """_search_semantic used to fall back to text itself, and search() then
    asked again for the fusion: every query of an unindexed session ran the
    text search -- and its LIKE scan -- twice. Both empty shapes: ChromaDB
    answers [[]], sqlite-vec [], and they are caught in different places."""
    archive, ids = _archive(tmp_path, [MARKER])
    archive._vector_store = PartialIndex(known_ids=[], flat=flat)
    calls = []
    original = archive._search_text
    monkeypatch.setattr(archive, "_search_text",
                        lambda *a, **k: calls.append(1) or original(*a, **k))

    archive.search("QS-4711", session_id="s", limit=5)

    assert len(calls) == 1, f"the text index was asked {len(calls)} times"


def test_without_semantic_search_only_the_text_index_is_asked(tmp_path):
    archive, ids = _archive(tmp_path, [MARKER])
    archive._vector_store = PartialIndex(known_ids=ids)
    archive.enable_semantic_search = False

    found = archive.search("QS-4711", session_id="s", limit=5)

    assert [m.id for m in found] == ids and archive._vector_store.queries == []


def test_fusion_gives_a_one_list_hit_the_place_its_rank_earns():
    """Concatenating would cut the second list whenever the first fills the
    limit; that is exactly how the unembedded row vanished before."""
    class M:
        def __init__(self, id):
            self.id = id

    semantic = [M("a"), M("b"), M("c")]
    text = [M("x"), M("a")]

    assert [m.id for m in _fused(semantic, text, limit=3)] == ["a", "x", "b"]


def test_fusion_lifts_what_both_lists_agree_on():
    """b is second in both lists. Summed, that beats being first in one --
    agreement is the strongest signal either index has. Scoring only the
    first sighting would leave b behind a and x."""
    class M:
        def __init__(self, id):
            self.id = id

    assert [m.id for m in _fused([M("a"), M("b"), M("c")], [M("x"), M("b")], limit=3)] \
        == ["b", "a", "x"]
