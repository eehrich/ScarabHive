"""Companion module of scene_review.yaml.

Pure functions only (docs/stategraph_design.md §3.6): no I/O, no clock, no
randomness, no environment. Every public name here is in scope for the
machine's code fields and templates.
"""


def writer_task(ctx, params):
    task = f"Write the scene for this premise: {params.premise}"
    if ctx.critique:
        task += f"\n\nRevise this draft:\n{ctx.draft}\n\nusing this critique:\n{ctx.critique}"
    return task
