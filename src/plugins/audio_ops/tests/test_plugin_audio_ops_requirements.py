"""What `pip install -e .` installs carries what pydub needs on every Python the project accepts.

pydub imports audioop, which Python 3.13 removed (PEP 594); there it falls back to pyaudioop and
fails to import without the audioop-lts backport. The audio_ops tests skipped then, all of them,
as if pydub were not installed. Read from requirements/all.txt, so the check holds whatever this
interpreter is and whatever the environment has installed besides.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from packaging.requirements import Requirement

REPO = Path(__file__).resolve().parents[4]


def _installed_on(python: str) -> set[str]:
    """The names requirements/all.txt installs on that Python version."""
    env = {"python_version": python, "python_full_version": f"{python}.0"}
    lines = (REPO / "requirements" / "all.txt").read_text(encoding="utf-8").splitlines()
    specs = [Requirement(line) for line in (line.strip() for line in lines) if line and not line.startswith("#")]
    return {spec.name.lower() for spec in specs if spec.marker is None or spec.marker.evaluate(env)}


@pytest.mark.parametrize("python", ["3.13", "3.14"])
def test_pydub_comes_with_the_audioop_backport_where_python_lacks_audioop(python):
    names = _installed_on(python)
    assert "pydub" in names, "fixture: pydub is no longer a requirement -- this check has nothing to guard"
    assert "audioop-lts" in names, f"pydub cannot import on Python {python} without audioop-lts"


def test_the_backport_is_not_asked_of_a_python_that_has_audioop():
    """audioop-lts publishes wheels for 3.13 and later only."""
    assert "audioop-lts" not in _installed_on("3.12")
