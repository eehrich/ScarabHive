"""How two pieces of configuration combine. Two merges for two jobs, and neither can do the other's:

- ``deep_merge`` stacks the config FILES -- an include over the master, the local layer over both
  (``overlay_section`` per section): mappings merge, anything else replaces what came before, a list
  or a null too. A ``+x`` in a list stays as written; it belongs to the inheritance below, which runs
  on the result.
- ``_deep_merge_dict`` resolves INHERITANCE -- a server entry over the one its ``type`` names and over
  ``plugins.default_config``, a model entry over the one it ``extends``: lists take the
  ``+item``/``!pattern`` syntax, and a null keeps the inherited value unless the caller says it
  means "no value".

One function for both would change results: the file merge would resolve a ``+x`` before the entry
it belongs to is known, and an include's ``key: null`` would no longer clear what came before.
"""
from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any


def deep_merge(base: dict, overlay: dict) -> dict:
    """Deep merge two dictionaries. Overlay values override base values.
    
    For nested dicts, merges recursively. For lists and other types, overlay replaces base.
    
    Args:
        base: Base dictionary
        overlay: Dictionary to merge on top of base
        
    Returns:
        Merged dictionary
    """
    result = dict(base)
    
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            # Recursively merge nested dicts
            result[key] = deep_merge(result[key], value)
        else:
            # Replace value (including lists, primitives, etc.)
            result[key] = value
    
    return result


def overlay_section(current: Any, value: Any) -> Any:
    """*value* over *current*, as a later config file sets a section: merged where both are mappings
    (deep_merge), else *value* replaces it."""
    return deep_merge(current, value) if isinstance(current, dict) and isinstance(value, dict) else value


def _deep_merge_dict(base: dict, override: dict, _path: str = "",
                     *, explicit_none: bool = False) -> dict:
    """Deep merge two dictionaries, with override values taking precedence.

    A list is either MERGED into the inherited one or REPLACES it, never both:

    - `+item` appends to the inherited list
    - `!pattern` removes matching items from it (supports wildcards)
    - a list with no prefixes at all replaces the inherited one wholesale

    Examples:
        # Replace the entire list (no +/! anywhere)
        tools:
          allowed: ["new_tool/*"]

        # Merge with the inherited list
        tools:
          allowed:
            - "+new_tool/*"     # add
            - "!old_tool/*"     # remove

    Mixing the two forms raises — see :func:`_merge_lists_with_syntax`.

    Args:
        base: Base dictionary (default values)
        override: Override dictionary (specific values that override base)
        _path: Dotted key path, used only to make error messages locatable.
        explicit_none: What ``key: null`` in the override means. Default False —
            "nothing said", the inherited value stays; that is what an agent
            yaml with an empty key needs. True means "no value", the inherited
            one is removed: a model entry that inherits ``max_tokens: 16384``
            has no other way to say it wants the provider default.

    Returns:
        New dictionary with merged values
    """
    result = base.copy()

    for key, value in override.items():
        path = f"{_path}.{key}" if _path else str(key)
        if isinstance(value, dict) and (result.get(key) is None or isinstance(result[key], dict)):
            # Recursively merge nested dictionaries -- also into a value the parent
            # lacks or leaves None, or a nested "+x" would survive as a literal.
            # Such a dict is taken over whole otherwise: its Nones stay.
            fresh = not isinstance(result.get(key), dict)
            result[key] = _deep_merge_dict(result.get(key) or {}, value, path,
                                           explicit_none=explicit_none or fresh)
        elif isinstance(value, list):
            # Also for a key the parent does not have: without this, a "+x"
            # would survive into the value as a literal and match nothing.
            parent = result.get(key)
            # an inherited string ("llm_profile: normal") is a list of one: "+x" adds to it
            base_list = parent if isinstance(parent, list) else [parent] if isinstance(parent, str) else []
            result[key] = _merge_lists_with_syntax(base_list, value, path)
        elif value is not None or explicit_none:
            result[key] = value

    return result


def _merge_lists_with_syntax(parent_list: list, child_list: list,
                             path: str = "list") -> list:
    """Merge a child list into the inherited one, or let it replace it.

    Two mutually exclusive intents:

    - **merge** — every string carries ``+`` (add) or ``!`` (remove pattern)
    - **replace** — no string carries a prefix; the child list wins wholesale

    Mixing them raises ``ValueError``. The mix is almost always a forgotten
    ``+``, and it is unrecoverable by guessing: read as "replace", the prefixed
    entries would be the only survivors; read as "merge", the bare one silently
    joins the inherited list. Both readings are defensible, which is exactly why
    the config must say which one it means.

    Args:
        parent_list: The inherited list
        child_list: The override list from the child config
        path: Dotted key path, for the error message

    Returns:
        Merged or replaced list

    Raises:
        ValueError: if the child list mixes prefixed and bare string entries.
    """
    strings = [item for item in child_list if isinstance(item, str)]
    prefixed = [s for s in strings if s.startswith(('+', '!'))]
    bare = [s for s in strings if not s.startswith(('+', '!'))]

    if prefixed and bare:
        raise ValueError(
            f"Config '{path}' mixes list merge syntax with replacement entries. "
            f"Prefixed: {prefixed} — these add to / remove from the inherited "
            f"list. Without a prefix: {bare} — a list of those REPLACES the "
            f"inherited list entirely. One list cannot mean both. "
            f"Most likely a '+' was forgotten on {bare}; add it, or drop every "
            f"prefix to replace the list instead."
        )

    if not prefixed:
        # No merge syntax - complete replacement (original behavior)
        return child_list

    # The inherited list may itself still carry prefixes: a server's own "+x" is
    # only stripped when it is merged against default_config, and that happens
    # AFTER inheritance is resolved. Normalise it against an empty base first —
    # otherwise the "+" rides along into the child's result and later looks like
    # an authoring error that nobody made.
    return _apply_list_ops(_apply_list_ops([], parent_list), child_list)


def _apply_list_ops(base: list, items: list) -> list:
    """Apply ``+``/``!``/bare entries of *items* onto *base*.

    Deliberately permissive — mixing is rejected by the caller, which is the
    only place that knows whether the list was author-written or already merged.
    """
    result = list(base)

    for item in items:
        if not isinstance(item, str):
            # Non-string items are added as-is
            if item not in result:
                result.append(item)
            continue

        if item.startswith('!'):
            # Remove pattern from result
            pattern = item[1:]  # Strip ! prefix
            result = [r for r in result if not _matches_pattern(r, pattern)]
        else:
            clean_item = item[1:] if item.startswith('+') else item
            if clean_item not in result:
                result.append(clean_item)

    return result


def _matches_pattern(value: str, pattern: str) -> bool:
    """Whether a list entry matches a ``!pattern`` -- fnmatch, case-sensitive;
    ``"plugin/*"`` also matches the bare ``"plugin"``.

    A wildcard in the middle (``"*_sam/*"``) used to match nothing, silently.
    """
    if not isinstance(value, str):
        return False
    return fnmatchcase(value, pattern) or (pattern.endswith("/*") and value == pattern[:-2])
