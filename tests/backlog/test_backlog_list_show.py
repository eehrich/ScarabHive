import importlib

SAMPLE = """
# Backlog

## 1. Epics - open

- \u2610 Epic 0001: First Epic
      - status: open
      - tasks:
            - \u2610 Task 0001: Task One
                  - status: open
                  - added: 2025-08-01

- \u2610 Epic 0002: Second Epic
      - status: open
      - tasks:
            - \u2610 Task 0002: Task Two
                  - status: done
                  - added: 2025-07-01
                  - closed: 2025-07-02

## 2. Epics - finished

"""


def test_list_and_show_cli(tmp_path, capfd):
      p = tmp_path / "b.md"
      p.write_text(SAMPLE, encoding="utf-8")
      mod = importlib.import_module("scripts.backlog")

      # list without color (default: epics only)
      rc = mod.main(["list", "--file", str(p), "--no-color"])
      out, err = capfd.readouterr()
      assert rc == 0
      assert "Epics:" in out
      # tasks are not printed by default; request them explicitly
      rc = mod.main(["list", "--file", str(p), "--only", "all", "--no-color"]) 
      out, err = capfd.readouterr()
      assert "Tasks:" in out
      assert "Epic 0001" in out
      assert "Task 0002" in out

      # show epic
      rc = mod.main(["show", "--id", "0001", "--file", str(p), "--no-color"])
      out, err = capfd.readouterr()
      assert rc == 0
      assert "Epic 0001: First Epic" in out
      assert "tasks:" in out

      # show task with color flag (output includes ANSI sequences)
      rc = mod.main(["show", "--id", "0002", "--file", str(p), "--color"]) 
      out, err = capfd.readouterr()
      assert rc == 0
      # colored output may insert ANSI codes between tokens, so check tokens separately
      assert "Task" in out
      assert "0002" in out
      assert "\x1b[" in out
