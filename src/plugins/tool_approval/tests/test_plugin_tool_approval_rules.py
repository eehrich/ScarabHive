"""allow/deny rules: the tool pattern means what it means in tools.allowed, and
an argument pattern narrows it."""
from __future__ import annotations

import pytest

from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns
from plugins.tool_approval.rules import RuleError, first_match, parse_rule, parse_rules

#: (tool name as the model calls it, server that runs it)
CALLS = [
    ("terminal_execute", "terminal"),
    ("terminal_get_output", "terminal"),
    ("file_ops_read_file", "file_ops"),
    ("workspace_file_ops_write_file", "workspace_file_ops"),
    ("weather_get_forecast", "weather.get_forecast"),
]
PATTERNS = [
    "terminal/*", "terminal", "terminal/terminal_execute", "*_read_*", "file_ops/file_ops_*",
    "workspace_file_ops/*", "weather.*", "nothing/*", "*",
]


@pytest.mark.parametrize("pattern", PATTERNS)
def test_a_tool_pattern_matches_what_the_allowlist_matches(pattern):
    """One matcher for both: a rule written like an allowlist entry names the
    same calls -- externals ("server.tool") included."""
    rule = parse_rule(pattern, "deny[0]")
    for name, server in CALLS:
        assert rule.matches(name, server, {}) == tool_matches_patterns(name, server, [pattern]), (pattern, name)


def test_the_parity_fixture_is_not_all_or_nothing():
    """Otherwise the parity above would hold for a matcher that answers one way."""
    outcomes = {parse_rule(p, "x").matches(n, s, {}) for p in PATTERNS for n, s in CALLS}
    assert outcomes == {True, False}


def test_an_argument_pattern_narrows_the_rule():
    rule = parse_rule({"tool": "terminal/*", "arguments": {"command": r"\brm\s+-[a-z]*r"}}, "deny[0]")

    assert rule.matches("terminal_execute", "terminal", {"command": "rm -rf /tmp/x"})
    assert not rule.matches("terminal_execute", "terminal", {"command": "ls -la"})
    assert not rule.matches("terminal_execute", "terminal", {}), "a call without the argument matched"
    assert not rule.matches("file_ops_delete", "file_ops", {"command": "rm -rf /"}), "the tool pattern was skipped"


def test_every_argument_pattern_must_match():
    rule = parse_rule({"tool": "file_ops/*", "arguments": {"path": r"^/etc/", "mode": "w"}}, "deny[0]")

    assert rule.matches("file_ops_write_file", "file_ops", {"path": "/etc/hosts", "mode": "w"})
    assert not rule.matches("file_ops_write_file", "file_ops", {"path": "/etc/hosts", "mode": "r"})


def test_a_value_that_is_no_string_is_matched_as_its_json():
    rule = parse_rule({"tool": "x/*", "arguments": {"paths": r'"/etc/'}}, "deny[0]")

    assert rule.matches("x_do", "x", {"paths": ["/home/a", "/etc/passwd"]})
    assert not rule.matches("x_do", "x", {"paths": ["/home/a"]})


def test_the_first_matching_rule_is_returned():
    rules = parse_rules(["file_ops/*", {"tool": "terminal/*", "arguments": {"command": "sudo"}}], "deny")

    assert first_match(rules, "terminal_execute", "terminal", {"command": "sudo ls"}) is rules[1]
    assert first_match(rules, "terminal_execute", "terminal", {"command": "ls"}) is None
    assert first_match(rules, "terminal_execute", "terminal", "not a mapping") is None


@pytest.mark.parametrize("raw, message", [
    ({"tool": "t/*", "argument": {"command": "rm"}}, "unknown key"),
    ({"tool": "t/*", "arguments": {"command": "("}}, "no valid regex"),
    ({"tool": "t/*", "arguments": {"command": 5}}, "must be a string"),
    ({"tool": "t/*", "arguments": ["command"]}, "must map"),
    ({"arguments": {"command": "rm"}}, "'tool' must be"),
    ("", "empty tool pattern"),
    (7, "expected a tool pattern"),
])
def test_a_rule_that_cannot_be_read_is_an_error(raw, message):
    with pytest.raises(RuleError, match=message):
        parse_rule(raw, "deny[0]")


def test_a_list_is_required():
    assert parse_rules(None, "allow") == []
    with pytest.raises(RuleError, match="list"):
        parse_rules("terminal/*", "deny")
    with pytest.raises(RuleError, match=r"deny\[1\]"):
        parse_rules(["terminal/*", {"tool": ""}], "deny")
