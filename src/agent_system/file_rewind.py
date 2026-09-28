"""Rewinding what an agent changed on disk -- the seam between the chat and the recorder.

`/undo` takes a turn out of the CONVERSATION; the files the agent wrote in
that turn stay as they are. Undoing those as well needs a record of how each
file looked before the agent touched it, and that record is kept by a plugin
(``file_checkpoints``): its tool hooks see every write before it happens.

The chat surfaces live in the core -- `/undo files` and `/rewind` in the
terminal, the /chat endpoints for the browser -- and must reach that record
without importing a plugin. So the plugin announces itself here when it is
built, and the surfaces ask for it by capability, the way file tools are
found by ``file_access_roots()``. No plugin loaded: ``file_rewinder()`` is
None and every surface says that file checkpoints are off.

What the surfaces rely on is the ``FileRewinder`` protocol below. Both calls
take the conversation as the caller holds it -- the turns a person sees are
what a checkpoint is named after -- and answer with plain dicts that carry a
``text`` rendered for a person, so the terminal and the browser print the
same lines.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Protocol, Sequence

logger = logging.getLogger(__name__)

__all__ = ["FileRewinder", "register_file_rewinder", "unregister_file_rewinder", "file_rewinder",
           "forget_session_files"]

#: ``rewind()`` status values.
REWOUND = "rewound"      # every recorded change since the checkpoint is undone
NOTHING = "nothing"      # no recorded change since the checkpoint
REFUSED = "refused"      # nothing was touched: files changed outside the agent, or runs still write
PARTIAL = "partial"      # some files could not be written; the rest is undone
UNKNOWN_CHECKPOINT = "unknown"  # the checkpoint number names none


class FileRewinder(Protocol):
    """What the chat surfaces call. Implemented by the file_checkpoints plugin."""

    async def checkpoints(self, *, user_id: str, session_id: str,
                          messages: Sequence[Any]) -> dict:
        """The checkpoints of a session, oldest first:
        ``{"checkpoints": [{"number", "turn", "dropped", "question", "files": [...],
        "untracked": [...]}], "text": str}``. ``turn`` is the 1-based turn of the
        conversation the checkpoint stands before, None for a turn /undo dropped."""
        ...

    async def rewind(self, *, user_id: str, session_id: str, messages: Sequence[Any],
                     checkpoint: Optional[int], registry: Any, overwrite: bool = False) -> dict:
        """Put every file the agent changed since ``checkpoint`` (a number from
        ``checkpoints()``; None: since the last turn of ``messages`` began) back as
        it was then. ``{"status": REWOUND|NOTHING|REFUSED|PARTIAL, "restored": [...],
        "removed": [...], "conflicts": [...], "unrestorable": [...], "failed": [...],
        "untracked": [...], "text": str}`` with a status from above. REFUSED and
        UNKNOWN_CHECKPOINT touch nothing; ``overwrite``
        restores files changed outside the agent anyway. ``registry`` holds the
        tool servers that recorded the changes: a path is written only while it
        still lies inside the directories its server allows."""
        ...


    async def forget(self, *, user_id: str, session_id: str) -> bool:
        """Delete everything recorded for a session -- called when the person
        deletes the session. True when there was a record."""
        ...


async def forget_session_files(user_id: str, session_id: str) -> None:
    """A session the person deleted takes its file checkpoints with it.

    Called by the delete endpoint, not by SessionManager.delete_session: the
    session archive deletes through that too, and an archived session can be
    restored -- its checkpoints must still be there then. Never raises: the
    session is gone either way, a record left behind is only disk.
    """
    rewinder = file_rewinder()
    forget = getattr(rewinder, "forget", None)
    if forget is None:
        return
    try:
        await forget(user_id=user_id, session_id=session_id)
    except Exception:  # noqa: BLE001 - logged; the delete itself succeeded
        logger.warning("The file checkpoints of deleted session %s could not be removed", session_id,
                       exc_info=True)


_rewinder: Optional[FileRewinder] = None


def register_file_rewinder(rewinder: FileRewinder) -> None:
    """Called by the plugin that records file changes, when it is built."""
    global _rewinder
    if _rewinder is not None and _rewinder is not rewinder:
        logger.warning("A second file rewinder was registered (%s); it replaces %s",
                       type(rewinder).__name__, type(_rewinder).__name__)
    _rewinder = rewinder


def unregister_file_rewinder(rewinder: FileRewinder) -> None:
    """Take ``rewinder`` back out; another one registered since stays."""
    global _rewinder
    if _rewinder is rewinder:
        _rewinder = None


def file_rewinder() -> Optional[FileRewinder]:
    """The registered rewinder, or None when no plugin records file changes."""
    return _rewinder
