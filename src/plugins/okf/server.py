"""OKF tool server — tools + consumer hook for the Open Knowledge Format.

Thin IO/dispatch layer over :mod:`plugins.okf.core` (which owns the format).
The server adds: a sandboxed filesystem (reuses the file_ops ``allowed_directories``
pattern), bundle loading from disk, the tool surface, and the opt-in
``pre_llm_call`` consumer hook that folds a bundle into agent context
(graph-anchored + lexical retrieval).

Retrieval note: ``okf_search`` and the hook rank concepts by a lexical relevance
score today. Embedding/semantic retrieval is a documented seam (``_rank_lexical``
is the single ranking entry point) — deliberately NOT a rushed second ChromaDB
consumer (file_ops disables its own semantic search to avoid ChromaDB conflicts).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import os
import re
import threading
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from filelock import FileLock, Timeout

from agent_system.paths import data_path, resolve_data_path
from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.utils.path_sandbox import remote_outside
from agent_system.utils.suggest import siblings_of, suggest_path

from . import core

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig
    from agent_system.hooks import HookContext, HookResult

logger = logging.getLogger(__name__)

# Unicode word characters: an ASCII class cut "Straße" into "stra" and "e".
_WORD_RE = re.compile(r"\w+")


def _tokens(text: str) -> List[str]:
    return [t.lower() for t in _WORD_RE.findall(text or "")]


class _NullStatus:
    """Stand-in when a caller supplies no ``_status``.

    Status reporting is an addition to this server, not a contract change:
    the tools are also driven directly (tests, the consumer hook, scripts).
    Swallowing the calls here keeps those callers working instead of forcing
    every one of them to pass a status object.
    """

    async def progress(self, message: str, meta: Optional[Dict[str, Any]] = None) -> None:
        pass

    async def end(self, message: str = "completed", meta: Optional[Dict[str, Any]] = None) -> None:
        pass

    async def error(self, message: str, meta: Optional[Dict[str, Any]] = None) -> None:
        pass


_NULL_STATUS = _NullStatus()


def _valid(shape: str, value: str, kind: Any) -> bool:
    """``value`` has the shape AND names a real date/time (no 2026-13-45)."""
    if not re.match(shape, value):
        return False
    try:
        kind.fromisoformat(value)
    except ValueError:
        return False
    return True


def _concept_key(path: Any) -> str:
    """A concept path as the bundle keys it: one leading slash.

    read/write accept ``tables/x.md`` too; the graph tools compared it verbatim,
    and subgraph silently dropped such a seed.
    """
    return "/" + str(path or "").lstrip("/")


def _status_of(params: Dict[str, Any]) -> Any:
    """The caller's status reporter, or a no-op one."""
    return params.get("_status") or _NULL_STATUS


def _atomic_write(path: Path, text: str) -> None:
    """Write via tmp-file + replace so a reader never sees a half-written file
    and a crash can't truncate the target (bundles are git-versioned).

    The temp name carries pid and thread id. A FIXED name would be worse than no
    temp file at all under concurrency: two writers would interleave their bytes
    into the SAME scratch file and then both rename it over the target — a
    corrupted concept instead of a merely lost one. On Windows the second
    ``replace`` can also fail outright while the first writer holds the handle.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        # A failed replace must not leave scratch files lying in the bundle.
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover - best effort
                pass


def _keep_frontmatter(index_path: Path, text: str) -> str:
    """``text`` under the frontmatter the existing index.md carries, if any.

    The root index declares ``okf_version`` there; a regenerated body alone
    dropped it on every reindex.
    """
    try:
        old = index_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):  # missing or broken: nothing to keep
        return text
    fm, _body, _err = core.parse_frontmatter(old)
    return core.dump_frontmatter(fm, text) if fm else text


#: Most concepts one list answer carries; the rest is counted, not listed.
LIST_LIMIT = 200


#: Cross-process guard file, one per bundle root. It lives IN the bundle on
#: purpose: services run under different accounts (agent-api, writer worker,
#: a developer's CLI), and their temp directories differ — a lock outside the
#: bundle would silently fail to be the same lock.
LOCK_FILENAME = ".okf.lock"

#: Long enough that a slow bundle walk never trips it, short enough that a
#: genuinely wedged holder surfaces as an error instead of hanging the turn.
LOCK_TIMEOUT_S = 30.0

#: In-process serialization, keyed by resolved bundle root. Module-level, not
#: per-server: a parent agent and the sub-agent it spawned may hold different
#: OkfServer instances, and both write the same bundle.
#:
#: A ``threading.Lock``, deliberately NOT an ``asyncio.Lock``: the latter binds
#: itself to the event loop of its first use and raises "bound to a different
#: event loop" on the next one. Two sequential ``asyncio.run()`` calls in one
#: process are enough to trip that, and the bundle path is stable in production
#: so the same lock object is reached again. This one has no loop affinity, and
#: the critical section already runs off the loop anyway.
_bundle_locks: Dict[str, threading.Lock] = {}
_bundle_locks_guard = threading.Lock()


def _lock_for(root: Path) -> threading.Lock:
    with _bundle_locks_guard:
        key = str(root)
        lock = _bundle_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _bundle_locks[key] = lock
        return lock


def _run_locked(root: Path, fn: Callable[[], Any]) -> Any:
    """Run ``fn`` holding both guards, on one thread.

    filelock's bookkeeping is per-thread, so acquire and release must not land
    on different pool threads — hence one call around the whole critical section
    instead of separate awaits for acquire and release.
    """
    root.mkdir(parents=True, exist_ok=True)
    with _lock_for(root):
        with FileLock(str(root / LOCK_FILENAME), timeout=LOCK_TIMEOUT_S):
            return fn()


async def _exclusive(root: Path, fn: Callable[[], Any]) -> Any:
    """Run a read-modify-write on ``root`` serialized against every other writer.

    Both guards are needed and neither is redundant:

    * the **in-process lock** covers this process — parallel tool calls of a
      single turn and spawned sub-agents all run as tasks on one event loop, so
      today's safety rests only on there being no ``await`` between the read and
      the write. Measured: insert one, and 11 of 12 log entries vanish.
    * the **file lock** covers other processes — agent-api, the writer worker
      and a developer's CLI share ``data/okf``. Measured: two processes
      appending concurrently lose half the entries (and on Windows ``os.replace``
      raises outright while the other holds the file).

    ``fn`` is synchronous and does the whole read-modify-write; it runs off the
    event loop, so waiting never stalls other turns.
    """
    return await asyncio.to_thread(_run_locked, root, fn)


class OkfServer(SchemaBasedToolServer):
    """Sandboxed read/write/validate/graph/search over OKF bundles, plus a
    context-injection hook."""

    def __init__(self, name: str, system_config: "AgentSystemConfig",
                 server_config: "ToolServerConfig") -> None:
        super().__init__(name, system_config, server_config)

        # Config may arrive either as top-level plugin keys (file_ops style,
        # via ToolServerConfig extra="allow") or under a 'config:' sub-block
        # (json_store style). Merge both, the explicit sub-block winning.
        top_level = getattr(server_config, "model_extra", None) or {}
        sub = getattr(server_config, "config", None) or {}
        cfg = {**top_level, **sub}

        # Sandbox roots — same contract as file_ops: relative paths resolve
        # against the project root; every bundle/concept path a tool touches
        # MUST resolve inside one of these.
        allowed = cfg.get("allowed_directories") or [str(data_path("okf"))]
        project_root = Path.cwd()
        self._roots: List[Path] = []
        for d in allowed:
            p = Path(d)
            self._roots.append(p.resolve() if p.is_absolute()
                               else (project_root / p).resolve())

        # Carve-outs inside the roots: a bundle another instance owns. Resolved
        # like the roots. A bundle inside one is refused, and so is a bundle
        # that contains one -- its concept paths and reindex would reach in.
        self._excluded: List[Path] = []
        for d in cfg.get("excluded_directories") or []:
            p = Path(d)
            self._excluded.append(p.resolve() if p.is_absolute()
                                  else (project_root / p).resolve())

        self._read_only: bool = bool(cfg.get("read_only", False))
        # Resource bounds — a bundle is walked/read fully per call, so cap it.
        self._max_file_kb: int = int(cfg.get("max_concept_file_kb", 512))
        self._max_files: int = int(cfg.get("max_bundle_files", 5000))

        # Hook config DEFAULTS (per-agent overrides read from context.hook_config
        # in on_pre_llm_call — see there). Opt-in; enabled per agent via
        # hooks.overrides.
        self._hook_bundle: Optional[str] = cfg.get("hook_bundle")  # bundle path
        self._hook_max_concepts: int = int(cfg.get("hook_max_concepts", 6))
        self._hook_graph_depth: int = int(cfg.get("hook_graph_depth", 1))
        self._hook_seed_count: int = int(cfg.get("hook_seed_count", 3))
        self._hook_seed: Optional[str] = cfg.get("hook_seed_concept")  # optional anchor

        # Loaded-bundle cache keyed by root -> (signature, Bundle). The signature
        # (every file's path, mtime and size) lets repeated tool calls and the per-turn
        # hook skip re-walking an unchanged bundle.
        self._cache: Dict[str, Any] = {}

        logger.info("OkfServer '%s' initialized: %d sandbox root(s), read_only=%s",
                    name, len(self._roots), self._read_only)

    def get_template_vars(self) -> Dict[str, Any]:
        v = super().get_template_vars()
        v["read_only"] = self._read_only
        return v

    # ------------------------------------------------------------------
    # Sandbox path resolution
    # ------------------------------------------------------------------

    def _resolve_bundle(self, bundle: str) -> Path:
        """Resolve a bundle root path and assert it is inside the sandbox.
        Raises ValueError with an agent-actionable message otherwise."""
        if not bundle:
            raise ValueError("missing 'bundle' path")
        # A host path would be opened by resolve() below -- a connection to the
        # host with the user's credentials -- before containment could refuse it.
        if remote_outside(str(bundle), Path.cwd(), self._roots):
            raise ValueError(
                f"bundle '{bundle}' is outside the allowed OKF directories")
        # data/... lands in the data directory (agent_system/paths.py)
        p = resolve_data_path(bundle)
        p = p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()
        if any(p == ex or ex in p.parents or p in ex.parents for ex in self._excluded):
            logger.warning("okf: rejected bundle %r overlapping an excluded directory", bundle)
            raise ValueError(
                f"bundle '{bundle}' is outside the allowed OKF directories")
        for root in self._roots:
            if p == root or root in p.parents:
                return p
        # Don't echo the server's absolute install paths to the LLM/transcript;
        # log them server-side only.
        logger.warning("okf: rejected out-of-sandbox bundle %r (roots=%s)",
                       bundle, [str(r) for r in self._roots])
        raise ValueError(
            f"bundle '{bundle}' is outside the allowed OKF directories")

    def _resolve_concept(self, bundle_root: Path, rel_path: str) -> Path:
        """Resolve a bundle-relative concept path (``/tables/x.md``) to an
        absolute path inside the bundle. Rejects traversal outside the bundle."""
        rel = (rel_path or "").lstrip("/")
        if not rel:
            raise ValueError("missing concept 'path'")
        if remote_outside(rel, bundle_root, (bundle_root,)):
            raise ValueError(f"concept path '{rel_path}' escapes the bundle")
        p = (bundle_root / rel).resolve()
        if p != bundle_root and bundle_root not in p.parents:
            raise ValueError(f"concept path '{rel_path}' escapes the bundle")
        return p

    @staticmethod
    def _resolve_dir(bundle_root: Path, subdir: str) -> Optional[Path]:
        """A bundle subdirectory, or None when it leads out of the bundle."""
        if not subdir:
            return bundle_root
        if remote_outside(subdir, bundle_root, (bundle_root,)):
            return None
        p = (bundle_root / subdir).resolve()
        return p if p == bundle_root or bundle_root in p.parents else None

    def _bundle_rel(self, bundle_root: Path, abs_path: Path) -> str:
        """Absolute path -> bundle-relative leading-slash POSIX path."""
        return "/" + abs_path.relative_to(bundle_root).as_posix()

    # ------------------------------------------------------------------
    # Bundle loading
    # ------------------------------------------------------------------

    def _load_bundle(self, bundle_root: Path) -> core.Bundle:
        """Walk a bundle directory and build an in-memory :class:`core.Bundle`,
        cached by every file's (path, mtime, size) so an unchanged bundle isn't
        re-read on every tool call / LLM turn.

        Concepts that fail to parse are still loaded (with their parse error
        recorded) so validation reports the true failure; unreadable files are
        skipped. Files larger than the per-file cap and files that resolve
        OUTSIDE the bundle (symlink escape) are skipped — the bulk path must be
        as sandbox-safe as _resolve_concept, since its content flows into search
        results and the context-injection hook (and thus to the LLM)."""
        mds = sorted(bundle_root.rglob("*.md"))[: self._max_files]
        # Every path with its mtime and size: a rename keeps both the count
        # and the newest mtime, so those two alone served the old bundle.
        try:
            sig = tuple((str(p), st.st_mtime_ns, st.st_size)
                        for p in mds for st in (p.stat(),))
        except OSError:
            sig = None
        key = str(bundle_root)
        if sig is not None:
            cached = self._cache.get(key)
            if cached and cached[0] == sig:
                return cached[1]

        bundle = core.Bundle()
        max_bytes = self._max_file_kb * 1024
        for md in mds:
            rel = self._bundle_rel(bundle_root, md)
            try:
                # Symlink-escape guard: the real file must stay inside the bundle.
                real = md.resolve()
                if real != bundle_root and bundle_root not in real.parents:
                    logger.warning("okf: skipping symlink escaping bundle: %s", rel)
                    continue
                if md.stat().st_size > max_bytes:
                    logger.warning("okf: skipping oversized concept %s (> %d KB)",
                                   rel, self._max_file_kb)
                    continue
                text = md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                logger.warning("okf: skipping unreadable %s: %s", rel, e)
                continue
            if core.is_reserved(rel):
                bundle.reserved[rel] = text
                if md.name == core.INDEX_FILENAME and md.parent == bundle_root:
                    fm, _b, _err = core.parse_frontmatter(text)
                    if fm and isinstance(fm.get("okf_version"), (str, float, int)):
                        bundle.version = str(fm["okf_version"])
                continue
            fm, body, err = core.parse_frontmatter(text)
            bundle.concepts[rel] = core.Concept(
                path=rel, frontmatter=(fm or {}), body=body,
                has_frontmatter=(fm is not None or err is not None),
                parse_error=err)

        if sig is not None:
            self._cache[key] = (sig, bundle)
        return bundle

    # ------------------------------------------------------------------
    # Lexical retrieval (single ranking seam — swap for embeddings later)
    # ------------------------------------------------------------------

    def _rank_lexical(self, bundle: core.Bundle, query: str,
                      limit: int) -> List[Tuple[str, float]]:
        """Rank concepts by TF-IDF-ish lexical relevance to ``query``. Returns
        ``[(path, score), ...]`` best first. This is THE retrieval entry point;
        an embedding-based ranker would replace only this method."""
        q_terms = set(_tokens(query))
        if not q_terms or not bundle.concepts:
            return []
        # Document frequency for IDF weighting.
        docs: Dict[str, Counter] = {}
        df: Counter = Counter()
        for path, c in bundle.concepts.items():
            text = " ".join(filter(None, [
                c.title, c.description, str(c.type or ""),
                " ".join(str(t) for t in (c.frontmatter.get("tags") or [])),
                c.body,
            ]))
            tf = Counter(_tokens(text))
            docs[path] = tf
            for term in set(tf):
                df[term] += 1
        n = len(docs)
        scored: List[Tuple[str, float]] = []
        for path, tf in docs.items():
            score = 0.0
            for term in q_terms:
                if term in tf:
                    idf = math.log(1 + n / (1 + df[term]))
                    score += tf[term] * idf
            if score > 0:
                scored.append((path, score))
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:limit]

    # ------------------------------------------------------------------
    # tools  (schema tool  okf_<op>  ->  method <op>)
    # ------------------------------------------------------------------

    async def validate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Check a bundle against OKF producer conformance (every concept has a
        non-empty ``type``); report broken links as warnings."""
        status = _status_of(params)
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
        except ValueError as e:
            await status.error(str(e))
            return {"status": "error", "error": str(e)}
        bundle = self._load_bundle(root)
        report = core.validate_bundle(bundle)
        d = report.to_dict()
        # to_dict() already reports errors/warnings as COUNTS, not lists.
        await status.end(
            f"{params.get('bundle')}: {len(bundle.concepts)} concept(s), "
            f"{d.get('errors', 0)} error(s), {d.get('warnings', 0)} warning(s)"
            f" — {'conformant' if d.get('conformant') else 'NOT conformant'}"
        )
        return {"status": "ok", "bundle": params.get("bundle"),
                "concepts": len(bundle.concepts), **d}

    async def read_concept(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Return a concept's frontmatter (all keys preserved) and body."""
        status = _status_of(params)
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
            abs_path = self._resolve_concept(root, params.get("path", ""))
        except ValueError as e:
            await status.error(str(e))
            return {"status": "error", "error": str(e)}
        if not abs_path.is_file():
            wanted = str(params.get("path") or "")
            known = list(self._load_bundle(root).concepts)
            hint = suggest_path(wanted, known)
            msg = f"concept not found: {wanted}"
            if hint:
                msg += f" — did you mean '{hint}'?"
            await status.error(msg)
            # Without the listing the agent can only guess again. With it, the
            # "not a typo, it genuinely is not written yet" case is visible too.
            return {"status": "error", "error": msg,
                    "did_you_mean": hint,
                    "available": siblings_of(wanted, known)}
        text = abs_path.read_text(encoding="utf-8")
        fm, body, err = core.parse_frontmatter(text)

        # Paged reading. A page can grow large without hitting the hard cap --
        # one measured at 70 KB (2026-08-06), about 17,000 tokens for ONE call,
        # which floods the context long before anything is cut. Line based.
        all_lines = body.split("\n")
        # A final line break is NOT a line. `split` yields an empty tail for
        # it; counting that reports a phantom line to a reader who has read
        # the whole page -- and the hint sends them after it.
        trailing_newline = bool(all_lines) and all_lines[-1] == ""
        if trailing_newline:
            all_lines = all_lines[:-1]

        start = max(1, int(params.get("start_line") or 1))
        count = params.get("line_count")
        # The schema says minimum: 1, but nothing checks it on this path. A
        # negative value would cut from the end and return some other slice
        # as the one asked for.
        count = max(1, int(count)) if count else None

        if all_lines and start > len(all_lines):
            msg = (f"start_line={start} is past the end: the page has "
                   f"{len(all_lines)} line(s). Nothing read.")
            await status.error(msg)
            return {"status": "error", "error": msg, "path": params.get("path"),
                    "lines_total": len(all_lines), "line_start": start}

        piece = all_lines[start - 1:(start - 1 + count) if count else None]
        rest = len(all_lines) - (start - 1) - len(piece)
        read_text = "\n".join(piece)
        # The final line break belongs to the last slice: `dump_frontmatter`
        # writes the body verbatim, so a read-write round trip would drop it.
        if trailing_newline and piece and rest == 0:
            read_text += "\n"
        # The answer is bounded by the bundle walk's per-file cap; a larger
        # page (a log only grows) stays readable in slices.
        if len(read_text.encode("utf-8")) > self._max_file_kb * 1024:
            msg = (f"the text asked for is larger than {self._max_file_kb} KB "
                   f"— read fewer lines with start_line/line_count")
            await status.error(msg)
            return {"status": "error", "error": msg, "path": params.get("path"),
                    "lines_total": len(all_lines), "line_start": start}

        await status.end(
            f"read {params.get('path')} — {len(text)} chars"
            + (f", lines {start}-{start + len(piece) - 1} of {len(all_lines)}"
               if (count or start > 1) else "")
            + (f", type={fm.get('type')}" if fm and fm.get("type") else "")
            + (" (frontmatter parse error)" if err else "")
        )
        out = {"status": "ok", "path": params.get("path"),
               "frontmatter": dict(fm) if fm else None,
               "body": read_text,
               "parse_error": err,
               "lines_total": len(all_lines),
               "line_start": start,
               "lines_returned": len(piece)}
        # The rest must NOT go missing silently: whoever takes half a page for
        # the whole judges text they never saw.
        if rest > 0:
            out["lines_remaining"] = rest
            out["hint"] = (
                f"{rest} more line(s) — continue with "
                f"start_line={start + len(piece)}. Do NOT write this slice "
                f"back as 'body': that cuts the page to it."
            )
        return out

    async def write_concept(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Write (create/overwrite) a concept. Enforces the format: a non-empty
        ``type`` frontmatter field is required, else the write is rejected.
        Existing extra frontmatter keys are preserved on overwrite."""
        status = _status_of(params)
        if self._read_only:
            await status.error("OKF server is read-only")
            return {"status": "error", "error": "OKF server is read-only"}
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
            abs_path = self._resolve_concept(root, params.get("path", ""))
        except ValueError as e:
            await status.error(str(e))
            return {"status": "error", "error": str(e)}

        # Reserved filenames are NOT concepts (spec MUST) — the producer tool
        # must refuse to write a concept there (use append_log / reindex).
        rel_path = params.get("path", "")
        if core.is_reserved(rel_path):
            msg = (f"'{rel_path}' is a reserved OKF filename "
                   f"(index.md/log.md) — use {self.name}_append_log / {self.name}_reindex")
            await status.error(msg)
            return {"status": "error", "error": msg}
        # Only .md files are concepts: anything else answered ok and was never
        # seen by list, search or validate again.
        if not abs_path.name.endswith(".md"):
            msg = f"concept path '{rel_path}' must end in .md"
            await status.error(msg)
            return {"status": "error", "error": msg}

        frontmatter = params.get("frontmatter")
        # None = not sent: an update of the frontmatter alone keeps the body
        # instead of wiping the page.
        body = params.get("body")
        if not isinstance(frontmatter, dict):
            msg = "'frontmatter' must be an object with at least a 'type'"
            await status.error(msg)
            return {"status": "error", "error": msg}

        def _write() -> Dict[str, Any]:
            # Read, merge and write as ONE step under the bundle lock: a
            # concurrent writer must not slip between the read and the write, or
            # its frontmatter keys are silently dropped on the next overwrite.
            # Overwrite deep-merges the caller's frontmatter INTO the existing
            # one, mutating the parsed CommentedMap so comments/order/quoting
            # and nested producer keys survive (spec: preserve unknown keys).
            existing_fm, existing_body = None, ""
            existed = abs_path.is_file()
            if existed:
                existing_fm, existing_body, _e = core.parse_frontmatter(
                    abs_path.read_text(encoding="utf-8"))
            merged = core.merge_frontmatter(existing_fm, frontmatter)

            text = core.dump_frontmatter(
                merged, existing_body if body is None else str(body))
            findings = core.validate_concept_text(rel_path, text)
            errors = [f for f in findings if f.severity == "error"]
            if errors:
                return {"status": "error",
                        "error": errors[0].message,
                        "findings": [f.__dict__ for f in errors]}

            abs_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(abs_path, text)
            return {"status": "ok", "path": rel_path, "bytes": len(text),
                    "_existed": existed}

        try:
            result = await _exclusive(root, _write)
        except Timeout:
            msg = (f"another writer is holding the OKF bundle lock "
                   f"(> {LOCK_TIMEOUT_S:.0f}s) — try again")
            await status.error(msg)
            return {"status": "error", "error": msg}

        if result["status"] == "error":
            await status.error(f"{rel_path} rejected: {result['error']}")
            return result

        existed = result.pop("_existed")
        await status.end(
            f"{'updated' if existed else 'created'} {rel_path} — {result['bytes']} bytes"
            + (f", type={frontmatter.get('type')}" if frontmatter.get("type") else "")
        )
        return result

    async def list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """List concepts in a bundle (optionally under a subdirectory), with
        type + description — the progressive-disclosure view index.md provides."""
        status = _status_of(params)
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
        except ValueError as e:
            await status.error(str(e))
            return {"status": "error", "error": str(e)}
        subdir = (params.get("dir") or "").strip("/")
        bundle = self._load_bundle(root)
        prefix = "/" + subdir + "/" if subdir else "/"
        # ``lifecycle`` (spec §5.4) belongs in the overview: OKF does not
        # delete, it sets ``deprecated`` ("kept for links and history").
        # Without it a reader would have to open every concept to tell retired
        # knowledge from current -- and quote it as valid until then.
        items = [
            {"path": p, "type": c.type, "title": c.title,
             "description": c.description, "lifecycle": c.lifecycle_status}
            for p, c in sorted(bundle.concepts.items())
            if p.startswith(prefix)
        ]
        total = len(items)
        out = {"status": "ok", "bundle": params.get("bundle"),
               "count": total, "concepts": items[:LIST_LIMIT],
               "version": bundle.version}
        # Bounded: a bundle may hold thousands of concepts.
        if total > LIST_LIMIT:
            out["omitted"] = total - LIST_LIMIT
            out["hint"] = (f"{total - LIST_LIMIT} more concept(s) not listed — "
                           f"narrow with 'dir' or use search.")
        await status.end(
            f"{total} concept(s) under {prefix} in {params.get('bundle')}"
            + (f", first {LIST_LIMIT} listed" if total > LIST_LIMIT else "")
        )
        return out

    async def neighbors(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Return the concepts a given concept links to (its graph neighbors),
        plus any broken links. This is the OKF 'graph, not just tree' surface."""
        status = _status_of(params)
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
        except ValueError as e:
            await status.error(str(e))
            return {"status": "error", "error": str(e)}
        path = _concept_key(params.get("path", ""))
        bundle = self._load_bundle(root)
        if path not in bundle.concepts:
            known = list(bundle.concepts)
            hint = suggest_path(path, known)
            msg = f"concept not found: {path}"
            if hint:
                msg += f" — did you mean '{hint}'?"
            await status.error(msg)
            return {"status": "error", "error": msg,
                    "did_you_mean": hint,
                    "available": siblings_of(path, known)}
        neighbors = bundle.neighbors(path)
        broken = bundle.broken_links(path)
        await status.end(
            f"{path}: {len(neighbors)} neighbor(s)"
            + (f", {len(broken)} broken link(s)" if broken else "")
        )
        return {"status": "ok", "path": path,
                "neighbors": neighbors,
                "broken_links": broken}

    async def subgraph(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Breadth-first concept paths reachable from seed concept(s) within a
        depth — the mechanism for pulling a related cluster of context."""
        status = _status_of(params)
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
        except ValueError as e:
            await status.error(str(e))
            return {"status": "error", "error": str(e)}
        seeds = params.get("seeds") or ([params["seed"]] if params.get("seed") else [])
        seeds = [_concept_key(s) for s in seeds]
        depth = params.get("depth")
        depth = min(5, max(0, int(1 if depth is None else depth)))
        bundle = self._load_bundle(root)
        paths = bundle.subgraph(list(seeds), depth=depth)
        await status.end(
            f"{len(paths)} concept(s) within depth {depth} of {', '.join(map(str, seeds)) or 'no seed'}"
        )
        return {"status": "ok", "seeds": seeds, "depth": depth,
                "concepts": paths, "count": len(paths)}

    async def search(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Rank a bundle's concepts by lexical relevance to a query. (Ranking is
        pluggable — an embedding ranker would replace the same seam.)"""
        status = _status_of(params)
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
        except ValueError as e:
            await status.error(str(e))
            return {"status": "error", "error": str(e)}
        query = params.get("query", "")
        limit = min(50, max(1, int(params.get("limit") or 8)))
        await status.progress(f"Searching {params.get('bundle')} for '{query}'")
        bundle = self._load_bundle(root)
        ranked = self._rank_lexical(bundle, query, limit)
        # ``lifecycle`` belongs here too, not only in ``list``: for most
        # readers a search is the WAY INTO the bundle. Retired knowledge found
        # unmarked is quoted as valid -- ``deprecated`` would be invisible
        # exactly where it counts.
        results = [
            {"path": p, "score": round(s, 4),
             "type": bundle.concepts[p].type,
             "description": bundle.concepts[p].description,
             "lifecycle": bundle.concepts[p].lifecycle_status}
            for p, s in ranked
        ]
        await status.end(
            f"'{query}': {len(results)} hit(s)"
            + (f" — best {results[0]['path']} ({results[0]['score']})" if results
               else f" (searched {len(bundle.concepts)} concepts)")
        )
        return {"status": "ok", "query": query, "count": len(results),
                "results": results}

    async def append_log(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Append an entry to a directory's ``log.md`` (ISO date, newest first).
        ``date`` MUST be supplied by the caller (YYYY-MM-DD) — the server never
        reads the clock (determinism + project UTC discipline)."""
        status = _status_of(params)
        if self._read_only:
            await status.error("OKF server is read-only")
            return {"status": "error", "error": "OKF server is read-only"}
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
        except ValueError as e:
            await status.error(str(e))
            return {"status": "error", "error": str(e)}
        subdir = (params.get("dir") or "").strip("/")
        date = params.get("date")
        action = params.get("action", "Update")
        desc = params.get("description", "")
        if not date or not _valid(r"^\d{4}-\d{2}-\d{2}$", str(date), dt.date):
            await status.error("'date' must be YYYY-MM-DD")
            return {"status": "error", "error": "'date' must be YYYY-MM-DD"}

        time_str = str(params.get("time") or "").strip()
        if time_str:
            if not _valid(r"^\d{2}:\d{2}(:\d{2})?$", time_str, dt.time):
                await status.error("'time' must be HH:MM or HH:MM:SS")
                return {"status": "error", "error": "'time' must be HH:MM or HH:MM:SS"}
        else:
            time_str = self._clock_time_for(str(date))
        log_dir = self._resolve_dir(root, subdir)
        if log_dir is None:
            await status.error("'dir' escapes the bundle")
            return {"status": "error", "error": "'dir' escapes the bundle"}
        log_path = log_dir / core.LOG_FILENAME

        def _append() -> None:
            # THE race that matters in practice: a log append is read-whole-file,
            # prepend, write-whole-file. Two of them interleaved lose an entry
            # outright, and nothing reports it — the tool returns ok to both.
            existing = (log_path.read_text(encoding="utf-8")
                        if log_path.is_file() else None)
            text = core.append_log_entry(existing, str(date), str(action),
                                         str(desc), time_str or None)
            log_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(log_path, text)

        try:
            await _exclusive(root, _append)
        except Timeout:
            msg = (f"another writer is holding the OKF bundle lock "
                   f"(> {LOCK_TIMEOUT_S:.0f}s) — try again")
            await status.error(msg)
            return {"status": "error", "error": msg}

        rel = self._bundle_rel(root, log_path)
        await status.end(
            f"logged to {rel} — {date}{' ' + time_str if time_str else ''} {action}")
        return {"status": "ok", "path": rel, "date": str(date),
                "time": time_str or None}

    def _clock_time_for(self, date_iso: str) -> str:
        """``HH:MM:SS`` for an entry dated TODAY, else ``""``.

        The caller supplies the date, so it may well be a past one (a backfilled
        entry). Stamping the current clock time onto it would not be a missing
        detail but a wrong one, so the time is simply omitted there.

        Both values come from ``get_datetime_context`` — the very function that
        produces the agent's ``{{ current_date }}``. Reading the clock here
        directly would work today and drift the moment the timezone handling
        changes on one side only; a UTC time beside a local date disagrees by
        hours, and around midnight by a whole day.
        """
        from agent_system.utils.prompt_renderer import get_datetime_context

        ctx_cfg = getattr(self.system_config, "context", None)
        tz = getattr(ctx_cfg, "timezone", None)
        location = getattr(ctx_cfg, "location", None)
        now = get_datetime_context(tz if isinstance(tz, str) else "UTC",
                                   location if isinstance(location, str) else "")
        return now["current_time"] if now["current_date"] == date_iso else ""

    @staticmethod
    def _index_description(concept: "core.Concept") -> Optional[str]:
        """Description as it appears in index.md, retirement made visible.

        index.md is the entry point a reader opens first — leaving the
        marker to list/search would hide it exactly where the overview is
        formed. Prefixed rather than appended so it survives a truncated
        line.
        """
        desc = concept.description
        if concept.lifecycle_status != "deprecated":
            return desc
        return f"[deprecated] {desc}" if desc else "[deprecated]"

    async def reindex(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """(Re)generate ``index.md`` from concept frontmatter descriptions.

        With ``dir``: that directory only, one level (unchanged historic
        behaviour — deeper concepts belong to their own subdir index).

        Without ``dir``: the WHOLE bundle, recursively — one ``index.md``
        per directory that holds concepts (spec form, one level each), and
        every index links its child-directory indexes with the subtree
        concept count. Before 2026-08-19 the root call indexed one level
        and silently dropped everything deeper: a bundle organised into
        subdirectories (writer_library: /buecher, /achsen, …) produced a
        near-empty root index that read like an empty library.
        """
        status = _status_of(params)
        if self._read_only:
            await status.error("OKF server is read-only")
            return {"status": "error", "error": "OKF server is read-only"}
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
        except ValueError as e:
            await status.error(str(e))
            return {"status": "error", "error": str(e)}
        subdir = (params.get("dir") or "").strip("/")
        index_dir = self._resolve_dir(root, subdir)
        if index_dir is None:
            await status.error("'dir' escapes the bundle")
            return {"status": "error", "error": "'dir' escapes the bundle"}
        prefix = "/" + subdir + "/" if subdir else "/"
        heading = params.get("heading") or (subdir.split("/")[-1].title() if subdir else "Contents")
        index_path = index_dir / core.INDEX_FILENAME

        def _reindex() -> Tuple[int, int]:
            # The bundle scan belongs inside the lock too: an index built from a
            # listing taken before a concurrent write would be published as
            # current while already missing that concept.
            bundle = self._load_bundle(root)

            if subdir:
                # Historic single-directory mode: one level of `dir`.
                entries: List[Tuple[str, Optional[str]]] = []
                for p, c in sorted(bundle.concepts.items()):
                    if not p.startswith(prefix):
                        continue
                    rest = p[len(prefix):]
                    if "/" in rest:  # deeper — belongs to a subdir index
                        continue
                    entries.append((p, self._index_description(c)))
                text = core.render_index(entries, heading=heading)
                index_dir.mkdir(parents=True, exist_ok=True)
                _atomic_write(index_path, _keep_frontmatter(index_path, text))
                return len(entries), 1

            # Root mode: recursive. One index.md per directory with concepts;
            # each index links its child-directory indexes with the subtree
            # concept count.
            by_dir: Dict[str, List[Tuple[str, Optional[str]]]] = {}
            subtree: Dict[str, int] = {"": 0}
            for p, c in sorted(bundle.concepts.items()):
                d = str(PurePosixPath(p.lstrip("/")).parent)
                d = "" if d == "." else d
                by_dir.setdefault(d, []).append((p, self._index_description(c)))
                cur = d
                while True:
                    subtree[cur] = subtree.get(cur, 0) + 1
                    if not cur:
                        break
                    parent = str(PurePosixPath(cur).parent)
                    cur = "" if parent == "." else parent
            children: Dict[str, List[str]] = {}
            for d in subtree:
                if d:
                    parent = str(PurePosixPath(d).parent)
                    parent = "" if parent == "." else parent
                    children.setdefault(parent, []).append(d)

            written = 0
            for d in sorted(subtree):
                entries = list(by_dir.get(d, []))
                for child in sorted(children.get(d, [])):
                    n = subtree[child]
                    entries.append((
                        f"/{child}/{core.INDEX_FILENAME}",
                        f"{n} concept(s)",
                    ))
                d_heading = (
                    d.split("/")[-1].title() if d
                    else (params.get("heading") or "Contents")
                )
                text = core.render_index(entries, heading=d_heading)
                target_dir = (root / d) if d else root
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / core.INDEX_FILENAME
                _atomic_write(target, _keep_frontmatter(target, text))
                written += 1
            return subtree.get("", 0), written

        try:
            count, written = await _exclusive(root, _reindex)
        except Timeout:
            msg = (f"another writer is holding the OKF bundle lock "
                   f"(> {LOCK_TIMEOUT_S:.0f}s) — try again")
            await status.error(msg)
            return {"status": "error", "error": msg}

        rel = self._bundle_rel(root, index_path)
        # The only one of the nine tools whose end line named no subject.
        subject = params.get("bundle", "")
        if subdir:
            subject = f"{subject}/{subdir}"
        await status.end(
            f"regenerated {written} index file(s), {count} concept(s) "
            f"-- {subject[:60]}"
        )
        return {"status": "ok", "path": rel, "entries": count, "indexes": written}

    # ------------------------------------------------------------------
    # Consumer hook — fold a bundle into agent context (opt-in per agent)
    # ------------------------------------------------------------------

    async def on_pre_llm_call(self, context: "HookContext") -> "HookResult":
        """Append relevant OKF concepts to the history before the LLM call
        ('wiki as context'). Dual retrieval: a few lexical seeds against
        the user's latest message, graph-EXPANDED to include the concepts they
        link to, then capped.

        Per-agent config comes from ``context.hook_config`` (the agent's
        ``hooks.overrides`` block) with the plugin-level values as fallback:
        ``hook_bundle`` (required to do anything), ``hook_max_concepts``,
        ``hook_graph_depth``, ``hook_seed_count``, ``hook_seed_concept``.

        Fires on EVERY step, so the block is written only when the selected
        concepts actually changed: it is appended at the END as a developer
        turn and left alone afterwards. A prior block keeps its place -- what
        the seeds selected then was true then -- and rewriting it would change
        the prefix the provider has already cached."""
        from agent_system.hooks import HookResult
        try:
            hc = getattr(context, "hook_config", None) or {}
            bundle_path = hc.get("hook_bundle", self._hook_bundle)
            if not bundle_path:
                return HookResult(success=True, modified=False)
            max_concepts = int(hc.get("hook_max_concepts", self._hook_max_concepts))
            depth = int(hc.get("hook_graph_depth", self._hook_graph_depth))
            seed_count = int(hc.get("hook_seed_count", self._hook_seed_count))
            seed_anchor = hc.get("hook_seed_concept", self._hook_seed)
            seed_anchor = _concept_key(seed_anchor) if seed_anchor else None

            try:
                root = self._resolve_bundle(bundle_path)
            except ValueError as e:
                logger.debug("okf hook: bundle unresolved: %s", e)
                return HookResult(success=True, modified=False)

            messages = getattr(context, "messages", None) or []
            user_msg = ""
            for m in reversed(messages):
                get = m.get if isinstance(m, dict) else lambda key, m=m: getattr(m, key, None)
                content, role = get("content"), get("role")
                # Seeded from what a person wrote: a message the loop or a hook
                # added (step note, follow-up, "continue") re-ranked the concepts
                # and rewrote this block right behind the system prompt.
                if (role == "user" and get("injected_by") is None
                        and isinstance(content, str) and content.strip()):
                    user_msg = content
                    break

            bundle = self._load_bundle(root)
            if not bundle.concepts:
                return HookResult(success=True, modified=False)

            # A FEW lexical seeds (seed_count < max_concepts) so graph expansion
            # has room to add the concepts they link to.
            if seed_anchor and seed_anchor in bundle.concepts:
                seeds = [seed_anchor]
            elif user_msg:
                seeds = [p for p, _ in self._rank_lexical(
                    bundle, user_msg, max(1, min(seed_count, max_concepts)))]
            else:
                seeds = []
            if not seeds:
                return HookResult(success=True, modified=False)

            paths = bundle.subgraph(seeds, depth=depth)[:max_concepts]
            injection = self._render_context_block(bundle, paths)
            if not injection:
                return HookResult(success=True, modified=False)

            # Append-only: the concept block is a turn in the history, not a
            # text at the head rebuilt on every step. At the head it changed
            # the prompt prefix on every call, so the whole history was paid
            # for again; appended at the end, everything before it stays
            # byte-identical. An earlier block keeps its place -- what the
            # seeds selected then was true then -- and one that compaction
            # took away simply comes back.
            from agent_system.llm.message_roles import DEVELOPER
            from agent_system.llm.models import ChatMessage
            previous = next(
                (m for m in reversed(messages)
                 if getattr(m, "injected_by", None) == "okf"), None)
            if previous is not None and previous.content == injection:
                return HookResult(success=True, modified=False, context=context)

            messages.append(ChatMessage(
                role=DEVELOPER, content=injection, injected_by="okf"))
            context.messages = messages
            return HookResult(success=True, modified=True, context=context,
                              metadata={"okf_concepts": len(paths)})
        except Exception as e:  # don't break the LLM call, but make it visible
            logger.warning("okf context hook failed: %s", e, exc_info=True)
            return HookResult(success=False, modified=False,
                              metadata={"error": str(e)})

    @staticmethod
    def _render_context_block(bundle: core.Bundle, paths: List[str]) -> str:
        parts = ["[OKF knowledge context — relevant curated concepts]"]
        for p in paths:
            c = bundle.concepts.get(p)
            if not c:
                continue
            header = f"## {c.title or p} ({c.type or 'concept'}) — {p}"
            # The riskiest read path of all: this text reaches the model
            # unasked and is read as valid knowledge. Retired knowledge
            # (spec §5.4) MUST be marked here -- it stays in the bundle "for
            # links and history", but a model given it unmarked quotes it as
            # current.
            if c.lifecycle_status == "deprecated":
                header += "  [DEPRECATED — retired, do not treat as current]"
            body = c.body.strip()
            if len(body) > 1500:
                body = body[:1500].rstrip() + "\n…[truncated]"
            parts.append(f"{header}\n{body}")
        return "\n\n".join(parts) if len(parts) > 1 else ""
