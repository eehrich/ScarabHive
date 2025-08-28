import importlib
from scripts.backlog_tool import parser as bl

SAMPLE = """
# Backlog

## 1. Epics - open

- ☐ Epic 0001: First Epic
  - status: open
  - Subtasks:
    - ☐ Task 0001: Task One
      - status: open
      - added: 2025-08-01

- ☐ Epic 0002: Second Epic
  - status: open
  - Subtasks:

## 2. Epics - finished

"""


def test_move_task_write(tmp_path):
    p = tmp_path / "b.md"
    p.write_text(SAMPLE, encoding="utf-8")
    mod = importlib.import_module("scripts.backlog")
    rc = mod.main(["move-task", "--task", "0001", "--to-epic", "0002", "--file", str(p), "--write"])
    assert rc == 0
    # re-parse and assert task exists in dest epic
    lines = bl.read_file(str(p))
    backlog = bl.parse(lines)
    dest = next(e for e in backlog.epics_open if e.id == '0002')
    assert any(t.id == '0001' for t in dest.subtasks)


def test_update_status_write(tmp_path):
    p = tmp_path / "b.md"
    p.write_text(SAMPLE, encoding="utf-8")
    mod = importlib.import_module("scripts.backlog")
    rc = mod.main(["update-status", "--task", "0001", "--status", "done", "--file", str(p), "--write"])
    assert rc == 0
    lines = bl.read_file(str(p))
    backlog = bl.parse(lines)
    epic, t = bl.find_task(backlog, '0001')
    assert t.status == 'done'
    assert t.closed is not None
