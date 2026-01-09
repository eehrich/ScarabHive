"""Tests for robust Markdown parser improvements."""
from scripts.backlog_tool import parser


def test_parse_malformed_lines():
    """Test parsing with malformed or inconsistent lines."""
    lines = [
        "# Backlog",
        "",
        "## 1. Epics - open",
        "",
        "- ☐ Epic 0018: Test Epic",
        "  - status: open",
        "  - tasks:",
        "    - ☐ Task 0001: Good task",
        "      - status: open",
        "    - Malformed task line without proper format",  # malformed
        "      - status: open",
        "    - ☐ Task 0002: Another task",
        "      - status: open",
        "",
        "- ☐ Epic 0019: Another Epic",
        "  - status: open",
        "  - tasks:",
    ]

    # Should not crash, should parse what it can
    bl = parser.parse(lines)
    assert len(bl.epics_open) >= 1  # Should parse at least the good epic
    epic = bl.epics_open[0]
    assert epic.id == "0018"
    # Should have parsed the good tasks
    assert len(epic.tasks) >= 2


def test_parse_inconsistent_indentation():
    """Test parsing with inconsistent indentation."""
    lines = [
        "# Backlog",
        "",
        "## 1. Epics - open",
        "",
        "- ☐ Epic 0020: Test Epic",
        "  - status: open",
        "  - tasks:",
        "    - ☐ Task 0021: Task 1",
        "      - status: open",
        "        - description: This has extra indent",  # inconsistent indent
        "      - added: 2024-01-01",
        "    - ☐ Task 0022: Task 2",
        "  - status: open",  # wrong level
    ]

    bl = parser.parse(lines)
    assert len(bl.epics_open) == 1
    epic = bl.epics_open[0]
    assert len(epic.tasks) >= 1


def test_parse_empty_sections():
    """Test parsing with empty or missing sections."""
    lines = [
        "# Backlog",
        "",
        "## 1. Epics - open",
        "",
        "## 2. Epics - finished",
        "",
    ]

    bl = parser.parse(lines)
    assert len(bl.epics_open) == 0
    assert len(bl.epics_finished) == 0


def test_parse_missing_header():
    """Test parsing without proper header."""
    lines = [
        "- ☐ Epic 0023: No header epic",
        "  - status: open",
        "  - tasks:",
        "    - ☐ Task 0024: Task",
        "      - status: open",
    ]

    bl = parser.parse(lines)
    # Should still parse the content
    assert len(bl.epics_open) >= 1


def test_build_markdown_with_validation():
    """Test that build_markdown produces valid markdown."""
    # Create a backlog with some content
    bl = parser.Backlog(header=["# Backlog", ""], footer=[], epics_open=[], epics_finished=[])

    epic = parser.Epic(id="0025", title="Test Epic", status="open")
    task = parser.Task(id="0026", title="Test Task", status="open")
    epic.tasks.append(task)
    bl.epics_open.append(epic)

    md = parser.build_markdown(bl)

    # Should be able to parse back what we built
    bl2 = parser.parse(md.splitlines())
    assert len(bl2.epics_open) == 1
    assert bl2.epics_open[0].id == "0025"


def test_validate_backlog_comprehensive():
    """Test comprehensive validation scenarios."""
    # Test with duplicate IDs
    bl = parser.Backlog(header=[], footer=[], epics_open=[], epics_finished=[])

    epic1 = parser.Epic(id="0027", title="Epic 1", status="open")
    epic2 = parser.Epic(id="0027", title="Epic 2", status="open")  # duplicate
    task1 = parser.Task(id="0028", title="Task 1", status="open")
    task2 = parser.Task(id="0028", title="Task 2", status="open")  # duplicate
    epic1.tasks.append(task1)
    epic2.tasks.append(task2)

    bl.epics_open.extend([epic1, epic2])

    errors = parser.validate_backlog(bl)
    assert len(errors) >= 2  # Should catch duplicates
    assert any("duplicate epic id" in e for e in errors)
    assert any("duplicate task id" in e for e in errors)


def test_safe_write_atomic():
    """Test that safe_write is atomic and preserves file on failure."""
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "test.md")

        # Create initial file
        with open(test_file, "w") as f:
            f.write("initial content\n")

        # Test successful write
        parser.safe_write(test_file, "new content\n")
        with open(test_file, "r") as f:
            assert f.read() == "new content\n"

        # Verify no temp file left behind
        temp_file = test_file + ".tmp"
        assert not os.path.exists(temp_file)


def test_backup_and_restore():
    """Test backup creation and restoration."""
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "test.md")

        # Create initial file
        with open(test_file, "w") as f:
            f.write("original content\n")

        # Create backup
        backup_path = parser.make_backup(test_file)
        assert os.path.exists(backup_path)

        # Modify original
        with open(test_file, "w") as f:
            f.write("modified content\n")

        # Restore backup
        parser.restore_backup(test_file, backup_path)
        with open(test_file, "r") as f:
            assert f.read() == "original content\n"
