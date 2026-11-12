"""File Ops Tool Server implementation."""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import time
from itertools import islice
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from agent_system.config import AgentSystemConfig, ToolServerConfig
from agent_system.llm.message_roles import ASSISTANT, DEVELOPER, opens_a_turn, role_of
from agent_system.utils.fields import field_of
from agent_system.paths import launch_dir
from agent_system.utils.path_sandbox import is_secret, might_be_private
from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.utils.params import bool_param as _bool_param

from .operations import FileOperations
from .search import FileSearchEngine, content_digest, latest_per_path
from .security import PathValidator, SecurityError, from_git_bash


def _whole_number(params: Dict[str, Any], key: str, default: Any) -> Any:
    """A whole-number parameter, or a ValueError that says which and how.

    The framework does not check arguments against the schema, and models send
    numbers as text: `offset: "1480, "` reached `offset > 0` and failed with
    "'>' not supported between 'str' and 'int'" -- an error that names neither
    the parameter nor the fix.
    """
    value = params.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{key}: a whole number, got {value!r}")
    if isinstance(value, int):
        return value
    text = str(value).strip(" ,")
    try:
        return int(text)
    except ValueError:
        pass
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None and number.is_integer():
        return int(number)
    raise ValueError(f"{key}: a whole number, got {value!r}")


class MissingArgument(ValueError):
    """A required argument the call left out: said by name, not as a bare KeyError ("'dir_path'")."""


def _required(params: Dict[str, Any], key: str) -> Any:
    """The argument ``key``; the framework does not check ``required``, so a call without it is answered here."""
    if params.get(key) is None:
        raise MissingArgument(f"Missing required argument '{key}': send it and call again.")
    return params[key]


def _log_failure(where: str, exc: Exception) -> None:
    """A missing argument is the model's mistake, logged without a traceback; anything else is unexpected."""
    if isinstance(exc, MissingArgument):
        logger.info("file_ops %s: %s", where, exc)
    else:
        logger.error(f"Unexpected error in {where}: {exc}", exc_info=True)


def _int_param(params: Dict[str, Any], key: str, default: Any,
               low: int | None = None, high: int | None = None) -> Any:
    """``_whole_number``, held within the bounds the schema shows the model.

    Nothing else enforces them: `max_results: -1` made list_directory raise
    IndexError and search_files answer `total_found: -1`, and a maximum the
    model reads is no limit when the server takes any number.
    """
    value = _whole_number(params, key, default)
    if value is None:
        return None
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


logger = logging.getLogger(__name__)


#: injected_by of the changed-files notice (on_pre_llm_call).
_NOTICE_BY = "file_ops"


#: Sessions whose notice state one server keeps; the oldest go first.
_NOTICE_SESSIONS = 256
#: Paths whose own writes one session remembers; the oldest go first, but
#: never those of the write just made.
_OWN_LIMIT = 500
#: Files of a moved or renamed folder whose content is remembered.
_OWN_TREE_FILES = 2000
#: A finding this much older than the mtime an own write left still counts
#: as that write's (file and wall clock may disagree a little, and FAT keeps
#: mtimes in 2-second steps); older, it is someone else's change the write
#: came after.
_OWN_CLOCK_SLACK = 2.0


@dataclass
class _NoticeState:
    """What the changed-files notice has told one session, and what its own
    writes left."""

    #: Findings up to here are told or the agent's own (None: not known yet).
    since: Optional[float] = None
    #: Findings above `since` already told (the journal may still receive
    #: older ones from another process, so `since` cannot simply move up).
    shown: Set[Tuple[float, str, str]] = field(default_factory=set)
    #: _key(path) -> (the path, the content digest and mtime this
    #: session's own write left there); digest None: its own write removed it
    #: (a file, or a folder and all in it).
    own: Dict[str, "_Own"] = field(default_factory=dict)


def _key(path: Any) -> str:
    """How own records and journal paths meet: the real path, in the case
    the platform compares (a write resolves a symlink, the walk does not)."""
    return os.path.normcase(os.path.realpath(path))


def _file_digest(path: Path) -> Optional[str]:
    """The index's content digest of the file at *path*; None when it cannot be read."""
    try:
        return content_digest(path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return None


#: (path as written, content digest or None when removed, st_mtime_ns or None)
_Own = Tuple[str, Optional[str], Optional[int]]


def _own_record(path: Path, removed: bool = False) -> _Own:
    if removed:
        return (str(path), None, None)
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        mtime = None
    return (str(path), _file_digest(path), mtime)


def _is_own(entry: Tuple[float, str, str], own: Dict[str, _Own]) -> bool:
    """Whether a journal entry is accounted for by the session's own writes.

    An added or changed path the session wrote is -- by name here; by content
    in _own_divergence, which reports it again if the file no longer holds
    what the write left (an own write and the user's edit before the next
    pass are ONE finding, and the user's content makes it news). A removal is
    the session's own when its write removed that path or a folder above it
    and the path is still gone.
    """
    found, kind, path = entry
    key = _key(path)
    if kind == "removed":
        if os.path.lexists(path):
            return False
        return any(record[1] is None and (key == gone or key.startswith(gone + os.sep))
                   for gone, record in own.items())
    # Whether the file still holds what the write left is _own_divergence's
    # question, asked for every record: a file the user changed after the
    # agent comes back from there as "changed", whatever the journal says.
    # Found BEFORE the write, the change was someone else's: a partial edit
    # of the agent's does not take back the user's elsewhere in the file.
    record = own.get(key)
    return (record is not None and record[1] is not None
            and (record[2] is None or found >= record[2] / 1e9 - _OWN_CLOCK_SLACK))


def _foreign_in(path: str, own: Dict[str, _Own]) -> bool:
    """Whether *path*, which the session's own write removed, holds now what
    its own writes did not put there -- its own writing into the folder
    again, or moving it back, is not news."""
    if not os.path.isdir(path):
        return True
    prefix = _key(path) + os.sep
    ours = False
    for found in islice(Path(path).rglob("*"), _OWN_TREE_FILES):
        if found.is_file():
            record = own.get(_key(found))
            if record is None or record[1] is None:
                return True
            ours = True
    # Empty: its own if it put something in there and took it away again.
    return not ours and not any(key.startswith(prefix) for key in own)


def _own_divergence(own: Dict[str, _Own]) -> List[Tuple[str, str]]:
    """(kind, path) where the disk no longer holds what the session's own
    writes left -- what the journal cannot see when the change undid itself
    against the index: the user restoring a file the agent deleted, or taking
    the agent's edit back, before a pass looked. A stat each; a read only when
    the mtime moved."""
    out: List[Tuple[str, str]] = []
    for path, digest, mtime in own.values():
        if digest is None:
            if os.path.lexists(path) and _foreign_in(path, own):
                out.append(("added", path))
            continue
        try:
            now = os.stat(path).st_mtime_ns
        except OSError:
            out.append(("removed", path))
            continue
        if now != mtime and _file_digest(Path(path)) != digest:
            out.append(("modified", path))
    return out


def _due_turn(messages: List[Any]) -> Optional[Tuple[int, int]]:
    """(index of the turn head, index of the previous one) when the notice is due.

    Due once per turn: the newest turn head has neither a reply of the model
    nor a notice behind it yet. NOT "the last message is the user's":
    tool_preload appends its results behind the head first, and its tool
    message carries no marker. None for the first turn -- nothing the agent
    saw can be out of date yet.
    """
    heads = [i for i, message in enumerate(messages) if opens_a_turn(message)]
    if len(heads) < 2:
        return None
    head, previous = heads[-1], heads[-2]
    for message in messages[head + 1:]:
        marker = field_of(message, "injected_by")
        if marker == _NOTICE_BY or (role_of(message) == ASSISTANT and not marker):
            return None
    return head, previous


def _message_time(message: Any) -> Optional[float]:
    """Wall-clock seconds of a message's timestamp (the loop stamps in UTC)."""
    stamp = field_of(message, "timestamp")
    if isinstance(stamp, str):
        try:
            stamp = datetime.fromisoformat(stamp)
        except ValueError:
            return None
    if stamp is None or not hasattr(stamp, "timestamp"):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


async def _end_or_error(status, result: Dict[str, Any], message: str,
                        meta: Dict[str, Any]) -> None:
    """Close the status scope according to what `result` actually says.

    The reading tools ended unconditionally, so a failure that operations.py
    or search.py RETURNS (rather than raises) closed the scope with a healthy
    line -- "Read config.yaml: 0/0 lines" for a file that could not be read.
    Only the raising paths were reported truthfully.
    """
    if status is None:
        return
    if isinstance(result, dict) and result.get("status") == "error":
        await status.error(
            result.get("error", "Unknown error"),
            meta={**meta, "error_type": result.get("error_type", "UnknownError")},
        )
        return
    await status.end(message, meta=meta)


class FileOpsServer(SchemaBasedToolServer):
    """tool server providing secure file operations with search capabilities."""

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig):
        """
        Initialize file operations server.

        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            server_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, server_config)

        # Extract configuration
        # A missing key opens nothing, as an empty list does. It used to open
        # src, docs, tests and tmp read-write: an instance defined without the
        # key let the agent write a plugin under src/plugins/ that the next
        # start loads.
        allowed_dirs = getattr(server_config, "allowed_directories", None)
        if allowed_dirs is None:
            logger.warning("%s: no allowed_directories configured -- every path is refused; "
                           "list the folders this instance may use", name)
            allowed_dirs = []
        else:
            # Resolve relative paths relative to project root
            project_root = Path.cwd()
            resolved_dirs = []
            for dir_path in allowed_dirs:
                if not str(dir_path).strip():
                    # pathlib reads "" as "." -- an empty entry would open
                    # the whole folder the CLI was started in.
                    logger.warning("allowed_directories: empty entry ignored")
                    continue
                # "./" and ".\" are the same "." -- compared as text they
                # quietly meant the project instead.
                if Path(dir_path) == Path("."):
                    # "." is where the PERSON stands, not where the process
                    # runs. Both CLIs enter the project at startup, so the
                    # working directory is the checkout -- allowing "." is
                    # how an agent is let into the directory it was started
                    # in, which is the whole point of typing it. Started from
                    # the project, as everything was until now, the two are
                    # the same directory and nothing changes.
                    resolved_dirs.append(str(launch_dir()))
                else:
                    # Resolve relative to project root
                    resolved_path = (project_root / dir_path).resolve()
                    resolved_dirs.append(str(resolved_path))
            allowed_dirs = resolved_dirs

        # Initialize components
        self.validator = PathValidator(allowed_dirs)
        
        # Read-only mode flag
        self.read_only = getattr(server_config, "read_only", False)
        if self.read_only:
            logger.info("FileOperationsServer running in READ-ONLY mode")
        
        # Get file reading limits from config
        max_unpaginated_kb = getattr(server_config, "max_unpaginated_file_size_kb", 100)
        default_line_limit = getattr(server_config, "default_line_limit", 500)
        self.operations = FileOperations(
            max_unpaginated_kb=max_unpaginated_kb,
            default_line_limit=default_line_limit
        )

        # Initialize search engine with configuration
        search_config = getattr(server_config, "search", {})

        # Off unless an instance asks for it: it is the only feature that
        # builds an index and persists a vector store.
        if "enable_semantic_search" not in search_config:
            search_config["enable_semantic_search"] = False
            logger.info("Semantic search off by default (search.enable_semantic_search)")

        # One collection per INSTANCE, because two instances are two trees.
        # Sharing the default name meant the second instance's full rebuild
        # cleared the first one's index. Instances that deliberately share a
        # tree (a read-write and a read-only twin) can share the index by
        # setting the same `collection_name` — then only one of them needs to
        # build it.
        search_config.setdefault("collection_name", f"file_ops_{name}")

        self.search_engine = FileSearchEngine(self.validator.allowed_dirs, search_config)
        #: session id -> what the changed-files notice has told it (on_pre_llm_call),
        #: the most recently used last
        self._notice_state: Dict[str, _NoticeState] = {}
        #: Own writes of sessions no notice has looked after (yet): kept
        #: aside, where only others like them push them out. The hook takes
        #: a session's over on its first call -- tool_preload's writes in that
        #: same call come before it.
        self._unclaimed: Dict[str, _NoticeState] = {}
        # NOT started here. Building the index at construction looks tempting --
        # the instance that owns an index is not always the one that searches
        # it -- but measured on this machine it started four background builds
        # at once, one per instance that has semantic search configured
        # (file_ops, agent_file_ops, coder_fs), for trees nobody had
        # asked a question about yet. The first semantic_search starts it.

        logger.info(f"FileOperationsServer initialized with {len(allowed_dirs)} allowed directories")
        logger.info(f"Search indexing: {search_config.get('enable_indexing', True)}")

    def get_template_vars(self) -> Dict[str, Any]:
        """Provide template variables for schema.yaml rendering."""
        vars = super().get_template_vars()
        vars['read_only'] = self.read_only
        # What read_file promises the model is what it does with THIS instance's limits.
        vars['max_unpaginated_kb'] = self.operations.max_unpaginated_size // 1024
        vars['default_line_limit'] = self.operations.default_line_limit
        # The folders as a relative path names them: "Absolute path" here made models invent one
        # (/workspace/data/..., /home/user/data/...), and no prompt can name the machine's.
        # Escaped for the double-quoted YAML string it goes into: a folder outside the project is
        # absolute, and a Windows one is all backslashes.
        vars['allowed'] = json.dumps(self.validator.describe_roots())[1:-1]
        return vars

    def file_access_roots(self) -> list[Path]:
        """The directories this instance's tools may touch, resolved.

        The question "which files does this agent work on" is asked by name:
        a tool server that answers ``file_access_roots()`` is a file tool --
        project_instructions reads the project's AGENTS.md from here. A name
        of its own, because ``allowed_roots`` already means something else
        elsewhere (a property of media_ops). The answer is the sandbox the
        tools enforce, not a second reading of the config that could drift
        from it.
        """
        return self.validator.allowed_dirs

    def _similar_paths(self, file_path: Any, count: int = 3) -> list[str]:
        """Up to ``count`` entries of a missing file's folder whose names are close to its own -- only in a folder
        the sandbox lets the agent see. Nothing when there is none or the folder is not there."""
        if not isinstance(file_path, str) or not file_path:
            return []
        # The whole path first: the parent of a Git Bash path (/c/Users/...) is no longer one it converts.
        wanted = Path(from_git_bash(file_path))
        try:
            folder = self.validator.validate_path(str(wanted.parent), must_exist=True)
            # A secret is no file to point at, whatever its name; only a name that can be one is resolved.
            names = [entry.name for _, entry in zip(range(5000), folder.iterdir())
                     if not (might_be_private(entry.name) and is_secret(entry))]
        except (SecurityError, OSError):
            return []
        # In the drive form: "/c/Users" as a parent became "\c\Users", a path no tool resolves.
        return [str(wanted.parent / name)
                for name in difflib.get_close_matches(wanted.name, names, n=count, cutoff=0.6)]

    async def read_file(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Read text file contents with pagination."""
        status = params.get("_status")

        try:
            file_path = _required(params, "filePath")
            # Copilot uses 1-indexed offset
            offset = _int_param(params, "offset", 0)
            if offset > 0:
                offset -= 1  # Convert 1-indexed to 0-indexed for internal use
            limit = _int_param(params, "limit", None, low=1)
            encoding = "utf-8"  # Always UTF-8

            if status:
                await status.progress(f"Reading: {Path(file_path).name}")

            # Validate path
            safe_path = self.validator.validate_path(file_path, must_exist=True)

            if not safe_path.is_file():
                return {
                    "status": "error",
                    "error": f"Path is not a file: {file_path}",
                    "error_type": "NotAFileError"
                }

            # Read file
            result = await self.operations.read_file_safe(
                safe_path, offset=offset, limit=limit, encoding=encoding
            )

            if status:
                lines_read = result.get("lines_read", 0)
                total_lines = result.get("total_lines", 0)
                await _end_or_error(
                    status, result,
                    f"Read {safe_path.name}: {lines_read}/{total_lines} lines", meta={
                        "file": str(safe_path),
                        "lines_read": lines_read,
                        "total_lines": total_lines
                    })

            return result

        except FileNotFoundError:
            # 'filePath' is the parameter name (line 99) -- 'file_path' never
            # existed here, so this message always read "File not found: None".
            error_msg = f"File not found: {params.get('filePath')}"
            if status:
                await status.error(error_msg, meta={"error_type": "FileNotFoundError"})
            similar = self._similar_paths(params.get("filePath"))
            return {
                "status": "error",
                "error": f"{error_msg}. Did you mean: {', '.join(similar)}?" if similar else error_msg,
                "error_type": "FileNotFoundError"
            }
        except SecurityError as e:
            error_msg = str(e)
            if status:
                await status.error(error_msg, meta={"error_type": "SecurityError"})
            return {
                "status": "error",
                "error": error_msg,
                "error_type": "SecurityError"
            }
        except Exception as e:
            _log_failure("read_file", e)
            if status:
                await status.error(f"Unexpected error: {e}", meta={"error_type": type(e).__name__})
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    async def manage(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Unified file/directory management: create, delete, move, rename.

        Operations:
        - create: Create new file with content (fails if exists)
        - delete: Delete file or directory (recursive option for non-empty dirs)
        - move: Move file/directory to new location
        - rename: Rename file/directory (same directory)
        """
        # Safety check: block write operations in read-only mode
        if self.read_only:
            return {
                "status": "error",
                "error": "File operations server is in read-only mode. Write operations are disabled.",
                "error_type": "ReadOnlyError"
            }

        status = params.get("_status")
        operation = params.get("operation")
        path = params.get("path")

        if not operation:
            return {
                "status": "error",
                "error": "Parameter 'operation' is required",
                "error_type": "ValidationError"
            }

        if not path:
            return {
                "status": "error",
                "error": "Parameter 'path' is required",
                "error_type": "ValidationError"
            }

        try:
            if operation == "create":
                return await self._manage_create(params, status)
            elif operation == "delete":
                return await self._manage_delete(params, status)
            elif operation == "move":
                return await self._manage_move(params, status)
            elif operation == "rename":
                return await self._manage_rename(params, status)
            else:
                return {
                    "status": "error",
                    "error": f"Unknown operation: {operation}. Valid: create, delete, move, rename",
                    "error_type": "ValidationError"
                }

        except FileNotFoundError as e:
            # A path that is not there is the model's mistake, not ours: it
            # used to land below, logged with a traceback as "Unexpected error".
            if status:
                await status.error(str(e), meta={"error_type": "FileNotFoundError"})
            return {
                "status": "error",
                "error": str(e),
                "error_type": "FileNotFoundError"
            }
        except SecurityError as e:
            error_msg = str(e)
            if status:
                await status.error(error_msg, meta={"error_type": "SecurityError"})
            return {
                "status": "error",
                "error": error_msg,
                "error_type": "SecurityError"
            }
        except Exception as e:
            _log_failure(f"manage ({operation})", e)
            if status:
                await status.error(f"Unexpected error: {e}", meta={"error_type": type(e).__name__})
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    def _refuse_root(self, path: Path, operation: str) -> None:
        """SecurityError when ``path`` is one of the allowed directories itself.

        The roots are the sandbox, not something in it: `delete . recursive`
        on an instance allowed "." removed the whole folder the person had
        started in, and a moved or renamed root takes the boundary with it.
        """
        if path in self.validator.allowed_dirs:
            raise SecurityError(f"Cannot {operation} {path}: it is one of the allowed "
                                f"directories itself. Work on what is inside it.")

    async def _manage_create(self, params: Dict[str, Any], status) -> Dict[str, Any]:
        """Create operation: create a file, or replace it whole with overwrite."""
        path = _required(params, "path")
        content = params.get("content")
        overwrite = _bool_param(params, "overwrite")

        if content is None:
            return {
                "status": "error",
                "error": "Parameter 'content' is required for create operation",
                "error_type": "ValidationError"
            }

        if status:
            await status.progress(f"Writing: {Path(path).name}")

        safe_path = self.validator.validate_path(path, write=True)

        result = await self.operations.create_file_safe(
            safe_path,
            content=content,
            overwrite=overwrite,
            create_dirs=True,
            encoding="utf-8"
        )
        if result.get("status") == "success":
            await self._remember_own(params, written=[safe_path])

        # Whether a file was there is what "replaced" says -- the request only
        # says whether it was allowed; a refusal must not close with a healthy line.
        bytes_written = result.get("bytes_written", 0)
        await _end_or_error(
            status, result,
            f"{'Replaced' if result.get('replaced') else 'Created'} {safe_path.name}: {bytes_written} bytes",
            meta={"file": str(safe_path), "bytes": bytes_written})

        return result

    async def _manage_delete(self, params: Dict[str, Any], status) -> Dict[str, Any]:
        """Delete operation: delete file or directory."""
        path = _required(params, "path")
        recursive = _bool_param(params, "recursive")

        if status:
            await status.progress(f"Deleting: {Path(path).name}")

        safe_path = self.validator.validate_entry(path)
        self._refuse_root(safe_path, "delete")

        result = await self.operations.delete_path_safe(safe_path, recursive=recursive)
        if result.get("status") == "success":
            await self._remember_own(params, gone=[safe_path])

        if status and result.get("status") == "success":
            path_type = result.get("type", "path")
            await status.end(f"Deleted {path_type}: {safe_path.name}", meta={
                "path": str(safe_path),
                "type": path_type,
                "recursive": recursive
            })

        return result

    async def _manage_move(self, params: Dict[str, Any], status) -> Dict[str, Any]:
        """Move operation: move file/directory to new location."""
        path = _required(params, "path")
        destination = params.get("destination")

        if not destination:
            return {
                "status": "error",
                "error": "Parameter 'destination' is required for move operation",
                "error_type": "ValidationError"
            }

        if status:
            await status.progress(f"Moving: {Path(path).name} → {Path(destination).name}")

        safe_source = self.validator.validate_entry(path)
        self._refuse_root(safe_source, "move")
        safe_dest = self.validator.validate_path(destination, write=True)

        result = await self.operations.move_path_safe(safe_source, safe_dest)
        if result.get("status") == "success":
            await self._remember_own(params, written=[safe_dest], gone=[safe_source])

        if status and result.get("status") == "success":
            path_type = result.get("type", "path")
            await status.end(f"Moved {path_type}: {safe_source.name} → {safe_dest.name}", meta={
                "source": str(safe_source),
                "destination": str(safe_dest),
                "type": path_type
            })

        return result

    async def _manage_rename(self, params: Dict[str, Any], status) -> Dict[str, Any]:
        """Rename operation: rename file/directory (same directory)."""
        path = _required(params, "path")
        new_name = params.get("new_name")

        if not new_name:
            return {
                "status": "error",
                "error": "Parameter 'new_name' is required for rename operation",
                "error_type": "ValidationError"
            }
        # Before any path is built from it: joined to the folder, a name like
        # "\??\UNC\host\share" is a path of its own that the check below resolves.
        if "/" in str(new_name) or "\\" in str(new_name):
            return {
                "status": "error",
                "error": f"new_name must not contain path separators: {new_name}",
                "error_type": "ValidationError"
            }

        if status:
            await status.progress(f"Renaming: {Path(path).name} → {new_name}")

        safe_path = self.validator.validate_entry(path)
        self._refuse_root(safe_path, "rename")

        # Validate destination is also in allowed directories
        new_path = safe_path.parent / new_name
        self.validator.validate_path(str(new_path), write=True)

        result = await self.operations.rename_path_safe(safe_path, new_name)
        if result.get("status") == "success":
            await self._remember_own(params, written=[new_path], gone=[safe_path])

        if status and result.get("status") == "success":
            path_type = result.get("type", "path")
            await status.end(f"Renamed {path_type}: {safe_path.name} → {new_name}", meta={
                "old_path": str(safe_path),
                "new_path": str(new_path),
                "type": path_type
            })

        return result

    async def replace_string_in_file(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Replace exact string match in file (VSCode/Copilot-style).

        This is a convenience wrapper around edit_file with mode='replace'.
        Uses exact string matching - oldString must match precisely including whitespace.
        """
        # Safety check: block write operations in read-only mode
        if self.read_only:
            return {
                "status": "error",
                "error": "File operations server is in read-only mode. Write operations are disabled.",
                "error_type": "ReadOnlyError"
            }

        status = params.get("_status")

        try:
            file_path = _required(params, "filePath")  # Note: Copilot uses camelCase
            old_string = _required(params, "oldString")
            new_string = _required(params, "newString")

            if status:
                await status.progress(f"Replacing in: {Path(file_path).name}")

            # Validate path
            safe_path = self.validator.validate_path(file_path, must_exist=True, write=True)

            result = await self.operations.edit_file_safe(
                safe_path,
                old_string=old_string,
                new_string=new_string
            )

            # Check if operation failed
            if result.get("status") == "error":
                if status:
                    await status.error(result.get("error", "Unknown error"), meta={
                        "error_type": result.get("error_type", "UnknownError"),
                        "file": str(safe_path)
                    })
                return result

            # Success
            await self._remember_own(params, written=[safe_path])
            if status:
                changes = result.get("changes", {})
                replacements = changes.get("replacements", 0)
                await status.end(f"Replaced in {safe_path.name}: {replacements} occurrence(s)", meta={
                    "file": str(safe_path),
                    "replacements": replacements
                })

            return result

        except FileNotFoundError:
            error_msg = f"File not found: {params.get('filePath')}"
            if status:
                await status.error(error_msg, meta={"error_type": "FileNotFoundError"})
            similar = self._similar_paths(params.get("filePath"))
            return {
                "status": "error",
                "error": f"{error_msg}. Did you mean: {', '.join(similar)}?" if similar else error_msg,
                "error_type": "FileNotFoundError"
            }
        except SecurityError as e:
            error_msg = str(e)
            if status:
                await status.error(error_msg, meta={"error_type": "SecurityError"})
            return {
                "status": "error",
                "error": error_msg,
                "error_type": "SecurityError"
            }
        except Exception as e:
            _log_failure("replace_string_in_file", e)
            if status:
                await status.error(f"Unexpected error: {e}", meta={"error_type": type(e).__name__})
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    async def list_directory(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """List directory contents."""
        status = params.get("_status")

        try:
            dir_path = _required(params, "dir_path")
            recursive = _bool_param(params, "recursive")
            pattern = params.get("pattern")
            include_hidden = _bool_param(params, "include_hidden")

            if status:
                msg = f"Listing: {Path(dir_path).name}"
                if pattern:
                    msg += f" ({pattern})"
                await status.progress(msg)

            # Validate path
            safe_path = self.validator.validate_path(dir_path, must_exist=True)

            # List directory
            result = await self.operations.list_directory_safe(
                safe_path,
                recursive=recursive,
                pattern=pattern,
                include_hidden=include_hidden,
                max_results=_int_param(params, "max_results", 200, low=1, high=1000),
                include_ignored=_bool_param(params, "include_ignored"),
                excludes=self.search_engine._search_backend_options()["excludes"],
            )

            if status:
                total_files = result.get("total_files", 0)
                total_dirs = result.get("total_directories", 0)
                await _end_or_error(
                    status, result,
                    f"Listed {safe_path.name}: {total_files} files, {total_dirs} directories", meta={
                        "directory": str(safe_path),
                        "total_files": total_files,
                        "total_directories": total_dirs,
                        "recursive": recursive
                    })

            return result

        except FileNotFoundError:
            error_msg = f"Directory not found: {params.get('dir_path')}"
            if status:
                await status.error(error_msg, meta={"error_type": "DirectoryNotFoundError"})
            return {
                "status": "error",
                "error": error_msg,
                "error_type": "DirectoryNotFoundError"
            }
        except SecurityError as e:
            error_msg = str(e)
            if status:
                await status.error(error_msg, meta={"error_type": "SecurityError"})
            return {
                "status": "error",
                "error": error_msg,
                "error_type": "SecurityError"
            }
        except Exception as e:
            _log_failure("list_directory", e)
            if status:
                await status.error(f"Unexpected error: {e}", meta={"error_type": type(e).__name__})
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    async def search_files(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Search files by glob pattern."""
        status = params.get("_status")

        try:
            pattern = _required(params, "pattern")
            max_results = _int_param(params, "max_results", 50, low=1, high=500)
            include_ignored = _bool_param(params, "include_ignored")

            if status:
                await status.progress(f"Searching files: {pattern}")

            result = await self.search_engine.search_files(
                pattern, max_results, include_ignored=include_ignored)

            if status:
                found = result.get("total_found", 0)
                await _end_or_error(
                    status, result,
                    f"File search '{pattern}': {found} matches", meta={
                        "pattern": pattern,
                        "found": found,
                        "truncated": result.get("truncated", False)
                    })

            return result

        except Exception as e:
            _log_failure("search_files", e)
            if status:
                await status.error(f"Search error: {e}", meta={"error_type": type(e).__name__})
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    async def grep_search(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Search text content across files."""
        status = params.get("_status")

        try:
            query = _required(params, "query")
            is_regex = _bool_param(params, "is_regex")
            include_pattern = params.get("include_pattern")
            case_sensitive = _bool_param(params, "case_sensitive")
            max_results = _int_param(params, "max_results", 100, low=1, high=500)
            context_lines = _int_param(params, "context_lines", 2, low=0, high=10)
            include_ignored = _bool_param(params, "include_ignored")

            if status:
                msg = f"Searching text: '{query[:40]}...'"
                if include_pattern:
                    msg += f" in {include_pattern}"
                await status.progress(msg)

            result = await self.search_engine.grep_search(
                query=query,
                is_regex=is_regex,
                include_pattern=include_pattern,
                case_sensitive=case_sensitive,
                max_results=max_results,
                context_lines=context_lines,
                include_ignored=include_ignored
            )

            if status:
                matches = result.get("total_matches", 0)
                files = result.get("total_files", 0)
                await _end_or_error(
                    status, result,
                    f"Grep '{query[:40]}': {matches} matches in {files} files", meta={
                        "query": query[:50],
                        "matches": matches,
                        "files": files,
                        "truncated": result.get("truncated", False)
                    })

            return result

        except Exception as e:
            _log_failure("grep_search", e)
            if status:
                await status.error(f"Grep error: {e}", meta={"error_type": type(e).__name__})
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    async def semantic_search(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Semantic/AI-powered search using embeddings."""
        status = params.get("_status")

        try:
            query = _required(params, "query")
            max_results = _int_param(params, "max_results", 10, low=1, high=50)
            filter_pattern = params.get("filter_pattern")

            if status:
                msg = f"Semantic search: '{query[:40]}...'"
                if filter_pattern:
                    msg += f" ({filter_pattern})"
                await status.progress(msg)

            result = await self.search_engine.semantic_search(
                query=query,
                max_results=max_results,
                filter_pattern=filter_pattern
            )

            if status:
                count = result.get("count", 0)
                await _end_or_error(
                    status, result,
                    f"Semantic search '{query[:40]}': {count} matches", meta={
                        "query": query[:50],
                        "count": count,
                        "filter": filter_pattern
                    })

            return result

        except Exception as e:
            _log_failure("semantic_search", e)
            if status:
                await status.error(f"Semantic search error: {e}", meta={"error_type": type(e).__name__})
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    # ------------------------------------------------------------------
    # Hook: which files changed since the agent's last turn (opt-in per agent)
    # ------------------------------------------------------------------

    async def on_pre_llm_call(self, context: Any) -> Any:
        """changed_files_notice: before the first LLM call of a turn, tell the
        agent which files in its workspace were added, changed or removed since
        its previous turn -- by the user, another agent or a command.

        Without it an agent works on from what it read earlier: a file the
        user copied in is unknown to it, one the user edited is quoted from
        memory. The changes come from the semantic index's passes (its journal,
        see search.py), so the hook needs an instance that builds one.

        What counts is when a pass FOUND a change, on the clock that also
        stamps the messages. File times would not do: copying over a file can
        give it an OLDER mtime, and edits made while the agent worked would
        fall before its reply. Each finding is told once per session; the
        boundary below which findings count as told moves only as far as the
        journal is known to be complete (another process's pass reaches it
        only when this one next reads the state). What this session's own
        write tools did is left out while the file still holds what they left
        (by content: an own edit and the user's before the next pass are one
        finding, and the user's content makes it news); changes the agent made
        through a shell are listed too.

        Once per turn, appended as a developer note behind what the turn
        already holds (tool_preload's results included): append-only, so the
        cached prefix stays as it was.
        """
        from agent_system.hooks import HookResult
        from agent_system.llm.models import ChatMessage

        unchanged = HookResult(success=True, modified=False, context=context)
        try:
            messages = list(getattr(context, "messages", None) or [])
            session = getattr(context, "session_id", None) or ""
            # From the first call on, so the session's own writes are kept
            # with the sessions a notice looks after.
            state = self._notice_session(session)
            turn = _due_turn(messages)
            if turn is None:
                return unchanged
            head, previous = turn
            engine = self.search_engine._owner() or self.search_engine
            if not engine.config.get("enable_semantic_search", True):
                return unchanged
            if state.since is None:
                # Not known (a new process): from the last notice's look, which
                # it carries as its timestamp -- else from the previous turn's
                # head, before which nothing of this session was looked at.
                since = next((_message_time(m) for m in reversed(messages[:head])
                              if field_of(m, "injected_by") == _NOTICE_BY), None)
                if since is None:
                    since = _message_time(messages[previous])
                if since is None:
                    return unchanged
                state.since = since
            if not engine._vector_store_initialized:
                engine._init_vector_store()
            engine._ensure_indexing_started()
            stale = await engine.freshen()
            now = time.time()

            asked_from = state.since
            findings = [entry for entry in engine.findings_after(asked_from)
                        if entry not in state.shown]
            own = dict(state.own)         # a write of the session may land meanwhile

            def judge() -> Tuple[List[Tuple[float, str, str]], Dict[str, Optional[_Own]]]:
                news = [entry for entry in findings if not _is_own(entry, own)]
                listed = {_key(entry[2]) for entry in news}
                news.extend((now, kind, path) for kind, path in _own_divergence(own)
                            if _key(path) not in listed)
                # Someone else moved a path the session wrote on: told now,
                # what it holds now is what the agent knows of it -- a later
                # finding of the same is not news again, a further change is.
                # A folder it removed keeps its record (its own removals in
                # there may still be found late); what it holds now is known
                # from here on.
                told: Dict[str, _Own] = {}
                known: List[Tuple[str, _Own]] = []
                for _, _, path in news:
                    key = _key(path)
                    if key not in own:
                        continue
                    target = Path(own[key][0])
                    if target.is_dir():
                        files = islice((f for f in target.rglob("*") if f.is_file()), _OWN_TREE_FILES)
                        known.extend((_key(f), _own_record(f)) for f in files)
                    else:
                        told[key] = _own_record(target, removed=not os.path.lexists(target))
                return news, told, known

            news, told, known = await asyncio.to_thread(judge)
            for key, record in told.items():
                if state.own.get(key) is own[key]:    # else written again meanwhile: that stands
                    state.own[key] = record
            for key, record in known:
                state.own.setdefault(key, record)
            while len(state.own) > _OWN_LIMIT:
                state.own.pop(next(iter(state.own)))

            # Told (or the agent's own): not again. The boundary moves to
            # where the journal is complete; what lies above it stays listed
            # in `shown` until the boundary passes it.
            since = max(asked_from, min(now, engine._journal_synced_at))
            state.shown = {entry for entry in state.shown | set(findings) if entry[0] > since}
            state.since = since

            changes = latest_per_path(news)
            if not changes:
                return unchanged
            hook_config = getattr(context, "hook_config", None) or {}
            # Against where this look asked from, not where the boundary moved.
            incomplete = stale is not None or engine._journal_trimmed_until > asked_from
            text = self._changed_files_notice(changes, incomplete,
                                              hook_config.get("max_listed", 30))
            context.messages = messages + [ChatMessage(
                role=DEVELOPER, content=text, injected_by=_NOTICE_BY,
                timestamp=datetime.fromtimestamp(now, timezone.utc))]
            return HookResult(success=True, modified=True, context=context,
                              metadata={"changed_files": len(changes)})
        except Exception as e:  # noqa: BLE001 - a notice must never sink the call
            logger.warning("%s: changed-files notice skipped: %s", self.name, e, exc_info=True)
            return unchanged

    def _notice_session(self, session: str) -> _NoticeState:
        """The notice state of *session*, made the most recently used."""
        state = (self._notice_state.pop(session, None) or self._unclaimed.pop(session, None)
                 or _NoticeState())
        self._notice_state[session] = state
        while len(self._notice_state) > _NOTICE_SESSIONS:
            self._notice_state.pop(next(iter(self._notice_state)))
        return state

    async def _remember_own(self, params: Dict[str, Any], written=(), gone=()) -> None:
        """What a successful write of this instance's tools left, per session:
        the changed-files notice leaves out a finding the file still matches.

        Per session, because sessions share an instance: one agent's edit is
        news to another. Kept in memory -- after a restart the agent's last
        edits are listed once as somebody else's. Never raises: the write it
        follows has succeeded."""
        session = params.get("_session_id")
        if not session:
            return

        def digests() -> List[Tuple[str, _Own]]:
            out = [(_key(path), _own_record(path, removed=True)) for path in gone]
            for path in written:
                if path.is_dir():
                    files = list(islice((f for f in path.rglob("*") if f.is_file()),
                                        _OWN_TREE_FILES))
                else:
                    files = [path]
                out.extend((_key(f), _own_record(f)) for f in files)
            return out

        try:
            # Read in a thread (a moved folder may hold thousands of files),
            # applied here on the loop, where the hook reads the same dict.
            updates = await asyncio.to_thread(digests)
        except Exception as e:  # noqa: BLE001 - a lost record only lists the edit
            logger.debug("%s: own write not remembered: %s", self.name, e)
            return
        session = str(session)
        if session in self._notice_state:
            own = self._notice_session(session).own
        else:
            aside = self._unclaimed.pop(session, None) or _NoticeState()
            self._unclaimed[session] = aside
            while len(self._unclaimed) > _NOTICE_SESSIONS:
                self._unclaimed.pop(next(iter(self._unclaimed)))
            own = aside.own
        for key, value in updates:
            if value[1] is None:
                # Removed: what its own writes left below is gone with it.
                below = key + os.sep
                for inner in [k for k in own if k.startswith(below)]:
                    del own[inner]
            own.pop(key, None)
            own[key] = value
        fresh = {key for key, _ in updates}
        while len(own) > _OWN_LIMIT:
            oldest = next(iter(own))
            if oldest in fresh:
                break                     # the rest is this write's: all of it holds
            own.pop(oldest)

    def _changed_files_notice(self, changes: List[Tuple[str, str]],
                              incomplete: bool, max_listed: Any) -> str:
        try:
            limit = max(1, int(max_listed))
        except (TypeError, ValueError):
            limit = 30
        roots = sorted((Path(r) for r in self.validator.allowed_dirs),
                       key=lambda root: len(root.parts), reverse=True)

        def shown(path: str) -> str:
            for root in roots:
                try:
                    return Path(path).relative_to(root).as_posix()
                except ValueError:
                    continue
            return path

        lines = []
        listed = 0
        for kind, label in (("added", "added"), ("modified", "changed"), ("removed", "removed")):
            paths = [shown(path) for k, path in changes if k == kind][:max(0, limit - listed)]
            if paths:
                lines.append(f"{label}: {', '.join(paths)}")
                listed += len(paths)
        text = ("Files in the workspace changed since your previous turn, other than "
                "through your own file edits (the user, another agent or a command):\n"
                + "\n".join(lines))
        if len(changes) > listed:
            text += (f"\n...and {len(changes) - listed} more, not listed -- "
                     "search_files or grep_search finds them.")
        if incomplete:
            text += ("\nThe workspace index is still catching up or has forgotten older "
                     "changes, so this list may be incomplete.")
        return text + "\nRead a file again before relying on what you saw of it earlier."

    async def stop_plugin(self) -> None:
        """Release the semantic index: the framework's ONE teardown hook.

        This used to be called `shutdown`, and nothing ever called it.
        `capabilities.stop_plugin` looks up exactly one attribute and has no
        fallback: `getattr(plugin, "stop_plugin", None)`, otherwise it
        returns. So the ordered stop of the semantic index never happened --
        the background indexer kept running and the vector store was never
        released. At process exit the OS takes both back; on a plugin reload
        inside a living process nobody does, which is the shape the FD leak
        had.

        Being unreachable, it had also rotted: it ended on
        `super().shutdown()`, and no class in the MRO has one -- an
        AttributeError that `capabilities.stop_plugin` swallows into a
        warning. Renaming it is the whole fix; there is no base teardown to
        chain to, and a second name for the same thing is what started this.

        It is defined HERE, on what PLUGIN_FACTORY returns, because that is
        what the adapter reaches: `getattr(adapter, "plugin_server", adapter)`.
        """
        await self.search_engine.stop()


PLUGIN_FACTORY = FileOpsServer
