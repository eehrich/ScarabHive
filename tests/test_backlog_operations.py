from datetime import date

from scripts.backlog_tool import parser


def sample_lines():
    return [
        "# Backlog",
        "",
        "## 1. Epics - open",
        "",
        "- ☐ Epic 0001: Source Epic",
        "  - status: open",
    "  - tasks:",
        "    - ☐ Task 0001: First task",
        "      - status: open",
        "      - added: 2025-08-01",
        "    - ☐ Task 0002: Second task",
        "      - status: open",
        "",
        "- ☐ Epic 0002: Dest Epic",
        "  - status: open",
    "  - tasks:",
        "",
        "## 2. Epics - finished",
        "",
    ]


def test_move_task():
    bl = parser.parse(sample_lines())
    assert any(e.id == '0001' for e in bl.epics_open)
    parser.move_task(bl, '0002', '0002')
    src = next(e for e in bl.epics_open if e.id == '0001')
    dest = next(e for e in bl.epics_open if e.id == '0002')
    assert all(t.id != '0002' for t in src.tasks)
    assert any(t.id == '0002' for t in dest.tasks)


def test_update_task_status_sets_closed_date():
    bl = parser.parse(sample_lines())
    parser.update_task_status(bl, '0001', 'done')
    epic, t = parser.find_task(bl, '0001')
    assert t.status == 'done'
    assert t.closed == date.today().isoformat()
