"""Which paths a tool call is about to change -- asked of the tool server itself.

Only tools whose writes can be named before they happen are recorded, and
each is resolved by the server's OWN sandbox (``file_ops``' PathValidator,
``media_ops``' PathSandbox): the path recorded is the path the tool will
write, not a second reading of the arguments that could drift from it. A
path the sandbox refuses is not recorded -- the tool refuses it too.

Keyed by the server's TYPE, since an instance may be named anything
(``coder_fs`` and ``workspace_file_ops`` are file_ops).

Tools that change files in ways nobody can name beforehand -- a shell
command, Claude Code, a Blender script, a state machine whose activities pass
no hook -- are not recorded but COUNTED (``UNTRACKED``): a rewind says which
of them ran, so nobody takes "restored" for "as it was".
"""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

#: Tools that change files without naming them, by server type ("*": all its tools).
UNTRACKED: Dict[str, FrozenSet[str]] = {
    "terminal": frozenset({"execute"}),
    "coding_cli": frozenset({"run_task"}),
    "blender": frozenset({"execute", "export"}),
    "godot": frozenset({"setup", "import_assets", "script", "export", "scene", "node", "command"}),
    "audio_ops": frozenset({"cut", "merge", "create", "mix", "volume", "compress_silence"}),
    "image_compose": frozenset({"render"}),
    "comfyui": frozenset({"workflow"}),
    "sqlite_query": frozenset({"execute_sql"}),
    # A machine's tool: activities are called by the framework and pass no hook.
    "stategraph": frozenset({"run_machine", "send_event", "control_run"}),
}


@dataclass(frozen=True)
class Target:
    """One path a call changes."""

    path: Path
    #: A directory whose entries go with it (recursive delete, the source of a move).
    with_contents: bool = False
    #: Where the entries of ``path`` land (the destination of a directory move).
    contents_to: Optional[Path] = None


@dataclass
class Plan:
    """What a call changes: the paths, and the directories it may create."""

    targets: List[Target] = field(default_factory=list)


def server_instance(agent: Any, server: str) -> Any:
    registry = getattr(agent, "registry", None)
    if registry is None or not server or "." in server:
        return None
    try:
        return registry.get(server)
    except KeyError:
        return None
    except Exception:  # noqa: BLE001 - a registry that cannot answer names no server
        logger.debug("file_checkpoints: no server %s", server, exc_info=True)
        return None


def server_type(agent: Any, server: str, instance: Any) -> Optional[str]:
    """The plugin type of ``server``: from the config it was built with, else
    from the configuration system (as tool_approval reads it)."""
    declared = getattr(getattr(instance, "server_config", None), "type", None)
    if isinstance(declared, str) and declared:
        return declared
    system_config = getattr(agent, "system_config", None)
    if system_config is None or not server:
        return None
    try:
        from agent_system.config.settings import get_tool_server_config
        return getattr(get_tool_server_config(server, system_config), "type", None)
    except Exception:  # noqa: BLE001 - an unknown server has no type
        return None


def tool_of(server: str, name: str) -> str:
    """The tool's own name: ``coder_fs_manage`` -> ``manage``."""
    prefix = f"{server}_"
    return name[len(prefix):] if name.startswith(prefix) else name


def server_roots(instance: Any) -> Tuple[Path, ...]:
    """The directories a file tool server may touch, resolved -- and spelled the
    way their directories are stored, like every recorded path: a root
    configured as "bücher" for a folder stored as "Bücher" (case, NFC/NFD) put
    every path of that server outside it."""
    from .fs import spelled_on_disk

    roots: Any = ()
    for attribute in ("file_access_roots", "allowed_roots"):
        value = getattr(instance, attribute, None)
        if callable(value):
            value = value()
        if value:
            roots = value
            break
    else:
        roots = getattr(getattr(instance, "sandbox", None), "roots", ()) or ()
    listings: dict = {}
    return tuple(spelled_on_disk(Path(root).resolve(), listings) for root in roots)


def _missing_parents(path: Path) -> List[Target]:
    """The directories a write creates on its way to ``path`` (outermost first)."""
    missing: List[Path] = []
    parent = path.parent
    while parent != parent.parent and not parent.exists():
        missing.append(parent)
        parent = parent.parent
    return [Target(p) for p in reversed(missing)]


def _file_ops_plan(instance: Any, tool: str, arguments: Mapping[str, Any]) -> Optional[Plan]:
    if getattr(instance, "read_only", False):
        return None
    validator = getattr(instance, "validator", None)
    if validator is None:
        return None
    if tool == "replace_string_in_file":
        path = arguments.get("filePath")
        if not isinstance(path, str) or not path:
            return None
        return Plan([Target(validator.validate_path(path, must_exist=True))])
    if tool != "manage":
        return None
    operation = arguments.get("operation")
    path = arguments.get("path")
    if not isinstance(path, str) or not path:
        return None
    if operation == "create":
        target = validator.validate_path(path)
        return Plan(_missing_parents(target) + [Target(target)])
    if operation == "delete":
        return Plan([Target(validator.validate_path(path, must_exist=True), with_contents=True)])
    if operation == "move":
        destination = arguments.get("destination")
        if not isinstance(destination, str) or not destination:
            return None
        source = validator.validate_path(path, must_exist=True)
        target = validator.validate_path(destination)
        return Plan([Target(source, with_contents=True, contents_to=target)]
                    + _missing_parents(target) + [Target(target)])
    if operation == "rename":
        new_name = arguments.get("new_name")
        if not isinstance(new_name, str) or not new_name or "/" in new_name or "\\" in new_name:
            return None
        source = validator.validate_path(path, must_exist=True)
        target = validator.validate_path(str(source.parent / new_name))
        return Plan([Target(source, with_contents=True, contents_to=target), Target(target)])
    return None


def _media_ops_plan(instance: Any, tool: str, arguments: Mapping[str, Any]) -> Optional[Plan]:
    if tool != "save":
        return None
    sandbox = getattr(instance, "sandbox", None)
    path = arguments.get("path")
    if sandbox is None or getattr(sandbox, "read_only", False) or not isinstance(path, str) or not path:
        return None
    target = sandbox.resolve(path, write=True)
    return Plan(_missing_parents(target) + [Target(target)])


_PLANNERS = {"file_ops": _file_ops_plan, "media_ops": _media_ops_plan}


def recorded_types() -> Iterable[str]:
    return _PLANNERS.keys()


def plan_for(kind: Optional[str], instance: Any, tool: str, arguments: Mapping[str, Any]) -> Optional[Plan]:
    """The paths a call of ``tool`` on a server of type ``kind`` changes, or
    None when it changes none that can be named (or the server will refuse it)."""
    planner = _PLANNERS.get(kind or "")
    if planner is None or instance is None:
        return None
    try:
        return planner(instance, tool, arguments)
    except (FileNotFoundError, NotADirectoryError):
        return None           # the tool refuses a path that is not there
    except Exception as exc:  # noqa: BLE001 - the sandbox refused it: so will the tool
        logger.debug("file_checkpoints: %s/%s not recorded: %s", kind, tool, exc)
        return None


def is_untracked(kind: Optional[str], tool: str, extra: Iterable[str] = ()) -> bool:
    """Whether a call of ``tool`` on a server of type ``kind`` changes files
    without naming them. ``extra``: more ``type/tool`` patterns (fnmatch)."""
    if not kind:
        return False
    listed = UNTRACKED.get(kind)
    if listed is not None and ("*" in listed or tool in listed):
        return True
    return any(fnmatch.fnmatchcase(f"{kind}/{tool}", pattern) for pattern in extra)
