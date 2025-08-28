import tempfile
from pathlib import Path
import subprocess
import sys
import os

BACKLOG_SYNONYMS = '''# Backlog

## 1. Epics - open

☐ Epic 9997: Example epic
 - description: test epic
 - status: ☐
 - Subtasks:
	 - Task 9003: done task
		 - status: Finished
	 - Task 9004: another done
		 - status: RESOLVED

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


def test_synonyms_move(tmp_path):
    p = tmp_path / 'bl.md'
    p.write_text(BACKLOG_SYNONYMS, encoding='utf-8')
    rc = run_script(p)
    assert rc == 0
    txt = p.read_text(encoding='utf-8')
    assert 'Epic 9997' in txt.split('## 2. Epics - finished')[1]
