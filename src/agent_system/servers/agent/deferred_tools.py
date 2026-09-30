"""Deferred tools: an agent's rarely used tools reach the model as a name and one
line, and their full schema joins the tool list only once the model asks for it.

Configured per agent under ``tools.deferred``, with the patterns of
``tools.allowed``, matched by the same ``tool_matches_patterns``. A deferred tool
stays allowed and dispatchable -- ``available_tools`` is untouched; only its
schema is held back. The model loads it with the core tool ``tool_search``, or by
calling it anyway: that call is not run, the schema is loaded, and the model is
told to send the call again with the parameters now in its tool list.

Loaded schemas are APPENDED to the run's tool list, so a load invalidates the
cached prefix from the tool list on, once. A new run of the same session loads
again what its history already loaded (``restore``), so it starts with the tool
list the previous run ended with instead of paying the load a second time.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional

from .tool_schema_builder import tool_matches_patterns

logger = logging.getLogger(__name__)

TOOL_SEARCH = "tool_search"
#: The ``type`` of the answer to a deferred tool called before it was loaded:
#: a call that never ran (tool_execution.tool_message_never_ran).
NOT_LOADED_TYPE = "ToolNotLoaded"
MAX_RESULTS = 5

_warned: set[tuple[str, str]] = set()  # (agent, pattern) already reported as matching nothing


def _name(schema: Dict[str, Any]) -> Optional[str]:
    function = schema.get("function")
    return function.get("name") if isinstance(function, dict) else None


def _summary(description: str) -> str:
    """The first sentence, at most 120 characters: what the index shows."""
    text = " ".join((description or "").split())
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return first if len(first) <= 120 else first[:117] + "..."


class DeferredTools:
    """The deferred schemas of one run, and which of them are still unloaded."""

    def __init__(self, schemas: Dict[str, Dict[str, Any]], servers: Dict[str, str]):
        self._schemas = schemas  # every deferred tool; the index never changes in a run
        self._servers = servers
        self.pending = set(schemas)
        self._refused: Dict[str, int] = {}  # tool -> step its unloaded call was refused in

    @classmethod
    def split(cls, tools_schema: List[Dict[str, Any]], tool_name_mapping: Dict[str, str],
              patterns: Optional[List[str]], agent_name: str = "") -> Optional["DeferredTools"]:
        """Take the schemas *patterns* defer out of *tools_schema* (in place) and
        append the ``tool_search`` schema. None when nothing is deferred."""
        if not patterns:
            return None
        deferred = {name: schema for schema in tools_schema
                    if (name := _name(schema))
                    and tool_matches_patterns(name, tool_name_mapping.get(name, ""), patterns)}
        for pattern in patterns:
            if (agent_name, pattern) not in _warned and not any(
                    tool_matches_patterns(name, tool_name_mapping.get(name, ""), [pattern]) for name in deferred):
                _warned.add((agent_name, pattern))  # once per process: a run starts per message
                logger.warning("Agent %s: tools.deferred pattern %r matches no allowed tool", agent_name, pattern)
        if not deferred:
            return None
        if TOOL_SEARCH in tool_name_mapping:
            logger.error("A tool named %r exists; deferring is off for this run, every schema is sent",
                         TOOL_SEARCH)
            return None
        tools_schema[:] = [schema for schema in tools_schema if _name(schema) not in deferred]
        instance = cls(deferred, {name: tool_name_mapping.get(name, "") for name in deferred})
        tools_schema.append(instance.search_schema())
        return instance

    def search_schema(self) -> Dict[str, Any]:
        lines = [f"- {name}: {_summary(self._schemas[name]['function'].get('description', ''))}"
                 for name in sorted(self._schemas)]
        return {"type": "function", "function": {
            "name": TOOL_SEARCH,
            "description": (
                "Load a tool listed below before you call it: only its name is known to you "
                "until then. query \"select:name1,name2\" loads those tools, any other query "
                "the best keyword matches. Loaded tools join your tool list with their "
                "parameters from your next step on.\n\nTools to load:\n" + "\n".join(lines)),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string",
                                         "description": "\"select:name1,name2\" or keywords"}},
                "required": ["query"],
            },
        }}

    def search(self, query: str) -> List[str]:
        query = (query or "").strip()
        if query.lower().startswith("select:"):
            wanted = (part.strip() for part in query[len("select:"):].split(","))
            return [name for name in dict.fromkeys(wanted) if name in self._schemas]
        terms = [term for term in re.split(r"[\s,/]+", query.lower()) if term]
        scored = []
        for name, schema in self._schemas.items():
            label = f"{name} {self._servers.get(name, '')}".lower()
            description = (schema["function"].get("description") or "").lower()
            score = sum(3 if term in label else 1 if term in description else 0 for term in terms)
            if score:
                scored.append((-score, name))
        return [name for _, name in sorted(scored)[:MAX_RESULTS]]

    def load(self, names: Iterable[str], tools_schema: List[Dict[str, Any]]) -> List[str]:
        """Append the schemas of *names* still unloaded; returns those appended."""
        loaded = [name for name in names if name in self.pending]
        for name in loaded:
            self.pending.discard(name)
            tools_schema.append(self._schemas[name])
        return loaded

    def intercept(self, name: Optional[str], arguments: Any, step: int,
                  tools_schema: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The answer to a call this class handles instead of a tool server:
        ``tool_search``, or a deferred tool called before it was loaded. None
        for every other call.

        Every call of a tool loaded in this *step* is refused as well -- a second
        call of an unloaded tool, a call right behind the ``tool_search`` that
        loaded it: the model wrote its arguments without the schema, which only
        the next step carries.

        *arguments* is None when they were not valid JSON: a ``tool_search``
        then gets the parse error (None here), an unloaded tool is refused and
        loaded all the same -- its arguments were never going to be used."""
        if name == TOOL_SEARCH:
            if arguments is None:
                return None
            query = arguments.get("query") if isinstance(arguments, dict) else None
            found = self.search(query if isinstance(query, str) else "")
            for loaded in self.load(found, tools_schema):
                self._refused[loaded] = step
            if not found:
                return {"loaded": [], "message": (
                    "No tool matches. The tools you can load are listed in the "
                    f"{TOOL_SEARCH} description.")}
            return {"loaded": found, "message": (
                "These tools are in your tool list now: call them with the parameters they declare.")}
        if name is not None and (name in self.pending or self._refused.get(name) == step):
            self.load([name], tools_schema)
            self._refused[name] = step
            return {"loaded": [name], "type": NOT_LOADED_TYPE, "error": (
                f"'{name}' was called before it was loaded and was NOT run. Its parameters "
                "are in your tool list now: send the call again with them.")}
        return None

    def restore(self, messages: Iterable[Any], tools_schema: List[Dict[str, Any]]) -> List[str]:
        """Load again, in the order they were loaded, the tools the history
        already loaded: named in a ``tool_search`` answer, or called. Takes
        ChatMessages and stored message dicts alike.

        The order is the run's: the calls in the order the model made them, a
        ``tool_search`` call standing for the tools its answer loaded. A
        different order would miss the cached prefix the last run left."""
        messages = list(messages)
        # tool_search call id -> what its answers loaded, in order (an id may repeat)
        answers: Dict[Any, List[List[str]]] = {}
        for message in messages:
            if _is_search_answer(message):
                answers.setdefault(_field(message, "tool_call_id"), []).append(_loaded_by(message))
        called_ids = {_field(call, "id") for message in messages
                      for call in _field(message, "tool_calls") or []} - {None}
        used: List[str] = []
        for message in messages:
            # An answer whose call a compaction took away (or that has no id)
            # counts where it stands.
            if _is_search_answer(message) and _field(message, "tool_call_id") not in called_ids:
                used.extend(_loaded_by(message))
            for call in _field(message, "tool_calls") or []:
                name = _field(_field(call, "function"), "name")
                if name == TOOL_SEARCH:
                    pending = answers.get(_field(call, "id")) or [[]]
                    used.extend(pending.pop(0))
                elif isinstance(name, str):
                    used.append(name)
        return self.load(dict.fromkeys(used), tools_schema)


def _is_search_answer(message: Any) -> bool:
    return _field(message, "role") == "tool" and _field(message, "name") == TOOL_SEARCH


def _loaded_by(message: Any) -> List[str]:
    """The tools a ``tool_search`` answer names as loaded; [] for one that is not JSON."""
    try:
        loaded = json.loads(_field(message, "content")).get("loaded") or []
    except (ValueError, TypeError, AttributeError):
        return []
    return [name for name in loaded if isinstance(name, str)] if isinstance(loaded, list) else []


def _field(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)
