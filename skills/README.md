# Skills

Packaged, reusable agent knowledge. **Plugins distribute capabilities (tools an
agent can call); skills distribute knowledge (how an agent should approach a
task).** See `docs/skills_design.md` for the concept and trade-offs.

## Layout

Every immediate subdirectory of a skill root is one skill. A skill is a
**bundle**: `SKILL.md` is the index/instructions, and the files next to it carry
depth that is loaded only when a task needs it.

We follow the [Agent Skills standard](https://agentskills.io) — the same format
Claude Code, Codex, Cursor, Copilot, Gemini CLI and others read. A skill written
here works there, and one downloaded from anywhere works here.

```
skills/
└── my-skill/
    ├── SKILL.md              # YAML frontmatter + instructions
    ├── references/           # optional: depth, read on demand
    ├── scripts/              # optional: executable code
    └── assets/               # optional: templates, data
```

`SKILL.md`:

```markdown
---
name: my-skill                    # 1-64 chars, lowercase a-z/0-9 and single
                                  # hyphens; must equal the directory name
description: One line — what this covers and WHEN to use it. This is the only
  text the agent sees before deciding to load the skill, so name the trigger.
license: Apache-2.0               # optional
compatibility: Requires git, jq   # optional, only if the skill needs it
allowed-tools: Read Grep          # optional; READ but NOT enforced here — we
                                  # have no per-skill tool gating, so this logs
                                  # a warning and the agent's own config applies
metadata:                         # optional free-form string map
  version: '1.0.0'
---

# The instructions

Markdown, no format restrictions. Keep it under ~500 lines and move detail
into `references/`.
```

Only `name` and `description` are required. The body is used **verbatim** —
`{{ ... }}` stays literal text, so a skill about templating survives intact.

### Legacy manifest (still supported)

Skills predating the standard used a `skill.toml` next to `SKILL.md`:

```toml
[skill]
name = "my-skill"                 # unique; defaults to the directory name
version = "1.0.0"
description = "One line: what this covers and WHEN to use it."
tags = ["writing"]
entry = "SKILL.md"                # optional, this is the default
```

## Using a skill

Reference it by name from the agent config — no prompt file has to be touched:

```yaml
agent_config:
  skills: ["house-style"]          # shorthand for always
  # or, explicit:
  skills:
    always:    ["house-style"]     # full body goes into the system prompt
    on_demand: ["clause-catalog"]  # only a one-line index goes in
```

**`always`** — the body is appended to the system prompt at render time. The
agent *has* the knowledge; it cannot forget to fetch it. Use this for anything
needed most of the time.

**`on_demand`** — only the skill's `description` is listed in the prompt, so the
agent knows it exists; it pulls the body (and any `reference/` files) with the
`skills` plugin's tools when a task needs them. Use this for large material
needed occasionally.

## Reading a bundle (the `skills` plugin)

Allow the plugin for agents that should browse skills:

```yaml
tools:
  allowed:
    - "+skills/*"
```

It provides:

- `skills_list()` — available skills, their descriptions and bundled files
- `skills_read(name)` — the skill's `SKILL.md`
- `skills_read(name, path="reference/catalog.md")` — a bundled file

Reads are confined to the skill directory (no `..`, no absolute paths, symlinks
resolved) and truncated at 100k characters so one file cannot flood the context.

Without the plugin, `always` skills still work — you just lose the ability to
read bundled reference files.

## Writing a skill body

`SKILL.md` is an ordinary prompt template:

* Jinja variables work (`{{ current_date }}`, `{{ tools }}`, custom
  `template_vars`).
* `{% include %}` works — partials resolve next to the skill and from
  `config/prompts/`.

**Keep bodies byte-stable.** No timestamps, relative times or random IDs in the
text: the system prompt is the cached prefix, and content that changes per call
breaks that cache for everything after it (see `docs/prompt_cache_design.md`).

## Where skills are found

Configured in `config/config.yaml`, exactly like `plugins.plugin_dirs`:

```yaml
skills:
  skill_dirs:
    - skills
    - /opt/team-skills
```

Relative paths resolve config-folder-first, then repo root. The first root
defining a name wins. If the block is missing or empty, the defaults are
`skills/` **and `.claude/skills/`** — the latter is where the ecosystem drops
project-local skills, so a downloaded one works without any config. `skills/`
is listed first, so ours win a name collision. `$AGENT_SKILL_DIRS`
(os.pathsep-separated) replaces both.

Being discovered does not put a skill into any prompt: that only happens when an
agent config names it under `always` or `on_demand`.

A skill with a broken manifest or a missing entry file is skipped with a
warning — one bad directory never stops startup. A skill referenced by an agent
but not found is logged as an ERROR (once per agent) and omitted, so a typo is
visible instead of silently changing behaviour.
