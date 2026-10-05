"""Context Engineer Plugin - Hook implementations.

This module implements the main hook for context engineering, which applies
layered compaction strategies to optimize context usage.
"""

from __future__ import annotations

import asyncio
import inspect
import hashlib
import json
import logging
import re
import shutil
import threading
import time
from collections import OrderedDict
from dataclasses import MISSING, dataclass, fields
from pathlib import Path
from typing import Any

from agent_system.hooks import HookContext, HookResult, SchemaBasedPluginHook
from agent_system.llm.message_roles import DEVELOPER, is_injected_note
from agent_system.llm.models import ChatMessage
from agent_system.paths import data_path, resolve_data_path
from agent_system.tools.status import StatusScope, status_bus

from .archival_memory import ARCHIVAL_COLLECTION, ArchivalMemory
from .compaction import (
    PLUGIN_LEVEL_KEYS,
    TOOL_RESULTS_SECTION,
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


@dataclass
class _HysteresisMark:
    """Where the last run of a rewriting layer left a session.

    ``tokens`` is the base growth is measured from. It stays None until the
    next call measures the compacted context, and a smaller reading lowers it.
    Taking it from the run itself mixed two scales: the hook measures the
    provider's prompt tokens where it can, the run reports original minus
    ESTIMATED savings — off by 30 % of the context on a provider that counts
    30 % more, enough to release the hold on the very next call. And a base
    that never came down (the context shrank through context_summarizer, or an
    inline image the estimate counts at 250k tokens was evicted) held every
    layer until the context was back above the old base.

    ``level`` is the deepest layer that was due when that run started
    (_due_level). A deeper one becoming due is new work, not a repeat, and
    passes the hold.

    ``stale_readings`` counts readings that could not set the base because the
    provider count was stale. A stale reading happens at most once per LLM call
    — each call records fresh usage — so a second one in a row means nothing is
    recorded any more, and the estimate becomes the base: without one, growth
    counts as zero and a level-3 mark would hold every layer for good. Such a
    base is ``provisional``: it is on the estimate's scale, so the next whole
    reading replaces it instead of being measured against it — a provider that
    counts 30 % more would otherwise release the hold with no growth at all.
    """

    level: int
    tokens: int | None = None
    stale_readings: int = 0
    provisional: bool = False


@dataclass
class _ShownBlock:
    """The restoration block a session's prompt carries, and what followed it.

    ``conversation_length`` and ``head`` describe the conversation this hook
    handed on last time (_conversation_probe). An incoming conversation that is
    shorter, or starts differently, was rewritten after this hook — by
    context_summarizer, typically.
    """

    text: str
    conversation_length: int
    head: bytes


#: Marks and shown blocks kept per plugin instance, oldest dropped first. Not
#: tied to the session components: those are evicted after two idle hours or
#: beyond max_tracked_sessions, and a coordinator waiting on a large fan-out
#: lost its mark that way and compacted again on its next call.
_MAX_HYSTERESIS_MARKS = 10_000

# Compaction events kept for the panel, in memory and in the history file.
HISTORY_LIMIT = 1000


def _conversation_probe(messages: list[dict[str, Any]]) -> tuple[int, bytes]:
    """Length of the non-system part, and a digest of its first message.

    A digest, not the text: the first message of an upload session carries the
    image as base64, and a verbatim copy of it stayed in _shown_blocks for up to
    _MAX_HYSTERESIS_MARKS sessions.
    """
    conversation = [m for m in messages if m.get("role") != "system"]
    if not conversation:
        return 0, b""
    first = conversation[0]
    head = f"{first.get('role')}:{first.get('content')}".encode("utf-8", "replace")
    return len(conversation), hashlib.blake2b(head, digest_size=16).digest()


def _context_window(context: HookContext) -> int | None:
    """The window of the model this call goes to — the client's first, so an
    llm_profile override counts; the agent's configured model otherwise."""
    window = getattr(context.llm, "context_window", None)
    if isinstance(window, int) and window > 0:
        return window
    agent = context.agent
    if getattr(agent, "agent_config", None) is None or getattr(agent, "system_config", None) is None:
        return None
    try:
        from agent_system.llm.factory import resolve_llm_config_for_agent
        window = resolve_llm_config_for_agent(agent.system_config, agent.agent_config).spec.context_window
    except Exception as e:
        logger.debug("[ContextEngineer] no context window for %s: %s", context.agent_name, e)
        return None
    return window if isinstance(window, int) and window > 0 else None


def _due_level(tokens: int, cfg: CompactionConfig) -> int:
    """The deepest layer whose threshold ``tokens`` has reached, 0 for none."""
    return max((level for level, threshold in ((1, cfg.layer1_threshold),
                                               (2, cfg.layer2_threshold),
                                               (3, cfg.layer3_threshold))
                if tokens >= threshold), default=0)

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
# media/metadata.json is rewritten on every store and every reuse. Without it a
# session that only evicted media in the last days looked idle by its database
# timestamps, and the sweep deleted files a live hint still pointed at.
_SESSION_MARKERS = ("archive.db", "tool_results.db", "core_memory.json", "media/metadata.json")
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
    # A raw tool_call id works too: call_… (OpenAI, Gemini) and toolu_…
    # (Anthropic), whose runs were told their id was "not a known reference".
    ("tool_result", re.compile(r"\A\$?(TR_[A-Za-z0-9_]+|(?:call|toolu)_[A-Za-z0-9_]+)\Z")),
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
    
    All stored information can be retrieved via tools.
    
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
        # Per session: the reference point of the hysteresis (_HysteresisMark).
        self._hysteresis_marks: OrderedDict[str, _HysteresisMark] = OrderedDict()
        # Per session: the restoration block its prompt carries (_ShownBlock).
        self._shown_blocks: OrderedDict[str, _ShownBlock] = OrderedDict()
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
        # One summary client per llm profile (tool_result_summary_profile).
        self._summary_llms: dict[str, Any] = {}

        self.apply_config(None)

        logger.info(
            f"ContextEngineerPlugin initialized: "
            f"thresholds=L1:{self.layer1_threshold}/L2:{self.layer2_threshold}/"
            f"L3:{self.layer3_threshold}, target={self.target_tokens}, "
            f"min_tokens_between={self.min_tokens_between_compactions}, "
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
            # A field with a default_factory has no `default` -- it holds
            # MISSING, which is truthy and not iterable. Running the factory
            # is what the dataclass constructor does; this loop sets the
            # attributes itself and has to do the same. (Before, the one list
            # field was saved from MISSING only by schema.yaml happening to
            # carry `default: []`.)
            fallback = f.default_factory() if f.default_factory is not MISSING else f.default
            setattr(self, f.name, _coerce(merged[f.name], f.type, f.name)
                    if f.name in merged else fallback)

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
        self.enable_semantic_search = bool(merged.get("enable_semantic_search", False))
        self.core_memory_max_tokens = int(merged.get("core_memory_max_tokens", 2000))
        self._storage_base = Path(merged.get("storage_path") or data_path("context_engineer"))

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
        rewriting a file in place leaves it untouched, and
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

    def _compaction_config(self, overrides: dict[str, Any]) -> CompactionConfig:
        """Plugin values with an agent's overrides on top, by field name."""
        # Mapping by name drops a misspelled key without a word; plugins.yaml is
        # checked in apply_config, an agent's hooks.overrides only here.
        unknown = unknown_config_keys(overrides)
        if unknown:
            logger.warning(
                "[ContextEngineer] agent override keys that reach no setting "
                "(misspelled?): %s", ", ".join(unknown))
        plugin_level = sorted(set(overrides) & PLUGIN_LEVEL_KEYS)
        if plugin_level:
            logger.warning(
                "[ContextEngineer] agent override keys that only plugins.yaml can "
                "set, ignored per agent: %s", ", ".join(plugin_level))
        # A key left blank (None) sets nothing and keeps the plugin's value.
        # Read as a value, a blank tool_result_summary_tools became the empty
        # list -- every tool -- over the plugin's own patterns.
        return compaction_config_from({
            **{f.name: getattr(self, f.name) for f in fields(CompactionConfig)},
            **{k: v for k, v in overrides.items()
               if k not in PLUGIN_LEVEL_KEYS and v is not None},
        })

    def _summarizer(self, context: HookContext, cfg: CompactionConfig):
        """What writes the summary of a long tool result, or None for none.

        The engine knows nothing of profiles or clients; it calls this. Every
        failure ends as None there, and the result is stored all the same.
        """
        profile = cfg.tool_result_summary_profile
        if not profile or cfg.tool_result_summary_from <= 0:
            return None
        system_config = getattr(getattr(context, "agent", None), "system_config", None)
        if system_config is None:
            return None
        # Asked here, not in the call: without a client there is no summary,
        # and a selection made on one that cannot be built would take every
        # long result out of the conversation for a pointer nobody writes.
        llm = self._summary_llm(system_config, profile)
        if llm is None:
            return None
        token = getattr(context, "cancellation_token", None)

        async def summarize(text: str, tool_name: str) -> str | None:
            from datetime import datetime as _dt
            from agent_system.llm.models import ChatMessage
            prompt = (f"Summarize what this {tool_name} result says, for another agent that must act on it "
                      f"without seeing the original. Keep every decision, number, name, path and open "
                      f"question; drop repetition and ceremony. No preamble, no markdown headings.\n\n{text}")
            # The time limit is the engine's, for the whole round: it knows how
            # many results share it. The token ends the call when the user
            # stops the turn.
            answer = await llm.chat(
                messages=[ChatMessage(role="user", content=prompt, timestamp=_dt.now())],
                cancellation_token=token)
            return answer if isinstance(answer, str) else str(answer)

        return summarize

    def _summary_llm(self, system_config: Any, profile: str):
        """One client per profile, kept for the life of the plugin -- a failure
        too: a profile that does not resolve would otherwise be rebuilt, and
        logged, for every single result."""
        if profile in self._summary_llms:
            return self._summary_llms[profile]
        try:
            from agent_system.llm.factory import create_llm_from_profile
            client = create_llm_from_profile(system_config, profile)
        except Exception as exc:  # noqa: BLE001 - no summary is not a failed compaction
            logger.warning("[ContextEngineer] no summary client for profile %r, none will be written: %s",
                           profile, exc)
            client = None
        self._summary_llms[profile] = client
        return client

    def _get_session_components(self, session_id: str,
                                overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """Get or create session-scoped components.

        Args:
            session_id: Session identifier
            overrides: Optional per-agent overrides for CompactionConfig fields.
                Sourced from ``context.hook_config`` in ``engineer_context``.
                A caller without them (a tool) keeps what is
                there; a caller with different ones replaces the compaction
                config. The stores keep the settings they were created with.

        Returns:
            Dict with tool_store, core_memory, archival_memory
        """
        if session_id in self._session_components:
            components = self._session_components[session_id]
            components["last_accessed"] = time.time()
            # Whoever creates the components first used to fix their config for
            # good. The tools (/compact, list, read) arrive without the agent's
            # hooks.overrides, so a tool call before the session's first step
            # replaced an agent's own thresholds with the plugin defaults. The
            # hook always brings them; apply them.
            if overrides and overrides != components.get("overrides"):
                components["strategy"].config = self._compaction_config(overrides)
                components["overrides"] = dict(overrides)
            return components

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
            
                # Per-agent hook overrides relax fields like `tool_result_keep_last`
                # for media-heavy agents (cover_artist, repeated comfyui image
                # loads) without touching the plugin-wide default for everyone else.
                # By field name, so an override now works for EVERY field — the
                # hand-written list this replaces silently ignored an override for
                # any field its author had not thought to include, and three of
                # them (store_media_before_compaction, media_store_ttl_seconds,
                # media_store_max_files) were not even wired to `_o`.
                compaction_config = self._compaction_config(ov)

                # Initialize media store for inline media preservation. From the
                # session's config, not the plugin's: built from the plugin
                # attributes, an agent's media_store_* overrides reached nothing.
                media_store = None
                if compaction_config.store_media_before_compaction:
                    media_store = MediaStore(
                        storage_path=session_path / "media",
                        ttl_seconds=compaction_config.media_store_ttl_seconds,
                        max_files=compaction_config.media_store_max_files
                    )
            
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
                    "overrides": dict(ov),
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
            # without changing the plugin-wide default.
            hook_overrides = context.hook_config if context.hook_config else None

            # Get session components (needed for token/byte estimation and compaction)
            components = self._get_session_components(session_id, overrides=hook_overrides)
            strategy: LayeredCompactionStrategy = components["strategy"]
            # Every decision below reads the SESSION's config. Reading the
            # plugin-level attributes here ignored an agent's own thresholds at
            # the gate, before the strategy that honours them was ever asked.
            cfg = strategy.config
            # Before the gate asks what arrives: whether a long result is worth
            # a summary is part of that question, and the LLM layer is reached
            # through the agent of THIS call, while the strategy outlives it.
            strategy.summarize = self._summarizer(context, cfg)

            # Get actual or estimated token usage (prefer actual from usage_tracker)
            current_tokens, reading_is_whole = self._read_tokens(context, messages_as_dicts, strategy)

            # A new tool result too big for the window (Pre-Layer T) is stored
            # whatever the thresholds and the hysteresis below say. Those decide
            # on the call as it will go out — without the results T stores. Read
            # again rather than subtracted: a provider count from the previous
            # call never contained them, and max(count, estimate) minus their
            # estimate undercut it.
            context_window = _context_window(context)
            arrivals = strategy.oversized_arrivals(messages_as_dicts, context_window)
            gate_tokens = current_tokens
            gate_messages = messages_as_dicts
            if arrivals:
                gate_messages = list(messages_as_dicts)
                for i in arrivals:
                    gate_messages[i] = {**gate_messages[i], "content": ""}
                gate_tokens = min(current_tokens, self._read_tokens(context, gate_messages, strategy)[0])

            # The hysteresis base follows every measurement, including the calls
            # the threshold gate below turns away: taken only on a call that
            # passed the gate, the first reading after a compaction would be the
            # one that crosses the threshold again, and it would hold exactly
            # the compaction that is due. Except readings of part of the picture:
            # stale provider counts (see _read_tokens), and the compact tool's,
            # which leaves the system prompt out — lowered to that, the next
            # call's reading was released by the prompt's size alone.
            mark = self._hysteresis_marks.get(session_id)
            if mark is not None and not (context.metadata or {}).get("partial_view"):
                if reading_is_whole:
                    if mark.tokens is None or mark.provisional or gate_tokens < mark.tokens:
                        mark.tokens = gate_tokens
                        mark.provisional = False
                elif mark.tokens is None:
                    mark.stale_readings += 1
                    if mark.stale_readings >= 2:
                        mark.tokens = gate_tokens
                        mark.provisional = True

            # Estimate request bytes using strategy's method (for Gemini 100MB limit check)
            # — of the call as it will go out, like the token gates above.
            request_bytes = strategy._estimate_request_bytes(gate_messages)
            bytes_exceeded = request_bytes > cfg.max_request_bytes

            if bytes_exceeded:
                logger.warning(
                    f"[ContextEngineer] Session {session_id}: Request size "
                    f"{request_bytes / (1024*1024):.1f}MB exceeds "
                    f"{cfg.max_request_bytes / (1024*1024):.0f}MB limit - forcing compaction"
                )
            
            # Check if compaction needed
            is_manual = context.metadata.get("manual_trigger", False) if context.metadata else False
            
            # Determine trigger event for media compaction BEFORE early return check
            # This is a pre_llm_call hook, so the last message is what the user just sent
            trigger_event = None
            # A person's message, behind any the agent loop added after it (the
            # step budget note follows drained input): a note alone is no new
            # turn, and evicting media on it rewrote old messages at the end of
            # every long run.
            last = len(messages_as_dicts) - 1
            while last >= 0 and is_injected_note(messages_as_dicts[last]):
                last -= 1
            if last >= 0 and messages_as_dicts[last].get("role") == "user":
                trigger_event = "user_message"
                # Note: final_response is detected in post-hooks, not here
            
            # Check if event-based media compaction should run even below threshold
            event_media_compaction_needed = False
            if trigger_event == "user_message" and cfg.compact_media_after_user_message:
                event_media_compaction_needed = True
                logger.debug(
                    f"[ContextEngineer] Session {session_id}: Event-based media compaction "
                    f"triggered by user_message (compact_media_after_user_message=true)"
                )
            
            # Check if always-compact-media is enabled (must run even below threshold)
            always_compact_media_enabled = cfg.always_compact_media_keep_last > 0

            # Over the message limit Pre-Layer P is due whatever the tokens. Not
            # asked here, the limit held only on calls another reason let in --
            # every call with always_compact_media on, none without it.
            over_message_limit = 0 < cfg.max_messages < len(messages_as_dicts)

            # Skip only if: not manual, below token threshold, below byte limit,
            # AND no event-based media compaction, AND always_compact_media disabled,
            # AND within the message limit
            below_gate = (not is_manual and gate_tokens < cfg.layer1_threshold and not bytes_exceeded
                          and not event_media_compaction_needed and not always_compact_media_enabled
                          and not over_message_limit)
            if below_gate and not arrivals:
                logger.debug(
                    f"[ContextEngineer] Session {session_id}: "
                    f"{current_tokens} tokens < {cfg.layer1_threshold} threshold, "
                    f"{request_bytes / (1024*1024):.1f}MB < {cfg.max_request_bytes / (1024*1024):.0f}MB limit, "
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
                        "threshold": cfg.layer1_threshold,
                        "request_bytes": request_bytes,
                        "max_request_bytes": cfg.max_request_bytes
                    }
                )

            # Hysteresis (min_tokens_between_compactions): after a run of the
            # layers that rewrite messages, the next waits until the context has
            # grown by that many tokens — unless a deeper layer has become due
            # since. Never for a manual run or the byte limit. The media passes
            # are not held: they used to bypass the old time limit ENTIRELY —
            # and took the token layers along, so with always_compact_media on
            # (production) the limit never held at all.
            growth = None
            held = False
            if mark is not None:
                # No base yet (only partial readings since the run): no growth.
                growth = 0 if mark.tokens is None else gate_tokens - mark.tokens
                held = (not is_manual and not bytes_exceeded
                        and growth < cfg.min_tokens_between_compactions
                        and _due_level(gate_tokens, cfg) <= mark.level)
            held_back = held and not event_media_compaction_needed and not always_compact_media_enabled
            if held_back and not arrivals:
                logger.debug(
                    f"[ContextEngineer] Session {session_id}: held by hysteresis - "
                    f"{growth} tokens since the last compaction "
                    f"(min: {cfg.min_tokens_between_compactions})"
                )
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={
                        "reason": "hysteresis",
                        "current_tokens": current_tokens,
                        "tokens_since_last": growth,
                        "min_tokens_between_compactions": cfg.min_tokens_between_compactions,
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
                    session_id=session_id,
                    manual=is_manual,
                    rewrite_layers=not held,
                    context_window=context_window,
                    # In only for Pre-Layer T: no other pass may run that the
                    # gates above would have kept out (media dedup, Pre-Layer P).
                    arrivals_only=below_gate or held_back,
                    tokens_after_arrivals=gate_tokens,
                )
            finally:
                self._active_compactions.discard(session_id)

            # Only a run of a rewriting layer sets the mark — also one that
            # found nothing to take: that is exactly the run which would
            # otherwise repeat on every call. A media-only pass must not, or it
            # would hold the token layers back for good.
            # The level is what was due once Pre-Layer T had taken its share.
            if any(layer in (1, 2, 3, "P") for layer in result.layers_applied):
                self._hysteresis_marks[session_id] = _HysteresisMark(
                    level=_due_level(gate_tokens, cfg))
                self._hysteresis_marks.move_to_end(session_id)
                while len(self._hysteresis_marks) > _MAX_HYSTERESIS_MARKS:
                    self._hysteresis_marks.popitem(last=False)

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
                # A child of the request id, as context_summarizer does. With
                # the session id the per-request forwarder dropped the line:
                # it passes only the request's own id and its "_…" children,
                # so API and WebUI clients never saw it.
                status_id = context.request_id or session_id
                if context.request_id and context.agent is not None and hasattr(
                        context.agent, "next_internal_tool_request_id"):
                    status_id = await context.agent.next_internal_tool_request_id(
                        context.request_id)
                async with StatusScope(
                    status_bus,
                    "context_engineer",
                    status_id,
                    start_msg=f"Engineering context: {current_tokens} tokens (target: {cfg.target_tokens}){' [MANUAL]' if is_manual else ''}",
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
                    # The panel reads it, and with always_compact_media on it is
                    # the main media path — missing here, the panel showed 0.
                    "media_always_compacted": result.media_always_compacted,
                    "media_bytes_saved": result.media_bytes_saved
                })
                del self.stats_history[:-HISTORY_LIMIT]

                # Save history to disk (support both sync and async callbacks)
                if self.history_callback is not None:
                    if inspect.iscoroutinefunction(self.history_callback):
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
            
            # Inject restoration context (info about how to retrieve stored data).
            # The block sits right behind the system prompt and lists the
            # core-memory facts; changed on every call it differs, each
            # store_fact rewrote the front of the conversation and re-billed all
            # of it. So the session keeps the block it was shown until the
            # content it describes is gone from the conversation or the front is
            # rewritten anyway: messages left or were archived (a stored fact may
            # have left with its store_fact call), someone else rewrote the
            # conversation since the last call, or the first tool results were
            # stored — their placeholder names no way back, the section does.
            # Until then a new fact is still in the context, in its store_fact call.
            # A rewrite by a hook that runs after this one (context_summarizer) is
            # seen one call late, so the new block costs a break of its own — on
            # the summarized, short conversation. Accepted rather than have the
            # summarizer reach into this plugin to swap the block in its own call.
            strategy: LayeredCompactionStrategy = components["strategy"]
            fresh_block = await strategy.get_restoration_context()
            shown = self._shown_blocks.get(session_id)
            incoming_length, incoming_head = _conversation_probe(messages_as_dicts)
            if shown is None or (fresh_block != shown.text and (
                    result.messages_dropped or result.messages_pruned or result.messages_archived
                    or incoming_length < shown.conversation_length
                    or incoming_head != shown.head
                    or (result.tool_results_stored
                        and TOOL_RESULTS_SECTION in fresh_block
                        and TOOL_RESULTS_SECTION not in shown.text))):
                restoration_context = fresh_block
            else:
                restoration_context = shown.text
            self._shown_blocks[session_id] = _ShownBlock(
                restoration_context, *_conversation_probe(result.modified_messages))
            self._shown_blocks.move_to_end(session_id)
            while len(self._shown_blocks) > _MAX_HYSTERESIS_MARKS:
                self._shown_blocks.popitem(last=False)
            
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
                # The legacy copies were system messages, and only those match
                # by content. Matching every role deleted whatever merely
                # CONTAINED the header — a tool result of an agent reading this
                # very file, or a user quoting it — before the LLM call, and the
                # conversation was persisted without it.
                content = getattr(msg, "content", None)
                if (getattr(msg, "role", None) == "system" and isinstance(content, str)
                        and _RESTORATION_HEADER in content):
                    new_messages.pop(i)

            previous_block = next(
                (m for m in reversed(new_messages)
                 if getattr(m, "injected_by", None) == _RESTORATION_MARKER), None)
            # NOT "fresh_block when nothing of ours stands here": the block is
            # not persisted, so across requests there never is a previous one,
            # and taking the fresh text there would switch the hysteresis off
            # entirely. What it holds back is deliberate and measured (see the
            # paragraph above), and the reason it gives -- do not rewrite the
            # front for a fact that is still readable in its store_fact call --
            # is weaker now that the block sits at the end, but it is not mine
            # to overrule as a side effect of moving it.
            if restoration_context and (previous_block is None
                                        or previous_block.content != restoration_context):
                # Appended, not inserted behind the system prompt: there a
                # provider hoists it into the prompt head, and rebuilding it
                # per call invalidated the cache for everything behind it.
                # An earlier block keeps its place -- what it said was true
                # when it was written -- and an unchanged one is not written
                # again at all. It is not persisted with the conversation
                # either, and the reason is worth naming exactly: what
                # drops it is `is_volatile_note` in SessionTracker
                # (session_tracking.py), which needs BOTH the developer role
                # and `injected_by` -- a marked `user` turn, like debate_forum's
                # posts, is deliberately kept. The two filters on THIS path
                # throw away nothing but `role == 'system'`, so they are not
                # what keeps the copies from piling up.
                new_messages.append(ChatMessage(
                    role=DEVELOPER,
                    content=restoration_context,
                    injected_by=_RESTORATION_MARKER,
                ))
            
            # Build modified context with all fields
            modified_context = HookContext(
                hook_type=context.hook_type,
                request_id=context.request_id,
                session_id=context.session_id,
                agent=context.agent,
                agent_name=context.agent_name,
                messages=new_messages,
                # Carried on: the next pre-LLM hook (context_summarizer) reads
                # the per-request schema from here, and without it fell back
                # to the agent's shared, racy _current_tools_schema.
                tools_schema=context.tools_schema,
                llm_response=context.llm_response,
                tool_call=context.tool_call,
                tool_result=context.tool_result,
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
            
            # NOTE: what persists this compaction is the auto-sync in the
            # HookIntegrationManager, which writes the same slot after we return
            # with modified=True and filters system messages correctly (keeping
            # archived_ref types). The explicit set_compacted_messages() call
            # below is NOT a safety net for that: on this path the loop clears
            # the marker right after the hook chain (server.py), so no reader
            # ever sees what we stage here. It is written for the one case the
            # auto-sync cannot cover -- a hook that changes context.messages IN
            # PLACE instead of returning a new list; then the auto-sync has
            # nothing to notice and this call is the only thing that persists.
            # No hook does that today. Note that the compact TOOL sets the same
            # marker on its own path (server.py), and THAT one is read -- do not
            # let this comment talk you out of that one.
            # Only when something changed. The compact tool reaches this with the
            # agent attached, and a staged history makes the agent rebuild its
            # list after the tool as [system prompt] + staged + tool messages —
            # dropping every other leading system message, a prompt rewrite for a
            # run that compacted nothing.
            if something_compacted and context.agent and hasattr(context.agent, '_session_tracker'):
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
                    # modified is True on every run that got this far; this says
                    # whether the messages actually changed.
                    "compacted": something_compacted,
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
        """The token count alone; see _read_tokens."""
        return self._read_tokens(context, messages, strategy)[0]

    def _read_tokens(
        self,
        context: HookContext,
        messages: list[dict[str, Any]],
        strategy: LayeredCompactionStrategy
    ) -> tuple[int, bool]:
        """Get actual token count from last LLM response or estimate from messages.

        The second value says whether the reading may move the hysteresis base:
        not when the session HAS provider counts but they are stale. Such a
        reading is the estimate alone, and the next fresh one is on the
        provider's scale again — measured against a base taken from the stale
        one, the growth jumped by the gap between the two scales.

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
            Maximum of actual or estimated token count, and whether it may move
            the hysteresis base
        """
        stale = False
        estimated_tokens = strategy._estimate_messages_tokens(messages)

        # Include tool definition tokens in estimation (they consume context window)
        # The per-request schema first; the agent attribute is shared by all of
        # its sessions and only a fallback for callers that build a bare context.
        tools_schema = context.tools_schema
        if tools_schema is None and context.agent and hasattr(context.agent, '_current_tools_schema'):
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
                if hasattr(system_config, 'tool_registry') and system_config.tool_registry:
                    registry = system_config.tool_registry

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
                                stale = True
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

        return max_tokens, not stale
    
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
                if hasattr(system_config, 'tool_registry') and system_config.tool_registry:
                    registry = system_config.tool_registry
                    usage_tracker_plugin = registry.get_server('context_usage_tracker')
                    if usage_tracker_plugin and hasattr(usage_tracker_plugin, 'tracker'):
                        usage_tracker_plugin.tracker.invalidate_session(session_id, reason)
        except Exception as e:
            logger.debug(f"[ContextEngineer] Could not invalidate usage_tracker session: {e}")
    
    # === MCP Tool Handlers ===
    # These are called by the tool server when tools are invoked

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
            success with the category and importance the fact is kept under,
            or success False with the error
        """
        components = self._get_session_components(session_id)
        core_memory: CoreMemory = components["core_memory"]
        
        stored = await core_memory.add_fact(fact, category=category, importance=importance)
        if not stored:
            # add_fact refuses rather than push out more important facts. Said
            # as success, the agent believed a fact was kept that is nowhere.
            too_large = not core_memory.fits_alone(fact, category)
            return {
                "success": False,
                "error": (f"This fact alone is larger than core memory ({core_memory.max_tokens} "
                          f"tokens); store a shorter summary of it." if too_large else
                          "Core memory is full of more important facts; this one was "
                          "not stored. Use a higher importance if it matters more."),
                "importance": importance,
                "total_facts": len(core_memory.facts),
            }

        # The fact as core memory keeps it, not as it was asked for: an unknown
        # category is filed under "facts", the importance is clamped to 0..1,
        # and a fact stored before keeps its category and the higher importance.
        # Echoing the request told the agent a category nothing holds. (A
        # "fact_id" used to be here: add_fact's True -- facts have no ids.)
        needle = fact.strip().lower()
        kept = next((f for f in core_memory.facts if f.content.strip().lower() == needle), None)
        if kept is None:
            # A concurrent store_fact of the session pushed it out again while
            # this one saved (add_fact awaits the save on a thread).
            return {
                "success": False,
                "error": ("The fact was stored and pushed out again at once by a more "
                          "important one; it is not kept. Use a higher importance if it "
                          "matters more."),
                "importance": importance,
                "total_facts": len(core_memory.facts),
            }
        return {
            "success": True,
            "category": kept.category,
            "importance": kept.importance,
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
        # data/... lands in the data directory (agent_system/paths.py)
        file_path = resolve_data_path(path)

        # SECURITY: `path` comes straight from LLM tool args
        # (read(ref="/path/to/file.wav")). Without containment this is an
        # arbitrary file read primitive: the file is base64-injected into the
        # model context. Restrict
        # to the project data roots where all plugin media legitimately lives
        # (context_engineer media store, comfyui outputs, audio, covers - all
        # under data/). Reject anything outside, including symlink escapes.
        allowed_roots = []
        for root in (self._storage_base, data_path()):
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

        # What compaction counts the item at once it is back: priced by file
        # size, a 2 MB cover was announced at ~700k tokens, above the whole
        # window, for an image that costs about a thousand.
        from agent_system.llm.token_utils import estimate_file_tokens
        estimated_tokens = estimate_file_tokens(file_path, file_type=content_type)

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
            # The hysteresis mark stays (see _MAX_HYSTERESIS_MARKS).

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
