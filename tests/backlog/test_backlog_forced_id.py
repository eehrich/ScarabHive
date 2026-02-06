import pytest
from scripts.backlog_tool import parser as bl

SAMPLE = '''# Backlog


## 1. Epics - open

- ☐ Epic 0001: Sample Epic
  - status: open
  - tasks:
    - ☐ Task 0002: Existing Task
      - status: open
'''


def test_add_epic_with_forced_numeric_id():
    lines = SAMPLE.splitlines()
    backlog = bl.parse(lines)
    e = bl.add_epic_to_backlog(backlog, 'Forced Title', forced_id='0123')
    assert e.id == '0123'
    assert any(ep.id == '0123' for ep in backlog.epics_open)


def test_add_epic_with_non_numeric_id_raises():
    lines = SAMPLE.splitlines()
    backlog = bl.parse(lines)
    with pytest.raises(ValueError):
        bl.add_epic_to_backlog(backlog, 'Bad', forced_id='abc')


def test_add_task_with_forced_numeric_id():
    lines = SAMPLE.splitlines()
    backlog = bl.parse(lines)
    t = bl.add_task_to_epic(backlog, '0001', 'New Task', forced_id='0456')
    assert t.id == '0456'
    assert any(t2.id == '0456' for e in backlog.epics_open for t2 in e.tasks)


def test_add_task_forced_id_collision_raises():
    lines = SAMPLE.splitlines()
    backlog = bl.parse(lines)
    # existing ids include 0001 and 0002
    with pytest.raises(ValueError):
        bl.add_task_to_epic(backlog, '0001', 'Collision Task', forced_id='0002')
