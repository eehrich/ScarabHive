import importlib
import io
import contextlib
import os
from pathlib import Path


MINI = '''# Backlog

## 1. Epics - open

☐ Epic 0001: Open Epic
  - status: open
    - tasks:
        - ☐ Task 0002: Open task
            - status: open

## 2. Epics - finished

✅ Epic 0003: Finished Epic
  - status: done
    - tasks:
        - ✅ Task 0004: Done task
            - status: done

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
        os.environ.clear(); os.environ.update(old)
    return rc, buf.getvalue()


def test_list_state_filters(tmp_path: Path):
    p = tmp_path / 'bl.md'
    p.write_text(MINI, encoding='utf-8')
    rc, out = run_cli(['list', '--state', 'open', '--file', str(p), '--no-color'])
    assert rc == 0
    assert 'Epic 0001:' in out
    assert 'Epic 0003:' not in out

    rc, out = run_cli(['list', '--state', 'finished', '--file', str(p), '--no-color'])
    assert rc == 0
    assert 'Epic 0003:' in out
    assert 'Epic 0001:' not in out


def test_list_ids_only(tmp_path: Path):
    p = tmp_path / 'bl.md'
    p.write_text(MINI, encoding='utf-8')
    rc, out = run_cli(['list', '--ids-only', '--state', 'all', '--file', str(p), '--no-color'])
    assert rc == 0
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    # Expect numeric ids like 0001, 0002, etc.
    assert '0001' in lines
    assert '0002' in lines
    assert '0003' in lines
