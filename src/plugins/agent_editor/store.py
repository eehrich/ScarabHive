"""Agent entries in the YAML files that define them: scan, read, write in place with comments kept, diff, roll back.

A write renders the edited file with ruamel and takes from that rendering only the lines the edit changed; every other
line is copied from the file as it was. So a file ruamel would reformat (trailing blanks, `null`, spacing in a flow
list) stays byte-exact outside the edit. Before anything is written, the result is parsed with the loader's parser
and must equal the intended data.

No FastAPI here: failures are `StoreError` with the HTTP status to answer.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import io
import logging
import math
import os
import re
import stat
import tempfile
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, LiteralScalarString, SingleQuotedScalarString
from ruamel.yaml.tokens import CommentToken

from agent_system.config.models import AgentSystemConfig, MCPConfig
from agent_system.config.settings import _known_plugin_types, config_files, get_mcp_config_by_name, load_settings
from agent_system.plugins.catalog import PluginCatalog
from agent_system.utils import yaml_io
from plugins.sub_agent_manager.server import agent_allowed

logger = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{1,63}")
PROMPT_SUFFIXES = (".md", ".txt", ".markdown")
#: ruamel indent settings (mapping, sequence, offset) tried per file; the one closest to the file is used.
INDENTS = ((2, 4, 2), (2, 2, 0))
CREATED_HEADER = "Created with the Agent Editor."
#: The loader's `${NAME}` expansion.
PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")
#: The plugin type of the sub-agent manager.
MANAGER_TYPE = "sub_agent_manager"
#: Values shorter than this are masked only where they are the whole string.
MIN_MASKED_LENGTH = 8
#: Attempts for a replace or unlink that Windows refuses while another process holds the file.
ATTEMPTS = 5


class StoreError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def error_text(error: Exception) -> str:
    """A validation error as `loc: msg` lines, anything else as its message."""
    if isinstance(error, ValidationError):
        return "\n".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors())
    return str(error)


def placeholders(value: Any) -> set[str]:
    """The `${NAME}` names anywhere in `value` (strings, lists, mappings)."""
    if isinstance(value, str):
        return set(PLACEHOLDER.findall(value))
    if isinstance(value, dict):
        return set().union(*(placeholders(item) for item in value.values()))
    if isinstance(value, (list, tuple)):
        return set().union(*(placeholders(item) for item in value))
    return set()


def hide_environment(text: str, names: set[str]) -> str:
    """`text` with the values of these variables (`MIN_MASKED_LENGTH` characters or more) shown as `${NAME}`.

    For error messages: they quote what the loader expanded -- from the variables the files name, and from those an
    entry that was sent names. Other environment values cannot be in them, and stay readable words.
    """
    values = {name: os.environ[name] for name in names if len(os.environ.get(name, "")) >= MIN_MASKED_LENGTH}
    for name, value in sorted(values.items(), key=lambda item: len(item[1]), reverse=True):
        text = text.replace(value, f"${{{name}}}")
    return text


def jsonable(value: Any) -> Any:
    """`value` as JSON can carry it, for showing: a date or an infinite number as text, every key as text. An entry
    that needs this is not editable in the form (`Store.form_reason`)."""
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def unplain(value: Any, path: str = "yaml") -> Optional[str]:
    """Why `value` does not come back as the same JSON (a date, a set, bytes, NaN, a non-string key), or None."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                return f"{path}: the key {key!r} must be text (quote it)"
            problem = unplain(item, f"{path}.{key}")
            if problem:
                return problem
        return None
    if isinstance(value, list):
        return next((problem for index, item in enumerate(value) if (problem := unplain(item, f"{path}[{index}]"))), None)
    if isinstance(value, float) and not math.isfinite(value):
        return f"{path}: {value} is not a number JSON can carry"
    if value is None or isinstance(value, (str, bool, int, float)):
        return None
    return f"{path}: a {type(value).__name__} is not supported here (quote it)"


class _OtherThreads(logging.Filter):
    """Lets through the records of every thread but the one that made it."""

    def __init__(self) -> None:
        super().__init__()
        self.thread = threading.get_ident()

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread != self.thread


@contextmanager
def quiet_loader():
    """The editor's own config loads without the loader's report: the app logged that at its start and on reload,
    and a save would repeat every config error of the whole tree."""
    settings_logger = logging.getLogger(load_settings.__module__)
    quiet = _OtherThreads()
    settings_logger.addFilter(quiet)
    try:
        yield
    finally:
        settings_logger.removeFilter(quiet)


def servers_of(data: Any) -> dict:
    """`plugins.servers` of one parsed file, {} when the file has none."""
    plugins = data.get("plugins") if isinstance(data, dict) else None
    servers = plugins.get("servers") if isinstance(plugins, dict) else None
    return servers if isinstance(servers, dict) else {}


@lru_cache(maxsize=4)
def plugin_catalog(dirs: tuple[str, ...]) -> PluginCatalog:
    return PluginCatalog(Path(d) for d in dirs)


def catalog_for(config: AgentSystemConfig) -> PluginCatalog:
    """The plugin types of the config's plugin directories: what the runtime can build."""
    return plugin_catalog(tuple(config.plugins.plugin_dirs))


def resolve_with(config: AgentSystemConfig, name: str, entry: dict) -> Optional[MCPConfig]:
    """`name` resolved as if its own entry were `entry`: default_config and the parent chain as in `config`."""
    servers = {**config.plugins.servers, name: MCPConfig.model_validate(entry)}
    patched = config.model_copy(update={"plugins": config.plugins.model_copy(update={"servers": servers})})
    return get_mcp_config_by_name(name, patched)


def parent_of(config: AgentSystemConfig, name: str) -> Optional[str]:
    """The server `name` inherits from, None when its type is a plugin type (the resolver's own rule)."""
    server = config.plugins.servers.get(name)
    parent = server.type if server is not None and "type" in server.model_fields_set else None
    if parent in (None, name) or parent in _known_plugin_types() or parent not in config.plugins.servers:
        return None
    return parent


def base_of(config: AgentSystemConfig, resolved: Optional[MCPConfig], name: str) -> str:
    """The final plugin type; for an entry that does not resolve, the type at the end of its parent chain."""
    if resolved is not None:
        return resolved.type
    seen = {name}
    while (parent := parent_of(config, name)) and parent not in seen:
        seen.add(parent)
        name = parent
    return config.plugins.servers[name].type


def version_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def spawn_rule(name: str, allowed: list[str], blocked: list[str]) -> str:
    """Which list entry decides the manager's answer, in the manager's order: blocked, `*`, exact, pattern."""
    if not agent_allowed(name, ["*"], blocked):
        return f"blocked_agents: {name}"
    for pattern in sorted(allowed, key=lambda p: (p != "*", p != name)):
        if agent_allowed(name, [pattern], []):
            return f"allowed_agents: {pattern}"
    return "not in allowed_agents"


def manager_lists(resolved: Optional[MCPConfig]) -> tuple[list[str], list[str]]:
    """`allowed_agents` and `blocked_agents` with the manager's own defaults."""
    return list(getattr(resolved, "allowed_agents", ["*"])), list(getattr(resolved, "blocked_agents", []))


def _merge_style(items: Optional[list]) -> bool:
    return bool(items) and all(isinstance(item, str) and item.startswith(("+", "!")) for item in items or [])


def _secret_names(config_dir: Path) -> set[str]:
    """The names `secrets.env` defines (the loader puts them into the environment)."""
    try:
        lines = (config_dir / "secrets.env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    names = (line.partition("=")[0].strip() for line in lines if "=" in line and not line.strip().startswith("#"))
    return {name for name in names if name}


@dataclass
class Snapshot:
    config: AgentSystemConfig
    files: list[Path]
    data: dict[Path, Any]
    #: The hash of the bytes each file had when `data` was parsed from them.
    versions: dict[Path, str]
    defined: dict[str, list[Path]]
    errors: list[str]
    resolved: dict[str, Optional[MCPConfig]]
    resolve_errors: dict[str, str]
    #: Expanded variable value -> its `${NAME}` placeholder.
    secrets: dict[str, str]
    #: The variables the config files name, and those `secrets.env` defines.
    names: set[str]

    def expand(self, value: Any) -> Any:
        """`value` with the placeholders of known variables expanded, as the loader reads them; any other stays as
        it is -- its value would be shown unmasked."""
        if isinstance(value, dict):
            return {key: self.expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.expand(item) for item in value]
        if isinstance(value, str):
            return PLACEHOLDER.sub(lambda match: os.environ.get(match[1], "") if match[1] in self.names else match[0],
                                   value)
        return value

    def mask(self, value: Any) -> Any:
        """`value` with every expanded variable shown as its placeholder again."""
        if isinstance(value, dict):
            return {key: self.mask(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.mask(item) for item in value]
        if isinstance(value, str):
            if value in self.secrets:
                return self.secrets[value]
            # ponytail: a short value is masked only as a whole string -- inside other text it would rewrite
            # unrelated words (and an override would write that into a file); a short secret leaks there.
            for secret in sorted(self.secrets, key=len, reverse=True):
                if len(secret) >= MIN_MASKED_LENGTH:
                    value = value.replace(secret, self.secrets[secret])
        return value


class Store:
    def __init__(self, config_path: Path, root: Path):
        self.config_path = Path(config_path)
        self.root = Path(root).resolve()
        # ponytail: one lock for every read and write of the config tree; per-file locks if saves ever queue up.
        self._lock = threading.RLock()
        self._key: Any = None
        self._snapshot: Optional[Snapshot] = None
        self._checked = 0.0
        #: The variables the files named at the last scan, kept when that scan's load failed.
        self._names: set[str] = set()

    # ------------------------------------------------------------------ reading

    def known_names(self) -> set[str]:
        """The variables the files named at the last scan (also one whose load failed), and those `secrets.env`
        defines."""
        return self._names | _secret_names(self.config_path.parent)

    def rel(self, path: Path) -> str:
        resolved = Path(path).resolve()
        return resolved.relative_to(self.root).as_posix() if resolved.is_relative_to(self.root) else resolved.as_posix()

    def snapshot(self) -> Snapshot:
        with self._lock:
            # ponytail: the stamp check parses the master config (~35 ms), so it runs at most once a second; own
            # writes clear the key, edits by others show up within that second.
            if self._snapshot is not None and self._key is not None and time.monotonic() - self._checked < 1.0:
                return self._snapshot
            # An include matched twice is loaded twice, but it is still one file.
            # A file included twice is loaded twice; its later load is the one that wins, so it keeps that place.
            try:
                loaded = [path.resolve() for path in config_files(str(self.config_path))]
            except Exception as error:
                raise StoreError(500, f"The config on disk does not load:\n{error_text(error)}")
            files = list(reversed(dict.fromkeys(reversed(loaded))))
            key = tuple((path, *_stamp(path)) for path in files)
            if key != self._key or self._snapshot is None:
                with quiet_loader():  # get_mcp_config_by_name reports as well (cycles, dropped llm_params)
                    self._snapshot, self._key = self._scan(files), key
            self._checked = time.monotonic()
            return self._snapshot

    def _scan(self, files: list[Path]) -> Snapshot:
        data: dict[Path, Any] = {}
        versions: dict[Path, str] = {}
        defined: dict[str, list[Path]] = {}
        errors: list[str] = []
        names = _secret_names(self.config_path.parent)
        read: list[tuple[Path, bytes, Any]] = []
        for path in files:
            try:
                raw = path.read_bytes()
                text = raw.decode("utf-8")
                names |= set(PLACEHOLDER.findall(text))
                read.append((path, raw, yaml_io.safe_load(text) or {}))
            except Exception as error:  # the loader skips such a file as well
                errors.append(f"{self.rel(path)}: {error}")
        # before the load: its error may quote a value the files name
        self._names = names
        try:
            config = load_settings(str(self.config_path))
        except Exception as error:
            raise StoreError(500, f"The config on disk does not load:\n{error_text(error)}")
        for path, raw, parsed in read:
            if not isinstance(parsed, dict):
                errors.append(f"{self.rel(path)}: not a mapping, the loader skips it")
                continue
            data[path], versions[path] = parsed, version_of(raw)
            for name in servers_of(parsed):
                defined.setdefault(name, []).append(path)
        resolved: dict[str, Optional[MCPConfig]] = {}
        resolve_errors: dict[str, str] = {}
        for name in config.plugins.servers:
            try:
                resolved[name] = get_mcp_config_by_name(name, config)
            except Exception as error:
                resolve_errors[name] = error_text(error)
        secrets = {os.environ[name]: f"${{{name}}}" for name in names if os.environ.get(name)}
        return Snapshot(config, files, data, versions, defined, errors, resolved, resolve_errors, secrets, names)

    def present(self, value: Any, snap: Snapshot) -> Any:
        """A resolved dump as the panel shows it: secrets as placeholders, and templates under the root relative to it.

        The loader turns `./` templates into absolute paths; relative to the root (the working directory the loader
        resolves other templates against) the value stays valid when an override copies it into an entry.
        """
        return jsonable(snap.mask(self._relative_templates(value)))

    def _relative_templates(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._relative_templates(item) for item in value]
        if not isinstance(value, dict):
            return value
        shown = {key: self._relative_templates(item) for key, item in value.items()}
        template = shown.get("system_template")
        if isinstance(template, str) and Path(template).is_absolute() and Path(template).is_relative_to(self.root):
            shown["system_template"] = Path(template).relative_to(self.root).as_posix()
        return shown

    def files_of(self, name: str, snap: Optional[Snapshot] = None) -> list[Path]:
        return (snap or self.snapshot()).defined.get(name, [])

    def readonly_reason(self, name: str, snap: Optional[Snapshot] = None) -> Optional[str]:
        files = self.files_of(name, snap)
        if not files:
            return "not defined in any config file"
        if len(files) > 1:
            return f"defined in {len(files)} files: {', '.join(self.rel(path) for path in files)}"
        return _file_problem(files[0], self.root, _stamp(files[0]))

    def form_reason(self, name: str, snap: Optional[Snapshot] = None) -> Optional[str]:
        """Why the form cannot edit the entry although its file can be written, None when it can: the form carries an
        entry as JSON, and a date or a number key would come back changed. Delete and the manager switches work on
        the parsed file and are not affected."""
        snap = snap or self.snapshot()
        files = snap.defined.get(name)
        plain = unplain(servers_of(snap.data.get(files[-1])).get(name), name) if files else None
        return f"{plain}: the form cannot carry this value, edit the file itself" if plain else None

    def own(self, name: str, snap: Optional[Snapshot] = None) -> Optional[dict]:
        """The entry as written in its (last) file: no `${VAR}` expansion, prefixes and `./` paths kept."""
        snap = snap or self.snapshot()
        files = snap.defined.get(name)
        return copy.deepcopy(servers_of(snap.data[files[-1]])[name]) if files else None

    def version(self, name: str, snap: Optional[Snapshot] = None) -> Optional[str]:
        """The version of the bytes `own` was read from, so a save based on them fails if the file moved on."""
        snap = snap or self.snapshot()
        files = snap.defined.get(name, [])
        return snap.versions[files[0]] if len(files) == 1 else None

    def children(self, name: str, snap: Optional[Snapshot] = None) -> list[str]:
        """Servers whose own `type` names this one."""
        servers = (snap or self.snapshot()).config.plugins.servers
        return sorted(n for n, s in servers.items() if n != name and "type" in s.model_fields_set and s.type == name)

    def descendants(self, name: str, snap: Optional[Snapshot] = None) -> list[str]:
        snap = snap or self.snapshot()
        found: list[str] = []
        pending = self.children(name, snap)
        while pending:
            child = pending.pop()
            if child not in found:
                found.append(child)
                pending += self.children(child, snap)
        return found

    def sams(self, snap: Optional[Snapshot] = None) -> list[str]:
        """Enabled servers whose resolved type is the sub-agent manager."""
        snap = snap or self.snapshot()
        return sorted(name for name, resolved in snap.resolved.items()
                      if resolved is not None and resolved.type == MANAGER_TYPE
                      and snap.config.plugins.servers[name].enabled)

    def spawnable(self, name: str, snap: Optional[Snapshot] = None) -> list[dict]:
        snap = snap or self.snapshot()
        rows = []
        for sam in self.sams(snap):
            allowed, blocked = manager_lists(snap.resolved[sam])
            files = self.files_of(sam, snap)
            rows.append({"sam": sam, "file": self.rel(files[0]) if files else None,
                         "editable": self.readonly_reason(sam, snap) is None, "version": self.version(sam, snap),
                         "allowed": agent_allowed(name, allowed, blocked), "rule": spawn_rule(name, allowed, blocked)})
        return rows

    # ------------------------------------------------------------------ writing

    def save(self, name: str, entry: dict, version: str, dry_run: bool) -> dict:
        with self._lock:
            path = self._editable(name)
            reason = self.form_reason(name)
            if reason:
                raise StoreError(400, f"{name} is read-only here: {reason}")
            return self._change(path, version, name, entry, dry_run)

    def delete(self, name: str, version: str, dry_run: bool) -> dict:
        with self._lock:
            path = self._editable(name)
            snap = self.snapshot()
            children = self.children(name, snap)
            if children:
                raise StoreError(400, f"{name} cannot be deleted while other entries inherit from it: {', '.join(children)}")
            notes = [f"{sam} still lists {name} in allowed_agents" for sam in self.sams(snap)
                     if {name, f"+{name}"} & set((self.own(sam, snap) or {}).get("allowed_agents") or [])]
            return {"deleted_file": False, **self._change(path, version, name, None, dry_run), "notes": notes}

    def set_spawnable(self, name: str, sam: str, allow: bool, version: str, dry_run: bool) -> dict:
        """Let `sam` spawn `name` or not, by editing the manager's own lists as little as possible.

        A list the manager does not have, or has in merge form, gets `+name` / `!name` entries, so what it inherits
        keeps applying; a plain own list gets the bare name.
        """
        with self._lock:
            snap = self.snapshot()
            if sam not in self.sams(snap):
                raise StoreError(404, f"{sam} is not an enabled sub-agent manager")
            path = self._editable(sam)
            config, own = snap.config, self.own(sam, snap)
            if own is None:  # _editable found exactly one file, so this is a race with a delete
                raise StoreError(409, f"{sam} changed on disk; reload")
            allowed_key, blocked_key = "allowed_agents", "blocked_agents"
            for key in (allowed_key, blocked_key):
                if own.get(key) is not None and not isinstance(own[key], list):
                    raise StoreError(400, f"{sam}'s {key} is not a list ({own[key]!r}); change it in {self.rel(path)}")
            lists = {key: list(own[key]) if isinstance(own.get(key), list) else None for key in (allowed_key, blocked_key)}

            def decision() -> tuple[bool, bool, str]:
                entry = {**{k: v for k, v in own.items() if k not in lists}, **{k: v for k, v in lists.items() if v is not None}}
                allowed, blocked = manager_lists(resolve_with(config, sam, entry))
                return (agent_allowed(name, allowed, blocked), not agent_allowed(name, ["*"], blocked),
                        spawn_rule(name, allowed, blocked))

            def take_out(key: str) -> None:
                current = lists[key]
                if current is not None:
                    merge = _merge_style(current)
                    current = [item for item in current if item not in (name, f"+{name}")]
                    # An emptied merge list would read as "replace with nothing" and drop the inherited one.
                    lists[key] = None if merge and not current else current

            def put_in(key: str, entry: str) -> None:
                current = lists[key]
                if current is None or _merge_style(current):
                    lists[key] = [item for item in current or [] if item not in (f"+{name}", f"!{name}")] + [entry]
                elif entry == name:
                    lists[key] = current + [name]

            if allow:
                take_out(blocked_key)
                if decision()[1]:
                    put_in(blocked_key, f"!{name}")  # blocked by the inherited list
                if not decision()[0]:
                    put_in(allowed_key, f"+{name}" if lists[allowed_key] is None or _merge_style(lists[allowed_key]) else name)
            else:
                take_out(allowed_key)
                if decision()[0]:
                    put_in(blocked_key, f"+{name}" if lists[blocked_key] is None or _merge_style(lists[blocked_key]) else name)
            allowed_now, _, rule = decision()
            if allowed_now != allow:
                raise StoreError(400, f"{sam}'s own lists cannot {'allow' if allow else 'block'} {name}: {rule}")
            new = dict(own)
            for key, value in lists.items():
                if value is None:
                    new.pop(key, None)
                else:
                    new[key] = value
            return self._change(path, version, sam, new, dry_run)

    def create(self, name: str, entry: dict, source: Optional[str], dry_run: bool) -> dict:
        with self._lock:
            snap = self.snapshot()
            if not NAME_PATTERN.fullmatch(name):
                raise StoreError(422, f"name: must match {NAME_PATTERN.pattern}")
            if name in snap.config.plugins.servers:
                raise StoreError(400, f"{name} is already a server in the config")
            if name in catalog_for(snap.config).types():
                raise StoreError(400, f"{name} is a plugin type; an entry of that name would shadow it")
            path = (self.config_path.parent / "agents" / f"{name}.yaml").resolve()
            rel = self.rel(path)
            if not path.is_relative_to(self.root):
                raise StoreError(400, f"{rel} is outside the repository root, where the editor cannot edit it")
            if path.exists():
                raise StoreError(400, f"{rel} already exists")
            entry = copy.deepcopy(entry)
            if source:
                if not snap.defined.get(source):
                    raise StoreError(404, f"source agent {source} is not defined in any config file")
                reason = self.form_reason(source, snap)
                if reason:
                    raise StoreError(400, f"{source} cannot be copied here: {reason}")
                _rebase_templates(entry, snap.defined[source][-1].parent, path.parent)
            doc = CommentedMap(plugins=CommentedMap(servers=CommentedMap({name: to_node(entry)})))
            doc.yaml_set_start_comment(CREATED_HEADER)
            text = dump(doc, INDENTS[0])
            _check_parse(text, {"plugins": {"servers": {name: entry}}})
            result = {"name": name, "file": rel, "diff": unified_diff("", text, rel)}
            if dry_run:
                return result
            path.parent.mkdir(parents=True, exist_ok=True)
            data = text.encode("utf-8")
            self._put(path, data, create=True)
            try:
                if path not in {p.resolve() for p in config_files(str(self.config_path))}:
                    raise StoreError(422, f"{self.config_path.name} does not include agents/*.yaml, so {rel} would not be read")
                self._verify(snap.config, [name])
            except Exception as error:
                self._roll_back(path, data, None, error)
            return {**result, "version": version_of(data)}

    def _editable(self, name: str) -> Path:
        snap = self.snapshot()
        files = self.files_of(name, snap)
        if not files:
            raise StoreError(404, f"{name} is not defined in any config file")
        reason = self.readonly_reason(name, snap)
        if reason:
            raise StoreError(400, f"{name} is read-only: {reason}")
        return files[0]

    def _change(self, path: Path, version: str, name: str, entry: Optional[dict], dry_run: bool) -> dict:
        """Set (`entry`) or remove (None) one server in `path`; dry run answers the diff only."""
        rel = self.rel(path)
        raw = self._read_for_change(path)
        if raw is None or version != version_of(raw):
            self._key = None  # the conflict proves the snapshot stale: the reload that follows must see the disk
            raise StoreError(409, f"{rel} changed on disk; reload")
        old = raw.decode("utf-8")
        crlf = "\r\n" in old
        src = old.replace("\r\n", "\n")
        before = yaml_io.safe_load(src) or {}
        expected = copy.deepcopy(before)
        servers = servers_of(expected)
        if entry is None:
            del servers[name]
        else:
            servers[name] = entry

        def edit(doc: CommentedMap) -> None:
            node = doc["plugins"]["servers"]
            if entry is None:
                del node[name]
            else:
                update_node(node, name, servers_of(before)[name], entry)

        empty = False
        removal = _remove_entry(src, name) if entry is None else None
        if removal and _parses_to(removal[0], expected):
            new, empty = removal
            new = "" if empty else new
        else:
            new = render(src, edit, expected)
        result: dict = {"diff": unified_diff(src, new, rel)}
        if empty:
            result["deleted_file"] = True
        if dry_run:
            return result
        if not result["diff"]:
            return {**result, "version": version}
        snap = self.snapshot()
        checks = self.descendants(name, snap) + ([] if entry is None else [name])
        data = None if empty else (new.replace("\n", "\r\n") if crlf else new).encode("utf-8")
        # the rendering and the snapshot take a while: a save by someone else meanwhile is not overwritten
        if self._read_for_change(path) != raw:
            self._key = None
            raise StoreError(409, f"{rel} changed on disk; reload")
        # ponytail: the invalid file is on disk for the ~0.1 s of the check below; validate a temp tree if a reader
        # ever trips over that window. The re-read above leaves a window of microseconds.
        self._put(path, data)
        try:
            self._verify(snap.config, checks)
        except Exception as error:
            self._roll_back(path, data, raw, error)
        return result if data is None else {**result, "version": version_of(data)}

    def _read_for_change(self, path: Path) -> Optional[bytes]:
        try:
            return _read(path)
        except OSError as error:
            raise StoreError(400, f"{self.rel(path)} cannot be read: {error}")

    def _put(self, path: Path, data: Optional[bytes], create: bool = False) -> None:
        """Write `data` atomically, or delete the file for None; `create` makes a new file and never replaces one."""
        self._key = None  # a rollback restores the same size, possibly within the same mtime tick: rescan next time
        try:
            if data is None:
                _retry(path.unlink)
            elif create:
                handle = path.open("xb")
                try:
                    with handle:
                        handle.write(data)
                except BaseException:
                    try:
                        _retry(lambda: path.unlink(missing_ok=True))
                    except OSError as failure:
                        logger.error("Agent editor: the partial file %s could not be removed: %s", path, failure)
                    raise
            else:
                atomic_write(path, data)
        except FileExistsError:
            raise StoreError(409, f"{self.rel(path)} was created meanwhile; reload")
        except OSError as error:
            raise StoreError(400, f"{self.rel(path)} cannot be written: {error}")

    def _roll_back(self, path: Path, written: Optional[bytes], original: Optional[bytes], error: Exception) -> None:
        """Put back what `path` held before our write, unless someone changed it meanwhile; re-raise the reason."""
        rel = self.rel(path)
        reason = error if isinstance(error, StoreError) else StoreError(500, f"the check after writing failed: {error}")
        try:
            current = _read(path)
            if current == written:
                self._put(path, original)
        except (OSError, StoreError) as failure:
            logger.error("Agent editor: %s holds rejected content and could not be restored: %s", path, failure)
            raise StoreError(500, f"{rel} could not be restored and holds the rejected content ({failure}). "
                                  f"The edit was rejected:\n{reason}")
        if current != written:
            raise StoreError(409, f"{rel} changed on disk while it was checked, so it was left as it is now; reload. "
                                  f"The edit was rejected:\n{reason}")
        raise reason

    def _verify(self, before: AgentSystemConfig, names: list[str]) -> None:
        """The written tree loads, and `names` resolve without a problem they did not have before."""
        with quiet_loader():
            try:
                after = load_settings(str(self.config_path))
            except Exception as error:
                raise StoreError(422, error_text(error))
            missing = [f"{name}: not in the config after the write" for name in names if name not in after.plugins.servers]
            new = sorted(self.problems(after, names) - self.problems(before, names))
        if missing or new:
            raise StoreError(422, "\n".join(missing + new))

    def problems(self, config: AgentSystemConfig, names: list[str]) -> set[str]:
        """What the loader would not catch until the agent starts or runs, per entry: an unknown type or an
        inheritance cycle, a resolution error, unknown profiles, a missing template. An entry that does not resolve
        is checked on the fields it sets itself, so one problem never hides another."""
        found: set[str] = set()
        profiles = set(getattr(config.llm_system, "profiles", None) or {})
        types = catalog_for(config).types() | {"agent"}
        for name in names:
            server = config.plugins.servers.get(name)
            if server is None:
                continue
            # The resolver's own parent rule; get_mcp_config_by_name only logs a cycle and carries on without parents.
            chain, current = [name], name
            while (parent := parent_of(config, current)) is not None and parent not in chain:
                chain.append(parent)
                current = parent
            if parent is not None:
                found.add(f"{name}: inheritance cycle {' -> '.join([*chain, parent])}")
            elif config.plugins.servers[current].type not in types:
                found.add(f"{name}: the type chain ends in unknown type '{config.plugins.servers[current].type}'")
            try:
                resolved = get_mcp_config_by_name(name, config)
            except Exception as error:
                found.add(f"{name}: {error_text(error)}")
                agent, own_fields = server.agent_config, True
            else:
                agent, own_fields = (resolved.agent_config if resolved else None), False
            if agent is None:
                continue
            given = agent.model_fields_set if own_fields else set(type(agent).model_fields)
            chain = [*(([agent.llm_profile] if isinstance(agent.llm_profile, str) else agent.llm_profile)
                       if "llm_profile" in given else []),
                     *((agent.llm_profile_advanced or []) if "llm_profile_advanced" in given else [])]
            if profiles:
                found |= {f"{name}: llm profile '{p}' does not exist" for p in chain if p not in profiles}
            template = agent.system_template if "system_template" in given else None
            inline = agent.system_prompt if "system_prompt" in given else None
            if template and not inline and not (self.root / template).is_file():
                found.add(f"{name}: system_template {template} does not exist")
        return found


# ---------------------------------------------------------------------- YAML text


def _represent_float(representer: RoundTripRepresenter, value: float) -> Any:
    """`1e-05` is a string for the loader's YAML 1.1 parser; `1.0e-05` is a float for both."""
    if not math.isfinite(value):
        return RoundTripRepresenter.represent_float(representer, value)
    mantissa, _, exponent = repr(value).partition("e")
    text = f"{mantissa}.0e{exponent}" if exponent and "." not in mantissa else repr(value)
    return representer.represent_scalar("tag:yaml.org,2002:float", text)


class _Representer(RoundTripRepresenter):
    """A subclass, so the float rule does not change ruamel for other plugins."""


_Representer.add_representer(float, _represent_float)


def _yaml(indent: tuple[int, int, int]) -> YAML:
    yaml = YAML()
    yaml.Representer = _Representer
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=indent[0], sequence=indent[1], offset=indent[2])
    return yaml


def dump(doc: Any, indent: tuple[int, int, int] = INDENTS[0]) -> str:
    buffer = io.StringIO()
    _yaml(indent).dump(doc, buffer)
    return buffer.getvalue()


def load(text: str) -> Any:
    return _yaml(INDENTS[0]).load(text)


def _key(key: Any) -> Any:
    """A key the loader would read as something else (`on`, `yes`, `null`) is quoted."""
    return SingleQuotedScalarString(key) if isinstance(key, str) and not _plain_string(key) else key


def to_node(value: Any, old: Any = None) -> Any:
    """Plain data as ruamel nodes that the loader reads back as the same values."""
    if isinstance(value, dict):
        return CommentedMap((_key(key), to_node(item)) for key, item in value.items())
    if isinstance(value, list):
        return _to_seq(value, old if isinstance(old, CommentedSeq) else None)
    if isinstance(value, str):
        if isinstance(old, (DoubleQuotedScalarString, SingleQuotedScalarString)):
            return type(old)(value)
        if "\n" in value:
            return LiteralScalarString(value)
        if not _plain_string(value):
            return SingleQuotedScalarString(value)  # `yes`, `on`: strings in YAML 1.2, booleans for the loader
    return value


def _to_seq(values: list, old: Optional[CommentedSeq]) -> CommentedSeq:
    """A new sequence that reuses the old items it still holds, with their quoting and comments.

    The comment token of the old last item also carries the comment block after the list; unless that item stays
    last, only its own line's comment goes with it, and the splice keeps the block where it was.
    """
    pool = list(enumerate(old)) if old is not None else []
    last = len(old) - 1 if old is not None else -1
    items, reused = [], []
    for value in values:
        match = next((n for n, (_, item) in enumerate(pool) if item == value), None)
        if match is None:
            items.append(to_node(value))
            continue
        index, item = pool.pop(match)
        reused.append((len(items), index))
        items.append(item)
    seq = CommentedSeq(items)
    if old is None:
        return seq
    # after filling: CommentedSeq.append shifts the comments it already holds
    for position, index in reused:
        comment = old.ca.items.get(index)
        if comment and index == last and position != len(items) - 1:
            comment = _own_line(comment)
        if comment:
            seq.ca.items[position] = comment
    seq.ca.comment = old.ca.comment  # the comment between the key and the first item; without it ruamel breaks the list
    if old.fa.flow_style():
        seq.fa.set_flow_style()
    return seq


def _own_line(comment: list) -> Optional[list]:
    token = comment[0]
    if token is None or not token.value.startswith("#"):
        return None
    return [CommentToken(token.value.split("\n", 1)[0] + "\n", token.start_mark, token.end_mark), *comment[1:]]


def _plain_string(value: str) -> bool:
    if value.lower() in ("y", "n"):  # booleans in the YAML 1.1 spec, though not for PyYAML
        return False
    try:
        return isinstance(yaml_io.safe_load(value), str)
    except Exception:
        return False


def update_node(parent: Any, key: Any, old: Any, new: Any) -> None:
    """Change `parent[key]` from `old` to `new` touching only what differs; dicts recurse, missing keys go."""
    node = parent[key]
    if old == new:
        return
    if not (isinstance(node, CommentedMap) and isinstance(old, dict) and isinstance(new, dict)):
        parent[key] = to_node(new, node)
        return
    for gone in [k for k in node if k not in new]:
        del node[gone]
    for k, value in new.items():
        if k in node and k in old:
            update_node(node, k, old[k], value)
        else:
            node[_key(k)] = to_node(value)


def render(src: str, edit: Callable[[CommentedMap], None], expected: Any) -> str:
    """`src` with `edit` applied: ruamel's rendering where the edit changed lines, `src` verbatim elsewhere."""
    try:
        doc = load(src)
    except Exception as error:
        raise StoreError(400, f"The file cannot be read for a round-trip: {error}")
    renderings = {}
    for indent in INDENTS:
        renderings[indent] = dump(doc, indent)
        if renderings[indent] == src:
            break
    else:
        indent = min(renderings, key=lambda setting: _distance(src, renderings[setting]))
    base = renderings[indent]
    edit(doc)
    new = dump(doc, indent)
    for tidy in (True, False):
        text = splice(src, base, new, tidy)
        if _parses_to(text, expected):
            return text
    _check_parse(new, expected)  # names the value that cannot be written at all
    raise StoreError(400, "The edit cannot be written in place without changing other parts of the file "
                          "(its layout is unusual here); change the file by hand")


def _distance(a: str, b: str) -> int:
    matcher = difflib.SequenceMatcher(None, a.splitlines(), b.splitlines(), autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal")


def _parses_to(text: str, expected: Any) -> bool:
    try:
        return (yaml_io.safe_load(text) or {}) == expected
    except Exception:
        return False


def _check_parse(text: str, expected: Any) -> None:
    """Raise naming the first value that does not read back as it was meant."""
    try:
        got = yaml_io.safe_load(text) or {}
    except Exception as error:
        raise StoreError(400, f"The entry cannot be written as YAML: {error}")
    if got == expected:
        return
    path, value = _difference(got, expected)
    raise StoreError(400, f"{'.'.join(map(str, path))}: the value {value!r} cannot be written so that it reads back the same")


def _difference(got: Any, want: Any, path: tuple = ()) -> tuple[tuple, Any]:
    if isinstance(got, dict) and isinstance(want, dict):
        for key in [*want, *(k for k in got if k not in want)]:
            if key not in got or key not in want or got[key] != want[key]:
                return _difference(got.get(key), want.get(key), (*path, key))
    if isinstance(got, list) and isinstance(want, list) and len(got) == len(want):
        for index, (a, b) in enumerate(zip(got, want)):
            if a != b:
                return _difference(a, b, (*path, index))
    return path, want


def _line_map(a: list[str], b: list[str]) -> dict[int, int]:
    blocks = difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()
    return {i + d: j + d for i, j, size in blocks for d in range(size)}


def _decor(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _tail(lines: list[str]) -> int:
    """Length of the trailing run of blank and comment lines."""
    n = 0
    while n < len(lines) and _decor(lines[len(lines) - 1 - n]):
        n += 1
    return n


def _missing_decor(old: list[str], new: list[str]) -> list[str]:
    """The blank and comment lines of `old` that `new` does not have (blank lines count alike)."""
    pending = Counter(line.strip() for line in new if _decor(line))
    kept = []
    for line in old:
        if _decor(line):
            if pending[line.strip()]:
                pending[line.strip()] -= 1
            else:
                kept.append(line)
    return kept


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def splice(src: str, base: str, new: str, tidy: bool = True) -> str:
    """Three-way merge: `base` is ruamel's rendering of `src`, `new` of the edited data.

    Between lines all three share, a stretch the edit left alone is copied from `src`, an edited one from `new`.
    With `tidy` (the caller falls back to without when the result does not read back right), comments stay where
    they were: ruamel hangs the comment above the next key on the last value before it, so replacing that value drops
    the comment and a key added after it lands below it.
    """
    s, b, n = (text.splitlines(keepends=True) for text in (src, base, new))
    open_end = bool(s) and not s[-1].endswith("\n")  # ruamel always ends the file with a newline
    if open_end:
        s[-1] += "\n"
    to_src, to_new = _line_map(b, s), _line_map(b, n)
    anchors = [(i, to_src[i], to_new[i]) for i in range(len(b)) if i in to_src and i in to_new]
    out: list[str] = []
    pi, pj, pk = -1, -1, -1
    for i, j, k in anchors + [(len(b), len(s), len(n))]:
        s_part, b_part, n_part = s[pj + 1:j], b[pi + 1:i], n[pk + 1:k]
        if b_part == n_part:
            out += s_part
        elif not tidy:
            out += n_part
        elif not b_part:
            # An added key belongs to the block above: put it before the comments that open the next one.
            out += s_part
            at = len(out) - _tail(out) if _indent(n_part[0]) > (_indent(b[i]) if i < len(b) else 0) else len(out)
            out[at:at] = n_part
        elif len(s_part) == len(b_part):
            # A line-for-line reformat: the lines the edit left alone come from `src`, and so do the comments of the
            # lines it replaced, unless the new rendering has them itself.
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, b_part, n_part, autojunk=False).get_opcodes():
                out += s_part[i1:i2] if tag == "equal" else n_part[j1:j2] + _missing_decor(s_part[i1:i2], n_part[j1:j2])
        else:
            out += n_part[:len(n_part) - _tail(n_part)] + s_part[len(s_part) - _tail(s_part):] if _tail(s_part) else n_part
        if i < len(b):
            out.append(s[j])
        pi, pj, pk = i, j, k
    if open_end and out and out[-1] == s[-1]:
        out[-1] = out[-1][:-1]
    return "".join(out)


def _remove_entry(src: str, name: str) -> Optional[tuple[str, bool]]:
    """`src` without the server `name`: its lines, the comment block right above it and the comments inside it.

    Answers the text and whether the file then holds nothing but the structure (and the editor's header), or None
    when the entry cannot be cut out as lines (the caller falls back to ruamel).
    """
    try:
        doc = load(src)
        line = doc["plugins"]["servers"].lc.key(name)[0]
        servers_line = doc["plugins"].lc.key("servers")[0]
        last = len(doc["plugins"]["servers"]) == 1
    except Exception:
        return None
    if line <= servers_line:  # `servers: {a: …}` on one line: not a block of lines
        return None
    lines = src.splitlines(keepends=True)
    open_end = bool(lines) and not lines[-1].endswith("\n")
    if open_end:
        lines[-1] += "\n"
    indent = _indent(lines[line])
    end = line
    for index in range(line + 1, len(lines)):
        if not lines[index].strip():
            continue
        if _indent(lines[index]) <= indent:
            break
        end = index
    start = line
    # Only comments at the key's own indentation: a deeper one is the end of the previous entry's body.
    while start - 1 > servers_line and lines[start - 1].lstrip().startswith("#") and _indent(lines[start - 1]) == indent:
        start -= 1
    if start > 0 and not lines[start - 1].strip():  # one blank line separated the entry: take one along
        if end + 1 < len(lines) and not lines[end + 1].strip():
            end += 1
        elif end + 1 == len(lines):
            start -= 1
    kept = lines[:start] + lines[end + 1:]
    if open_end and end + 1 < len(lines):  # the old last line is still the last one
        kept[-1] = kept[-1][:-1]
    if last:
        match = re.fullmatch(r"(\s*servers:)([ \t]+#.*)?\n", kept[servers_line])
        if match is None:
            return None
        kept[servers_line] = f"{match.group(1)} {{}}{match.group(2) or ''}\n"
    structure = {"plugins:", "servers: {}", f"# {CREATED_HEADER}"}
    empty = last and all(not text.strip() or text.strip() in structure for text in kept)
    return "".join(kept), empty


def unified_diff(old: str, new: str, rel: str) -> str:
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{rel}", f"b/{rel}")
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


def _rebase_templates(entry: Any, source_dir: Path, target_dir: Path) -> None:
    """`./`/`../` system_template paths, written relative to the source file, made relative to the new one."""
    if isinstance(entry, dict):
        for key, value in entry.items():
            if key == "system_template" and isinstance(value, str) and value.startswith(("./", "../")):
                moved = Path(os.path.relpath((source_dir / value).resolve(), target_dir)).as_posix()
                entry[key] = moved if moved.startswith("../") else f"./{moved}"
            else:
                _rebase_templates(value, source_dir, target_dir)
    elif isinstance(entry, list):
        for item in entry:
            _rebase_templates(item, source_dir, target_dir)


# ---------------------------------------------------------------------- files


def _stamp(path: Path) -> tuple[int, int, int]:
    try:
        info = path.stat()
    except OSError:
        return (-1, -1, -1)
    return (info.st_mtime_ns, info.st_size, info.st_mode)


@lru_cache(maxsize=1024)
def _file_problem(path: Path, root: Path, stamp: tuple[int, int, int]) -> Optional[str]:
    """Why a file cannot be written in place, None when it can (cached per file state)."""
    if not path.is_relative_to(root):
        return "the file is outside the repository root"
    if not os.access(path, os.W_OK):
        return "the file is read-only"
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return f"the file cannot be read: {error}"
    if "\r\n" in text and text.count("\n") != text.count("\r\n"):
        return "the file mixes CRLF and LF line endings"
    try:
        load(text.replace("\r\n", "\n"))
    except Exception as error:
        return f"the file cannot be read for a round-trip: {error}"
    return None


def _retry(action: Callable[[], Any]) -> Any:
    """Windows refuses a replace or unlink while another process has the file open; that passes quickly."""
    for attempt in range(ATTEMPTS):
        try:
            return action()
        except PermissionError:
            if attempt == ATTEMPTS - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def _read(path: Path) -> Optional[bytes]:
    """The file's bytes, None when it is gone; a file another process holds is retried like a write."""
    try:
        return _retry(path.read_bytes)
    except FileNotFoundError:
        return None


def _discard(tmp: str) -> None:
    try:
        os.chmod(tmp, stat.S_IREAD | stat.S_IWRITE)  # a read-only file cannot be deleted on Windows
        _retry(Path(tmp).unlink)
    except FileNotFoundError:
        pass


def atomic_write(path: Path, data: bytes) -> None:
    """A temp file in the same directory, then a rename; the file keeps its permissions."""
    if path.exists() and not os.access(path, os.W_OK):
        raise PermissionError(f"{path.name} is read-only")
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, mode)
        _retry(lambda: os.replace(tmp, path))
    except BaseException:
        _discard(tmp)
        raise
