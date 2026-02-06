from pathlib import Path
from scripts.backlog_tool import parser as bl


GOOD = '''# Backlog

## 1. Epics - open

☐ Epic 1000: OK epic
 - status: open
  - tasks:
    - ☐ Task 1001: task
      - status: open

## 2. Epics - finished

'''

DUP_TASK = '''# Backlog

## 1. Epics - open

☐ Epic 1001: Epic A
 - status: open
 - tasks:
    - ☐ Task 2001: task A
      - status: open

☐ Epic 1002: Epic B
 - status: open
 - tasks:
    - ☐ Task 2001: task B
      - status: open

## 2. Epics - finished

'''

BAD_DATES = '''# Backlog

## 1. Epics - open

☐ Epic 1003: Epic
 - status: open
 - tasks:
    - ☐ Task 3001: task
      - status: open
      - added: 2025-13-01

## 2. Epics - finished

'''

BAD_STATUS = '''# Backlog

## 1. Epics - open

☐ Epic 1004: Epic
 - status: foobar
 - tasks:
    - ☐ Task 4001: task
      - status: open

## 2. Epics - finished

'''


def run_validator_text(backlog_text: str) -> list[str]:
    lines = backlog_text.splitlines()
    b = bl.parse(lines)
    return bl.validate_backlog(b)


def test_good():
    errors = run_validator_text(GOOD)
    assert not errors


def test_dup_task():
    errors = run_validator_text(DUP_TASK)
    assert any('duplicate task id' in e.lower() for e in errors)


def test_bad_dates():
    errors = run_validator_text(BAD_DATES)
    assert any('bad date' in e.lower() for e in errors)


def test_bad_status():
    errors = run_validator_text(BAD_STATUS)
    assert any('unknown epic status' in e.lower() or 'unknown task status' in e.lower() for e in errors)


def test_cli_validate_success(tmp_path):
    """Test CLI validate command with valid backlog."""
    import subprocess
    import sys

    # Create a simple valid backlog
    backlog_content = """# Backlog

## 1. Epics - open

- ☐ Epic 0001: Test Epic
  - status: open
  - tasks:
    - ☐ Task 0001: Test Task
      - status: open

## 2. Epics - finished
"""

    backlog_file = tmp_path / "test_backlog.md"
    backlog_file.write_text(backlog_content, encoding='utf-8')

    # Run the validate command
    result = subprocess.run([
        sys.executable, "-m", "scripts.backlog", "validate",
        "--file", str(backlog_file)
  ], capture_output=True, text=True, encoding='utf-8', errors='replace', cwd=Path(__file__).parent.parent)

    assert result.returncode == 0
    assert "[SUCCESS] Backlog validation successful!" in result.stdout
    assert "Total epics: 1" in result.stdout
    assert "Total tasks: 1" in result.stdout


def test_cli_validate_errors(tmp_path):
    """Test CLI validate command with invalid backlog."""
    import subprocess
    import sys

    # Create a backlog with duplicate task IDs
    backlog_content = """# Backlog

## 1. Epics - open

- ☐ Epic 0001: Test Epic 1
  - status: open
  - tasks:
    - ☐ Task 0001: Test Task
      - status: open

- ☐ Epic 0002: Test Epic 2
  - status: open
  - tasks:
    - ☐ Task 0001: Duplicate Task
      - status: open

## 2. Epics - finished
"""

    backlog_file = tmp_path / "test_backlog.md"
    backlog_file.write_text(backlog_content, encoding='utf-8')

    # Run the validate command
    result = subprocess.run([
        sys.executable, "-m", "scripts.backlog", "validate",
        "--file", str(backlog_file)
  ], capture_output=True, text=True, encoding='utf-8', errors='replace', cwd=Path(__file__).parent.parent)

    assert result.returncode == 1
    assert "[ERROR] Validation failed" in result.stderr
    assert "duplicate task id" in result.stderr
