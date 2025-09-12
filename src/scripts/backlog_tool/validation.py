"""Validation functionality for backlog integrity checking.

This module contains functions for validating backlog structure and data integrity.
"""
import datetime
import logging
from typing import Optional

from .models import Backlog
from . import values

logger = logging.getLogger(__name__)


def validate_backlog(backlog: Backlog) -> list[str]:
    """Run comprehensive validation rules and return list of error strings.

    Validation rules:
    - Epic ids must be unique across open and finished lists.
    - Task ids must be unique across all epics.
    - Date fields (added, closed) must be ISO dates YYYY-MM-DD when present.
    - Status values must be among allowed set.
    - Epics with all tasks finished should be marked as finished.
    - ID format validation (4-digit numeric preferred).
    - Required fields presence.

    Args:
        backlog: Backlog object to validate

    Returns:
        List of error messages (empty if valid)
    """
    # Defensive isinstance check for runtime safety, though mypy considers it unreachable
    # due to type annotation. This is intentional defensive programming.
    if not isinstance(backlog, Backlog):
        return ["Invalid backlog object provided"]

    errors: list[str] = []
    logger.debug("Starting backlog validation")

    try:
        # Check epic ids uniqueness
        epic_ids = [e.id for e in backlog.epics_open + backlog.epics_finished]
        dup_epics = {i for i in epic_ids if epic_ids.count(i) > 1}
        for de in sorted(dup_epics):
            errors.append(f"duplicate epic id: {de}")

        # Check task ids uniqueness
        task_ids: list[str] = []
        for e in backlog.epics_open + backlog.epics_finished:
            for t in e.tasks:
                task_ids.append(t.id)
        dup_tasks = {i for i in task_ids if task_ids.count(i) > 1}
        for dt in sorted(dup_tasks):
            errors.append(f"duplicate task id: {dt}")

        # Date format validation
        def is_iso_date(s: Optional[str]) -> bool:
            if not s:
                return True
            try:
                datetime.date.fromisoformat(s)
                return True
            except Exception:
                return False

        # Validate epic fields
        for e in backlog.epics_open + backlog.epics_finished:
            if not e.id or not e.id.strip():
                errors.append(f"epic missing id: {e.title}")
            if not e.title or not e.title.strip():
                errors.append(f"epic {e.id} missing title")

            if not is_iso_date(e.added):
                errors.append(f"bad date (added) for epic {e.id}: {e.added}")
            if not is_iso_date(e.closed):
                errors.append(f"bad date (closed) for epic {e.id}: {e.closed}")

            # Validate task fields
            for t in e.tasks:
                if not t.id or not t.id.strip():
                    errors.append(f"task in epic {e.id} missing id: {t.title}")
                if not t.title or not t.title.strip():
                    errors.append(f"task {t.id} in epic {e.id} missing title")

                if not is_iso_date(t.added):
                    errors.append(f"bad date (added) for task {t.id}: {t.added}")
                if not is_iso_date(t.closed):
                    errors.append(f"bad date (closed) for task {t.id}: {t.closed}")

        # Status values validation
        allowed = set(values.get('allowed_statuses', ["open", "done", "closed", "complete", "finished", "resolved", "in progress", "todo"]))
        for e in backlog.epics_open + backlog.epics_finished:
            if e.status and e.status.strip().lower() not in allowed:
                errors.append(f"unknown epic status for {e.id}: {e.status}")
            for t in e.tasks:
                if t.status and t.status.strip().lower() not in allowed:
                    errors.append(f"unknown task status for {t.id}: {t.status}")

        # Check if epics should be finished
        finish_list = set(values.get('finish_statuses', ["done", "closed", "complete", "finished"]))
        for e in backlog.epics_open + backlog.epics_finished:
            if not getattr(e, 'tasks', None):
                continue

            all_finished = True
            for t in e.tasks:
                st = (t.status or '').strip().lower()
                if st not in finish_list:
                    all_finished = False
                    break

            if all_finished:
                est = (e.status or '').strip().lower()
                if est not in finish_list:
                    errors.append(f"epic {e.id} not finished but all tasks are finished")

        # ID format validation (prefer 4-digit numeric)
        for e in backlog.epics_open + backlog.epics_finished:
            if e.id and e.id.isdigit() and len(e.id) != 4:
                errors.append(f"epic id {e.id} should be 4 digits")
            for t in e.tasks:
                if t.id and t.id.isdigit() and len(t.id) != 4:
                    errors.append(f"task id {t.id} should be 4 digits")

        logger.info(f"Validation completed: {len(errors)} errors found")

    except Exception as e:
        errors.append(f"Validation failed with exception: {e}")
        logger.error(f"Validation exception: {e}")

    return errors
