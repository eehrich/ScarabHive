"""The approvals waiting for a person, and what a person allowed per session.

A question is ``core.run_questions``'s, with the call it asks about; this
module adds what only an approval has: the decisions, the reason for a deny,
and the tools a person allowed for a session. One broker per plugin instance,
in the process that runs the agents: the hook opens a question and waits on
it, the web endpoint answers it. Nothing is persisted -- a restart ends every
run that waits, and forgets what was allowed for a session.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Set, Tuple

from agent_system.core.run_questions import AnswerRejected, Question, QuestionBroker

__all__ = ["ALLOW_ONCE", "ALLOW_SESSION", "DENY", "DECISIONS", "Answer", "AnswerRejected",
           "ApprovalBroker", "ApprovalQuestion"]

ALLOW_ONCE = "allow_once"
ALLOW_SESSION = "allow_session"
DENY = "deny"
DECISIONS = (ALLOW_ONCE, ALLOW_SESSION, DENY)

#: Longest reason a person can hand the model, in characters.
MAX_REASON_CHARS = 1000

#: The decisions as the person reads them.
DECISION_LABELS = {ALLOW_ONCE: "Allow once", ALLOW_SESSION: "Allow for this session", DENY: "Deny"}
#: How a client may set a decision apart: the narrow allow first, the refusal as one.
DECISION_TONES = {ALLOW_ONCE: "primary", DENY: "danger"}
#: Said where the preview left out part of a long value.
CUT_NOTE = "Long values are shortened in the middle -- check what the call writes before you allow it."

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
class ApprovalQuestion(Question):
    """One call waiting for a person."""

    tool: str
    server: str
    arguments_preview: str
    #: The preview left out part of a long value (never a whole argument).
    arguments_cut: bool = False
    #: Said above the buttons: what allowing this call gives up (a spawn without approvals).
    warning: Optional[str] = None
    #: The answers offered; a script is never allowed for the session.
    decisions: Tuple[str, ...] = DECISIONS

    def to_public(self) -> Dict[str, Any]:
        """What the page shows about the question."""
        return {
            **super().to_public(),
            "tool": self.tool,
            "server": self.server,
            "arguments": self.arguments_preview,
            "arguments_cut": self.arguments_cut,
            "warning": self.warning,
            "decisions": list(self.decisions),
        }

    def form(self) -> Dict[str, Any]:
        """The call as any client draws it: its arguments, the decisions offered, a reason for Deny."""
        warnings = [note for note in (self.warning, CUT_NOTE if self.arguments_cut else None) if note]
        return {"prompt": f"Approve {self.tool}?", "detail": self.arguments_preview or None,
                "warning": "\n".join(warnings) or None,
                "choices": [{"value": decision, "label": DECISION_LABELS[decision],
                             **({"tone": DECISION_TONES[decision]} if decision in DECISION_TONES else {})}
                            for decision in self.decisions],
                "multi_select": False,
                "text": {"label": "Why not (sent to the agent with Deny)", "alone": False,
                         "max_chars": MAX_REASON_CHARS}}


class ApprovalBroker(QuestionBroker):
    """Open approvals by id, and the tools a person allowed per session."""

    def __init__(self) -> None:
        super().__init__()
        self._grants: "OrderedDict[Session, Set[str]]" = OrderedDict()

    # --- questions ---------------------------------------------------------

    def open(self, *, owner: Optional[str], session_id: str, request_id: str, agent_name: str,
             tool: str, server: str, arguments_preview: str, timeout: float,
             arguments_cut: bool = False, warning: Optional[str] = None,
             decisions: Tuple[str, ...] = DECISIONS) -> ApprovalQuestion:
        """A new question whose answer the caller awaits on ``question.answer``."""
        return self.open_question(
            ApprovalQuestion, owner=owner, session_id=session_id, request_id=request_id,
            agent_name=agent_name, timeout=timeout, tool=tool, server=server,
            arguments_preview=arguments_preview, arguments_cut=arguments_cut, warning=warning,
            decisions=tuple(decisions))

    def answer(self, question_id: str, decision: str, reason: str = "",
               answered_by: Optional[str] = None) -> Question:
        """Hand the waiting hook a person's decision. Who may answer is the
        caller's check (it knows the request); this one checks the answer."""
        if decision not in DECISIONS:
            raise AnswerRejected(422, f"decision must be one of {', '.join(DECISIONS)}")
        if not isinstance(reason, str):
            raise AnswerRejected(422, "reason must be text")
        question = self.get(question_id)
        if isinstance(question, ApprovalQuestion) and decision not in question.decisions:
            raise AnswerRejected(422, f"this question takes {', '.join(question.decisions)}")
        answer = Answer(decision=decision, reason=reason.strip()[:MAX_REASON_CHARS], answered_by=answered_by)
        return self.resolve(question_id, answer)

    def take(self, question_id: str, choices: Sequence[str], text: str,
             answered_by: Optional[str] = None) -> Question:
        """An answer in the form any client sends: one decision, and a reason for Deny."""
        if len(choices) != 1:
            raise AnswerRejected(422, f"pick one decision: {', '.join(DECISIONS)}")
        # words with an Allow are a line misread as one ("2 files left, stop"): they allow nothing
        if text.strip() and choices[0] != DENY:
            raise AnswerRejected(422, "a reason goes with Deny only")
        return self.answer(question_id, choices[0], text, answered_by=answered_by)

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
