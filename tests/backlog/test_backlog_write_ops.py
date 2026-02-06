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


def test_auto_fix_date_formats_unit(tmp_path):
    """Unit test for auto_fix_date_formats function."""
    from scripts.backlog_tool.parser import auto_fix_date_formats, Backlog, Epic, Task
    
    # Create a backlog with various date formats
    backlog = Backlog(header=[], footer=[], epics_open=[], epics_finished=[])
    
    epic = Epic(id="0001", title="Test Epic", status="open")
    epic.added = "08/15/2023"  # MM/DD/YYYY
    epic.closed = "2023/08/20"  # YYYY/MM/DD
    
    task1 = Task(id="0001", title="Task 1", status="open")
    task1.added = "15-08-2023"  # DD-MM-YYYY
    
    task2 = Task(id="0002", title="Task 2", status="done")
    task2.added = "2023-08-10"  # Already ISO
    task2.closed = "invalid date"  # Invalid date
    
    epic.tasks = [task1, task2]
    backlog.epics_open = [epic]
    
    # Apply date fixes
    changes = auto_fix_date_formats(backlog)
    
    # Check results
    assert len(changes) == 3, f"Expected 3 date changes, got {len(changes)}"
    assert epic.added == "2023-08-15"
    assert epic.closed == "2023-08-20"
    assert task1.added == "2023-08-15"
    assert task2.added == "2023-08-10"  # Should remain unchanged
    assert task2.closed == "invalid date"  # Should remain unchanged (can't convert)


def test_auto_fix_id_formats_unit(tmp_path):
    """Unit test for auto_fix_id_formats function."""
    from scripts.backlog_tool.parser import auto_fix_id_formats, Backlog, Epic, Task
    
    # Create a backlog with various ID formats
    backlog = Backlog(header=[], footer=[], epics_open=[], epics_finished=[])
    
    epic = Epic(id="1", title="Test Epic", status="open")  # 1-digit
    
    task1 = Task(id="12", title="Task 1", status="open")  # 2-digit
    task2 = Task(id="123", title="Task 2", status="open")  # 3-digit
    task3 = Task(id="1234", title="Task 3", status="open")  # Already 4-digit
    task4 = Task(id="12345", title="Task 4", status="open")  # 5-digit (should not change)
    
    epic.tasks = [task1, task2, task3, task4]
    backlog.epics_open = [epic]
    
    # Apply ID fixes
    changes = auto_fix_id_formats(backlog)
    
    # Check results
    assert len(changes) == 3, f"Expected 3 ID changes, got {len(changes)}"
    assert epic.id == "0001"
    assert task1.id == "0012"
    assert task2.id == "0123"
    assert task3.id == "1234"  # Should remain unchanged
    assert task4.id == "12345"  # Should remain unchanged (too long)


def test_auto_complete_epics_unit(tmp_path):
    """Unit test for auto_complete_epics function."""
    from scripts.backlog_tool.parser import auto_complete_epics, Backlog, Epic, Task
    
    # Create a backlog with epics that should be auto-completed
    backlog = Backlog(header=[], footer=[], epics_open=[], epics_finished=[])
    
    # Epic with all tasks done but epic not marked as done
    epic1 = Epic(id="0001", title="Epic 1", status="open")
    task1 = Task(id="0001", title="Task 1", status="done")
    task2 = Task(id="0002", title="Task 2", status="finished")
    epic1.tasks = [task1, task2]
    
    # Epic with mixed status tasks (should not be completed)
    epic2 = Epic(id="0002", title="Epic 2", status="open")
    task3 = Task(id="0003", title="Task 3", status="done")
    task4 = Task(id="0004", title="Task 4", status="in progress")
    epic2.tasks = [task3, task4]
    
    # Epic with no tasks (should not be completed)
    epic3 = Epic(id="0003", title="Epic 3", status="open")
    epic3.tasks = []
    
    backlog.epics_open = [epic1, epic2, epic3]
    
    # Apply epic completion
    changes = auto_complete_epics(backlog)
    
    # Check results
    assert len(changes) == 2, f"Expected 2 changes, got {len(changes)}"
    assert epic1.status == "done"
    assert epic1 in backlog.epics_finished  # Should be moved to finished
    assert epic2.status == "open"  # Should remain open
    assert epic2 in backlog.epics_open
    assert epic3.status == "open"  # Should remain open


def test_fix_format_integration_with_validation(tmp_path):
    """Integration test: fix-format followed by validation should pass."""
    mod = importlib.import_module("scripts.backlog")
    p = tmp_path / "backlog.md"
    
    # Create a backlog with multiple issues
    lines = [
        "# Backlog",
        "",
        "## 1. Epics - open",
        "",
        "- ☐ Epic 001: Problem Epic",
        "  - status: open",
        "  - added: 08/15/2023",  # Non-ISO date
        "  - tasks:",
        "    - ☐ Task 001: Task One",
        "      - status: open",
        "      - added: 2023/08/15",  # Non-ISO date
        "    - ☐ Task 001: Task Two",  # Duplicate ID
        "      - status: done",
        "      - added: 15-08-2023",  # Non-ISO date
        "      - closed: 08/20/2023",  # Non-ISO date
        "",
        "## 2. Epics - finished",
        "",
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    
    # First validate - should have errors
    rc = mod.main(["validate", "--file", str(p)])
    assert rc == 1  # Should fail validation
    
    # Apply fixes
    rc = mod.main(["fix-format", "--file", str(p), "--write"])
    assert rc == 0
    
    # Validate again - should pass
    rc = mod.main(["validate", "--file", str(p)])
    assert rc == 0  # Should pass validation now


def test_fix_format_ids_only_mode(tmp_path):
    """Test fix-format with --ids-only flag."""
    mod = importlib.import_module("scripts.backlog")
    p = tmp_path / "backlog.md"
    
    # Create a backlog with ID issues and formatting issues
    lines = [
        "# Backlog",
        "",
        "## 1. Epics - open",
        "",
        "- ☐ Epic 001: Test Epic",
        "  - status: open",
        "  - added: 08/15/2023",  # Non-ISO date (should not be fixed in ids-only mode)
        "  - tasks:",
        "    - ☐ Task 001: Task One",
        "      - status: open",
        "      - added: 2023/08/15",  # Non-ISO date (should not be fixed)
        "    - ☐ Task 001: Task Two",  # Duplicate ID (should be fixed)
        "      - status: done",
        "      - closed: —",  # Placeholder (should not be fixed)
        "",
        "## 2. Epics - finished",
        "",
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    
    # Apply fixes with --ids-only
    rc = mod.main(["fix-format", "--file", str(p), "--write", "--ids-only"])
    assert rc == 0
    
    # Parse result
    from scripts.backlog_tool import parser as bl
    blines = bl.read_file(str(p))
    backlog = bl.parse(blines)
    
    # Check that duplicate IDs were fixed (this is what ids-only does)
    epic = backlog.epics_open[0]
    task_ids = [t.id for t in epic.tasks]
    assert len(task_ids) == len(set(task_ids)), "Duplicate IDs should be resolved in ids-only mode"
    
    # Check that dates were NOT fixed (ids-only mode preserves formatting)
    assert epic.added == "08/15/2023", "Date should not be fixed in ids-only mode"
    for task in epic.tasks:
        if task.added:
            assert task.added == "2023/08/15" or task.added == "08/15/2023", "Dates should not be fixed in ids-only mode"
        if task.closed and task.closed != "—":
            # Only check if there's an actual date to validate
            pass
