"""Fix round of the 2026-10-15 model review: the validator survives a package that fails on import (R-MODEL-1),
and fixed agent params are judged as bind_params sees them (R-MODEL-4)."""
from __future__ import annotations

import datetime
import textwrap

import pytest

from plugins.stategraph.model.spec import ParamSpec
from plugins.stategraph.model.validate import agent_params_problems
from plugins.stategraph.tests.stategraph_testkit import errors, validate


@pytest.mark.parametrize("raises", ["", "raise RuntimeError('needs config')", "raise SystemExit(3)",
                                    "raise KeyError('x')"], ids=["healthy", "runtime", "system_exit", "key"])
def test_a_parent_package_that_fails_on_import_does_not_crash_the_validator(tmp_path, monkeypatch, raises):
    package = tmp_path / f"r15boom_{len(raises)}"
    package.mkdir()
    (package / "__init__.py").write_text(raises + "\n", encoding="utf-8")
    (package / "mod.py").write_text("def f(text):\n    return text\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    text = textwrap.dedent(f"""\
        stategraph: 1
        id: m
        initial: a
        states:
          a:
            do: {{call: '{package.name}.mod:f', args: {{nope: 1}}}}
            transitions:
              - target: b
          b: {{type: final}}
        """)

    tree = validate({"m.yaml": text})

    told = [p for p in errors(tree) if p.code == "SG004" and "nope" in p.message]
    assert bool(told) == (not raises), tree.problems  # healthy: the signature is read; failing: left to the run


def test_a_fixed_param_is_judged_as_json_like_every_run_binds_it():
    declared = {"day": ParamSpec(type="string")}

    assert agent_params_problems("m", declared, {"input": "json", "params": {"day": datetime.date(2026, 10, 15)}}) == []
    assert agent_params_problems("m", declared, {"input": "json", "params": {"day": 3}}) != []
