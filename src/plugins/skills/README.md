# Skills

Lets an agent browse skill bundles -- folders with a `SKILL.md` and the reference files, scripts and templates next
to it -- and read the instructions or one file when a task needs it. Read-only; it stores nothing. It sees every skill
the core finds under `skills.skill_dirs`; what a skill is and how one gets into a prompt is the core's "Skills" node
in the ScarabHive guide. It has no hooks and no panel.

- **Tools** `skills_list` (all skills, or one by `name`, with their files) and `skills_read` (`name`, optional
  `path`; without a path the instructions from `SKILL.md`).

It is enabled in `config/plugins.yaml` (`skills: {type: skills, enabled: true}`); allow `+skills/*` in an agent's
tool list. Agents with `on_demand` skills need it; agents that only use `always` skills do not.

The full manual -- the answers and every error text, which paths may be read, the 100,000-character limit, when new
skills show up, and what the model sees -- is the plugin's guide, `skills.guide`, in the Help panel.
