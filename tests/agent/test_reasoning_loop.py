"""What the reasoning loop detector must and must not fire on.

The numbers here are not invented: they come from 4.916 stored runs in the
debugger DB (worst healthy window 0.19, the two stuck runs 0.90 and 0.95).
The texts imitate what those runs actually contained.
"""

from agent_system.servers.agent.reasoning_loop import ReasoningLoopDetector


# The real thing, from run #943509: the model counted compounds forever.
LOOP_LINE = ('Need perhaps "Bettkante" yes.\n\nNeed perhaps "Heimweg" yes.\n\n'
             'Need perhaps "Stadtrand" yes.\n\n')


def healthy(chars: int, seed: int = 0) -> str:
    """Thinking that moves forward: every line says something new."""
    out = []
    index = seed
    while sum(len(line) for line in out) < chars:
        out.append(
            f"Step {index}: scene {index % 37} needs a check of the "
            f"{'timeline' if index % 3 else 'wording'}, because beat B{index % 91:02d} "
            f"promises {index * 7 % 1000} words and the draft holds "
            f"{index * 13 % 1000}.\n")
        index += 1
    return "".join(out)


def cycling(words: int, repeats: int) -> str:
    """A loop that CYCLES: a recurring frame with changing content.

    The shape of the harder real case (#943507), where the model walked a list
    of compounds over and over instead of repeating a single sentence.
    """
    vocabulary = ["Zeitpunkt", "Putzzeug", "Heimweg", "Stadtrand", "Augenblick",
                  "Stoffbeuteln", "Gesichtsausdruck", "Hausflur", "Putzwasser",
                  "Bettkante", "Tuerrahmen", "Fensterbrett", "Kuechentisch",
                  "Schulranzen", "Gartenzaun", "Dachfenster", "Treppenhaus",
                  "Wohnzimmer", "Nachttisch", "Fahrradweg"]
    chosen = (vocabulary * 10)[:words]
    return "".join(
        f'Need perhaps "{word}" replacement "{word[:4]}-{word[4:]}" '
        f'with {word[0]}. Good.\n\n'
        for _ in range(repeats) for word in chosen)


def long_period_loop(unit_chars: int = 503, repeats: int = 60) -> str:
    """A loop whose repeating unit is long and shares no factor with the
    stride — the case that decides the geometry.

    The unit itself is varied prose, so it has no periodicity of its own: the
    only repetition in this text is the loop.
    """
    words = ["Szene", "Absatz", "Figur", "Motiv", "Wendung", "Kapitel",
             "Dialog", "Bild", "Satz", "Klang"]
    unit, index = [], 0
    while sum(len(part) for part in unit) < unit_chars:
        unit.append(f"{words[index % len(words)]} {index}: geprueft, "
                    f"Ergebnis {index * 17 % 997}. ")
        index += 1
    return "".join(unit)[:unit_chars] * repeats


def feed(detector: ReasoningLoopDetector, text: str, chunk: int = 400):
    """Hand the text over the way the gateway does — in small deltas."""
    reasons = []
    for start in range(0, len(text), chunk):
        reason = detector.record(text[start:start + chunk])
        if reason:
            reasons.append(reason)
    return reasons


class TestItCatchesAStuckModel:

    def test_a_repeating_window_is_reported(self):
        detector = ReasoningLoopDetector()
        reasons = feed(detector, LOOP_LINE * 900)
        assert reasons, "a model repeating one line forever was not noticed"
        assert "repeats" in reasons[0]

    def test_a_loop_that_starts_late_is_still_caught(self):
        """THE case the measurement turned up.

        Measured over the whole text so far, run #943509 scored 0.00 up to
        100.000 characters — the healthy first half dilutes the loop that
        follows. A detector that judges everything it has seen instead of a
        window is blind to exactly the runs it exists for.
        """
        detector = ReasoningLoopDetector()
        reasons = feed(detector, healthy(100_000) + LOOP_LINE * 900)
        assert reasons, "a loop after a healthy start went unnoticed"

    def test_a_cycling_loop_is_caught_too(self):
        """Not every stuck model repeats ONE sentence. The harder real case
        walked a list of compounds in circles — same frame, changing content,
        forever."""
        detector = ReasoningLoopDetector()
        assert feed(detector, cycling(20, 120)), "a cycling loop went unnoticed"

    def test_it_reports_once_and_then_stays_quiet(self):
        """The caller acts on the first report; further deltas are noise."""
        detector = ReasoningLoopDetector()
        reasons = feed(detector, LOOP_LINE * 2000)
        assert len(reasons) == 1


class TestItLeavesHealthyThinkingAlone:

    def test_a_long_healthy_run_is_never_reported(self):
        """The expensive runs in the DB (37 min, 85k reasoning tokens) are
        healthy. A guard that kills those costs more than it saves."""
        detector = ReasoningLoopDetector()
        assert feed(detector, healthy(250_000)) == []

    def test_nothing_is_judged_before_a_full_window(self):
        """Early thinking is where healthy runs look most repetitive, so a
        short window is exactly where a false alarm would come from."""
        detector = ReasoningLoopDetector()
        text = LOOP_LINE * 900
        assert feed(detector, text[:19_000]) == []

    def test_a_healthy_run_with_repeated_phrasing_stays_below_the_line(self):
        """Recurring wording is not a loop — the same sentence FRAME with
        different content is how checklists think."""
        detector = ReasoningLoopDetector()
        text = "".join(
            f'Need perhaps "{word}{index}" yes.\n\n'
            for index, word in enumerate(["Bettkante", "Heimweg", "Stadtrand"] * 3000))
        assert feed(detector, text) == []


class TestWhenItIsOff:

    def test_disabled_never_reports(self):
        detector = ReasoningLoopDetector(enabled=False)
        assert feed(detector, LOOP_LINE * 900) == []

    def test_an_impossible_threshold_counts_as_off(self):
        """A threshold outside (0, 1] can never fire. Saying "off" beats a
        detector that looks armed and measures nothing."""
        for threshold in (0.0, -1.0, 1.5):
            detector = ReasoningLoopDetector(repetition_threshold=threshold)
            assert not detector.enabled, f"threshold {threshold} pretended to be armed"

    def test_a_window_too_small_to_compare_counts_as_off(self):
        detector = ReasoningLoopDetector(window_chars=100, shingle_chars=60)
        assert not detector.enabled
        assert feed(detector, LOOP_LINE * 900) == []

    def test_the_threshold_decides(self):
        """Same text, two thresholds: the configured number is what acts."""
        text = healthy(60_000, seed=5) + LOOP_LINE * 400
        assert feed(ReasoningLoopDetector(repetition_threshold=0.5), text)
        assert feed(ReasoningLoopDetector(repetition_threshold=0.99), text) == []


class TestTheMeasureStaysCalibrated:
    """The threshold only means something while the geometry holds.

    Pieces are only taken at multiples of the stride, so the number of
    distinct ones is ``period / gcd(period, stride)``: a stride sharing no
    factor with the loop's period is blind to it. That makes the stride a
    calibrated value, and this is what holds it.

    A regular fixture cannot do that job — with a period the stride divides,
    the score is identical for every stride (the first version of this test
    used one, changed the geometry, and stayed green). So the witness here is
    a LONG, coprime period, which is exactly the case the measurement flagged
    as the weakness of the wider stride.
    """

    def test_a_long_coprime_loop_is_seen_by_the_calibrated_stride(self):
        detector = ReasoningLoopDetector()
        score = detector._repetition(long_period_loop()[:20_000])
        # Measured: stride 10 sees this at ~0.75, the wider stride 30 at 0.24
        # — i.e. below every usable threshold, a missed runaway.
        assert score >= 0.60, f"a 503-character loop scored only {score:.2f}"

    # Deleted: a second check on ``cycling(20, 120)`` claimed to pin the
    # geometry and did not. Measured, it scores 0.932 at strides 1, 2, 3, 5,
    # 10, 15, 30 and 50 and at every shingle length from 20 to 200 — its
    # period is divisible by all of them, so the ratio cancels out. It was
    # exactly the trap this class's docstring warns about, and the test above
    # already holds the stride with a witness that cannot cancel.

    def test_healthy_thinking_scores_near_zero(self):
        """The other end of the calibration — without it, a measure that
        returns a high number for everything would pass the test above."""
        detector = ReasoningLoopDetector()
        assert detector._repetition(healthy(20_000)) <= 0.15


class TestBookkeeping:

    def test_it_counts_what_it_saw(self):
        detector = ReasoningLoopDetector()
        feed(detector, healthy(30_000))
        assert detector.characters_seen >= 30_000 - 400

    def test_empty_deltas_are_ignored(self):
        detector = ReasoningLoopDetector()
        assert detector.record("") is None
        assert detector.characters_seen == 0
