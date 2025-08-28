from pathlib import Path
import importlib

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


def test_move_task_cli(tmp_path, capfd):
    p = tmp_path / "b.md"
    p.write_text(SAMPLE, encoding="utf-8")
    mod = importlib.import_module("scripts.backlog")
    rc = mod.main(["move-task", "--task", "0001", "--to-epic", "0002", "--file", str(p)])
    out, err = capfd.readouterr()
    assert rc == 0
    assert "Dry-run: moved task 0001 -> epic 0002" in out


def test_update_status_cli(tmp_path, capfd):
    p = tmp_path / "b.md"
    p.write_text(SAMPLE, encoding="utf-8")
    mod = importlib.import_module("scripts.backlog")
    rc = mod.main(["update-status", "--task", "0001", "--status", "done", "--file", str(p)])
    out, err = capfd.readouterr()
    assert rc == 0
    assert "Dry-run: updated task 0001 status -> done" in out
