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
└── coding/                       # a group — see "Where skills are found"
    └── trading-view/
        ├── SKILL.md              # YAML frontmatter + instructions
        └── references/           # optional: depth, read on demand
```

A skill that belongs to one plugin lives with it instead, under
`src/plugins/<name>/skills/` — same layout, also discovered (see below).

The standard also names `scripts/` (executable code) and `assets/` (templates,
data). Any subdirectory works — the names only matter for humans and for other
tools that look for them; discovery cares about `SKILL.md` alone.

`SKILL.md`:

```markdown
---
name: amiga-coding                # 1-64 chars, lowercase a-z/0-9 and single
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

### Retired: the `skill.toml` manifest

Skills predating the standard kept their metadata in a `skill.toml` next to
`SKILL.md`, and their body ran through Jinja. Both are gone — one format, one
contract. A directory that still has a `skill.toml` but no frontmatter is
**reported** at discovery, naming the fix, instead of quietly not loading.

## Using a skill

Reference it by name from the agent config — no prompt file has to be touched:

```yaml
agent_config:
  skills: ["amiga-coding"]          # shorthand for always
  # or, explicit (what src/plugins/amiga/agents/amiga_coder.yaml does):
  skills:
    always:    ["amiga-coding"]     # full body goes into the system prompt
    on_demand: ["m68k-assembly"]    # only a one-line index goes in
```

Skills are addressed by **name**, never by path — moving a skill between groups
changes nothing for the agents that use it.

**`always`** — the body is appended to the system prompt at render time. The
agent *has* the knowledge; it cannot forget to fetch it. Use this for anything
needed most of the time.

**`on_demand`** — only the skill's `description` is listed in the prompt, so the
agent knows it exists; it pulls the body (and any `references/` files) with the
`skills` plugin's tools when a task needs them. Use this for large material
needed occasionally.

`on_demand` is a **hint, not a permission**: an agent that has the `skills`
plugin can list and read *every* discovered skill, whether or not its config
names it. What the entry buys is the description sitting in the (cached) system
prompt, so the agent knows the skill exists without spending a `skills_list()`
call — and without having to think of making it.

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
- `skills_read(name, path="references/catalog.md")` — a bundled file

Reads are confined to the skill directory (no `..`, no absolute paths, symlinks
resolved) and truncated at 100k characters so one file cannot flood the context.

A miss returns `files` (what the bundle actually holds) and, when the request was
close enough to be a typo, `did_you_mean`. Agents slip on `reference/` vs
`references/` in particular — **pick one spelling per repo**. Ours is the
plural `references/`, in every bundle, matching the folder layout in Anthropic's
skill guide; keep it that way when you add one, and the slip mostly stops
happening. (It used to be the singular here — the rename was purely about
following the published convention, not because one spelling reads better. The
`did_you_mean` hint stays either way: a shared spelling makes the slip rare, it
does not make it impossible.) No suggestion is offered when the only
difference is a number: `kapitel_15.md` and `kapitel_16.md` are two chapters, not
two spellings, and pointing an agent at the neighbour is worse than the miss.

Without the plugin, `always` skills still work — you just lose the ability to
read bundled reference files.

## Writing a skill body

`SKILL.md` is plain markdown, used **verbatim**:

* No Jinja. `{{ current_date }}` stays literal text — a skill about templating
  survives intact, and nothing gets blanked by an unknown variable.
* Shared prompt fragments belong in the prompt *templates* via `{% include %}`,
  not in skills (see `docs/skills_design.md` §8).

**Keep bodies byte-stable.** No timestamps, relative times or random IDs in the
text: the system prompt is the cached prefix, and content that changes per call
breaks that cache for everything after it (see `docs/prompt_cache_design.md`).

## Where skills are found

Configured in `config/config.yaml`, exactly like `plugins.plugin_dirs`. This is
what the repo currently uses:

```yaml
skills:
  skill_dirs:
    - .claude/skills    # what other tools drop into the project
    - skills/*/         # our own, grouped: skills/<group>/<skill>/SKILL.md
```

Wildcards work like in the config `includes`. **A pattern expands to roots**, and
each expanded root is then scanned for skills the usual way — which is exactly
what makes grouping work:

| Entry | Finds |
|---|---|
| `skills` | `skills/<skill>/SKILL.md` — flat only |
| `skills/*/` | `skills/<group>/<skill>/SKILL.md` — grouped only |
| `skills/**/` | both, at any depth |

Note the middle row: `skills/*/` does **not** include flat skills sitting
directly in `skills/`. Either list `skills` alongside it, or use `**`.

Overlapping entries (`skills` and `skills/**/`) are scanned once, so mixing plain
roots and patterns is safe. A pattern that matches nothing is logged — an empty
skill list is otherwise hard to explain.

Relative paths resolve config-folder-first, then repo root — for a pattern that
is decided by which base actually matches, since a wildcard path never exists as
such. The first root defining a name wins. If the block is missing or empty, the
defaults are
`skills/` **and `.claude/skills/`** — the latter is where the ecosystem drops
project-local skills, so a downloaded one works without any config. `skills/`
is listed first, so ours win a name collision. `$AGENT_SKILL_DIRS`
(os.pathsep-separated) replaces both.

Being discovered does not put a skill into any prompt: that only happens when an
agent config names it under `always` or `on_demand`.

A directory without a readable `SKILL.md` frontmatter is not a skill and is
skipped — one bad directory never stops startup. A skill referenced by an agent
but not found is logged as an ERROR (once per agent) and omitted, so a typo is
visible instead of silently changing behaviour.

Two cases are reported louder, because the failure is otherwise invisible: a
directory that still carries a retired `skill.toml`, and a `skill_dirs` pattern
that matches nothing. Both look identical to "there are simply no skills here".
