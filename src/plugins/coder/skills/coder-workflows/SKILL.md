---
name: coder-workflows
description: The workflows a coder hands a change to -- build until green then review with refutation, or a read-only review with refutation -- as machines to copy, with when each pays and how to run it. Load before writing a workflow; the format itself is in stategraph-workflows.
metadata:
  version: '0.1.0'
---

# Workflows for a change

Two machines to start from, in this skill's `machines/`. Read one with
`skills_read(name="coder-workflows", path="machines/build_until_green.yaml")`, copy it, give it an id of
its own, change only what the task needs. The format and its rules are in the skill `stategraph-workflows`.

| Machine | Does | Pays when |
|---|---|---|
| `build_until_green` | `coder` builds, `coder_tester` runs the tests, red goes back with the failure (at most four builds); green goes to one `coder_reviewer` per lens, three skeptics try to refute each finding, what stands goes back to the builder | a change of many steps whose tests exist or come with it -- the loop you would otherwise hold over many turns |
| `review_with_refutation` | one reviewer per lens at once, three skeptics per finding | a finished change of your own before you call it done, or a diff you were handed |

## Running one

1. Scout first: the files, the test command, what "done" means. Write them into `params.task` -- the
   builder sees nothing of your conversation, as a sub-agent does not.
2. `stategraph_validate_machine(yaml=...)` until no error.
3. `stategraph_run_workflow(yaml=..., params=..., max_output_tokens=...)`. Name the cap to the user. Its answer's
   `next` says whether to end your turn (the result comes with your next one) or to wait with
   `stategraph_get_run(run_id, wait="finish")`.
4. Do not touch the files while it runs: its builder works in the same tree.
5. Read the result. `open_findings` are confirmed findings the builder did not get to; `lost` names reviews
   that failed and findings whose skeptics failed before two agreed -- neither is "no problem". Then check it yourself: the diff, the tests.

## Changing them

- **One builder.** `coder` writes; no `map` or `parallel` may run two `coder` steps at once. Reviewers,
  testers and explorers read and may run side by side.
- **Lenses fit the change**: a parser wants "malformed input", a cache "invalidation", an API "the callers".
  Three is enough for most changes; every lens costs a reviewer and three skeptics per finding.
- **Bounds stay**: the build counter, `max_visits`, `limits.max_output_tokens`. A loop without them is refused or runs
  to its cap.
- Agents only from `stategraph_catalog(for_workflow=true)`: the ones you could start yourself.
