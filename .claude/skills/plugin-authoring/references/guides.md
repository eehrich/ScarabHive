# Writing a plugin's guide

A plugin's manual is `<plugin folder>/<folder name>.guide` (AmigaGuide, English), shown in the Help
panel; the shell's help button of the plugin's panel opens it. The README becomes a short overview.
Worked examples, both written this way: `src/plugins/todo/todo.guide`, `src/plugins/memory/memory.guide`
(and their READMEs). Read one of them first -- structure, tone and density are the pattern.

## The format

Syntax: the manual's nodes "Writing a guide" and "Beyond AmigaOS" in `docs/guides/scarabhive.guide`
(`@node authoring`, `@node markdown`). In short: `@database`, `@author "Enrico Ehrich"`, `@$VER: <name>.guide 1.0
(<date>)`, `@smartwrap`; nodes `@node <name> "<title>"` ... `@endnode`; `@{h1}`, `@{h2}`, `@{bullet}`,
`@{table}` (rows `a | b`, first row the header) ... `@{body}`, `@{code yaml}` ... `@{body}`, `@{tt}x@{utt}`,
`@{b}x@{ub}`, `@{i}x@{ui}`, links `@{"  label  " link node}`, images `@{image docs/panel.png "alt"}`
(images only next to the guide or under `<plugin>/docs/`). A literal `@` in text is `\@`.

## The nodes (leave out what the plugin does not have)

- `main` -- what the plugin is for, in a paragraph a user understands; the screenshot when there is a panel;
  a `@{code}` menu of the other nodes, split "For users" / "For agents and developers".
- `panel` -- for the person using the web UI: how to open it (launcher category, session info button),
  which data it shows (follows the chat / pinned session), what each figure, filter and button does, what
  happens when the server refuses. Read `static/panel.js`, `templates/panel.html`, `web_endpoints.py` and
  the panel test (`tests/test_plugin_<name>_panel.py`, its docstring says what it seeds).
- `tool` (one node per tool, or one for a single multi-operation tool) -- what it is for, operations
  table, parameters table (name, used by, meaning -- limits and defaults AS THE CODE APPLIES THEM),
  answers (fields, status values, errors), one short example.
- `hook` (per hook) -- when it runs, what exactly it changes or appends (show it), when it does nothing,
  how an agent switches it on (`hooks.overrides.<instance>.<hook>`), its settings with defaults, and where
  they are read from (schema `config`, server entry, the agent's override -- check which the code reads).
- `model` "What the model sees" -- the Model Experience: where the tool descriptions come from, error
  texts verbatim, the chat status line, token and prompt-cache effect (append-only? rewrites?), known gaps.
- `setup` -- the real `config/plugins.yaml` entry (copy it, do not invent), the allowlist line
  (`+<instance>/*`), dependencies, server settings table (key, default in code, meaning), where data lives.

## The rule that matters: write from the code

Never copy a claim from the old README, the schema descriptions or docstrings -- they have been wrong
in every plugin so far (invented parameters, wrong thresholds, defaults the code never applies). Read the
server code end to end; for every sentence you write, know the line that makes it true. Check:
defaults in `schema.yaml` against what `execute` actually applies (the framework does NOT fill schema
defaults into tool arguments and does NOT validate arguments against the schema); limits against what the
code enforces; hook settings against where the hook reads them (`context.hook_config` = the agent's
override); error paths (does a bad value answer `{"error": ...}` or raise?).

## Bugs you find on the way: fix them (the user wants this)

A bug you can state -- what is wrong, how it should be -- is fixed, not documented:
- fix it in the plugin's code; keep the diff small and in the style around it (English comments);
- add a test that goes red without the fix, next to the plugin's tests;
- prove it with a mutation probe IN MEMORY (never edit the source to mutate): a subprocess that reads
  the module source, replaces one marker (assert the marker occurs exactly once), `exec`s it into the
  imported module's `__dict__`, runs pytest; plus a CONTROL mutant (a comment change) that must stay
  green. Pattern: `exec(compile(src.replace(old, new), mod.__file__, "exec"), mod.__dict__)` with
  `PYTHONPATH=src`. Every fix's mutant must turn a test red;
- a schema description that tells the model something false is a bug too: correct the text; remove
  `default:` values the code does not apply.
Only a fix that would change behaviour other code or agents rely on in a way you cannot judge: do not
build it, report it.

## The README

Replace it with a short overview (see the todo/memory READMEs): a paragraph what it is for, one bullet
each for tools, hooks and panel, how to enable it, and that the full manual is `<name>.guide` in the Help
panel. No parameter tables, no examples.

## Checks before you report

- `.venv/Scripts/python.exe -m pytest src/plugins/<name>/tests -q -p no:cacheprovider -k "not panel"`
- `.venv/Scripts/python.exe -m pytest tests/ui/test_help.py -q -p no:cacheprovider -k repository`
  (reads every guide in the repo: dead links, unknown commands)
- `.venv/Scripts/python.exe -m ruff check src/plugins/<name>`
- the guide parses without warnings:
  `.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'src'); from agent_system.ui.help import Library; print(Library(['src/plugins']).guides['<name>'].warnings)"`

## Limits for a sub-agent doing this

- Touch only `src/plugins/<name>/`. Do not edit `src/scripts/guide_screenshots.py`, docs, skills or
  other plugins -- report what they would need.
- NEVER start a browser: no panel browser tests, no screenshot script. Reference the screenshot as
  `@{image docs/panel.png "..."}`; the coordinator takes it.
- No commits, no `git add`, no `git stash`/`checkout`/`reset`. Heredocs mangle backslashes: write scripts
  with the Write tool into the scratchpad.
- Report at the end: files changed; each bug fixed (what, where, the test, the mutant killed); schema
  texts changed; claims you could not verify; for a panel, the entry for the screenshot script:
  module with the panel test's app factory, factory name, page path with a seeded session, window size.
