"""JSON Store Plugin - validated JSON working documents for LLM agents.

Agents that assemble large JSON documents across multiple steps (merge
sub-agent results, update single fields) fail when they re-type the JSON
through their own token stream: values get shortened, ``\\n`` escapes turn
into raw newlines, commas vanish. This plugin moves those operations into
code: the document lives as a **Python data structure** (not text), every
input is parsed (= validated) on the way in, and every read serialises a
guaranteed-valid canonical JSON on the way out.

Exposed as ONE tool (``{{name}}_manage_json``) dispatched via ``operation``
(same pattern as sub_agent_manager) to keep the agent's tool list small:
  - write        create/replace a document
  - read         whole document or a sub-path, canonical JSON
  - merge        deep-merge an object into the document (code, not LLM)
  - set_value    set one value at a path (``a.b[2].c``)
  - delete_keys  delete paths from the document
  - delete_doc   drop a whole document
  - list         what documents exist
  - outline      structure overview without values (token-cheap inspection)

Input tolerance: ``data`` (structured object, preferred) or ``json_text``
(string). ``json_text`` is fence-stripped (``agent_system.utils.json_utils.
strip_markdown_fences``) and, when strict parsing fails, repaired via
``repair_json`` (json-repair library: raw newlines in strings, missing
commas, truncation, ...). Unrecoverable input is rejected with a precise
error so the agent can fix its call. Repairs are reported in the result.

Documents are scoped per agent session by default (``session_scoped:
false`` shares them process-wide; calls without a session id share the
"global" namespace). They live in memory AND — like the todo/memory
plugins — are persisted per namespace to disk
(``data/json_store/<server>/<namespace>/<doc>.json``, incl. the owning
session), so a CLI abort + continue or a server restart does not lose
the working documents. Idle namespaces are evicted from MEMORY after
``namespace_ttl_hours`` (reloaded from disk on next access); files are
removed only by ``file_retention_hours``. Undo history stays volatile.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import shutil
import time
from collections import deque
from difflib import get_close_matches

from agent_system.utils.suggest import suggest_path
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from agent_system.paths import data_path
from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.utils.id import short_id
from agent_system.utils.json_utils import repair_json, strip_markdown_fences

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

# Windows refuses os.replace onto a file another process has open, and a read
# that lands during a replace -- both with PermissionError. Retry briefly.
# Elsewhere a PermissionError is a real permission problem: raise at once.
_RETRY_SHARING = os.name == "nt"
_SHARING_ATTEMPTS = 40
_SHARING_PAUSE_S = 0.05


def _retry_sharing_violation(fn):
    for attempt in range(_SHARING_ATTEMPTS):
        try:
            return fn()
        except PermissionError:
            if not _RETRY_SHARING or attempt == _SHARING_ATTEMPTS - 1:
                raise
            time.sleep(_SHARING_PAUSE_S)


def _file_sig(st: os.stat_result) -> Tuple[int, int, int]:
    """Identity, time and size: a file another process put in place differs
    in its inode even when time and size happen to match."""
    return (st.st_ino, st.st_mtime_ns, st.st_size)


class JsonStoreServer(SchemaBasedToolServer):
    """In-memory, validated JSON document store."""

    def __init__(self, name: str, system_config: "AgentSystemConfig",
                 server_config: "ToolServerConfig") -> None:
        super().__init__(name, system_config, server_config)

        config_dict = getattr(server_config, "config", None) or {}
        self._session_scoped: bool = bool(config_dict.get("session_scoped", True))
        # A missing ``namespace`` silently falls back to the caller's own
        # session (see ``_ns``). For a store whose ONLY purpose is a shared
        # workspace that default is wrong and invisible: the write reports
        # success and returns a doc id that nobody — not even its author, who
        # reads WITH the namespace — can ever resolve. Observed on a v6 beat
        # panel: one writer omitted the namespace five times in a row, each
        # ~11 KB delta landing in its private session, five failed read-backs,
        # ~3 minutes and five LLM turns burnt before it happened to switch.
        # With this flag the first such call fails loudly instead.
        self._require_namespace: bool = bool(
            config_dict.get("require_namespace", False))
        self._max_docs: int = int(config_dict.get("max_docs", 50))
        self._max_doc_bytes: int = int(config_dict.get("max_doc_bytes", 2 * 1024 * 1024))
        # Idle namespaces are dropped after this TTL so a months-running server
        # process doesn't accumulate session state forever. 0 disables.
        self._namespace_ttl_s: float = float(
            config_dict.get("namespace_ttl_hours", 48)) * 3600.0
        # Optional per-document key models: doc name -> nested dict of allowed
        # keys. "*" allows any key at that level (dynamic names); {} = free
        # subtree (no further enforcement). Writes with keys outside the model
        # are rejected with the allowed keys listed.
        key_models = config_dict.get("key_models") or {}
        self._key_models: Dict[str, Any] = key_models if isinstance(key_models, dict) else {}
        # Explicit wrong->right key remaps applied before fuzzy matching (for
        # semantic renames fuzzy is too weak for, e.g. synopsis -> synopsis_text).
        aliases = config_dict.get("key_aliases") or {}
        self._key_aliases: Dict[str, str] = {
            str(k): str(v) for k, v in aliases.items()} if isinstance(aliases, dict) else {}

        # Write protection: a document belongs to the session that created it and
        # only that session may modify it; everyone sharing the namespace may
        # read. Agents share a namespace to collaborate, and a confused (or
        # overeager) reader would otherwise silently clobber the coordinator's
        # main document — observed live: three panel writers merging into and
        # deleting keys from the same 'synopsis' doc, shrinking it each time.
        # Ownership is bound to the runtime-injected _session_id (which the LLM
        # cannot forge), not to a token the model has to carry around.
        # 'shared' opts a document out at creation time.
        self._default_write_access: str = str(
            config_dict.get("default_write_access", "owner")).lower()

        # Undo: each mutation snapshots the document's prior state, so an agent
        # that made a mistake can revert it ('undo' operation) instead of
        # re-typing the previous JSON by hand — the exact error class the store
        # exists to avoid. The interface is single-step ("undo the last change"),
        # deliberately parameter-free (an LLM must never have to count how many
        # ops to reverse); depth lets it be called repeatedly to walk a few
        # steps back. 0 disables.
        self._undo_depth: int = int(config_dict.get("undo_depth", 5))

        # Disk persistence (like the todo/memory plugins, but per NAMESPACE —
        # a coordinator and its sub-agents share documents via the namespace,
        # so that is the durable unit, not the session): every mutation writes
        # the touched document to <storage_path>/<server>/<namespace>/<doc>.json
        # (atomic tmp+rename), including its owner session id — the read-only
        # write protection survives a restart because session ids are restored
        # on continue. A namespace missing from memory is lazily reloaded from
        # disk in _bucket(). Undo history stays volatile by design.
        self._persist: bool = bool(config_dict.get("persist", True))
        self._storage_dir: Path = (
            Path(str(config_dict.get("storage_path") or data_path("json_store")))
            / self._safe_filename(name))
        # Files (not memory) are cleaned by this retention; 0 keeps them forever.
        self._file_retention_s: float = float(
            config_dict.get("file_retention_hours", 14 * 24)) * 3600.0

        # namespace (session id or "global") -> doc name -> Python object
        self._docs: Dict[str, Dict[str, Any]] = {}
        # namespace -> doc name -> owning session id (None = shared)
        self._doc_owners: Dict[str, Dict[str, Optional[str]]] = {}
        # namespace -> doc name -> bounded stack of pre-mutation snapshots
        self._doc_history: Dict[str, Dict[str, "deque[Dict[str, Any]]"]] = {}
        self._ns_last_access: Dict[str, float] = {}
        # namespace -> file name -> ((inode, mtime_ns, size) as this process
        # last loaded or saved it, doc name). A file whose stat differs was
        # written by another process and is reloaded before the next operation.
        self._file_sigs: Dict[str, Dict[str, Tuple[Tuple[int, int, int], Optional[str]]]] = {}

        # A read answer is sent to the model whole; above this it is refused
        # with a hint to narrow it, never cut (cut JSON is invalid JSON). A store
        # that sizes its documents (max_doc_bytes set) reads them whole, indented.
        default_read = (2 * self._max_doc_bytes if "max_doc_bytes" in config_dict
                        else 50000)
        try:
            self._max_read_chars: int = int(config_dict.get("max_read_chars", default_read))
        except (TypeError, ValueError):
            self._max_read_chars = default_read
        if self._max_read_chars < 1:
            self._max_read_chars = default_read

        if self._persist:
            self._sweep_expired_files()

    def get_template_vars(self) -> Dict[str, Any]:
        """schema.yaml variables: where a call without namespace lands."""
        return {"name": self.name,
                "ns_default": "your session" if self._session_scoped else "the default store"}

    # ------------------------------------------------------------------
    # Namespaces
    # ------------------------------------------------------------------

    def _ns(self, params: Dict[str, Any]) -> str:
        # Explicit namespace wins — lets a coordinator and its (separately
        # sessioned) sub-agents share one document by passing the same id.
        explicit = params.get("namespace")
        if explicit not in (None, ""):
            return str(explicit)
        if self._session_scoped:
            return str(params.get("_session_id") or "global")
        return "global"

    def _bucket(self, params: Dict[str, Any]) -> Dict[str, Any]:
        ns = self._ns(params)
        now = time.time()
        if self._namespace_ttl_s > 0:
            # Memory-only eviction: the files stay and are lazily reloaded on
            # the next access — durable cleanup is file_retention_hours' job.
            expired = [k for k, ts in self._ns_last_access.items()
                       if k != ns and now - ts > self._namespace_ttl_s]
            for k in expired:
                self._docs.pop(k, None)
                self._doc_owners.pop(k, None)
                self._doc_history.pop(k, None)
                self._ns_last_access.pop(k, None)
                self._file_sigs.pop(k, None)
                logger.info("json_store: evicted idle namespace '%s' (TTL)", k)
        self._ns_last_access[ns] = now
        if ns not in self._docs:
            self._sync_namespace(ns)
        return self._docs.setdefault(ns, {})

    # ------------------------------------------------------------------
    # Disk persistence (per namespace, one file per document)
    # ------------------------------------------------------------------

    _WIN_RESERVED: Set[str] = {"CON", "PRN", "AUX", "NUL",
                               *{f"COM{i}" for i in range(1, 10)},
                               *{f"LPT{i}" for i in range(1, 10)}}

    @classmethod
    def _safe_filename(cls, raw: str, hash_trailing_dot: bool = True) -> str:
        """Deterministic filesystem-safe name. Namespace and doc names are
        LLM-supplied, so they must not be able to escape the storage dir or
        hit Windows-invalid names — and distinct raws must never share a file.
        Only a pure-lowercase safe name maps to itself; everything else (incl.
        anything with uppercase — NTFS/macOS paths are case-INsensitive, so
        'Run7' and 'run7' would otherwise be the same file) gets a '~<sha1>'
        suffix of the exact raw. '~' is outside the safe charset, so the two
        forms can never collide with each other. A trailing '.' is hashed too:
        Windows drops it, so namespace 'run.' would open the directory 'run'."""
        s = re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:60]  # keep Windows MAX_PATH headroom
        reserved = s.split(".")[0].upper() in cls._WIN_RESERVED
        if (s != raw or s != s.lower() or reserved or not s.strip("._-")
                or (hash_trailing_dot and s.endswith("."))):
            # surrogatepass: names come from parsed LLM JSON, which can carry
            # lone surrogates — hashing must never raise on them.
            digest = hashlib.sha1(
                raw.encode("utf-8", "surrogatepass")).hexdigest()[:10]
            s = f"{s.strip('._-')[:48].lower() or 'x'}~{digest}"
        return s

    def _doc_file(self, ns: str, doc: str) -> Path:
        return self._storage_dir / self._safe_filename(ns) / (
            self._safe_filename(doc) + ".json")

    def _persist_doc(self, params: Dict[str, Any], name: str) -> Optional[str]:
        """Write one document (value + owner + original names) to disk,
        atomically. Returns an error string instead of raising — persistence
        failure must not roll back the in-memory mutation, but it must be
        VISIBLE (logged + surfaced in the result), never silent."""
        if not self._persist:
            return None
        ns = self._ns(params)
        try:
            payload = {"namespace": ns, "doc": name,
                       "owner": self._doc_owners.get(ns, {}).get(name),
                       "data": self._docs.get(ns, {}).get(name)}
            path = self._doc_file(ns, name)
            self._file_sigs.setdefault(ns, {})[path.name] = (
                self._write_payload(path, payload), name)
            return None
        except (OSError, UnicodeError, ValueError, TypeError) as e:
            logger.error("json_store: persisting '%s' (ns '%s') failed: %s",
                         name, ns, e)
            return f"document saved in memory, but writing it to disk failed: {e}"

    @staticmethod
    def _write_payload(path: Path, payload: Dict[str, Any]) -> Tuple[int, int, int]:
        """Write atomically (tmp + replace); return the sig of the file written."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # Per-process tmp name: two processes sharing the storage must not
        # write into (and then rename) each other's half-written tmp file.
        tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
        # surrogatepass mirrors _sync_namespace's read: document values come
        # from parsed LLM JSON and may contain lone surrogates — strict
        # utf-8 would raise and the doc would silently stay memory-only.
        tmp.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8", errors="surrogatepass")
        try:
            # Stat the tmp BEFORE the replace: the rename keeps its identity,
            # so a file another process puts in place right after ours never
            # passes for ours.
            st = os.stat(tmp)
            _retry_sharing_violation(lambda: os.replace(tmp, path))
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        return _file_sig(st)

    def _unpersist_doc(self, params: Dict[str, Any], name: str) -> Optional[str]:
        """Remove a document's file. Returns an error string when that fails —
        the file then keeps its sig, so a sync does not load it back."""
        if not self._persist:
            return None
        ns = self._ns(params)
        path = self._doc_file(ns, name)
        try:
            _retry_sharing_violation(lambda: path.unlink(missing_ok=True))
        except OSError as e:
            logger.error("json_store: removing persisted '%s' failed: %s", name, e)
            return f"document deleted in memory, but removing its file failed: {e}"
        self._file_sigs.get(ns, {}).pop(path.name, None)
        return None

    def _sync_namespace(self, ns: str, migrate: bool = True) -> None:
        """Bring a namespace in memory up to date with its directory: load
        files this process has not seen or that changed since it last loaded
        or saved them (another process wrote them), drop documents whose file
        another process deleted. A reloaded document takes value and owner
        from the file and loses its undo history — those snapshots describe a
        state that is no longer the one on disk. Documents never saved (persist
        failed) are left alone. A corrupt file is skipped with a warning."""
        bucket = self._docs.setdefault(ns, {})
        if not self._persist:
            return
        owners = self._doc_owners.setdefault(ns, {})
        history = self._doc_history.get(ns, {})
        first_sync = ns not in self._file_sigs
        sigs = self._file_sigs.setdefault(ns, {})
        ns_dir = self._storage_dir / self._safe_filename(ns)
        legacy_dir = self._storage_dir / self._safe_filename(ns, hash_trailing_dot=False)
        if first_sync and legacy_dir != ns_dir:
            self._migrate_legacy_dir(ns, legacy_dir, ns_dir)
        try:
            files = {f.name: f for f in ns_dir.glob("*.json")} if ns_dir.is_dir() else {}
        except OSError as e:
            logger.warning("json_store: listing %s failed: %s", ns_dir, e)
            return
        for fname in [n for n in sigs if n not in files]:
            _, name = sigs.pop(fname)
            if name is not None:
                bucket.pop(name, None)
                owners.pop(name, None)
                history.pop(name, None)
        loaded = 0
        moved = False
        for fname, f in sorted(files.items()):
            try:
                sig = _file_sig(f.stat())
                if fname in sigs and sigs[fname][0] == sig:
                    continue
                payload = json.loads(_retry_sharing_violation(
                    lambda: f.read_text(encoding="utf-8", errors="surrogatepass")))
            except OSError as e:  # vanished or locked: try again next call
                logger.warning("json_store: reading %s failed: %s", f, e)
                continue
            except (ValueError, UnicodeError) as e:
                logger.warning("json_store: skipping corrupt persisted doc %s: %s",
                               f, e)
                sigs[fname] = (sig, None)
                continue
            if not isinstance(payload, dict):
                logger.warning("json_store: skipping malformed persisted "
                               "doc %s (not an object)", f)
                sigs[fname] = (sig, None)
                continue
            if payload.get("namespace") not in (None, ns):
                # Another namespace's file (Windows once stored 'run.' in the
                # folder of 'run'); _migrate_legacy_dir moves it out.
                sigs[fname] = (sig, None)
                continue
            name = str(payload.get("doc") or f.stem)
            expected = self._safe_filename(name) + ".json"
            if fname != expected:
                # Written under an older naming rule: move it to its name now,
                # then load the result in a second pass. A file still there in
                # that pass could not be removed; its data is in the new one.
                if not migrate:
                    sigs[fname] = (sig, None)
                    continue
                if self._move_legacy_file(f, payload, ns_dir / expected) is not None:
                    moved = True
                    continue
            bucket[name] = payload.get("data")
            owners[name] = payload.get("owner")
            history.pop(name, None)
            sigs[fname] = (sig, name)
            loaded += 1
        if loaded:
            logger.info("json_store: loaded %d doc(s) for namespace '%s' "
                        "from disk", loaded, ns)
        if moved:
            self._sync_namespace(ns, migrate=False)

    @staticmethod
    def _remove_file(f: Path) -> None:
        try:
            _retry_sharing_violation(lambda: f.unlink(missing_ok=True))
        except OSError as e:
            logger.warning("json_store: removing %s failed: %s", f, e)

    def _move_legacy_file(self, f: Path, payload: Dict[str, Any],
                          target: Path) -> Optional[Tuple[int, int, int]]:
        """Move a file saved under an older name to ``target`` without losing
        newer data: a hard link never overwrites (a second process migrating
        at the same moment gets FileExistsError). When both exist, the later
        written one wins — a process still running old code keeps writing the
        old name. Returns the target's sig, or None when ``f`` stays in place."""
        def old_is_newer() -> bool:
            return f.stat().st_mtime_ns > target.stat().st_mtime_ns
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(f, target)
            except FileExistsError:
                if old_is_newer():
                    self._write_payload(target, payload)
            except (OSError, AttributeError, NotImplementedError):
                # no hard links on this file system: check, then write
                if not target.exists() or old_is_newer():
                    self._write_payload(target, payload)
            sig = _file_sig(target.stat())
        except OSError as e:
            logger.warning("json_store: moving %s failed: %s", f, e)
            return None
        self._remove_file(f)
        return sig

    def _migrate_legacy_dir(self, ns: str, legacy_dir: Path, ns_dir: Path) -> None:
        """Move this namespace's files out of the folder an older naming rule
        gave it (a trailing '.', which Windows drops: 'run.' landed in 'run').
        Only files whose payload names this namespace move — on Windows the
        legacy folder is another namespace's folder."""
        try:
            if not legacy_dir.is_dir():
                return
            files = sorted(legacy_dir.glob("*.json"))
        except OSError:
            return
        for f in files:
            try:
                payload = json.loads(_retry_sharing_violation(
                    lambda: f.read_text(encoding="utf-8", errors="surrogatepass")))
            except (OSError, ValueError, UnicodeError):
                continue
            if not isinstance(payload, dict) or payload.get("namespace") != ns:
                continue
            target = ns_dir / (self._safe_filename(str(payload.get("doc") or f.stem))
                               + ".json")
            self._move_legacy_file(f, payload, target)
        try:
            # On Windows 'run.' IS the folder of namespace 'run': leave it.
            if legacy_dir.resolve().name == legacy_dir.name:
                legacy_dir.rmdir()   # only succeeds when empty
        except OSError:
            pass

    def _sweep_expired_files(self) -> None:
        """Drop namespace dirs whose NEWEST file is past the retention —
        run once at startup so abandoned working state doesn't pile up."""
        if self._file_retention_s <= 0 or not self._storage_dir.is_dir():
            return
        cutoff = time.time() - self._file_retention_s
        try:
            for ns_dir in self._storage_dir.iterdir():
                if not ns_dir.is_dir():
                    continue
                newest = max((f.stat().st_mtime for f in ns_dir.glob("*.json")),
                             default=ns_dir.stat().st_mtime)
                if newest < cutoff:
                    shutil.rmtree(ns_dir, ignore_errors=True)
                    logger.info("json_store: removed expired namespace dir %s",
                                ns_dir)
        except OSError as e:  # pragma: no cover - startup sweep is best-effort
            logger.warning("json_store: retention sweep failed: %s", e)

    # ------------------------------------------------------------------
    # Write protection (owner = creating session)
    # ------------------------------------------------------------------

    @staticmethod
    def _caller(params: Dict[str, Any]) -> str:
        """The calling session. Runtime-injected, not LLM-supplied."""
        return str(params.get("_session_id") or "global")

    def _owners(self, params: Dict[str, Any]) -> Dict[str, Optional[str]]:
        """doc name -> owning session id (None = shared, anyone may write)."""
        return self._doc_owners.setdefault(self._ns(params), {})

    def _write_guard(self, params: Dict[str, Any],
                     name: str) -> Optional[Dict[str, Any]]:
        """Reject a mutation of a document owned by a different session.

        Returns an error dict, or None when the write may proceed (document is
        new, shared, or owned by the caller). Reads never pass through here.

        Ownership is per SESSION, deliberately not per agent-role: a panel runs
        several sub-agents of the SAME type (e.g. three v6_synopsis_writer) in
        one namespace, and role-based takeover would let them overwrite each
        other's documents — exactly the confused-writer clobbering this guards
        against.
        """
        if name not in self._bucket(params):
            return None  # creating a new document — ownership is set on commit
        owner = self._owners(params).get(name)
        if owner is None or owner == self._caller(params):
            return None  # shared, or owned by the caller
        return {
            "status": "error",
            "error": f"Document '{name}' belongs to another agent and is "
                     f"read-only for you. Read it freely, but do not modify it: "
                     f"write your own document instead (omit 'doc' to get a "
                     f"fresh id) and let its owner merge your changes.",
        }

    def _resolve_write_access(self, params: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """(access, error) — the effective write_access for a create, validated.
        error is set for an invalid explicit value."""
        access = str(params.get("write_access") or self._default_write_access).lower()
        if access not in ("owner", "shared"):
            return None, "write_access must be 'owner' or 'shared'"
        return access, None

    def _register_owner(self, params: Dict[str, Any], name: str,
                        access: str) -> None:
        """Record ownership of a freshly created document (access pre-validated)."""
        self._owners(params)[name] = (
            self._caller(params) if access == "owner" else None)

    # ------------------------------------------------------------------
    # Undo history (snapshot before every mutation)
    # ------------------------------------------------------------------

    def _history(self, params: Dict[str, Any]) -> Dict[str, "deque[Dict[str, Any]]"]:
        return self._doc_history.setdefault(self._ns(params), {})

    def _snapshot(self, params: Dict[str, Any], name: str, op: str) -> None:
        """Capture a document's state BEFORE a mutation so ``undo`` can revert
        exactly this operation. Records existence, a deep copy of the value, and
        the owner (so undo restores ownership, and undo of a deleted doc knows
        who may do it). No-op when undo is disabled."""
        if self._undo_depth <= 0:
            return
        bucket = self._bucket(params)
        hist = self._history(params)
        # Re-insert the key at the end so dict order = recency (most-recently
        # touched last); _prune_history evicts the oldest first.
        stack = hist.pop(name, None) or deque(maxlen=self._undo_depth)
        existed = name in bucket
        stack.append({
            "op": op,
            "existed": existed,
            "value": copy.deepcopy(bucket[name]) if existed else None,
            "owner": self._owners(params).get(name),
        })
        hist[name] = stack
        self._prune_history(hist, bucket)

    def _pop_snapshot(self, params: Dict[str, Any], name: str) -> None:
        """Drop the most recent snapshot for ``name`` — used to undo a snapshot
        taken for a mutation that turned out to change nothing (a no-op), so a
        no-op never erodes the bounded undo depth. Removes the entry if empty."""
        hist = self._history(params)
        stack = hist.get(name)
        if stack:
            stack.pop()
            if not stack:
                del hist[name]

    def _prune_history(self, hist: Dict[str, "deque[Dict[str, Any]]"],
                       bucket: Dict[str, Any]) -> None:
        """Bound per-namespace history. Deleted docs keep their history so their
        deletion can be undone, but that would otherwise grow forever across
        doc-name churn (create/delete of ever-new auto-ids). Cap the number of
        tracked docs at 2x max_docs (room for the live set plus recently-deleted
        docs), evicting the oldest — dead docs (no longer in the bucket) first,
        then the least-recently-touched live ones."""
        cap = max(1, self._max_docs * 2)
        if len(hist) <= cap:
            return
        dead = [n for n in hist if n not in bucket]
        live = [n for n in hist if n in bucket]
        for n in dead + live:
            if len(hist) <= cap:
                break
            del hist[n]

    @staticmethod
    def _new_doc_id(bucket: Dict[str, Any]) -> str:
        """Mint a short, collision-free document id within ``bucket``. Used by
        ``write`` when the caller omits ``doc`` — parallel writers (e.g. a panel
        of synopsis writers) each get their own document and return its id
        upstream, so no name coordination is needed and nothing gets clobbered."""
        for _ in range(10000):
            candidate = f"doc_{short_id(8)}"
            if candidate not in bucket:
                return candidate
        return f"doc_{short_id(16)}"  # pragma: no cover — 8-char space exhausted

    # ------------------------------------------------------------------
    # Parsing / validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json_text(text: str) -> Tuple[Any, List[str]]:
        """Parse ``json_text`` tolerantly. Returns (value, applied_repairs).

        Strict parse first; on failure the shared repair_json (json-repair
        lib) recovers common LLM mistakes (raw newlines in strings, missing
        commas, truncation). Raises ``ValueError`` with an agent-actionable
        message when even repair can't produce an object/array.
        """
        repairs: List[str] = []
        candidate = strip_markdown_fences(text)
        if candidate != text.strip():
            repairs.append("stripped_code_fence")
        try:
            return json.loads(candidate), repairs
        except json.JSONDecodeError as first_err:
            repaired = repair_json(candidate)
            # Only accept containers from the repair path: for document input a
            # scalar result means the repairer swallowed non-JSON prose.
            if isinstance(repaired, (dict, list)):
                repairs.append("auto_repaired_json")
                return repaired, repairs
            raise ValueError(
                f"Invalid JSON at line {first_err.lineno} column {first_err.colno}: "
                f"{first_err.msg}. Auto-repair could not recover a JSON "
                f"object/array — check for truncation or non-JSON content, then resend."
            ) from first_err

    def _extract_payload(self, params: Dict[str, Any]) -> Tuple[Any, List[str]]:
        """Get the input value from ``data`` (preferred) or ``json_text``."""
        if "data" in params and params["data"] is not None:
            return params["data"], []
        json_text = params.get("json_text")
        if isinstance(json_text, str) and json_text.strip():
            return self._parse_json_text(json_text)
        raise ValueError("Provide the JSON either as 'data' (object) or 'json_text' (string).")

    @staticmethod
    def _as_index(seg: Any) -> Optional[int]:
        """Non-negative list index from a path segment, else None."""
        if isinstance(seg, int):
            return seg
        s = str(seg)
        return int(s) if s.isdigit() else None

    @staticmethod
    def _split_path(path: str) -> List[Any]:
        """``a.b[2].c`` -> ['a', 'b', 2, 'c']; ``[0].x`` -> [0, 'x']."""
        segments: List[Any] = []
        for raw in path.split("."):
            raw = raw.strip()
            if not raw:
                continue
            m = re.match(r"^([^\[\]]*)((?:\[\d+\])*)$", raw)
            if not m or (not m.group(1) and not m.group(2)):
                raise ValueError(f"Invalid path segment: '{raw}'")
            name, indices = m.group(1), m.group(2)
            if name:
                segments.append(name)
            for idx in re.findall(r"\[(\d+)\]", indices):
                segments.append(int(idx))
        if not segments:
            raise ValueError("Empty path")
        return segments

    @classmethod
    def _resolve(cls, value: Any, segments: List[Any]) -> Any:
        """Walk ``segments`` down ``value`` with agent-friendly errors."""
        current = value
        for seg in segments:
            if isinstance(current, list):
                idx = cls._as_index(seg)
                if idx is None or not (0 <= idx < len(current)):
                    raise ValueError(f"Path segment '{seg}' not found (list of length "
                                     f"{len(current)})")
                current = current[idx]
            elif isinstance(current, dict):
                if seg not in current:
                    hint = suggest_path(str(seg), [str(k) for k in current])
                    raise ValueError(
                        f"Path segment '{seg}' not found"
                        + (f" — did you mean '{hint}'?" if hint else "")
                        + f" (available keys: {list(current.keys())[:15]})")
                current = current[seg]
            else:
                raise ValueError(f"Cannot descend into {type(current).__name__} at '{seg}'")
        return current

    @classmethod
    def _deep_merge(cls, base: Any, incoming: Any, array_mode: str) -> Any:
        """Non-mutating deep merge: dicts merge recursively, arrays replace
        (or concat), scalars overwrite. ``base`` is never modified, so a
        failed post-merge check needs no rollback."""
        if isinstance(base, dict) and isinstance(incoming, dict):
            result = dict(base)
            for key, value in incoming.items():
                result[key] = (cls._deep_merge(base[key], value, array_mode)
                               if key in base else value)
            return result
        if isinstance(base, list) and isinstance(incoming, list) and array_mode == "concat":
            return base + incoming
        return incoming

    # JSON type names usable as a LEAF constraint in a key model (a string
    # model node, e.g. ``title_suggestion: string``). Value = predicate.
    _JSON_TYPE_CHECKS = {
        "string": lambda v: isinstance(v, str),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
        "null": lambda v: v is None,
        "any": lambda v: True,
    }

    @staticmethod
    def _json_type_name(value: Any) -> str:
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, str):
            return "string"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "number"
        if isinstance(value, list):
            return "array"
        if isinstance(value, dict):
            return "object"
        return "null" if value is None else type(value).__name__

    def _check_type(self, value: Any, type_name: str, path: str,
                    violations: List[str]) -> None:
        """Enforce a leaf type constraint. ``None`` always passes (a typed field
        left empty/optional must not block the write — the bug class this guards
        against is the wrong CONTAINER, e.g. an array where a scalar belongs, not
        a null). An unrecognised type name is treated as free (a config typo must
        not punish the agent)."""
        check = self._JSON_TYPE_CHECKS.get(type_name.strip().lower())
        if check is None or value is None or check(value):
            return
        violations.append(
            f"'{path}' must be {type_name.strip().lower()} but is "
            f"{self._json_type_name(value)}")

    def _resolve_key(self, key: str, allowed: List[str]) -> Optional[str]:
        """Map an unknown key to an allowed one: explicit alias first, then a
        confident fuzzy match (typos like 'genere'→'genre'). None if neither.
        Semantic renames (e.g. synopsis→synopsis_text) that fuzzy is too weak
        for belong in the ``key_aliases`` config."""
        alias = self._key_aliases.get(key)
        if alias and alias in allowed:
            return alias
        close = get_close_matches(key, allowed, n=1, cutoff=0.85)
        return close[0] if close else None

    def _normalize_keys(self, value: Any, model: Any, path: str,
                        remaps: List[str], violations: List[str]) -> Any:
        """Return a copy of ``value`` with unknown keys remapped to the closest
        allowed key (recording each remap); keys that can't be mapped are left
        in place and recorded as violations (with a 'did you mean' hint).

        Model semantics: dict = allowed keys at this level ("*" = any key,
        models the children); empty dict = free subtree; a string node = a leaf
        TYPE constraint (e.g. "string", "array"); a one-element LIST node
        (``[elem_model]``) asserts the value is an array and models each element;
        other scalars are free."""
        if isinstance(model, str):
            # Leaf type constraint — the value carries no keys to model further.
            self._check_type(value, model, path, violations)
            return value
        if isinstance(model, list):
            # Array constraint: the value must be a list; each element is modeled
            # by model[0] (or {} = free). Asserts array-ness so a bare object is
            # rejected instead of silently passing as a single element.
            if value is None:
                return value
            if not isinstance(value, list):
                violations.append(f"'{path}' must be array but is "
                                  f"{self._json_type_name(value)}")
                return value
            elem_model = model[0] if model else {}
            return [self._normalize_keys(v, elem_model, f"{path}[{i}]",
                                         remaps, violations)
                    for i, v in enumerate(value)]
        if not isinstance(model, dict) or not model:
            return value
        if isinstance(value, list):
            return [self._normalize_keys(v, model, f"{path}[{i}]", remaps, violations)
                    for i, v in enumerate(value)]
        if not isinstance(value, dict):
            return value
        wildcard = model.get("*")
        allowed = [k for k in model if k != "*"]
        result: Dict[str, Any] = {}
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key in model:
                result[key] = self._normalize_keys(child, model[key], child_path,
                                                   remaps, violations)
            elif wildcard is not None:
                result[key] = self._normalize_keys(child, wildcard, child_path,
                                                   remaps, violations)
            else:
                target = self._resolve_key(key, allowed)
                if target and target not in value and target not in result:
                    remaps.append(f"{child_path} → {target}")
                    tgt_path = f"{path}.{target}" if path else target
                    result[target] = self._normalize_keys(child, model[target], tgt_path,
                                                           remaps, violations)
                else:
                    # Not auto-remapped (no confident match, or the target key is
                    # already present). Suggest the nearest allowed key loosely so
                    # the agent knows how to correct it.
                    suggest = target or next(
                        iter(get_close_matches(key, allowed, n=1, cutoff=0.5)), None)
                    hint = (f" (did you mean '{suggest}'?)" if suggest
                            else f" (allowed: {allowed})")
                    violations.append(f"'{child_path}'{hint}")
                    result[key] = child  # keep so the doc is rejected, not silently dropped
        return result

    def _apply_key_model(self, doc_name: str, value: Any) -> Tuple[Any, List[str], List[str]]:
        """(normalized_value, remaps, violations) for ``doc_name``'s key model.
        A ``"*"`` entry is a default model applied to any doc without its own —
        so auto-id documents (a writer's unnamed delta) are validated against the
        same schema at write time, not only when merged into a named target."""
        model = self._key_models.get(doc_name)
        if model is None:
            model = self._key_models.get("*")
        if not model:
            return value, [], []
        remaps: List[str] = []
        violations: List[str] = []
        normalized = self._normalize_keys(value, model, "", remaps, violations)
        return normalized, remaps, violations

    # ------------------------------------------------------------------
    # Result / storage helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False))

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2)

    @staticmethod
    def _top_level(value: Any) -> Any:
        if isinstance(value, dict):
            keys = list(value.keys())
            return keys[:15] + ([f"+{len(keys) - 15} more"] if len(keys) > 15 else [])
        if isinstance(value, list):
            return f"array[{len(value)}]"
        return type(value).__name__

    def _summary(self, name: str, value: Any, size: int, **extra: Any) -> Dict[str, Any]:
        return {"status": "ok", "doc": name, "chars": size,
                "top_level": self._top_level(value), **extra}

    def _require_doc(self, params: Dict[str, Any]):
        """Common guard: 'doc' present and existing. Returns (bucket, name, error)."""
        name = (params.get("doc") or "").strip()
        bucket = self._bucket(params)
        if not name:
            return None, None, {"status": "error", "error": "'doc' (document name) is required"}
        if name not in bucket:
            existing = sorted(bucket.keys())
            hint = suggest_path(name, existing)
            shown = existing[:15]
            more = f" (+{len(existing) - 15} more)" if len(existing) > 15 else ""
            return None, None, {
                "status": "error",
                "error": f"Document '{name}' not found."
                         + (f" Did you mean '{hint}'?" if hint else "")
                         + f" Existing: {shown}{more}",
                "did_you_mean": hint,
                "existing": shown}
        return bucket, name, None

    def _key_model_error(self, violations: List[str]) -> Dict[str, Any]:
        shown = violations[:10]
        more = f" (+{len(violations) - 10} more)" if len(violations) > 10 else ""
        return {
            "status": "error",
            "error": f"Key model violation(s): {'; '.join(shown)}{more}. Rename the "
                     f"flagged key to the suggested/allowed one and use the required "
                     f"type — do not invent fields or change types.",
        }

    def _commit(self, params: Dict[str, Any], name: str, value: Any,
                op: str = "write", **extra: Any) -> Dict[str, Any]:
        """Single gate for every store: write protection, max_docs (new docs),
        size limit and key model are enforced here, and a failed check never
        touches the stored document (callers pass fully-built candidate values).
        ``op`` labels the mutation for the undo history."""
        bucket = self._bucket(params)
        denied = self._write_guard(params, name)
        if denied:
            return denied
        created = name not in bucket
        if created and len(bucket) >= self._max_docs:
            return {"status": "error",
                    "error": f"Too many documents ({self._max_docs}). Delete unused ones."}
        access, access_error = (self._resolve_write_access(params)
                                if created else (None, None))
        if access_error:
            return {"status": "error", "error": access_error}
        # Key model: auto-remap known/typo'd keys, reject the rest.
        value, remaps, violations = self._apply_key_model(name, value)
        if violations:
            return self._key_model_error(violations)
        size = self._size(value)
        if size > self._max_doc_bytes:
            return {"status": "error",
                    "error": f"Document too large ({size} chars > limit "
                             f"{self._max_doc_bytes})."}
        # All checks passed — snapshot the prior state, then apply. Skip the
        # snapshot for a no-op (replace/merge/set_value that yields the same
        # value), which would only erode the bounded undo depth.
        if created or bucket.get(name) != value:
            self._snapshot(params, name, op)
        if created:
            self._register_owner(params, name, access)
        bucket[name] = value
        if remaps:
            extra["remapped"] = remaps
        persist_error = self._persist_doc(params, name)
        if persist_error:
            extra["persist_error"] = persist_error
        return self._summary(name, value, size, **extra)

    @classmethod
    def _outline(cls, value: Any, depth: int) -> Any:
        if isinstance(value, dict):
            if depth <= 0:
                return f"object({len(value)} keys)"
            return {k: cls._outline(v, depth - 1) for k, v in value.items()}
        if isinstance(value, list):
            if not value:
                return "array(empty)"
            if depth <= 0:
                return f"array({len(value)})"
            return [cls._outline(value[0], depth - 1)] + (
                [f"... +{len(value) - 1} more"] if len(value) > 1 else []
            )
        if isinstance(value, str):
            return f"string({len(value)})"
        return type(value).__name__ if value is not None else "null"

    # ------------------------------------------------------------------
    # Tool entrypoint (single tool, dispatched via 'operation' — keeps the
    # agent's tool list small, same pattern as sub_agent_manager)
    # ------------------------------------------------------------------

    @staticmethod
    def _status_line(operation: str, params: Dict[str, Any],
                     result: Dict[str, Any]) -> str:
        """Human-readable one-liner for the UI's status stream.

        Built centrally so EVERY operation says what it did — a bare
        "completed" leaves the operator guessing which of nine operations on
        which document just finished.
        """
        doc = result.get("doc") or params.get("doc") or "?"
        chars = result.get("chars")
        size = f" ({chars} chars)" if chars is not None else ""

        if operation == "write":
            verb = "replaced" if result.get("replaced") else "wrote"
            return f"{verb} '{doc}'{size}"
        if operation == "read":
            path = result.get("path")
            where = f"'{doc}'.{path}" if path else f"'{doc}'"
            return f"read {where}{size}"
        if operation in ("merge", "merge_doc"):
            keys = result.get("merged_keys") or []
            shown = ", ".join(str(k) for k in keys[:4])
            more = f" +{len(keys) - 4}" if len(keys) > 4 else ""
            detail = f" [{shown}{more}]" if shown else ""
            if operation == "merge_doc":
                head = f"merged '{result.get('merged_from', params.get('source'))}' → '{doc}'"
            else:
                head = f"merged into '{doc}'"
            created = " (created)" if result.get("created") else ""
            return f"{head}{created}{detail}{size}"
        if operation == "set_value":
            return f"set '{doc}'.{result.get('set_path', params.get('path'))}{size}"
        if operation == "delete_keys":
            deleted, missing = result.get("deleted") or [], result.get("missing") or []
            tail = f", {len(missing)} not found" if missing else ""
            return f"deleted {len(deleted)} key(s) from '{doc}'{tail}"
        if operation == "delete_doc":
            return f"deleted doc '{result.get('deleted', doc)}'"
        if operation == "list":
            return f"listed {result.get('count', 0)} doc(s)"
        if operation == "outline":
            return f"outline of '{doc}'"
        if operation == "stats":
            n_terms = len(result.get("recurring_terms", []))
            return (f"stats of '{doc}': {n_terms} recurring term(s) across "
                    f"{result.get('n_children', 0)} children")
        if operation == "undo":
            left = result.get("snapshots_left", 0)
            undone = result.get("undone", "?")
            if result.get("deleted"):
                return f"undid {undone} → deleted '{doc}' ({left} undo(s) left)"
            return f"undid {undone} → '{doc}'{size} ({left} undo(s) left)"
        return f"{operation} '{doc}'"  # pragma: no cover - future operations

    async def manage_json(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch to the operation handlers below."""
        operation = (params.get("operation") or "").strip()
        handlers = {
            "write": self.write,
            "read": self.read,
            "merge": self.merge,
            "merge_doc": self.merge_doc,
            "set_value": self.set_value,
            "delete_keys": self.delete_keys,
            "delete_doc": self.delete_doc,
            "undo": self.undo,
            "list": self.list_docs,
            "outline": self.outline,
            "stats": self.stats,
        }
        handler = handlers.get(operation)
        if not handler:
            return {"status": "error",
                    "error": f"Unknown or missing operation '{operation}'. "
                             f"Valid: {sorted(handlers)}"}

        # Vor dem Dispatch, damit KEINE Operation den privaten Fallback nimmt —
        # ein Lesen im falschen Namespace ist genauso still wie ein Schreiben.
        #
        # ponytail: Erkennung, nicht Verhinderung. Das Tool-Schema (schema.yaml)
        # nennt ``namespace`` weiterhin „optional" und teilt ``required`` mit
        # allen Instanzen — es per Instanz zu verschaerfen braeuchte einen
        # Mechanismus in der MCP-Basis. Der Riegel kostet damit EINE verworfene
        # LLM-Runde statt fuenf; auf Prevention umbauen, wenn eine zweite
        # Instanz denselben Bedarf hat.
        if self._require_namespace and (params.get("namespace") in (None, "")):
            return {
                "status": "error",
                "error": (
                    "'namespace' is required on this store — pass the shared "
                    "store id you were given (the same value on every call). "
                    "Without it your documents land in a private, "
                    "per-session space that nobody else can read."
                ),
            }

        # Another process may have changed this namespace on disk since we last
        # looked; without this its changes were invisible and overwritten here.
        self._sync_namespace(self._ns(params))
        try:
            result = await handler(params)
        except (AttributeError, TypeError, ValueError) as e:
            # A parameter of the wrong JSON type (doc: 5, depth: "abc"): answer
            # like every other refusal, with a terminal status event.
            logger.warning("json_store: %s refused: %r", operation, e)
            result = {"status": "error",
                      "error": f"Invalid parameter for '{operation}': {e}"}

        # Exactly one terminal status event per operation — the handlers never
        # touch _status, so it can neither go missing nor be emitted twice.
        # Failures go through error() (ERROR phase) so the UI shows them as
        # failures, not as a green "completed".
        status = params.get("_status")
        if status is not None and isinstance(result, dict):
            try:
                if result.get("status") == "error":
                    doc = params.get("doc") or result.get("doc") or "?"
                    await status.error(f"{operation} '{doc}' failed: "
                                       f"{str(result.get('error'))[:150]}")
                else:
                    await status.end(self._status_line(operation, params, result))
            except Exception as e:  # pragma: no cover - status is best-effort
                logger.debug("json_store: status event failed: %s", e)
        return result

    # ------------------------------------------------------------------
    # Operation handlers
    # ------------------------------------------------------------------

    async def write(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Create a document. Give ``doc`` to name it yourself, or OMIT it to get
        a fresh collision-free id back (best for parallel writers — each gets its
        own document with no name coordination). ``if_exists`` ('error' default —
        catch a name collision / 'replace' — overwrite on purpose) applies only to
        a named doc; an auto-id is always a fresh create."""
        name = (params.get("doc") or "").strip()
        bucket = self._bucket(params)
        if name:
            # Ownership first: otherwise a foreign doc answers "already exists,
            # use if_exists='replace'" — an invitation to retry that only then
            # hits the owner check. One wasted turn and a misleading hint.
            denied = self._write_guard(params, name)
            if denied:
                return denied
            if_exists = (params.get("if_exists") or "error").lower()
            if if_exists not in ("error", "replace"):
                return {"status": "error", "error": "if_exists must be 'error' or 'replace'"}
            exists = name in bucket
            if exists and if_exists == "error":
                return {"status": "error",
                        "error": f"Document '{name}' already exists. Use a unique doc id, "
                                 f"or if_exists='replace' to overwrite on purpose."}
        else:
            # No name given: mint a fresh collision-free id so parallel writers
            # never clobber each other. The caller returns this id to whoever
            # merges it (coordinator / panel moderator).
            name = self._new_doc_id(bucket)
            exists = False
        try:
            value, repairs = self._extract_payload(params)
        except ValueError as e:
            return {"status": "error", "error": str(e)}

        return self._commit(params, name, value, op="write", replaced=exists,
                            **({"repairs": repairs} if repairs else {}))

    async def read(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Read a document (whole or sub-path) as canonical JSON."""
        bucket, name, err = self._require_doc(params)
        if err:
            return err
        value = bucket[name]
        path = (params.get("path") or "").strip()
        try:
            if path:
                value = self._resolve(value, self._split_path(path))
        except ValueError as e:
            return {"status": "error", "error": str(e)}
        text = self._canonical(value)
        if len(text) > self._max_read_chars:
            return {"status": "error",
                    "error": f"'{name}'{'.' + path if path else ''} is {len(text)} "
                             f"chars, over the read limit of {self._max_read_chars}. "
                             f"Read a part with 'path'; 'outline' shows the structure."}
        return {"status": "ok", "doc": name, "path": path or None,
                "chars": len(text), "json": text}

    async def merge(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Deep-merge an object into a document (dicts recurse, scalars overwrite)."""
        name = (params.get("doc") or "").strip()
        if not name:
            return {"status": "error", "error": "'doc' (document name) is required"}
        denied = self._write_guard(params, name)
        if denied:
            return denied
        array_mode = params.get("array_mode") or "replace"
        if array_mode not in ("replace", "concat"):
            return {"status": "error", "error": "array_mode must be 'replace' or 'concat'"}
        try:
            incoming, repairs = self._extract_payload(params)
        except ValueError as e:
            return {"status": "error", "error": str(e)}

        bucket = self._bucket(params)
        existed = name in bucket
        base = bucket.get(name)
        # Root-type mismatch (object vs non-object, either direction) would
        # silently discard the whole document — force an explicit write instead.
        if existed and isinstance(base, dict) != isinstance(incoming, dict):
            return {"status": "error",
                    "error": f"Root type mismatch: document '{name}' is "
                             f"{type(base).__name__}, incoming is "
                             f"{type(incoming).__name__} — use write to replace."}
        merged = self._deep_merge(base, incoming, array_mode) if existed else incoming
        merged_keys = list(incoming.keys()) if isinstance(incoming, dict) else None
        return self._commit(params, name, merged, op="merge", created=not existed,
                            merged_keys=merged_keys,
                            **({"repairs": repairs} if repairs else {}))

    async def merge_doc(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Deep-merge one stored document into another (``source`` -> ``doc``),
        entirely in code — the moderator/coordinator never re-types the JSON.

        Same semantics as merge (dicts recurse, scalars/new keys overwrite,
        arrays replace or concat). The source is deep-copied so the two stored
        documents never share references afterwards. Both docs are in the same
        namespace (pass ``namespace`` to share across sessions)."""
        name = (params.get("doc") or "").strip()          # target
        source = (params.get("source") or "").strip()
        if not name or not source:
            return {"status": "error",
                    "error": "'doc' (target) and 'source' document names are required"}
        if name == source:
            return {"status": "error", "error": "source and target must differ"}
        denied = self._write_guard(params, name)   # target only; source is read-only
        if denied:
            return denied
        array_mode = params.get("array_mode") or "replace"
        if array_mode not in ("replace", "concat"):
            return {"status": "error", "error": "array_mode must be 'replace' or 'concat'"}

        bucket = self._bucket(params)
        if source not in bucket:
            existing = sorted(bucket.keys())
            hint = suggest_path(source, existing)
            return {"status": "error",
                    "error": f"Source document '{source}' not found."
                             + (f" Did you mean '{hint}'?" if hint else "")
                             + f" Existing: {existing[:15]}",
                    "did_you_mean": hint,
                    "existing": existing[:15]}
        incoming = copy.deepcopy(bucket[source])  # decouple the two stored docs
        existed = name in bucket
        base = bucket.get(name)
        if existed and isinstance(base, dict) != isinstance(incoming, dict):
            return {"status": "error",
                    "error": f"Root type mismatch: target '{name}' is "
                             f"{type(base).__name__}, source '{source}' is "
                             f"{type(incoming).__name__} — use write to replace."}
        merged = self._deep_merge(base, incoming, array_mode) if existed else incoming
        merged_keys = list(incoming.keys()) if isinstance(incoming, dict) else None
        return self._commit(params, name, merged, op="merge_doc", created=not existed,
                            merged_from=source, merged_keys=merged_keys)

    async def set_value(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Set one value at a path; missing intermediates are created
        (objects for name segments, arrays for ``[i]`` segments)."""
        name = (params.get("doc") or "").strip()
        path = (params.get("path") or "").strip()
        if not name or not path:
            return {"status": "error", "error": "'doc' and 'path' are required"}
        if "value" not in params:
            return {"status": "error", "error": "'value' is required"}
        denied = self._write_guard(params, name)
        if denied:
            return denied
        value = params["value"]

        bucket = self._bucket(params)
        # Work on a copy so a failed check leaves the stored document untouched.
        doc = copy.deepcopy(bucket[name]) if name in bucket else {}
        try:
            segments = self._split_path(path)
            parent: Any = doc
            for i, seg in enumerate(segments[:-1]):
                nxt = segments[i + 1]
                container: Any = [] if isinstance(nxt, int) else {}
                if isinstance(parent, list):
                    idx = self._as_index(seg)
                    if idx is None:
                        raise ValueError(f"'{seg}' is not a list index")
                    if idx == len(parent):
                        parent.append(container)
                    elif not (0 <= idx < len(parent)):
                        raise ValueError(f"List index {idx} out of range (length {len(parent)})")
                    parent = parent[idx]
                else:
                    if isinstance(seg, int):
                        # an int key would be stored as-is: unreadable by path, "0" after a restart
                        raise ValueError(f"Path expects an array at '[{seg}]' but found an object")
                    if seg not in parent or not isinstance(parent[seg], (dict, list)):
                        parent[seg] = container
                    parent = parent[seg]
                if not isinstance(parent, (dict, list)):
                    raise ValueError(f"Cannot descend into {type(parent).__name__} before '{nxt}'")
            last = segments[-1]
            if isinstance(parent, list):
                idx = self._as_index(last)
                if idx is None:
                    raise ValueError(f"'{last}' is not a list index")
                if idx == len(parent):
                    parent.append(value)
                elif 0 <= idx < len(parent):
                    parent[idx] = value
                else:
                    raise ValueError(f"List index {idx} out of range (length {len(parent)})")
            elif isinstance(last, int):
                raise ValueError(f"Path expects an array at '[{last}]' but found an object")
            else:
                parent[last] = value
        except (ValueError, IndexError, KeyError, TypeError) as e:
            return {"status": "error", "error": f"set_value failed at path '{path}': {e}"}
        return self._commit(params, name, doc, op="set_value", set_path=path)

    async def delete_keys(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete one or more paths from a document. Each path is applied
        independently; unknown paths are reported in 'missing'."""
        paths = params.get("paths") or []
        if not paths or not isinstance(paths, list):
            return {"status": "error", "error": "non-empty 'paths' list is required"}
        bucket, name, err = self._require_doc(params)
        if err:
            return err
        denied = self._write_guard(params, name)
        if denied:
            return denied
        self._snapshot(params, name, "delete_keys")   # before in-place deletion
        deleted, missing = [], []
        for path in paths:
            try:
                segments = self._split_path(str(path))
                parent = self._resolve(bucket[name], segments[:-1]) if len(segments) > 1 \
                    else bucket[name]
                last = segments[-1]
                if isinstance(parent, list):
                    idx = self._as_index(last)
                    if idx is None or not (0 <= idx < len(parent)):
                        raise ValueError("not found")
                    parent.pop(idx)
                elif isinstance(parent, dict) and last in parent:
                    del parent[last]
                else:
                    raise ValueError("not found")
                deleted.append(path)
            except (ValueError, IndexError, KeyError, TypeError):
                missing.append(path)
        extra: Dict[str, Any] = {}
        if deleted:
            persist_error = self._persist_doc(params, name)
            if persist_error:
                extra["persist_error"] = persist_error
        else:
            self._pop_snapshot(params, name)   # nothing changed → no-op snapshot
        return self._summary(name, bucket[name], self._size(bucket[name]),
                             deleted=deleted, missing=missing, **extra)

    async def delete_doc(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete a whole document."""
        bucket, name, err = self._require_doc(params)
        if err:
            return err
        denied = self._write_guard(params, name)
        if denied:
            return denied
        self._snapshot(params, name, "delete_doc")   # so undo can recreate it
        del bucket[name]
        self._owners(params).pop(name, None)
        result = {"status": "ok", "deleted": name, "remaining": list(bucket.keys())}
        persist_error = self._unpersist_doc(params, name)
        if persist_error:
            result["persist_error"] = persist_error
        return result

    async def undo(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Revert the LAST mutation on a document (repeatable up to the history
        depth). Parameter-free by design — the agent never has to count how many
        operations to reverse; call it again to step further back. Reverts write/
        replace/merge/merge_doc/set_value/delete_keys/delete_doc; undoing the
        operation that created a document deletes it again. Owner-guarded like
        any mutation. This is the recovery path for a mistake, so the agent
        doesn't have to reconstruct the previous JSON by hand."""
        name = (params.get("doc") or "").strip()
        if not name:
            return {"status": "error", "error": "'doc' (document name) is required"}
        if self._undo_depth <= 0:
            return {"status": "error", "error": "undo is disabled for this store"}
        stack = self._history(params).get(name)
        if not stack:
            return {"status": "error",
                    "error": f"Nothing to undo for '{name}' (no recorded history)."}

        bucket = self._bucket(params)
        top = stack[-1]
        # Authorize against the current owner, or the snapshot's owner when the
        # document was deleted (so only its owner may undo the deletion).
        owner = self._owners(params).get(name, top.get("owner"))
        if owner is not None and owner != self._caller(params):
            return {"status": "error",
                    "error": f"Document '{name}' belongs to another agent — you "
                             f"cannot undo changes to it."}

        snap = stack.pop()
        if not stack:
            self._history(params).pop(name, None)   # don't leave an empty deque
        owners = self._owners(params)
        if snap["existed"]:
            bucket[name] = snap["value"]
            owners[name] = snap["owner"]
            size = self._size(snap["value"])
            result = {"status": "ok", "doc": name, "undone": snap["op"],
                      "restored": True, "chars": size,
                      "top_level": self._top_level(snap["value"]),
                      "snapshots_left": len(stack)}
            persist_error = self._persist_doc(params, name)
            if persist_error:
                result["persist_error"] = persist_error
            return result
        # The undone operation had created the document → remove it again.
        bucket.pop(name, None)
        owners.pop(name, None)
        result = {"status": "ok", "doc": name, "undone": snap["op"],
                  "restored": False, "deleted": True,
                  "snapshots_left": len(stack)}
        persist_error = self._unpersist_doc(params, name)
        if persist_error:
            result["persist_error"] = persist_error
        return result

    async def list_docs(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """List the caller's documents with size and top-level keys."""
        bucket = self._bucket(params)
        owners = self._owners(params)
        caller = self._caller(params)
        docs = [{"doc": name, "chars": self._size(value),
                 "writable": owners.get(name) in (None, caller),
                 "top_level": self._top_level(value)}
                for name, value in bucket.items()]
        return {"status": "ok", "count": len(docs), "docs": docs}

    async def outline(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Structure overview (keys + types, no values) — cheap inspection."""
        bucket, name, err = self._require_doc(params)
        if err:
            return err
        depth_param = params.get("depth")
        depth = 3 if depth_param is None else max(0, int(depth_param))
        outline = self._outline(bucket[name], depth)
        size = len(self._canonical(outline))
        if size > self._max_read_chars:
            return {"status": "error",
                    "error": f"Outline of '{name}' at depth {depth} is {size} chars, "
                             f"over the read limit of {self._max_read_chars}. "
                             f"Use a smaller 'depth'."}
        return {"status": "ok", "doc": name, "outline": outline}

    #: Haeufige deutsche/englische Funktionswoerter (>=4 Zeichen), die als
    #: Sättigungs-Signal wertlos sind. Bewusst klein — perfekte Filterung ist
    #: nicht noetig, der Konsument (LLM) ignoriert Restrauschen selbst.
    _STATS_STOPWORDS = frozenset((
        "aber auch beim dann dass dein deine dem den einer einem einen eines "
        "eine doch dort durch fast hier ihre ihrem ihren ihrer mehr nach nicht "
        "noch nur ohne schon sein seine seinem seinen seiner sich sind ueber "
        "unter viel wieder wird wurde zwei zum zur als wenn weil wie was wer "
        "the and with from that this have will into over "
        # erzaehlagnostische Allerwelts-Verben/-Woerter — als Saettigungs-
        # Signal wertlos, verstopfen sonst die Top-Slots
        "kommt sitzt liegt steht geht sagt sieht legt nimmt macht bleibt "
        "beginnt haelt laesst zeigt spuert wirkt traegt bringt erste ersten "
        "zurueck davor danach dabei etwas nichts alles beide diesem dieser "
        "dieses jetzt heute leser kapitel szene beat "
        # native Umlaut-Formen (der Tokenizer ist Unicode-aware, .lower()
        # transliteriert NICHT — beide Schreibweisen abdecken)
        "über während möchte hält lässt spürt trägt zurück wäre könnte "
        "müsste hätte würde später früher nächste nächsten für"
    ).split())

    async def stats(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Per-Kind-Wiederkehr von Begriffen ueber einen Doc-Teilbaum.

        Deterministische Saettigungs-Analyse (docs/prompt_cache_design.md
        verwandt; primaer fuer writer O9c): fuer jedes Kind unter ``path``
        (dict-Werte oder Listen-Elemente) werden alle String-Werte rekursiv
        eingesammelt, in Woerter (>=4 Zeichen, lowercase) und Wort-Bigramme
        zerlegt, und pro Kind als MENGE gezaehlt. Ergebnis: Begriffe, die in
        >= ``min_children`` Kindern vorkommen — d.h. wiederkehrende
        Requisiten/Phrasen/Motive, nicht blosse Haeufigkeit in einem Kind.

        Params: doc (Pflicht), path (z.B. "beats"; leer = ganzes data),
        min_children (Default 2), top (Default 12).
        """
        bucket, name, err = self._require_doc(params)
        if err:
            return err
        node: Any = bucket[name]
        path = (params.get("path") or "").strip()
        if path:
            # Gleiche Pfad-Syntax wie read/set_value (inkl. [i]-Array-Index)
            try:
                node = self._resolve(node, self._split_path(path))
            except ValueError as e:
                # _resolve names the failing segment AND the available keys —
                # replacing that with a generic line forced a second read turn.
                return {"status": "error",
                        "error": f"path '{path}' in doc '{name}': {e}"}
            except (KeyError, IndexError, TypeError):
                return {"status": "error",
                        "error": f"path '{path}' not found in doc '{name}'"}
        if isinstance(node, dict):
            children = list(node.values())
        elif isinstance(node, list):
            children = node
        else:
            return {"status": "error",
                    "error": f"path '{path or '.'}' is not a dict/list"}
        try:
            min_children = max(2, int(params.get("min_children") or 2))
            top = max(1, int(params.get("top") or 12))
        except (TypeError, ValueError):
            return {"status": "error",
                    "error": "min_children/top must be integers"}
        # exclude: erwartbar-haeufige Begriffe (Figuren-/Ortsnamen) rausfiltern,
        # damit die Top-Slots den echten Saettigungs-Signalen gehoeren.
        # Leere/Kurzst-Eintraege fallen raus (Substring-Match: "" traefe ALLES,
        # 1-2 Zeichen wuerden massiv ueberfiltern); Nicht-Listen-Skalare sind
        # ein Param-Fehler, kein Crash.
        exclude_raw = params.get("exclude") or []
        if isinstance(exclude_raw, str):
            exclude_raw = exclude_raw.split(",")
        if not isinstance(exclude_raw, list):
            return {"status": "error",
                    "error": "exclude must be a list of strings (or comma-string)"}
        exclude = [s for s in (str(x).strip().lower() for x in exclude_raw)
                   if len(s) >= 3]

        def collect_strings(obj: Any, out: List[str]) -> None:
            if isinstance(obj, str):
                out.append(obj)
            elif isinstance(obj, dict):
                for v in obj.values():
                    collect_strings(v, out)
            elif isinstance(obj, list):
                for v in obj:
                    collect_strings(v, out)

        import re as _re
        term_children: Dict[str, int] = {}
        for child in children:
            texts: List[str] = []
            collect_strings(child, texts)
            words = [w for w in _re.findall(r"[^\W\d_]{4,}", " ".join(texts).lower())
                     if w not in self._STATS_STOPWORDS]
            terms = set(words)
            terms.update(f"{a} {b}" for a, b in zip(words, words[1:]))
            for t in terms:
                term_children[t] = term_children.get(t, 0) + 1
        recurring = [
            {"term": t, "children": c}
            for t, c in term_children.items()
            if c >= min_children and not any(x in t for x in exclude)
        ]
        # Bigramme vor ihren Teil-Wörtern bevorzugen: gleiche Zählung ->
        # das spezifischere Bigramm behalten, Teilwort unterdruecken.
        # (Index-Lookup statt Rescan — linear statt O(n^2).)
        by_count = sorted(recurring, key=lambda x: (-x["children"], -len(x["term"])))
        count_by_term = {e["term"]: e["children"] for e in by_count}
        suppressed: set = set()
        for entry in by_count:
            if " " in entry["term"]:
                for part in entry["term"].split(" "):
                    if count_by_term.get(part) == entry["children"]:
                        suppressed.add(part)
        result = [e for e in by_count if e["term"] not in suppressed][:top]
        return {"status": "ok", "doc": name, "path": path or ".",
                "n_children": len(children), "min_children": min_children,
                "recurring_terms": result}
