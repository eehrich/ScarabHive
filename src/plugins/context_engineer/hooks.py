"""Context Engineer Plugin - Hook implementations.

This module implements the main hook for context engineering, which applies
layered compaction strategies to optimize context usage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import threading
import time
from dataclasses import fields
from pathlib import Path
from typing import Any

from agent_system.hooks import HookContext, HookResult, SchemaBasedPluginHook
from agent_system.llm.models import ChatMessage
from agent_system.mcp.status import StatusScope, status_bus

from .archival_memory import ARCHIVAL_COLLECTION, ArchivalMemory
from .compaction import (
    PLUGIN_LEVEL_KEYS,
    CompactionConfig,
    LayeredCompactionStrategy,
    _coerce,
    compaction_config_from,
    unknown_config_keys,
)
from .core_memory import CoreMemory
from .media_store import MediaStore
from .paging import (
    find_in_text,
    slice_text,
)
from .tool_result_store import ToolResultStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared vector store
#
# chromadb parks one System per persist directory in a process-global
# registry, and each System owns a tokio runtime (~6 descriptors, 4 threads).
# Giving every session its own directory therefore meant one runtime PER
# SESSION — 229 of them on the writer host, which exhausted the API's
# descriptor limit and turned every API-key request into a 401. The vectors
# never needed separate directories: every write carries the session_id and
# every query filters on it. So all sessions — and all plugin instances in
# the process — share one store per storage base, and the runtime count stays
# constant however many sessions run.
# ---------------------------------------------------------------------------
_SHARED_VECTOR_STORES: dict[str, Any] = {}
_SHARED_VECTOR_STORES_LOCK = threading.Lock()
_SHARED_VECTORS_DIRNAME = "_shared_vectors"
# A directory under the storage base is a session directory iff it holds one
# of these. The TTL sweep deletes nothing else — not the shared vector
# directory, not history.json, not whatever another plugin may drop there.
_SESSION_MARKERS = ("archive.db", "tool_results.db", "core_memory.json")
# A directory is renamed to <session_id> + this before rmtree. The rename is
# atomic, so a session coming alive meanwhile gets a fresh directory instead
# of writing into one being torn down — and a sweep stopped mid-rmtree (the
# thread is a daemon; interpreter shutdown does not wait) leaves a leftover
# the next run recognises, not a marker-less directory it would skip forever.
_SWEEPING_SUFFIX = ".sweeping"
# Sweep bookkeeping per storage base, process-wide: however many plugin
# instances exist, one base is swept by one thread at a time, once per
# interval. Per-instance state let a test process with dozens of instances
# run dozens of concurrent sweeps against the same directory.
_SWEEP_LOCK = threading.Lock()
_SWEEP_LAST_RUN: dict[str, float] = {}
_SWEEP_RUNNING: set[str] = set()
# A failed store construction is retried after this long. Caching it for
# good would pin every session to its own store — the leak this exists to
# end — over a transient cause such as another process on the file.
_SHARED_STORE_RETRY_SECONDS = 600.0
_SHARED_STORE_FAILED_AT: dict[str, float] = {}


def _shared_vector_store(storage_base: Path, create: bool = True) -> Any | None:
    """The process-wide VectorStore for *storage_base*, or None.

    None means "keep the per-session layout": either the backend is not
    chromadb (sqlite-vec ignores metadata filters, and without them a shared
    store would rank other sessions' messages — final, a property of the
    installation), or the store could not be built just now (logged, retried
    after _SHARED_STORE_RETRY_SECONDS). Each session then builds its own
    store as before and degrades to text search if that fails too — the hook
    itself never goes down over it.
    """
    key = str(storage_base.resolve())
    with _SHARED_VECTOR_STORES_LOCK:
        if key in _SHARED_VECTOR_STORES:
            return _SHARED_VECTOR_STORES[key]
        if not create:
            return None
        failed_at = _SHARED_STORE_FAILED_AT.get(key)
        if failed_at is not None and time.time() - failed_at < _SHARED_STORE_RETRY_SECONDS:
            return None
        store: Any | None = None
        try:
            from agent_system.utils.vector_store import VectorStore, get_vector_backend
            if get_vector_backend() != "chromadb":
                _SHARED_VECTOR_STORES[key] = None
                return None
            store = VectorStore(persist_path=storage_base / _SHARED_VECTORS_DIRNAME)
            store.get_or_create_collection(ARCHIVAL_COLLECTION)
        except Exception as exc:  # noqa: BLE001 — degrade, never take the hook down
            logger.error(
                "shared VectorStore unavailable (%s: %s); sessions fall back to "
                "their own stores or text search, retry in %ds",
                exc.__class__.__name__, exc, _SHARED_STORE_RETRY_SECONDS,
            )
            if store is not None:
                # Built but not usable: it already holds a Chroma runtime, and
                # dropping the reference would leak it — in the very function
                # that exists to stop that.
                try:
                    store.close()
                except Exception:  # noqa: BLE001 — best effort on a failed store
                    pass
            _SHARED_STORE_FAILED_AT[key] = time.time()
            return None
        _SHARED_STORE_FAILED_AT.pop(key, None)
        _SHARED_VECTOR_STORES[key] = store
        logger.info("shared VectorStore initialized at %s", store.persist_path)
        return store

#: What the list tool can browse. Named sections, not guessed ones — the whole
#: point of replacing `recall` is that the caller says which store it means.
CONTEXT_SECTIONS = ("history", "tool_results", "facts")

#: Rows per list page. A map has to fit in the context it is describing.
MAX_LIST_LIMIT = 50

#: Reference shapes, in the order they are tested. Each store owns a distinct
#: prefix, so dispatch is a lookup rather than the heuristic `recall` used.
_REF_PATTERNS = (
    ("tool_result", re.compile(r"\A\$?(TR_[A-Za-z0-9_]+|call_[A-Za-z0-9_]+)\Z")),
    ("message", re.compile(r"\Aarch_[A-Za-z0-9]+\Z")),
    # The `content_hash` the tool_result_ref placeholder carries. Without this
    # the field was advertised in every placeholder — and resent on every turn
    # forever — while _ref_kind rejected it before the retrieve_by_hash
    # fallback below could ever run. Either the field earns its bytes or it
    # should not be in the placeholder; this makes it earn them.
    ("tool_result", re.compile(r"\A[0-9a-f]{8}\Z")),
)

#: File suffixes that mean "a stored media file", not a text reference.
_MEDIA_SUFFIXES = frozenset({
    ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".mp4", ".webm", ".avi", ".mov",
})


def _ref_kind(ref: str) -> str | None:
    """Which store a reference belongs to, or None if it is not a reference."""
    candidate = (ref or "").strip()
    if not candidate:
        return None
    for kind, pattern in _REF_PATTERNS:
        if pattern.match(candidate):
            return kind
    if Path(candidate).suffix.lower() in _MEDIA_SUFFIXES:
        return "media"
    return None


#: How far a pointer chain is followed before giving up. Chains are a defect
#: (see _is_archive_pointer in compaction.py); archives written before the fix
#: hold them up to 20 deep, so the walk has to be generous — but bounded, or a
#: cycle in damaged data would hang the turn.
MAX_ARCHIVE_HOPS = 32


def _resolve_archive_chain(archival: "ArchivalMemory", ref: str):
    """Follow ``archived_ref`` pointers to the entry that holds real content.

    Archives written before compaction stopped re-archiving its own
    placeholders contain pointers to pointers — measured 20 levels deep, with
    the original text alive at the bottom. Without this walk every read of such
    a ref returns another placeholder, and the content is unreachable even
    though it is still there.

    Returns ``(entry_or_None, hops_followed)``.
    """
    seen: set[str] = set()
    current = ref
    hops = 0
    while hops <= MAX_ARCHIVE_HOPS:
        if current in seen:          # damaged data can point in a circle
            return None, hops
        seen.add(current)
        entry = archival.get(current)
        if entry is None:
            return None, hops
        content = entry.content or ""
        if "archived_ref" not in content[:120]:
            return entry, hops
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            return entry, hops
        nxt = data.get("ref_id") if isinstance(data, dict) else None
        if not isinstance(nxt, str) or not nxt:
            return entry, hops
        current, hops = nxt, hops + 1
    return None, hops


def _page_start(offset: int | None, total: int, limit: int) -> int:
    """Where a browse page starts, in file-reader terms.

    ``None`` means the caller named no position and gets the tail — the newest
    entries, which is what an agent asking "what left my context" wants. A
    negative offset counts back from the end, a non-negative one is absolute.
    Same rule as ``slice_text`` inside a single item, so one habit covers both.
    """
    if offset is None:
        return max(0, total - limit)
    offset = int(offset)
    return max(0, total + offset) if offset < 0 else offset


def _one_line(text: str, limit: int) -> str:
    """Collapse to a single bounded line — list rows must stay scannable."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _around(content: str, needle: str, context_chars: int) -> str:
    """The part of ``content`` around the first hit, or its head if none."""
    text = content or ""
    idx = text.lower().find((needle or "").lower())
    if idx < 0:
        return text[: context_chars * 2]
    start = max(0, idx - context_chars)
    end = min(len(text), idx + len(needle) + context_chars)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")

#: Markiert die eigene System-Injektion ("Stored Information"), damit sie beim
#: naechsten Turn ERSETZT statt ein zweites Mal eingefuegt wird. Gleiche
#: Konvention wie ``debate_forum`` (INJECTION_MARKER + ChatMessage.injected_by).
_RESTORATION_MARKER = "context_engineer_restoration"

#: Ueberschrift des Blocks — Fallback fuer Sessions, deren Historie noch
#: unmarkierte Kopien aus der Zeit vor dem Marker enthaelt. Muss zum Text in
#: ``LayeredCompactionStrategy.get_restoration_context`` passen.
_RESTORATION_HEADER = "# Context Engineer - Stored Information"


class ContextEngineerPlugin(SchemaBasedPluginHook):
    """Schema-based plugin for advanced context engineering.
    
    Implements layered compaction strategy:
    1. Reversible: Store tool results and attached files, evict media
    2. Semi-Reversible: Archive old messages with summaries
    3. Irreversible: Drop very old messages
    
    All stored information can be retrieved via MCP tools.
    
    Configuration is loaded from schema.yaml.
    """
    
    def __init__(
        self,
        plugin_dir: Path | str,
        stats_history: list[dict[str, Any]] | None = None,
        history_callback: Any = None
    ):
        """Initialize the context engineer plugin.
        
        Args:
            plugin_dir: Directory containing schema.yaml
            stats_history: Optional list for web UI tracking
            history_callback: Optional callback to invoke after adding history events
        """
        super().__init__(plugin_dir)
        
        self.plugin_dir = Path(plugin_dir)
        self.stats_history = stats_history
        self.history_callback = history_callback
        
        # Session tracking (with TTL to prevent memory leak)
        self._last_compaction_time: dict[str, float] = {}
        self._session_components: dict[str, dict[str, Any]] = {}
        # Sessions with an in-flight compaction. Their stores (tool_store /
        # archival_memory) must NOT be closed by eviction while a compaction
        # holds references and is mid-flight on a worker thread (use-after-close).
        self._active_compactions: set[str] = set()
        # Sessions whose directory already exists while their components are
        # still being built. Neither set above knows them yet, so the TTL
        # sweep consults this one before deleting anything (reserved BEFORE
        # mkdir, released after registration).
        self._creating: set[str] = set()

        self.apply_config(None)

        logger.info(
            f"ContextEngineerPlugin initialized: "
            f"thresholds=L1:{self.layer1_threshold}/L2:{self.layer2_threshold}/"
            f"L3:{self.layer3_threshold}, target={self.target_tokens}, "
            f"min_time_between={self.min_time_between}s, "
            f"deduplicate_media={self.deduplicate_media}, "
            f"compact_media_after_user_message={self.compact_media_after_user_message}, "
            f"compact_media_after_final_response={self.compact_media_after_final_response}, "
            f"always_compact_media_keep_last={self.always_compact_media_keep_last}, "
            f"max_messages={self.max_messages}"
        )

    def apply_config(self, values: dict[str, Any] | None) -> None:
        """Take the plugin's configuration as ONE mapping.

        ``values`` comes from config/plugins.yaml and wins over the defaults in
        schema.yaml. Every CompactionConfig field is set as an attribute of the
        same name, because the compaction thresholds are also read directly
        here (engineer_context checks them before deciding to run at all).

        This replaces three hand-maintained lists that all had to name the same
        key — read it in server.py, copy it onto this object, name it a third
        time when constructing CompactionConfig. A key missing from any of them
        was dropped in silence. Measured on the shipped config: 5 of 25
        settings never arrived. A loop over the dataclass fields cannot forget
        one, and unknown_config_keys() covers the other half — a key whose NAME
        is wrong maps to nothing and now says so.
        """
        merged = {**(self.get_config() or {}), **(values or {})}

        unknown = unknown_config_keys(merged)
        if unknown:
            logger.warning(
                "[ContextEngineer] these config keys reach nothing and are "
                "ignored: %s — check the spelling against CompactionConfig's "
                "fields or PLUGIN_LEVEL_KEYS",
                ", ".join(unknown),
            )

        self._config = merged

        # Compaction settings: one attribute per dataclass field, defaults from
        # the dataclass itself so there is no second copy of them anywhere.
        for f in fields(CompactionConfig):
            setattr(self, f.name, _coerce(merged[f.name], f.type, f.name)
                    if f.name in merged else f.default)

        # Plugin-level settings — the ones with no CompactionConfig field.
        # PLUGIN_LEVEL_KEYS must list exactly these, or unknown_config_keys()
        # would report a key that is in fact used.
        self._session_ttl_seconds = int(merged.get("session_ttl_seconds", 7200))
        self._max_tracked_sessions = int(merged.get("max_tracked_sessions", 100))
        # Off unless the operator's config turns it on. A background job that
        # deletes directories must never start because someone merely
        # instantiated the plugin — a test suite did exactly that against the
        # real data/context_engineer and swept 18,000 local session
        # directories before this default was 0.
        self._session_data_ttl_days = int(merged.get("session_data_ttl_days", 0))
        self.min_time_between = float(merged.get("min_time_between_compactions", 120.0))
        self.enable_semantic_search = bool(merged.get("enable_semantic_search", False))
        self.core_memory_max_tokens = int(merged.get("core_memory_max_tokens", 2000))
        self._storage_base = Path(merged.get("storage_path", "data/context_engineer"))

    def _cleanup_expired_sessions(self) -> None:
        """Remove expired session components based on TTL and max count."""
        current_time = time.time()
        
        # First: TTL-based cleanup. Never evict a session with an in-flight
        # compaction - closing its stores mid-operation is a use-after-close.
        expired = [
            sid for sid, components in self._session_components.items()
            if sid not in self._active_compactions
            and (current_time - components.get("last_accessed", 0)) > self._session_ttl_seconds
        ]
        for sid in expired:
            self.cleanup_session(sid)

        # Second: LRU eviction if still over limit (also skips active sessions)
        if len(self._session_components) > self._max_tracked_sessions:
            # Sort by last_accessed, evict oldest non-active sessions
            sorted_sessions = sorted(
                (kv for kv in self._session_components.items() if kv[0] not in self._active_compactions),
                key=lambda x: x[1].get("last_accessed", 0)
            )
            evict_count = len(self._session_components) - self._max_tracked_sessions
            for sid, _ in sorted_sessions[:evict_count]:
                self.cleanup_session(sid)
        
        # Also cleanup _last_compaction_time
        stale_compaction = [
            sid for sid, ts in self._last_compaction_time.items()
            if (current_time - ts) > self._session_ttl_seconds
        ]
        for sid in stale_compaction:
            del self._last_compaction_time[sid]

    # ------------------------------------------------------------------
    # Session data TTL
    #
    # Nothing ever deleted a session directory: 1833 of them on the writer
    # host since June, one per (mostly short-lived) writer sub-agent run. The
    # sweep removes directories idle for longer than session_data_ttl_days,
    # together with the session's vectors in the shared store — one criterion
    # for both, so neither can outlive the other. Rate-limited and on its own
    # thread: the first run after a deploy meets every legacy directory at
    # once, and rmtree does not belong on the event loop.
    # ------------------------------------------------------------------
    _SWEEP_INTERVAL_SECONDS = 3600.0

    def _maybe_start_sweep(self) -> None:
        if self._session_data_ttl_days <= 0:
            return
        key = str(self._storage_base.resolve())
        now = time.time()
        with _SWEEP_LOCK:
            # The interval check below already keeps sweeps apart — this one
            # only matters when a sweep outlasts the interval (a huge backlog
            # on a slow disk). Then it is the only thing preventing two
            # threads from rmtree-ing the same directories.
            if key in _SWEEP_RUNNING:
                return
            if now - _SWEEP_LAST_RUN.get(key, 0.0) < self._SWEEP_INTERVAL_SECONDS:
                return
            _SWEEP_LAST_RUN[key] = now
            _SWEEP_RUNNING.add(key)
        try:
            self._start_sweep_thread(key)
        except Exception as exc:  # noqa: BLE001 — whatever start() throws, the flag must clear
            # Thread exhaustion. The flag must not stick — it would silence
            # every later sweep in this process — and the session creation
            # that triggered this must not fail over housekeeping.
            with _SWEEP_LOCK:
                _SWEEP_RUNNING.discard(key)
            logger.error("session data sweep thread could not start (%s)", exc)

    def _start_sweep_thread(self, key: str) -> None:
        threading.Thread(
            target=self._run_sweep, args=(key,),
            name="context_engineer_sweep", daemon=True,
        ).start()

    def _run_sweep(self, key: str) -> None:
        try:
            self._sweep_stale_session_dirs()
        except Exception:  # noqa: BLE001 — a failed sweep must not leave the flag stuck
            logger.exception("session data sweep failed")
        finally:
            with _SWEEP_LOCK:
                _SWEEP_RUNNING.discard(key)

    def _is_session_live(self, session_id: str) -> bool:
        """Known to THIS instance as live. A second plugin instance on the same
        base is not consulted: for one of its sessions to be hit, it would
        have to be idle for longer than the TTL by file mtime and still
        registered there, which the 2 h eviction makes a corner case — and
        the outcome would be logged write failures for that session, not a
        crash."""
        return (
            session_id in self._session_components
            or session_id in self._active_compactions
            or session_id in self._creating
        )

    def _sweep_stale_session_dirs(self) -> int:
        """Delete session directories idle longer than the TTL; returns the count.

        Idle = the newest of the directory's own mtime and its marker files'
        mtimes lies before the cutoff. The directory mtime alone would not do:
        rewriting a file in place (core_memory.json) leaves it untouched, and
        it only moves for the databases because SQLite's default DELETE
        journal adds and removes a -journal entry per commit — switching the
        stores to WAL would freeze it. The file mtimes depend on neither.
        """
        ttl_days = self._session_data_ttl_days
        cutoff = time.time() - ttl_days * 86400
        base = self._storage_base
        if not base.is_dir():
            return 0
        started = time.monotonic()
        removed = 0
        scanned = 0
        for path in list(base.iterdir()):
            if not path.is_dir():
                continue
            leftover = path.name.endswith(_SWEEPING_SUFFIX)
            if leftover:
                # A previous run got as far as the rename. Its markers may be
                # gone already, so neither age nor allow-list apply: finish it.
                session_id = path.name[: -len(_SWEEPING_SUFFIX)]
                scanned += 1
            else:
                markers = [path / m for m in _SESSION_MARKERS if (path / m).exists()]
                if not markers:
                    continue
                scanned += 1
                try:
                    newest = max(p.stat().st_mtime for p in [path, *markers])
                except OSError:
                    continue
                if newest >= cutoff:
                    continue
                session_id = path.name
                # Checked right here and again right before the rename, not
                # when the listing was taken: the session may come alive at
                # any point in between.
                if self._is_session_live(session_id):
                    continue
                # Vectors first. Were the directory removed first and this
                # failed, no later sweep would ever see the session_id again
                # and its vectors would stay forever. This way a failure
                # leaves the directory in place and the next run retries
                # both. A leftover never gets here: its vectors went in the
                # run that renamed it — touching them again would hit the
                # fresh vectors of a session that has since come back.
                if not self._forget_session_vectors(session_id):
                    continue
            try:
                if not leftover:
                    if self._is_session_live(session_id):
                        continue
                    target = path.with_name(path.name + _SWEEPING_SUFFIX)
                    path.rename(target)
                    path = target
                shutil.rmtree(path)
            except OSError as exc:
                # Windows refuses to rename or unlink an open SQLite file; one
                # busy directory must not end the whole sweep.
                logger.warning("sweep: %s not removed (%s)", path.name, exc.__class__.__name__)
                continue
            removed += 1
        # Always, even for 0: after the first deploy nothing is left to remove,
        # and "ran, nothing to do" must stay distinguishable from "never ran".
        logger.info(
            "session data sweep: removed %d of %d session directories idle for "
            "more than %d days (%.1fs)",
            removed, scanned, ttl_days, time.monotonic() - started,
        )
        return removed

    def _forget_session_vectors(self, session_id: str) -> bool:
        """Drop a session's vectors from the shared store; False if that failed.

        True without doing anything when this process holds no shared store:
        legacy directories kept their vectors inside themselves, and with
        semantic search switched off nothing reads _shared_vectors — whatever
        an earlier deploy left there is unused; delete that directory by hand
        if the space matters.
        """
        store = _shared_vector_store(self._storage_base, create=False)
        if store is None:
            return True
        try:
            store.delete(ARCHIVAL_COLLECTION, where={"session_id": session_id})
            return True
        except Exception as exc:  # noqa: BLE001 — reported; the directory stays for a retry
            logger.warning(
                "sweep: vectors of %s not removed (%s); directory kept for the next run",
                session_id, exc.__class__.__name__,
            )
            return False

    def _get_session_components(self, session_id: str,
                                overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """Get or create session-scoped components.

        Args:
            session_id: Session identifier
            overrides: Optional per-agent overrides for CompactionConfig fields.
                Sourced from ``context.hook_config`` in ``engineer_context``.
                Applied only on first creation of session components — caching
                means later calls with different overrides are ignored.

        Returns:
            Dict with tool_store, core_memory, archival_memory
        """
        if session_id in self._session_components:
            # Update last accessed time
            self._session_components[session_id]["last_accessed"] = time.time()
            return self._session_components[session_id]

        ov = overrides or {}
        if session_id not in self._session_components:
            # Reserve the name BEFORE the directory exists: the TTL sweep runs
            # on its own thread and skips every name in _creating. Without the
            # reservation there is a window between mkdir and the registration
            # at the end of this block in which the directory is on disk but
            # no set knows the session — the sweep's first run after a deploy,
            # with ~1800 legacy directories, is exactly when that window bites.
            self._creating.add(session_id)
            try:
                # Create session-specific storage paths
                session_path = self._storage_base / session_id
                session_path.mkdir(parents=True, exist_ok=True)
            
                # Initialize components
                # The session_id is load-bearing, not decoration: every write that
                # omits it falls back to "default", while list/search read with the
                # REAL id and find nothing. Measured before this: a compaction
                # stored a result, and list(section='tool_results') answered
                # "0 of 0" — the agent could not see its own catalogue, and the
                # system prompt telling it to look there was a dead instruction.
                # Setting it on the store fixes every call site at once.
                tool_store = ToolResultStore(session_path / "tool_results.db",
                                             session_id=session_id)
                core_memory = CoreMemory(storage_path=session_path / "core_memory.json",
                                         max_tokens=self.core_memory_max_tokens)
                shared_store = (_shared_vector_store(self._storage_base)
                                if self.enable_semantic_search else None)
                archival_memory = ArchivalMemory(
                    session_path / "archive.db",
                    session_id=session_id,
                    enable_semantic_search=self.enable_semantic_search,
                    # Used only when there is no shared store (non-chromadb
                    # backend): the session then keeps its own, as before.
                    vector_store_path=session_path / "vectors" if self.enable_semantic_search else None,
                    vector_store=shared_store,
                )
            
                # Initialize media store for inline media preservation
                media_store = None
                if self.store_media_before_compaction:
                    media_store = MediaStore(
                        storage_path=session_path / "media",
                        ttl_seconds=self.media_store_ttl_seconds,
                        max_files=self.media_store_max_files
                    )
            
                # Per-agent hook overrides relax fields like `tool_result_keep_last`
                # for media-heavy agents (cover_artist, repeated comfyui image
                # loads) without touching the plugin-wide default for everyone else.
                # By field name, so an override now works for EVERY field — the
                # hand-written list this replaces silently ignored an override for
                # any field its author had not thought to include, and three of
                # them (store_media_before_compaction, media_store_ttl_seconds,
                # media_store_max_files) were not even wired to `_o`.
                compaction_config = compaction_config_from({
                    **{f.name: getattr(self, f.name) for f in fields(CompactionConfig)},
                    **{k: v for k, v in ov.items()
                       if k not in PLUGIN_LEVEL_KEYS},
                })
            
                logger.info(
                    f"[ContextEngineer] Created CompactionConfig for session {session_id}: "
                    f"max_messages={compaction_config.max_messages}, "
                    f"always_compact_media_keep_last={compaction_config.always_compact_media_keep_last}"
                )
            
                # Create strategy
                strategy = LayeredCompactionStrategy(
                    tool_store=tool_store,
                    core_memory=core_memory,
                    archival_memory=archival_memory,
                    config=compaction_config,
                    media_store=media_store
                )
            
                self._session_components[session_id] = {
                    "tool_store": tool_store,
                    "core_memory": core_memory,
                    "archival_memory": archival_memory,
                    "media_store": media_store,
                    "strategy": strategy,
                    "last_accessed": time.time()
                }
            
                # Cleanup expired sessions periodically
                self._cleanup_expired_sessions()
                self._maybe_start_sweep()
            
                logger.debug(f"Created session components for {session_id}")
            finally:
                self._creating.discard(session_id)

        return self._session_components[session_id]
    
    async def engineer_context(self, context: HookContext) -> HookResult:
        """Apply context engineering to messages.
        
        Handler for 'engineer_context' hook defined in schema.yaml.
        
        Args:
            context: Hook context with messages and metadata
            
        Returns:
            HookResult with engineered messages
        """
        try:
            messages = context.messages or []
            
            if not messages:
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={"reason": "no_messages"}
                )
            
            session_id = context.session_id or "default"
            
            # Convert ChatMessage objects to dicts
            messages_as_dicts = []
            for msg in messages:
                if isinstance(msg, ChatMessage):
                    messages_as_dicts.append(msg.model_dump(exclude_none=True))
                else:
                    messages_as_dicts.append(msg)
            
            # Per-agent overrides via hooks.overrides[context_engineer.engineer_context]
            # in agent YAML — relax compaction thresholds for media-heavy agents
            # without changing the plugin-wide default. Applied at session-component
            # creation only (subsequent overrides ignored due to caching).
            hook_overrides = context.hook_config if context.hook_config else None

            # Get session components (needed for token/byte estimation and compaction)
            components = self._get_session_components(session_id, overrides=hook_overrides)
            strategy: LayeredCompactionStrategy = components["strategy"]
            
            # Get actual or estimated token usage (prefer actual from usage_tracker)
            current_tokens = self._get_actual_or_estimated_tokens(context, messages_as_dicts, strategy)
            
            # Estimate request bytes using strategy's method (for Gemini 100MB limit check)
            request_bytes = strategy._estimate_request_bytes(messages_as_dicts)
            bytes_exceeded = request_bytes > self.max_request_bytes
            
            if bytes_exceeded:
                logger.warning(
                    f"[ContextEngineer] Session {session_id}: Request size "
                    f"{request_bytes / (1024*1024):.1f}MB exceeds "
                    f"{self.max_request_bytes / (1024*1024):.0f}MB limit - forcing compaction"
                )
            
            # Check if compaction needed
            is_manual = context.metadata.get("manual_trigger", False) if context.metadata else False
            
            # Determine trigger event for media compaction BEFORE early return check
            # This is a pre_llm_call hook, so the last message is what the user just sent
            trigger_event = None
            if messages_as_dicts:
                last_msg = messages_as_dicts[-1]
                last_role = last_msg.get("role", "")
                if last_role == "user":
                    trigger_event = "user_message"
                # Note: final_response is detected in post-hooks, not here
            
            # Check if event-based media compaction should run even below threshold
            event_media_compaction_needed = False
            if trigger_event == "user_message" and self.compact_media_after_user_message:
                event_media_compaction_needed = True
                logger.debug(
                    f"[ContextEngineer] Session {session_id}: Event-based media compaction "
                    f"triggered by user_message (compact_media_after_user_message=true)"
                )
            
            # Check if always-compact-media is enabled (must run even below threshold)
            always_compact_media_enabled = self.always_compact_media_keep_last > 0
            
            # Skip only if: not manual, below token threshold, below byte limit, 
            # AND no event-based media compaction, AND always_compact_media disabled
            if not is_manual and current_tokens < self.layer1_threshold and not bytes_exceeded and not event_media_compaction_needed and not always_compact_media_enabled:
                logger.debug(
                    f"[ContextEngineer] Session {session_id}: "
                    f"{current_tokens} tokens < {self.layer1_threshold} threshold, "
                    f"{request_bytes / (1024*1024):.1f}MB < {self.max_request_bytes / (1024*1024):.0f}MB limit, "
                    f"no event-based media compaction needed, skipping"
                )
                
                # Below threshold - no compaction needed, return unchanged
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={
                        "reason": "below_threshold",
                        "current_tokens": current_tokens,
                        "threshold": self.layer1_threshold,
                        "request_bytes": request_bytes,
                        "max_request_bytes": self.max_request_bytes
                    }
                )
            
            # Rate limiting - but NOT if bytes exceeded (must compact to avoid API errors!)
            # Also NOT if event-based media compaction or always_compact_media is needed
            current_time = time.monotonic()
            last_compaction = self._last_compaction_time.get(session_id)
            
            if not is_manual and not bytes_exceeded and not event_media_compaction_needed and not always_compact_media_enabled and last_compaction:
                time_since = current_time - last_compaction
                if time_since < self.min_time_between:
                    logger.info(
                        f"[ContextEngineer] Session {session_id}: Rate limited - "
                        f"{time_since:.1f}s since last compaction "
                        f"(min: {self.min_time_between}s)"
                    )
                    return HookResult(
                        success=True,
                        modified=False,
                        context=context,
                        metadata={
                            "reason": "rate_limited",
                            "time_since_last": time_since,
                            "min_time_between": self.min_time_between
                        }
                    )
            
            # Force compaction if manually triggered, byte limit exceeded, event-based media compaction needed,
            # OR always_compact_media enabled
            # Byte limit MUST be enforced to avoid API errors (Gemini 100MB limit)
            force = is_manual or bytes_exceeded or event_media_compaction_needed or always_compact_media_enabled
            
            # Apply compaction. Run it before emitting any status so we can
            # decide afterwards whether anything actually changed - this hook
            # fires on every LLM call (always_compact_media), and emitting
            # START/END for no-op runs would flood the CLI with noise.
            #
            # Mark the session active for the duration so a concurrent
            # _get_session_components (for another session) can't evict and
            # close this session's stores while the compaction is mid-flight
            # on a worker thread (use-after-close).
            import asyncio
            self._active_compactions.add(session_id)
            try:
                result = await strategy.compact(
                    messages_as_dicts,
                    current_tokens,
                    force=force,
                    trigger_event=trigger_event,
                    session_id=session_id
                )
            finally:
                self._active_compactions.discard(session_id)

            # Update rate limit tracker
            self._last_compaction_time[session_id] = current_time

            # Determine whether anything was actually compacted. Gates both the
            # START/END status messages and the web UI history entry.
            something_compacted = (
                result.tokens_saved > 0 or
                result.tool_results_stored > 0 or
                result.messages_archived > 0 or
                result.messages_dropped > 0 or
                result.messages_pruned > 0 or  # Pre-Layer P (message count limit)
                result.media_deduplicated > 0 or
                result.media_compacted_after_event > 0 or
                result.media_always_compacted > 0  # Always-compact media (Pre-Layer M)
            )

            # Emit START/END status only when the run actually changed the
            # context - suppress the noise for no-op runs.
            if something_compacted:
                async with StatusScope(
                    status_bus,
                    "context_engineer",
                    session_id,
                    start_msg=f"Engineering context: {current_tokens} tokens (target: {self.target_tokens}){' [MANUAL]' if is_manual else ''}",
                ) as scope:
                    await asyncio.sleep(0.01)  # Allow START message to be delivered
                    # The end line is what survives in the WebUI (it replaces
                    # the start line), so it must carry the result -- a bare
                    # 'completed' says nothing. The something_compacted gate
                    # guarantees at least one part below is non-zero.
                    parts = []
                    if result.tokens_saved > 0:
                        parts.append(f"saved {result.tokens_saved} tokens "
                                     f"({result.reduction_percent:.1f}%)")
                    if result.tool_results_stored:
                        parts.append(f"{result.tool_results_stored} tool result(s) stored")
                    if result.messages_archived:
                        parts.append(f"{result.messages_archived} message(s) archived")
                    if result.messages_dropped:
                        parts.append(f"{result.messages_dropped} message(s) dropped")
                    if result.messages_pruned:
                        parts.append(f"{result.messages_pruned} message(s) pruned")
                    media_total = (result.media_deduplicated
                                   + result.media_compacted_after_event
                                   + result.media_always_compacted)
                    if media_total:
                        parts.append(f"{media_total} media item(s) compacted")
                    await scope.end("Context engineered: " + ", ".join(parts))
            
            # Invalidate usage tracker data for this session if something was compacted
            # This prevents subsequent hooks (e.g., context_summarizer) from using stale
            # token counts that don't reflect the optimized message list
            if something_compacted:
                self._invalidate_usage_tracker_session(context, session_id, "context_engineer_compaction")
            
            if self.stats_history is not None and something_compacted:
                self.stats_history.append({
                    "timestamp": time.time(),
                    "session_id": session_id,
                    "agent_name": context.agent_name or "unknown",
                    "original_tokens": result.original_tokens,
                    "final_tokens": result.final_tokens,
                    "tokens_saved": result.tokens_saved,
                    "reduction_percent": result.reduction_percent,
                    "layers_applied": result.layers_applied,
                    "tool_results_stored": result.tool_results_stored,
                    "messages_archived": result.messages_archived,
                    "messages_dropped": result.messages_dropped,
                    "messages_pruned": result.messages_pruned,  # Pre-Layer P
                    "media_deduplicated": result.media_deduplicated,
                    "media_compacted_after_event": result.media_compacted_after_event,
                    "media_bytes_saved": result.media_bytes_saved
                })
                
                # Save history to disk (support both sync and async callbacks)
                if self.history_callback is not None:
                    if asyncio.iscoroutinefunction(self.history_callback):
                        await self.history_callback()
                    else:
                        # Run sync callback in thread pool to avoid blocking
                        await asyncio.to_thread(self.history_callback)
            
            # Convert modified messages to ChatMessage objects
            modified_messages = result.modified_messages
            new_messages = [
                ChatMessage(**msg) if isinstance(msg, dict) else msg
                for msg in modified_messages
            ]
            
            # Inject restoration context (info about how to retrieve stored data)
            strategy: LayeredCompactionStrategy = components["strategy"]
            restoration_context = await strategy.get_restoration_context()
            
            # Vorherige Injektion ENTFERNEN, bevor neu eingefuegt wird
            # (Konvention wie debate_forum: ueber `injected_by` markiert).
            # Ohne das wuchs der Block mit: die kompaktierten Messages werden
            # persistiert, also ist die Injektion des letzten Turns beim
            # naechsten schon Teil der Historie -- und weil sie selbst eine
            # system-Message ist, wandert die Einfuegestelle jedes Mal eins
            # weiter. Gemessen an einem Sub-Agenten mit 109 Aufrufen:
            # Request 21 = 15 Kopien, Request 61 = 55, Request 109 = 103
            # Kopien in 201 Messages. Folge: halber Kontext war Duplikat, und
            # die verschobene Einfuegestelle brach den Prompt-Cache in 103 von
            # 108 Turns (Cache-Quote 8-13 % statt 50-65 %).
            # Der Inhalts-Treffer ist NICHT redundant: Sessions, die vor
            # diesem Fix liefen, tragen unmarkierte Kopien in ihrer
            # persistierten Historie -- ohne ihn blieben die dort stehen.
            for i in range(len(new_messages) - 1, -1, -1):
                msg = new_messages[i]
                if getattr(msg, "injected_by", None) == _RESTORATION_MARKER:
                    new_messages.pop(i)
                    continue
                content = getattr(msg, "content", None)
                if isinstance(content, str) and _RESTORATION_HEADER in content:
                    new_messages.pop(i)

            if restoration_context:
                # Find position after last system message to insert restoration context
                # This preserves the agent's system prompt while adding our context
                insert_pos = 0
                for i, msg in enumerate(new_messages):
                    msg_role = msg.role if hasattr(msg, 'role') else msg.get('role')
                    if msg_role == 'system':
                        insert_pos = i + 1
                    else:
                        break  # Stop at first non-system message

                restoration_msg = ChatMessage(
                    role="system",
                    content=restoration_context,
                    injected_by=_RESTORATION_MARKER,
                )
                new_messages.insert(insert_pos, restoration_msg)
            
            # Build modified context with all fields
            modified_context = HookContext(
                hook_type=context.hook_type,
                request_id=context.request_id,
                session_id=context.session_id,
                agent=context.agent,
                agent_name=context.agent_name,
                messages=new_messages,
                llm_response=context.llm_response,
                tool_call=context.tool_call,
                tool_result=context.tool_result,
                output=context.output,
                metadata=context.metadata,
                step=context.step,
                llm=context.llm,
                cancellation_token=context.cancellation_token
            )
            
            logger.info(
                f"[ContextEngineer] Session {session_id}: "
                f"{result.original_tokens} -> {result.final_tokens} tokens "
                f"({result.reduction_percent:.1f}% reduction), "
                f"layers: {result.layers_applied}"
                + (f", media_dedup: {result.media_deduplicated}" if result.media_deduplicated > 0 else "")
                + (f", media_event: {result.media_compacted_after_event}" if result.media_compacted_after_event > 0 else "")
            )
            
            # NOTE: Session persistence is now handled automatically by HookIntegrationManager
            # when we return HookResult with modified=True. The _auto_sync_session_messages()
            # method filters system messages correctly (keeping archived_ref types).
            # The explicit set_compacted_messages() call below is kept for backwards compatibility
            # and as a safety net, but is no longer strictly required.
            if context.agent and hasattr(context.agent, '_session_tracker'):
                # Filter out the ORIGINAL system message (agent's system prompt)
                # for persistence — it is rebuilt each turn from config. The
                # system messages that ARE compacted conversation must stay.
                #
                # This used to be a hand-written copy of that rule which knew
                # only about archived_ref, so it dropped the prune breadcrumb.
                # It survived by accident: the agent's auto-sync runs afterwards
                # and overwrote the same slot with the correct list. Ordering is
                # not a guarantee — a hook that mutates context.messages in
                # place skips that overwrite and this copy wins.
                from agent_system.servers.agent.components.hook_integration import (
                    is_compaction_system_message,
                )
                conversation_msgs = [
                    msg for msg in new_messages
                    if msg.role != 'system' or is_compaction_system_message(msg)
                ]


                context.agent._session_tracker.set_compacted_messages(
                    session_id, conversation_msgs
                )
                logger.debug(
                    f"[ContextEngineer] Persisted {len(conversation_msgs)} compacted messages "
                    f"for session {session_id}"
                )
            
            # CRITICAL: modified=True signals hook registry to use our modified context
            return HookResult(
                success=True,
                modified=True,  # Always True when we made changes
                context=modified_context,
                metadata={
                    "original_tokens": result.original_tokens,
                    "final_tokens": result.final_tokens,
                    "tokens_saved": result.tokens_saved,
                    "reduction_percent": result.reduction_percent,
                    "layers_applied": result.layers_applied,
                    "tool_results_stored": result.tool_results_stored,
                    "messages_archived": result.messages_archived,
                    "messages_dropped": result.messages_dropped,
                    "messages_pruned": result.messages_pruned,
                    "media_deduplicated": result.media_deduplicated,
                    "media_compacted_after_event": result.media_compacted_after_event
                }
            )
            
        except Exception as e:
            logger.exception(f"[ContextEngineer] Error during context engineering: {e}")
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error=str(e)
            )
    
    def _get_actual_or_estimated_tokens(
        self, 
        context: HookContext, 
        messages: list[dict[str, Any]],
        strategy: LayeredCompactionStrategy
    ) -> int:
        """Get actual token count from last LLM response or estimate from messages.

        Uses the MAXIMUM of:
        1. Actual prompt_tokens from last LLM response (via context_usage_tracker)
        2. Estimated tokens from current messages (via strategy)

        This ensures we trigger compaction if either metric exceeds threshold,
        preventing context overflow.

        Args:
            context: Hook context with session_id
            messages: Current message list
            strategy: Compaction strategy for token estimation

        Returns:
            Maximum of actual or estimated token count
        """
        estimated_tokens = strategy._estimate_messages_tokens(messages)

        # Include tool definition tokens in estimation (they consume context window)
        if context.agent and hasattr(context.agent, '_current_tools_schema'):
            tools_schema = context.agent._current_tools_schema
            if tools_schema and isinstance(tools_schema, list):
                from agent_system.llm.token_utils import estimate_tools_token_count
                tool_tokens = estimate_tools_token_count(tools_schema)
                estimated_tokens += tool_tokens
                logger.debug(
                    f"[ContextEngineer] Added {tool_tokens} tool definition tokens "
                    f"({len(tools_schema)} tools)"
                )

        actual_tokens = 0

        # Try to get actual tokens from context_usage_tracker's latest snapshot FOR THIS SESSION
        # This uses the previous LLM call's token count as baseline - if it was already high,
        # the next call will be at least as large (probably larger with new messages)
        try:
            # Access the plugin registry via agent's system_config
            if context.agent and hasattr(context.agent, 'system_config'):
                system_config = context.agent.system_config
                if hasattr(system_config, 'mcp_registry') and system_config.mcp_registry:
                    registry = system_config.mcp_registry

                    # Get context_usage_tracker plugin
                    usage_tracker_plugin = registry.get_server('context_usage_tracker')
                    if usage_tracker_plugin and hasattr(usage_tracker_plugin, 'tracker'):
                        tracker = usage_tracker_plugin.tracker

                        # Get latest snapshot FOR THIS SESSION (not global _latest_snapshot!)
                        # This filters by session_id to avoid interference from sub-agents
                        latest = tracker.get_latest(session_id=context.session_id)
                        if latest:
                            # Check if data is stale (context was optimized since last LLM call)
                            # Stale data doesn't reflect current message list, so ignore it
                            if latest.get('is_stale'):
                                logger.debug(
                                    f"[ContextEngineer] Ignoring stale usage_tracker data for session "
                                    f"{context.session_id} (context was already optimized)"
                                )
                            else:
                                actual_tokens = latest.get('prompt_tokens', 0)
                                logger.debug(
                                    f"[ContextEngineer] Got actual tokens from usage_tracker: {actual_tokens} "
                                    f"(estimated: {estimated_tokens})"
                                )
        except Exception as e:
            logger.debug(f"[ContextEngineer] Could not get actual tokens from usage_tracker: {e}")

        # Return the MAXIMUM to ensure we trigger on either metric
        max_tokens = max(actual_tokens, estimated_tokens)

        if actual_tokens > 0 and estimated_tokens > 0:
            logger.debug(
                f"[ContextEngineer] Session {context.session_id}: Using max tokens - "
                f"actual={actual_tokens}, estimated={estimated_tokens}, using={max_tokens}"
            )

        return max_tokens
    
    def _invalidate_usage_tracker_session(
        self, 
        context: HookContext, 
        session_id: str, 
        reason: str
    ) -> None:
        """Mark usage tracker data as stale after context optimization.
        
        This prevents subsequent hooks from using outdated token counts
        that don't reflect the optimized message list.
        
        Args:
            context: Hook context with agent reference
            session_id: Session to invalidate
            reason: Reason for invalidation (for logging)
        """
        try:
            if context.agent and hasattr(context.agent, 'system_config'):
                system_config = context.agent.system_config
                if hasattr(system_config, 'mcp_registry') and system_config.mcp_registry:
                    registry = system_config.mcp_registry
                    usage_tracker_plugin = registry.get_server('context_usage_tracker')
                    if usage_tracker_plugin and hasattr(usage_tracker_plugin, 'tracker'):
                        usage_tracker_plugin.tracker.invalidate_session(session_id, reason)
        except Exception as e:
            logger.debug(f"[ContextEngineer] Could not invalidate usage_tracker session: {e}")
    
    # === MCP Tool Handlers ===
    # These are called by the MCP server when tools are invoked

    async def _handle_store_fact(
        self,
        fact: str,
        category: str = "facts",
        importance: float = 0.5,
        session_id: str = "default"
    ) -> dict[str, Any]:
        """Handle store_fact tool - add to core memory.
        
        Args:
            fact: The fact to store
            category: Category for organization
            importance: Importance score
            session_id: Session ID
            
        Returns:
            Result with fact ID
        """
        components = self._get_session_components(session_id)
        core_memory: CoreMemory = components["core_memory"]
        
        fact_id = await core_memory.add_fact(fact, category=category, importance=importance)
        
        return {
            "success": True,
            "fact_id": fact_id,
            "category": category,
            "importance": importance,
            "total_facts": len(core_memory.facts)
        }
    
    @staticmethod
    def _is_within(path: "Path", root: "Path") -> bool:
        """True if `path` (already resolved) is inside `root` (already resolved)."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    async def _handle_restore_multimodal(
        self,
        path: str,
        session_id: str = "default"
    ) -> dict[str, Any]:
        """Handle restore_multimodal tool - reload compacted audio/image/video.
        
        This tool allows the LLM to restore previously compacted multimodal
        content back into the conversation. The file will be marked for
        re-injection on the next LLM call.
        
        Args:
            path: Full file path of the multimodal content to restore
            session_id: Session ID
            
        Returns:
            Status and file info, or error if file not found
        """
        from pathlib import Path

        file_path = Path(path)

        # SECURITY: `path` comes straight from LLM tool args
        # (read(ref="/path/to/file.wav")). Without containment this is an
        # arbitrary file read primitive: the file is base64-injected into the
        # model context. Restrict
        # to the project data roots where all plugin media legitimately lives
        # (context_engineer media store, comfyui outputs, audio, covers - all
        # under data/). Reject anything outside, including symlink escapes.
        allowed_roots = []
        for root in (self._storage_base, Path("data")):
            try:
                allowed_roots.append(root.resolve())
            except Exception:
                pass
        try:
            resolved = file_path.resolve()
        except Exception:
            resolved = file_path
        if not any(self._is_within(resolved, r) for r in allowed_roots):
            logger.warning("restore_multimodal rejected out-of-root path: %r", path)
            return {
                "status": "error",
                "error": "Path is outside the allowed media directories.",
            }

        # Validate file exists
        if not file_path.exists():
            return {
                "status": "error",
                "error": f"File not found: {path}",
                "hint": "The file may have been moved, deleted, or the path is incorrect."
            }
        
        # Get file info
        stat = file_path.stat()
        size_bytes = stat.st_size
        size_mb = size_bytes / (1024 * 1024)
        
        # Estimate token cost
        # Base64 encoding adds ~33% overhead, then ~4 chars per token
        estimated_tokens = int(size_bytes * 0.33)
        
        # Determine type from extension
        suffix = file_path.suffix.lower()
        if suffix in ('.wav', '.mp3', '.ogg', '.flac', '.m4a', '.aac'):
            content_type = "audio"
            mime_type = {
                '.wav': 'audio/wav',
                '.mp3': 'audio/mpeg',
                '.ogg': 'audio/ogg',
                '.flac': 'audio/flac',
                '.m4a': 'audio/mp4',
                '.aac': 'audio/aac'
            }.get(suffix, 'audio/wav')
        elif suffix in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'):
            content_type = "image"
            mime_type = {
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.gif': 'image/gif',
                '.webp': 'image/webp',
                '.bmp': 'image/bmp'
            }.get(suffix, 'image/png')
        elif suffix in ('.mp4', '.webm', '.avi', '.mov'):
            content_type = "video"
            mime_type = {
                '.mp4': 'video/mp4',
                '.webm': 'video/webm',
                '.avi': 'video/avi',
                '.mov': 'video/quicktime'
            }.get(suffix, 'video/mp4')
        else:
            return {
                "status": "error",
                "error": f"Unsupported file type: {suffix}",
                "hint": "Supported types: audio (wav, mp3, ogg, flac, m4a, aac), image (png, jpg, gif, webp, bmp), video (mp4, webm, avi, mov)"
            }
        
        # Return multimodal content for injection
        # The _multimodal_content key will be picked up by tool execution
        return {
            "status": "success",
            "message": f"File will be loaded in the next response. Estimated cost: ~{estimated_tokens:,} tokens",
            "file_info": {
                "path": str(file_path),
                "name": file_path.name,
                "type": content_type,
                "mime_type": mime_type,
                "size_mb": round(size_mb, 2),
                "estimated_tokens": estimated_tokens
            },
            "_multimodal_content": [{
                "type": content_type,
                "path": str(file_path),
                "mime_type": mime_type,
                "description": f"Restored {content_type}: {file_path.name}"
            }]
        }
    
    async def _handle_stats(
        self,
        session_id: str = "default"
    ) -> dict[str, Any]:
        """Context engineering statistics. NOT a tool — the stats tool was
        removed; this serves the web panel via web_endpoints.py.
        
        Args:
            session_id: Session ID
            
        Returns:
            Statistics about stored data
        """
        components = self._get_session_components(session_id)
        
        tool_store: ToolResultStore = components["tool_store"]
        core_memory: CoreMemory = components["core_memory"]
        archival: ArchivalMemory = components["archival_memory"]
        
        # Aggregate media compaction stats from history for this session
        media_deduplicated = 0
        media_compacted = 0
        if self.stats_history:
            for event in self.stats_history:
                if event.get("session_id") == session_id:
                    media_deduplicated += event.get("media_deduplicated", 0)
                    media_compacted += event.get("media_compacted_after_event", 0)
        
        return {
            "tool_results": tool_store.get_stats(),
            "core_memory": {
                "facts": len(core_memory.facts),
                "facts_count": len(core_memory.facts),  # Added for UI compatibility
                "categories": core_memory.get_categories(),
                "token_usage": core_memory.get_token_usage()
            },
            "archival_memory": archival.get_stats(),
            "media_deduplicated": media_deduplicated,
            "media_compacted": media_compacted
        }
    
    def cleanup_session(self, session_id: str) -> None:
        """Clean up session resources.
        
        Args:
            session_id: Session to clean up
        """
        if session_id in self._session_components:
            components = self._session_components[session_id]
            
            # Close database connections
            if "archival_memory" in components:
                components["archival_memory"].close()
            if "tool_store" in components:
                components["tool_store"].close()
            
            del self._session_components[session_id]
            
            logger.debug(f"Cleaned up session components for {session_id}")

    # ------------------------------------------------------------------
    # list / read — the browsing surface over stored context
    #
    # Replaces the single `recall` tool, which took a free-text query and
    # guessed from its shape which of five stores was meant. The guess was
    # documented as misfiring (agents writing "$TR_…" landed in the wrong
    # handler), and the stores answered in four different shapes, so no stable
    # expectation could form. These three verbs are the ones every model is
    # already fluent in: what is there, where is it, give me a piece of it.
    # ------------------------------------------------------------------

    async def _handle_context_list(
        self,
        section: str | None = None,
        offset: int | None = None,
        limit: int = 20,
        role: str | None = None,
        filter: str | None = None,
        session_id: str = "default",
    ) -> dict[str, Any]:
        """What is stored: addresses and one-line summaries, never bodies.

        With ``filter`` the same rows come back narrowed to what matches, each
        carrying the matching excerpt. Browsing and finding are one verb because
        they answer one question and hand back one shape — the caller either has
        words to go on or does not.

        The two differ in one respect, which is why the section default depends
        on it: browsing pages through ONE store in a defined order, so it
        defaults to the conversation; filtering has no natural order across
        stores and searches all of them. Both are overridable, and the reply
        echoes which section it used.

        Offsets read like a file, and all sections are chronological:
        no offset → the LAST page (what just left the view, which is what an
        agent asks about); a negative offset counts back from the end; a
        non-negative one is absolute. Defaulting to the oldest page meant the
        recent entries — the ones the conversation was actually about — could
        only be reached by knowing the total and doing the arithmetic.
        """
        limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        needle = (filter or "").strip()
        section = section or ("all" if needle else "history")
        components = self._get_session_components(session_id)

        if section not in CONTEXT_SECTIONS and section != "all":
            return {
                "status": "error",
                "error": f"unknown section '{section}'",
                "sections": ["all", *CONTEXT_SECTIONS],
            }
        if section == "all" and not needle:
            return {
                "status": "error",
                "error": "section='all' needs a filter",
                "hint": ("browsing pages through one store — pick a section "
                         f"({', '.join(CONTEXT_SECTIONS)}), or pass a filter"),
            }

        wanted = CONTEXT_SECTIONS if section == "all" else (section,)
        entries: list[dict[str, Any]] = []
        total = 0
        # Only meaningful while browsing; a filter ranks rather than orders, so
        # it has no page to be at. Resolved per section against that section's
        # count — safe because browsing is always exactly one section.
        start = max(0, int(offset)) if offset is not None else 0

        if "history" in wanted:
            archival: ArchivalMemory = components["archival_memory"]
            if needle:
                # Off the loop: search takes the vector store's lock, and that
                # store is shared by every session — a batch embedding elsewhere
                # can hold it for seconds. Awaited inline, that would freeze
                # the whole API for the duration, not just this call.
                found = await asyncio.to_thread(
                    archival.search, needle, session_id=session_id, limit=limit,
                )
                if role:
                    # The index cannot filter by role, so do it here. Accepting
                    # the parameter and ignoring it would be worse than not
                    # offering it: the caller believes it narrowed the result.
                    found = [m for m in found if m.role == role]
            else:
                total += archival.count_session_messages(session_id, role=role)
                start = _page_start(offset, total, limit)
                found = archival.get_session_messages(
                    session_id=session_id, limit=limit, role=role, offset=start)
            for m in found:
                row = {
                    "ref": m.id, "kind": "message", "role": m.role,
                    "tool": m.tool_name, "tokens": m.token_count,
                    "at": m.timestamp.isoformat(),
                    "summary": _one_line(m.summary or "", 200),
                }
                if needle:
                    row["match"] = _one_line(_around(m.content, needle, 200), 460)
                entries.append(row)

        if "tool_results" in wanted:
            tool_store: ToolResultStore = components["tool_store"]
            if needle:
                rows = tool_store.search_entries(needle, session_id=session_id, limit=limit)
            else:
                total += tool_store.count_entries(session_id)
                start = _page_start(offset, total, limit)
                rows = tool_store.list_entries(session_id, offset=start, limit=limit)
            for r in rows:
                row = {
                    "ref": r["ref"], "kind": "tool_result", "tool": r["tool"],
                    "tokens": r["tokens"], "at": r["at"], "chars": r["chars"],
                }
                if needle:
                    row["match"] = _one_line(r["match"], 460)
                else:
                    row["summary"] = _one_line(r.get("summary") or "", 200)
                entries.append(row)

        if "facts" in wanted:
            core_memory: CoreMemory = components["core_memory"]
            facts = list(core_memory.facts)
            if needle:
                facts = [f for f in facts
                         if needle.lower() in getattr(f, "content", "").lower()][:limit]
            else:
                total += len(facts)
                start = _page_start(offset, total, limit)
                facts = facts[start:start + limit]
            for f in facts:
                row = {
                    "ref": None, "kind": "fact",
                    "category": getattr(f, "category", None),
                    "importance": getattr(f, "importance", None),
                    "summary": _one_line(getattr(f, "content", ""), 400),
                }
                if needle:
                    row["match"] = _one_line(getattr(f, "content", ""), 460)
                entries.append(row)

        out: dict[str, Any] = {
            "status": "success",
            "section": section,
            "count": len(entries),
            "entries": entries,
        }
        if needle:
            # Filtered results are ranked, not ordered, so there is no stable
            # "next page" to hand out — say so rather than imply one exists.
            out["filter"] = needle
            out["hint"] = (
                "nothing matched — drop the filter to see what is stored"
                if not entries else
                "the match is often the whole answer; read a ref only if you "
                "need more of that item (find= gets just its matching parts)")
        else:
            shown = start + len(entries)
            out["total"] = total
            out["offset"] = start
            # Both directions, because the default page is now the LAST one:
            # with only next_offset a caller landing on the tail cannot tell
            # that anything came before it.
            out["has_more_before"] = start > 0
            out["has_more_after"] = shown < total
            out["next_offset"] = shown if shown < total else None
        return out

    async def _handle_context_read(
        self,
        ref: str,
        session_id: str = "default",
        offset: int = 0,
        limit: int | None = None,
        find: str | None = None,
    ) -> dict[str, Any]:
        """Read ONE stored item by address, always bounded.

        The address decides the store — a declared reference, not a guess from
        free text. An unrecognised ref is an error naming the valid shapes,
        never a silent fallback into a keyword search.
        """
        ref = str(ref or "").strip()
        if not ref:
            return {"status": "error", "error": "ref is required",
                    "hint": "get a ref from the list tool, or from a placeholder in this conversation"}

        kind = _ref_kind(ref)
        if kind is None:
            return {
                "status": "error",
                "error": f"'{ref}' is not a known reference",
                "hint": ("refs look like arch_… (archived message), TR_… (tool "
                         "result or attached file) or a media file path; "
                         "list returns valid refs"),
            }

        components = self._get_session_components(session_id)

        if kind == "message":
            archival: ArchivalMemory = components["archival_memory"]
            entry, hops = _resolve_archive_chain(archival, ref)
            if entry is None:
                return {"status": "error", "ref": ref, "kind": kind,
                        "error": f"no archived message '{ref}'",
                        "hint": "list(section='history') shows valid refs"}
            base = {
                "status": "success", "ref": ref, "kind": "message",
                "role": entry.role, "tool": entry.tool_name,
                "tokens": entry.token_count, "at": entry.timestamp.isoformat(),
                "summary": _one_line(entry.summary or "", 200),
            }
            if hops:
                # Say it rather than quietly hand back different content than
                # the ref names — the chain is a defect being worked around.
                base["resolved_ref"] = entry.id
                base["resolved_through"] = hops
            if find:
                return {**base, **find_in_text(entry.content, find)}
            return {**base, **slice_text(entry.content, offset=offset, limit=limit)}

        if kind == "media":
            result = await self._handle_restore_multimodal(path=ref, session_id=session_id)
            return {"status": "success", "ref": ref, "kind": "media", **result}

        # Tool results (and the attached files that share this store) go through
        # the same two paging helpers as the archive above, so every read
        # answers in one shape. There used to be a second, private
        # implementation here with four modes, two of which nothing called.
        tool_store: ToolResultStore = components["tool_store"]
        bare = ref.lstrip("$")
        entry = tool_store.retrieve(bare) or tool_store.retrieve_by_hash(bare)
        if entry is None:
            return {"status": "error", "ref": ref, "kind": kind,
                    "error": f"no stored tool result '{ref}'",
                    "hint": "list(section='tool_results') shows valid refs"}

        base = {
            "status": "success", "ref": ref, "kind": kind,
            "tool": entry.tool_name, "tokens": entry.token_count,
            "at": entry.timestamp.isoformat(),
            "summary": _one_line(entry.summary or "", 200),
        }
        if find:
            return {**base, **find_in_text(entry.content, find)}
        return {**base, **slice_text(entry.content, offset=offset, limit=limit)}
