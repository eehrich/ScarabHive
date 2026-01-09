import subprocess
import sys
import pathlib

PY = sys.executable
MOD = 'scripts.backlog'

BACKLOG_TEMPLATE = """# Backlog\n\n## 1. Epics - open\n\n- Epic 0001: Sample Epic\n  - status: open\n  - tasks:\n    - Task 0002: First task\n      - status: open\n    - Task 0003: Second task\n      - status: open\n\n## 2. Epics - finished\n\n""".lstrip()

def run(args, cwd):
    cmd = [PY, '-m', MOD] + args
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')


def test_edit_multi_ids_updates_tasks(tmp_path: pathlib.Path):
    bl = tmp_path / 'backlog.md'
    bl.write_text(BACKLOG_TEMPLATE, encoding='utf-8')

    # dry-run first
    r = run(['edit', '0002', '0003', '--set', 'status=in progress'], tmp_path)
    assert r.returncode == 0, r.stderr
    assert 'Dry-run: would update tasks: 0002, 0003' in r.stdout
    assert 'in progress' not in bl.read_text(encoding='utf-8')  # unchanged

    # write changes
    r = run(['edit', '0002', '0003', '--set', 'status=in progress', '--write'], tmp_path)
    assert r.returncode == 0, r.stderr
    txt = bl.read_text(encoding='utf-8')
    assert '- status: in progress' in txt
    # both tasks should have updated status lines (count 2 occurrences)
    assert txt.count('- status: in progress') == 2


def test_edit_multi_ids_mixed_epic_and_task(tmp_path: pathlib.Path):
    bl = tmp_path / 'backlog.md'
    bl.write_text(BACKLOG_TEMPLATE, encoding='utf-8')

    r = run(['edit', '0001', '0002', '--set', 'status=done', '--write'], tmp_path)
    assert r.returncode == 0, r.stderr
    txt = bl.read_text(encoding='utf-8')
    # Epic 0001 status updated
    # Epic and task both updated (two occurrences of status: done)
    assert txt.count('- status: done') == 2 or txt.count('  - status: done') == 2


def test_edit_multi_ids_missing_id(tmp_path: pathlib.Path):
    bl = tmp_path / 'backlog.md'
    bl.write_text(BACKLOG_TEMPLATE, encoding='utf-8')

    r = run(['edit', '0002', '9999', '--set', 'status=done'], tmp_path)
    # Should return non-zero because one id missing
    assert r.returncode != 0
    assert "ERROR: id '9999' not found. Use 'backlog list' to see available items." in r.stderr
    # existing id should still show dry-run message
    assert 'Dry-run: would update tasks: 0002' in r.stdout
