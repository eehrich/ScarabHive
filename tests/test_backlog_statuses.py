import os
from pathlib import Path
import subprocess
import sys


GOOD = '''# Backlog

## 1. Epics - open

☐ Epic 9000: Test epic
  - description: test
  - status: open
  - Subtasks:
    - Task 9001: subtask
      - status: done

'''

BAD = '''# Backlog

## 1. Epics - open

☐ Epic 9002: Test epic bad
  - description: test
  - status: open
  - Subtasks:
    - Task 9003: subtask
      - status: foobar

'''


def run_validator(path: Path) -> tuple[int, str]:
    env = dict(**os.environ)
    env['BACKLOG_MD'] = str(path)
    res = subprocess.run(
        [sys.executable, 'scripts/update_backlog.py'],
        cwd=Path('.'),
        env=env,
        capture_output=True,
        text=True,
    )
    return res.returncode, res.stdout + res.stderr


def test_good(tmp_path):
    p = tmp_path / 'bl.md'
    p.write_text(GOOD, encoding='utf-8')
    rc, out = run_validator(p)
    assert rc == 0, out


def test_bad(tmp_path):
  p = tmp_path / 'bl.md'
  p.write_text(BAD, encoding='utf-8')
  rc, out = run_validator(p)
  assert rc != 0
  assert 'unknown status' in out.lower() or 'found unknown status' in out.lower()
