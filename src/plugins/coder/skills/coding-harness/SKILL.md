---
name: coding-harness
description: The working loop for a coding change — understand the real flow before editing, plan as a task list, fix the root cause rather than the reported symptom, verify what you claim, and have the finished change attacked before calling it done. Use for any request to write, fix, refactor or review code.
metadata:
  version: '1.2.0'
---

# The loop

Cheap in this order, expensive in any other.

## 1. Trace the real flow

A request names a symptom, not what the code does. Before the first edit,
follow the actual path: entry point → the function that decides → what
finally writes. Read the callers of what you are about to change.

A small diff in the wrong place is a second bug. Read fully, then be brief —
brevity belongs to the solution, never to the reading.

**Answer your own questions first.** One real exploration pass before you ask
anything. Then ask only what reading cannot settle: which behaviour they
want, which trade-off they prefer, a credential you cannot see.

## 2. Task list before you start

More than one edit → write the steps into the task tool first, and update
them as you go. It is what makes drift visible; a list still on step 1 after
twenty turns is decoration.

**One step in progress at a time.** Three half-started things give you no way
to tell which one broke the build.

## 3. Fix the cause, where all callers pass

Grep every caller before you edit. The same bug is nearly always reachable
through siblings the report never mentioned. One guard in the shared function
is the smaller diff *and* the correct fix.

Not the same as fixing the cause:

- **Healing the artifact** — correcting bad output ≠ stopping what produced
  it. Sometimes you need both; say which you did.
- **Documenting it** — if you can say what is wrong and how it should be, fix
  it. Something stays open only when you lack information you cannot obtain
  yourself, and then name which information.

## 4. Look before you write

Stop at the first rung that holds: does this need to exist → already in this
codebase → standard library → a dependency already installed → the smallest
thing that works.

A re-implementation of what sits a few files over will drift from it.

## 5. Slices, each one verified

One coherent step, then check it. Ten edits then a first run leaves ten
candidates for the failure.

**Verify = you ran something and read the output.** Not that the edit applied,
not that it looks right. For a web page that means the browser: a script
error or a dead button passes every build. Detail: your `web-testing` skill.

**Edit with the file tool, not the shell.** A heredoc, `echo >` or `sed -i`
writing code breaks quotes and indentation, and bypasses the sandbox the file
tool enforces. Write a large file in parts: one enormous call can run past the
output limit and be lost whole.

Before a sweep you cannot easily undo (mass rename, scripted edit, files you
have not read): copy the originals outside the project and say where. Never
undo your own edit with `git checkout`/`stash`/`reset` — they reset to the
last commit and take everyone else's uncommitted work in that file with them.

## 6. One runnable check, proven to bite

Non-trivial logic (branch, loop, parser, money, auth, user input) leaves the
smallest thing that fails when the logic breaks: one test, or a `__main__`
self-check. Trivial one-liners need none.

**Then break the guarded line, watch it go red, restore from your copy.** A
green run has two readings — the code is correct, or the check never looked —
and only the mutation tells them apart.

Where other processes run the code you work on — a server, workers, other
agents importing the same tree — a mutant on disk is live for all of them for
as long as it exists. Mutate a copy of the package instead and put it first on
the import path of your test run.

## 7. Have it attacked

Review the finished change with a reviewer sub-agent, not yourself: you know
what you meant, and that is what hides the bug. Review your own fix round the
same way — two corrections to one area, each sound alone, together wrong.

Findings are claims. Verify each in the code before acting; roughly a third
do not survive, and "fixing" those damages working code. A refutation that
concedes the mechanism and argues only severity has **confirmed** the finding.

Detail: your `adversarial-review` skill.

# When to delegate

| Ask a sub-agent | Because |
|---|---|
| "Where is X handled, who calls Y" | The sweep costs thousands of tokens, the answer is three lines |
| "Run these tests, report failures" | A failing suite is mostly noise |
| "Attack this finished change" | A fresh context sees what yours cannot |

Keep yourself: every judgement about whether code is *right*, every edit,
every decision about what the change should be, and every check in the
browser — the sub-agents have none. Delegated judgement returns as
confident text with nothing behind it.

A sub-agent has none of your conversation. Name the files, paste the diff,
state the question as a question.

# What to carry forward

| | Holds | How you get it |
|---|---|---|
| **Skills** | Procedures for a *kind* of task | Read-only, loaded on demand |
| **Knowledge bundle** | Facts about *this* project | Yours to write; relevant part is injected |
| **The code** | What the system does | Read it |

Write a concept for what the code does not say and the next session would
rediscover the hard way: a **measured** fact with its conditions, a **trap**
with its evidence, a **convention** no file states, a **decision** and its
reason, the command that turned out to work.

Never write what the repo already records — structure, what a function does, a
fix now in the code, anything a grep answers. That copy goes stale, contradicts
the code, and by then it is believed.

- **Always include the why.** "Run the suite per directory" gets followed into
  the wrong situation; "— the full run takes 20 minutes" can be judged.
- **Bundle and code disagree → say so.** One is wrong; which matters.
- **Keep a concept under ~1500 characters.** Injection cuts the body there,
  mid-sentence, silently — past that point is knowledge you never see again
  (measured: over half of existing concepts are long enough to be cut). Split
  into linked concepts instead. Two of 800 arrive whole; one of 1600 arrives
  as a fragment that reads complete.

Knowledge true in *any* codebase is a skill, not a bundle concept.

# Reporting

What changed, what you ran, what came back. Failures shown, not summarised. If
something is blocked, name what is missing and finish everything that is not —
scaling the work down is the asker's decision.

**Never describe a step you did not run as if you ran it.**

Close with these three lines, last thing in your answer:

```
Ran:      <what you executed, and what came back>
Check:    <the check you left behind — or why this logic is too trivial for one>
Reviewed: <coder_reviewer instance + findings — or "skipped: <reason>">
```

`Reviewed: skipped` is legitimate — a change too small for fresh eyes to find
anything does not need a round, and spending one is theatre. **Judging that is
yours; leaving it unsaid is not.** A skipped step and an unnecessary one look
identical to the reader unless you say which it was.

Test for skipping: could a reader who does not know your intent plausibly find
something here? "It is small" is not the criterion — small changes to shared
code are where fresh eyes pay.
