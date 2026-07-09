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

Documents are held in memory, scoped per agent session by default
(``session_scoped: false`` shares them process-wide; calls without a
session id share the "global" namespace). Idle namespaces are evicted
after ``namespace_ttl_hours`` (long-running server processes must not
accumulate state forever). This is working state, not durable storage.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.utils.json_utils import repair_json, strip_markdown_fences

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class JsonStoreServer(SchemaBasedMCPServer):
    """In-memory, validated JSON document store."""

    def __init__(self, name: str, system_config: "AgentSystemConfig",
                 mcp_config: "MCPConfig") -> None:
        super().__init__(name, system_config, mcp_config)

        config_dict = getattr(mcp_config, "config", None) or {}
        self._session_scoped: bool = bool(config_dict.get("session_scoped", True))
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

        # namespace (session id or "global") -> doc name -> Python object
        self._docs: Dict[str, Dict[str, Any]] = {}
        self._ns_last_access: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Namespaces
    # ------------------------------------------------------------------

    def _ns(self, params: Dict[str, Any]) -> str:
        if self._session_scoped:
            return str(params.get("_session_id") or "global")
        return "global"

    def _bucket(self, params: Dict[str, Any]) -> Dict[str, Any]:
        ns = self._ns(params)
        now = time.time()
        if self._namespace_ttl_s > 0:
            expired = [k for k, ts in self._ns_last_access.items()
                       if k != ns and now - ts > self._namespace_ttl_s]
            for k in expired:
                self._docs.pop(k, None)
                self._ns_last_access.pop(k, None)
                logger.info("json_store: evicted idle namespace '%s' (TTL)", k)
        self._ns_last_access[ns] = now
        return self._docs.setdefault(ns, {})

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
                    raise ValueError(f"Path segment '{seg}' not found "
                                     f"(available keys: {list(current.keys())[:15]})")
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

    @classmethod
    def _collect_key_violations(cls, value: Any, model: Any, path: str,
                                violations: List[str]) -> None:
        """Recursively check ``value`` against a key model.

        Model semantics: dict = allowed keys at this level ("*" = any key,
        its value models the children); empty dict = free subtree. Arrays
        apply the model to each element. Scalars are always fine (this is a
        KEY model, not a type schema)."""
        if not isinstance(model, dict) or not model:
            return  # free subtree
        if isinstance(value, list):
            for i, item in enumerate(value):
                cls._collect_key_violations(item, model, f"{path}[{i}]", violations)
            return
        if not isinstance(value, dict):
            return
        wildcard = model.get("*")
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key in model:
                cls._collect_key_violations(child, model[key], child_path, violations)
            elif wildcard is not None:
                cls._collect_key_violations(child, wildcard, child_path, violations)
            else:
                allowed = [k for k in model.keys() if k != "*"]
                violations.append(f"'{child_path}' (allowed here: {allowed})")

    def _validate_keys(self, doc_name: str, value: Any) -> List[str]:
        """Violations of the configured key model for ``doc_name`` (empty = ok)."""
        model = self._key_models.get(doc_name)
        if not model:
            return []
        violations: List[str] = []
        self._collect_key_violations(value, model, "", violations)
        return violations

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
            return list(value.keys())
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
            return None, None, {
                "status": "error",
                "error": f"Document '{name}' not found. Existing: {list(bucket.keys())}"}
        return bucket, name, None

    def _key_model_error(self, violations: List[str]) -> Dict[str, Any]:
        shown = violations[:10]
        more = f" (+{len(violations) - 10} more)" if len(violations) > 10 else ""
        return {
            "status": "error",
            "error": f"Unknown key(s) not in the document's key model: "
                     f"{'; '.join(shown)}{more}. Remove or rename these keys — "
                     f"do not invent fields.",
        }

    def _commit(self, params: Dict[str, Any], name: str, value: Any,
                **extra: Any) -> Dict[str, Any]:
        """Single gate for every store: max_docs (new docs), size limit and
        key model are enforced here, and a failed check never touches the
        stored document (callers pass fully-built candidate values)."""
        bucket = self._bucket(params)
        if name not in bucket and len(bucket) >= self._max_docs:
            return {"status": "error",
                    "error": f"Too many documents ({self._max_docs}). Delete unused ones."}
        size = self._size(value)
        if size > self._max_doc_bytes:
            return {"status": "error",
                    "error": f"Document too large ({size} chars > limit "
                             f"{self._max_doc_bytes})."}
        violations = self._validate_keys(name, value)
        if violations:
            return self._key_model_error(violations)
        bucket[name] = value
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

    async def manage_json(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch to the operation handlers below."""
        operation = (params.get("operation") or "").strip()
        handlers = {
            "write": self.write,
            "read": self.read,
            "merge": self.merge,
            "set_value": self.set_value,
            "delete_keys": self.delete_keys,
            "delete_doc": self.delete_doc,
            "list": self.list_docs,
            "outline": self.outline,
        }
        handler = handlers.get(operation)
        if not handler:
            return {"status": "error",
                    "error": f"Unknown or missing operation '{operation}'. "
                             f"Valid: {sorted(handlers)}"}
        return await handler(params)

    # ------------------------------------------------------------------
    # Operation handlers
    # ------------------------------------------------------------------

    async def write(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Create or fully replace a document."""
        status = params.get("_status")
        name = (params.get("doc") or "").strip()
        if not name:
            return {"status": "error", "error": "'doc' (document name) is required"}
        try:
            value, repairs = self._extract_payload(params)
        except ValueError as e:
            return {"status": "error", "error": str(e)}

        replaced = name in self._bucket(params)
        result = self._commit(params, name, value, replaced=replaced,
                              **({"repairs": repairs} if repairs else {}))
        if status and result.get("status") == "ok":
            await status.end(f"json_store: wrote '{name}' ({result['chars']} chars)")
        return result

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
        return {"status": "ok", "doc": name, "path": path or None,
                "chars": len(text), "json": text}

    async def merge(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Deep-merge an object into a document (dicts recurse, scalars overwrite)."""
        status = params.get("_status")
        name = (params.get("doc") or "").strip()
        if not name:
            return {"status": "error", "error": "'doc' (document name) is required"}
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
        result = self._commit(params, name, merged, created=not existed,
                              merged_keys=merged_keys,
                              **({"repairs": repairs} if repairs else {}))
        if status and result.get("status") == "ok":
            await status.end(f"json_store: merged into '{name}' ({result['chars']} chars)")
        return result

    async def set_value(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Set one value at a path; missing intermediates are created
        (objects for name segments, arrays for ``[i]`` segments)."""
        name = (params.get("doc") or "").strip()
        path = (params.get("path") or "").strip()
        if not name or not path:
            return {"status": "error", "error": "'doc' and 'path' are required"}
        if "value" not in params:
            return {"status": "error", "error": "'value' is required"}
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
        return self._commit(params, name, doc, set_path=path)

    async def delete_keys(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete one or more paths from a document. Each path is applied
        independently; unknown paths are reported in 'missing'."""
        paths = params.get("paths") or []
        if not paths or not isinstance(paths, list):
            return {"status": "error", "error": "non-empty 'paths' list is required"}
        bucket, name, err = self._require_doc(params)
        if err:
            return err
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
        return self._summary(name, bucket[name], self._size(bucket[name]),
                             deleted=deleted, missing=missing)

    async def delete_doc(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete a whole document."""
        bucket, name, err = self._require_doc(params)
        if err:
            return err
        del bucket[name]
        return {"status": "ok", "deleted": name, "remaining": list(bucket.keys())}

    async def list_docs(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """List the caller's documents with size and top-level keys."""
        bucket = self._bucket(params)
        docs = [{"doc": name, "chars": self._size(value),
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
        return {"status": "ok", "doc": name, "outline": self._outline(bucket[name], depth)}
