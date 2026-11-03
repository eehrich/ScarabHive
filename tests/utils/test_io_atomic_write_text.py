"""atomic_write_text: a unique temp file, the normal file mode, nothing left behind."""
import os
import stat

import pytest

from agent_system.utils.io import atomic_write_text


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_a_rewritten_file_keeps_the_mode_an_open_gives(tmp_path):
    """tempfile made the temp 0600, and the replace carried that over."""
    target = tmp_path / "f.txt"
    atomic_write_text(target, "one")
    mask = os.umask(0)
    os.umask(mask)
    assert stat.S_IMODE(target.stat().st_mode) == 0o666 & ~mask


def test_content_and_no_leftovers(tmp_path):
    target = tmp_path / "f.txt"
    atomic_write_text(target, "one")
    atomic_write_text(target, "two")
    assert target.read_text() == "two"
    assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]


def test_a_failed_write_leaves_no_temp_file(tmp_path):
    with pytest.raises(UnicodeEncodeError):
        atomic_write_text(tmp_path / "f.txt", "\udcff", encoding="utf-8")
    assert list(tmp_path.iterdir()) == []
