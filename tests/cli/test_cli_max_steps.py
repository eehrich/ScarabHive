"""``agent-cli run|chat --max-steps N`` -- the step budget for one process.

The manual promised this flag years before it existed. ``git log -S`` over
agent_cli.py finds ``max_steps`` only as a display field of the config-agent
listing that was removed in 2025 -- never as an argument. Anyone following
docs/cli_reference.md got "unrecognized arguments: --max-steps".

What is NOT covered here: that the number really reaches the run loop. That
needs a bootstrapped agent and was measured live instead (``--max-steps 2``
turns the status line into "step 1/2" for an agent configured with 200).
These tests pin the two things a fast process can prove -- the flag exists on
both subcommands, and a budget below one is refused at PARSE time rather than
after a fifteen-second bootstrap.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def run_cli(args: list[str]) -> tuple[int, str, str]:
    finished = subprocess.run(
        [sys.executable, "-m", "agent_system.agent_cli", *args],
        cwd=str(REPO), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    return finished.returncode, finished.stdout, finished.stderr


class TestTheFlagIsOffered:
    @pytest.mark.parametrize("subcommand", ["run", "chat"])
    def test_on_both_subcommands(self, subcommand):
        """chat shares the resolved agent with run, so the budget has to be
        settable there too -- a REPL is where a long run is watched."""
        code, out, _err = run_cli([subcommand, "-h"])

        assert code == 0
        # With the metavar, not as a substring: "--max-steps-anything" would
        # satisfy a bare "--max-steps" in out.
        assert "--max-steps N" in out, out


class TestABudgetBelowOneIsRefused:
    """A zero budget is not a smaller run, it is an agent that cannot take a
    single step -- and it would only show up as an empty answer."""

    @pytest.mark.parametrize("value", ["0", "-3"])
    def test_the_value_is_rejected(self, value):
        code, _out, err = run_cli(["run", "--max-steps", value, "task"])

        assert code == 2, err
        assert "at least 1" in err

    def test_it_is_refused_at_parse_time_not_after_the_bootstrap(self):
        """argparse prints the usage banner; a later sys.exit does not.

        Checking this behind the plugin bootstrap would answer a typo fifteen
        seconds later, which is the wrong place to learn it.
        """
        _code, _out, err = run_cli(["run", "--max-steps", "0", "task"])

        assert "usage:" in err.lower(), err
        assert "bootstrapping servers" not in err.lower()
