"""The archive on the command line: what it builds, and what it prints.

The three flags (``--list-archived``, ``--archive-sessions``,
``--restore-session``) go through the same ``SessionArchive`` as the panel, so
what is tested here is only the part the CLI owns: that ``build_archive``
hands the configured values down, that a CLI process brings the guard it CAN
have (the presence locks, which cross processes) and not the one it cannot
(this process's jobs), and that a refusal reaches the person as a line rather
than a traceback.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from agent_system.cli_utils.session_archive_cli import (
    build_archive,
    print_archived,
    run_restore,
    run_sweep,
)
from agent_system.config.models import (
    AgentSystemConfig,
    SessionArchiveConfig,
    SessionPresenceConfig,
)
from agent_system.services.session_manager import SessionManager

USER = "cli_user"


@pytest.fixture
def sm(tmp_path):
    return SessionManager(storage_path=str(tmp_path / "sessions"))


@pytest.fixture
def config(tmp_path):
    return AgentSystemConfig(
        session_presence=SessionPresenceConfig(enabled=True),
        session_archive=SessionArchiveConfig(
            retention_days=45,
            max_trees_per_sweep=7,
            archive_path=str(tmp_path / "elsewhere"),
        ),
    )


async def _tree(sm: SessionManager, root: str, children: list[str], days: float) -> None:
    await sm.create_session(user_id=USER, title=f"Chat {root}", session_id=root)
    for child in children:
        await sm.create_session(
            user_id=USER, title=child, session_id=child, parent_session_id=root)
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    user_dir = sm.storage_path / USER
    for session_id in [root, *children]:
        path = user_dir / f"{session_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["messages"] = [{"role": "user", "content": "hi"}]
        data["updated_at"] = stamp
        path.write_text(json.dumps(data), encoding="utf-8")
        old = time.time() - days * 86400
        os.utime(path, (old, old))
    for index_path in [user_dir / "index.json", *user_dir.glob(".subs.*.index.json")]:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for session_id in [root, *children]:
            if session_id in index:
                index[session_id]["updated_at"] = stamp
        index_path.write_text(json.dumps(index), encoding="utf-8")
    sm.clear_cache()


def test_build_archive_passes_the_configured_values(sm, config, tmp_path):
    archive = build_archive(sm, config)

    assert archive.retention_days == 45
    assert archive.max_trees_per_sweep == 7
    assert archive.archive_path == tmp_path / "elsewhere"


def test_build_archive_defaults_the_path_next_to_the_sessions(sm, tmp_path):
    archive = build_archive(sm, AgentSystemConfig())

    assert archive.retention_days == 30
    assert archive.archive_path == tmp_path / "session_archive"


def test_a_cli_process_brings_the_guard_that_crosses_processes(sm, config):
    """It cannot see the API's jobs -- the lock files are what it has."""
    archive = build_archive(sm, config)

    assert archive._presence is not None
    assert archive._busy_sessions is None
    assert archive.has_busy_guard is True


def test_without_presence_a_cli_sweep_has_no_guard_and_says_so(sm, tmp_path):
    """session_presence: false leaves the age limit alone with the job."""
    archive = build_archive(sm, AgentSystemConfig(
        session_archive=SessionArchiveConfig(archive_path=str(tmp_path / "a"))))

    assert archive._presence is None
    assert archive.has_busy_guard is False


@pytest.mark.asyncio
async def test_a_sweep_without_a_guard_warns_before_it_runs(sm, tmp_path, capsys):
    archive = build_archive(sm, AgentSystemConfig(
        session_archive=SessionArchiveConfig(archive_path=str(tmp_path / "a"))))

    await run_sweep(archive, USER, dry_run=True)

    assert "session_presence is off" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_sweep_with_a_guard_does_not_warn(sm, config, capsys):
    await run_sweep(build_archive(sm, config), USER, dry_run=True)

    assert "session_presence is off" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_an_empty_archive_says_where_things_go(sm, config, capsys):
    await print_archived(build_archive(sm, config), USER)

    out = capsys.readouterr().out
    assert "No archived sessions for cli_user" in out
    assert "45 days" in out  # the configured retention, not the default


@pytest.mark.asyncio
async def test_the_listing_prints_one_line_per_conversation(sm, config, capsys):
    await _tree(sm, "root_a", ["kid_a1", "kid_a2"], days=90)
    archive = build_archive(sm, config)
    await archive.archive_user(USER)
    capsys.readouterr()

    await print_archived(archive, USER)

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("root_a")]
    assert len(lines) == 1
    assert "Chat root_a" in lines[0]
    assert "    3" in lines[0]  # three sessions in the tree
    assert "--restore-session <id>" in out


@pytest.mark.asyncio
async def test_a_dry_sweep_reports_without_archiving(sm, config, capsys):
    await _tree(sm, "root_b", ["kid_b1"], days=90)
    archive = build_archive(sm, config)

    await run_sweep(archive, USER, dry_run=True)

    out = capsys.readouterr().out
    assert "Would archive 1 conversation(s) with 2 sessions" in out
    assert "older than 45 days" in out
    assert (sm.storage_path / USER / "root_b.json").exists()


@pytest.mark.asyncio
async def test_a_sweep_says_what_it_left_alone(sm, config, capsys):
    await _tree(sm, "root_c", [], days=90)
    await _tree(sm, "root_d", [], days=2)
    archive = build_archive(sm, config)

    await run_sweep(archive, USER)

    out = capsys.readouterr().out
    assert "Archived 1 conversation(s) with 1 sessions" in out
    assert "1 not old enough" in out
    assert (sm.storage_path / USER / "root_d.json").exists()


@pytest.mark.asyncio
async def test_a_capped_sweep_does_not_read_as_the_whole_job(sm, config, capsys):
    """Without this line the run prints what it did and nothing about the rest."""
    for index in range(3):
        await _tree(sm, f"root_cap{index}", [], days=90)
    archive = build_archive(sm, config)
    archive.max_trees_per_sweep = 1

    await run_sweep(archive, USER)

    out = capsys.readouterr().out
    assert "Archived 1 conversation(s)" in out
    assert "2 conversation(s) still waiting" in out
    assert "run it again" in out


@pytest.mark.asyncio
async def test_a_retention_given_on_the_command_line_wins(sm, config, capsys):
    await _tree(sm, "root_e", [], days=60)
    archive = build_archive(sm, config)

    await run_sweep(archive, USER, retention_days=90)

    out = capsys.readouterr().out
    assert "older than 90 days" in out
    assert "Archived 0 conversation(s)" in out
    assert (sm.storage_path / USER / "root_e.json").exists()


@pytest.mark.asyncio
async def test_restoring_prints_how_to_continue(sm, config, capsys):
    await _tree(sm, "root_f", ["kid_f1"], days=90)
    archive = build_archive(sm, config)
    await archive.archive_user(USER)
    capsys.readouterr()

    await run_restore(archive, USER, "root_f")

    out = capsys.readouterr().out
    assert "Restored 'Chat root_f' (2 sessions)" in out
    assert "--session root_f" in out
    assert (sm.storage_path / USER / "kid_f1.json").exists()


@pytest.mark.asyncio
async def test_a_refused_restore_is_a_line_not_a_traceback(sm, config, capsys):
    archive = build_archive(sm, config)

    await run_restore(archive, USER, "never_existed")

    out = capsys.readouterr().out
    assert out.startswith("Not restored: No archived session never_existed")
