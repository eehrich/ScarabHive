"""What the model asks, and what counts as an answer to it.

A question is ``core.run_questions``'s with the model's text and options; the
broker adds the check an answer has to pass before the waiting call takes it.
Both the tool (arguments) and the route (answers) are checked here, because
the framework checks neither against the schema.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent_system.core.run_questions import AnswerRejected, Question, QuestionBroker

#: Longest question, in characters: a question, not a document.
MAX_QUESTION_CHARS = 2000
#: Longest option, in characters: a button's label.
MAX_OPTION_CHARS = 200
MIN_OPTIONS = 2
MAX_OPTIONS = 4
#: Longest answer a person can type, in characters.
MAX_TEXT_CHARS = 4000


class ArgumentError(ValueError):
    """The model's arguments do not make a question; the text says how to fix them."""


@dataclass(frozen=True)
class UserAnswer:
    """What the person answered: the options picked, and what they typed."""

    choices: Tuple[str, ...]
    text: str
    answered_by: Optional[str] = None


@dataclass
class UserQuestion(Question):
    """The model's question to the person watching the run."""

    question: str
    options: Tuple[str, ...] = ()
    multi_select: bool = False

    def to_public(self) -> Dict[str, Any]:
        """What the page shows about the question."""
        return {**super().to_public(), "question": self.question, "options": list(self.options),
                "multi_select": self.multi_select}

    def form(self) -> Dict[str, Any]:
        """The question as any client draws it: the options, and an answer in one's own words."""
        return {"prompt": self.question, "detail": None, "warning": None,
                "choices": [{"value": option, "label": option} for option in self.options],
                "multi_select": self.multi_select,
                "text": {"label": "Or answer in your own words" if self.options else "Your answer", "alone": True}}


def parse_arguments(params: Dict[str, Any]) -> Tuple[str, Tuple[str, ...], bool]:
    """(question, options, multi_select) from the model's arguments, or an
    ArgumentError that says what to send instead."""
    question = params.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ArgumentError("question is required: the question to the user, as text.")
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        raise ArgumentError(f"question is {len(question)} characters long; keep it under "
                            f"{MAX_QUESTION_CHARS} -- ask one thing, briefly.")
    raw_options = params.get("options")
    if raw_options is None:
        raw_options = []
    if not isinstance(raw_options, list) or not all(isinstance(o, str) for o in raw_options):
        raise ArgumentError("options must be a list of short texts (2 to 4), or left out.")
    options = tuple(o.strip() for o in raw_options)
    if options and not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        raise ArgumentError(f"options has {len(options)} entries; give {MIN_OPTIONS} to {MAX_OPTIONS}, "
                            "or leave options out and let the user answer in their own words.")
    if any(not o for o in options):
        raise ArgumentError("an option is empty; every option needs a text.")
    if any(len(o) > MAX_OPTION_CHARS for o in options):
        raise ArgumentError(f"an option is longer than {MAX_OPTION_CHARS} characters; options are "
                            "short labels -- put the explanation in the question.")
    if len({o.casefold() for o in options}) != len(options):
        raise ArgumentError("two options say the same; every option must be different.")
    multi_select = params.get("multi_select", False)
    if multi_select is None:
        multi_select = False
    if not isinstance(multi_select, bool):
        raise ArgumentError("multi_select must be true or false.")
    if multi_select and not options:
        raise ArgumentError("multi_select needs options to pick from; give 2 to 4 options, "
                            "or leave multi_select out.")
    return question, options, multi_select


def check_answer(question: UserQuestion, choices: Sequence[Any], text: Any) -> Tuple[Tuple[str, ...], str]:
    """(choices, text) as the waiting call takes them, or AnswerRejected (422)."""
    if not isinstance(text, str):
        raise AnswerRejected(422, "text must be text")
    if not isinstance(choices, (list, tuple)) or not all(isinstance(c, str) for c in choices):
        raise AnswerRejected(422, "choices must be a list of the question's options")
    text = text.strip()
    if len(text) > MAX_TEXT_CHARS:
        raise AnswerRejected(422, f"the answer is longer than {MAX_TEXT_CHARS} characters")
    picked: List[str] = []
    for choice in choices:
        if choice not in question.options:
            raise AnswerRejected(422, f"{choice!r} is not one of the question's options")
        if choice not in picked:
            picked.append(choice)
    if len(picked) > 1 and not question.multi_select:
        raise AnswerRejected(422, "this question takes one option")
    if not picked and not text:
        raise AnswerRejected(422, "pick an option or write an answer")
    # In the order the question lists them: the model reads what it offered.
    return tuple(o for o in question.options if o in picked), text


class AskUserBroker(QuestionBroker):
    """The model's open questions by id."""

    def answer(self, question_id: str, choices: Sequence[Any], text: Any,
               answered_by: Optional[str] = None) -> Question:
        """Hand the waiting call a person's answer. Who may answer is the
        caller's check (it knows the request); this one checks the answer."""
        question = self.get(question_id)
        if isinstance(question, UserQuestion):
            picked, typed = check_answer(question, choices, text)
        else:
            picked, typed = (), ""   # nothing waits under this id: resolve says so
        return self.resolve(question_id, UserAnswer(choices=picked, text=typed, answered_by=answered_by))

    def take(self, question_id: str, choices: Sequence[str], text: str,
             answered_by: Optional[str] = None) -> Question:
        """An answer in the form any client sends: the options picked and the words written."""
        return self.answer(question_id, list(choices), text, answered_by=answered_by)
