"""Shared guards for the CLI tests.

German with the user, code and test names in English.
"""
from __future__ import annotations

import pytest

from agent_system import paths


@pytest.fixture(autouse=True)
def _no_launch_dir_carried_between_tests():
    """Give every test back the "never entered" state paths.py starts in.

    `enter_project` remembers where the process began, and a CLI main() under
    test writes that global for the REST of the pytest session. Run the suite
    from outside the checkout and the first main() pins it to wherever pytest
    was started; from then on the export tests write their transcripts there
    instead of into their own tmp_path, and the failure looks like a bug in
    user_path. One line per test is cheaper than finding that once.
    """
    before = paths._launch_dir
    paths._launch_dir = None
    try:
        yield
    finally:
        paths._launch_dir = before
