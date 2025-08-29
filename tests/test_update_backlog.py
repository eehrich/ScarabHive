import tempfile
from pathlib import Path
import subprocess
import sys
import os
from pathlib import Path

BACKLOG_CONTENT = '''# Backlog

## 1. Epics - open

☐ Epic 9999: Example epic
 - description: test epic
 - status: ☐
 - Subtasks:
     - Task 9000: done task
         - status: done
     - Task 9001: another done
         - status: done

## 2. Epics - finished

'''

BACKLOG_NO_MOVE = '''# Backlog

## 1. Epics - open

☐ Epic 9998: Example epic
 - description: test epic
 - status: ☐
 - Subtasks:
     - Task 9002: not done
         - status: open

## 2. Epics - finished

'''


def run_script(backlog_path: Path) -> int:
    env = dict(**os.environ)
    env['BACKLOG_MD'] = str(backlog_path)
    # canonical implementation location under src
    script = Path('.') / 'src' / 'scripts' / 'update_backlog.py'
    res = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True)
    print('stdout:', res.stdout)
    print('stderr:', res.stderr)
    return res.returncode


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
