"""Detects a model that is stuck INSIDE its own thinking.

A reasoning model can fall into a loop while thinking and keep paying for it:
measured in the debugger DB, one run spent 65.535 reasoning tokens over 20
minutes and its thinking ended in `Need perhaps "Bettkante" yes.` — repeated
forty times. Nothing downstream noticed, because the call eventually returned.

WHY REPETITION, AND NOT TIME OR TOKENS
======================================
The obvious guard — abort after N minutes or N tokens — was measured against
4.916 stored runs and is WRONG. The two most expensive calls in the whole
database (37 min / 85.051 reasoning tokens, and 35 min / 74.441) are HEALTHY:
both thought that long, then delivered a correct result. A budget rule would
have killed exactly those and paid for a restart.

What separates the two is repetition, measured in a SLIDING WINDOW:

    worst window of 4.914 healthy runs   0.19
    the two stuck runs                   0.90 and 0.95
    any threshold in 0.30 .. 0.60        both caught, zero false alarms

THE WINDOW IS THE POINT
=======================
Measured over the whole text so far (a prefix), the same two runs look clean —
0.00 up to 100.000 characters — because the healthy first half dilutes the
loop that starts later. Only a window shows the step: 0.00 in one window, 0.84
in the next. A prefix-based version of this detector would be measuring
nothing, which is why the window is not an implementation detail.

LIMITS, HONESTLY
================
The false-alarm side rests on 4.914 runs; the threshold itself rests on two
positives. Aborting would have saved 8,6 of 20,1 minutes in one case and 2,5
of 16,8 in the other — the value is that a stuck model becomes VISIBLE, not
the minutes.

The gap between 0.19 and the threshold is smaller than "zero false alarms"
suggests. Synthetic probes at this geometry: a plan template with one varying
number scores 0.39, a chapter template with a varying title 0.51, and a long
fixed preamble followed by a short varying tail 0.59 — above the line. No real
run came near that, but the threshold does not tolerate being lowered towards
0.30, and a 60-character piece reacts to templates whose variable part is
short.

What reaches this detector is every ``thinking_delta`` the client emits. For
most models that is their raw thinking; for the OpenAI family it is the
SUMMARY of their thinking, which the Responses client forwards under the same
chunk type. Summary prose is a different shape of text from raw reasoning, and
the calibration below was measured on raw reasoning — so for those models this
is a guard against the degenerate case, not a calibrated instrument.

WHERE IT IS ARMED AND BLIND
===========================
Some clients emit no ``thinking_delta`` at all, so nothing reaches this
detector and it can never fire:

  * Both Gemini clients stream thoughts as ``content_delta`` — deliberately,
    and pinned by ``test_streaming_thought_parts_stream_as_content_delta``.
  * ``BatchLLMClient.supports_streaming()`` is False, so batch runs take the
    polling path, which sees no deltas.

Silence on those paths is NOT evidence that the thinking was healthy, and the
comment at the call site calling these "Gemini reasoning/thinking tokens" is
wrong. Feeding ``content_delta`` in instead would mean measuring answer prose
with a threshold calibrated on reasoning — a different instrument, not a
wider one.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class ReasoningLoopError(Exception):
    """Raised in the run loop when a call's thinking started repeating.

    Deliberately NOT an LLM error: nothing is wrong with the provider, the
    model, or the request — the sampling walked into a circle. The run loop
    therefore retries the SAME model once instead of switching profiles, which
    would punish a healthy model for one unlucky roll.
    """

    def __init__(self, reason: str, *, characters: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.characters = characters


class ReasoningLoopDetector:
    """Watches a stream of reasoning deltas for a repeating loop.

    One instance per LLM call — it carries the text of that call and nothing
    else. Feed it every reasoning delta as it arrives; it answers with a reason
    string the first time the window repeats past the threshold, and ``None``
    every other time.

    Usage:
        detector = ReasoningLoopDetector(**config)
        for delta in reasoning_deltas:
            reason = detector.record(delta)
            if reason:
                ...  # abort this call and try again

    Disabled or mis-configured, it is a no-op: ``record`` always returns None.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        repetition_threshold: float = 0.5,
        window_chars: int = 20_000,
        check_every_chars: int = 10_000,
        shingle_chars: int = 60,
        stride_chars: int = 10,
    ) -> None:
        """
        Args:
            enabled: Off means every ``record`` returns None.
            repetition_threshold: Share of duplicate windows that counts as a
                loop. Measured band: healthy runs peak at 0.19, stuck runs sit
                at 0.90+, so anything in 0.30..0.60 separates them.
            window_chars: How much recent thinking is judged at once. A loop
                that starts late is invisible in a longer view.
            check_every_chars: How much new thinking to collect between two
                checks. Deltas arrive in the hundreds; measuring on each one
                would re-scan the window for every token.
            shingle_chars: Length of the compared text pieces. Changing it
                changes what the threshold means — both were measured together.
            stride_chars: Distance between two compared pieces. See
                ``_repetition``: it decides which loop periods are visible at
                all, and it was measured against the corpus, not chosen.
        """
        self.repetition_threshold = float(repetition_threshold)
        self.window_chars = int(window_chars)
        self.check_every_chars = int(check_every_chars)
        self.shingle_chars = int(shingle_chars)
        self.stride_chars = max(1, int(stride_chars))
        # A threshold outside (0, 1] can never fire, and a window that cannot
        # hold four pieces has nothing to compare — treat both as "off" rather
        # than as a detector that silently never triggers.
        self.enabled = (
            bool(enabled)
            and 0.0 < self.repetition_threshold <= 1.0
            and self.window_chars >= self.shingle_chars * 4
            and self.check_every_chars > 0
        )
        self._chunks: list[str] = []
        self._buffered = 0      # characters currently held in _chunks
        self._since_check = 0   # characters seen since the last measurement
        self._fired = False
        self._total = 0

    @property
    def characters_seen(self) -> int:
        """Total reasoning characters this call has produced."""
        return self._total

    def record(self, delta: str) -> Optional[str]:
        """Take one reasoning delta; return a reason once a loop is found.

        Returns None while things look healthy, while disabled, and on every
        call after the first detection — the caller acts once.
        """
        if not self.enabled or self._fired or not delta:
            return None
        self._chunks.append(delta)
        self._buffered += len(delta)
        self._since_check += len(delta)
        self._total += len(delta)
        if self._since_check < self.check_every_chars:
            return None
        if self._buffered < self.window_chars:
            # Not enough thinking yet to judge a full window. Wait rather than
            # judge a short one: early text is where healthy runs look most
            # repetitive (headers, enumerations), so a short window is exactly
            # where a false alarm would come from.
            self._since_check = 0
            return None

        window = "".join(self._chunks)[-self.window_chars:]
        # Collapse to one chunk: the join above is the only place that pays for
        # the accumulated pieces, and past the window they are never read again.
        self._chunks = [window]
        self._buffered = len(window)
        self._since_check = 0

        score = self._repetition(window)
        if score < self.repetition_threshold:
            return None
        self._fired = True
        return (f"thinking repeats itself ({score:.2f} of the last "
                f"{self.window_chars} characters, threshold "
                f"{self.repetition_threshold:.2f})")

    def _repetition(self, text: str) -> float:
        """Share of DUPLICATE pieces: 0.0 all different, towards 1.0 a loop.

        THE STRIDE DECIDES WHAT IS VISIBLE, so it was measured, not chosen.
        Pieces are only ever taken at multiples of the stride, so two of them
        match only when their distance is a multiple of the loop's period:
        the number of distinct pieces is ``period / gcd(period, stride)``,
        capped by how many pieces fit in the window. A stride that shares no
        factor with the period is therefore blind to it — the whole loop
        scores as if every piece were new.

        Measured over 4.916 stored runs, worst healthy window against the two
        stuck runs:

            stride  1   0.55  |  1.00  0.90      healthy ceiling unusable
            stride 10   0.19  |  0.95  0.90      chosen
            stride 15   0.16  |  0.93  0.69
            stride 30   0.10  |  0.86  0.69      blind above ~330 characters

        Stride 10 lifts the WEAKER stuck run from 0.69 to 0.90 while the
        healthy ceiling rises only to 0.19, and its 2.000 pieces per window
        (against 665) keep even a period that shares no factor with it
        visible up to roughly a thousand characters — a 503-character loop
        scores 0.75 here and 0.24 at stride 30. Reading EVERY offset removes
        the blind spot entirely but is useless: healthy thinking is locally
        self-similar enough to reach 0.55, which leaves no room under any
        threshold.

        What stays blind: a loop whose unit is longer than the window can
        hold, and one whose period shares no factor with the stride AND runs
        past a thousand characters. Both are named here rather than papered
        over — a measure that sees everything has not been found.
        """
        width = self.shingle_chars
        if len(text) < width * 4:
            return 0.0
        pieces = [text[i:i + width]
                  for i in range(0, len(text) - width, self.stride_chars)]
        if not pieces:
            return 0.0
        return 1.0 - len(set(pieces)) / len(pieces)
