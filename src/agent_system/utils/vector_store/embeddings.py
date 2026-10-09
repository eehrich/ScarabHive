"""The embedding model of this process: all-MiniLM-L6-v2, readied once and shared by every caller.

It holds the ONNX provider detection, the bundled MiniLM embedder that runs on
chromadb's ONNX export without torch (`_OnnxMiniLM`), the loading of a model
once per process behind a lock file in its cache (`_load_embedding_model`,
`_cache_lock`), and the helpers that need no store at all
(`compute_embedding(s)`, `cosine_similarity`, `texts_are_duplicate`). Its own
module because nothing here knows a backend: the help index and file_ops embed
through it directly, and both backends of `VectorStore` embed through it.
"""

import contextlib
import errno
import logging
import math
import threading
from pathlib import Path
from typing import Any, List, Optional

from filelock import FileLock, SoftFileLock, Timeout

logger = logging.getLogger(__name__)

# Vector dimension for MiniLM-L6-v2 (default embedding model)
EMBEDDING_DIM = 384

# Cached provider detection
_ONNX_PROVIDERS: Optional[List[str]] = None


def get_available_onnx_providers() -> List[str]:
    """Detect available ONNX Runtime execution providers.
    
    Returns providers in priority order: GPU first, then CPU fallback.
    Result is cached for performance.
    """
    global _ONNX_PROVIDERS
    if _ONNX_PROVIDERS is not None:
        return _ONNX_PROVIDERS

    try:
        import onnxruntime as ort
        available = ort.get_available_providers()
        logger.debug(f"Available ONNX providers: {available}")
        
        # Prefer GPU providers, fallback to CPU
        preferred_order = [
            "CUDAExecutionProvider",
            "ROCMExecutionProvider", 
            "DmlExecutionProvider",  # DirectML for Windows
            "CoreMLExecutionProvider",  # Apple Silicon
            "AzureExecutionProvider",
            "CPUExecutionProvider"
        ]
        
        # Filter to only available providers, maintaining priority order
        _ONNX_PROVIDERS = [p for p in preferred_order if p in available]
        
        # If none of our preferred are available, use whatever is available
        if not _ONNX_PROVIDERS:
            _ONNX_PROVIDERS = available if available else ["CPUExecutionProvider"]
        
        logger.info(f"Selected ONNX providers: {_ONNX_PROVIDERS}")
        return _ONNX_PROVIDERS
        
    except ImportError:
        logger.warning("onnxruntime not installed, using CPU provider only")
        _ONNX_PROVIDERS = ["CPUExecutionProvider"]
        return _ONNX_PROVIDERS
    except Exception as e:
        logger.warning(f"Failed to detect ONNX providers: {e}, using CPU")
        _ONNX_PROVIDERS = ["CPUExecutionProvider"]
        return _ONNX_PROVIDERS


# ---------------------------------------------------------------------------
# Standalone embedding utilities (no VectorStore instance required)
# ---------------------------------------------------------------------------
_embedding_models: dict = {}

#: The model chromadb ships as an ONNX export -- the one model that runs without torch.
_ONNX_MODEL_NAMES = {"all-MiniLM-L6-v2", "sentence-transformers/all-MiniLM-L6-v2"}

#: Each model loaded once in this process: eight first uses at once built eight copies. One lock
#: per model (both names of all-MiniLM-L6-v2 share one) -- a SentenceTransformer loading for
#: seconds holds up no other model.
_LOADING: dict[str, threading.Lock] = {}

#: The longest one process waits for another to ready the model: an unpack takes seconds, a
#: download a minute. Past it the model is readied without the lock.
_MODEL_LOCK_TIMEOUT = 600.0


def _refuses_flock(file: Any) -> None:
    """Raise where the file system refuses flock itself -- ENOLCK on NFS without lockd, EOPNOTSUPP.

    filelock takes any refusal but ENOSYS for a lock held elsewhere and waits the
    whole timeout out; a lock that is held answers EWOULDBLOCK -- or EACCES, where
    flock is emulated through fcntl locks and on CIFS. Without fcntl (Windows)
    there is nothing to ask.
    """
    try:
        import fcntl
    except ImportError:
        return
    try:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
            return  # held elsewhere: waited for below
        raise
    fcntl.flock(file.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _cache_lock(model_dir: Path):
    """A lock file beside the model's cache folder, held while one process -- or thread -- readies the model.

    Threads as well: each takes the lock through a handle of its own, and the
    operating system serializes them as it does processes.

    A cache the process may not write -- a unit under ProtectHome=read-only, as
    the writer worker's -- gets none: nothing can be downloaded or unpacked there
    either, the model is provisioned or it is not.
    """
    lock_path = model_dir.parent / f"{model_dir.name}.lock"
    lock = FileLock(str(lock_path))
    held = False
    try:
        model_dir.parent.mkdir(parents=True, exist_ok=True)
        # Asked first: filelock takes "access denied" on Windows for a lock another process
        # holds, and waits out the whole timeout (measured 28.09.2026). Not for a soft lock (no fcntl):
        # there the file's existence is the lock, and this open would take it for good.
        if FileLock is not SoftFileLock:
            with open(lock_path, "a", encoding="utf-8") as probe:
                _refuses_flock(probe)
        lock.acquire(timeout=_MODEL_LOCK_TIMEOUT)
        held = True
    except Timeout:  # an OSError too: caught first
        logger.warning("Waited %.0f s for %s; readying the embedding model without it",
                       _MODEL_LOCK_TIMEOUT, lock_path)
    except (OSError, NotImplementedError) as error:  # read-only; a file system refusing flock
        logger.debug("no lock for the embedding model cache %s: %s", model_dir, error)
    # Outside the except: an error of the readying is not "during handling of" the lock's.
    try:
        yield
    finally:
        if held:
            lock.release()


class _OnnxMiniLM:
    """all-MiniLM-L6-v2 through chromadb's ONNX export, with the `encode` of a SentenceTransformer.

    The vectors are sentence-transformers' own (cosine 1.000000 over code, German
    prose over 256 tokens, short queries and non-Latin scripts), and what it saves
    is torch, which the core needed for this model alone. It runs the export
    itself instead of through chromadb's call, which pads every text to 256
    tokens: here a batch is sorted by length and padded to its longest text, as
    sentence-transformers does. Measured 28.09.2026 on 2000 file_ops documents:
    7.7 ms a text against 7.3 for sentence-transformers (chromadb's call: 13.9);
    a single query 1.4 against 6.4 ms. The vectors always come back at unit
    length, `normalize_embeddings` or not -- every store and comparison here
    ranks by cosine, where the length is moot.
    """

    #: sentence-transformers' max_seq_length for this model; chromadb truncates there too.
    MAX_TOKENS = 256

    #: Texts per ONNX run, whatever the caller asks: chromadb's call ran 32; at 128 onnxruntime's
    #: arena grew the process by 0.9-1.4 GB for good and ran slower (measured 28.09.2026).
    MAX_BATCH = 32

    def __init__(self) -> None:
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
        # Never DirectML: it takes one Run at a time per session (onnxruntime's DirectML docs), and this
        # one session serves every store and thread of the process.
        providers = [name for name in get_available_onnx_providers() if name != "DmlExecutionProvider"]
        self._function = ONNXMiniLM_L6_V2(preferred_providers=providers or ["CPUExecutionProvider"])
        self._tokenizer = self._ready()
        # Configured once, read by every thread after: a tokenizer changed while in use raises.
        self._tokenizer.enable_truncation(max_length=self.MAX_TOKENS)
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")  # to the batch's longest

    def _ready(self) -> Any:
        """Download, unpack and open the model, one process at a time; its tokenizer.

        chromadb does it on first use without a lock and checks only that the
        files exist: measured 28.09.2026, eight first uses at once failed seven
        times opening a model.onnx another one was still unpacking, and a file
        cut short by an interrupted unpack failed on every later call. A model
        that does not open is unpacked again from the archive, which is verified
        by its hash -- only where the archive is there: without it the folder is
        all there is.
        """
        import shutil
        function = self._function
        model_dir = Path(function.DOWNLOAD_PATH)
        folder = model_dir / function.EXTRACTED_FOLDER_NAME
        with _cache_lock(model_dir):
            try:
                function(["ready"])
            except Exception as error:
                if not (model_dir / function.ARCHIVE_FILENAME).is_file():
                    raise self._not_ready(model_dir, error) from error
                logger.warning("ONNX embedding model did not open (%s); unpacking it again", error)
                shutil.rmtree(folder, ignore_errors=True)
                try:
                    function(["ready"])
                except Exception as again:
                    raise self._not_ready(model_dir, again) from again
            # Under the lock: a process unpacking again cannot take the file away mid-read.
            return function.Tokenizer.from_file(str(folder / "tokenizer.json"))

    def _not_ready(self, model_dir: Path, error: Exception) -> RuntimeError:
        # chromadb's own error names neither the model nor where it goes (a bare ConnectError offline).
        # The remedy first: callers cut the message (file_ops keeps 300 characters).
        return RuntimeError(
            f"The embedding model all-MiniLM-L6-v2 could not be readied in {model_dir}: ready it once, as this "
            f"user, where it may write and reach {self._function.MODEL_DOWNLOAD_URL}. Cause: {error}")

    def encode(self, texts, batch_size: int = 32, normalize_embeddings: bool = False,
               convert_to_numpy: bool = True, show_progress_bar: bool = False):
        import numpy as np
        # anything but a list of texts is one text: bytes or None must not be taken apart first
        single = not isinstance(texts, (list, tuple, np.ndarray))
        texts = [texts] if single else list(texts)
        for text in texts:  # else the sort below fails on len(None)
            if not isinstance(text, str):
                raise TypeError(f"A text to embed must be a str, got {type(text).__name__}")
        vectors = np.empty((len(texts), EMBEDDING_DIM), dtype=np.float32)
        order = sorted(range(len(texts)), key=lambda index: len(texts[index]))
        step = min(max(1, batch_size), self.MAX_BATCH)
        for start in range(0, len(order), step):
            batch = order[start:start + step]
            encoded = self._tokenizer.encode_batch([texts[index] for index in batch])
            ids = np.array([one.ids for one in encoded], dtype=np.int64)
            mask = np.array([one.attention_mask for one in encoded], dtype=np.int64)
            hidden = self._function.model.run(
                None, {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)})[0]
            weights = mask[..., None].astype(np.float32)
            pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
            vectors[batch] = pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
        return vectors[0] if single else vectors


def _load_embedding_model(model_name: str):
    """The embedding model *model_name*, loaded once for the lifetime of the process.

    all-MiniLM-L6-v2 runs on chromadb's ONNX export when chromadb is there.
    Any other model is a SentenceTransformer: sentence-transformers (and with
    it torch) is then the dependency of the plugin that names the model.
    """
    # One model under both of its names: one copy and one lock, not two unpacking the same folder.
    key = "all-MiniLM-L6-v2" if model_name in _ONNX_MODEL_NAMES else model_name
    model = _embedding_models.get(key)
    if model is not None:
        return model
    with _LOADING.setdefault(key, threading.Lock()):
        if key in _embedding_models:  # loaded while this caller waited
            return _embedding_models[key]
        import importlib.util
        if key in _ONNX_MODEL_NAMES and importlib.util.find_spec("chromadb") is not None:
            _embedding_models[key] = _OnnxMiniLM()
            logger.info(f"Embedding model loaded ({model_name}, ONNX)")
            return _embedding_models[key]
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            needs = ("chromadb (its ONNX export) or sentence-transformers" if model_name in _ONNX_MODEL_NAMES
                     else "sentence-transformers (pip install sentence-transformers); "
                          "only all-MiniLM-L6-v2 runs without it")
            raise ImportError(f"Embedding model {model_name!r} needs {needs}") from error
        try:
            _embedding_models[key] = SentenceTransformer(
                model_name, device="cpu", local_files_only=True,
            )
        except OSError:
            _embedding_models[key] = SentenceTransformer(
                model_name, device="cpu",
            )
        logger.info(f"SentenceTransformer loaded ({model_name})")
        return _embedding_models[key]


def get_embedding_model():
    """Lazy-load and cache the default embedding model (all-MiniLM-L6-v2)."""
    return _load_embedding_model("all-MiniLM-L6-v2")


def compute_embedding(text: str) -> List[float]:
    """Return 384-dim embedding for *text*."""
    if not isinstance(text, str):  # a list here came back as one vector per item
        raise TypeError(f"A text to embed must be a str, got {type(text).__name__}")
    model = get_embedding_model()
    return model.encode(text, convert_to_numpy=True).tolist()


def compute_embeddings(texts: List[str], batch_size: int = 128,
                       normalize: bool = True) -> List[List[float]]:
    """Embed many texts in one batched pass.

    Measured on this machine (18.09.2026, CPU, all-MiniLM-L6-v2): letting the
    store embed document by document costs 26 ms each -- 22 minutes for the
    51.730 documents of one code tree. The same texts through one batched
    encode take about 8 ms each, the store write included. Callers that index
    a corpus should compute the vectors here and hand them to
    :meth:`VectorStore.add`.

    `normalize` is on by default so that an L2 store ranks by direction, which
    is what a text query means. A caller that normalises its documents MUST
    normalise its queries the same way -- mixing the two compares vectors of
    different length and quietly reorders the results.
    """
    if not texts:
        return []
    model = get_embedding_model()
    vectors = model.encode(texts, batch_size=batch_size,
                           normalize_embeddings=normalize,
                           convert_to_numpy=True, show_progress_bar=False)
    return [vector.tolist() for vector in vectors]


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two equal-length float vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def texts_are_duplicate(
    text_a: str,
    text_b: str,
    threshold: float = 0.85,
) -> tuple[bool, float]:
    """Check whether two texts are near-duplicates via embedding similarity.

    Returns ``(is_duplicate, similarity_score)``.

    Typical cosine ranges for German prose:
    - Unrelated scenes: 0.30 – 0.50
    - Same setting, advancing plot: 0.50 – 0.70
    - True continuation (same characters, different events): 0.70 – 0.85
    - Near-identical / duplicate content: > 0.85
    """
    if not text_a.strip() or not text_b.strip():
        return False, 0.0
    emb_a = compute_embedding(text_a)
    emb_b = compute_embedding(text_b)
    sim = cosine_similarity(emb_a, emb_b)
    return sim >= threshold, sim
