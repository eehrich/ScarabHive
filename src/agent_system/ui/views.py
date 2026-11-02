"""The two views of the shell: the simple one, which shows a person what their work needs, and the admin one, which
shows everything.

The role decides what someone may do (the catalogue lists the panels their role may open); the view decides what
they see of it. Admins see everything and may switch to the simple view to see what the others see; every other
role always sees the simple view. What the simple view shows is the admins' choice, kept here:

* ``panels`` -- the panels it lists (in the launcher, the palette and on the start page), by catalogue id;
* ``topics`` -- what the composer offers in place of the agent, model and thinking choice: a topic in plain words,
  the agent behind it ("" = the default agent), and the model profile for "Quick" and for "Thorough" ("" = the
  agent's own).

A view is what is shown, not a right: whoever may open a panel still may, by its address. The routes are in
``routes.py``; the file is ``data/ui/views.json``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MODES = ("simple", "admin")
MAX_TOPICS = 8
MAX_PANELS = 60
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_NAME = re.compile(r"^[A-Za-z0-9_.:-]{0,100}$")

#: What the simple view shows before an admin chose, as far as it is there.
DEFAULT_PANELS = ("todo", "help", "settings")
DEFAULT_TOPICS = (
    {"id": "code", "label": "Check code", "hint": "e.g. \"Is the login in app.py safe?\"",
     "agent": "coder_reviewer", "quick": "", "deep": "think"},
    {"id": "text", "label": "Write a text", "hint": "e.g. \"Write the security notes for the manual\"",
     "agent": "", "quick": "", "deep": "think"},
    {"id": "explain", "label": "Explain something", "hint": "e.g. \"What is a race condition?\"",
     "agent": "", "quick": "", "deep": "think"},
    {"id": "plan", "label": "Plan something", "hint": "e.g. \"What has to happen before the release?\"",
     "agent": "", "quick": "", "deep": "think"},
)


class ViewError(ValueError):
    """A view that cannot be kept; the message says why."""


def check_topics(raw: Any) -> list[dict[str, str]]:
    """The topics as kept, or ViewError."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TOPICS:
        raise ViewError(f"topics must list 1 to {MAX_TOPICS} topics")
    topics, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ViewError("each topic is an object: {id, label, hint, agent, quick, deep}")
        topic = {key: str(item.get(key) or "").strip() for key in ("id", "label", "hint", "agent", "quick", "deep")}
        if not _ID.match(topic["id"]) or topic["id"] in seen:
            raise ViewError(f"topic id {topic['id']!r}: lower case letters, digits, - or _, unique")
        if not 1 <= len(topic["label"]) <= 40 or len(topic["hint"]) > 120:
            raise ViewError(f"topic {topic['id']}: a label of 1 to 40 characters, a hint of at most 120")
        for key in ("agent", "quick", "deep"):
            if not _NAME.match(topic[key]):
                raise ViewError(f"topic {topic['id']}: {key} {topic[key]!r} is no agent or profile name")
        seen.add(topic["id"])
        topics.append(topic)
    return topics


class ViewStore:
    """The simple view's panels and topics, and the view each admin chose -- one JSON file."""

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
        """What the simple view shows, among the ``available`` panel ids: the admins' choice, else the defaults."""
        with self._lock:
            kept = self._section("user_view")
        panels = kept.get("panels")
        if not isinstance(panels, list):
            panels = list(DEFAULT_PANELS)
        try:
            topics = check_topics(kept.get("topics")) if kept.get("topics") is not None else [dict(t) for t in DEFAULT_TOPICS]
        except ViewError:
            topics = [dict(t) for t in DEFAULT_TOPICS]
        return {"panels": [p for p in panels if p in available], "topics": topics,
                "chosen": isinstance(kept.get("panels"), list)}

    def set_user_view(self, panels: Any, topics: Any, available: list[str], by: str) -> dict[str, Any]:
        if not isinstance(panels, list) or len(panels) > MAX_PANELS or not all(isinstance(p, str) for p in panels):
            raise ViewError(f"panels must list at most {MAX_PANELS} panel ids")
        unknown = sorted(set(panels) - set(available))
        if unknown:
            raise ViewError(f"no such panel for users: {', '.join(unknown)}")
        kept_topics = check_topics(topics)
        with self._lock:
            data = self._read(saving=True)
            data["user_view"] = {"panels": list(dict.fromkeys(panels)), "topics": kept_topics, "by": by}
            self._write(data)
        return self.user_view(available)

    def mode_of(self, username: str) -> str:
        """The view an admin chose; admin until they choose."""
        with self._lock:
            mode = self._section("modes").get(username or "")
        return mode if mode in MODES else "admin"

    def set_mode(self, username: str, mode: str) -> str:
        if mode not in MODES:
            raise ViewError(f"mode must be one of {', '.join(MODES)}")
        with self._lock:
            data = self._read(saving=True)
            modes = data.get("modes")
            data["modes"] = {**(modes if isinstance(modes, dict) else {}), username or "": mode}
            self._write(data)
        return mode
