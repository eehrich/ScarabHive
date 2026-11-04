"""Every job-tracker call closes its connection (a `with sqlite3.connect()` does not)."""
import sqlite3

from plugins.comfyui import job_tracker as module


def test_connections_are_closed(tmp_path, monkeypatch):
    opened = []
    real = sqlite3.connect

    def tracking(*a, **k):
        conn = real(*a, **k)
        opened.append(conn)
        return conn

    monkeypatch.setattr(module.sqlite3, "connect", tracking)
    tracker = module.ComfyUIJobTracker(tmp_path / "jobs.db")
    tracker.get_stats()
    assert opened
    for conn in opened:
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            continue  # closed
        raise AssertionError("a connection was left open")
