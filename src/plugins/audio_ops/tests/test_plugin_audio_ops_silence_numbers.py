"""silencedetect prints negative starts (encoder priming) and exponents.

"[\\d.]+" skipped "-0.0013" and read "1e-05" as "1"; the index pairing of
starts and ends then shifted every later segment.
"""
import re

from plugins.audio_ops.server import SILENCE_NUMBER

STDERR = ("[silencedetect @ 0x1] silence_start: -0.0013\n"
          "[silencedetect @ 0x1] silence_end: 1.5 | silence_duration: 1e-05\n"
          "[silencedetect @ 0x1] silence_start: 3.25\n")


def test_every_start_is_read():
    assert re.findall(rf"silence_start: {SILENCE_NUMBER}", STDERR) == ["-0.0013", "3.25"]


def test_an_exponent_is_read_whole():
    assert re.findall(rf"silence_duration: {SILENCE_NUMBER}", STDERR) == ["1e-05"]
