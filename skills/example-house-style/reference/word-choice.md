# Word Choice Reference

Detail material for the house style. **This file is not in the prompt** — an
agent loads it with `skills_read(name="example-house-style",
path="reference/word-choice.md")` when it actually needs this depth. That is the
point of a skill bundle: the index stays cheap, the depth is on demand.

## Prefer / avoid

| Avoid | Prefer | Why |
|---|---|---|
| "utilize" | "use" | No added meaning |
| "in order to" | "to" | Filler |
| "leverage" (verb) | "use", "build on" | Corporate vagueness |
| "delve into" | "examine", "look at" | Overused |
| "it's important to note that" | *(delete)* | Says nothing |
| "seamless", "robust", "powerful" | a concrete property | Unfalsifiable marketing |
| "simply", "just", "obviously" | *(delete)* | Belittles the reader when it is not simple |

## Hedging

State uncertainty once and precisely:

- Bad: "This might possibly be somewhat related to the timeout, perhaps."
- Good: "This is probably the timeout — I have not verified it."

## Numbers

Give the unit and the baseline: "3.2 s (was 11 s)" beats "much faster".
