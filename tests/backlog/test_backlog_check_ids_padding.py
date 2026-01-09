import importlib
import io
import contextlib
import os
from pathlib import Path


BL = '''# Backlog

## 1. Epics - open

☐ Epic 0001: Epic A
  - tasks:
    - ☐ Task 0013: Task with padded id
      - status: open

☐ Epic 0002: Epic B
  - tasks:
    - ☐ Task 13: Task with short id
      - status: open

'''


def run_cli(argv, env=None):
    mod = importlib.import_module('scripts.backlog')
    buf = io.StringIO()
    old = dict(os.environ)
    try:
        if env:
            os.environ.update(env)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                rc = mod.main(argv)
            except SystemExit as e:
                rc = int(e.code or 0)
    finally:
        os.environ.clear()
        os.environ.update(old)
    return rc, buf.getvalue()


def test_check_ids_detects_padded_duplicates(tmp_path: Path):
    p = tmp_path / 'bl.md'
    p.write_text(BL, encoding='utf-8')
    rc, out = run_cli(['check-ids', '--file', str(p)])
    assert rc != 0
    assert '0013' in out
