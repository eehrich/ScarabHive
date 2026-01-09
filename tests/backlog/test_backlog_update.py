from pathlib import Path
import os

BACKLOG_CONTENT = '''# Backlog

## 1. Epics - open

- ☐ Epic 9999: Example epic
    - description: test epic
    - status: open
    - tasks:
        - ☐ Task 9000: done task
            - status: done
        - ☐ Task 9001: another done
            - status: done

## 2. Epics - finished

'''

BACKLOG_NO_MOVE = '''# Backlog

## 1. Epics - open

- ☐ Epic 9998: Example epic
    - description: test epic
    - status: open
    - tasks:
        - ☐ Task 9002: not done
            - status: open

## 2. Epics - finished

'''


def run_script(backlog_path: Path) -> int:
    # Use the new subcommand interface rather than an external script.
    import importlib
    # set BACKLOG_MD in the current process so the in-process call uses the test file
    os.environ['BACKLOG_MD'] = str(backlog_path)
    mod = importlib.import_module('scripts.backlog')
    return mod.main(['update'])


def test_move_finished_epic(tmp_path):
    p = tmp_path / 'bl.md'
    p.write_text(BACKLOG_CONTENT, encoding='utf-8')
    rc = run_script(p)
    assert rc == 0
    txt = p.read_text(encoding='utf-8')
    assert 'Epic 9999' in txt
    # moved epic should no longer appear in open section
    assert 'Epic 9999' in txt.split('## 2. Epics - finished')[1]


def test_no_move(tmp_path):
    p = tmp_path / 'bl.md'
    p.write_text(BACKLOG_NO_MOVE, encoding='utf-8')
    rc = run_script(p)
    assert rc == 0
    txt = p.read_text(encoding='utf-8')
    # epic remains in open section
    assert 'Epic 9998' in txt.split('## 1. Epics - open')[1]
