"""The questions a run puts to the person at ``agent-cli chat``: drawn from the
form every client draws (``core.run_questions.Question.form``), answered with
the next line typed while one is open, in the form every client sends.

Pure but for ``TurnQuestions``, which the chat turn owns: it sees the turn's
status events, shows each question once (the asker sends its line again every
few seconds), and hands a typed line to ``answer_question`` in this process --
the person at the terminal started the run, so they may answer it.
"""
from __future__ import annotations

import re
import string
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.run_questions import AnswerRejected, answer_question, waiting_question

#: Numbers alone, several with commas or spaces.
_NUMBERS = re.compile(r"\d+(?:(?:\s*,\s*|\s+)\d+)*")
#: Numbers a line begins with: "4 too wide".
_LEADING_NUMBERS = re.compile(r"\d+(?:\s*,\s*\d+)*(?=$|[\s:])")
#: Picks, a colon (or a dash between spaces), then words: "3: too wide".
_AFTER_COLON = re.compile(r"^([0-9a-z]+(?:(?:\s*,\s*|\s+)[0-9a-z]+)*)(?:\s*:|\s+-)\s*(.*)$", re.I | re.S)
#: Picks, a space, then words: "3 too wide" -- read so only where words cannot answer alone.
_AFTER_SPACE = re.compile(r"^([0-9a-z]+(?:\s*,\s*[0-9a-z]+)*)\s+(.*)$", re.I | re.S)

#: (text, colour) -- a line as the renderer prints it.
Line = Tuple[str, Optional[str]]

#: What TurnQuestions.answer did with a line.
ANSWERED = "answered"
NOT_AN_ANSWER = "not an answer"
TOO_LATE = "too late"


class NotAnAnswer(ValueError):
    """The line does not answer the question; the text says what would."""


def asked_in(meta: Any) -> List[Dict[str, Any]]:
    """The questions a status line's ``meta`` carries: each kind under its own
    key, as ``put_to_person`` puts it -- whatever the kind, by its form."""
    if not isinstance(meta, dict):
        return []
    return [value for value in meta.values()
            if isinstance(value, dict) and isinstance(value.get("id"), str) and isinstance(value.get("form"), dict)]


def marks(form: Dict[str, Any]) -> List[str]:
    """What picks each choice: its number -- or its letter where a label begins
    with a number, which a typed number would name twice ("1) 4 ... 4) 1",
    "1) 1 day 2) 3 days 3) a week")."""
    choices = form.get("choices") or []
    if len(choices) <= 26 and any(str(c.get("label", "")).strip()[:1].isdigit() for c in choices):
        return list(string.ascii_lowercase[:len(choices)])
    return [str(n) for n in range(1, len(choices) + 1)]


def _mark_word(shown: List[str]) -> str:
    return "number" if shown[0] == "1" else "letter"


def hint(form: Dict[str, Any]) -> str:
    """How to answer, in one line."""
    choices = form.get("choices") or []
    text = form.get("text")
    if not choices:
        return "Type your answer, Enter sends it."
    shown = marks(form)
    what, span = _mark_word(shown), f"{shown[0]}-{shown[-1]}"
    pick = (f"Type the {what}s of your picks ({span}, several with commas)" if form.get("multi_select")
            else f"Type a {what} ({span})")
    if text and text.get("alone"):
        return f"{pick}, words after a colon if you like -- or answer in your own words. Enter sends it."
    if text:
        label = str(text["label"])
        return f"{pick}, and after it {label[:1].lower()}{label[1:]} if you like. Enter sends it."
    return f"{pick}. Enter sends it."


def question_lines(public: Dict[str, Any], *, sub_agent: bool = False) -> List[Line]:
    """The question as the chat prints it: what is asked, the detail as it is,
    the warning, the choices marked, how to answer."""
    form = public["form"]
    who = f" ({public.get('agent')})" if sub_agent and public.get("agent") else ""
    lines: List[Line] = [(f"? {form.get('prompt', '')}{who}", "35")]
    detail = form.get("detail")
    if detail:
        lines += [(f"    {row}", "90") for row in str(detail).splitlines()]
    warning = form.get("warning")
    if warning:
        lines += [(f"  ! {row}", "33") for row in str(warning).splitlines()]
    for mark, choice in zip(marks(form), form.get("choices") or []):
        lines.append((f"  {mark}) {choice.get('label', choice.get('value'))}", None))
    lines.append((f"  {hint(form)}", "90"))
    return lines


def _tokens(text: str) -> List[str]:
    return [token for token in re.split(r"\s*,\s*|\s+", text.strip().casefold()) if token]


def _only_marks(text: str, shown: List[str]) -> bool:
    tokens = _tokens(text)
    return bool(tokens) and all(token in shown for token in tokens)


def _picks(form: Dict[str, Any], shown: List[str], text: str) -> List[str]:
    at = list(dict.fromkeys(shown.index(token) for token in _tokens(text)))
    if len(at) > 1 and not form.get("multi_select"):
        raise NotAnAnswer(f"Pick one: {hint(form)}")
    return [form["choices"][n]["value"] for n in at]


def parse_answer(form: Dict[str, Any], line: str) -> Tuple[List[str], str]:
    """(choices, text) a typed line answers ``form`` with -- the ``value`` of
    each choice picked by its mark or its label, and what is written -- or
    NotAnAnswer saying what would answer it.

    Marks alone pick, as the hint says; a line that is a choice's own text
    picks that choice ("3 days" is the option "3 days"). Words after the marks
    go with them after a colon, and after a space only where words cannot
    answer alone: where they can, "2 days" is the person's own answer, not
    the second choice with a remark. The question's own check comes after
    this one (AnswerRejected), as for an answer from the web."""
    typed = line.strip()
    choices = form.get("choices") or []
    text = form.get("text")
    alone = bool(text and text.get("alone"))
    if not typed:
        raise NotAnAnswer(hint(form))
    if choices:
        shown = marks(form)
        if _only_marks(typed, shown):
            return _picks(form, shown, typed), ""
        named = [c["value"] for c in choices if str(c.get("label", "")).casefold() == typed.casefold()]
        if named:
            return named[:1], ""
        for split in (_AFTER_COLON.match(typed), None if alone else _AFTER_SPACE.match(typed)):
            if split and _only_marks(split.group(1), shown):
                words = split.group(2).strip()
                if words and not text:
                    raise NotAnAnswer(f"Only a {_mark_word(shown)} answers this: {hint(form)}")
                return _picks(form, shown, split.group(1)), words
        # numbers that pick nothing: a slip -- unless words follow that may answer alone ("2024 was ...")
        slip = _NUMBERS.fullmatch(typed) if alone else _LEADING_NUMBERS.match(typed)
        if slip:
            words = " A number as your own answer needs a word with it." if alone else ""
            raise NotAnAnswer(f"There is no choice {slip.group(0)}: {hint(form)}{words}")
    if text and (alone or not choices):
        return [], typed
    raise NotAnAnswer(hint(form))


class TurnQuestions:
    """The questions open in one chat turn, oldest first. Only the one the next
    line answers is shown in full -- the oldest still waiting --, a later one is
    named below it until its turn comes: two calls of one tool would otherwise
    be told apart by nothing but their order on the screen."""

    def __init__(self, run_id: str, answered_by: Optional[str], show: Callable[[str, Optional[str]], None],
                 agent: Optional[str] = None):
        self.run_id = run_id
        self.answered_by = answered_by
        self._show = show
        #: The chat's agent: a question of another one (a sub-agent) says whose it is.
        self.agent = agent
        #: The id of the question shown in full -- the one a line begun now answers --, or None.
        self.current: Optional[str] = None
        self._open: Dict[str, Dict[str, Any]] = {}
        #: Each question of the turn as it was shown, by id: a line too late for one is read against it.
        self._asked: Dict[str, Dict[str, Any]] = {}
        self._drawn: set = set()

    def _ours(self, public: Dict[str, Any]) -> bool:
        request_id = str(public.get("request_id") or "")
        return request_id == self.run_id or request_id.startswith(f"{self.run_id}_")

    def _who(self, public: Dict[str, Any]) -> bool:
        asker = public.get("agent")
        return bool(asker) and asker != self.agent

    def refresh(self) -> None:
        """Forget what no longer waits -- timed out, given up, its run ended --, say so,
        and show the question the next line answers now. The chat looks on every tick:
        a question may end while the run says nothing and nobody types."""
        for question_id in [q for q in self._open if waiting_question(q) is None]:
            public = self._open.pop(question_id)
            self._show(f"  (no longer waiting: {public['form'].get('prompt', '')})", "90")
        self._present()

    def _present(self) -> None:
        """Show the question the next line answers in full, once."""
        current = next(iter(self._open.values()), None)
        self.current = current["id"] if current is not None else None
        if current is None or current["id"] in self._drawn:
            return
        self._drawn.add(current["id"])
        for text, colour in question_lines(current, sub_agent=self._who(current)):
            self._show(text, colour)

    def see(self, event: Any) -> None:
        """A status event of the turn: a question new to it is shown, or named after the one shown."""
        self.refresh()
        for public in asked_in(getattr(event, "meta", None)):
            if public["id"] in self._open or not self._ours(public) or waiting_question(public["id"]) is None:
                continue
            self._open[public["id"]] = public
            self._asked[public["id"]] = public
            if len(self._open) > 1:
                who = f" ({public.get('agent')})" if self._who(public) else ""
                self._show(f"  … then: {public['form'].get('prompt', '')}{who}", "90")
        self._present()

    def answer(self, line: str, meant_for: Optional[str]) -> Optional[str]:
        """Answer with ``line`` the question ``meant_for`` -- the one shown when the
        line was begun (``current`` then), never one that came while it was typed:
        that one the person has not read.

        ANSWERED; NOT_AN_ANSWER when the line does not answer it (it says why,
        and the question keeps waiting); TOO_LATE when that question waits no
        more and the line is a bare mark or number ("2", "b"): it means nothing
        without its question, and answers no other; None when the line is the
        chat's -- begun before any question was shown, or words for a question
        that waits no more ("SQLite", "2: for now"): they reach the agent as a
        message."""
        self.refresh()
        if meant_for is None:
            return None
        public = self._open.get(meant_for)
        if public is None:
            gone = self._asked.get(meant_for)
            prompt = gone["form"].get("prompt", "") if gone else "the question"
            # by the line's shape, not by what it picks: sent on, a "2" reaches the run bare
            if _NUMBERS.fullmatch(line.strip()) or (gone and _only_marks(line, marks(gone["form"]))):
                self._show(f"  Not taken: {prompt} -- it no longer waits.", "33")
                return TOO_LATE
            self._show(f"  {prompt} no longer waits: your line goes to the agent as a message.", "90")
            return None
        try:
            choices, text = parse_answer(public["form"], line)
            answer_question(public["id"], choices, text, answered_by=self.answered_by)
        except NotAnAnswer as exc:
            self._show(f"  {exc}", "33")
            return NOT_AN_ANSWER
        except AnswerRejected as exc:
            self._show(f"  Not taken: {exc.detail}", "31")
            return NOT_AN_ANSWER
        self._open.pop(public["id"], None)
        # what the line was read as, not the line: "2 ..." picked a choice the person can see now
        labels = {c.get("value"): c.get("label", c.get("value")) for c in public["form"].get("choices") or []}
        said = ", ".join(str(labels.get(choice, choice)) for choice in choices)
        self._show(f"  » {said}: {text}" if said and text else f"  » {said or text}", "36")
        self._present()
        return ANSWERED
