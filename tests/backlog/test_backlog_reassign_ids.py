from scripts.backlog_tool import parser as bl


def test_reassign_duplicate_task_ids_ignores_long_ids():
    """Ensure reassign_duplicate_task_ids ignores long numeric IDs (e.g. '900100') when
    computing the next 4-digit id and reassigns duplicates to short 4-digit ids.
    """
    # build backlog with an epic that has duplicate long task ids
    b = bl.Backlog(header=[], epics_open=[], epics_finished=[], footer=[])

    # Epic with a canonical 4-digit epic id that will influence the start
    epic1 = bl.Epic(id='9001', title='Epic 9001', status='open')
    # Two tasks with the same long migration-style id that should be reassigned
    t1 = bl.Task(id='900100', title='task-a', status='open')
    t2 = bl.Task(id='900100', title='task-b', status='open')
    epic1.tasks = [t1, t2]
    b.epics_open.append(epic1)

    # Another epic to populate the existing id pool with a small numeric id
    epic2 = bl.Epic(id='0002', title='Epic 0002', status='open')
    t3 = bl.Task(id='0003', title='task-c', status='open')
    epic2.tasks = [t3]
    b.epics_open.append(epic2)

    # Run reassign_duplicate_task_ids
    changes = bl.reassign_duplicate_task_ids(b)

    # Expect that the duplicate long ids were changed
    assert changes, "expected at least one reassignment"

    # No duplicate task ids remain
    all_ids = [t.id for e in b.epics_open + b.epics_finished for t in e.tasks]
    assert len(all_ids) == len(set(all_ids)), f"duplicate ids remain: {all_ids}"

    # All newly assigned ids should be canonical 4-digit numeric ids and different from the long original
    for old, new in changes:
        assert new.isdigit() and len(new) == 4, f"new id {new} is not 4-digit numeric"
        assert old != new

    # Also check that none of the new ids equal the long migration ids
    assert not any(id_.isdigit() and len(id_) > 4 for id_ in all_ids if id_ not in [c[1] for c in changes])
