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


def test_update_task_status_preserves_closed_date_for_terminal_states():
    """Test that cancelled/failed/rejected/reverted tasks keep their closed date."""
    bl = parser.parse(sample_lines())

    # Test cancelled status
    parser.update_task_status(bl, '0001', 'cancelled')
    epic, t = parser.find_task(bl, '0001')
    assert t.status == 'cancelled'
    assert t.closed == date.today().isoformat()

    # Test failed status
    parser.update_task_status(bl, '0001', 'failed')
    epic, t = parser.find_task(bl, '0001')
    assert t.status == 'failed'
    assert t.closed == date.today().isoformat()

    # Test rejected status
    parser.update_task_status(bl, '0001', 'rejected')
    epic, t = parser.find_task(bl, '0001')
    assert t.status == 'rejected'
    assert t.closed == date.today().isoformat()

    # Test reverted status
    parser.update_task_status(bl, '0001', 'reverted')
    epic, t = parser.find_task(bl, '0001')
    assert t.status == 'reverted'
    assert t.closed == date.today().isoformat()


def test_update_task_status_clears_closed_date_for_non_terminal_states():
    """Test that reopening a task clears the closed date."""
    bl = parser.parse(sample_lines())

    # First set to cancelled (terminal state)
    parser.update_task_status(bl, '0001', 'cancelled')
    epic, t = parser.find_task(bl, '0001')
    assert t.closed == date.today().isoformat()

    # Then change to open (non-terminal state)
    parser.update_task_status(bl, '0001', 'open')
    epic, t = parser.find_task(bl, '0001')
    assert t.status == 'open'
    assert t.closed is None
