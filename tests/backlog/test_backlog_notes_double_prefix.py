"""Test that editing task notes doesn't add extra '- ' prefixes.

This regression test ensures that when a task with multi-line notes is edited,
the list markers '- ' are not duplicated on round-trip (parse -> edit -> serialize).
"""
import tempfile
import os

from scripts.backlog_tool import parser as bl


def test_edit_notes_no_double_prefix():
    """Verify editing a task with multi-line notes doesn't add double '- ' prefixes."""
    
    # Create a minimal backlog with a task containing multi-line notes
    content = """# Test Backlog

## 1. Epics - open

- ☐ Epic 0001: Test Epic
  - status: open
  - tasks:
    - ☐ Task 0213: Test Task
      - status: reverted
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Added `.githooks/pre-commit` that runs the validator and fails the commit if it modifies the backlog; see `developer_rules.md` for install steps.

## 2. Epics - finished

EOF
"""
    
    # Write to temporary file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
        f.write(content)
        temp_path = f.name
    
    try:
        # Parse the backlog
        lines = bl.read_file(temp_path)
        backlog = bl.parse(lines)
        
        # Verify the task exists and has the note without double prefix
        epic = backlog.epics_open[0]
        assert epic.id == '0001'
        task = epic.tasks[0]
        assert task.id == '0213'
        assert task.notes is not None
        assert len(task.notes) == 1
        # The note should NOT have '- ' prefix after parsing (stripped by parser)
        expected_note = "Added `.githooks/pre-commit` that runs the validator and fails the commit if it modifies the backlog; see `developer_rules.md` for install steps."
        assert task.notes[0] == expected_note, f"Expected: {expected_note!r}, got: {task.notes[0]!r}"
        
        # Simulate an edit (just touch the task to mark it as changed)
        # In real scenario, this would be done by cmd_edit, but we test the round-trip
        task.status = "reverted"  # No actual change, just to trigger serialization
        
        # Serialize back to markdown
        output = bl.build_markdown(backlog)
        
        # Parse the output again to verify no double prefix was added
        output_lines = output.splitlines()
        backlog2 = bl.parse(output_lines)
        
        # Check the task in the re-parsed backlog
        epic2 = backlog2.epics_open[0]
        task2 = epic2.tasks[0]
        assert task2.notes is not None
        assert len(task2.notes) == 1
        # Should still be the same note without double '- '
        assert task2.notes[0] == expected_note, f"After round-trip: expected {expected_note!r}, got {task2.notes[0]!r}"
        
        # Also verify in the raw markdown that there's no '- - ' pattern
        assert '- - ' not in output, "Found double '- - ' prefix in serialized output!"
        
        # Verify the note line appears correctly in the output
        assert '        - Added `.githooks/pre-commit`' in output, "Note not formatted correctly in output"
        
    finally:
        # Clean up
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def test_edit_notes_preserves_existing_double_dash():
    """Verify that '--' (double dash) is not treated as a list marker."""
    
    content = """# Test Backlog

## 1. Epics - open

- ☐ Epic 0001: Test Epic
  - status: open
  - tasks:
    - ☐ Task 0100: Test Task with double dash
      - status: open
      - notes:
        - Use --verbose flag for detailed output

## 2. Epics - finished

EOF
"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
        f.write(content)
        temp_path = f.name
    
    try:
        lines = bl.read_file(temp_path)
        backlog = bl.parse(lines)
        
        task = backlog.epics_open[0].tasks[0]
        # Should preserve the '--verbose' text
        assert task.notes[0] == "Use --verbose flag for detailed output"
        
        # Round-trip
        output = bl.build_markdown(backlog)
        backlog2 = bl.parse(output.splitlines())
        task2 = backlog2.epics_open[0].tasks[0]
        
        # Should still have the '--' intact
        assert task2.notes[0] == "Use --verbose flag for detailed output"
        assert '        - Use --verbose flag' in output
        
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def test_multiple_notes_round_trip():
    """Verify multiple note lines survive round-trip editing."""
    
    content = """# Test Backlog

## 1. Epics - open

- ☐ Epic 0001: Test Epic
  - status: open
  - tasks:
    - ☐ Task 0001: Test Task
      - status: open
      - notes:
        - First note item
        - Second note item with details
        - Third note with special chars: @#$%

## 2. Epics - finished

EOF
"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
        f.write(content)
        temp_path = f.name
    
    try:
        lines = bl.read_file(temp_path)
        backlog = bl.parse(lines)
        
        task = backlog.epics_open[0].tasks[0]
        assert len(task.notes) == 3
        assert task.notes[0] == "First note item"
        assert task.notes[1] == "Second note item with details"
        assert task.notes[2] == "Third note with special chars: @#$%"
        
        # Round-trip
        output = bl.build_markdown(backlog)
        backlog2 = bl.parse(output.splitlines())
        task2 = backlog2.epics_open[0].tasks[0]
        
        assert len(task2.notes) == 3
        assert task2.notes[0] == "First note item"
        assert task2.notes[1] == "Second note item with details"
        assert task2.notes[2] == "Third note with special chars: @#$%"
        
        # No double prefixes
        assert '- - ' not in output
        
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def test_description_bullets_preserved():
    """Verify that bullet points in description fields are preserved."""
    
    content = """# Test Backlog

## 1. Epics - open

- ☐ Epic 0017: MCP server status & progress interface
  - status: open
  - description:
    - Add a lightweight, standardized status/progress reporting interface
    - In the CLI this shall be shown in sequence
    - Different colors for output shall be supported
    - Messages will be streamed to the WebUI and CLI
  - tasks:
    - ☐ Task 0001: Implement status reporting
      - status: open

## 2. Epics - finished

EOF
"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
        f.write(content)
        temp_path = f.name
    
    try:
        lines = bl.read_file(temp_path)
        backlog = bl.parse(lines)
        
        epic = backlog.epics_open[0]
        # Description bullets should be preserved with '- '
        assert len(epic.description) == 4
        assert epic.description[0] == "- Add a lightweight, standardized status/progress reporting interface"
        assert epic.description[1] == "- In the CLI this shall be shown in sequence"
        assert epic.description[2] == "- Different colors for output shall be supported"
        assert epic.description[3] == "- Messages will be streamed to the WebUI and CLI"
        
        # Round-trip
        output = bl.build_markdown(backlog)
        backlog2 = bl.parse(output.splitlines())
        epic2 = backlog2.epics_open[0]
        
        # Should still have bullets in description
        assert len(epic2.description) == 4
        assert epic2.description[0] == "- Add a lightweight, standardized status/progress reporting interface"
        assert epic2.description[1] == "- In the CLI this shall be shown in sequence"
        assert epic2.description[2] == "- Different colors for output shall be supported"
        assert epic2.description[3] == "- Messages will be streamed to the WebUI and CLI"
        
        # Verify in raw output
        assert '    - Add a lightweight' in output  # Description bullets preserved
        
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
