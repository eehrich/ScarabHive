"""The machine file format (docs/stategraph_design.md §2, §4 SG001/SG006): spec models and loader.

Every test loads YAML text through the real loader (``load_tree`` over
``SnapshotSources``, the class a resume reads a definition snapshot with) and
the real validator; nothing builds a ``MachineSpec`` by hand.

Mutation checks run (each turned the named tests red, then was restored from a copy):
- spec.Strict: ``extra="forbid"`` -> ``extra="ignore"``           -> test_unknown_key_*
- loader.parse_yaml: ``allow_duplicate_keys = True``              -> test_duplicate_key_*
- loader._load: the ``_non_string_keys`` check removed             -> test_unquoted_template_*
- loader._load: the version check removed                          -> test_unknown_format_version_*
- loader._load: ``if target in stack`` -> ``if False``             -> test_import_cycle_* (recursion error)
- loader._load: ``(load_module if execute else scan_module)`` -> ``load_module``
                                                                    -> test_companion_module_is_not_executed_*
- loader.line_of: ``+ 1`` removed                                  -> test_problems_carry_the_line_*
- code.module_names: the ``startswith("_")`` filter removed        -> test_private_companion_names_are_not_in_scope
"""

from __future__ import annotations

import pytest

from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, errors, found, load, tool_config, validate

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

MINIMAL = """\
stategraph: 1
id: m
initial: a
states:
  a:
    type: final
"""


def test_minimal_machine_loads_clean():
    tree = validate({"m.yaml": MINIMAL})
    assert tree.problems == []
    assert tree.root_file.spec is not None and tree.root_file.spec.id == "m"


def test_unknown_key_is_sg001_with_path_and_line():
    tree = validate({"m.yaml": """\
        stategraph: 1
        id: m
        initial: a
        states:
          a:
            type: final
          b:
            trasitions: []
        """})
    [problem] = found(tree, "SG001")
    assert problem.level == "error"
    assert problem.path == "states.b.trasitions"
    assert problem.line == 8
    assert "trasitions" in problem.message


def test_unknown_top_level_key_is_sg001():
    tree = validate({"m.yaml": MINIMAL + "tittle: typo\n"})
    [problem] = found(tree, "SG001")
    assert problem.path == "tittle"
    assert problem.line == 7


def test_duplicate_key_is_sg001_with_its_line():
    tree = validate({"m.yaml": """\
        stategraph: 1
        id: m
        initial: a
        states:
          a:
            type: final
          a:
            type: final
        """})
    [problem] = found(tree, "SG001")
    assert "duplicate" in problem.message
    assert problem.line == 7
    assert tree.root_file.spec is None, "a file with a duplicate key must not produce a spec"


def test_unquoted_template_is_sg001_at_the_field():
    tree = validate({"m.yaml": """\
        stategraph: 1
        id: m
        context: {x: 1}
        initial: a
        states:
          a:
            do:
              agent: writer
              task: {{ ctx.x }}
            transitions:
              - target: b
          b:
            type: final
        """})
    [problem] = found(tree, "SG001")
    assert problem.path == "states.a.do.task"
    assert problem.line == 9
    assert "quote" in problem.message


def test_quoted_template_is_accepted():
    tree = validate({"m.yaml": """\
        stategraph: 1
        id: m
        context: {x: 1}
        initial: a
        states:
          a:
            do:
              agent: writer
              task: "{{ ctx.x }}"
            transitions:
              - target: b
          b:
            type: final
        """})
    assert errors(tree) == []


@pytest.mark.parametrize("header", ["stategraph: 2\n", "stategraph: '1'\n", ""])
def test_unknown_format_version_is_refused(header):
    text = MINIMAL.replace("stategraph: 1\n", header)
    tree = validate({"m.yaml": text})
    [problem] = found(tree, "SG001")
    assert problem.path == "stategraph"
    assert "format version" in problem.message
    assert tree.root_file.spec is None


def test_names_follow_the_name_pattern_and_reserved_words():
    tree = validate({"m.yaml": """\
        stategraph: 1
        id: m
        context: {done: 1}
        initial: a
        states:
          a:
            type: final
          Bad:
            type: final
        """})
    messages = " | ".join(p.message for p in found(tree, "SG001"))
    assert "'done' is reserved" in messages
    assert "'Bad'" in messages


def test_a_duration_must_be_parseable():
    tree = validate({"m.yaml": """\
        stategraph: 1
        id: m
        events: {go: {}}
        initial: w
        states:
          w:
            timeout: five minutes
            transitions:
              - trigger: go
                target: done
          done:
            type: final
        """})
    [problem] = found(tree, "SG001")
    assert problem.path == "states.w.timeout"


def test_import_cycle_is_sg006():
    tree = validate({
        "m.yaml": MINIMAL + "imports: {helper: ./b.yaml}\n",
        "b.yaml": """\
            stategraph: 1
            id: b
            imports: {back: ./m.yaml}
            initial: a
            states:
              a:
                type: final
            """,
    })
    [problem] = found(tree, "SG006")
    assert problem.file == "b.yaml"
    assert problem.path == "imports.back"
    assert "m.yaml -> b.yaml -> m.yaml" in problem.message


def test_self_import_is_a_cycle():
    tree = validate({"m.yaml": MINIMAL + "imports: {me: ./m.yaml}\n"})
    [problem] = found(tree, "SG006")
    assert "cycle" in problem.message


def test_unresolvable_import_is_sg006():
    tree = validate({"m.yaml": MINIMAL + "imports: {gone: ./missing.yaml, unknown: no_such_machine}\n"})
    paths = sorted(p.path for p in found(tree, "SG006"))
    assert paths == ["imports.gone", "imports.unknown"]


def test_imports_resolve_by_relative_path_and_by_id():
    tree = validate({
        "m.yaml": MINIMAL + "imports: {by_path: ./lib/sub.yaml}\n",
        "lib/sub.yaml": MINIMAL.replace("id: m", "id: sub"),
    })
    assert tree.problems == []
    assert tree.root_file.imports == {"by_path": "lib/sub.yaml"}


# ------------------------------------------------------------------ companion module

def _side_effect_module(flag) -> str:
    return (
        "from pathlib import Path\n"
        f"Path({str(flag)!r}).write_text('imported')\n"
        "def build(ctx):\n"
        "    return 'task'\n"
    )


MACHINE_WITH_MODULE = """\
stategraph: 1
id: m
python: m.py
initial: a
states:
  a:
    do:
      agent: writer
      task: "{{ build(ctx) }}"
    transitions:
      - target: done
  done:
    type: final
"""


def test_companion_module_is_not_executed_during_validation(tmp_path):
    flag = tmp_path / "imported.flag"
    files = {"m.yaml": MACHINE_WITH_MODULE, "m.py": _side_effect_module(flag)}

    tree = validate(files)

    assert errors(tree) == [], "the AST scan must know build() without running the module"
    assert not flag.exists(), "validation executed the companion module"
    # Positive control: the module really has the side effect when a run loads it.
    load(files, execute=True)
    assert flag.exists(), "fixture: the module's import side effect never happens -- the test would be vacuous"


def test_companion_module_is_not_executed_by_the_service_validate(tmp_path):
    """The same through the tool front end: validate_machine, list_machines and get_machine never import it."""
    flag = tmp_path / "imported.flag"
    server = StateGraphServer("stategraph", _config(), tool_config(tmp_path))
    try:
        machines = tmp_path / "machines"
        (machines / "m.yaml").write_text(MACHINE_WITH_MODULE, encoding="utf-8")
        (machines / "m.py").write_text(_side_effect_module(flag), encoding="utf-8")

        result = server.service.validate(files={"m.yaml": MACHINE_WITH_MODULE}, machine_id="m")
        listed = server.service.list_machines()
        got = server.service.get_machine("m")
    finally:
        server.run_store.close()

    assert [p for p in result["problems"] if p["level"] == "error"] == []
    assert listed[0]["valid"] is True
    assert "m.py" in got["files"]
    assert not flag.exists(), "a front end executed the companion module while only reading the machine"


def test_missing_companion_module_is_sg004():
    tree = validate({"m.yaml": MACHINE_WITH_MODULE})
    [problem] = [p for p in found(tree, "SG004") if p.path == "python"]
    assert "not found" in problem.message
    assert problem.line == 3


def test_companion_module_that_does_not_compile_is_sg004():
    tree = validate({"m.yaml": MACHINE_WITH_MODULE, "m.py": "def build(ctx:\n    return 1\n"})
    [problem] = [p for p in found(tree, "SG004") if p.file == "m.py"]
    assert "does not compile" in problem.message


def test_private_companion_names_are_not_in_scope():
    tree = validate({
        "m.yaml": MACHINE_WITH_MODULE.replace("build(ctx)", "_hidden(ctx)"),
        "m.py": "def _hidden(ctx):\n    return 1\n",
    })
    [problem] = found(tree, "SG004")
    assert "_hidden" in problem.message


def test_problems_carry_the_line_of_the_offending_key():
    tree = validate({"m.yaml": """\
        stategraph: 1
        id: m
        context: {n: 0}
        initial: a
        states:
          a:
            do:
              agent: writer
              task: go
            transitions:
              - target: nowhere
              - trigger: error
                guard: out == 1
                target: done
          done:
            type: final
        """})
    by_code = {p.code: p for p in errors(tree)}
    assert by_code["SG002"].path == "states.a.transitions[0].target"
    assert by_code["SG002"].line == 11
    assert by_code["SG004"].path == "states.a.transitions[1].guard"
    assert by_code["SG004"].line == 13
    assert all(p.file == "m.yaml" for p in tree.problems)


def _config():
    from agent_system.config.models import AgentSystemConfig

    return AgentSystemConfig()
