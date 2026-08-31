"""A prompt must not carry a clock that ticks inside the cached prefix.

Measured 2026-09-01 on a live sysadmin_agent chat: the system prompt was
5354 characters and identical for the first 5290 of them — then

    - Current time: 01:01:39      turn n
    - Current time: 01:01:58      turn n+1

Those 64 characters sit inside the block the cache breakpoint covers, so
every turn produced a new prefix. The system prompt is only 4% of the
payload, but the 94 conversation messages behind it lose the cache with it:
the chat footer showed 21-22% cache hits and ~$0.13 per turn on an almost
unchanged history.

`current_date` is fine — it breaks the cache once a day. Anything carrying
seconds is not. Agents that genuinely need the exact time have the
`datetime` tool, which asks at the moment it matters instead of freezing an
answer into every later turn.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Template variables that change faster than a conversation does.
TICKING = ("current_time", "current_datetime", "unix_timestamp")

#: Matches "{{ var" / "{{- var" and requires a word boundary after the name:
#: "{{ current_timezone }}" starts with "{{ current_time" and is perfectly
#: stable — a prefix test reported all seven files right after the fix.
OPENER = r"\{\{-?\s*"
BOUNDARY = r"\b"

#: Where prompt templates live.
PROMPT_DIRS = (
    REPO / "config" / "agents" / "prompts",
    REPO / "config" / "prompts",
    REPO / "src" / "plugins",
    REPO / "src" / "plugins_writer",
)


def _prompt_files():
    for base in PROMPT_DIRS:
        if not base.exists():
            continue
        for path in base.rglob("*.md"):
            if "prompts" in path.parts or base.name == "prompts":
                yield path


def test_no_prompt_embeds_a_ticking_clock():
    offenders = []
    for path in _prompt_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for var in TICKING:
            if re.search(OPENER + var + BOUNDARY, text):
                offenders.append(f"{path.relative_to(REPO)}: {var}")
    assert not offenders, (
        "these prompts re-render a second-precision clock into the cached "
        "prefix, so every turn re-pays the whole history: "
        + "; ".join(offenders)
        + " -- use current_date, or the datetime tool for the exact time")


def test_the_scan_actually_reaches_the_prompts():
    """Empty-set trap: a path typo would make the test above pass forever."""
    files = list(_prompt_files())
    assert len(files) > 20, f"only {len(files)} prompt files found"
    assert any("system_admin_prompt" in f.name for f in files), (
        "the prompt this was measured on is not being scanned")
