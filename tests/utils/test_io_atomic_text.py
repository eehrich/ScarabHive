"""atomic_write_text writes the way atomic_write_json does: a unique temp file, retried, never left behind."""
from __future__ import annotations

import os
import stat
import sys

import pytest

from agent_system.utils import io


def test_parents_are_created_on_request(tmp_path):
    target = tmp_path / "a" / "b" / "file.txt"
    with pytest.raises(OSError):
        io.atomic_write_text(target, "x", retries=1)
    io.atomic_write_text(target, "x", mkdir=True)
    assert target.read_text(encoding="utf-8") == "x"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_file_is_the_owners_alone(tmp_path):
    """As the mkstemp it used to write through made it: config/local.env is written here."""
    target = tmp_path / "local.env"
    io.atomic_write_text(target, "KEY=value\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_passing_lock_is_ridden_out(tmp_path, monkeypatch):
    target = tmp_path / "file.txt"
    real_replace, failures = os.replace, []

    def busy_once(src, dst):
        if not failures:
            failures.append(src)
            raise PermissionError("being replaced")
        real_replace(src, dst)

    monkeypatch.setattr(io.os, "replace", busy_once)
    monkeypatch.setattr(io.time, "sleep", lambda delay: None)
    io.atomic_write_text(target, "done")
    assert target.read_text(encoding="utf-8") == "done" and failures
    assert [p.name for p in tmp_path.iterdir()] == ["file.txt"], "a temp file was left behind"


def test_a_failed_write_leaves_no_temp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(io.os, "replace", lambda src, dst: (_ for _ in ()).throw(PermissionError("held")))
    monkeypatch.setattr(io.time, "sleep", lambda delay: None)
    with pytest.raises(OSError):
        io.atomic_write_text(tmp_path / "file.txt", "x")
    assert list(tmp_path.iterdir()) == []
