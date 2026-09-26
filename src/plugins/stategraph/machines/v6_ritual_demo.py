"""Companion module of v6_ritual_demo.yaml.

Pure functions only (docs/stategraph_design.md §3.6): no I/O, no clock, no
randomness, no environment. Every public name here is in scope for the
machine's code fields and templates; ``parse_marker`` is the agent's parser.
"""

import re

_DOC_ID = re.compile(r"[A-Za-z0-9_.:-]+")
MARKER_HELP = ("End your answer with one line 'DELTA_DOC=<doc id>'; add ' | STATUS_DOC=<doc id>' "
               "and ' | DELETE_KEYS=<path>,<path>' when they apply.")


def parse_marker(text):
    """The panel's marker line as data.

    The line looks like ``DELTA_DOC=<id> | STATUS_DOC=<id> | DELETE_KEYS=a.b,c``;
    only DELTA_DOC is required, and the last line that carries it counts.
    Raising ValueError sends the message back to the same agent instance.
    """
    lines = [line.strip() for line in str(text).splitlines() if "DELTA_DOC=" in line]
    if not lines:
        raise ValueError(f"your answer has no marker line. {MARKER_HELP}")
    fields = {}
    for part in lines[-1].split("|"):
        key, separator, value = part.partition("=")
        if separator:
            fields[key.strip().strip("`*").upper()] = value.strip().strip("`*")
    delta_doc = fields.get("DELTA_DOC", "")
    if not _DOC_ID.fullmatch(delta_doc):
        raise ValueError(f"DELTA_DOC={delta_doc!r} is not a document id. {MARKER_HELP}")
    status_doc = fields.get("STATUS_DOC") or None
    delete_keys = [key.strip() for key in fields.get("DELETE_KEYS", "").split(",") if key.strip()]
    return {"delta_doc": delta_doc, "status_doc": status_doc, "delete_keys": delete_keys}


def panel_task(params):
    """The panel's task; phase and work item also reach its prompt as template vars."""
    return (f"Phase {params.phase}: work on the {params.work_item} of the story. "
            f"Write your result into a new document in namespace {params.namespace}. {MARKER_HELP}")


def fix_task(feedback):
    """Follow-up to the same panel instance after its delta could not be merged."""
    return (f"Your delta could not be merged into the story document: {feedback}\n"
            f"Fix the delta document (or write a new one) and answer again. {MARKER_HELP}")


def merge_feedback(message, data):
    """The store's error for the panel: the message plus the store's own error text, if any."""
    detail = data.get("error") if hasattr(data, "get") else None
    return f"{message} ({detail})" if detail and str(detail) not in message else message
