"""The views: which panels the shell offers a viewer, and whether a panel shows its simple or its admin view.

The role decides what someone may do (the catalogue lists the panels their role may open); the view decides what
they see of it.

* **Released panels** -- admins are offered every panel their role may open; everyone else the panels the admins
  released (User view panel), in the launcher and the palette. The chat is the same for everyone.
* **Simple or admin view of a panel** -- a panel may show less in its simple view. Everyone starts in the simple view,
  admins too; admins switch in the panel, and the choice is kept per admin. Everyone else always sees the simple view.

A view is what is shown, not a right: whoever may open a panel still may, by its address. The routes are in
``routes.py``; the file is ``data/ui/views.json``.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MODES = ("simple", "admin")
MAX_PANELS = 60

#: What users are offered before an admin chose, as far as it is there.
DEFAULT_PANELS = ("todo", "help", "settings")


class ViewError(ValueError):
    """A view that cannot be kept; the message says why."""


class ViewStore:
    """The panels released to users, and the view each admin chose -- one JSON file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self, *, saving: bool = False) -> dict[str, Any]:
        """The file, and {} for none. A broken one is logged and counts as none: everybody gets the defaults, and the
        next save writes it whole -- it must not leave the shell without a view until someone repairs it by hand.
        ``saving``: that save is about to overwrite it, so it is kept beside it first, each one under its own name."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ViewError(f"{self.path} cannot be read: {exc}") from exc
        except ValueError as exc:
            data, why = None, str(exc)
        else:
            why = "not a mapping"
        if isinstance(data, dict):
            return data
        logger.error("%s is broken, the defaults hold until the next save: %s", self.path, why)
        if saving:
            try:
                handle, kept = tempfile.mkstemp(dir=self.path.parent, prefix=f"{self.path.name}.broken-")
                os.close(handle)
                os.replace(self.path, kept)
            except OSError as exc:
                raise ViewError(f"{self.path} is broken and cannot be set aside: {exc}") from exc
        return {}

    def _section(self, name: str) -> dict[str, Any]:
        """One section of the file; anything but a mapping counts as none."""
        section = self._read().get(name)
        return section if isinstance(section, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        """Whole or not at all: a temporary file of its own beside it, then the rename."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, part = tempfile.mkstemp(dir=self.path.parent, prefix=f"{self.path.name}.", suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as out:
                    json.dump(data, out, ensure_ascii=False, indent=1)
                os.replace(part, self.path)
            except BaseException:
                Path(part).unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise ViewError(f"{self.path} cannot be written: {exc}") from exc

    def user_view(self, available: list[str]) -> dict[str, Any]:
        """The panels released to users, among the ``available`` panel ids: the admins' choice, else the defaults."""
        with self._lock:
            kept = self._section("user_view")
        panels = kept.get("panels")
        chosen = isinstance(panels, list)
        return {"panels": [p for p in (panels if chosen else DEFAULT_PANELS) if p in available], "chosen": chosen}

    def set_user_view(self, panels: Any, available: list[str], by: str) -> dict[str, Any]:
        if not isinstance(panels, list) or len(panels) > MAX_PANELS or not all(isinstance(p, str) for p in panels):
            raise ViewError(f"panels must list at most {MAX_PANELS} panel ids")
        unknown = sorted(set(panels) - set(available))
        if unknown:
            raise ViewError(f"no such panel for users: {', '.join(unknown)}")
        with self._lock:
            data = self._read(saving=True)
            data["user_view"] = {"panels": list(dict.fromkeys(panels)), "by": by}
            self._write(data)
        return self.user_view(available)

    def mode_of(self, username: str) -> str:
        """The view an admin chose; the simple one until they switch."""
        with self._lock:
            mode = self._section("modes").get(username or "")
        return mode if mode in MODES else "simple"

    def set_mode(self, username: str, mode: str) -> str:
        if mode not in MODES:
            raise ViewError(f"mode must be one of {', '.join(MODES)}")
        with self._lock:
            data = self._read(saving=True)
            modes = data.get("modes")
            data["modes"] = {**(modes if isinstance(modes, dict) else {}), username or "": mode}
            self._write(data)
        return mode
