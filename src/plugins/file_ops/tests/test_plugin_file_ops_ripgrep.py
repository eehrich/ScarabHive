"""Where ripgrep comes from, and that nothing unverified is ever run.

The download is faked here -- a test must not reach the network -- but the
archive is a real zip or tar, and the checks are the real SHA-256 checks.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from plugins.file_ops import ripgrep

#: Taken before the suite's stand-in replaces it (see conftest): the function
#: under the per-process cache, so every test asks afresh.
_LOCATE = ripgrep.locate.__wrapped__

MEMBER = f"ripgrep-{ripgrep.RG_VERSION}-x86_64-test/rg.exe"


def _archive(kind: str, body: bytes) -> bytes:
    buffer = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(MEMBER, body)
    else:
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            info = tarfile.TarInfo(MEMBER)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def release(tmp_path, monkeypatch):
    """A fake release for this platform; returns (serve, target path)."""
    monkeypatch.setattr(ripgrep.platform, "system", lambda: "TestOS")
    monkeypatch.setattr(ripgrep.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(ripgrep, "_EXE", ".exe")
    monkeypatch.setattr(ripgrep, "_on_path", lambda: None)
    target = tmp_path / "bin" / f"rg-{ripgrep.RG_VERSION}.exe"
    monkeypatch.setattr(ripgrep, "_cached_binary", lambda: target)
    # Running the fake binary is the one thing replaced: it is not a program.
    monkeypatch.setattr(ripgrep, "_usable",
                        lambda candidate: candidate if candidate and Path(candidate).is_file() else None)
    monkeypatch.delenv("FILE_OPS_RIPGREP_DOWNLOAD", raising=False)
    served = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(url, timeout):
        if isinstance(served["body"], Exception):
            raise served["body"]
        served["fetches"] = served.get("fetches", 0) + 1
        return Response(served["body"])

    monkeypatch.setattr(ripgrep.urllib.request, "urlopen", urlopen)

    def serve(body, binary: bytes = b"", kind: str = "zip", pinned: str = "") -> dict:
        served["body"] = body
        archive_hash = pinned or (_sha(body) if isinstance(body, bytes) else "0" * 64)
        monkeypatch.setitem(ripgrep._RELEASES, ("TestOS", "x86_64"),
                            ("x86_64-test", kind, archive_hash, _sha(binary)))
        return served

    return serve, target


def test_a_download_that_matches_its_pinned_hash_is_unpacked(release):
    serve, target = release
    serve(_archive("zip", b"MZ the binary"), b"MZ the binary")

    assert _LOCATE(None, True) == str(target)
    assert target.read_bytes() == b"MZ the binary"


def test_a_tarball_is_unpacked_the_same_way(release):
    serve, target = release
    serve(_archive("tar.gz", b"ELF the binary"), b"ELF the binary", kind="tar.gz")

    assert _LOCATE(None, True) == str(target)
    assert target.read_bytes() == b"ELF the binary"


def test_a_download_with_another_hash_is_never_written(release):
    """The whole point of pinning: an archive that was swapped is not run."""
    serve, target = release
    serve(_archive("zip", b"MZ something else"), b"MZ something else", pinned="0" * 64)

    assert _LOCATE(None, True) is None
    assert not target.exists() and not target.with_name(target.name + ".part").exists()


def test_a_cached_copy_that_was_changed_is_not_run(release):
    """data/cache/bin may be writable by an agent: the copy there is checked
    against the pinned binary hash every time, and replaced when it differs."""
    serve, target = release
    served = serve(_archive("zip", b"MZ the binary"), b"MZ the binary")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"MZ planted by somebody")

    assert _LOCATE(None, True) == str(target)
    assert target.read_bytes() == b"MZ the binary"
    assert served["fetches"] == 1


def test_a_failed_download_leaves_the_walker_not_an_error(release):
    serve, target = release
    serve(http.client.IncompleteRead(b"half"))

    assert _LOCATE(None, True) is None


def test_no_download_when_it_is_switched_off(release, monkeypatch):
    serve, target = release
    serve(_archive("zip", b"MZ the binary"), b"MZ the binary")

    assert _LOCATE(None, False) is None
    monkeypatch.setenv("FILE_OPS_RIPGREP_DOWNLOAD", "0")
    assert _LOCATE(None, True) is None
    assert not target.exists()


def test_off_keeps_the_walker_even_with_ripgrep_on_path(release, monkeypatch):
    monkeypatch.setattr(ripgrep, "_on_path", lambda: __file__)

    assert _LOCATE("off", True) is None
    assert _LOCATE(None, True) == __file__


def test_the_working_directory_is_never_searched_for_ripgrep(tmp_path, monkeypatch):
    """shutil.which looks there first on Windows: an agent that may write to
    "." would have had its rg run by the next search."""
    planted = tmp_path / "work"
    planted.mkdir()
    (planted / ("rg" + ripgrep._EXE)).write_bytes(b"planted")
    elsewhere = tmp_path / "bin"
    elsewhere.mkdir()
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(planted), str(elsewhere)]))

    assert ripgrep._on_path() is None


def test_the_cached_copy_is_checked_again_once_it_changed(tmp_path, monkeypatch):
    """locate() is cached per process; the file is not. A copy replaced after
    the first search ran unchecked from then on."""
    target = tmp_path / f"rg-{ripgrep.RG_VERSION}.exe"
    target.write_bytes(b"MZ the binary")
    monkeypatch.setattr(ripgrep, "_cached_binary", lambda: target)
    monkeypatch.setattr(ripgrep, "_release",
                        lambda: (("TestOS", "x86_64"),
                                 ("x86_64-test", "zip", "0" * 64, _sha(b"MZ the binary"))))
    monkeypatch.setattr(ripgrep, "_last_verified", None)
    binary = str(target.resolve())

    assert ripgrep.still_trusted(binary)
    target.write_bytes(b"MZ swapped in later")
    os.utime(target, ns=(1, 1))

    assert not ripgrep.still_trusted(binary)
    assert ripgrep.still_trusted(__file__), "a ripgrep from PATH is the operator's choice"
