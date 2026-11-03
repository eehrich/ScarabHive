"""A foreground ssh answer is bounded like terminal's."""


def test_a_foreground_answer_is_bounded():
    """`cat big.log` returned megabytes into the model's context."""
    from plugins.ssh_control import tool_server

    big = "x" * (tool_server._ANSWER_STREAM_CAP * 3)
    cut = tool_server._capped(big)
    assert len(cut) < tool_server._ANSWER_STREAM_CAP + 200 and cut.endswith("limit output]")
    assert tool_server._capped("short") == "short"
