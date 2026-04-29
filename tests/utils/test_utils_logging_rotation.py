"""Tests für FailTolerantRotatingFileHandler.

Hintergrund: bei parallelen agent-cli-Instanzen unter Windows scheitert
``os.rename(source, dest)`` während der Log-Rotation mit
``PermissionError [WinError 32]``, weil andere Prozesse die Datei offen
halten. Der Handler soll diesen Fehler verschlucken statt einen
Logging-Crash mit StackTrace zu produzieren.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agent_system.utils.logging import FailTolerantRotatingFileHandler


def test_rotate_swallows_permission_error(tmp_path: Path, capfd) -> None:
    """Wenn ``os.rename`` ein PermissionError wirft, wird die Rotation
    übersprungen — keine Exception bubbled up.
    """
    log_file = tmp_path / "test.log"
    handler = FailTolerantRotatingFileHandler(
        str(log_file), maxBytes=100, backupCount=2, encoding="utf-8",
    )
    try:
        with patch("os.rename", side_effect=PermissionError("locked")):
            handler.rotate(str(log_file), str(log_file) + ".1")  # must not raise
    finally:
        handler.close()

    captured = capfd.readouterr()
    assert "Rotation skipped" in captured.err


def test_rotate_swallows_oserror(tmp_path: Path) -> None:
    """OSError (Superklasse) wird ebenfalls verschluckt."""
    log_file = tmp_path / "test.log"
    handler = FailTolerantRotatingFileHandler(
        str(log_file), maxBytes=100, backupCount=2, encoding="utf-8",
    )
    try:
        with patch("os.rename", side_effect=OSError("disk error")):
            handler.rotate(str(log_file), str(log_file) + ".1")
    finally:
        handler.close()


def test_rotate_normal_path_delegates_to_super(tmp_path: Path) -> None:
    """Ohne Fehler delegiert ``rotate`` an die Parent-Implementierung
    (verifiziert via os.rename-Mock-Aufruf-Count).
    """
    log_file = tmp_path / "test.log"
    log_file.write_text("x")
    handler = FailTolerantRotatingFileHandler(
        str(log_file), maxBytes=100, backupCount=2, encoding="utf-8",
    )
    try:
        with patch("os.rename") as mock_rename:
            handler.rotate(str(log_file), str(log_file) + ".1")
            assert mock_rename.called
    finally:
        handler.close()
