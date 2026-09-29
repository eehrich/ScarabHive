---
name: adversarial-review
description: How to review a code change so the findings are real — try to refute every finding before reporting it, review past the diff to the callers a contract change breaks, and re-review your own fix round. Use when reviewing a diff, a pull request, or your own finished change before calling it done.
metadata:
  version: '1.1.0'
---

# Findings that are real

Two failure modes; the second is the expensive one:

- Missing a real bug.
- Reporting something that is not a bug — it buys a fix round on healthy
  code, and that round can break what worked.

Roughly a **third** of findings do not survive a check against the code.

## No finding without a refutation attempt

Before a finding leaves your report, genuinely try to show it cannot happen:

- A guard earlier in the path that already rejects this input?
- A caller that normalises the value before it arrives?
- Is the branch reachable from any real entry point?
- Does a type, schema or validation layer exclude the case?
- Is the "missing" handling done by the framework one layer up?

Go read those places — the refutation is a code question, not a thought
experiment. **When it holds, drop the finding.** Not "report with lower
confidence". Ten findings with three noise are trusted less than seven that
all hold.

**The refutation that does not count:** one that concedes the mechanism and
disputes only severity has *confirmed* the finding. "Yes, it can be `None`
there, but that is rare" is agreement — rare inputs arrive. Keep it, and say
what it costs when it fires.

## A finding names a failing case

Fill this in concretely: *given these inputs or this state, the code takes
this path and produces this wrong result.*

If you cannot, you have a suspicion. Report suspicions as suspicions, never in
the findings list.

## The diff is the wrong boundary

Where to start looking, not where to stop — the whole class of bug where the
diff is locally correct and globally wrong lives outside it.

| Changed | Read |
|---|---|
| Signature or contract | Every caller. Grep the symbol; the diff did not find them |
| A default | The config, callers and tests relying on the old one |
| A shared function | The paths the ticket never mentioned |
| A data shape | Writer and reader — they live in different files |
| An instruction to a model | The prompt and the code parsing the answer are one contract |

## Review the fix round too

Fix-round code gets less scrutiny than what caused it — backwards, since it
was written believing the thinking was done. Look specifically for:

- **Fixes that cancel each other.** Two corrections in one area, each sound
  alone, together wrong. This happens repeatedly.
- **A fix that moves the symptom.** The failure stops appearing; the cause is
  untouched and resurfaces somewhere quieter.

## Green does not mean checked

| Signal | Often actually means |
|---|---|
| Tests pass | Nothing exercises the changed line |
| Counter is zero | The measurement was never wired up |
| "No findings" | Nobody looked, or looked only at the diff |
| The log is quiet | The error path swallows and returns |

A new test proves nothing until it has been red against deliberately broken
code.

## Not your business

- Style, naming, formatting — unless it contradicts a rule the project
  documents.
- Pre-existing problems the change did not touch. Serious ones get their own
  paragraph, not a line in this change's list.
- Missing generality nobody asked for.
- The version you would have written. The question is whether *this* change
  is wrong.

## Read-only

A reviewer that can edit stops reviewing and starts rewriting — and then the
change nobody reviewed is the one that ships. Read, grep, run tests. The fix
belongs to the author, who has the context.

## The report

Verdict first: sound or not. Then worst-first:

```
SEVERITY · path/to/file.py:142 · one sentence, the defect itself
  Failing case:     <inputs/state → path → wrong result>
  Refutation tried: <what you checked, why it did not hold>
```

Found nothing? Say what you checked — which callers, tests, edge cases, how
far past the diff. Bare "no findings" cannot be told from not having looked,
and will be read that way.
