# Coder

A coding agent with a harness: `coder` plans a change, edits the files, runs the tests and has the finished change
attacked by a read-only reviewer before it calls the work done. Three sub-agents take the parts that would cost it
its context or its bias -- `coder_explorer` finds things, `coder_reviewer` reviews read-only, `coder_tester` runs the
checks. What it learns about a project it keeps in an OKF bundle (`data/okf/coder`) that is folded into later
sessions.

- **Agents** `coder`, `coder_explorer`, `coder_reviewer`, `coder_tester` -- all admin-only; only `coder` writes files.
- **Tool instances** in `agents/tools.yaml`: `coder_fs` (read/write), `coder_fs_ro` (the same tree, read-only),
  `coder_shell`, `coder_okf`, `coder_sam`.
- **Skills** `coding-harness` (the working loop and the report contract) and `adversarial-review`.
- No tools, hooks or panel of its own: the plugin is configuration only.

Nothing to enable: the agent YAMLs, prompts and skills are found by convention. Try it with
`agent-cli chat --agent coder`.

The full manual -- what each agent does and may touch, the knowledge bundle, the prompts and tools the models see,
and every setting of the five instances -- is the plugin's guide, `coder.guide`, in the Help panel.

Apache-2.0 -- see `LICENSE`.
