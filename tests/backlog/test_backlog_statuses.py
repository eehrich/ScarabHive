import os
from pathlib import Path
import subprocess
import sys


GOOD = '''# Backlog

## 1. Epics - open

☐ Epic 9000: Test epic
  - description: test
  - status: open
  - tasks:
    - Task 9001: subtask
      - status: done

'''

BAD = '''# Backlog

## 1. Epics - open

☐ Epic 9002: Test epic bad
  - description: test
  - status: open
  - tasks:
    - Task 9003: subtask
      - status: foobar

'''


def run_validator(path: Path) -> tuple[int, str]:
  # Run the canonical updater in-process and capture stdout/stderr.
  import importlib
  import io
  import contextlib
  os.environ['BACKLOG_MD'] = str(path)
  mod = importlib.import_module('scripts.backlog')
  buf = io.StringIO()
  try:
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
      rc = mod.main(['update'])
  except SystemExit as e:
    rc = int(e.code or 0)
  out = buf.getvalue()
  return rc, out


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
