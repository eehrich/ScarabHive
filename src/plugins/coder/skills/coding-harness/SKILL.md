---
name: coding-harness
description: The working loop for a coding change — understand the real flow before editing, plan as a task list, fix the root cause rather than the reported symptom, verify what you claim, and have the finished change attacked before calling it done. Use for any request to write, fix, refactor or review code.
metadata:
  version: '1.0.0'
---

# The loop

Seven steps. They are cheap in this order and expensive in any other.

## 1. Understand the real flow, not the description

A request names a symptom or a wish. Neither tells you what the code does.
Before the first edit, trace the actual path end to end: the entry point, the
function that decides, the thing that finally writes. Read the callers of what
you are about to change.

This is the step people skip, and skipping it is how a confident wrong fix
gets shipped. It dresses up as efficiency: a small diff in the wrong place is
not a small change, it is a second bug.

**Read fully, then be brief.** Brevity belongs to the solution, never to the
reading.

**Answer your own questions first.** Anything the code can tell you, get from
the code — not from the person who asked. Do at least one real exploration
pass before you ask anything, and then ask only what no amount of reading
could settle: which behaviour they actually want, which trade-off they
prefer, a credential you cannot see. A question whose answer was two greps
away spends someone else's attention on your reading.

## 2. Turn it into a task list

Anything that is more than one edit goes into the task tool as steps, before
you start. Not as ceremony — as the thing that makes drift visible. Without a
written plan a long change quietly becomes a different change, and nobody
notices until the review.

Update it as you go. A list that still says step 1 after twenty turns is not a
plan, it is decoration.

**At most one step in progress at a time.** Three things half-started is not
progress on three fronts, it is three unfinished things and no way to tell
which one broke the build. Finish, mark it done, take the next.

## 3. Fix the cause, at the place all callers pass through

The report names one path. Grep every caller of the function you are about to
touch before you edit it. Nearly always the same bug is reachable through
siblings the report never mentioned.

One guard in the shared function is a smaller diff *and* the correct fix.
Patching only the named path is more work and leaves the rest broken.

Two things that are not the same as fixing the cause:

- **Healing the artifact.** Correcting the bad output that already exists is
  not the same as stopping the thing that produced it. Sometimes you need
  both — say which one you did.
- **Documenting it.** If you can say what is wrong and how it should be, it
  gets fixed. "Deliberately left open", "worth a separate round", "tracking
  it" are ways of saying *not done*. Something may stay open only when you
  are missing information you cannot obtain yourself — and then you must be
  able to name which information that is.

## 4. Look for it in the codebase before you write it

Most of what a change needs already exists a few files over: a helper, a
validator, a type, a pattern. Re-implementing it is the most common kind of
avoidable code, and the copy will drift from the original.

The order that holds, top to bottom, stopping at the first rung that works:
does this need to exist at all → is it already in this codebase → does the
standard library do it → does a dependency already installed do it → write
the smallest thing that works.

## 5. Change in slices, and verify each one

One coherent step, then check it. Not ten edits and then a first run — when
that run fails you have ten candidates and no way to tell which.

"Verify" means you ran something and read the output. Not that the edit
applied. Not that it looks right.

Before a sweep you cannot easily undo — the same rename across twenty files,
a scripted edit, anything touching files you have not read — **copy the
originals somewhere outside the project first** and say where. Then a bad
sweep costs one restore instead of a reconstruction. And never undo your own
edit with `git checkout`, `git stash` or `git reset`: those reset to the last
commit and take every other uncommitted change in the file with them,
including work that was not yours.

## 6. Leave one runnable check behind

Non-trivial logic — a branch, a loop, a parser, anything handling money,
auth, or user input — leaves the smallest thing that fails if the logic
breaks: one test, or a self-check in `__main__`. Trivial one-liners need none.

**Then prove the check bites.** Break the line it guards, run it, watch it go
red, restore the file from a copy. A test that has never been red is a claim,
not a measurement — and a green suite is indistinguishable from an absent one
until you have made it fail on purpose.

Two readings of a green run, and only one of them is good news:

- The code is correct.
- The check never looked.

You cannot tell them apart without the mutation.

## 7. Have it attacked before you call it done

Review the finished change with fresh eyes — a reviewer sub-agent, not
yourself. You know what you meant, and that knowledge is exactly what hides
the bug.

Then **review your own fix round the same way.** Fixes cancelling each other
out is a real failure mode: two corrections to the same area, each sound
alone, together wrong.

Findings come back as claims, not facts. Verify each one in the code before
you act on it — roughly a third do not survive, and "fixing" those damages
working code. The reverse also holds: a refutation that concedes the mechanism
and only argues the severity has confirmed the finding, not refuted it.

Your `adversarial-review` skill carries the detail.

# When to delegate

The instruments are worth their latency only for the right question.

| Ask a sub-agent | Because |
|---|---|
| "Where is X handled, who calls Y" | The sweep costs thousands of tokens and the answer is three lines |
| "Run these tests, report the failures" | A failing suite is mostly noise |
| "Attack this finished change" | A fresh context sees what yours cannot |

Keep for yourself: every judgement about whether code is *right*, every edit,
every decision about what the change should be. Delegated judgement comes back
as confident text with nothing behind it.

Give a sub-agent everything it needs in the task itself — it has none of your
conversation. Name the files. Paste the diff. State the question as a
question.

# What to carry into the next session

Three kinds of knowledge, and mixing them makes all three useless:

| | Holds | How you get it |
|---|---|---|
| **Skills** | Procedures for a *kind* of task | Read-only, loaded when a task needs one |
| **Knowledge bundle** | Facts about *this* project | Yours to write; the relevant part is injected |
| **The code** | What the system actually does | Read it |

Write a concept when you learn something durable that the code does not
already say and the next session would otherwise rediscover the hard way:

- a **measured** fact — a timing, a limit, a size, with the conditions it was
  measured under
- a **trap** you fell into, and the evidence that it is a trap
- a **convention** this project holds to that no file states outright
- a **decision** and its reason, so the next session does not relitigate it
- the command that turned out to be the one that works

Do **not** write what the repository already records: its structure, what a
function does, a fix that now sits in the code, anything a grep would answer.
That is not knowledge, it is a second copy of the source — and the copy is the
one that goes stale. It will contradict the code eventually, and by then it
will be believed.

Two rules that decide whether this stays useful or becomes a liability:

- **Always include the *why*.** A fact without its reason gets applied where
  it does not belong. "Run the suite per directory" is a rule someone will
  follow into the wrong situation; "run the suite per directory — the full run
  takes 20 minutes" is a rule they can judge.
- **When the bundle and the code disagree, say so.** Do not quietly follow
  either. One of them is wrong, and which one it is matters.

**Keep a concept under ~1500 characters.** This is not a style preference.
When a concept is folded into a prompt, its body is cut off at 1500 characters
mid-sentence, with no warning to the reader — so everything you wrote past
that point is knowledge you will never see again. Measured on the bundles that
already exist here: more than half of all concepts are long enough to be cut.

So when a subject outgrows that, **split it into linked concepts** rather than
letting one sprawl. Two concepts of 800 characters both arrive whole; one of
1600 arrives as a fragment that reads like a complete thought.

Reference material that would apply to *any* project belongs in a skill, not
in the bundle. If it would still be true in a different codebase, it is a
procedure, and the bundle is the wrong place for it.

# Reporting

State what changed, what you ran, and what the output was. If tests failed,
say so and show them. If you skipped a step, say which. If something is
blocked, name what is missing and finish everything that is not blocked —
scaling the work down is the asker's decision, not yours.

Do not describe a step you did not run as if you ran it. Everything else in
this loop depends on that one being true.
