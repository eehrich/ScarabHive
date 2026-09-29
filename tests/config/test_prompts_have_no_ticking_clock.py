"""A prompt must not carry a clock that ticks inside the cached prefix.

Measured 2026-09-01 on a live sysadmin_agent chat: the system prompt was
5354 characters and identical for the first 5290 of them — then

    - Current time: 01:01:39      turn n
    - Current time: 01:01:58      turn n+1

Those 64 characters sit inside the block the cache breakpoint covers, so
every turn produced a new prefix. The system prompt is only 4% of the
payload, but the 94 conversation messages behind it lose the cache with it:
the chat footer showed 21-22% cache hits and ~$0.13 per turn on an almost
unchanged history.

`current_date` is fine — it breaks the cache once a day. Anything carrying
seconds is not. Agents that genuinely need the exact time have the
`datetime` tool, which asks at the moment it matters instead of freezing an
answer into every later turn.

`current_step` is the same clock counted in steps: the agent re-renders the
system prompt before every step, so "Current step: 3/30" turned into "4/30"
and every call of a run re-paid the whole conversation (found 2026-09-13 in
the default template, used by 104 agents).
"""
from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, TemplateSyntaxError, meta

REPO = Path(__file__).resolve().parents[2]

#: Template variables that change faster than a conversation does.
TICKING = ("current_time", "current_datetime", "unix_timestamp", "current_step")

_JINJA = Environment()

#: Fallback for text Jinja cannot parse: the variable anywhere inside a block.
#: The word boundary keeps "{{ current_timezone }}" out — it starts with
#: "current_time" and is perfectly stable.
USE = re.compile(r"\{[{%][^}]*?\b(" + "|".join(TICKING) + r")\b")


def _ticking_uses(text: str) -> list[str]:
    """The variables a template reads, as Jinja parses them: inside any
    expression ("{{ max_steps - current_step }}", "{% if current_step > 3 %}"),
    never inside a {# comment #}."""
    try:
        names = meta.find_undeclared_variables(_JINJA.parse(text))
    except TemplateSyntaxError:
        names = {m.group(1) for m in USE.finditer(text)}
    return sorted(names & set(TICKING))


def _prompt_files():
    for base in (REPO / "config", REPO / "src"):
        for path in base.rglob("*.md"):
            if "prompts" in path.relative_to(REPO).parts:
                yield path
        # Agent YAMLs carry inline system_prompt blocks, rendered the same way;
        # "agents_priv" and "agents_trading" hold agents too. Relative parts:
        # a checkout in /root/agentsystem starts with "agents" as well.
        for path in base.rglob("*.yaml"):
            if any(part.startswith("agents") for part in path.relative_to(REPO).parts):
                yield path


def test_no_prompt_embeds_a_ticking_clock():
    offenders = []
    for path in _prompt_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for var in _ticking_uses(text):
            offenders.append(f"{path.relative_to(REPO)}: {var}")
    assert not offenders, (
        "these prompts re-render a value that changes per call (a clock or the "
        "step counter) into the cached prefix, so every call re-pays the whole "
        "history: " + "; ".join(offenders)
        + " -- use current_date / max_steps, or the datetime tool for the exact time")


def test_the_scan_actually_reaches_the_prompts():
    """Empty-set trap: a path typo would make the test above pass forever."""
    files = list(_prompt_files())
    assert len(files) > 20, f"only {len(files)} prompt files found"
    assert any("system_admin_prompt" in f.name for f in files), (
        "the prompt this was measured on is not being scanned")
    assert any(f.name == "system_prompt.md" and f.parent.name == "prompts" for f in files), (
        "the default template is not being scanned")
    assert any(f.suffix == ".yaml" for f in files), "no agent YAML is being scanned"
    private = REPO / "config" / "agents_priv"
    for path in [*private.glob("*.yaml"), *private.glob("prompts/*.md")]:
        assert path in files, f"{path.relative_to(REPO)} is not being scanned"


def test_the_scan_finds_every_way_a_template_uses_the_variable():
    for use in ("{{ current_step }}", "{{- current_time -}}", "{{ max_steps - current_step }}",
                "{% if current_step > max_steps - 3 %}x{% endif %}", "{{ current_datetime | default('') }}",
                "{{ '{}'.format(current_step) }}", "{{ {'a': 1}['a'] + current_step }}",
                "{{ current_step ", "{% if current_step %}unclosed"):
        assert _ticking_uses(use), use
    for stable in ("{{ current_timezone }}", "{{ current_date }}", "{{ max_steps }}",
                   "the current_step in prose", "{# {{ current_time }} #}",
                   "{% set current_step = 1 %}{{ current_step }}"):
        assert not _ticking_uses(stable), stable


def test_the_scan_ignores_the_checkout_directory_name(monkeypatch, tmp_path):
    """Server checkouts live in /root/agentsystem, which starts with "agents"."""
    repo = tmp_path / "agentsystem"
    (repo / "src" / "plugins" / "demo").mkdir(parents=True)
    (repo / "src" / "plugins" / "demo" / "schema.yaml").write_text("x: 1", encoding="utf-8")
    (repo / "config" / "agents").mkdir(parents=True)
    (repo / "config" / "agents" / "demo.yaml").write_text("x: 1", encoding="utf-8")
    monkeypatch.setitem(globals(), "REPO", repo)

    assert [p.relative_to(repo).as_posix() for p in _prompt_files()] == ["config/agents/demo.yaml"]
