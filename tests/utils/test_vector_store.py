"""Tests for the VectorStore utility."""

import tempfile
import pytest
import gc
from pathlib import Path

from agent_system.utils.vector_store import (
    VectorStore,
    get_vector_backend,
    get_available_onnx_providers,
)


# Helper to create temp directories that ignore cleanup errors on Windows
def create_temp_dir():
    """Create a temp directory that ignores cleanup errors (Windows file locking)."""
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class TestGetVectorBackend:
    """Tests for backend detection."""

    def test_returns_valid_backend(self):
        """Should return either 'chromadb' or 'sqlite-vec'."""
        backend = get_vector_backend()
        assert backend in ("chromadb", "sqlite-vec")

    def test_caches_result(self):
        """Should return same result on repeated calls."""
        backend1 = get_vector_backend()
        backend2 = get_vector_backend()
        assert backend1 == backend2


class TestGetAvailableOnnxProviders:
    """Tests for ONNX provider detection."""

    def test_returns_list(self):
        """Should return a list of providers."""
        providers = get_available_onnx_providers()
        assert isinstance(providers, list)
        assert len(providers) > 0

    def test_includes_cpu_provider(self):
        """Should always include CPU provider as fallback."""
        providers = get_available_onnx_providers()
        assert "CPUExecutionProvider" in providers


class TestVectorStoreInit:
    """Tests for VectorStore initialization."""

    def test_creates_persist_directory(self):
        """Should create persist directory if it doesn't exist."""
        tmpdir_obj = create_temp_dir()
        tmpdir = tmpdir_obj.name
        persist_path = Path(tmpdir) / "new_dir" / "vectors"
        store = VectorStore(persist_path=persist_path)
        assert persist_path.exists()
        store.close()
        gc.collect()  # Help release file handles
        tmpdir_obj.cleanup()

    def test_backend_property(self):
        """Should expose backend type."""
        tmpdir_obj = create_temp_dir()
        store = VectorStore(persist_path=tmpdir_obj.name)
        assert store.backend in ("chromadb", "sqlite-vec")
        store.close()
        gc.collect()
        tmpdir_obj.cleanup()

    def test_context_manager(self):
        """Should work as context manager."""
        tmpdir_obj = create_temp_dir()
        with VectorStore(persist_path=tmpdir_obj.name) as store:
            assert store.backend in ("chromadb", "sqlite-vec")
        gc.collect()
        tmpdir_obj.cleanup()


class TestVectorStoreOperations:
    """Tests for VectorStore CRUD operations."""

    @pytest.fixture
    def store(self):
        """Create a temporary VectorStore for testing."""
        tmpdir_obj = create_temp_dir()
        store = VectorStore(persist_path=tmpdir_obj.name)
        yield store
        store.close()
        gc.collect()  # Help release file handles on Windows
        tmpdir_obj.cleanup()

    def test_add_documents(self, store):
        """Should add documents to a collection."""
        store.add(
            collection="test_collection",
            ids=["doc1", "doc2"],
            documents=["Hello world", "Goodbye world"],
            metadatas=[{"type": "greeting"}, {"type": "farewell"}]
        )
        
        count = store.count("test_collection")
        assert count == 2

    def test_add_documents_without_metadata(self, store):
        """Should add documents without metadata."""
        store.add(
            collection="test_collection",
            ids=["doc1"],
            documents=["Hello world"]
        )
        
        count = store.count("test_collection")
        assert count == 1

    def test_query_by_text(self, store):
        """Should query documents by text similarity."""
        store.add(
            collection="test_collection",
            ids=["doc1", "doc2", "doc3"],
            documents=[
                "The cat sat on the mat",
                "Dogs are loyal pets",
                "The feline rested on the rug"
            ]
        )
        
        results = store.query(
            collection="test_collection",
            query_text="cat on mat",
            n_results=2
        )
        
        assert "ids" in results
        assert len(results["ids"]) <= 2

    def test_query_returns_distances(self, store):
        """Should return distances in query results."""
        store.add(
            collection="test_collection",
            ids=["doc1"],
            documents=["Hello world"]
        )
        
        results = store.query(
            collection="test_collection",
            query_text="Hello",
            n_results=1
        )
        
        assert "distances" in results

    def test_delete_by_ids(self, store):
        """Should delete documents by ID."""
        store.add(
            collection="test_collection",
            ids=["doc1", "doc2"],
            documents=["Hello", "World"]
        )
        
        assert store.count("test_collection") == 2
        
        store.delete(collection="test_collection", ids=["doc1"])
        
        assert store.count("test_collection") == 1

    def test_count_empty_collection(self, store):
        """Should return 0 for empty collection."""
        store.get_or_create_collection("empty_collection")
        count = store.count("empty_collection")
        assert count == 0

    def test_delete_collection(self, store):
        """Should delete entire collection."""
        store.add(
            collection="test_collection",
            ids=["doc1"],
            documents=["Hello"]
        )
        
        store.delete_collection("test_collection")
        
        # After deletion, creating new collection should be empty
        store.get_or_create_collection("test_collection")
        assert store.count("test_collection") == 0

    def test_get_or_create_collection(self, store):
        """Should create collection if it doesn't exist."""
        # First call creates
        store.get_or_create_collection("new_collection")
        
        # Second call gets existing
        store.get_or_create_collection("new_collection")
        
        # Should be able to add to it
        store.add(
            collection="new_collection",
            ids=["doc1"],
            documents=["Test"]
        )
        assert store.count("new_collection") == 1


class TestVectorStoreReset:
    """Tests for VectorStore reset functionality."""

    def test_reset_clears_all_data(self):
        """Should clear all collections on reset."""
        tmpdir_obj = create_temp_dir()
        store = VectorStore(persist_path=tmpdir_obj.name)
        
        # Add data to multiple collections
        store.add(
            collection="collection1",
            ids=["doc1"],
            documents=["Hello"]
        )
        store.add(
            collection="collection2",
            ids=["doc1"],
            documents=["World"]
        )
        
        # Reset
        store.reset()
        
        # Both should be empty after reset
        store.get_or_create_collection("collection1")
        store.get_or_create_collection("collection2")
        assert store.count("collection1") == 0
        assert store.count("collection2") == 0
        
        store.close()
        gc.collect()
        tmpdir_obj.cleanup()


class TestVectorStoreMetadata:
    """Tests for metadata handling."""

    @pytest.fixture
    def store(self):
        """Create a temporary VectorStore for testing."""
        tmpdir_obj = create_temp_dir()
        store = VectorStore(persist_path=tmpdir_obj.name)
        yield store
        store.close()
        gc.collect()
        tmpdir_obj.cleanup()

    def test_metadata_preserved(self, store):
        """Should preserve metadata in query results."""
        store.add(
            collection="test_collection",
            ids=["doc1"],
            documents=["Hello world"],
            metadatas=[{"author": "test", "year": "2024"}]
        )
        
        results = store.query(
            collection="test_collection",
            query_text="Hello",
            n_results=1
        )
        
        assert "metadatas" in results
        # Results are in batch format: [[metadata1, metadata2, ...]]
        # So metadatas[0] is the list of metadatas for the first (and only) query
        if results["metadatas"] and results["metadatas"][0]:
            metadata = results["metadatas"][0][0]  # First result of first query
            assert metadata.get("author") == "test"
            assert metadata.get("year") == "2024"

    def test_list_entries_returns_the_metadata_with_each_id(self, store):
        """Deciding whether an entry is stale needs its metadata, not only its id."""
        store.add(
            collection="test_collection",
            ids=["doc1", "doc2"],
            documents=["Hello world", "Goodbye world"],
            metadatas=[{"content_hash": "aaa"}, {"content_hash": "bbb"}],
        )

        entries = {e["id"]: e["metadata"] for e in store.list_entries("test_collection")}

        assert set(entries) == {"doc1", "doc2"}
        assert entries["doc1"]["content_hash"] == "aaa"
        assert entries["doc2"]["content_hash"] == "bbb"

    def test_list_entries_reads_the_sqlite_fallback_too(self):
        """The fallback has its own SQL, and nothing here normally runs it."""
        from agent_system.utils.vector_store import store as module

        original = module.get_vector_backend
        module.get_vector_backend = lambda: "sqlite-vec"
        tmpdir_obj = create_temp_dir()
        try:
            store = VectorStore(persist_path=tmpdir_obj.name)
            assert store.backend == "sqlite-vec"
            store.add(
                collection="fallback_collection",
                ids=["doc1"],
                documents=["Hello world"],
                metadatas=[{"content_hash": "aaa"}],
            )

            assert store.list_entries("fallback_collection") == [
                {"id": "doc1", "metadata": {"content_hash": "aaa"}},
            ]
        finally:
            module.get_vector_backend = original
            try:
                store.close()
            except Exception:
                pass
            gc.collect()
            tmpdir_obj.cleanup()

    def test_sqlite_vec_re_add_replaces_and_a_failed_add_leaves_nothing(self):
        """vec0 refuses INSERT OR REPLACE on an id it holds: a re-add raised
        where ChromaDB replaces. And a batch failing halfway left its first
        items pending, for the next commit on the connection to save."""
        from agent_system.utils.vector_store import store as module
        from agent_system.utils.vector_store import VectorStoreError

        original = module.get_vector_backend
        module.get_vector_backend = lambda: "sqlite-vec"
        tmpdir_obj = create_temp_dir()
        try:
            store = VectorStore(persist_path=tmpdir_obj.name)
            store.add(collection="re_add", ids=["doc1"], documents=["Hello world"])
            store.add(collection="re_add", ids=["doc1"], documents=["Hello again"])
            assert store.count("re_add") == 1

            with pytest.raises(VectorStoreError):
                # doc2 is written, doc3 has no document and raises.
                store.add(collection="re_add", ids=["doc2", "doc3"], documents=["Zwei"])
            store.add(collection="re_add", ids=["doc4"], documents=["Vier"])

            assert {e["id"] for e in store.list_entries("re_add")} == {"doc1", "doc4"}
        finally:
            module.get_vector_backend = original
            try:
                store.close()
            except Exception:
                pass
            gc.collect()
            tmpdir_obj.cleanup()

    def test_list_entries_without_metadata_yields_an_empty_dict(self, store):
        """An entry stored without metadata must not break the caller's lookup."""
        store.add(
            collection="test_collection",
            ids=["doc1"],
            documents=["Hello world"],
        )

        assert store.list_entries("test_collection") == [{"id": "doc1", "metadata": {}}]


class TestVectorStoreConcurrency:
    """The lock must serialize concurrent access to the single sqlite connection."""

    @pytest.fixture
    def store(self):
        tmpdir_obj = create_temp_dir()
        store = VectorStore(persist_path=tmpdir_obj.name)
        yield store
        store.close()
        gc.collect()
        tmpdir_obj.cleanup()

    def test_synchronized_serializes_concurrent_ops(self, store):
        """Concurrent add() calls from many threads must NOT overlap.

        Callers fan VectorStore calls out via asyncio.to_thread; without the
        per-instance lock two threads would use the same sqlite cursor at once
        ('recursive use of cursors' / corruption). We replace the backend-
        specific inner add with one that records concurrency and assert the
        observed max-concurrency is exactly 1.
        """
        import threading
        import time
        from unittest.mock import patch

        state = {"cur": 0, "max": 0}
        guard = threading.Lock()

        def fake_inner(*args, **kwargs):
            with guard:
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
            time.sleep(0.01)  # widen the window for overlap to show
            with guard:
                state["cur"] -= 1

        # the backend's whole add, chromadb's write lock with it: that lock serializes the writes on its own,
        # and only the instance lock may do it here
        with patch.object(store._backend, "add", side_effect=fake_inner):
            threads = [
                threading.Thread(
                    target=store.add,
                    args=("c",),
                    kwargs={"ids": [f"id{i}"], "documents": ["d"], "embeddings": [[0.1] * 384]},
                )
                for i in range(10)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert state["max"] == 1, f"lock failed to serialize (max concurrency {state['max']})"

    def test_a_model_that_fails_writes_nothing_the_others_must_reopen_for(self, store, monkeypatch):
        """The documents are embedded before the store's write lock: a model readied on first use may wait
        minutes for another process, not with every other process's write waiting behind it -- and one that fails
        has touched no store, so nobody reopens theirs for it."""
        from pathlib import Path
        from agent_system.utils.vector_store import chroma
        if store.backend != "chromadb":
            pytest.skip("the write lock and its generation are chromadb's")
        counter = Path(store.persist_path) / chroma._CHROMA_GENERATION
        store.add("notes", ids=["a"], documents=["first"], embeddings=[[0.1] * 384])  # no model: made here
        before = counter.read_text(encoding="utf-8")

        def failing(texts, *args, **kwargs):
            raise RuntimeError("the embedding model could not be readied")
        monkeypatch.setattr(chroma, "compute_embeddings", failing)

        with pytest.raises(RuntimeError, match="could not be readied"):
            store.add("notes", ids=["b"], documents=["second"])

        assert counter.read_text(encoding="utf-8") == before

    def test_reentrant_lock_allows_nested_calls(self, store):
        """The lock is reentrant: a synchronized method may call another."""
        # close() is @_synchronized and is safe to call twice; a non-reentrant
        # lock would be fine here, but this documents the reentrancy guarantee.
        store.close()
        store.close()  # must not deadlock


class TestCloseReleasesChromaSystem:
    """close() must hand the ChromaDB client back to the library.

    chromadb parks every System in a process-global registry keyed by
    persist_directory, and each System owns a tokio runtime with its own
    threads and epoll/eventfd handles. Callers hand out one persist path per
    session, so nothing is ever reused: dropping the reference alone leaked
    ~6 descriptors and ~5 threads per store. On the writer host that reached
    229 leaked clients / 460 tokio threads and pushed the API into the 1024
    descriptor limit, where every X-API-Key lookup failed with a 401.
    """

    class _FakeChromaClient:
        def __init__(self, raises: bool = False):
            self.close_calls = 0
            self._raises = raises

        def close(self) -> None:
            self.close_calls += 1
            if self._raises:
                raise RuntimeError("system already stopped")

    def test_close_hands_the_client_back(self):
        with create_temp_dir() as tmp:
            store = VectorStore(persist_path=Path(tmp) / "vectors")
            fake = self._FakeChromaClient()
            store._backend._client = fake
            store._backend._collections["archival_memory"] = object()

            store.close()

            assert fake.close_calls == 1, (
                "VectorStore.close() dropped the reference without calling "
                "close() — the tokio runtime stays alive and leaks descriptors"
            )
            assert store._backend._client is None
            assert store._backend._collections == {}

    def test_close_survives_a_failing_client(self, caplog):
        """A failing close must not abort the caller's cleanup.

        The only caller is context_engineer's cleanup_session, which closes
        several stores in sequence; an exception here would skip the rest and
        leak more than it saves. It must still be loud — a silent failure is
        exactly the leak we are fixing.
        """
        with create_temp_dir() as tmp:
            store = VectorStore(persist_path=Path(tmp) / "vectors")
            fake = self._FakeChromaClient(raises=True)
            store._backend._client = fake

            with caplog.at_level("WARNING"):
                store.close()  # must not raise

            assert store._backend._client is None
            assert any(
                "ChromaDB client close failed" in r.message for r in caplog.records
            ), "a failed close must be logged, or the leak returns unnoticed"

    def test_missing_close_is_reported_once_not_per_cleanup(self, caplog, monkeypatch):
        """chromadb < 1.5 has no Client.close().

        There the leak cannot be avoided, so the operator must hear about it —
        but once, not on every session cleanup. context_engineer closes a store
        per session, which would otherwise bury the log.
        """
        from agent_system.utils.vector_store import chroma
        monkeypatch.setattr(chroma._ChromaAccess, "_close_unsupported_warned", False)

        class _OldClient:  # no close() — chromadb 1.4 shape
            pass

        def _close_once():
            with create_temp_dir() as tmp:
                store = VectorStore(persist_path=Path(tmp) / "vectors")
                store._backend._client = _OldClient()
                store.close()
                assert store._backend._client is None

        with caplog.at_level("WARNING"):
            _close_once()
            _close_once()

        hits = [r for r in caplog.records if "has no Client.close()" in r.message]
        assert len(hits) == 1, (
            f"expected exactly one warning for the whole process, got {len(hits)}"
        )


class TestTheDefaultModelNeedsNoTorch:
    """all-MiniLM-L6-v2 runs on chromadb's ONNX export: sentence-transformers, and torch with it,
    is no dependency of the core any more. Any other model still needs it."""

    @pytest.fixture
    def fresh_models(self, monkeypatch):
        from agent_system.utils.vector_store import embeddings
        monkeypatch.setattr(embeddings, "_embedding_models", {})
        return embeddings

    @pytest.mark.timeout(600)  # on a clean machine the child downloads the model first
    def test_the_default_model_embeds_without_sentence_transformers(self):
        """In a process of its own, the three blocked before the module is imported: a module-level import
        of any of them would otherwise have happened long before a monkeypatch in this one."""
        script = "\n".join([
            "import sys",
            "for name in ('sentence_transformers', 'transformers', 'torch'):",
            "    sys.modules[name] = None",
            "import agent_system.utils.vector_store as vector_store",
            "vectors = vector_store.compute_embeddings(['how is the config loaded', 'a second text'])",
            "single = vector_store.compute_embedding('one text')",
            "print(vector_store.__file__)",
            "print([len(vector) for vector in vectors], len(single), type(single[0]).__name__)",
        ])
        run = _child(script, timeout=540)

        assert run.returncode == 0, run.stderr[-2000:]
        imported, result = run.stdout.strip().splitlines()[-2:]
        assert Path(imported) == _UNDER_TEST, (imported, _UNDER_TEST)
        assert result == "[384, 384] 384 float", run.stdout

    def test_its_vectors_are_the_ones_sentence_transformers_computes(self, fresh_models):
        import numpy as np
        sentence_transformers = pytest.importorskip("sentence_transformers")
        # the longer first: a batch is sorted by length, and the vectors must come back in the callers' order;
        # the last is over 256 tokens, where both truncate
        texts = ["Die Heldin verlässt das Dorf im Morgengrauen.", "def load_settings(config_path=None)",
                 " ".join(f"word{index}" for index in range(400))]

        assert isinstance(fresh_models.get_embedding_model(), fresh_models._OnnxMiniLM)
        onnx = np.array(fresh_models.compute_embeddings(texts))
        single = np.array(fresh_models.compute_embedding(texts[1]))
        try:
            model = sentence_transformers.SentenceTransformer("all-MiniLM-L6-v2", device="cpu", local_files_only=True)
        except OSError:  # not cached here: fetched once
            model = sentence_transformers.SentenceTransformer("all-MiniLM-L6-v2", device="cpu")
        reference = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)

        np.testing.assert_allclose(onnx, reference, atol=1e-5)  # direction and unit length
        np.testing.assert_allclose(single, reference[1], atol=1e-5)  # the one-text path too

    def test_another_model_without_sentence_transformers_names_what_is_missing(self, fresh_models, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "sentence_transformers", None)

        with pytest.raises(ImportError) as raised:
            fresh_models._load_embedding_model("paraphrase-multilingual-MiniLM-L12-v2")

        assert "paraphrase-multilingual-MiniLM-L12-v2" in str(raised.value)
        assert "sentence-transformers" in str(raised.value)

    def test_a_model_loading_holds_up_no_other(self, fresh_models, monkeypatch):
        """premise_dedupe's SentenceTransformer loads for seconds; the chat's MiniLM does not wait for it."""
        import sys
        import threading
        from types import SimpleNamespace
        entered, release = threading.Event(), threading.Event()

        class Slow:
            def __init__(self, name, **kwargs):
                entered.set()
                release.wait(30)
        monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Slow))
        monkeypatch.setattr(fresh_models, "_OnnxMiniLM", lambda: "the ONNX model")
        other = threading.Thread(target=fresh_models._load_embedding_model, args=("paraphrase-multilingual-MiniLM-L12-v2",))
        other.start()
        try:
            assert entered.wait(10), "fixture: the other model never started loading"
            loaded = []
            first = threading.Thread(target=lambda: loaded.append(fresh_models.get_embedding_model()))
            first.start()
            first.join(5)
            assert loaded == ["the ONNX model"], "the default model waited for another one"
        finally:
            release.set()
            other.join(10)
            first.join(10)

    def test_both_names_first_used_at_once_build_one_model(self, fresh_models, monkeypatch):
        """The two names used to take two locks: without the file lock both unpacked the same folder at once. The
        second name arrives while the first is still building -- one lock, one model, the same one for both."""
        import threading
        entered, release = threading.Event(), threading.Event()
        built, got = [], []

        def build():
            built.append(1)
            entered.set()
            release.wait(10)
            return object()
        monkeypatch.setattr(fresh_models, "_OnnxMiniLM", build)
        first = threading.Thread(target=lambda: got.append(
            fresh_models._load_embedding_model("sentence-transformers/all-MiniLM-L6-v2")))
        second = threading.Thread(target=lambda: got.append(fresh_models.get_embedding_model()))
        first.start()
        try:
            assert entered.wait(10), "fixture: the first name never started building"
            second.start()
            second.join(0.5)  # time to build a second copy, if its lock were another
        finally:
            release.set()
            first.join(10)
            if second.ident:
                second.join(10)

        assert len(built) == 1 and len(got) == 2 and got[0] is got[1], (built, got)

    @pytest.mark.parametrize("texts, named", [(["a text", None], "NoneType"), (b"one text", "bytes"), (None, "NoneType")])
    def test_a_text_that_is_no_str_is_named(self, fresh_models, texts, named):
        """Named before anything is sorted -- and one value is one text: bytes are not taken apart into ints."""
        model = fresh_models._OnnxMiniLM.__new__(fresh_models._OnnxMiniLM)  # the check comes before the model

        with pytest.raises(TypeError, match=f"must be a str, got {named}"):
            model.encode(texts)

    def test_one_embedding_is_of_one_text(self, fresh_models):
        """A list came back as one vector per item, where the caller stores one."""
        with pytest.raises(TypeError, match="must be a str, got list"):
            fresh_models.compute_embedding(["a", "b"])

    def test_the_fallback_store_without_a_model_refuses_instead_of_hashing(self, fresh_models, monkeypatch):
        """sqlite-vec is the backend without chromadb: with no sentence-transformers either, it stored hash
        vectors for good, and every later query ranked by noise."""
        import sys
        from agent_system.utils.vector_store import store as store_module
        monkeypatch.setattr(store_module, "get_vector_backend", lambda: "sqlite-vec")
        for name in ("chromadb", "sentence_transformers"):
            monkeypatch.setitem(sys.modules, name, None)
        tmpdir_obj = create_temp_dir()
        store = VectorStore(persist_path=tmpdir_obj.name)
        try:
            with pytest.raises(ImportError) as raised:
                store.add(collection="docs", ids=["doc1"], documents=["Hello world"])

            assert "chromadb" in str(raised.value) and "sentence-transformers" in str(raised.value), raised.value
            assert store.count("docs") == 0
        finally:
            store.close()
            gc.collect()
            tmpdir_obj.cleanup()


#: The source tree these tests are about -- a worktree's, not the one an editable install points at.
_UNDER_TEST = Path(__import__("agent_system.utils.vector_store", fromlist=["x"]).__file__).resolve()
_CACHED_ARCHIVE = Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2" / "onnx.tar.gz"


def _child(script, *args, timeout, popen=False):
    """A Python process that imports the tree under test: PYTHONPATH names its src/ first, since an
    editable install would hand every child the main checkout instead."""
    import os
    import subprocess
    import sys
    source = str(_UNDER_TEST.parents[3])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [source, os.environ.get("PYTHONPATH")]))}
    command = [sys.executable, "-c", script, *map(str, args)]
    if popen:
        return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)


def _link_or_copy(source: Path, target: Path) -> None:
    """A hard link where the volume allows it: the archive is 83 MB and read only."""
    import os
    import shutil
    try:
        os.link(source, target)
    except OSError:
        shutil.copy(source, target)


@pytest.fixture(scope="session")
def _unpacked_model(tmp_path_factory):
    """chromadb's model unpacked once per run, from the cached archive -- no network."""
    import tarfile
    if not _CACHED_ARCHIVE.is_file():
        pytest.skip("chromadb's model archive is not cached here")
    root = tmp_path_factory.mktemp("onnx_model")
    with tarfile.open(_CACHED_ARCHIVE, "r:gz") as tar:
        tar.extractall(root, filter="data")
    return root / "onnx"


class TestTheOnnxModelIsReadiedOnce:
    """chromadb downloads and unpacks its model on first use without a lock and checks only that the files
    exist: eight first uses at once failed seven times, and a model.onnx cut short failed for good."""

    @pytest.fixture
    def model_dir(self, tmp_path, monkeypatch):
        """A model folder of this test's own holding only the archive: the first use unpacks it."""
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
        from agent_system.utils.vector_store import embeddings
        if not _CACHED_ARCHIVE.is_file():
            pytest.skip("chromadb's model archive is not cached here")
        folder = tmp_path / "all-MiniLM-L6-v2"
        folder.mkdir()
        _link_or_copy(_CACHED_ARCHIVE, folder / "onnx.tar.gz")  # verified by its hash: nothing is downloaded
        monkeypatch.setattr(ONNXMiniLM_L6_V2, "DOWNLOAD_PATH", folder)
        monkeypatch.setattr(embeddings, "_embedding_models", {})
        return folder

    @pytest.fixture
    def ready_dir(self, model_dir, _unpacked_model):
        """The same, already unpacked -- linked from the run's one unpack. A test that changes a file replaces
        it rather than writing into it, or it would change the run's copy for every other test."""
        (model_dir / "onnx").mkdir()
        for file in _unpacked_model.iterdir():
            _link_or_copy(file, model_dir / "onnx" / file.name)
        return model_dir

    @staticmethod
    def offline(monkeypatch):
        import httpx
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

        def refuse(self, url, fname, chunk_size=1024):
            raise httpx.ConnectError("[WinError 10061] connection refused")
        monkeypatch.setattr(ONNXMiniLM_L6_V2, "_download", refuse)

    def test_a_model_cut_short_is_unpacked_again_from_the_archive(self, ready_dir, monkeypatch):
        from agent_system.utils.vector_store.embeddings import _OnnxMiniLM
        self.offline(monkeypatch)  # from the archive, not a download
        model = ready_dir / "onnx" / "model.onnx"
        short = model.read_bytes()[:1000]
        model.unlink()  # the link goes, the run's copy stays whole
        model.write_bytes(short)  # an unpack that was interrupted

        vector = _OnnxMiniLM().encode("a text")

        assert len(vector) == 384 and model.stat().st_size > 1_000_000

    def test_eight_first_uses_at_once_all_succeed(self, model_dir):
        from concurrent.futures import ThreadPoolExecutor
        from agent_system.utils.vector_store.embeddings import _OnnxMiniLM
        assert not (model_dir / "onnx").exists(), "fixture: the model must not be unpacked yet"

        with ThreadPoolExecutor(8) as pool:
            outcomes = list(pool.map(lambda _: _try(lambda: len(_OnnxMiniLM().encode("a text"))), range(8)))

        assert outcomes == [384] * 8, outcomes

    @pytest.mark.timeout(300)
    def test_processes_started_one_after_another_all_succeed(self, model_dir):
        """The file lock covers other processes. Started staggered, while the first still unpacks: measured
        28.09.2026, 1 of 8 without the lock, 8 of 8 with it."""
        import time
        assert not (model_dir / "onnx").exists(), "fixture: the model must not be unpacked yet"
        script = "\n".join([
            "import sys",
            "from pathlib import Path",
            "from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2",
            "ONNXMiniLM_L6_V2.DOWNLOAD_PATH = Path(sys.argv[1])",
            "import agent_system.utils.vector_store as vector_store",
            "print(vector_store.__file__)",
            "print(len(vector_store.embeddings._OnnxMiniLM().encode('a text')))",
        ])
        runs = []
        for _ in range(8):
            runs.append(_child(script, model_dir, timeout=None, popen=True))
            time.sleep(0.25)
        outcomes = []
        for run in runs:
            out, err = run.communicate(timeout=240)
            lines = out.strip().splitlines()
            if run.returncode == 0 and Path(lines[-2]) == _UNDER_TEST:
                outcomes.append(lines[-1])
            else:
                outcomes.append(f"failed: {lines[-2:] if lines else ''} {err.strip()[-160:]}")

        assert outcomes == ["384"] * 8, outcomes

    def test_a_lock_file_it_cannot_open_is_not_waited_for(self, ready_dir, monkeypatch):
        """On Windows filelock takes "access denied" for a lock another process holds, and waits out its timeout
        (measured 28.09.2026 on a folder without write access). The stand-in waits as it does; a folder where the
        lock file belongs is a file this process cannot open. The readying must not sit out the wait."""
        import time
        from agent_system.utils.vector_store import embeddings

        class WaitsLikeFilelockOnAccessDenied:
            def __init__(self, path):
                pass

            def acquire(self, timeout):
                time.sleep(timeout)
                raise TimeoutError("could not acquire the lock")

            def release(self):
                pass
        monkeypatch.setattr(embeddings, "FileLock", WaitsLikeFilelockOnAccessDenied)
        monkeypatch.setattr(embeddings, "_MODEL_LOCK_TIMEOUT", 8.0)
        (ready_dir.parent / f"{ready_dir.name}.lock").mkdir()

        started = time.monotonic()
        vector = embeddings._OnnxMiniLM().encode("a text")

        assert len(vector) == 384 and time.monotonic() - started < 6, time.monotonic() - started

    @pytest.mark.parametrize("failure, level", [("timed out", "WARNING"), ("no flock", "DEBUG"), ("read-only", "DEBUG")])
    def test_without_the_lock_the_readying_still_runs_and_says_why(self, tmp_path, monkeypatch, caplog, failure, level):
        """Ten minutes waited is the race the lock is there for: said above DEBUG. A file system without flock
        raises NotImplementedError, no OSError. And an error of the readying is its own, not the lock's."""
        import filelock
        from agent_system.utils.vector_store import embeddings
        error = {"timed out": filelock.Timeout(str(tmp_path / "model.lock")),
                 "no flock": NotImplementedError("flock: function not implemented"),
                 "read-only": OSError(30, "Read-only file system")}[failure]

        class Refuses:
            def __init__(self, path):
                pass

            def acquire(self, timeout):
                raise error

            def release(self):
                raise AssertionError("released a lock it never held")
        monkeypatch.setattr(embeddings, "FileLock", Refuses)
        caplog.set_level("DEBUG", logger=embeddings.logger.name)

        with pytest.raises(ValueError) as raised:
            with embeddings._cache_lock(tmp_path / "model"):
                raise ValueError("the model did not open")

        assert raised.value.__context__ is None, raised.value.__context__
        said = [record.levelname for record in caplog.records if record.name == embeddings.logger.name]
        assert said == [level], said

    @pytest.mark.parametrize("answer, waits", [("ENOLCK", False), ("EWOULDBLOCK", True), ("EACCES", True), (None, True)],
                             ids=["refused", "held-elsewhere", "held-on-cifs", "granted"])
    def test_a_file_system_refusing_flock_is_not_waited_out(self, tmp_path, monkeypatch, answer, waits):
        """filelock takes any flock refusal but ENOSYS -- ENOLCK on NFS without lockd -- for a lock held elsewhere
        and waits the whole timeout out; asked first, the readying goes on without the lock at once. A lock that is
        held, or free, is still taken through filelock."""
        import errno
        import sys
        import types
        from agent_system.utils.vector_store import embeddings

        def flock(fd, operation):
            if answer:
                raise OSError(getattr(errno, answer), answer)
        monkeypatch.setitem(sys.modules, "fcntl", types.SimpleNamespace(flock=flock, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8))
        taken = []
        monkeypatch.setattr(embeddings.FileLock, "acquire", lambda self, *args, **kwargs: taken.append(True))

        with embeddings._cache_lock(tmp_path / "model"):
            pass

        assert bool(taken) is waits, taken

    def test_a_soft_lock_is_taken_not_waited_for(self, tmp_path, monkeypatch, caplog):
        """Without fcntl filelock falls back to a soft lock, whose file's existence is the lock: opened in advance,
        as Windows needs, it was never to be had, and every first use waited the whole timeout."""
        import filelock
        from agent_system.utils.vector_store import embeddings
        monkeypatch.setattr(embeddings, "FileLock", filelock.SoftFileLock)
        monkeypatch.setattr(embeddings, "_MODEL_LOCK_TIMEOUT", 2.0)
        lock_file = tmp_path / "model.lock"

        with embeddings._cache_lock(tmp_path / "model"):
            held = lock_file.exists()

        assert held and not lock_file.exists(), "the soft lock was not taken, or not given back"
        assert not [record for record in caplog.records if record.levelname == "WARNING"], caplog.text

    def test_without_the_archive_a_model_that_does_not_open_is_not_deleted(self, ready_dir, monkeypatch):
        """Unpacking again needs the archive; without it the folder is all there is, whatever failed."""
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
        from agent_system.utils.vector_store import embeddings
        (ready_dir / "onnx.tar.gz").unlink()  # provisioned as a folder only

        def failing(self, input):
            raise RuntimeError("the execution provider failed")
        monkeypatch.setattr(ONNXMiniLM_L6_V2, "__call__", failing)

        with pytest.raises(RuntimeError, match="could not be readied") as raised:
            embeddings._OnnxMiniLM()

        assert (ready_dir / "onnx" / "model.onnx").is_file()
        said = str(raised.value)  # the remedy before the cause: file_ops keeps the first 300 characters
        assert "ready it once" in said[:300] and said.index("ready it once") < said.index("the execution provider"), said

    def test_the_model_never_runs_on_directml(self, ready_dir, monkeypatch):
        """DirectML takes one Run at a time per session; this one session serves every thread of the process."""
        from agent_system.utils.vector_store import embeddings
        monkeypatch.setattr(embeddings, "get_available_onnx_providers",
                            lambda: ["DmlExecutionProvider", "CPUExecutionProvider"])

        model = embeddings._OnnxMiniLM()

        assert "DmlExecutionProvider" not in model._function.model.get_providers()

    def test_one_onnx_run_takes_at_most_32_texts_padded_to_the_longest(self, ready_dir):
        """At 128 texts at once onnxruntime's arena grew the process by 0.9-1.4 GB for good, and it ran slower;
        padded to 256 tokens like chromadb's call, a short text cost 7.5 times as long."""
        import numpy as np
        from agent_system.utils.vector_store import embeddings
        model = embeddings._OnnxMiniLM()
        session = model._function.model
        widths, rows = [], []

        class Recording:
            def run(self, names, feed):
                rows.append(len(feed["input_ids"]))
                widths.append(feed["input_ids"].shape[1])
                return session.run(names, feed)
        long = " ".join(f"word{index}" for index in range(150))
        # short and long interleaved: sorted by length, the short ones share batches of their own
        texts = [f"text number {index}" if index % 2 else f"{long} {index}" for index in range(100)]
        alone = np.array([model.encode(text) for text in texts[:5]])
        model._function.__dict__["model"] = Recording()  # chromadb's cached_property, shadowed

        vectors = model.encode(texts, batch_size=128)

        assert max(rows) <= 32 and sum(rows) == 100, rows
        assert min(widths) < 16, widths  # "text number 99" is a handful of tokens, not 256
        np.testing.assert_allclose(vectors[:5], alone, atol=1e-5)
        assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-4)  # no row left unwritten

    def test_first_uses_at_once_load_one_model(self, ready_dir):
        from concurrent.futures import ThreadPoolExecutor
        from agent_system.utils import vector_store

        with ThreadPoolExecutor(8) as pool:
            models = list(pool.map(lambda _: vector_store.get_embedding_model(), range(8)))

        assert len({id(model) for model in models}) == 1

    def test_a_chroma_store_embeds_through_the_same_model(self, ready_dir, monkeypatch, tmp_path):
        """Documents and queries without vectors went through the collection's own function: unlocked on first
        use, padded to 256 tokens. They go through compute_embeddings now -- the same vectors."""
        import numpy as np
        from agent_system.utils import vector_store
        from agent_system.utils.vector_store import chroma
        if vector_store.get_vector_backend() != "chromadb":
            pytest.skip("the chromadb backend is not installed")
        calls = []
        real_many, real_one = vector_store.compute_embeddings, vector_store.compute_embedding
        monkeypatch.setattr(chroma, "compute_embeddings", lambda texts, **kw: calls.append(len(texts)) or real_many(texts, **kw))
        monkeypatch.setattr(chroma, "compute_embedding", lambda text: calls.append(1) or real_one(text))
        store = VectorStore(persist_path=tmp_path / "store")
        try:
            store.add("docs", ids=["a", "b"], documents=["the config loader", "a German scene in the village"])
            stored = store._backend._get_collection("docs").get(ids=["a"], include=["embeddings"])["embeddings"][0]
            hits = store.query("docs", query_text="how the config is loaded", n_results=1)
        finally:
            store.close()

        assert calls == [2, 1], calls
        np.testing.assert_allclose(stored, real_one("the config loader"), atol=1e-5)
        assert hits["ids"][0][0] == "a", hits

    def test_offline_the_error_names_the_model_where_it_goes_and_whence(self, tmp_path, monkeypatch):
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
        from agent_system.utils.vector_store import embeddings
        folder = tmp_path / "all-MiniLM-L6-v2"
        monkeypatch.setattr(ONNXMiniLM_L6_V2, "DOWNLOAD_PATH", folder)
        self.offline(monkeypatch)

        with pytest.raises(RuntimeError) as raised:
            embeddings._OnnxMiniLM()

        message = str(raised.value)
        assert "all-MiniLM-L6-v2" in message and str(folder) in message and ONNXMiniLM_L6_V2.MODEL_DOWNLOAD_URL in message


def _try(call):
    try:
        return call()
    except Exception as error:  # the outcome of each thread, failures included
        return f"{type(error).__name__}: {error}"[:120]
