import importlib

SAMPLE = (
  "# Backlog\n\n"
  "## 1. Epics - open\n\n"
  "- [ ] Epic 0001: First Epic\n"
  "  - status: open\n"
  "  - tasks:\n"
  "    - [ ] Task 0001: Task One\n"
  "      - status: open\n"
  "      - added: 2025-08-01\n\n"
  "- [ ] Epic 0002: Second Epic\n"
  "  - status: open\n"
  "  - tasks:\n\n"
  "## 2. Epics - finished\n"
)


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
  rc = mod.main(["edit", "0001", "--set", "status=done", "--file", str(p)])
  out, err = capfd.readouterr()
  assert rc == 0
  assert "Dry-run: updated task 0001 (Epic 0001)" in out
