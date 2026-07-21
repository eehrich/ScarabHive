"""OKF MCP server — tools + consumer hook for the Open Knowledge Format.

Thin IO/dispatch layer over :mod:`plugins.okf.core` (which owns the format).
The server adds: a sandboxed filesystem (reuses the file_ops ``allowed_directories``
pattern), bundle loading from disk, the MCP tool surface, and the opt-in
``pre_llm_call`` consumer hook that folds a bundle into agent context
(graph-anchored + lexical retrieval).

Retrieval note: ``okf_search`` and the hook rank concepts by a lexical relevance
score today. Embedding/semantic retrieval is a documented seam (``_rank_lexical``
is the single ranking entry point) — deliberately NOT a rushed second ChromaDB
consumer (file_ops disables its own semantic search to avoid ChromaDB conflicts).
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from agent_system.mcp.schema_based import SchemaBasedMCPServer

from . import core

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig
    from agent_system.hooks import HookContext, HookResult

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokens(text: str) -> List[str]:
    return [t.lower() for t in _WORD_RE.findall(text or "")]


def _atomic_write(path: Path, text: str) -> None:
    """Write via tmp-file + replace so a reader never sees a half-written file
    and a crash can't truncate the target (bundles are git-versioned)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class OkfServer(SchemaBasedMCPServer):
    """Sandboxed read/write/validate/graph/search over OKF bundles, plus a
    context-injection hook."""

    def __init__(self, name: str, system_config: "AgentSystemConfig",
                 mcp_config: "MCPConfig") -> None:
        super().__init__(name, system_config, mcp_config)

        # Config may arrive either as top-level plugin keys (file_ops style,
        # via MCPConfig extra="allow") or under a 'config:' sub-block
        # (json_store style). Merge both, the explicit sub-block winning.
        top_level = getattr(mcp_config, "model_extra", None) or {}
        sub = getattr(mcp_config, "config", None) or {}
        cfg = {**top_level, **sub}

        # Sandbox roots — same contract as file_ops: relative paths resolve
        # against the project root; every bundle/concept path a tool touches
        # MUST resolve inside one of these.
        allowed = cfg.get("allowed_directories") or ["data/okf"]
        project_root = Path.cwd()
        self._roots: List[Path] = []
        for d in allowed:
            p = Path(d)
            self._roots.append(p.resolve() if p.is_absolute()
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
        # (file count + newest mtime) lets repeated tool calls and the per-turn
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
        p = Path(bundle)
        p = p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()
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
        p = (bundle_root / rel).resolve()
        if p != bundle_root and bundle_root not in p.parents:
            raise ValueError(f"concept path '{rel_path}' escapes the bundle")
        return p

    def _bundle_rel(self, bundle_root: Path, abs_path: Path) -> str:
        """Absolute path -> bundle-relative leading-slash POSIX path."""
        return "/" + abs_path.relative_to(bundle_root).as_posix()

    # ------------------------------------------------------------------
    # Bundle loading
    # ------------------------------------------------------------------

    def _load_bundle(self, bundle_root: Path) -> core.Bundle:
        """Walk a bundle directory and build an in-memory :class:`core.Bundle`,
        cached by (file count, newest mtime) so an unchanged bundle isn't
        re-read on every tool call / LLM turn.

        Concepts that fail to parse are still loaded (with their parse error
        recorded) so validation reports the true failure; unreadable files are
        skipped. Files larger than the per-file cap and files that resolve
        OUTSIDE the bundle (symlink escape) are skipped — the bulk path must be
        as sandbox-safe as _resolve_concept, since its content flows into search
        results and the context-injection hook (and thus to the LLM)."""
        mds = sorted(bundle_root.rglob("*.md"))[: self._max_files]
        try:
            sig = (len(mds), max((p.stat().st_mtime_ns for p in mds), default=0))
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
            except OSError as e:
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
    # MCP tools  (schema tool  okf_<op>  ->  method <op>)
    # ------------------------------------------------------------------

    async def validate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Check a bundle against OKF producer conformance (every concept has a
        non-empty ``type``); report broken links as warnings."""
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
        except ValueError as e:
            return {"status": "error", "error": str(e)}
        bundle = self._load_bundle(root)
        report = core.validate_bundle(bundle)
        return {"status": "ok", "bundle": params.get("bundle"),
                "concepts": len(bundle.concepts), **report.to_dict()}

    async def read_concept(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Return a concept's frontmatter (all keys preserved) and body."""
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
            abs_path = self._resolve_concept(root, params.get("path", ""))
        except ValueError as e:
            return {"status": "error", "error": str(e)}
        if not abs_path.is_file():
            return {"status": "error", "error": f"concept not found: {params.get('path')}"}
        fm, body, err = core.parse_frontmatter(abs_path.read_text(encoding="utf-8"))
        return {"status": "ok", "path": params.get("path"),
                "frontmatter": dict(fm) if fm else None, "body": body,
                "parse_error": err}

    async def write_concept(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Write (create/overwrite) a concept. Enforces the format: a non-empty
        ``type`` frontmatter field is required, else the write is rejected.
        Existing extra frontmatter keys are preserved on overwrite."""
        if self._read_only:
            return {"status": "error", "error": "OKF server is read-only"}
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
            abs_path = self._resolve_concept(root, params.get("path", ""))
        except ValueError as e:
            return {"status": "error", "error": str(e)}

        # Reserved filenames are NOT concepts (spec MUST) — the producer tool
        # must refuse to write a concept there (use append_log / reindex).
        rel_path = params.get("path", "")
        if core.is_reserved(rel_path):
            return {"status": "error",
                    "error": f"'{rel_path}' is a reserved OKF filename "
                             f"(index.md/log.md) — use okf_append_log / okf_reindex"}

        frontmatter = params.get("frontmatter")
        body = params.get("body") or ""
        if not isinstance(frontmatter, dict):
            return {"status": "error",
                    "error": "'frontmatter' must be an object with at least a 'type'"}

        # Overwrite: deep-merge the caller's frontmatter INTO the existing one,
        # mutating the parsed CommentedMap so comments/order/quoting and nested
        # producer keys survive (spec: preserve unknown keys on round-trip).
        existing_fm = None
        if abs_path.is_file():
            existing_fm, _b, _e = core.parse_frontmatter(
                abs_path.read_text(encoding="utf-8"))
        merged = core.merge_frontmatter(existing_fm, frontmatter)

        text = core.dump_frontmatter(merged, body)
        findings = core.validate_concept_text(rel_path, text)
        errors = [f for f in findings if f.severity == "error"]
        if errors:
            return {"status": "error",
                    "error": errors[0].message,
                    "findings": [f.__dict__ for f in errors]}

        abs_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(abs_path, text)
        return {"status": "ok", "path": rel_path, "bytes": len(text)}

    async def list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """List concepts in a bundle (optionally under a subdirectory), with
        type + description — the progressive-disclosure view index.md provides."""
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
        except ValueError as e:
            return {"status": "error", "error": str(e)}
        subdir = (params.get("dir") or "").strip("/")
        bundle = self._load_bundle(root)
        prefix = "/" + subdir + "/" if subdir else "/"
        items = [
            {"path": p, "type": c.type, "title": c.title, "description": c.description}
            for p, c in sorted(bundle.concepts.items())
            if p.startswith(prefix)
        ]
        return {"status": "ok", "bundle": params.get("bundle"),
                "count": len(items), "concepts": items,
                "version": bundle.version}

    async def neighbors(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Return the concepts a given concept links to (its graph neighbors),
        plus any broken links. This is the OKF 'graph, not just tree' surface."""
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
        except ValueError as e:
            return {"status": "error", "error": str(e)}
        path = params.get("path", "")
        bundle = self._load_bundle(root)
        if path not in bundle.concepts:
            return {"status": "error", "error": f"concept not found: {path}"}
        return {"status": "ok", "path": path,
                "neighbors": bundle.neighbors(path),
                "broken_links": bundle.broken_links(path)}

    async def subgraph(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Breadth-first concept paths reachable from seed concept(s) within a
        depth — the mechanism for pulling a related cluster of context."""
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
        except ValueError as e:
            return {"status": "error", "error": str(e)}
        seeds = params.get("seeds") or ([params["seed"]] if params.get("seed") else [])
        depth = int(params.get("depth", 1))
        bundle = self._load_bundle(root)
        paths = bundle.subgraph(list(seeds), depth=depth)
        return {"status": "ok", "seeds": seeds, "depth": depth,
                "concepts": paths, "count": len(paths)}

    async def search(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Rank a bundle's concepts by lexical relevance to a query. (Ranking is
        pluggable — an embedding ranker would replace the same seam.)"""
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
        except ValueError as e:
            return {"status": "error", "error": str(e)}
        query = params.get("query", "")
        limit = int(params.get("limit", 8))
        bundle = self._load_bundle(root)
        ranked = self._rank_lexical(bundle, query, limit)
        results = [
            {"path": p, "score": round(s, 4),
             "type": bundle.concepts[p].type,
             "description": bundle.concepts[p].description}
            for p, s in ranked
        ]
        return {"status": "ok", "query": query, "count": len(results),
                "results": results}

    async def append_log(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Append an entry to a directory's ``log.md`` (ISO date, newest first).
        ``date`` MUST be supplied by the caller (YYYY-MM-DD) — the server never
        reads the clock (determinism + project UTC discipline)."""
        if self._read_only:
            return {"status": "error", "error": "OKF server is read-only"}
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
        except ValueError as e:
            return {"status": "error", "error": str(e)}
        subdir = (params.get("dir") or "").strip("/")
        date = params.get("date")
        action = params.get("action", "Update")
        desc = params.get("description", "")
        if not date or not re.match(r"^\d{4}-\d{2}-\d{2}$", str(date)):
            return {"status": "error", "error": "'date' must be YYYY-MM-DD"}
        log_dir = (root / subdir).resolve() if subdir else root
        if log_dir != root and root not in log_dir.parents:
            return {"status": "error", "error": "'dir' escapes the bundle"}
        log_path = log_dir / core.LOG_FILENAME
        existing = log_path.read_text(encoding="utf-8") if log_path.is_file() else None
        text = core.append_log_entry(existing, str(date), str(action), str(desc))
        log_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(log_path, text)
        return {"status": "ok", "path": self._bundle_rel(root, log_path)}

    async def reindex(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """(Re)generate ``index.md`` for a bundle directory from the concepts it
        contains (one level), using each concept's frontmatter description."""
        if self._read_only:
            return {"status": "error", "error": "OKF server is read-only"}
        try:
            root = self._resolve_bundle(params.get("bundle", ""))
        except ValueError as e:
            return {"status": "error", "error": str(e)}
        subdir = (params.get("dir") or "").strip("/")
        index_dir = (root / subdir).resolve() if subdir else root
        if index_dir != root and root not in index_dir.parents:
            return {"status": "error", "error": "'dir' escapes the bundle"}
        bundle = self._load_bundle(root)
        prefix = "/" + subdir + "/" if subdir else "/"
        # Direct children only (concepts one level under index_dir).
        entries: List[Tuple[str, Optional[str]]] = []
        for p, c in sorted(bundle.concepts.items()):
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            if "/" in rest:  # deeper than one level — skip (belongs to subdir index)
                continue
            entries.append((p, c.description))
        heading = params.get("heading") or (subdir.split("/")[-1].title() if subdir else "Contents")
        text = core.render_index(entries, heading=heading)
        index_path = index_dir / core.INDEX_FILENAME
        index_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(index_path, text)
        return {"status": "ok", "path": self._bundle_rel(root, index_path),
                "entries": len(entries)}

    # ------------------------------------------------------------------
    # Consumer hook — fold a bundle into agent context (opt-in per agent)
    # ------------------------------------------------------------------

    async def on_pre_llm_call(self, context: "HookContext") -> "HookResult":
        """Inject relevant OKF concepts into the system prompt before the LLM
        call ('wiki as context'). Dual retrieval: a few lexical seeds against
        the user's latest message, graph-EXPANDED to include the concepts they
        link to, then capped.

        Per-agent config comes from ``context.hook_config`` (the agent's
        ``hooks.overrides`` block) with the plugin-level values as fallback:
        ``hook_bundle`` (required to do anything), ``hook_max_concepts``,
        ``hook_graph_depth``, ``hook_seed_count``, ``hook_seed_concept``.

        Fires on EVERY step, so a prior OKF block (marked ``injected_by='okf'``)
        is removed before re-injecting, and the block is placed right after the
        leading system messages (providers expect system content up front)."""
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

            try:
                root = self._resolve_bundle(bundle_path)
            except ValueError as e:
                logger.debug("okf hook: bundle unresolved: %s", e)
                return HookResult(success=True, modified=False)

            messages = getattr(context, "messages", None) or []
            user_msg = ""
            for m in reversed(messages):
                content = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
                role = getattr(m, "role", None) if not isinstance(m, dict) else m.get("role")
                if role == "user" and isinstance(content, str) and content.strip():
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

            from agent_system.llm.models import ChatMessage
            # Drop any prior OKF injection (this hook re-runs every step).
            msgs = [m for m in messages
                    if getattr(m, "injected_by", None) != "okf"]
            insert_pos = self._leading_system_end(msgs)
            msgs.insert(insert_pos, ChatMessage(
                role="system", content=injection, injected_by="okf"))
            context.messages = msgs
            return HookResult(success=True, modified=True, context=context,
                              metadata={"okf_concepts": len(paths)})
        except Exception as e:  # don't break the LLM call, but make it visible
            logger.warning("okf context hook failed: %s", e, exc_info=True)
            return HookResult(success=False, modified=False,
                              metadata={"error": str(e)})

    @staticmethod
    def _leading_system_end(messages: List[Any]) -> int:
        """Index just after the leading run of system messages (where injected
        context belongs — after the agent's base prompt, before the convo)."""
        i = 0
        for m in messages:
            role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
            if role == "system":
                i += 1
            else:
                break
        return i

    @staticmethod
    def _render_context_block(bundle: core.Bundle, paths: List[str]) -> str:
        parts = ["[OKF knowledge context — relevant curated concepts]"]
        for p in paths:
            c = bundle.concepts.get(p)
            if not c:
                continue
            header = f"## {c.title or p} ({c.type or 'concept'}) — {p}"
            body = c.body.strip()
            if len(body) > 1500:
                body = body[:1500].rstrip() + "\n…[truncated]"
            parts.append(f"{header}\n{body}")
        return "\n\n".join(parts) if len(parts) > 1 else ""
