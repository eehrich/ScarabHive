"""Tests for the Agent's per-session live conversation state.

The Agent is a process-wide singleton shared by concurrent requests for
DIFFERENT sessions. Live "current messages / tools schema" used to live on a
single shared attribute (_current_messages / _current_tools_schema), so request
B could overwrite it while a compaction tool for session A read it — persisting
B's conversation into A (cross-session data leak + corruption).

These tests exercise the per-session helpers (_set_live_messages /
get_live_messages / _set_live_tools_schema / get_live_tools_schema) directly via
the unbound Agent methods on a lightweight stub, so no full Agent construction
is required.
"""

from __future__ import annotations

from agent_system.servers.agent.server import Agent


class _Stub:
    """Minimal carrier of the attributes/methods the live-state helpers touch.

    Binds the real Agent live-state methods so we exercise the actual code
    without constructing a full Agent (which needs heavy config wiring).
    """

    def __init__(self):
        self._live_state_by_session = {}
        self._live_state_max_sessions = 200
        self._current_messages = []
        self._current_tools_schema = []
        # Bind the real methods to this instance
        for name in (
            "_set_live_messages", "_set_live_tools_schema",
            "_evict_live_state", "get_live_messages", "get_live_tools_schema",
        ):
            setattr(self, name, getattr(Agent, name).__get__(self))


def test_live_messages_are_session_scoped():
    a = _Stub()
    Agent._set_live_messages(a, "sess_a", ["A1", "A2"])
    Agent._set_live_messages(a, "sess_b", ["B1"])

    # Each session sees only its own messages
    assert Agent.get_live_messages(a, "sess_a") == ["A1", "A2"]
    assert Agent.get_live_messages(a, "sess_b") == ["B1"]
    # Unknown session does NOT leak another session's messages
    assert Agent.get_live_messages(a, "sess_unknown") is None
    assert Agent.get_live_messages(a, None) is None


def test_concurrent_overwrite_does_not_cross_sessions():
    """Interleaved writes for two sessions stay isolated (the original race)."""
    a = _Stub()
    Agent._set_live_messages(a, "sess_a", ["A-secret"])
    # session B "runs" and overwrites the shared state in the old design
    Agent._set_live_messages(a, "sess_b", ["B-other"])
    # session A's compaction tool reads back ITS messages, not B's
    assert Agent.get_live_messages(a, "sess_a") == ["A-secret"]


def test_tools_schema_is_session_scoped():
    a = _Stub()
    Agent._set_live_tools_schema(a, "sess_a", [{"name": "tool_a"}])
    Agent._set_live_tools_schema(a, "sess_b", [{"name": "tool_b"}])
    assert Agent.get_live_tools_schema(a, "sess_a") == [{"name": "tool_a"}]
    assert Agent.get_live_tools_schema(a, "sess_b") == [{"name": "tool_b"}]
    assert Agent.get_live_tools_schema(a, "missing") is None


def test_live_state_is_lru_bounded():
    a = _Stub()
    a._live_state_max_sessions = 5
    for i in range(20):
        Agent._set_live_messages(a, f"sess_{i}", [f"m{i}"])
    # The dict never grows unbounded
    assert len(a._live_state_by_session) <= 5
    # The most-recently-written session is retained
    assert Agent.get_live_messages(a, "sess_19") == ["m19"]


def test_none_session_id_is_noop_for_store():
    """A None session_id (internal/CLI call) only updates the deprecated attr."""
    a = _Stub()
    Agent._set_live_messages(a, None, ["x"])
    assert a._live_state_by_session == {}
    assert a._current_messages == ["x"]  # deprecated shared fallback still set
