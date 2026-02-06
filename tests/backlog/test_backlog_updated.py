import os
from pathlib import Path

BACKLOG_WITHOUT_UPDATED = '''# Backlog

## 1. Epics - open

- ☐ Epic 9996: Example epic
    - description: test epic
    - status: open
    - tasks:
        - ☐ Task 9005: done task
            - status: done

## 2. Epics - finished

'''


def run_script(backlog_path: Path) -> int:
    import importlib
    os.environ['BACKLOG_MD'] = str(backlog_path)
    mod = importlib.import_module('scripts.backlog')
    return mod.main(['update'])


def test_updated_added(tmp_path):
    p = tmp_path / 'bl.md'
    p.write_text(BACKLOG_WITHOUT_UPDATED, encoding='utf-8')
    rc = run_script(p)
    assert rc == 0
    txt = p.read_text(encoding='utf-8')
    # The updater inserts a closed date when moving finished epics
    assert '- closed:' in txt
