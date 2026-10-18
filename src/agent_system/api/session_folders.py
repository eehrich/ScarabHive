"""The session list's folders: the ones plugins declare, and a user's own.

A folder has a name, an icon of the UI kit's sprite and the agent groups it
claims (``metadata.category``, the agent picker's groups). A session sits in
the folder its ``metadata.folder`` names; a session that names none sits in
the folder claiming its group -- the group its agent belongs to, or the one
its starter stamped on it (a machine run's ``metadata.category``) -- and
``""`` keeps it out of every folder. So whatever a plugin's agents start
files itself, and what the viewer moves stays where they put it.

The core knows no folder. A plugin declares the one its sessions belong in,
in its ``plugin.toml``::

    [session_folder]
    name = "Video"
    icon = "clapperboard"
    groups = ["Video"]

Its id is the plugin's directory name. A user sees every declared folder,
until they delete it; one they change keeps their version; their own come
after the declared ones. All of that is kept per user in
``<sessions>/<user>/.folders.json`` -- ``{"folders": [...], "removed": [...]}``:
the folders they made or changed, and the declared ones they deleted -- like
the sessions themselves, with or without accounts (``viewer()``). The dot
keeps it out of the index rebuild, which reads every other ``*.json`` there
as a session. A declared folder the user left as it is is not copied: a
plugin that changes its declaration reaches them.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Iterable, List

from pydantic import BaseModel, Field, field_validator

from agent_system.services.session_paths import user_dir_of

logger = logging.getLogger(__name__)

FOLDERS_FILE = ".folders.json"
MANIFEST = "plugin.toml"
#: The folders a user may make; the declared ones come on top (the list a viewer saves holds both).
MAX_FOLDERS = 200
#: An icon is a symbol id of static/kit/icons.svg; the panel offers its folder set.
ICON_PATTERN = re.compile(r"^[a-z0-9-]{1,40}$")


class SessionFolder(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    name: str = Field(min_length=1, max_length=60)
    icon: str = "folder"
    groups: List[str] = Field(default_factory=list, max_length=20)

    @field_validator("name")
    @classmethod
    def _name_is_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("a folder needs a name")
        return value

    @field_validator("icon")
    @classmethod
    def _icon_is_a_symbol(cls, value: str) -> str:
        if not ICON_PATTERN.match(value):
            raise ValueError("icon must be a symbol id of the kit's sprite")
        return value

    @field_validator("groups")
    @classmethod
    def _groups_are_names(cls, value: List[str]) -> List[str]:
        # once each: a group named twice (or once with a stray space) would claim itself twice in the list
        return list(dict.fromkeys(group.strip()[:60] for group in value if group and group.strip()))


class SessionFolders(BaseModel):
    folders: List[SessionFolder] = Field(default_factory=list, max_length=MAX_FOLDERS * 5)

    @field_validator("folders")
    @classmethod
    def _ids_and_groups_once(cls, value: List[SessionFolder]) -> List[SessionFolder]:
        ids = [folder.id for folder in value]
        if len(ids) != len(set(ids)):
            raise ValueError("two folders share an id")
        claimed: dict[str, str] = {}
        for folder in value:
            for group in folder.groups:
                if group in claimed:
                    raise ValueError(f"the group {group!r} is claimed by {claimed[group]!r} already")
                claimed[group] = folder.name
        return value


class _Stored(BaseModel):
    folders: List[SessionFolder] = Field(default_factory=list)
    removed: List[str] = Field(default_factory=list)


@functools.lru_cache(maxsize=8)
def _declared(dirs: tuple[str, ...]) -> tuple[SessionFolder, ...]:
    found: list[SessionFolder] = []
    for root in dirs:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for plugin in sorted(p for p in root_path.iterdir() if (p / MANIFEST).is_file()):
            try:
                with (plugin / MANIFEST).open("rb") as handle:
                    table = tomllib.load(handle).get("session_folder")
                if table is None:
                    continue
                folder = SessionFolder(id=plugin.name, **table)
            except (OSError, ValueError, TypeError) as exc:  # not UTF-8, not TOML, not a folder: its own only
                logger.warning("plugin %s: its [session_folder] is unreadable, no folder from it: %s", plugin.name, exc)
                continue
            if any(other.id == folder.id for other in found):
                continue  # the same plugin name under a later root: discovery takes the first, so does this
            found.append(folder)
    return tuple(found)


def declared_folders(plugin_dirs: Iterable[str]) -> tuple[SessionFolder, ...]:
    """The folders the plugins under these roots declare (``[session_folder]`` in a plugin.toml), in directory
    order. Read once per set of roots: a manifest changes with a restart."""
    return _declared(tuple(str(d) for d in plugin_dirs))


def folders_path(root: Path, user_id: str) -> Path:
    return user_dir_of(root, user_id) / FOLDERS_FILE


def _read_stored(root: Path, user_id: str) -> _Stored:
    path = folders_path(root, user_id)
    try:
        return _Stored.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except (ValueError, OSError) as exc:
        logger.warning("session folders of %s unreadable, the declared ones stand in: %s", user_id, exc)
    return _Stored()


def _merged(stored: _Stored, declared: Iterable[SessionFolder]) -> SessionFolders:
    """The folders a user sees: the declared ones they kept (in their version where they changed one), then their
    own. A group goes to the first folder claiming it -- a user's own or changed folder before a declared one as
    it stands, which yields what a plugin added to its declaration since."""
    declared = list(declared)
    declared_ids = {folder.id for folder in declared}
    own = {folder.id: folder for folder in stored.folders}
    shown = [own.get(folder.id, folder) for folder in declared if folder.id not in stored.removed]
    shown += [folder for folder in stored.folders if folder.id not in declared_ids]
    seen: set = set()  # a file edited by hand may name an id twice: the first one stands
    shown = [folder for folder in shown if not (folder.id in seen or seen.add(folder.id))]
    chosen = {folder.id for folder in stored.folders}
    claimed: set = set()

    def keep_unclaimed(folder: SessionFolder) -> SessionFolder:
        kept = [g for g in folder.groups if g not in claimed]
        claimed.update(kept)
        return folder if kept == folder.groups else folder.model_copy(update={"groups": kept})

    # the user's own and changed folders first, in their order (a file edited by hand may give two of them one
    # group: the first keeps it), then the declared ones as they stand
    first = {folder.id: keep_unclaimed(folder) for folder in shown if folder.id in chosen}
    result = [first[folder.id] if folder.id in first else keep_unclaimed(folder) for folder in shown]
    # built to hold: ids and groups once each -- and not cut to MAX_FOLDERS, which would drop a folder of the
    # user's from the list they save back
    return SessionFolders.model_construct(folders=result)


def read_folders(root: Path, user_id: str, declared: Iterable[SessionFolder]) -> SessionFolders:
    """The folders the user sees."""
    return _merged(_read_stored(root, user_id), declared)


def write_folders(root: Path, user_id: str, wanted: SessionFolders, declared: Iterable[SessionFolder]) -> SessionFolders:
    """Make ``wanted`` the folders the user sees: a declared folder left out is deleted for them, one changed is
    kept in their version, one as declared is not copied. Atomic: a reader sees the old file or the new one."""
    declared = {folder.id: folder for folder in declared}
    wanted_ids = {folder.id for folder in wanted.folders}
    removed = [fid for fid in declared if fid not in wanted_ids]
    # Kept: what the merge would not give back by itself. The viewer saves the list whole, a declared folder as it
    # was shown -- short of a group another folder files -- so "changed" is not "differs from the declaration" but
    # "the merge shows it otherwise". Groups compare as sets: the dialog lists them sorted. Each round keeps at
    # least one more folder, so this ends.
    kept = {folder.id for folder in wanted.folders
            if folder.id not in declared or (folder.name, folder.icon) != (declared[folder.id].name, declared[folder.id].icon)}
    if len([folder for folder in wanted.folders if folder.id not in declared]) > MAX_FOLDERS:
        raise ValueError(f"at most {MAX_FOLDERS} folders of a user's own")
    # A folder that wants a group the merge gives elsewhere has claimed it: kept first. Only once none is left, one
    # still showing a group it does not want had it unticked -- before, it may only show it for a claimant not kept
    # yet (B takes G from A: A shows G until B is kept), and keeping it would freeze it against its plugin.
    while True:
        stored = _Stored(folders=[folder for folder in wanted.folders if folder.id in kept], removed=removed)
        shown = {folder.id: set(folder.groups) for folder in _merged(stored, declared.values()).folders}
        loose = [folder for folder in wanted.folders if folder.id not in kept]
        claiming = {f.id for f in loose if not set(f.groups) <= shown.get(f.id, set())}
        more = claiming or {f.id for f in loose if shown.get(f.id) != set(f.groups)}
        if not more:
            break
        kept |= more
    path = folders_path(root, user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".folders.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(stored.model_dump_json(indent=2))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return _merged(stored, declared.values())
