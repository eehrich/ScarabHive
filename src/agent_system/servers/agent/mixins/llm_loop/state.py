"""The state of one run of the step loop: what lives across its steps, and what one step holds.

_execute_llm_loop was one generator whose locals carried the run -- some fifteen counters and the
run's LLM across the steps; the step's LLM, its fallback chain and its answer within one -- and its
closures read them. Its phases are methods now (loop.py drives them), and these are the locals they
share, spelled out: LoopState for the run, StepState for a step, StepEnd for how a step ended early.
Request-scoped by construction: the Agent is a singleton serving concurrent requests, so nothing of a
run may live on it.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Dict, Iterator, List, Optional

from .....llm.structured_output import ResponseFormat, supports_response_format

if TYPE_CHECKING:
    from .....core.cancellation import CancellationToken
    from .....llm.models import ChatMessage, LLMClient
    from .....tools.status import StatusScope
    from ...escalation import StuckEscalator
    from ...loop_detection import ToolCallLoopDetector
    from ..run_phases import ConversationContext


def takes_format(response_format: Optional[ResponseFormat], client: Any) -> bool:
    """Whether *client* may answer a step of a run with *response_format*. Any client, when the
    run has no format or allows the prompt fallback; else only one that puts the field on the
    wire. Asked where the run CHOOSES another model -- an escalation, a walk around a blocked
    LLM, a failover: that choice must not end the run over a format the chosen model cannot
    take while another one could."""
    return (response_format is None or response_format.prompt_fallback
            or supports_response_format(client, response_format))


class StepEnd(enum.Enum):
    """How a step ended before its last phase -- what was a ``continue`` or a ``return`` of the loop."""

    #: On to the next step (the loop's ``continue``).
    NEXT_STEP = "next_step"
    #: The run is over (the loop's ``return``); its last event is out.
    RUN = "run"


@dataclass(eq=False)
class LoopState:
    """What lives across the steps of one run: the request, the run's LLM, the loop guards."""

    # -- the request, as the run started
    context: ConversationContext
    request_id: str
    session_id: str
    status_coordinator: StatusScope
    status_worker: StatusScope
    llm_override: Optional[LLMClient]
    llm_profile_info_override: Optional[str]
    use_advanced_model: bool
    #: The run's structured output, as the schema worker made it; None for a run without one.
    response_format: Optional[ResponseFormat]
    #: The format's description as the note that carries it says it (instruction_text).
    format_note_text: Optional[str]
    # Extracted from the context at the start of the run.
    tools_schema: List[Dict[str, Any]]
    tool_name_mapping: Dict[str, str]
    max_steps: int
    main_token: CancellationToken
    #: The longest block of an LLM this agent sets; a quota or a refused key
    #: blocks this long at once.
    max_block_seconds: float
    loop_detector: ToolCallLoopDetector
    escalator: StuckEscalator
    escalate_error_streak: int
    results: Dict[str, Any]
    #: The history the steps work on. Mostly the same list as context.messages; where it is
    #: not, the phases sync the two the way the loop always did.
    messages: List[ChatMessage]

    # -- the run's LLM: a request-scoped fallback swap replaces it for the rest of the request
    active_llm: LLMClient
    #: Request-LOCAL display label: the agent's configured profile at request
    #: start, or the last in-run swap of the base -- not re-derived per step
    #: (a 5xx run keeps showing the fallback label until the request ends).
    #: A step that walks around a blocked LLM is labelled by the pick.
    display_profile_info: Optional[str]
    #: The profile a request-scoped fallback swap made this run's base
    #: (active_llm): later steps leave it out of their fallback chain, or the
    #: fallback that fails next would be retried as its own fallback.
    base_profile: Optional[str] = None

    # -- safeguards against infinite loops
    consecutive_no_tool_calls: int = 0
    consecutive_empty_responses: int = 0
    consecutive_tool_error_steps: int = 0  # steps whose tool calls ALL errored (stuck signal)
    prev_step_all_errored: bool = False    # was the IMMEDIATELY preceding step an all-error tool step?
    max_consecutive_no_tools: ClassVar[int] = 3  # Break after 3 consecutive responses without tool calls
    max_consecutive_empty: ClassVar[int] = 2     # Break after 2 consecutive empty responses
    #: Text answers cut off at the output cap in a row, each sent back with a
    #: note (see _output_cap_note) -- only where the agent opted in
    #: (agent_config.output_cap_notes): for an agent whose product is its
    #: text the cut-off text is still the reply, and a model that loops
    #: until a 120k cap must not be sent back for two more rounds of it.
    consecutive_cut_off: int = 0
    max_cut_off_notes: int = 0
    #: Structured output (response_format): a final answer that does not match is sent back
    #: once. The note that describes the format to a model without the field is looked for
    #: before every call, not remembered: a compaction may have taken it out of the history.
    format_repaired: bool = False

    def takes_format(self, client: Any) -> bool:
        """Whether *client* may answer a step of this run (see takes_format)."""
        return takes_format(self.response_format, client)

    def pending_status_events(self) -> Iterator[dict]:
        """Yield any pending status events from the per-request forwarder."""
        for event in self.context.status_forwarder.get_pending_events():
            yield event

    def base_label(self) -> str:
        """The profile name of the run's base LLM, as a fallback candidate is labelled."""
        if self.base_profile is not None:
            return self.base_profile
        if self.llm_override is not None and self.llm_profile_info_override:
            return self.llm_profile_info_override.split(":", 1)[0]
        return (self.display_profile_info or "base").split(":", 1)[0]


@dataclass(eq=False)
class StepState:
    """What one step holds: its LLM and fallback chain, the call, the answer and what it led to."""

    step: int
    #: One iteration past the budget: the call that only asks for the answer, after which no
    #: step follows.
    final_call: bool
    #: Set by the phase that ends the step early; None while the step runs on.
    end: Optional[StepEnd] = None

    # -- the step's LLM (step_llm.py)
    current_llm: Any = None
    fallback_profiles: List[str] = field(default_factory=list)
    fallback_taken: set = field(default_factory=set)
    escalated: bool = False
    #: The fallback profile a walk around a blocked LLM picked, else None.
    step_profile: Optional[str] = None
    health_seen: int = 0
    #: Clients this step already asked a second time after an error in
    #: the body -- each gets that once, see the upstream-error branch.
    #: The clients themselves, not id()s: a dropped fallback client's id
    #: can come back on the next one built.
    body_error_retried: list = field(default_factory=list)
    budget_note: Optional[ChatMessage] = None

    # -- the call and its retries (fallback.py)
    wire_format: Optional[ResponseFormat] = None
    #: WHICH client was told to try again after its thinking looped —
    #: not merely THAT one was. A fallback switch later in this step
    #: replaces current_llm, and the new model has earned no exemption:
    #: comparing the client re-arms the watchdog by itself, where a
    #: plain flag would leave an innocent model unwatched.
    reasoning_loop_llm: Any = None
    llm_out: Optional[Dict[str, Any]] = None
    pending_thinking_complete: Optional[dict] = None
    call_started: float = 0.0
    health_asked_at: float = 0.0

    # -- the answer (step.py, answer.py, tool_step.py)
    content: Any = None
    tool_calls: Any = None
    finish_reason: Optional[str] = None
    assistant_msg: Optional[ChatMessage] = None
    #: Init per step: the continuation check reads hook_metadata even
    #: when the hook block fails — without this a first-step hook failure
    #: raises NameError, and later steps would reuse the PREVIOUS step's
    #: metadata (stale continuation signal).
    hook_metadata: Dict[str, Any] = field(default_factory=dict)
    content_format: str = "text"
    pending_intervention_msg: Optional[ChatMessage] = None
    tool_messages: list = field(default_factory=list)
    tool_results: list = field(default_factory=list)

    def ended(self) -> bool:
        """Whether a phase ended the step (``end`` is set): no further phase of it runs."""
        return self.end is not None
