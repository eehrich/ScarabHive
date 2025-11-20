import importlib
import json
import csv
import tempfile
from pathlib import Path
import subprocess
import sys
import os

# Import the backlog module
mod = importlib.import_module("scripts.backlog")

# Sample backlog content for testing
SAMPLE_BACKLOG = """# Backlog

## 1. Epics - open

- Epic 0001: First Epic
  - status: open
  - tasks:
    - Task 0001: Task One
      - status: open
      - added: 2025-09-08

- Epic 0002: Second Epic
  - status: open
  - tasks:
    - Task 0002: Task Two
      - status: open
      - added: 2025-09-08

## 2. Epics - finished

""".strip()


def run_cli(args, cwd=None):
    """Helper to run CLI commands"""
    cmd = [sys.executable, "-m", "scripts.backlog"] + args
    # Use explicit pipes and handle Windows encoding issues
    result = subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace'  # Replace problematic characters instead of failing
    )
    return result


class TestBulkOperations:
    """Test suite for bulk operations functionality"""

    def test_bulk_add_task_csv_dry_run(self, tmp_path):
        """Test bulk adding tasks from CSV file (dry-run mode)"""
        backlog_file = tmp_path / "backlog.md"
        backlog_file.write_text(SAMPLE_BACKLOG, encoding="utf-8")

        # Create CSV file with tasks to add
        csv_file = tmp_path / "tasks.csv"
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['title', 'epic', 'notes'])
            writer.writerow(['New Task 1', '0001', 'First bulk task'])
            writer.writerow(['New Task 2', '0002', 'Second bulk task'])

        # Run bulk add command
        result = run_cli(['add-task', '--from-file', str(csv_file), '--file', str(backlog_file)], cwd=tmp_path)

        assert result.returncode == 0
        assert "Successfully created 2 tasks:" in result.stdout
        assert "Task" in result.stdout and "under epic" in result.stdout
        assert "Dry-run: would create 2 tasks" in result.stdout

        # Verify file was not modified (dry-run)
        content = backlog_file.read_text(encoding="utf-8")
        assert "New Task 1" not in content
        assert "New Task 2" not in content

    def test_bulk_add_task_csv_with_write(self, tmp_path):
        """Test bulk adding tasks from CSV file (with --write)"""
        backlog_file = tmp_path / "backlog.md"
        backlog_file.write_text(SAMPLE_BACKLOG, encoding="utf-8")

        # Create CSV file with tasks to add
        csv_file = tmp_path / "tasks.csv"
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['title', 'epic', 'notes', 'id'])
            writer.writerow(['New Task 1', '0001', 'First bulk task', '1001'])
            writer.writerow(['New Task 2', '0002', 'Second bulk task', '1002'])

        # Run bulk add command with write
        result = run_cli(['add-task', '--from-file', str(csv_file), '--file', str(backlog_file), '--write'], cwd=tmp_path)

        assert result.returncode == 0
        assert "Successfully created 2 tasks:" in result.stdout
        assert "Task 1001 under epic 0001" in result.stdout
        assert "Task 1002 under epic 0002" in result.stdout
        assert "Wrote changes" in result.stdout

        # Verify file was modified
        content = backlog_file.read_text(encoding="utf-8")
        assert "New Task 1" in content
        assert "New Task 2" in content
        assert "Task 1001:" in content
        assert "Task 1002:" in content

    def test_bulk_add_task_json(self, tmp_path):
        """Test bulk adding tasks from JSON file"""
        backlog_file = tmp_path / "backlog.md"
        backlog_file.write_text(SAMPLE_BACKLOG, encoding="utf-8")

        # Create JSON file with tasks to add
        json_file = tmp_path / "tasks.json"
        tasks_data = [
            {"title": "JSON Task 1", "epic": "0001", "notes": "From JSON", "id": "2001"},
            {"title": "JSON Task 2", "epic": "0002", "notes": "Also from JSON", "id": "2002"}
        ]
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(tasks_data, f)

        # Run bulk add command
        result = run_cli(['add-task', '--from-file', str(json_file), '--file', str(backlog_file), '--write'], cwd=tmp_path)

        assert result.returncode == 0
        assert "Successfully created 2 tasks:" in result.stdout
        assert "Task 2001 under epic 0001" in result.stdout
        assert "Task 2002 under epic 0002" in result.stdout

        # Verify file was modified
        content = backlog_file.read_text(encoding="utf-8")
        assert "JSON Task 1" in content
        assert "JSON Task 2" in content

    def test_bulk_add_epic_csv(self, tmp_path):
        """Test bulk adding epics from CSV file"""
        backlog_file = tmp_path / "backlog.md"
        backlog_file.write_text(SAMPLE_BACKLOG, encoding="utf-8")

        # Create CSV file with epics to add
        csv_file = tmp_path / "epics.csv"
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['title', 'id'])
            writer.writerow(['New Epic 1', '3001'])
            writer.writerow(['New Epic 2', '3002'])

        # Run bulk add command
        result = run_cli(['add-epic', '--from-file', str(csv_file), '--file', str(backlog_file), '--write'], cwd=tmp_path)

        assert result.returncode == 0
        assert "Successfully created 2 epics:" in result.stdout
        assert "Epic 3001" in result.stdout
        assert "Epic 3002" in result.stdout

        # Verify file was modified
        content = backlog_file.read_text(encoding="utf-8")
        assert "New Epic 1" in content
        assert "New Epic 2" in content
        assert "Epic 3001:" in content
        assert "Epic 3002:" in content

    def test_bulk_add_epic_json(self, tmp_path):
        """Test bulk adding epics from JSON file"""
        backlog_file = tmp_path / "backlog.md"
        backlog_file.write_text(SAMPLE_BACKLOG, encoding="utf-8")

        # Create JSON file with epics to add
        json_file = tmp_path / "epics.json"
        epics_data = [
            {"title": "JSON Epic 1", "id": "4001"},
            {"title": "JSON Epic 2", "id": "4002"}
        ]
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(epics_data, f)

        # Run bulk add command
        result = run_cli(['add-epic', '--from-file', str(json_file), '--file', str(backlog_file), '--write'], cwd=tmp_path)

        assert result.returncode == 0
        assert "Successfully created 2 epics:" in result.stdout
        assert "Epic 4001" in result.stdout
        assert "Epic 4002" in result.stdout

        # Verify file was modified
        content = backlog_file.read_text(encoding="utf-8")
        assert "JSON Epic 1" in content
        assert "JSON Epic 2" in content

    def test_bulk_move_task_csv(self, tmp_path):
        """Test bulk moving tasks from CSV file"""
        backlog_file = tmp_path / "backlog.md"
        backlog_file.write_text(SAMPLE_BACKLOG, encoding="utf-8")

        # Create CSV file with moves to perform
        csv_file = tmp_path / "moves.csv"
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['task', 'to_epic'])
            writer.writerow(['0001', '0002'])

        # Run bulk move command
        result = run_cli(['move-task', '--from-file', str(csv_file), '--file', str(backlog_file), '--write'], cwd=tmp_path)

        assert result.returncode == 0
        assert "Successfully moved 1 tasks:" in result.stdout
        assert "Task 0001 -> epic 0002" in result.stdout

    def test_bulk_move_task_json(self, tmp_path):
        """Test bulk moving tasks from JSON file"""
        backlog_file = tmp_path / "backlog.md"
        backlog_file.write_text(SAMPLE_BACKLOG, encoding="utf-8")

        # Create JSON file with moves to perform
        json_file = tmp_path / "moves.json"
        moves_data = [
            {"task": "0002", "to_epic": "0001"}
        ]
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(moves_data, f)

        # Run bulk move command
        result = run_cli(['move-task', '--from-file', str(json_file), '--file', str(backlog_file), '--write'], cwd=tmp_path)

        assert result.returncode == 0
        assert "Successfully moved 1 tasks:" in result.stdout
        assert "Task 0002 -> epic 0001" in result.stdout

    def test_bulk_add_task_invalid_file(self, tmp_path):
        """Test bulk add with invalid file"""
        backlog_file = tmp_path / "backlog.md"
        backlog_file.write_text(SAMPLE_BACKLOG, encoding="utf-8")

        # Try to use non-existent file
        result = run_cli(['add-task', '--from-file', 'nonexistent.csv', '--file', str(backlog_file)], cwd=tmp_path)

        assert result.returncode == 2
        assert "File not found" in result.stderr

    def test_bulk_add_task_invalid_csv_format(self, tmp_path):
        """Test bulk add with invalid CSV format"""
        backlog_file = tmp_path / "backlog.md"
        backlog_file.write_text(SAMPLE_BACKLOG, encoding="utf-8")

        # Create CSV file with missing required columns
        csv_file = tmp_path / "invalid.csv"
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['title'])  # Missing 'epic' column
            writer.writerow(['Task without epic'])

        # Run bulk add command
        result = run_cli(['add-task', '--from-file', str(csv_file), '--file', str(backlog_file)], cwd=tmp_path)

        assert result.returncode == 2
        assert "Missing required fields" in result.stderr

    def test_bulk_add_task_invalid_json_format(self, tmp_path):
        """Test bulk add with invalid JSON format"""
        backlog_file = tmp_path / "backlog.md"
        backlog_file.write_text(SAMPLE_BACKLOG, encoding="utf-8")

        # Create invalid JSON file
        json_file = tmp_path / "invalid.json"
        with open(json_file, 'w', encoding='utf-8') as f:
            f.write('{"invalid": "json"')

        # Run bulk add command
        result = run_cli(['add-task', '--from-file', str(json_file), '--file', str(backlog_file)], cwd=tmp_path)

        assert result.returncode == 2
        assert "Invalid JSON file" in result.stderr

    def test_bulk_add_task_missing_epic(self, tmp_path):
        """Test bulk add with tasks that reference non-existent epics"""
        backlog_file = tmp_path / "backlog.md"
        backlog_file.write_text(SAMPLE_BACKLOG, encoding="utf-8")

        # Create CSV file with invalid epic reference
        csv_file = tmp_path / "invalid_epic.csv"
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['title', 'epic'])
            writer.writerow(['Task with bad epic', '9999'])

        # Run bulk add command
        result = run_cli(['add-task', '--from-file', str(csv_file), '--file', str(backlog_file)], cwd=tmp_path)

        assert result.returncode == 1  # Partial failure
        assert "Errors encountered" in result.stderr
        assert "Epic '9999' not found" in result.stderr

    def test_bulk_operations_with_empty_file(self, tmp_path):
        """Test bulk operations with empty input file"""
        backlog_file = tmp_path / "backlog.md"
        backlog_file.write_text(SAMPLE_BACKLOG, encoding="utf-8")

        # Create empty CSV file
        csv_file = tmp_path / "empty.csv"
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['title', 'epic'])  # Header only, no data

        # Run bulk add command
        result = run_cli(['add-task', '--from-file', str(csv_file), '--file', str(backlog_file)], cwd=tmp_path)

        assert result.returncode == 2
        assert "File contains no data" in result.stderr

    def test_bulk_add_task_unsupported_format(self, tmp_path):
        """Test bulk add with unsupported file format"""
        backlog_file = tmp_path / "backlog.md"
        backlog_file.write_text(SAMPLE_BACKLOG, encoding="utf-8")

        # Create file with unsupported extension
        txt_file = tmp_path / "data.txt"
        txt_file.write_text("title,epic\nTask,0001", encoding="utf-8")

        # Run bulk add command
        result = run_cli(['add-task', '--from-file', str(txt_file), '--file', str(backlog_file)], cwd=tmp_path)

        assert result.returncode == 2
        assert "File must be .csv or .json" in result.stderr

    def test_bulk_operations_create_backups(self, tmp_path):
        """Test that bulk operations create backups when writing"""
        backlog_file = tmp_path / "backlog.md"
        backlog_file.write_text(SAMPLE_BACKLOG, encoding="utf-8")

        # Create CSV file with tasks to add
        csv_file = tmp_path / "tasks.csv"
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['title', 'epic'])
            writer.writerow(['Backup Test Task', '0001'])

        # Run bulk add command with write
        result = run_cli(['add-task', '--from-file', str(csv_file), '--file', str(backlog_file), '--write'], cwd=tmp_path)

        assert result.returncode == 0
        assert "backup:" in result.stdout

        # Check that backup directory and files exist
        backup_dir = tmp_path / ".backups"
        assert backup_dir.exists()
        backup_files = list(backup_dir.glob("backlog.md.*.bak"))
        assert len(backup_files) > 0

    def test_single_operations_still_work(self, tmp_path):
        """Test that single operations still work after bulk implementation"""
        backlog_file = tmp_path / "backlog.md"
        backlog_file.write_text(SAMPLE_BACKLOG, encoding="utf-8")

        # Test single add-task
        result = run_cli(['add-task', '--title', 'Single Task', '--epic', '0001', '--file', str(backlog_file), '--write'], cwd=tmp_path)
        assert result.returncode == 0
        assert "Created task" in result.stdout

        # Test single add-epic
        result = run_cli(['add-epic', '--title', 'Single Epic', '--file', str(backlog_file), '--write'], cwd=tmp_path)
        assert result.returncode == 0
        assert "Created epic" in result.stdout

        # Test single move-task
        result = run_cli(['move-task', '--task', '0001', '--to-epic', '0002', '--file', str(backlog_file), '--write'], cwd=tmp_path)
        assert result.returncode == 0
        assert "Moved task" in result.stdout

    def test_bulk_operations_with_notes_and_ids(self, tmp_path):
        """Test bulk operations with optional fields like notes and custom IDs"""
        backlog_file = tmp_path / "backlog.md"
        backlog_file.write_text(SAMPLE_BACKLOG, encoding="utf-8")

        # Create CSV file with all optional fields
        csv_file = tmp_path / "full_tasks.csv"
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['title', 'epic', 'notes', 'id'])
            writer.writerow(['Task with notes', '0001', 'These are detailed notes', '5001'])
            writer.writerow(['Task with ID only', '0002', '', '5002'])

        # Run bulk add command
        result = run_cli(['add-task', '--from-file', str(csv_file), '--file', str(backlog_file), '--write'], cwd=tmp_path)

        assert result.returncode == 0
        assert "Successfully created 2 tasks:" in result.stdout
        assert "Task 5001 under epic 0001" in result.stdout
        assert "Task 5002 under epic 0002" in result.stdout

        # Verify content includes notes
        content = backlog_file.read_text(encoding="utf-8")
        assert "Task with notes" in content
        assert "These are detailed notes" in content
        assert "Task 5001:" in content
        assert "Task 5002:" in content
