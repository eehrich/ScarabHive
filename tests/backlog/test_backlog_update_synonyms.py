import tempfile
from pathlib import Path
import subprocess
import sys
import os

BACKLOG_SYNONYMS = '''# Backlog

## 1. Epics - open

- ☐ Epic 9997: Example epic
    - description: test epic
    - status: open
    - tasks:
        - ☐ Task 9003: done task
            - status: Finished
        - ☐ Task 9004: another done
            - status: RESOLVED

## 2. Epics - finished

'''


def run_script(backlog_path: Path) -> int:
    import importlib, os
    os.environ['BACKLOG_MD'] = str(backlog_path)
    mod = importlib.import_module('scripts.backlog')
    return mod.main(['update'])


def test_synonyms_move(tmp_path):
    p = tmp_path / 'bl.md'
    p.write_text(BACKLOG_SYNONYMS, encoding='utf-8')
    rc = run_script(p)
    assert rc == 0
    txt = p.read_text(encoding='utf-8')
    assert 'Epic 9997' in txt.split('## 2. Epics - finished')[1]
