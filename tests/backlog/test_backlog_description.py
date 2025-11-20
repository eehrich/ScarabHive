"""Test description field functionality for add commands."""
from scripts.backlog_tool import parser as bl


def test_add_task_with_description():
    """Test that add_task_to_epic correctly handles description field."""
    # Create a minimal backlog with one epic
    lines = [
        "# Backlog",
        "",
        "## 1. Epics - open",
        "",
        "- Epic 0001: Test Epic",
        "  - status: open",
        "",
        "## 2. Epics - finished",
        "",
        "## 3. Footer",
    ]

    backlog = bl.parse(lines)

    # Add a task with description
    description_text = "This is a test description\nWith multiple lines"
    task = bl.add_task_to_epic(
        backlog,
        "0001",
        "Test Task",
        notes="Some notes",
        description=description_text
    )

    # Verify the task has the description
    assert task.description == ["This is a test description", "With multiple lines"]
    assert task.notes == ["Some notes"]
    assert task.title == "Test Task"


def test_add_epic_with_description():
    """Test that add_epic_to_backlog correctly handles description field."""
    # Create a minimal backlog
    lines = [
        "# Backlog",
        "",
        "## 1. Epics - open",
        "",
        "## 2. Epics - finished",
        "",
        "## 3. Footer",
    ]

    backlog = bl.parse(lines)

    # Add an epic with description
    description_text = "This is a test epic description\nWith multiple lines"
    epic = bl.add_epic_to_backlog(
        backlog,
        "Test Epic",
        description=description_text
    )

    # Verify the epic has the description
    assert epic.description == ["This is a test epic description", "With multiple lines"]
    assert epic.title == "Test Epic"


def test_build_markdown_with_description():
    """Test that description fields are properly serialized to markdown."""
    # Create a backlog with task and epic that have descriptions
    lines = [
        "# Backlog",
        "",
        "## 1. Epics - open",
        "",
        "## 2. Epics - finished",
        "",
        "## 3. Footer",
    ]

    backlog = bl.parse(lines)

    # Add epic with description
    epic = bl.add_epic_to_backlog(
        backlog,
        "Epic with Description",
        description="Epic description\nSecond line"
    )

    # Add task with description
    bl.add_task_to_epic(
        backlog,
        epic.id,
        "Task with Description",
        description="Task description\nSecond line"
    )

    # Build markdown and check that descriptions are included
    markdown_text = bl.build_markdown(backlog)

    # Should contain both descriptions
    assert "Epic description" in markdown_text
    assert "Second line" in markdown_text
    assert "Task description" in markdown_text
