"""Search the help by meaning: every node of every guide embedded once, a query ranked against them.

The model is the one the core already runs without torch (all-MiniLM-L6-v2 on ONNX, see
``agent_system.utils.vector_store``). Measured on the 583 nodes of 06.10.2026: 9.3 s to embed them all, so the
vectors are kept on disk and a guide is embedded again only when its file changed. English only: on German
queries the model found the right guide 3 times in 8, and the multilingual models measured worse overall.

Where the model cannot be readied (no network on the first use), the search falls back on the words
(``Library.search``) and says so in the result -- it never fails its caller over the index.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from agent_system.paths import PROJECT_ROOT, data_path
from agent_system.utils import vector_store

from .help import MANUAL, Library, _snippet, node_text

logger = logging.getLogger(__name__)

#: What the index embeds of a node: its title and the start of its text (as measured).
TEXT_CHARS = 1500
#: Bump when the stored layout or what is embedded changes: an older file is then rebuilt, not misread.
FORMAT = 1
#: Windows refuses a file another process is replacing that moment (the API and agent-cli share the index):
#: tries, and the pause before the next.
SHARING_TRIES, SHARING_PAUSE = 40, 0.05  # as coding_cli measured it (22.09.2026)
#: Seconds the words answer after the model failed before it is tried again: offline, each try is a
#: download attempt that holds the search for seconds.
RETRY_MODEL_AFTER = 300.0


def _document(title: str, text: str) -> str:
    return f"{title}\n{text[:TEXT_CHARS]}"


def _shared(action: Any) -> Any:
    """``action()``, tried again while another process holds the file (a PermissionError on Windows)."""
    for attempt in range(SHARING_TRIES):
        try:
            return action()
        except PermissionError:
            if attempt == SHARING_TRIES - 1:
                raise
            time.sleep(SHARING_PAUSE)


class _Index:
    """The vectors of every guide's nodes, per guide, mirrored in one file."""

    def __init__(self, file: Path):
        self.file = file
        self.lock = threading.Lock()
        #: guide id -> {"key": what the guide was built from, "nodes": [node key], "vectors": float32 rows}
        self.guides: dict[str, dict[str, Any]] | None = None
        #: time.monotonic() of the model's last failure; only the window after it counts
        self.failed_at: float | None = None

    def _read(self) -> dict[str, dict[str, Any]]:
        import numpy as np
        try:
            data = json.loads(_shared(lambda: self.file.read_text(encoding="utf-8")))
            if data.get("format") != FORMAT:
                return {}
            return {guide: {"key": entry["key"], "nodes": list(entry["nodes"]),
                            "vectors": np.frombuffer(base64.b64decode(entry["vectors"]), dtype=np.float32)
                            .reshape(len(entry["nodes"]), vector_store.EMBEDDING_DIM)}
                    for guide, entry in data["guides"].items()}
        except FileNotFoundError:
            return {}
        except Exception as error:  # cut short, edited, another layout: built anew, never a failed search
            logger.warning("Help search index %s unreadable (%s); building it again", self.file, error)
            return {}

    def _write(self) -> None:
        data = {"format": FORMAT, "guides": {
            guide: {"key": entry["key"], "nodes": entry["nodes"],
                    "vectors": base64.b64encode(entry["vectors"].astype("float32").tobytes()).decode("ascii")}
            for guide, entry in (self.guides or {}).items()}}
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            # a temporary name of this writer's own, replaced onto the index: never a half-written index
            temporary = self.file.with_name(f"{self.file.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                temporary.write_text(json.dumps(data), encoding="utf-8")
                _shared(lambda: os.replace(temporary, self.file))
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as error:  # the next process embeds again; this one has its vectors
            logger.warning("Help search index not saved to %s: %s", self.file, error)

    def refresh(self, library: Library) -> tuple[list[tuple[str, str]], Any]:
        """(guide, node) of every node of ``library`` and its vectors, row by row; guides that changed embedded.

        One caller at a time: concurrent first searches embed the guides once, not once each.
        """
        import numpy as np
        with self.lock:
            if self.guides is None:
                self.guides = self._read()
            fresh: dict[str, dict[str, Any]] = {}
            stale: list[tuple[str, str, list[str], list[str]]] = []  # guide, key, node keys, documents
            for guide in library.guides.values():
                nodes = [(key, node) for key, node in guide.nodes.items() if not node.generated]
                key = library.sources.get(guide.id)
                documents = None
                if key is None:  # generated (the plugin list): no file to stamp, its text is the key
                    documents = [_document(node.title, node_text(guide, node)) for _, node in nodes]
                    key = "sha1:" + hashlib.sha1("\0".join(documents).encode("utf-8")).hexdigest()
                known = self.guides.get(guide.id)
                # the names too: a stamp that did not move (a copy keeping its mtime) must not serve rows
                # of nodes the guide no longer has -- a KeyError on every search, kept on disk
                if known is not None and known["key"] == key and known["nodes"] == [name for name, _ in nodes]:
                    fresh[guide.id] = known
                    continue
                if documents is None:
                    documents = [_document(node.title, node_text(guide, node)) for _, node in nodes]
                stale.append((guide.id, key, [name for name, _ in nodes], documents))
            if stale:
                # all in one batch, before anything is replaced: a model that fails leaves the index as it was
                vectors = np.asarray(vector_store.compute_embeddings(
                    [document for *_, documents in stale for document in documents]), dtype=np.float32)
                at = 0
                for guide_id, key, names, documents in stale:
                    rows = vectors[at:at + len(documents)].reshape(len(documents), vector_store.EMBEDDING_DIM)
                    fresh[guide_id] = {"key": key, "nodes": names, "vectors": rows}
                    at += len(documents)
                logger.info("Help search: embedded %d node(s) of %d guide(s)", len(vectors), len(stale))
            changed = bool(stale) or fresh.keys() != self.guides.keys()  # new, changed or removed guides
            self.guides = fresh
            if changed:
                self._write()
            refs = [(guide_id, name) for guide_id, entry in fresh.items() for name in entry["nodes"]]
            rows = [entry["vectors"] for entry in fresh.values()]
        return refs, (np.vstack(rows) if rows else np.empty((0, vector_store.EMBEDDING_DIM), dtype=np.float32))


_index: _Index | None = None
_index_guard = threading.Lock()


def _the_index() -> _Index:
    """The process's index, its file in the data directory -- read when first needed, not at import."""
    global _index
    with _index_guard:
        if _index is None:
            _index = _Index(PROJECT_ROOT / data_path("help", "search_index.json"))
        return _index


def _normal(text: str) -> str:
    """Lower case, with spaces, underscores and hyphens alike: "Sub-agent manager" is "sub_agent_manager"."""
    return " ".join(re.split(r"[\s_-]+", text.strip().lower())).strip()


def _exact(library: Library, query: str) -> dict[str, str] | None:
    """The node ``query`` names: "guide/node" or a guide's id.

    Not a node title: "Export", "Status" or "Files" are each the title of one node of one plugin, and
    opening that for a one-word question hid the list it belongs in. A title ranks like any other text.
    """
    wanted = _normal(query)
    if not wanted:
        return None
    found = library.resolve(MANUAL, query.strip()) if "/" in query else None
    if found is None:
        guide = next((one for one in library.guides.values() if _normal(one.id) == wanted), None)
        if guide is not None and guide.nodes:
            found = guide.id, "main" if "main" in guide.nodes else next(iter(guide.nodes))
    if found is None:
        return None
    return {"guide": found[0], "node": found[1], "title": library.guides[found[0]].nodes[found[1]].title}


def _search(library: Library, query: str, limit: int) -> dict[str, Any]:
    import numpy as np
    exact = _exact(library, query)
    if not query.strip():
        return {"query": query, "exact": None, "hits": []}
    index = _the_index()

    def by_words() -> dict[str, Any]:
        found = library.search(query)
        hits = [{**hit, "score": None} for hit in found[:limit]]
        return {"query": query, "exact": exact, "hits": hits, "fallback": "lexical", "total": len(found)}

    if index.failed_at is not None and time.monotonic() - index.failed_at < RETRY_MODEL_AFTER:
        return by_words()
    try:
        refs, matrix = index.refresh(library)
        scores = np.empty(0)
        if refs:  # unit vectors both: the dot product is the cosine
            scores = matrix @ np.asarray(vector_store.compute_embeddings([query])[0], dtype=np.float32)
    except Exception as error:  # the model could not be readied: the words still find something
        index.failed_at = time.monotonic()
        logger.warning("Help search by meaning unavailable (%s); searching the words for %d s",
                       error, int(RETRY_MODEL_AFTER))
        return by_words()
    # the snippet shows the longest query word the node holds -- the most telling one -- else the node's start
    words = sorted({word for word in re.findall(r"\w+", query.lower()) if len(word) > 2}, key=len, reverse=True)
    hits = []
    for at in np.argsort(-scores, kind="stable")[:max(0, limit)]:
        guide_id, name = refs[at]
        guide = library.guides[guide_id]
        node = guide.nodes[name]
        text = node_text(guide, node)
        lowered = text.lower()
        term = next((word for word in words if word in lowered), "")
        hits.append({"guide": guide_id, "node": name, "title": node.title, "database": guide.title,
                     "snippet": _snippet(text, term), "score": round(float(scores[at]), 3)})
    return {"query": query, "exact": exact, "hits": hits}


async def find_help(library: Library, query: str, *, limit: int = 8) -> dict:
    """The help nodes closest in meaning to ``query``, and the node it names exactly, if any.

    ``{"query": <as typed>, "exact": {"guide", "node", "title"} | None,
    "hits": [{"guide", "node", "title", "database", "snippet", "score"}, ...]}`` -- best first, at most
    ``limit``. With ``"fallback": "lexical"`` when the embedding model could not be readied: the hits are then
    the nodes holding every word (``Library.search``), ``score`` None. The first call builds the index (seconds);
    the embedding runs off the event loop.
    """
    return await asyncio.to_thread(_search, library, query, limit)
