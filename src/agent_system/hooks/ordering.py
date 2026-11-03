"""
Hook execution order: resolve ``order.before`` / ``order.after`` references
(full hook names, categories, the virtual ``begin`` / ``end`` nodes) and sort
the hooks of one type topologically.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Set

from .exceptions import CircularDependencyError
from .plugin_hook import PluginHook

logger = logging.getLogger(__name__)

BEGIN = "begin"
END = "end"
_VIRTUAL = (BEGIN, END)

HookEntry = tuple[str, PluginHook, Dict[str, List[str]], Dict[str, Any]]


def _reachable(graph: Dict[str, Set[str]], start: str) -> Set[str]:
    """Every node reachable from ``start`` along ``graph`` (``start`` excluded)."""
    seen: Set[str] = set()
    stack = list(graph.get(start, ()))
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(graph.get(node, ()))
    return seen


def _reverse(graph: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    reverse: Dict[str, Set[str]] = defaultdict(set)
    for first, thens in graph.items():
        for then in thens:
            reverse[then].add(first)
    return reverse


def topological_sort(
    hooks_list: Iterable[HookEntry],
) -> List[tuple[str, PluginHook, Dict[str, Any]]]:
    """
    Sort hooks by dependency order using topological sort.

    ``begin`` and ``end`` anchor the chain: every hook runs after ``begin``
    and before ``end`` unless its order says otherwise -- a hook with
    ``before: [begin]`` (or ordered before such a hook) runs ahead of all
    others, one with ``after: [end]`` (or ordered after such a hook) behind
    all others. Ties are broken alphabetically by full name.

    Args:
        hooks_list: List of (name, hook, order_spec, metadata) tuples

    Returns:
        List of (name, hook, metadata) tuples in execution order

    Raises:
        CircularDependencyError: If circular dependencies detected
    """
    hooks_list = list(hooks_list)
    hooks_map = {name: hook for name, hook, _, _ in hooks_list}
    order_specs = {name: spec for name, _, spec, _ in hooks_list}
    metadata_map = {name: meta for name, _, _, meta in hooks_list}

    # Build category map: category -> set of hook names
    category_map: Dict[str, Set[str]] = defaultdict(set)
    for hook_name, meta in metadata_map.items():
        category = meta.get("category")
        if category:
            category_map[category].add(hook_name)

    # Adjacency list (hook -> hooks that must come after it)
    graph: Dict[str, Set[str]] = defaultdict(set)

    def resolve_reference(ref: str) -> List[str]:
        """Resolve a reference to hook name(s). Can be hook name, category, or virtual node."""
        if ref in hooks_map or ref in _VIRTUAL:
            return [ref]
        if ref in category_map:
            return list(category_map[ref])
        return []

    # Process ordering specifications to build the dependency graph.
    # Edge A->B means "A must execute before B" (B depends on A).
    for hook_name, order_spec in order_specs.items():
        # "after: [A]" -> A -> hook; "before: [B]" -> hook -> B.
        for clause, opposite in (("after", "before"), ("before", "after")):
            for reference in order_spec.get(clause, []):
                ref_key = str(reference)
                resolved_refs = resolve_reference(ref_key)

                if not resolved_refs:
                    # Debug level - it's normal for referenced hooks/categories to be disabled
                    logger.debug(
                        f"Hook '{hook_name}' references inactive hook/category '{ref_key}' in '{clause}' clause. "
                        f"Dependency ignored (hook may be disabled)."
                    )
                    continue

                for resolved in resolved_refs:
                    if clause == "after":
                        first, then = resolved, hook_name
                    else:
                        first, then = hook_name, resolved
                    if first in graph.get(then, ()):
                        logger.warning(
                            f"Hook ordering conflict: '{hook_name}' wants to run {clause} '{resolved}', "
                            f"but '{hook_name}' is already scheduled {opposite} '{resolved}'. "
                            f"This may cause circular dependencies."
                        )
                    graph[first].add(then)

    # Anchor every hook between the virtual nodes, except those the explicit
    # order places ahead of "begin" or behind "end".
    ahead_of_begin = _reachable(_reverse(graph), BEGIN)
    behind_end = _reachable(graph, END)
    for hook_name in hooks_map:
        if hook_name not in ahead_of_begin:
            graph[BEGIN].add(hook_name)
        if hook_name not in behind_end:
            graph[hook_name].add(END)

    in_degree: Dict[str, int] = {node: 0 for node in (*hooks_map, *_VIRTUAL)}
    for thens in graph.values():
        for then in thens:
            in_degree[then] += 1

    # Kahn's algorithm; the queue is sorted for deterministic ties
    queue = [node for node, degree in in_degree.items() if degree == 0]
    result = []
    processed_count = 0

    while queue:
        queue.sort()
        current = queue.pop(0)
        processed_count += 1

        # Skip virtual nodes in output, but include real hooks
        if current in hooks_map:
            result.append(current)

        for successor in graph.get(current, ()):
            in_degree[successor] -= 1
            if in_degree[successor] == 0:
                queue.append(successor)

    # If not every node (including the virtual ones) was processed, there's a cycle
    if processed_count != len(in_degree):
        remaining = set(hooks_map) - set(result)
        raise CircularDependencyError(
            f"Circular dependency detected in hooks: {sorted(remaining)}"
        )

    return [(name, hooks_map[name], metadata_map[name]) for name in result]
