# agent_eval

Measures agents against task suites: a suite names an agent, a few variants of it (arms) and tasks with a check
each; every arm runs every task several times, each run in a process and workspace of its own, and the report
compares the arms task by task -- with a verdict that says when a difference is too small to tell from noise.

- **Panel** "Evals" (admin): start a suite, read runs, compare arms or against the suite's baseline.
- **CLI** `agent-eval run|list|show|baseline|stop` (also `python -m plugins.agent_eval`).
- **Suites** live in `evals/<name>/suite.yaml` -- the project's and every plugin's.
- **Checks**: command, pytest (share of green tests), expectations on the answer, a script, or a Python function.
- **Overlays**: an arm's change to the agent -- and, under `agents`, to the agents it starts -- for the run only;
  `machine_overlay_problems()` limits what a
  model-written overlay may change and lets it only narrow the agent's rights.
- No tools, no hooks.

Switch on with `agent_eval: {type: agent_eval, enabled: true}` under `plugins.servers`. Everything else --
the suite format, how to read a report, how a run is executed -- is in [agent_eval.guide](agent_eval.guide).
