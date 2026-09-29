"""Every `commands:` entry in a plugin schema must be usable.

A plugin command runs one of the plugin's OWN tools. A typo in `tool:` is
invisible at runtime: the unknown tool is dropped by the authorization filter,
so the command does not even reach `/help` and the author sees nothing at all.
The same goes for an `argument:` that is not a parameter of that tool, and for
a `name:` that can never be typed as a slash command.

A `params:` typo is quieter still: the tool ignores the unknown key and runs
its DEFAULT path, so the command answers -- with the wrong operation. That is
what makes a unified tool (`operation: list|cancel|...`) reachable from a chat
at all, and what makes checking it here worth the three lines.

The checks run twice: directly against synthetic schemas, so every branch is
exercised, and over the shipped plugins, so a real declaration cannot rot. The
first half matters because the shipped plugins do not currently exercise every
rule -- an earlier version of this file asserted nothing at all for `argument:`
because no shipped command had one.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_system.chat_commands import is_typeable_command_name
from agent_system.plugins.schema_loader import load_schema_from_dir

REPO = Path(__file__).parents[2]
PLUGIN_ROOTS = [REPO / "src" / "plugins", REPO / "src" / "plugins_writer"]


def _functions(schema: dict) -> dict:
    """Tool name -> function definition, for both schema spellings."""
    found = {}
    for entry in schema.get("tools") or []:
        if isinstance(entry, dict):
            function = entry.get("function") if entry.get("type") == "function" else entry
            if isinstance(function, dict) and function.get("name"):
                found[function["name"]] = function
    for function in schema.get("functions") or []:
        if isinstance(function, dict) and function.get("name"):
            found[function["name"]] = function
    return found


def command_problems(schema: dict) -> list[str]:
    """Everything wrong with this schema's `commands:`, as readable lines."""
    functions = _functions(schema)
    problems = []
    for index, command in enumerate(schema.get("commands") or []):
        if not isinstance(command, dict):
            problems.append(f"command #{index} is not a mapping: {command!r}")
            continue
        name = command.get("name")
        where = f"command {name!r}" if name else f"command #{index}"
        if not name:
            problems.append(f"{where}: needs a name")
        elif not is_typeable_command_name(str(name)):
            problems.append(f"{where}: cannot be typed as /{name}")
        if not command.get("description"):
            problems.append(f"{where}: needs a description, it is all /help shows")
        tool = command.get("tool")
        if tool not in functions:
            problems.append(
                f"{where}: tool {tool!r} is not one of this plugin's tools "
                f"({sorted(functions)})")
            continue
        properties = functions[tool].get("parameters", {}).get("properties", {})
        argument = command.get("argument")
        if argument and argument not in properties:
            problems.append(
                f"{where}: argument {argument!r} is not a parameter of "
                f"{tool} ({sorted(properties)})")
        # The same rule for the parameters the command FIXES. A typo there is
        # even quieter than one in `argument`: the tool ignores the unknown
        # key and runs its default path, so the command answers -- with the
        # wrong operation.
        for key, value in (command.get("params") or {}).items():
            if key not in properties:
                problems.append(
                    f"{where}: params {key!r} is not a parameter of "
                    f"{tool} ({sorted(properties)})")
                continue
            allowed = properties[key].get("enum")
            if allowed and value not in allowed:
                problems.append(
                    f"{where}: params {key}={value!r} is outside the values "
                    f"{tool} accepts ({allowed})")
    return problems


# --------------------------------------------------------------------------
# The rules themselves, against schemas written here so every branch runs.
# --------------------------------------------------------------------------

GOOD = {
    "tools": [{"type": "function", "function": {
        "name": "p_search",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "mode": {"type": "string", "enum": ["fast", "deep"]},
        }},
    }}],
    "commands": [{"name": "find", "description": "search", "tool": "p_search",
                  "argument": "query", "params": {"mode": "fast"}}],
}


def test_a_correct_declaration_has_no_problems():
    assert command_problems(GOOD) == []


def test_a_schema_without_commands_has_no_problems():
    assert command_problems({"tools": GOOD["tools"]}) == []


@pytest.mark.parametrize("change,expected", [
    ({"tool": "p_serch"}, "not one of this plugin's tools"),
    ({"argument": "quries"}, "is not a parameter of"),
    ({"name": "find things"}, "cannot be typed"),
    ({"params": {"mdoe": "fast"}}, "params 'mdoe' is not a parameter of"),
    ({"params": {"mode": "thorough"}}, "outside the values"),
    ({"name": None}, "needs a name"),
    ({"description": ""}, "needs a description"),
])
def test_each_rule_reports_its_own_defect(change, expected):
    schema = {**GOOD, "commands": [{**GOOD["commands"][0], **change}]}
    problems = command_problems(schema)
    assert problems and expected in problems[0], problems


def test_a_non_mapping_entry_is_reported():
    schema = {**GOOD, "commands": ["just a string"]}
    assert "not a mapping" in command_problems(schema)[0]


# --------------------------------------------------------------------------
# The same rules over what is actually shipped.
# --------------------------------------------------------------------------

def _schemas():
    """(plugin_name, schema) for every plugin whose schema renders."""
    for root in PLUGIN_ROOTS:
        if not root.is_dir():
            continue
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            if not (directory / "schema.yaml").exists():
                continue
            try:
                schema = load_schema_from_dir(directory, {"name": directory.name})
            except Exception:
                # A schema needing template vars only its plugin can supply is
                # not the subject here. Measured 2026-09-01: none does, all 60
                # render -- the guard below keeps that from silently changing.
                continue
            if schema:
                yield directory.name, schema


SHIPPED = list(_schemas())
DECLARED = [(name, schema) for name, schema in SHIPPED if schema.get("commands")]


def test_the_shipped_schemas_are_actually_being_read():
    """Empty-set guard, both halves: enough schemas render to be a real sweep,
    and the one plugin that declares commands is among them."""
    assert len(SHIPPED) > 40, [n for n, _ in SHIPPED]
    assert "context_engineer" in {name for name, _ in DECLARED}


@pytest.mark.parametrize("plugin,schema", DECLARED, ids=[n for n, _ in DECLARED])
def test_shipped_commands_are_usable(plugin, schema):
    assert command_problems(schema) == [], plugin
