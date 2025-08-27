import os
from pathlib import Path
import subprocess
import sys

BACKLOG_WITHOUT_UPDATED = '''# Backlog

## 1. Epics - open

☐ Epic 9996: Example epic
 - description: test epic
 - status: ☐
 - Subtasks:
	 - Task 9005: done task
		 - status: done

## 2. Epics - finished

'''


def run_script(backlog_path: Path) -> int:
    env = dict(**os.environ)
    env['BACKLOG_MD'] = str(backlog_path)
    script = Path('.') / '.prompts' / 'scripts' / 'update_backlog.py'
    res = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True)
    print('stdout:', res.stdout)
    print('stderr:', res.stderr)
    return res.returncode


def test_updated_added(tmp_path):
    p = tmp_path / 'bl.md'
    p.write_text(BACKLOG_WITHOUT_UPDATED, encoding='utf-8')
    rc = run_script(p)
    assert rc == 0
    txt = p.read_text(encoding='utf-8')
    assert '- updated:' in txt
