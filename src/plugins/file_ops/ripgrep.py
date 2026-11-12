"""Where the ripgrep binary comes from: PATH, the cache, or one verified download.

grep_search and search_files hand the walk and the matching to ripgrep when
one is found (see ``textsearch``), and walk in Python when none is. Measured
on 12.11.2026 with the coder harness tree (45.920 files, nine checkouts of
this repository under data/workspace among them): a grep that matches nothing
took 24 s warm and 251 s cold in the Python walker, and 0.8 s in ripgrep.

The order is the one opencode uses:

1. ``search.ripgrep_path`` in the instance config -- a path, or ``off`` to
   keep the Python walker;
2. ``rg`` on PATH -- absolute PATH entries only, never the working directory,
   and on Windows only ``rg.exe``: ``shutil.which`` looks in the working
   directory first there and takes ``rg.bat`` too, and an agent that may write
   to ``.`` would have had its script run by the next search;
3. the copy downloaded earlier into ``data/cache/bin`` -- run only while its
   SHA-256 is the pinned one, since an agent may be able to write there too;
4. a download of the official release (``search.ripgrep_download``, on by
   default; ``FILE_OPS_RIPGREP_DOWNLOAD=0`` switches it off for a process --
   the test suite does).

A download is written only when its SHA-256 matches the hash pinned below,
and nothing else is ever run. The PyPI package ``ripgrep`` was looked at and
not taken: its Windows binary is not the one the ripgrep release ships, and
nothing says who built it.
"""

from __future__ import annotations

import functools
import hashlib
import io
import logging
import os
import platform
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

from filelock import FileLock

from agent_system.paths import data_path

logger = logging.getLogger(__name__)

RG_VERSION = "15.1.0"

#: (system, machine) -> (release target, archive type, SHA-256 of the archive,
#: SHA-256 of the rg binary in it). The archive hashes are the ones the release
#: publishes next to each asset (``<asset>.sha256``); on 12.11.2026 each
#: matched a fresh download and the list opencode pins for the same version.
#: The binary hashes were taken from those verified archives.
_RELEASES = {
    ("Windows", "x86_64"): (
        "x86_64-pc-windows-msvc", "zip",
        "124510b94b6baa3380d051fdf4650eaa80a302c876d611e9dba0b2e18d87493a",
        "decdd4992f3f1b9a5ef9898f1b40ab16886d579d6516b4efd3d5eaa19364e408"),
    ("Linux", "x86_64"): (
        "x86_64-unknown-linux-musl", "tar.gz",
        "1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599",
        "ebeaf56f8a25e102e9419933423738b3a2a613a444fd749d695e15eba53f71f2"),
    ("Linux", "aarch64"): (
        "aarch64-unknown-linux-gnu", "tar.gz",
        "2b661c6ef508e902f388e9098d9c4c5aca72c87b55922d94abdba830b4dc885e",
        "968cabe8efed72fd8fd482cb76b6084fcb695fc5293af7fb62296b02f487fb69"),
    ("Darwin", "x86_64"): (
        "x86_64-apple-darwin", "tar.gz",
        "64811cb24e77cac3057d6c40b63ac9becf9082eedd54ca411b475b755d334882",
        "3bafa7e6ee51ba3ac4ed065883484a309be09b26ea6dad561ae4049bfe049c50"),
    ("Darwin", "aarch64"): (
        "aarch64-apple-darwin", "tar.gz",
        "378e973289176ca0c6054054ee7f631a065874a352bf43f0fa60ef079b6ba715",
        "4fdf1d8365af224bc70e3c1490d8461d859c37cc70e739a11e987af0215f3e94"),
}
_MACHINES = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}

#: The oldest ripgrep whose flags textsearch relies on (--no-require-git came in 11).
_MIN_MAJOR = 13
_DOWNLOAD_TIMEOUT = 60
_EXE = ".exe" if os.name == "nt" else ""


def _release():
    key = (platform.system(), _MACHINES.get(platform.machine().lower(), platform.machine()))
    return key, _RELEASES.get(key)


def _usable(candidate: Optional[str]) -> Optional[str]:
    """*candidate*, made absolute, if it runs and is new enough, else None.

    Absolute because every search starts ripgrep with the search root as its
    cwd, and on POSIX a relative program is looked up from there:
    ``data/cache/bin/rg-...`` would have failed to start on a Linux server,
    and every search would have walked in Python without a word.
    """
    if not candidate or not Path(candidate).is_file():
        return None
    candidate = str(Path(candidate).resolve())
    try:
        out = subprocess.run([candidate, "--version"], capture_output=True, text=True,
                             timeout=10, stdin=subprocess.DEVNULL).stdout
        major = int(out.split()[1].split(".")[0])
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None
    return candidate if major >= _MIN_MAJOR else None


def _on_path() -> Optional[str]:
    """``rg`` from an absolute PATH entry that is not the working directory."""
    here = os.path.normcase(os.path.abspath(os.getcwd()))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        if os.path.normcase(os.path.abspath(entry)) == here:
            continue
        candidate = os.path.join(entry, "rg" + _EXE)
        if os.path.isfile(candidate) and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return candidate
    return None


def _cached_binary() -> Path:
    return Path(data_path("cache", "bin")) / f"rg-{RG_VERSION}{_EXE}"


def _verified(path: Path) -> bool:
    """Whether *path* is byte for byte the binary of the pinned release."""
    _, release = _release()
    if release is None:
        return False
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == release[3]
    except OSError:
        return False


def _download(target: Path) -> Optional[str]:
    """Fetch the release for this platform, check it, and unpack ``rg`` to *target*.

    Every failure answers None -- the search then walks in Python -- and is
    logged: a failed download must not turn a search into an error.
    """
    key, release = _release()
    if release is None:
        logger.info("FILE_OPS: no pinned ripgrep release for %s -- searching in Python", key)
        return None
    triple, kind, pinned, _ = release
    stem = f"ripgrep-{RG_VERSION}-{triple}"
    url = f"https://github.com/BurntSushi/ripgrep/releases/download/{RG_VERSION}/{stem}.{kind}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # One process downloads, the others wait and then find the file.
        with FileLock(str(target.parent / ".rg-download.lock"), timeout=_DOWNLOAD_TIMEOUT * 2):
            if _verified(target):
                return _usable(str(target))
            logger.info("FILE_OPS: downloading ripgrep %s from %s", RG_VERSION, url)
            with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as response:  # noqa: S310 - fixed https URL
                archive = response.read()
            digest = hashlib.sha256(archive).hexdigest()
            if digest != pinned:
                logger.warning("FILE_OPS: ripgrep download from %s has SHA-256 %s, expected %s "
                               "-- discarded, searching in Python", url, digest, pinned)
                return None
            member = f"{stem}/rg{_EXE}"
            if kind == "zip":
                binary = zipfile.ZipFile(io.BytesIO(archive)).read(member)
            else:
                with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                    handle = tar.extractfile(member)
                    if handle is None:
                        raise KeyError(member)
                    binary = handle.read()
            partial = target.with_name(target.name + ".part")
            partial.write_bytes(binary)
            partial.chmod(0o755)
            os.replace(partial, target)
    except Exception as exc:  # noqa: BLE001 - network, archive, lock, disk: the walker answers instead
        logger.warning("FILE_OPS: could not fetch ripgrep (%s: %s) -- searching in Python",
                       type(exc).__name__, exc)
        return None
    return _usable(str(target)) if _verified(target) else None


#: (binary, its size, mtime and file id) at the last check that passed.
_last_verified: Optional[tuple] = None


def still_trusted(binary: str) -> bool:
    """Whether *binary* may run now -- asked before every search.

    ``locate`` is cached per process, the file is not: the copy in
    data/cache/bin could be replaced after the first search, and from then on
    the replacement ran unchecked. That copy is hashed again whenever its
    size, mtime or file id changed since the last check that passed; a ripgrep
    from PATH or from ``search.ripgrep_path`` is the operator's choice.
    """
    global _last_verified
    try:
        cached = str(_cached_binary().resolve())
    except OSError:
        return True
    if os.path.normcase(binary) != os.path.normcase(cached):
        return True
    try:
        info = os.stat(binary)
    except OSError:
        return False
    key = (binary, info.st_size, info.st_mtime_ns, info.st_ino)
    if key == _last_verified:
        return True
    if not _verified(Path(binary)):
        logger.warning("FILE_OPS: %s changed and is not the pinned ripgrep any more -- not run",
                       binary)
        return False
    _last_verified = key
    return True


@functools.lru_cache(maxsize=8)
def locate(path_setting: Optional[str] = None, download: bool = True) -> Optional[str]:
    """The ripgrep binary to search with, or None for the Python walker.

    Cached per process and per setting: a failed download is not retried on
    every grep. ``path_setting`` is ``search.ripgrep_path``.
    """
    setting = str(path_setting or "").strip()
    if setting.lower() in ("off", "false", "none", "python"):
        return None
    if setting:
        found = _usable(setting)
        if found is None:
            logger.warning("FILE_OPS: search.ripgrep_path %r is not a usable ripgrep "
                           "-- searching in Python", setting)
        return found
    found = _usable(_on_path())
    if found:
        return found
    cached = _cached_binary()
    if _verified(cached):
        found = _usable(str(cached))
        if found:
            return found
    elif cached.exists():
        logger.warning("FILE_OPS: %s is not the pinned ripgrep %s -- not run", cached, RG_VERSION)
    if not download or os.getenv("FILE_OPS_RIPGREP_DOWNLOAD", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    return _download(cached)
