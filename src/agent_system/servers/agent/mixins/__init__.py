"""The responsibilities of the Agent class, one mixin per module (servers/agent/server.py).

Agent was one class of 4800 lines. Each module here holds one of its responsibilities; Agent
inherits them all and keeps its constructor, the config reload and the ToolServer interface.

Mixins, not components (components/): every method here is part of the class's contract --
subclasses override them, plugins call private ones, tests patch them on the class or on the
instance -- and they all work on the instance state Agent.__init__ sets up. A component owns its
state behind an API of its own; these keep theirs on the agent, and their names stay where every
subclass and caller finds them, on Agent.

A mixin never imports server.py at run time (server.py imports the mixins). Its methods annotate
``self`` as Agent for the type checker only (TYPE_CHECKING), so a call into another mixin is
checked against the real class; each mixin's docstring names the attributes of Agent.__init__ it
relies on. What several of them share lives outside: the refusals' error_types in
servers/agent/refusals.py, ConversationContext in run_phases.py.
"""
