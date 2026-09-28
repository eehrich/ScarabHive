"""A test area for real Seatbelt runs, shared by the backend and its consumers.

Every confined test writes only inside a fresh directory made for it. That
directory must NOT lie inside the user's temp directory, which the Seatbelt
profile opens on purpose: a target in there is writable anyway, and a test
"proving" a write outside the workspace fails would prove nothing. pytest's
``tmp_path`` lives exactly there on macOS, so the area is made under
``/private/tmp`` -- or under ``$SCARABHIVE_SANDBOX_TEST_DIR`` if set -- and the
fixture asserts that it is outside, instead of hoping.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest

from agent_system.utils import process_sandbox as ps

on_macos = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Seatbelt (sandbox-exec) exists only on macOS; Linux runs the "
           "bubblewrap tests in tests/utils/test_process_sandbox.py")


@dataclass
class Area:
    """``root`` holds ``ws`` (the workspace) and ``out`` (beside it, outside)."""

    root: Path
    ws: Path
    out: Path


@contextmanager
def seatbelt_area() -> Iterator[Area]:
    base = os.environ.get("SCARABHIVE_SANDBOX_TEST_DIR") or "/private/tmp"
    made = Path(tempfile.mkdtemp(prefix="seatbelt-", dir=base))
    try:
        root = Path(ps._kernel_path(made))
        user_temp = ps._kernel_path(os.confstr(ps._CS_DARWIN_USER_TEMP_DIR))
        assert not (str(root) + "/").startswith(user_temp + "/"), \
            f"{root} lies inside the user temp directory {user_temp}"
        # The two-spelling tests need the /tmp firmlink above the area.
        assert str(root).startswith("/private/tmp/"), \
            f"the test area must lie below /private/tmp, got {root}"
        (root / "ws").mkdir()
        (root / "out").mkdir()
        yield Area(root, root / "ws", root / "out")
    finally:
        shutil.rmtree(made, ignore_errors=True)


@contextmanager
def probe_scratch_in(directory: Path) -> Iterator[None]:
    """Let the first-use probe try its forbidden write in ``directory``.

    In production that is the user's cache directory; tests keep every write,
    even an intended-to-fail one, inside their own area.
    """
    original = ps._probe_scratch_parent
    ps._probe_scratch_parent = lambda: str(directory)
    try:
        yield
    finally:
        ps._probe_scratch_parent = original


@contextmanager
def probed_seatbelt() -> Iterator["ps._Seatbelt"]:
    """A freshly probed backend. A failing probe fails the test -- a skip
    would turn a broken profile into a green run. The probe's scratch goes
    into a fresh area of its own."""
    ps.reset_backend_cache()
    try:
        with seatbelt_area() as scratch, probe_scratch_in(scratch.root):
            backend = ps._select_backend()
            assert isinstance(backend, ps._Seatbelt), \
                f"sandbox-exec is not usable here: {ps._backend_problem}"
        yield backend
    finally:
        ps.reset_backend_cache()
