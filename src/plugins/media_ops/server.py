"""media_ops MCP server — move media between disk and the agent's context.

Both directions, because neither existed:

* ``load``  — audio_ops/comfyui/image_compose can only hand back media they
  produced themselves. This loads what is already on disk, via the same
  mechanism: a ``_multimodal_content`` entry in the tool result, which
  ``tool_execution.py`` pops and attaches to the tool answer.
* ``list_context`` / ``save`` — media that arrived inline (base64) lives only
  in the conversation and is lost when the context is compacted. These two put
  it on disk under a name the agent chooses.

SECURITY: every path — the one read AND the one written — arrives verbatim from
LLM tool arguments. Unbounded, ``load`` is an arbitrary-file-read primitive
whose payload lands base64-encoded in the model context, and ``save`` an
arbitrary-file-write primitive. Both therefore route through ``_resolve()``,
which delegates to the shared ``agent_system.utils.path_sandbox`` — the same
boundary ``file_ops`` uses, so there is one interpretation of "inside
``allowed_directories``" rather than one per plugin. ``save`` declares itself
as a write, so a ``read_only`` sandbox refuses it without a second flag.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.utils.multimodal_tool_content import extract_inline_media
from agent_system.utils.path_sandbox import PathSandbox, PathSandboxDenied

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)

# Extension -> (multimodal type, mime type). Doubles as the allowlist: an
# extension that is not in here is refused. Deliberately explicit instead of
# mimetypes.guess_type(), which is registry-driven on Windows and has no notion
# of "is this something a model can look at".
MEDIA_TYPES: Dict[str, tuple[str, str]] = {
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".gif": ("image", "image/gif"),
    ".webp": ("image", "image/webp"),
    ".bmp": ("image", "image/bmp"),
    ".wav": ("audio", "audio/wav"),
    ".mp3": ("audio", "audio/mpeg"),
    ".ogg": ("audio", "audio/ogg"),
    ".flac": ("audio", "audio/flac"),
    ".m4a": ("audio", "audio/mp4"),
    ".aac": ("audio", "audio/aac"),
    ".mp4": ("video", "video/mp4"),
    ".webm": ("video", "video/webm"),
    ".avi": ("video", "video/avi"),
    ".mov": ("video", "video/quicktime"),
}

# Content-item types that count as media in a conversation.
CONTEXT_MEDIA_TYPES = frozenset({"image", "image_url", "audio", "video"})


class MediaOpsServer(SchemaBasedMCPServer):
    """Moves image/audio/video between disk and the conversation.

    Every path — read or written — must resolve inside ``allowed_directories``.
    """

    def __init__(self, name: str, system_config: "AgentSystemConfig",
                 mcp_config: "MCPConfig") -> None:
        super().__init__(name, system_config, mcp_config)

        # ONE mapping, read once. Config may arrive as top-level plugin keys
        # (MCPConfig extra="allow") or under a 'config:' sub-block; both are
        # merged here so neither style is swallowed silently.
        top_level = getattr(mcp_config, "model_extra", None) or {}
        sub = getattr(mcp_config, "config", None) or {}
        cfg = {**top_level, **sub}

        project_root = Path.cwd()
        self.max_file_size_mb: float = float(cfg.get("max_file_size_mb", 20))
        # Deliberately NO self.project_root: the base lives in the sandbox. A
        # second attribute beside it can be set without taking effect — a test
        # walked straight past that and stayed green.

        # The boundary itself is shared (agent_system.utils.path_sandbox) —
        # the same resolution file_ops uses, instead of two readings of one
        # idea. read_only belongs to that vocabulary: when set, this server
        # still loads but no longer writes anything back.
        self.sandbox = PathSandbox.from_config(
            cfg.get("allowed_directories") or ["data"],
            base=project_root,
            read_only=bool(cfg.get("read_only", False)),
        )

        logger.info("MediaOpsServer '%s' initialized — roots=%s, max=%.1f MB, read_only=%s",
                    name, [str(r) for r in self.sandbox.roots], self.max_file_size_mb,
                    self.sandbox.read_only)

    def get_template_vars(self) -> Dict[str, Any]:
        """Feed schema.yaml so a read-only sandbox does not advertise `save`.

        Same pattern as file_ops: refusing at the boundary is correct but
        costs the model a turn on a tool it was offered and cannot use.
        """
        vars = super().get_template_vars()
        vars["read_only"] = self.sandbox.read_only
        return vars

    @property
    def allowed_roots(self) -> list[Path]:
        """The resolved roots (for callers and tests that read them)."""
        return list(self.sandbox.roots)

    def _resolve(self, path: str, *, write: bool = False) -> Path:
        """Resolve `path` and assert it is inside a sandbox root.

        resolve() collapses ``..`` and follows symlinks, so both traversal and
        symlink escapes are caught. Raises PathSandboxDenied otherwise.
        """
        return self.sandbox.resolve(path, write=write)

    async def load(self, params: Dict[str, Any]) -> Dict[str, Any]:
        status = params.get("_status")
        path = params.get("path")
        if not path or not isinstance(path, str):
            return await _fail(status, "path is required (string)", "ValidationError")

        try:
            full = self._resolve(path)
        except PathSandboxDenied as e:
            return await _fail(status, str(e), "PermissionError")

        if not full.is_file():
            return await _fail(status, f"File not found: {full}", "FileNotFoundError")

        entry = MEDIA_TYPES.get(full.suffix.lower())
        if entry is None:
            return await _fail(
                status,
                f"Unsupported file type: {full.suffix or '(no extension)'}. "
                f"Supported extensions: {', '.join(sorted(MEDIA_TYPES))}",
                "UnsupportedMediaType",
            )
        content_type, mime_type = entry

        size_bytes = full.stat().st_size
        size_mb = size_bytes / (1024 * 1024)
        if size_mb > self.max_file_size_mb:
            return await _fail(
                status,
                f"File is too large for the context: {size_mb:.1f} MB "
                f"(limit {self.max_file_size_mb:g} MB): {full}",
                "FileTooLarge",
            )

        if status:
            await status.end(
                f"Loaded {full.name} [{content_type}, {_fmt_size(size_bytes)}]",
                meta={"path": str(full)},
            )
        return {
            "status": "success",
            "path": str(full),
            "type": content_type,
            "mime_type": mime_type,
            "size_mb": round(size_mb, 2),
            "_multimodal_content": [{
                "type": content_type,
                "path": str(full),
                "mime_type": mime_type,
                "description": f"Loaded {content_type}: {full.name}",
            }],
        }

    def _live_messages(self, params: Dict[str, Any]) -> List[Any]:
        """Current conversation of the calling agent.

        Same access path as context_engineer's compact(): live messages first
        (they include the turn that is calling us), persisted session tracker
        as fallback. A missing agent/session is an error, never an empty list —
        "no media in context" and "we never looked" must not read alike.
        """
        agent = params.get("_agent")
        session_id = params.get("_session_id")
        if not agent or not session_id:
            raise LookupError(
                "Session context not available — this tool only works when "
                "called by an agent inside a session."
            )

        messages = None
        if hasattr(agent, "get_live_messages"):
            live = agent.get_live_messages(session_id)
            if isinstance(live, list):
                messages = live
        if not messages:
            tracker = getattr(agent, "_session_tracker", None)
            if tracker is None:
                raise LookupError(
                    f"No live messages for session {session_id} and the agent "
                    f"has no session tracker to fall back to."
                )
            messages = tracker.get_session_messages(session_id)
        return messages or []

    async def list_context(self, params: Dict[str, Any]) -> Dict[str, Any]:
        status = params.get("_status")
        try:
            entries = _context_media(self._live_messages(params))
        except LookupError as e:
            return await _fail(status, str(e), "SessionContextMissing")

        if status:
            await status.end(f"{len(entries)} media item(s) in context")
        return {
            "status": "success",
            "count": len(entries),
            "media": [{k: v for k, v in e.items() if not k.startswith("_")}
                      for e in entries],
        }

    async def save(self, params: Dict[str, Any]) -> Dict[str, Any]:
        status = params.get("_status")
        # First gate, before anything else: a read-only sandbox cannot be
        # talked into a write by any argument, and the model gets the reason
        # instead of a validation complaint about the arguments it supplied.
        if self.sandbox.read_only:
            return await _fail(
                status,
                f"Sandbox is read-only, refusing to write: {params.get('path')}",
                "PermissionError")

        media_id = params.get("id")
        target = params.get("path")
        if not media_id or not isinstance(media_id, str):
            return await _fail(status,
                               "id is required (string, from media_ops_list_context)",
                               "ValidationError")
        if not target or not isinstance(target, str):
            return await _fail(status, "path is required (string)", "ValidationError")

        try:
            entries = _context_media(self._live_messages(params))
        except LookupError as e:
            return await _fail(status, str(e), "SessionContextMissing")

        entry = next((e for e in entries if e["id"] == media_id), None)
        if entry is None:
            return await _fail(
                status,
                f"No media with id {media_id!r} in the current context. "
                f"Call media_ops_list_context for the current ids "
                f"({len(entries)} item(s) available).",
                "MediaNotFound",
            )
        if entry.get("error"):
            return await _fail(status,
                               f"Media {media_id} cannot be saved: {entry['error']}",
                               "MediaUnreadable")

        # Already a file on disk — hand back the path instead of copying it.
        if not entry["inline"]:
            if status:
                await status.end(f"Already on disk: {entry['path']} -- nothing written")
            return {
                "status": "success",
                "path": entry["path"],
                "copied": False,
                "message": "Already on disk — nothing was written.",
            }

        try:
            full = self._resolve(target, write=True)
        except PathSandboxDenied as e:
            return await _fail(status, str(e), "PermissionError")

        if full.suffix.lower() not in MEDIA_TYPES:
            return await _fail(
                status,
                f"Unsupported target extension: {full.suffix or '(none)'}. "
                f"Supported extensions: {', '.join(sorted(MEDIA_TYPES))}",
                "UnsupportedMediaType",
            )
        if full.exists() and not params.get("overwrite"):
            return await _fail(
                status,
                f"File already exists: {full}. Pass overwrite=true to replace it.",
                "FileExists",
            )

        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(entry["_data"])
        logger.info("media_ops saved %s (%d bytes) -> %s",
                    media_id, len(entry["_data"]), full)
        if status:
            await status.end(
                f"Saved {entry['type']} -> {full.name} "
                f"({_fmt_size(len(entry['_data']))})",
                meta={"path": str(full)},
            )
        return {
            "status": "success",
            "path": str(full),
            "copied": True,
            "bytes": len(entry["_data"]),
            "type": entry["type"],
        }


def _field(obj: Any, key: str) -> Any:
    """Read `key` from a dict OR a pydantic message/content object."""
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _media_id(payload: bytes | str) -> str:
    """Stable handle for a media item — same bytes (or same path) = same id.

    Position would be the cheaper key, but message indices shift under
    compaction between the list call and the save call.
    """
    raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    return "m" + hashlib.sha256(raw).hexdigest()[:12]


def _entry(item: Any, msg_index: int, origin: str) -> Optional[Dict[str, Any]]:
    """One media item -> listing entry, or None if it carries no media.

    Inline entries keep the decoded bytes under ``_data`` for save(); the
    listing strips that.
    """
    kind = _field(item, "type")
    if kind not in CONTEXT_MEDIA_TYPES:
        return None

    entry: Dict[str, Any] = {
        "type": "image" if kind == "image_url" else kind,
        "message_index": msg_index,
        "origin": origin,
    }

    # Already on disk (MultimodalToolContent) — nothing to write later.
    path = _field(item, "path")
    if path:
        p = Path(str(path))
        entry.update(
            id=_media_id(str(p)), path=str(p), inline=False,
            mime_type=_field(item, "mime_type"),
            size_bytes=p.stat().st_size if p.is_file() else None,
        )
        return entry

    try:
        data, mime, name = extract_inline_media(item)
    except ValueError as e:
        # Corrupt payload: say so instead of dropping the item silently.
        entry.update(id=None, inline=True, error=f"undecodable base64: {e}")
        return entry
    if data is None:
        return None  # remote URL or text item — no bytes in the context

    entry.update(id=_media_id(data), inline=True, mime_type=mime,
                 name=name, size_bytes=len(data), _data=data)
    return entry


def _context_media(messages: Any) -> List[Dict[str, Any]]:
    """All media in the conversation, deduplicated by id, in message order.

    Media sits in TWO places per message: as an item in ``content`` (list form)
    and in ``multimodal_content`` (what a tool attached). Both are scanned.
    """
    entries: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for idx, msg in enumerate(messages or []):
        content = _field(msg, "content")
        candidates = list(content) if isinstance(content, list) else []
        items = [(c, "content") for c in candidates]
        items += [(m, "multimodal_content")
                  for m in (_field(msg, "multimodal_content") or [])]
        for item, origin in items:
            entry = _entry(item, idx, origin)
            if entry is None:
                continue
            if entry["id"] and entry["id"] in seen:
                continue
            if entry["id"]:
                seen.add(entry["id"])
            entries.append(entry)
    return entries


def _fmt_size(n: int) -> str:
    """Human size for status lines -- '0.00 MB' for a 3 KB file says nothing."""
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.1f} KB"


def _error(msg: str, kind: str) -> Dict[str, Any]:
    return {"status": "error", "error": msg, "error_type": kind}


async def _fail(status: Any, msg: str, kind: str) -> Dict[str, Any]:
    """Error result that also lands in the status stream.

    Without this the surrounding StatusScope closes with its default
    'completed' -- a denied load would read as a success in the CLI/WebUI.
    """
    if status:
        await status.error(msg, meta={"error_type": kind})
    return _error(msg, kind)


PLUGIN_FACTORY = MediaOpsServer
