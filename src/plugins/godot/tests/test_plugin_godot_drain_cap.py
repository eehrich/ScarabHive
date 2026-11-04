"""A run's output is bounded in memory: the head and the latest part are kept."""
import io

from plugins.godot.server import _drain, _text


def test_a_long_output_keeps_head_and_tail_within_the_cap():
    data = b"".join(f"line {n:06d}\n".encode() for n in range(200_000))  # ~2.4 MB
    sink: list[bytes] = []
    _drain(io.BufferedReader(io.BytesIO(data)), sink, cap=256 * 1024)
    text = _text(sink)
    assert sum(len(c) for c in sink) < 256 * 1024 + 1024
    assert text.startswith("line 000000") and text.rstrip().endswith("line 199999")
    assert "[output cut: too long]" in text


def test_a_short_output_is_kept_whole():
    sink: list[bytes] = []
    _drain(io.BufferedReader(io.BytesIO(b"hello\n")), sink, cap=1024)
    assert _text(sink) == "hello\n"
