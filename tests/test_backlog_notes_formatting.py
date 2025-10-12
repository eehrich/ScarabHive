"""Test cases for backlog notes formatting fixes.

Tests that the parser and builder correctly handle notes without adding
excessive blank lines or empty entries.
"""
from src.scripts.backlog_tool.parser import parse, build_markdown


class TestNotesFormattingFixes:
    """Test that notes formatting issues are properly handled."""

    def test_parser_skips_empty_lines_in_notes(self):
        """Test that parser doesn't add empty strings for blank lines in notes."""
        content = [
            "# Backlog",
            "",
            "## 1. Epics - open",
            "",
            "- ☐ Epic 1001: Test Epic",
            "  - status: open",
            "  - notes:",
            "    - First note",
            "    ",  # Empty line
            "    - Second note",
            "    ",  # Another empty line
            "    - Third note",
            "  - tasks:",
            "    - ☐ Task 2001: Test Task",
            "      - status: open",
            "      - notes:",
            "        - Task note one",
            "        ",  # Empty line in task notes
            "        - Task note two",
        ]
        
        backlog = parse(content)
        epic = backlog.epics_open[0]
        task = epic.tasks[0]
        
        # Should have 3 notes, no empty strings
        # Note: '- ' prefix is now preserved to maintain list formatting
        assert len(epic.notes) == 3
        assert epic.notes == ["- First note", "- Second note", "- Third note"]
        assert "" not in epic.notes
        
        # Task should have 2 notes, no empty strings
        assert len(task.notes) == 2
        assert task.notes == ["- Task note one", "- Task note two"]
        assert "" not in task.notes

    def test_builder_skips_empty_notes_entries(self):
        """Test that builder doesn't output blank lines for empty notes."""
        content = [
            "# Backlog",
            "",
            "## 1. Epics - open",
            "",
            "- ☐ Epic 1001: Test Epic",
            "  - status: open",
            "  - notes:",
            "    - Only note",
            "  - tasks:",
            "    - ☐ Task 2001: Test Task",
            "      - status: open",
            "      - notes:",
            "        - Single task note",
        ]
        
        backlog = parse(content)
        rebuilt = build_markdown(backlog)
        lines = rebuilt.splitlines()
        
        # Count consecutive blank lines
        max_consecutive = 0
        current_consecutive = 0
        for line in lines:
            if line.strip() == "":
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 0
        
        # Should not have more than 2 consecutive blank lines (normal formatting)
        assert max_consecutive <= 2, f"Found {max_consecutive} consecutive blank lines"

    def test_round_trip_preserves_notes_without_extra_blanks(self):
        """Test that parsing and rebuilding doesn't add extra blank lines."""
        content = [
            "# Backlog",
            "",
            "## 1. Epics - open",
            "",
            "- ☐ Epic 1001: Test Epic",
            "  - status: open",
            "  - notes:",
            "    - Note with multiple lines",
            "      that continues here",
            "    - Second note",
            "  - tasks:",
            "    - ☐ Task 2001: Test Task",
            "      - status: open",
            "      - notes:",
            "        - Task note",
            "    - ☐ Task 2002: Another Task",
            "      - status: open",
            "      - notes:",
            "        - Another task note",
            "        - Multi-line note",
            "          with continuation",
        ]
        
        backlog = parse(content)
        rebuilt = build_markdown(backlog)
        
        # Count notes in parsed version
        epic = backlog.epics_open[0]
        total_notes = len(epic.notes)
        for task in epic.tasks:
            total_notes += len(task.notes)
        
        # Should have notes but no empty ones
        assert total_notes > 0
        
        # Check for empty notes in parsed data
        for note in epic.notes:
            assert note.strip() != "", "Found empty note in epic"
        for task in epic.tasks:
            for note in task.notes:
                assert note.strip() != "", "Found empty note in task"
        
        # Check rebuilt content doesn't have excessive blank lines
        rebuilt_lines = rebuilt.splitlines()
        max_consecutive = 0
        current_consecutive = 0
        for line in rebuilt_lines:
            if line.strip() == "":
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 0
        
        assert max_consecutive <= 2, "Rebuilt content has excessive blank lines"

    def test_notes_with_list_markers_preserved(self):
        """Test that list markers in notes are properly handled."""
        content = [
            "# Backlog",
            "",
            "## 1. Epics - open",
            "",
            "- ☐ Epic 1001: Test Epic",
            "  - status: open",
            "  - notes:",
            "    - First item with -- dashes inside",
            "    - Second item with -o option style",
            "    - Third normal item",
        ]
        
        backlog = parse(content)
        epic = backlog.epics_open[0]
        
        # Notes should preserve '- ' prefix to maintain list formatting
        expected_notes = [
            "- First item with -- dashes inside",
            "- Second item with -o option style", 
            "- Third normal item"
        ]
        assert epic.notes == expected_notes

    def test_multiline_notes_without_list_markers(self):
        """Test that multiline notes without list markers work correctly."""
        content = [
            "# Backlog",
            "",
            "## 1. Epics - open",
            "",
            "- ☐ Epic 1001: Test Epic",
            "  - status: open", 
            "  - notes:",
            "    This is a multiline note",
            "    that doesn't use list markers",
            "    and should be preserved as-is",
        ]
        
        backlog = parse(content)
        epic = backlog.epics_open[0]
        
        expected_notes = [
            "This is a multiline note",
            "that doesn't use list markers", 
            "and should be preserved as-is"
        ]
        assert epic.notes == expected_notes

    def test_empty_notes_section_handled_gracefully(self):
        """Test that empty notes sections don't cause issues."""
        content = [
            "# Backlog",
            "",
            "## 1. Epics - open",
            "",
            "- ☐ Epic 1001: Test Epic",
            "  - status: open",
            "  - notes:",
            "  - tasks:",
            "    - ☐ Task 2001: Test Task",
            "      - status: open",
        ]
        
        backlog = parse(content)
        epic = backlog.epics_open[0]
        
        # Empty notes section should result in empty list, not list with empty string
        assert epic.notes == []
        assert len(epic.tasks) == 1
        assert epic.tasks[0].notes == []

    def test_notes_with_special_characters_preserved(self):
        """Test that notes with special characters are preserved correctly."""
        content = [
            "# Backlog",
            "",
            "## 1. Epics - open",
            "",
            "- ☐ Epic 1001: Test Epic",
            "  - status: open", 
            "  - notes:",
            "    - Note with `code blocks` and **bold**",
            "    - Note with: colons and semicolons;",
            "    - Note with [links](http://example.com)",
        ]
        
        backlog = parse(content)
        epic = backlog.epics_open[0]
        
        # Notes preserve '- ' prefix for consistent list formatting
        expected_notes = [
            "- Note with `code blocks` and **bold**",
            "- Note with: colons and semicolons;",
            "- Note with [links](http://example.com)"
        ]
        assert epic.notes == expected_notes

    def test_builder_produces_clean_notes_output(self):
        """Test that builder produces clean notes output without artifacts."""
        content = [
            "# Backlog",
            "",
            "## 1. Epics - open",
            "",
            "- ☐ Epic 1001: Test Epic",
            "  - status: open",
            "  - notes:",
            "    - Clean note one",
            "    - Clean note two",
            "  - tasks:",
            "    - ☐ Task 2001: Test Task", 
            "      - status: open",
            "      - notes:",
            "        - Task note",
        ]
        
        backlog = parse(content)
        rebuilt = build_markdown(backlog)
        
        # Check that notes sections are properly formatted
        lines = rebuilt.splitlines()
        notes_section_started = False
        consecutive_blanks = 0
        
        for line in lines:
            if line.strip().startswith("notes:"):
                notes_section_started = True
                consecutive_blanks = 0
                continue
                
            if notes_section_started:
                if line.strip() == "":
                    consecutive_blanks += 1
                    # Should not have more than 1 blank line in notes section
                    assert consecutive_blanks <= 1, "Too many consecutive blank lines in notes"
                else:
                    consecutive_blanks = 0
                    if not line.startswith("  ") and not line.startswith("#"):
                        # End of notes section
                        notes_section_started = False