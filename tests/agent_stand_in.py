"""A stand-in for the agent in CLI tests: the real Agent class with the stand-in's body.

Replacing the class in its module instead (``monkeypatch.setattr("...server.Agent", Stand)``) poisons every plugin
first imported during that test: plugin discovery imports them all, and stategraph's ``class MachineAgent(Agent)``
then subclasses the stand-in for the rest of the session (measured: 28 facade tests failed after
test_cli_attachments.py). Patching the methods keeps the class; subclasses built meanwhile stay real ones.
"""


def stand_in_for_agent(monkeypatch, stand_in) -> None:
    """``Agent(...)`` runs the stand-in's ``__init__`` and ``run_events`` for the test; isinstance holds."""
    from agent_system.servers.agent.server import Agent

    monkeypatch.setattr(Agent, "__init__", stand_in.__init__)
    monkeypatch.setattr(Agent, "run_events", stand_in.run_events)
