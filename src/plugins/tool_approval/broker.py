"""The questions waiting for a person, and what a person allowed for a session.

One broker per plugin instance, in the process that runs the agents: the hook
opens a question and waits on it, the web endpoint answers it. Nothing is
persisted -- a restart ends every run that waits, and forgets what was allowed
for a session.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

ALLOW_ONCE = "allow_once"
ALLOW_SESSION = "allow_session"
DENY = "deny"
DECISIONS = (ALLOW_ONCE, ALLOW_SESSION, DENY)

#: Longest reason a person can hand the model, in characters.
MAX_REASON_CHARS = 1000

#: Sessions whose grants are kept; the oldest one is forgotten beyond this.
MAX_SESSIONS_WITH_GRANTS = 1000

#: A session: (owner, session id). A grant is the owner's, in that session only.
Session = Tuple[Optional[str], str]


@dataclass(frozen=True)
class Answer:
    """What a person decided about one call."""

    decision: str
    reason: str = ""
    answered_by: Optional[str] = None


@dataclass
class Question:
    """One call waiting for a person."""

    id: str
    owner: Optional[str]
    session_id: str
    request_id: str
    agent_name: str
    tool: str
    server: str
    arguments_preview: str
    asked_at: float
    timeout: float
    answer: "asyncio.Future[Answer]" = field(repr=False)
    #: The preview left out part of a long value (never a whole argument).
    arguments_cut: bool = False
    #: Said above the buttons: what allowing this call gives up (a spawn without approvals).
    warning: Optional[str] = None
    #: The answers offered; a script is never allowed for the session.
    decisions: Tuple[str, ...] = DECISIONS

    def to_public(self) -> Dict[str, Any]:
        """What the page shows about the question."""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "agent": self.agent_name,
            "tool": self.tool,
            "server": self.server,
            "arguments": self.arguments_preview,
            "arguments_cut": self.arguments_cut,
            "warning": self.warning,
            "decisions": list(self.decisions),
            "asked_at": self.asked_at,
            "expires_at": self.asked_at + self.timeout,
        }


def _resolve(future: "asyncio.Future[Answer]", answer: Answer) -> None:
    if not future.done():
        future.set_result(answer)


class AnswerRejected(Exception):
    """The answer cannot be taken; ``status`` is the HTTP status that says why."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class ApprovalBroker:
    """Open questions by id, and the tools a person allowed per session."""

    def __init__(self) -> None:
        self._questions: Dict[str, Question] = {}
        self._grants: "OrderedDict[Session, Set[str]]" = OrderedDict()

    # --- questions ---------------------------------------------------------

    def open(self, *, owner: Optional[str], session_id: str, request_id: str, agent_name: str,
             tool: str, server: str, arguments_preview: str, timeout: float,
             arguments_cut: bool = False, warning: Optional[str] = None,
             decisions: Tuple[str, ...] = DECISIONS) -> Question:
        """A new question whose answer the caller awaits on ``question.answer``."""
        question = Question(
            # hex: the id ends up in a status line's request id, where a `_` or a
            # trailing `_nnn` would read as a level of the run tree
            id=secrets.token_hex(8), owner=owner, session_id=session_id,
            request_id=request_id, agent_name=agent_name, tool=tool, server=server,
            arguments_preview=arguments_preview, asked_at=time.time(), timeout=timeout,
            answer=asyncio.get_running_loop().create_future(), arguments_cut=arguments_cut,
            warning=warning, decisions=tuple(decisions))
        self._questions[question.id] = question
        return question

    def close(self, question: Question) -> None:
        """The asker stopped waiting: the question can no longer be answered."""
        self._questions.pop(question.id, None)
        if not question.answer.done():
            question.answer.cancel()

    def get(self, question_id: str) -> Optional[Question]:
        return self._questions.get(question_id)

    def pending(self) -> List[Question]:
        """Every open question, oldest first."""
        return sorted(self._questions.values(), key=lambda q: q.asked_at)

    def answer(self, question_id: str, decision: str, reason: str = "",
               answered_by: Optional[str] = None) -> Question:
        """Hand the waiting hook a person's decision. Who may answer is the
        caller's check (it knows the request); this one checks the answer."""
        if decision not in DECISIONS:
            raise AnswerRejected(422, f"decision must be one of {', '.join(DECISIONS)}")
        if not isinstance(reason, str):
            raise AnswerRejected(422, "reason must be text")
        question = self._questions.get(question_id)
        if question is not None and decision not in question.decisions:
            raise AnswerRejected(422, f"this question takes {', '.join(question.decisions)}")
        # Taken out first: of two answers at once, one finds it and the other does not.
        question = self._questions.pop(question_id, None)
        if question is None or question.answer.done():
            raise AnswerRejected(404, "No such question is waiting -- it was answered, timed out or its run ended.")
        answer = Answer(decision=decision, reason=reason.strip()[:MAX_REASON_CHARS], answered_by=answered_by)
        loop = question.answer.get_loop()
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        if here is loop:
            question.answer.set_result(answer)
        else:
            # The app serves routes on the loop the runs run on; a server that
            # does not must not touch the future from another thread.
            loop.call_soon_threadsafe(_resolve, question.answer, answer)
        return question

    # --- what was allowed for a session ------------------------------------

    def grant(self, sessions: Sequence[Session], tool_key: str) -> None:
        """Allow ``tool_key`` in every one of ``sessions`` (the asking levels of
        the chain the question was asked for), for the rest of each."""
        for session in sessions:
            if not session[1]:
                continue
            tools = self._grants.pop(session, set())
            tools.add(tool_key)
            self._grants[session] = tools
        while len(self._grants) > MAX_SESSIONS_WITH_GRANTS:
            self._grants.popitem(last=False)

    def granted(self, sessions: Sequence[Session], tool_key: str) -> bool:
        """Whether ``tool_key`` was allowed in every one of ``sessions``."""
        return bool(sessions) and all(
            session[1] and tool_key in self._grants.get(session, ()) for session in sessions)
