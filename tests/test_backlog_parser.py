from scripts.backlog_tool import parser


def test_parse_minimal():
    lines = [
        "# Backlog",
        "",
        "## 1. Epics - open",
        "",
        "- ☐ Epic 0018: Backlog maintenance tool",
        "  - status: ☐",
    "  - tasks:",
        "    - ☐ Task 0189: Design CLI",
        "      - status: open",
    ]
    bl = parser.parse(lines)
    assert bl.epics_open
    epic = bl.epics_open[0]
    assert epic.id == "0018"
    assert len(epic.tasks) == 1
    task = epic.tasks[0]
    assert task.id == "0189"
    assert task.title.startswith("Design CLI")


def test_build_markdown_roundtrip(tmp_path):
    lines = [
        "# Backlog",
        "",
    ]
    bl = parser.parse(lines)
    # add an epic programmatically
    e = parser.Epic(id="9999", title="Tst Epic", status="open")
    t = parser.Task(id="999901", title="Sample", status="open", added="2025-08-28")
    e.tasks.append(t)
    bl.epics_open.append(e)
    md = parser.build_markdown(bl)
    assert "Epic 9999" in md
    assert "Task 999901" in md


def test_build_markdown_with_cancelled_status():
    """Test that cancelled tasks show red cross symbol."""
    bl = parser.Backlog(header=["# Backlog", ""], footer=[], epics_open=[], epics_finished=[])

    epic = parser.Epic(id="0030", title="Test Epic", status="open")
    task_cancelled = parser.Task(id="0031", title="Cancelled Task", status="cancelled", added="2025-08-28", closed="2025-08-28")
    task_failed = parser.Task(id="0032", title="Failed Task", status="failed", added="2025-08-28", closed="2025-08-28")
    task_rejected = parser.Task(id="0033", title="Rejected Task", status="rejected", added="2025-08-28", closed="2025-08-28")
    task_reverted = parser.Task(id="0034", title="Reverted Task", status="reverted", added="2025-08-28", closed="2025-08-28")

    epic.tasks.extend([task_cancelled, task_failed, task_rejected, task_reverted])
    bl.epics_open.append(epic)

    md = parser.build_markdown(bl)

    # Check that all terminal status tasks show red cross
    assert "❌ Task 0031: Cancelled Task" in md
    assert "☐ Task 0032: Failed Task" in md
    assert "❌ Task 0033: Rejected Task" in md
    assert "❌ Task 0034: Reverted Task" in md

    # Check that closed dates are preserved
    assert "- closed: 2025-08-28" in md


def test_build_markdown_with_mixed_statuses():
    """Test symbol mapping for various task statuses."""
    bl = parser.Backlog(header=["# Backlog", ""], footer=[], epics_open=[], epics_finished=[])

    epic = parser.Epic(id="0040", title="Mixed Status Epic", status="open")
    tasks = [
        parser.Task(id="0041", title="Open Task", status="open", added="2025-08-28"),
        parser.Task(id="0042", title="In Progress Task", status="in progress", added="2025-08-28"),
        parser.Task(id="0043", title="Done Task", status="done", added="2025-08-28", closed="2025-08-28"),
        parser.Task(id="0044", title="Cancelled Task", status="cancelled", added="2025-08-28", closed="2025-08-28"),
    ]

    epic.tasks.extend(tasks)
    bl.epics_open.append(epic)

    md = parser.build_markdown(bl)

    # Check correct symbols for each status
    assert "☐ Task 0041: Open Task" in md
    assert "⏳ Task 0042: In Progress Task" in md
    assert "✅ Task 0043: Done Task" in md
    assert "❌ Task 0044: Cancelled Task" in md
