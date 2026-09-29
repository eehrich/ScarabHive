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

A SECOND, LONGER WINDOW — AND ITS OWN, THINNER GROUND
=====================================================
Everything above belongs to the 20.000-character window. It is blind to a loop
whose period is longer than it can hold, and that blindness was measured, not
feared: of 17 calls that ran into the 131.072-token output ceiling, nine still
have their thinking stored, and this window fires on NONE of them. They repeat
at periods of roughly 2.000 to 16.500 characters — a piece can only match one
that far back if the window spans it, so the ceiling on the score is
(window - period) / window, which for a period of 13.799 leaves 0.31 and no
threshold can help. Their cost: 11,2 hours of wall clock in 25 days, and worse,
a judge that returns NOTHING, which reads downstream as "no findings".

Simply enlarging the window would trade one family for the other. Of the seven
runs this detector HAS caught, the two whose record outlived the debugger's
retention had thought 40.040 and 90.080 characters; for the other five only
the floor is certain — 20.000, or the short window could not have judged at
all. A 200.000-character window would have looked at neither of the two,
because a window only judges once it is full. So a call is watched by TWO
detectors, and the long one only ever catches what the short one lets through.

Measured with this module, streamed as in production, at window 200.000 and
threshold 0.5:

    the 9 stored runaway loops   0.892 0.706 0.656 0.642 0.577
                                 0.569 0.542 0.334 0.121   -> 7 caught
    healthy runs it judged       0.043 worst of EIGHT
    scored windows per runaway   6 at a 50.000 interval, ~3 ms each (the
                                 checks at 50k/100k/150k find the window
                                 not yet full and score nothing)

THE EIGHT IS THE NUMBER THAT MATTERS, and it is not the 4.914 above: of 388
healthy runs offered to the long window, 380 were never judged at all because
it never filled. Its false-alarm side therefore rests on eight runs, and no
threshold should be derived from it. All eight are writer scorers. This
detector runs on every streaming agent, but outside those four only five calls
in 25 days thought long enough to fill the window at all (~51.500 reasoning
tokens at a measured 3,4..3,9 characters per token) — and none of the five
kept its body, so for them the long window is unmeasured, not cleared.

What that thin ground hides, measured rather than waved away: a template whose
varying share is 23..38 % scores 0.45..0.49 in the short window and 0.91 in the
long one. The long window CAN raise an alarm the short one would not — for an
exact template the number of distinct pieces is bounded by its period while the
window grows, so the score walks towards 1.0. Real runs are nowhere near: the
worst of the eight it judged scores 0.043, a factor of ten below that band and
of eleven below the threshold. That is the same argument the short
window rests on, with more room, not less — but it is an argument about the
corpus we have, not a property of the measure.

Eight runs are enough ground here because the CONSEQUENCE is bounded, not
because the evidence is strong. A template does not roll dice the way an
unlucky sampling loop does: it would score the same on the retry, and a guard
that kept watching would lock such an agent out for good. It does not — the run
loop retries an aborted call on the same client with the watchdog off
(``watch_reasoning=current_llm is not reasoning_loop_llm``, server.py). That
exemption lasts one STEP, though: every step re-arms the watchdog, so a template
that trips it pays one discarded call per client in each step of each run — at
least twice per scorer run with its follow-up. What is bounded is the lock-out
and the cost per step, at most a doubling; the total over many runs is not.
That is bearable on eight runs a factor of eleven below the line; on a guard
whose false alarm were common it would not be.

The trade, stated rather than implied: the long window judges LATE, because it
judges only once its buffer is full. A false alarm there throws away about ten
times the thinking one in the short window does. Against 11,2 hours of wall
clock in 25 days that is a good trade — but it is a trade, not a free win.

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


# The long window, for loops whose period the short one cannot span. Measured
# in the module head. The window is ten times the short one and the interval
# five times, because the score of the stored cases is the same at 6 checks as
# at 27, and a check here shingles 20.000 pieces instead of 2.000 (~3 ms against
# ~0,2 ms).
LONG_WINDOW_CHARS = 200_000
LONG_CHECK_EVERY_CHARS = 50_000


def build_detectors(
    *, enabled: bool = True, repetition_threshold: float = 0.5,
) -> list[ReasoningLoopDetector]:
    """The detectors ONE call is watched with — feed every delta to each.

    Two windows, not one bigger one: the short window is the calibrated
    instrument and catches short-period loops, and a longer window would stop
    looking at exactly those, because a window only judges once it is full.
    The long one therefore only ever sees what the short one let through.

    The geometry stays here rather than in ``ReasoningLoopConfig`` for the
    reason that config gives itself: window, interval, piece length and stride
    were calibrated together with the threshold, and a knob on one of them
    quietly changes what the threshold means.
    """
    return [
        ReasoningLoopDetector(
            enabled=enabled, repetition_threshold=repetition_threshold),
        ReasoningLoopDetector(
            enabled=enabled, repetition_threshold=repetition_threshold,
            window_chars=LONG_WINDOW_CHARS,
            check_every_chars=LONG_CHECK_EVERY_CHARS),
    ]
