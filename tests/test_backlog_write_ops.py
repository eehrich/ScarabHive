import importlib
from pathlib import Path
from scripts.backlog_tool import parser as bl


def make_minimal_backlog(path: Path, epic_id: str = None) -> None:
    content = [
        "# Backlog",
        "",
        "## 1. Epics - open",
        "",
    ]
    if epic_id:
        content.extend([
            f"- ☐ Epic {epic_id}: Sample Epic",
            "  - status: open",
            "  - tasks:",
            "",
        ])
    content.extend(["## 2. Epics - finished", ""]) 
    path.write_text("\n".join(content), encoding="utf-8")


def test_add_epic_write_creates_backup(tmp_path):
    mod = importlib.import_module("scripts.backlog")
    p = tmp_path / "backlog.md"
    make_minimal_backlog(p)
    rc = mod.main(["add-epic", "--title", "New Epic", "--file", str(p), "--write"])
    assert rc == 0
    text = p.read_text(encoding="utf-8")
    assert "Epic" in text
    backups = (p.parent / ".backups")
    assert backups.exists()
    files = list(backups.glob(p.name + ".*.bak"))
    assert files, "Expected a backup file to be created"


def test_add_task_write_appends_task_and_backup(tmp_path):
    mod = importlib.import_module("scripts.backlog")
    p = tmp_path / "backlog.md"
    make_minimal_backlog(p, epic_id="0018")
    rc = mod.main(["add-task", "--title", "New Task", "--epic", "0018", "--file", str(p), "--write"])
    assert rc == 0
    text = p.read_text(encoding="utf-8")
    assert "Task" in text
    backups = (p.parent / ".backups")
    assert backups.exists()
    files = list(backups.glob(p.name + ".*.bak"))
    assert files


def test_add_task_write_requires_epic(tmp_path, capfd):
    """Using --write without --epic should fail with an error and non-zero return."""
    mod = importlib.import_module("scripts.backlog")
    p = tmp_path / "backlog.md"
    make_minimal_backlog(p)
    rc = mod.main(["add-task", "--title", "Orphan Task", "--file", str(p), "--write"])
    assert rc != 0
    out, err = capfd.readouterr()
    assert "--epic is required when using --write" in err


def test_fix_format_reassigns_duplicates_and_backups(tmp_path):
    mod = importlib.import_module("scripts.backlog")
    p = tmp_path / "backlog.md"
    # create a backlog with duplicate task ids and placeholder closed
    lines = [
        "# Backlog",
        "",
        "## 1. Epics - open",
        "",
        "- ☐ Epic 0001: Dup Epic",
        "  - status: open",
    "  - tasks:",
        "    - ☐ Task 0001: First",
        "      - status: open",
        "      - added: 2025-08-28",
        "    - ☐ Task 0001: Second",
        "      - status: open",
        "      - closed: —",
        "",
        "## 2. Epics - finished",
        "",
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    rc = mod.main(["fix-format", "--file", str(p), "--write"])
    assert rc == 0
    # parse result: no duplicate ids
    from scripts.backlog_tool import parser as bl
    blines = bl.read_file(str(p))
    backlog = bl.parse(blines)
    ids = [t.id for e in backlog.epics_open + backlog.epics_finished for t in e.tasks]
    assert len(ids) == len(set(ids)), "Expected duplicate task ids to be reassigned"
    backups = (p.parent / ".backups")
    assert backups.exists()
    files = list(backups.glob(p.name + ".*.bak"))
    assert files


def test_check_ids_detects_duplicates(tmp_path):
    mod = importlib.import_module("scripts.backlog")
    p = tmp_path / "backlog.md"
    lines = [
        "# Backlog",
        "",
        "## 1. Epics - open",
        "",
        "- ☐ Epic 0002: Example",
        "  - status: open",
    "  - tasks:",
        "    - ☐ Task 0100: A",
        "    - ☐ Task 0100: B",
        "",
        "## 2. Epics - finished",
        "",
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    rc = mod.main(["check-ids", "--file", str(p)])
    assert rc != 0

SAMPLE = """
# Backlog

## 1. Epics - open

- ☐ Epic 0001: First Epic
  - status: open
    - tasks:
    - ☐ Task 0001: Task One
      - status: open
      - added: 2025-08-01

- ☐ Epic 0002: Second Epic
  - status: open
    - tasks:

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
    assert any(t.id == '0001' for t in dest.tasks)


def test_update_status_write(tmp_path):
    p = tmp_path / "b.md"
    p.write_text(SAMPLE, encoding="utf-8")
    mod = importlib.import_module("scripts.backlog")
    rc = mod.main(["edit", "0001", "--set", "status=done", "--file", str(p), "--write"])
    assert rc == 0
    lines = bl.read_file(str(p))
    backlog = bl.parse(lines)
    epic, t = bl.find_task(backlog, '0001')
    assert t.status == 'done'
    assert t.closed is not None


def test_fix_format_enhanced_auto_fixes(tmp_path):
    """Test enhanced auto-fix functionality for dates, IDs, and epic completion."""
    mod = importlib.import_module("scripts.backlog")
    p = tmp_path / "backlog.md"
    
    # Create a backlog with various issues that can be auto-fixed
    lines = [
        "# Backlog",
        "",
        "## 1. Epics - open",
        "",
        "- ☐ Epic 001: Test Epic",
        "  - status: open",
        "  - added: 08/15/2023",  # Non-ISO date
        "  - tasks:",
        "    - ☐ Task 123: Task One",  # Non-4-digit ID
        "      - status: open",
        "      - added: 2023/08/15",  # Non-ISO date
        "    - ☐ Task 0123: Task Two",  # Non-4-digit ID
        "      - status: done",
        "      - added: 15-08-2023",  # Non-ISO date
        "      - closed: 08/20/2023",  # Non-ISO date
        "",
        "## 2. Epics - finished",
        "",
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    
    # Run fix-format with --write
    rc = mod.main(["fix-format", "--file", str(p), "--write"])
    assert rc == 0
    
    # Parse result and verify fixes
    from scripts.backlog_tool import parser as bl
    blines = bl.read_file(str(p))
    backlog = bl.parse(blines)
    
    # Check epic
    epic = backlog.epics_open[0]
    assert epic.id == "0001", f"Expected epic ID to be normalized to 0001, got {epic.id}"
    assert epic.added == "2023-08-15", f"Expected epic added date to be ISO format, got {epic.added}"
    
    # Check tasks - IDs should be unique due to duplicate detection
    task_ids = [t.id for t in epic.tasks]
    assert len(task_ids) == len(set(task_ids)), "Task IDs should be unique"
    assert all(len(tid) == 4 and tid.isdigit() for tid in task_ids), "All task IDs should be 4-digit numeric"
    
    # Check that dates were converted to ISO format
    for task in epic.tasks:
        if task.added:
            assert task.added == "2023-08-15", f"Expected task added date to be ISO format, got {task.added}"
        if task.closed:
            assert task.closed == "2023-08-20", f"Expected task closed date to be ISO format, got {task.closed}"
    
    # Verify backup was created
    backups = (p.parent / ".backups")
    assert backups.exists()
    files = list(backups.glob(p.name + ".*.bak"))
    assert files, "Expected a backup file to be created"
