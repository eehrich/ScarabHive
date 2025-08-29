import os
import importlib
import io
import contextlib
from pathlib import Path


MINI = '''# Backlog

## 1. Epics - open

☐ Epic 0001: Open Epic
  - description: test
  - status: open
    - tasks:
        - ☐ Task 0002: Open task
            - status: open

## 2. Epics - finished

✅ Epic 0003: Finished Epic
  - description: done
  - status: done
    - tasks:
        - ✅ Task 0004: Done task
            - status: done

'''


def run_cli(argv, env=None):
    if env is None:
        env = {}
    # import in-process and capture stdout/stderr
    mod = importlib.import_module('scripts.backlog')
    buf = io.StringIO()
    old = dict(os.environ)
    try:
        os.environ.update(env)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                rc = mod.main(argv)
            except SystemExit as e:
                rc = int(e.code or 0)
    finally:
        os.environ.clear(); os.environ.update(old)
    return rc, buf.getvalue()


def test_show_multiple_positional(tmp_path: Path):
    p = tmp_path / 'bl.md'
    p.write_text(MINI, encoding='utf-8')
    rc, out = run_cli(['show', '0001', '0002', '--file', str(p), '--no-color'])
    assert rc == 0, out
    assert 'Epic 0001:' in out
    assert 'Task 0002:' in out


def test_show_legacy_flag_multiple(tmp_path: Path):
    p = tmp_path / 'bl.md'
    p.write_text(MINI, encoding='utf-8')
    # legacy --id accepts multiple values
    rc, out = run_cli(['show', '--id', '0001', '0002', '--file', str(p), '--no-color'])
    assert rc == 0, out
    assert 'Epic 0001:' in out
    assert 'Task 0002:' in out


def test_show_missing_id_returns_nonzero(tmp_path: Path):
    p = tmp_path / 'bl.md'
    p.write_text(MINI, encoding='utf-8')
    rc, out = run_cli(['show', '9999', '--file', str(p), '--no-color'])
    assert rc != 0
    assert 'not found' in out.lower()
