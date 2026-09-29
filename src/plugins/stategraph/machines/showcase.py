"""Companion module of showcase.yaml.

Its public names are in scope in every code field and template of the machine, and its functions can be
``call``, ``parse`` and ``check`` targets. Pure functions only (format §9): no I/O, no clock, no randomness --
a replay runs them again and must get the same result.
"""


def store_namespace(run_id):
    """The run's own json store namespace: a distinctive string, so a fork's replay can swap it (format §14)."""
    return f"showcase-{run_id}"


def fork_namespace(source, run_id):
    """A fork's namespace, named after its source's so the store shows where it came from. Nothing is copied:
    this machine writes its store only in its last step, after any fork point that has something to keep."""
    return f"{store_namespace(run_id)}-from-{source}"


def outline_task(params, ctx, manner):
    task = (f"Plan a short article about: {params.topic}\n"
            f"Give {params.sections} section headings, {manner}ly thought through.\n"
            'Answer with JSON only: {"sections": ["<heading>", ...]}')
    if ctx.notes:
        task += "\n\nThe last outline was refused: " + "; ".join(ctx.notes)
    return task


def check_outline(sg, wanted):
    """A ``call`` whose first parameter is ``sg``: it reads the run's scope (read-only), here ``ctx``."""
    outline = sg.ctx["outline"]
    if len(outline) < wanted:
        return {"ok": False, "problem": f"{len(outline)} headings, {wanted} wanted"}
    if len(dict.fromkeys(heading.strip().lower() for heading in outline[:wanted])) < wanted:
        return {"ok": False, "problem": "two headings are the same"}
    return {"ok": True, "problem": None}


def section_task(heading, index, ctx, params):
    task = (f"Write section {index + 1} of a short article about {params.topic}: \"{heading}\".\n"
            "Two or three paragraphs, no heading. Answer with the text only.")
    if ctx.notes:
        task += "\n\nThe review asked for this: " + "; ".join(ctx.notes)
    return task


def long_enough(out):
    """A ``check``: raising ValueError sends the message back to the same agent instance as feedback."""
    if len(out.split()) < 40:
        raise ValueError("the section is too short: write at least two full paragraphs")


def pair_sections(outline, texts):
    return [{"heading": heading, "text": text} for heading, text in zip(outline, texts)]


def article_text(ctx):
    return "\n\n".join(f"## {section['heading']}\n\n{section['text']}" for section in ctx.sections)


def needs_source(answer):
    """The facts check answers NONE, or the claims that need a source."""
    return answer.strip().strip(".").upper() != "NONE"


def collect_review(out):
    """The review's branches ran with ``fail: collect``: each is {status, out} or {status, error}."""
    got = {name: branch["out"] for name, branch in out.items() if branch["status"] == "succeeded"}
    notes = []
    if "critique" in got and got["critique"]["blocking"]:
        notes.append(got["critique"]["notes"])
    notes += ["give a source for: " + answer.strip() for answer in got.get("facts", []) if needs_source(answer)]
    if "fit" in got and got["fit"]["value"] != "fits":
        notes.append("the tone is " + got["fit"]["value"].replace("_", " "))
    return {"notes": notes,
            "clarity": got["clarity"]["value"] if "clarity" in got else None,
            "tags": {name: answer["value"] for name, answer in got.get("tags", {}).items()},
            "failed": sorted(name for name, branch in out.items() if branch["status"] == "failed")}


def mean_verdict(out):
    """The judges that counted (``join: {count: 2}``): out = {judge: {value, ...}}."""
    return sum(verdict["value"] for verdict in out.values()) / len(out)


def strip_fences(text):
    """A ``parse``: its return value is ``out``; ValueError goes back to the same instance as feedback."""
    lines = [line for line in text.strip().splitlines() if not line.strip().startswith("```")]
    if not "".join(lines).strip():
        raise ValueError("answer with the article text itself")
    return "\n".join(lines).strip()
