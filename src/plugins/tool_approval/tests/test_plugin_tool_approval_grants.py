"""What "allow calls like this for the session" covers: a command's first words, never what a shell chains to them."""
from __future__ import annotations

import pytest

from plugins.tool_approval.grants import call_grant

TOOL = "terminal/terminal_execute"


def covers(granted: dict, later: dict) -> bool:
    key, _, _ = call_grant(TOOL, granted)
    return key in call_grant(TOOL, later)[2]


@pytest.mark.parametrize("granted, later, covered", [
    ("git status -s", "git status", True),
    ("git status", "git status origin --short", True),   # a shorter prefix covers a longer command
    ("git status", "git push", False),
    ("npm run build", "npm run test", False),
    ("ls", "ls; rm -rf ~", False),                       # a chained command covers only itself ...
    ("ls", "ls && rm -rf ~", False),
    ("ls", "ls | sh", False),
    ("ls", "ls $(rm -rf ~)", False),
    ("ls", "ls > /etc/passwd", False),
    ("ls; rm -rf ~", "ls", False),                       # ... and is granted as itself only
    ("ls; rm -rf ~", "ls; rm -rf ~", True),
])
def test_a_command_grant_covers_its_first_words_and_never_a_chain(granted, later, covered):
    assert covers({"command": granted}, {"command": later}) is covered


def test_a_command_on_another_machine_is_not_covered():
    assert covers({"command": "uptime", "machine": "a"}, {"command": "uptime -p", "machine": "a"})
    assert not covers({"command": "uptime", "machine": "a"}, {"command": "uptime", "machine": "b"})


def test_every_other_argument_must_be_the_same_not_only_text():
    """env_vars with GIT_CONFIG_* or PATH made an allowed git status run any program."""
    assert not covers({"command": "git status"},
                      {"command": "git status", "env_vars": {"GIT_CONFIG_KEY_0": "core.fsmonitor"}})
    assert covers({"command": "git status", "env_vars": {"A": "1"}},
                  {"command": "git status -s", "env_vars": {"A": "1"}})
    assert call_grant(TOOL, {"command": "git status", "env_vars": {"A": "1"}})[1] == \
        "'git status …', the other arguments as in this call"


@pytest.mark.parametrize("command", ["ls\x1fevil", "ls evil", "ls\x0bevil", "ls evil"])
def test_a_space_a_shell_does_not_split_at_covers_only_itself(command):
    """Python's split() cut there: an allowed ls covered the program ls<US>evil."""
    assert not covers({"command": "ls"}, {"command": command})
    assert call_grant(TOOL, {"command": command})[1] == "this exact call"


def test_a_lone_surrogate_is_keyed_not_raised():
    """As UTF-8 it raised: the hook failed and blocked the call instead of asking."""
    cut = "cut " + chr(0xD83D)
    assert covers({"text": cut}, {"text": cut})


def test_a_call_without_a_command_covers_exactly_its_arguments():
    assert covers({"path": "a.txt", "content": "x"}, {"content": "x", "path": "a.txt"})
    assert not covers({"path": "a.txt", "content": "x"}, {"path": "b.txt", "content": "x"})


def test_the_person_reads_what_the_grant_covers():
    assert call_grant(TOOL, {"command": "git status -s", "machine": "a"})[1] == "'git status …', machine a"
    assert call_grant(TOOL, {"command": "ls; rm -rf ~"})[1] == "this exact call"
