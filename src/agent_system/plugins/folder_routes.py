"""A folder picker's route for a plugin panel: the folders of the server a person may pick from.

A panel picks a folder with ``folderPicker({url})`` from ``static/kit/panel-kit.js``; the plugin's web class inherits
``FolderRoutes`` and says which folders may be listed (``folder_roots()``), and its ``schema.yaml`` names the route::

    - path: "/dirs"
      method: "GET"
      handler: "get_dirs"

The answer is ``{path, parent, dirs: [{name, path}], truncated}``. The top (an empty ``path``) lists the roots that
exist; below them only folders that lie inside the roots: hidden folders, links that lead out of the roots and the
installation's private folders are left out (``PathSandbox.resolve``). Listing the server's folders is for admins.
A refusal is a 422 in the language the panel asks in (the kit sends ``<html lang>`` as Accept-Language): German for
``de``, English otherwise.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import Any, Iterable

from fastapi import HTTPException, Query, Request

from agent_system.plugins.agent_tasks import admin_of
from agent_system.utils.path_sandbox import PathSandbox, PathSandboxDenied, private_folders

MAX_DIRS = 1000  # folders listed in one answer; more are cut and the answer says so


def refusal(request: Request, en: str, de: str) -> HTTPException:
    """A 422 whose detail is German when the request asks for German first, else English."""
    german = request.headers.get("accept-language", "").strip().lower().startswith("de")
    return HTTPException(status_code=422, detail=de if german else en)


def _is_link(entry: os.DirEntry) -> bool:
    """A symlink, or on Windows any reparse point (a junction too: DirEntry.is_junction only exists from 3.12).
    scandir has the attributes already: no call to the file system."""
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return entry.is_symlink() or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _sandbox(roots: PathSandbox | Iterable[Path | str]) -> PathSandbox:
    if isinstance(roots, PathSandbox):
        return roots
    return PathSandbox(roots=tuple(Path(root).resolve() for root in roots), base=Path.cwd().resolve())


class FolderRoutes:
    """The folder picker's route for a web handler class that provides ``folder_roots()``: a PathSandbox, or the
    root directories themselves. The plugin's schema.yaml names it: GET /dirs -> get_dirs."""

    def folder_roots(self) -> PathSandbox | Iterable[Path | str]:
        raise NotImplementedError("a web class on FolderRoutes says which folders may be listed")

    def _dirs(self, path: str, request: Request) -> dict[str, Any]:
        sandbox = _sandbox(self.folder_roots())
        if not path:  # the top: the roots themselves -- those that are there
            there = [root for root in sandbox.roots if root.is_dir()]
            if not there:
                roots = sandbox.describe_roots()
                raise refusal(request,
                              f"None of the allowed folders exists ({roots}): create one, or name others in the "
                              "plugin's configuration",
                              f"Keiner der erlaubten Ordner existiert ({roots}): einen anlegen oder in der "
                              "Konfiguration des Plugins andere nennen")
            return {"path": "", "parent": None, "dirs": [{"name": str(root), "path": str(root)} for root in there],
                    "truncated": False}
        try:
            folder = sandbox.resolve(path)
        except PathSandboxDenied as exc:
            raise refusal(request, str(exc), f"{path} liegt außerhalb der erlaubten Ordner oder ist privat") from exc
        if not folder.is_dir():
            raise refusal(request, f"No such folder: {path}", f"Kein Ordner: {path}")
        below = next(folder.relative_to(root) for root in sandbox.roots if folder == root or root in folder.parents)
        if any(part.startswith(".") for part in below.parts):  # not listed, so not entered by its path either
            raise refusal(request, f"{path} is a hidden folder", f"{path} ist ein versteckter Ordner")
        try:
            entries = sorted(os.scandir(folder), key=lambda entry: entry.name.lower())
        except OSError as exc:
            raise refusal(request, f"{folder} cannot be listed ({exc})",
                          f"{folder} lässt sich nicht auflisten ({exc})") from exc
        dirs = []
        private = private_folders()  # once for the listing: resolving each entry fully cost ten times the walk
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            if _is_link(entry):  # it may lead anywhere: out of the roots, into a private folder
                try:
                    sandbox.resolve(entry.path)
                except PathSandboxDenied:
                    continue
            elif os.path.normcase(entry.path) in private:  # folder is canonical, so its own child is too
                continue
            dirs.append({"name": entry.name, "path": entry.path})
            if len(dirs) > MAX_DIRS:  # one past the cap says that more are there; the rest need not be resolved
                break
        parent = "" if folder in sandbox.roots else str(folder.parent)
        return {"path": str(folder), "parent": parent, "dirs": dirs[:MAX_DIRS], "truncated": len(dirs) > MAX_DIRS}

    async def get_dirs(self, request: Request,
                       path: str = Query("", description="A folder; empty: the allowed folders")) -> dict[str, Any]:
        await admin_of(request)  # the server's folders: admins only
        return await asyncio.to_thread(self._dirs, path, request)
