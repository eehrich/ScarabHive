---
name: adversarial-review
description: How to review a code change so the findings are real — try to refute every finding before reporting it, review past the diff to the callers a contract change breaks, and re-review your own fix round. Use when reviewing a diff, a pull request, or your own finished change before calling it done.
metadata:
  version: '1.0.0'
---

# Reviewing so that the findings are real

A review has two failure modes, and the second one is the expensive one:

- It misses a real bug.
- It reports something that is not a bug.

The second is worse than it looks. A false finding costs a fix round on
healthy code, and that round can break what worked. Roughly a **third** of
findings do not survive a careful check against the code. Reporting them
unchecked is not thoroughness — it is handing someone else your homework.

## The rule: no finding without a refutation attempt

Before a finding leaves your report, argue the other side. Genuinely try to
show it cannot happen:

- Is there a guard earlier in the path that already prevents this input?
- Does a caller normalise the value before it ever arrives here?
- Is the branch reachable at all, from any real entry point?
- Does the type, schema, or validation layer exclude the case?
- Is the "missing" handling done by the framework a layer up?

Go and read those places. The refutation is a code question, not a thought
experiment.

**When the refutation holds, drop the finding.** Not "report it with lower
confidence" — drop it. A report of ten findings where three are noise gets
trusted less than a report of seven that all hold.

### The one refutation that does not count

A refutation that **concedes the mechanism and only disputes the severity** has
confirmed the finding. "Yes, the value can be `None` there, but that would be
rare" is agreement. Rare inputs arrive.

Keep those, and say what they cost when they fire.

## A finding names a failing case

Write this sentence, concretely: *given these inputs or this state, the code
takes this path and produces this wrong result.*

If you cannot fill it in, you have a suspicion. Suspicions are worth
mentioning as suspicions — never in the same list as findings, and never with
the same confidence.

## The diff is the wrong boundary

The change you were handed is where to *start* looking, not where to stop. A
review confined to the diff misses the whole class of bug where the diff is
locally correct and globally wrong.

Follow the change outward, wherever it applies:

- **A changed signature or contract** — read every caller. Grep the symbol;
  do not trust the diff to have found them.
- **A changed default** — find the config, callers and tests that were relying
  on the old one.
- **A changed shared function** — the paths that were not mentioned in the
  ticket are the ones nobody checked.
- **A changed data shape** — whoever writes it and whoever reads it must have
  moved together, and they usually live in different files.
- **A changed instruction to a model** — the prompts and the code that parses
  the answer are one contract. Changing one side silently is a bug you cannot
  see in the diff.

## Review the fix round too

Fixes are code, and code from a fix round gets less scrutiny than the change
that caused it — which is exactly backwards, because it was written under the
impression the thinking was already done.

Two failure modes to look for specifically:

- **Fixes that cancel each other.** Two corrections in the same area, each
  sound alone, together wrong. This happens, repeatedly.
- **A fix that moves the symptom.** The reported failure stops appearing; the
  cause is untouched and now surfaces somewhere quieter.

Re-read the fix round against itself, with the same rules. It is not finished
because it is a fix.

## Green does not mean checked

Treat these as unproven, never as evidence:

| Signal | What it often actually means |
|---|---|
| The tests pass | Nothing exercises the changed line |
| The counter is zero | The measurement was never wired up |
| "No findings" | Nobody looked, or looked only at the diff |
| The log is quiet | The error path swallows and returns |

For a new test, the only proof it measures anything is watching it go red
against deliberately broken code.

## What is not the reviewer's business

Say it early, so the real findings are not buried:

- Style, naming, formatting — unless the change contradicts a rule the
  project itself documents.
- Pre-existing problems the change did not touch. Serious ones get their own
  paragraph, not a line in this change's list.
- Missing generality nobody asked for. Doing exactly what was requested is
  finished, not incomplete.
- The version you would have written. The question is whether this change is
  wrong.

## Reviewing read-only

A reviewer that can edit stops reviewing and starts rewriting, and then the
change nobody reviewed is the one that ships. Read, grep, run tests if you
have a shell — but the fix belongs to the author, who has the context for it.

## The report

Lead with the verdict: is the change sound or not.

Then, worst first, per finding:

```
SEVERITY · path/to/file.py:142 · one sentence, the defect itself
  Failing case:  <inputs/state → path → wrong result>
  Refutation tried: <what you checked, and why it did not hold>
```

If you found nothing, say so and name what you checked — which callers, which
tests, which edge cases, how far past the diff you went. "No findings" with
nothing behind it cannot be told apart from not having looked, and that is how
it will be read.
