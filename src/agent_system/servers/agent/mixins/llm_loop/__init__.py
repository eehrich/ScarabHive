"""Phase 2 of a run: the step loop (_execute_llm_loop) and the LLM call of one step.

_execute_llm_loop runs the steps of a request: drains appended messages, chooses the step's LLM
(fallbacks, blocks, escalation), calls it (_call_llm_with_streaming, streaming or polled), runs the
hooks around the call and the tool calls the answer asks for, and ends with the final answer, an
error or the max-steps end. run.py drives it between Phase 1 and Phase 3 (run_phases.py).

It was one generator of 1600 lines, its state in some thirty locals and the closures that read
them. Here it is a sequence of phases, one module per part of a step, each a mixin that LLMLoopMixin
gathers:

- loop.py: _execute_llm_loop -- the run's setup and the steps -- and _run_step, the order of a
  step's phases
- state.py: LoopState (what lives across the steps), StepState (one step), StepEnd
- step.py: the phases around the call -- open the step, the pre- and post-LLM hooks, the answer
  recorded and shown, an empty answer, the end of the step; the events a cancel ends the run with
- step_llm.py: which LLM answers the step, and the fallback a failed call moves to
- fallback.py: the call with its retries -- what each error means, which LLM is blocked
- llm_call.py: the LLM call itself, _call_llm_with_streaming, streaming or polled
- answer.py: an answer without tool calls -- continue, or the final answer
- tool_step.py: an answer with tool calls -- loop detection, the tools, the history after them

Mixins like the rest of mixins/: the phases work on the agent (its hook, session and tool
managers, _step_llms) and call the other mixins. What belongs to the request lives on the state
objects, never on the agent, which serves concurrent requests.
"""
from .loop import LLMLoopMixin

__all__ = ["LLMLoopMixin"]
