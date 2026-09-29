"""Run the browser-side composer-history harness from the Python suite.

The module under test is plain JS with no framework, so the harness is a
node script with a DOM stub next to this file. Wiring it in here means it
runs with everything else instead of only when someone remembers it.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "input_history.test.js"


def test_the_browser_escape_rules_match_the_python_ones():
    """The two regexes in input_history.js are copies. Copies drift.

    The browser cannot import chat_commands, so needsEscape() carries the
    patterns as literals. If _COMMAND_WORD or _QUALIFIED_WORD is ever changed
    on the Python side, the composer would start escaping messages that the
    parser no longer unescapes -- and the agent would receive them with an
    extra slash. This test is the only thing that would notice.
    """
    from agent_system import chat_commands

    source = (Path(__file__).parent.parent.parent / "static" / "js"
              / "input_history.js").read_text(encoding="utf-8")
    for name in ("_COMMAND_WORD", "_QUALIFIED_WORD"):
        pattern = getattr(chat_commands, name).pattern
        # JS needs the slashes escaped inside a regex literal; nothing else
        # differs between the two flavours for these patterns.
        as_js = pattern.replace("/", r"\/")
        assert as_js in source, (
            f"{name} is {pattern!r} in chat_commands.py but input_history.js "
            f"does not carry the same rule")


def test_composer_history():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    assert HARNESS.exists(), f"harness missing: {HARNESS}"

    result = subprocess.run(
        [node, str(HARNESS)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
