---
name: example-house-style
description: 'Example skill: house writing style for user-facing text. Use it when
  an agent produces prose, summaries or reports that a human will read.'
metadata:
  version: 1.0.0
  tags: example, writing, style
---

# House Writing Style

This is an **example skill** shipped to show the format — copy it as a starting
point, or delete it. It is not referenced by any agent by default.

## Rules

- Lead with the outcome. The first sentence answers "what happened" or "what did
  you find", not "here is what I will explain".
- Prefer plain sentences over bullet fragments when the reader needs reasoning.
  Use lists for genuinely list-shaped content.
- Name things concretely: file paths, numbers, identifiers — not "the relevant
  file" or "several issues".
- State uncertainty explicitly. "I could not verify X" beats an implied claim.
- No filler openers ("Great question!", "Certainly!") and no closing summaries
  that repeat what was just said.

## Formatting

- Short paragraphs; a blank line between them.
- Code, paths and identifiers in backticks.
- Never invent a heading structure for a two-sentence answer.

## Bundled reference

This skill ships detail material that is deliberately **not** in the prompt.
Load it only when a task needs that depth:

- `reference/word-choice.md` — prefer/avoid word list, hedging and number style.

Read it with the skills tools, e.g.
`skills_read(name="example-house-style", path="reference/word-choice.md")`.
