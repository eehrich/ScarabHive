from scripts.backlog_tool import parser


def test_parse_minimal():
    lines = [
        "# Backlog",
        "",
        "## 1. Epics - open",
        "",
        "- ☐ Epic 0018: Backlog maintenance tool",
        "  - status: ☐",
        "  - Subtasks:",
        "    - ☐ Task 0189: Design CLI",
        "      - status: open",
    ]
    bl = parser.parse(lines)
    assert bl.epics_open
    epic = bl.epics_open[0]
    assert epic.id == "0018"
    assert len(epic.subtasks) == 1
    task = epic.subtasks[0]
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
    e.subtasks.append(t)
    bl.epics_open.append(e)
    md = parser.build_markdown(bl)
    assert "Epic 9999" in md
    assert "Task 999901" in md
