"""A loaded, valid machine tree compiled into the structure the interpreter walks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from plugins.stategraph.kinds import ActivityKind, KindSpec, parse_activity
from plugins.stategraph.model.code import Namespace
from plugins.stategraph.model.loader import MachineTree
from plugins.stategraph.model.spec import MachineSpec, StateSpec, TransitionSpec


class CompileError(Exception):
    """The tree has errors; the engine refuses to run it."""


@dataclass(eq=False)
class Node:
    """One state or pseudostate."""

    name: str
    spec: StateSpec
    parent: Optional["Node"]
    depth: int
    children: dict[str, "Node"] = field(default_factory=dict)
    kind: Optional[ActivityKind] = None
    activity: Optional[KindSpec] = None
    activity_raw: Optional[dict[str, Any]] = None

    @property
    def type(self) -> str:
        return self.spec.type

    @property
    def transitions(self) -> list[TransitionSpec]:
        return self.spec.transitions

    @property
    def composite(self) -> bool:
        return bool(self.children)

    @property
    def is_final(self) -> bool:
        return self.spec.type == "final"

    @property
    def is_pseudo(self) -> bool:
        return self.spec.type in ("choice", "junction")

    def chain(self) -> list["Node"]:
        """Root-most ancestor first, this node last."""
        nodes: list[Node] = []
        node: Optional[Node] = self
        while node is not None:
            nodes.append(node)
            node = node.parent
        return list(reversed(nodes))

    def descendants(self) -> list["Node"]:
        found: list[Node] = []
        for child in self.children.values():
            found.append(child)
            found.extend(child.descendants())
        return found

    def is_descendant_of(self, other: "Node") -> bool:
        node = self.parent
        while node is not None:
            if node is other:
                return True
            node = node.parent
        return False

    def __repr__(self) -> str:
        return f"<Node {self.name}>"


def lca(a: Optional[Node], b: Optional[Node]) -> Optional[Node]:
    """Deepest node that is a PROPER ancestor of both (None = the machine itself).

    Proper on both sides gives UML external-transition semantics: a self-transition
    exits and re-enters its state, a transition to an enclosing composite exits
    and re-enters that composite.
    """
    if a is None or b is None:
        return None
    ancestors_a = a.chain()[:-1]
    ancestors_b = set(id(n) for n in b.chain()[:-1])
    found: Optional[Node] = None
    for node in ancestors_a:
        if id(node) in ancestors_b:
            found = node
    return found


@dataclass
class Machine:
    """One compiled machine file."""

    path: str
    spec: MachineSpec
    namespace: Namespace
    nodes: dict[str, Node]
    top: dict[str, Node]
    imports: dict[str, "Machine"]

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def initial(self) -> Node:
        return self.top[self.spec.initial]


def compile_tree(tree: MachineTree) -> Machine:
    if not tree.ok:
        errors = [p for p in tree.problems if p.level == "error"]
        raise CompileError("; ".join(f"{p.file}:{p.line or '?'} {p.path} {p.message}" for p in errors[:5]))
    compiled: dict[str, Machine] = {}

    def build(path: str) -> Machine:
        if path in compiled:
            return compiled[path]
        loaded = tree.files[path]
        spec = loaded.spec
        assert spec is not None  # tree.ok
        nodes: dict[str, Node] = {}

        def make(states: dict[str, StateSpec], parent: Optional[Node], depth: int) -> dict[str, Node]:
            made: dict[str, Node] = {}
            for name, state in states.items():
                node = Node(name=name, spec=state, parent=parent, depth=depth)
                if state.do is not None:
                    node.kind, node.activity = parse_activity(state.do)
                    node.activity_raw = state.do
                nodes[name] = node
                made[name] = node
                if state.states:
                    node.children = make(state.states, node, depth + 1)
            return made

        top = make(spec.states, None, 0)
        machine = Machine(path=path, spec=spec, namespace=loaded.namespace, nodes=nodes, top=top, imports={})
        compiled[path] = machine
        for alias, target in loaded.imports.items():
            machine.imports[alias] = build(target)
        return machine

    return build(tree.root)
