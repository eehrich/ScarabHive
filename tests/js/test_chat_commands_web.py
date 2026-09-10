"""Run the browser-side command harness from the Python suite.

The module under test is plain JS with no framework, so the harness is a node
script with a DOM stub next to this file. Wiring it in here means it runs with
everything else instead of only when someone remembers it.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "chat_commands_web.test.js"
CHAT_MODULE = Path(__file__).parents[2] / "static" / "js" / "chat_module.js"


def test_both_surfaces_default_to_the_same_count():
    """`/sessions` without an argument must mean the same in both chats.

    The catalogue in chat_commands.py advertises the same grammar to the
    terminal and the browser, but the browser cannot import DEFAULT_LIMIT --
    it carries the number as a literal. If one side is retuned and the other
    is not, the same keystrokes answer with two different listings and nothing
    else in the repo would notice.
    """
    from agent_system.cli_utils.session_listing import DEFAULT_LIMIT

    source = CHAT_MODULE.read_text(encoding="utf-8")
    match = re.search(r"raw \? parseInt\(raw, 10\) : (\d+)", source)
    assert match, "the /sessions count is no longer parsed the way this test reads it"
    assert int(match.group(1)) == DEFAULT_LIMIT, (
        f"chat_module.js lists {match.group(1)} sessions by default, "
        f"agent-cli lists {DEFAULT_LIMIT}")


def test_web_chat_commands():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run([node, str(HARNESS)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
