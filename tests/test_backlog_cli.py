import importlib
import sys


def test_backlog_version_and_help(capfd):
    mod = importlib.import_module("scripts.backlog")
    # test version
    rc = mod.main(["--version"])
    assert rc == 0
    out, err = capfd.readouterr()
    assert out.strip() != ""


def test_backlog_help_text_patterns_and_grouping(capfd):
    """Test Task 9048: Enhanced Help Text Patterns and Task 9049: Command Grouping in Help"""
    import subprocess
    # Use subprocess to capture help output without SystemExit
    result = subprocess.run([sys.executable, "-m", "scripts.backlog", "--help"], 
                          capture_output=True, text=True)
    
    assert result.returncode == 0
    out = result.stdout
    
    # Test command grouping in help text
    assert "management (add-task, add-epic, edit, move-task)" in out
    assert "viewing (list, show, validate)" in out
    assert "maintenance (backup, undo, check-ids, fix-format)" in out
    assert "legacy (update, init)" in out
    
    # Test enhanced help text patterns with emojis
    assert "🔍 Validate backlog file" in out
    assert "➕ Add a new task" in out
    assert "📋 Create a new epic" in out
    assert "↔️ Move a task between epics" in out
    assert "✏️ Edit epic or task fields" in out
    assert "💾 Create or manage backups" in out
    assert "↶ Restore from backup" in out
    assert "🔍 Check for duplicate IDs" in out
    assert "🔧 Auto-fix formatting issues" in out
    assert "📦 Move finished epics" in out
    assert "📄 Create new backlog file" in out
    assert "📋 List epics and tasks" in out
    assert "👀 Show detailed information" in out
    assert "🔧 Generate shell completion scripts" in out
    
    # Test examples section
    assert "EXAMPLES:" in out
    assert "Basic Operations:" in out
    assert "Task Management:" in out
    assert "Safety & Recovery:" in out


def test_backlog_validate_missing_temp(tmp_path, capfd):
    mod = importlib.import_module("scripts.backlog")
    # point to a non-existent file
    rc = mod.main(["validate", "--file", str(tmp_path / "nope.md")])
    assert rc != 0
    out, err = capfd.readouterr()
    assert "not found" in err


def test_backlog_add_task_dryrun(capfd):
    mod = importlib.import_module("scripts.backlog")
    import importlib
import sys
import subprocess


def test_backlog_version_and_help(capfd):
    mod = importlib.import_module("scripts.backlog")
    # test version
    rc = mod.main(["--version"])
    assert rc == 0
    out, err = capfd.readouterr()
    assert out.strip() != ""


def test_backlog_help_text_patterns_and_grouping():
    """Test Task 9048: Enhanced Help Text Patterns and Task 9049: Command Grouping in Help"""
    # Use subprocess to capture help output without SystemExit
    result = subprocess.run([sys.executable, "-m", "scripts.backlog", "--help"], 
                          capture_output=True, text=True, encoding='utf-8')
    
    assert result.returncode == 0
    out = result.stdout
    
    # Test command grouping in help text
    assert "management (add-task, add-epic, edit, move-task)" in out
    assert "viewing (list, show, validate)" in out
    assert "maintenance (backup, undo, check-ids, fix-format)" in out
    assert "legacy (update, init)" in out
    
    # Test enhanced help text patterns with emojis
    assert "🔍 Validate backlog file" in out
    assert "➕ Add a new task" in out
    assert "📋 Create a new epic" in out
    assert "↔️ Move a task between epics" in out
    assert "✏️ Edit epic or task fields" in out
    assert "💾 Create or manage backups" in out
    assert "↶ Restore from backup" in out
    assert "🔍 Check for duplicate IDs" in out
    assert "🔧 Auto-fix formatting issues" in out
    assert "📦 Move finished epics" in out
    assert "📄 Create new backlog file" in out
    assert "📋 List epics and tasks" in out
    assert "👀 Show detailed information" in out
    assert "🔧 Generate shell completion scripts" in out
    
    # Test examples section
    assert "EXAMPLES:" in out
    assert "Basic Operations:" in out
    assert "Task Management:" in out
    assert "Safety & Recovery:" in out


def test_backlog_validate_missing_temp(tmp_path, capfd):
    mod = importlib.import_module("scripts.backlog")
    # point to a non-existent file
    rc = mod.main(["validate", "--file", str(tmp_path / "nope.md")])
    assert rc != 0
    out, err = capfd.readouterr()
    assert "not found" in err


def test_backlog_add_task_dryrun(capfd):
    mod = importlib.import_module("scripts.backlog")
    rc = mod.main(["add-task", "--title", "Example task", "--notes", "line1\nline2"])
    assert rc == 0
    out, err = capfd.readouterr()
    assert "Dry-run: task entry to insert:" in out


def test_backlog_option_ordering_standardization():
    """Test Task 9047: Standardize Option Ordering"""
    # Use subprocess to capture help output without SystemExit
    result = subprocess.run([sys.executable, "-m", "scripts.backlog", "add-task", "--help"], 
                          capture_output=True, text=True)
    
    assert result.returncode == 0
    out = result.stdout
    
    # The help should show options in standardized order
    # This is hard to test directly, but we can check that the command works
    # and that the help is generated properly
    assert "usage: backlog add-task" in out
    assert "--title" in out
    assert "--epic" in out
    assert "--write" in out


def test_backlog_shell_completion_generation(capfd):
    """Test Task 9055: Shell Completion Support"""
    mod = importlib.import_module("scripts.backlog")
    
    # Test bash completion generation
    rc = mod.main(["completion", "bash"])
    assert rc == 0
    out, err = capfd.readouterr()
    assert "# backlog bash completion" in out
    assert "eval \"$(backlog completion bash)\"" in out
    assert "local commands=" in out
    
    # Test zsh completion generation
    rc = mod.main(["completion", "zsh"])
    assert rc == 0
    out, err = capfd.readouterr()
    assert "#compdef backlog" in out
    assert "_backlog" in out
    
    # Test fish completion generation
    rc = mod.main(["completion", "fish"])
    assert rc == 0
    out, err = capfd.readouterr()
    assert "function __backlog_complete" in out  # Fish uses 'complete' commands


def test_backlog_improved_error_messages(capfd):
    """Test Task 9050: Improved Error Messages"""
    mod = importlib.import_module("scripts.backlog")
    
    # Test error when no id provided to show command
    rc = mod.main(["show"])
    assert rc == 2
    out, err = capfd.readouterr()
    assert "ERROR: no id provided" in err
    
    # Test error for invalid --set format
    rc = mod.main(["edit", "0001", "--set", "invalid-format"])
    assert rc == 2
    out, err = capfd.readouterr()
    assert "ERROR: invalid --set value" in err


def test_backlog_interactive_mode_show(capfd, monkeypatch, tmp_path):
    """Test Task 9051: Interactive Mode Enhancements - show command"""
    # Create a temporary backlog file
    sample_backlog = """# Backlog

## 1. Epics - open

- [ ] Epic 0001: Test Epic
  - status: open
  - tasks:
    - [ ] Task 0001: Test Task
      - status: open
      - added: 2025-09-08

## 2. Epics - finished
"""
    
    backlog_file = tmp_path / "test_backlog.md"
    backlog_file.write_text(sample_backlog, encoding='utf-8')
    
    mod = importlib.import_module("scripts.backlog")
    
    # Mock input to simulate user selecting item 1
    monkeypatch.setattr('builtins.input', lambda: "1")
    
    rc = mod.main(["show", "--interactive", "--file", str(backlog_file)])
    assert rc == 0
    out, err = capfd.readouterr()
    
    # Should show the selected epic
    assert "Epic 0001: Test Epic" in out


def test_backlog_interactive_mode_edit(capfd, monkeypatch, tmp_path):
    """Test Task 9051: Interactive Mode Enhancements - edit command"""
    # Create a temporary backlog file
    sample_backlog = """# Backlog

## 1. Epics - open

- [ ] Epic 0001: Test Epic
  - status: open
  - tasks:
    - [ ] Task 0001: Test Task
      - status: open
      - added: 2025-09-08

## 2. Epics - finished
"""
    
    backlog_file = tmp_path / "test_backlog.md"
    backlog_file.write_text(sample_backlog, encoding='utf-8')
    
    mod = importlib.import_module("scripts.backlog")
    
    # Mock input to simulate user entering field and value
    inputs = ["title", "Updated Title", "done"]
    input_iter = iter(inputs)
    monkeypatch.setattr('builtins.input', lambda: next(input_iter))
    
    rc = mod.main(["edit", "0001", "--interactive", "--file", str(backlog_file)])
    assert rc == 0
    out, err = capfd.readouterr()
    
    # Should show dry-run of the update
    assert "Dry-run: updated epic 0001" in out 
    assert rc == 0
    out, err = capfd.readouterr()
    assert "Dry-run: task entry to insert:" in out


def test_backlog_option_ordering_standardization(capfd):
    """Test Task 9047: Standardize Option Ordering"""
    mod = importlib.import_module("scripts.backlog")
    
    # Test add-task command option ordering
    try:
        rc = mod.main(["add-task", "--help"])
    except SystemExit as e:
        rc = e.code
    assert rc == 0
    out, err = capfd.readouterr()
    
    # The help should show options in standardized order
    # This is hard to test directly, but we can check that the command works
    # and that the help is generated properly
    assert "usage: backlog add-task" in out
    assert "--title" in out
    assert "--epic" in out
    assert "--write" in out


def test_backlog_shell_completion_generation(capfd):
    """Test Task 9055: Shell Completion Support"""
    mod = importlib.import_module("scripts.backlog")
    
    # Test bash completion generation
    rc = mod.main(["completion", "bash"])
    assert rc == 0
    out, err = capfd.readouterr()
    assert "# backlog bash completion" in out
    assert "eval \"$(backlog completion bash)\"" in out
    assert "local commands=" in out
    
    # Test zsh completion generation
    rc = mod.main(["completion", "zsh"])
    assert rc == 0
    out, err = capfd.readouterr()
    assert "#compdef backlog" in out
    assert "_backlog" in out
    
    # Test fish completion generation
    rc = mod.main(["completion", "fish"])
    assert rc == 0
    out, err = capfd.readouterr()
    assert "# backlog fish completion" in out
    assert "complete -c backlog" in out


def test_backlog_improved_error_messages(capfd):
    """Test Task 9050: Improved Error Messages"""
    mod = importlib.import_module("scripts.backlog")
    
    # Test error when no id provided to show command
    try:
        rc = mod.main(["show"])
    except SystemExit as e:
        rc = e.code
    assert rc == 2
    out, err = capfd.readouterr()
    assert "ERROR: no id provided" in err
    
    # Test error when no id provided to edit command
    try:
        rc = mod.main(["edit"])
    except SystemExit as e:
        rc = e.code
    assert rc == 2
    out, err = capfd.readouterr()
    assert "the following arguments are required: id" in err
    
    # Test error for invalid --set format
    try:
        rc = mod.main(["edit", "0001", "--set", "invalid-format"])
    except SystemExit as e:
        rc = e.code
    assert rc == 2
    out, err = capfd.readouterr()
    assert "ERROR: invalid --set value" in err


def test_backlog_interactive_mode_show(capfd, monkeypatch):
    """Test Task 9051: Interactive Mode Enhancements - show command"""
    import tempfile
    from pathlib import Path
    
    # Create a temporary backlog file
    sample_backlog = """# Backlog

## 1. Epics - open

- ☐ Epic 0001: Test Epic
  - status: open
  - tasks:
    - ☐ Task 0001: Test Task
      - status: open
      - added: 2025-09-08

## 2. Epics - finished
"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
        f.write(sample_backlog)
        temp_file = f.name
    
    try:
        mod = importlib.import_module("scripts.backlog")
        
        # Mock input to simulate user selecting item 1
        monkeypatch.setattr('builtins.input', lambda prompt="": "1")
        
        rc = mod.main(["show", "--interactive", "--file", temp_file])
        assert rc == 0
        out, err = capfd.readouterr()
        
        # Should show the selected epic
        assert "Epic 0001: Test Epic" in out
        
    finally:
        Path(temp_file).unlink()


def test_backlog_interactive_mode_edit(capfd, monkeypatch):
    """Test Task 9051: Interactive Mode Enhancements - edit command"""
    import tempfile
    from pathlib import Path
    
    # Create a temporary backlog file
    sample_backlog = """# Backlog

## 1. Epics - open

- ☐ Epic 0001: Test Epic
  - status: open
  - tasks:
    - ☐ Task 0001: Test Task
      - status: open
      - added: 2025-09-08

## 2. Epics - finished
"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
        f.write(sample_backlog)
        temp_file = f.name
    
    try:
        mod = importlib.import_module("scripts.backlog")
        
        # Mock input to simulate user entering field and value
        inputs = ["title", "Updated Title", "done"]
        input_iter = iter(inputs)
        monkeypatch.setattr('builtins.input', lambda prompt="": next(input_iter))
        
        rc = mod.main(["edit", "0001", "--interactive", "--file", temp_file])
        assert rc == 0
        out, err = capfd.readouterr()
        
        # Should show dry-run of the update
        assert "Dry-run: updated task 0001 (Epic 0001)" in out
        
    finally:
        Path(temp_file).unlink()
