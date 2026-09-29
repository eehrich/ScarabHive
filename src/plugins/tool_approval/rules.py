"""allow/deny rules for tool calls.

A rule names tools the way an agent's ``tools.allowed`` does -- ``server/tool``,
``server/*``, ``server``, fnmatch wildcards -- and is matched by the same
function (``tool_matches_patterns``), so a pattern means the same thing in both
places. It may add patterns on the call's arguments.

Two spellings::

    - "file_ops/file_ops_read_file"          # the tool alone
    - tool: "terminal/terminal_execute"      # the tool and its arguments
      arguments:
        command: "\\brm\\s+-[a-z]*r"         # re.search on the value

Every argument pattern must match for the rule to match. A value that is not a
string is matched as its JSON text; a call without the argument does not match.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional, Tuple

from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns

_RULE_KEYS = frozenset({"tool", "arguments"})


class RuleError(ValueError):
    """A rule in the configuration cannot be read."""


@dataclass(frozen=True)
class Rule:
    """One allow or deny rule."""

    tool: str
    arguments: Tuple[Tuple[str, "re.Pattern[str]"], ...] = ()

    def matches(self, name: str, server: str, arguments: Mapping[str, Any]) -> bool:
        """Whether the call of ``name`` on ``server`` with ``arguments`` is one this rule names."""
        if not tool_matches_patterns(name, server, [self.tool]):
            return False
        for key, pattern in self.arguments:
            if key not in arguments:
                return False
            if not pattern.search(_as_text(arguments[key])):
                return False
        return True

    def describe(self) -> str:
        """The rule as a person would write it, for messages and logs."""
        if not self.arguments:
            return self.tool
        parts = ", ".join(f"{key} ~ /{pattern.pattern}/" for key, pattern in self.arguments)
        return f"{self.tool} ({parts})"


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def parse_rule(raw: Any, where: str) -> Rule:
    """One rule from the configuration; ``where`` names it in the error."""
    if isinstance(raw, str):
        if not raw.strip():
            raise RuleError(f"{where}: an empty tool pattern")
        return Rule(tool=raw.strip())
    if not isinstance(raw, Mapping):
        raise RuleError(f"{where}: expected a tool pattern or a mapping with 'tool', got {type(raw).__name__}")
    unknown = sorted(set(raw) - _RULE_KEYS)
    if unknown:
        raise RuleError(f"{where}: unknown key(s) {unknown}; a rule has 'tool' and optionally 'arguments'")
    tool = raw.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise RuleError(f"{where}: 'tool' must be a non-empty pattern")
    patterns = raw.get("arguments") or {}
    if not isinstance(patterns, Mapping):
        raise RuleError(f"{where}: 'arguments' must map argument names to regular expressions")
    compiled: List[Tuple[str, "re.Pattern[str]"]] = []
    for key, pattern in patterns.items():
        if not isinstance(pattern, str):
            raise RuleError(f"{where}: the pattern for argument '{key}' must be a string")
        try:
            compiled.append((str(key), re.compile(pattern)))
        except re.error as exc:
            raise RuleError(f"{where}: the pattern for argument '{key}' is no valid regex ({exc})") from exc
    return Rule(tool=tool.strip(), arguments=tuple(compiled))


def parse_rules(raw: Any, kind: str) -> List[Rule]:
    """The rules of one list (``kind`` is "allow" or "deny"); None or empty is no rules."""
    if raw is None:
        return []
    if isinstance(raw, (str, Mapping)) or not isinstance(raw, Iterable):
        raise RuleError(f"{kind}: expected a list of rules")
    return [parse_rule(item, f"{kind}[{index}]") for index, item in enumerate(raw)]


def first_match(rules: Iterable[Rule], name: str, server: str,
                arguments: Optional[Mapping[str, Any]]) -> Optional[Rule]:
    """The first rule that names the call, or None."""
    args = arguments if isinstance(arguments, Mapping) else {}
    for rule in rules:
        if rule.matches(name, server, args):
            return rule
    return None
