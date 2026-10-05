# stategraph_machine

One [stategraph](../stategraph/README.md) machine, addressed as an agent: a SAM spawns it, the chat lists it, another
agent calls it by name. Each request starts one run of the machine through a `stategraph` instance -- the State Graph
panel shows and controls it -- and the answer is the run's output as a JSON object. The agent runs no LLM itself.

- **No tools, no hooks, no panel** of its own; the State Graph panel's inspector shows which agents run a machine.
- A retry of the same request attaches to its run, resumes it, or answers its outcome again; a cancel terminates the
  run (its `finally` activities run).
- `on_wait: ask` turns a wait state into a question in the conversation; `block` waits for the event from elsewhere.

Enable it with an `agent:` block in the machine's file (`agent: {visibility: tool}`, declared when a process starts),
or with a `type: stategraph_machine` entry naming `machine:` in `config/plugins.yaml`. Without `visibility` it is
private: only callers that name it reach it.

The full manual -- the block's and the entry's keys with their defaults, what a request does, the question and its
answer, every error -- is the plugin's guide, `stategraph_machine.guide`, in the Help panel.
