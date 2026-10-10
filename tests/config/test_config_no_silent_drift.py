"""Two assertions against silently swallowed configuration.

Both checks found two defects each that none of the existing tests had
noticed — because a swallowed value breaks nothing a test looks at: the
application starts, the table is there, only the switch is missing.

**Duplicate YAML keys.** ``yaml.safe_load`` silently keeps the last one.
That is how ``mode: pipeline`` vanished from ``repair_pipeline.yaml`` (the
agent came up as a plain tool server instead of an orchestrator), a complete
tool server from ``mcp_servers.yaml`` (its block ended up in ``localhost`` and
overwrote its URL), and a parameter description from
``json_store/schema.yaml``.

**Profile references into the void.** A profile that does not exist is no
error at load time — it only shows up at runtime, and as a fallback entry
possibly never. That is how ``or-gmini-flash-unlimited`` (a missing "e") went
unnoticed and took the fallback away from two panel agents.

Both tests secure their BASE SET: an empty set satisfies every all-statement,
and a scanner that finds nothing any more could not be told apart from a
working one.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[2]
#: Further plugin roots (src/plugins_<name>/) are not part of the open-source checkout; the fixture
#: counts below are calibrated for both.
PRIVATE_ROOTS = any(d.is_dir() and d.name.isidentifier() for d in (REPO_ROOT / "src").glob("plugins_*"))
sys.path.insert(0, str(REPO_ROOT / "src"))

#: Where configuration lives. `docs` is excluded — it holds examples.
SCAN_ROOTS = ("config", "src")

#: Directories that never contain operational configuration.
SKIP_PARTS = frozenset({"docs", "node_modules", "__pycache__", ".venv"})


class _DuplicateKeyLoader(yaml.SafeLoader):
    """SafeLoader that reports duplicate mapping keys instead of swallowing them."""


def _mapping_without_duplicates(loader, node, deep=False):
    seen: dict = {}
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError:
            # YAML allows composite keys (`? {a: b}`). Rare, but they collide
            # just the same — compare them by their representation.
            key = repr(key)
        if key in seen:
            raise ValueError(
                f"duplicate key {key!r} on line "
                f"{key_node.start_mark.line + 1} (first on line {seen[key]})")
        seen[key] = key_node.start_mark.line + 1
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_DuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_without_duplicates)


def _config_files() -> list[Path]:
    out = []
    for root in SCAN_ROOTS:
        for path in (REPO_ROOT / root).rglob("*.y*ml"):
            if path.is_file() and not SKIP_PARTS & set(path.parts):
                out.append(path)
    return sorted(out)


def test_no_yaml_file_has_a_duplicate_key():
    files = _config_files()
    # ~200 with further plugin roots, ~114 with src/plugins alone.
    least = 100 if PRIVATE_ROOTS else 80
    assert len(files) >= least, (
        f"only {len(files)} yaml files found -- the scanner no longer sees the "
        f"tree, the test would be vacuous")

    duplicates, parsed = [], 0
    for path in files:
        try:
            yaml.load(path.read_text(encoding="utf-8"), Loader=_DuplicateKeyLoader)
            parsed += 1
        except ValueError as exc:
            duplicates.append(f"{path.relative_to(REPO_ROOT)}: {exc}")
        except yaml.YAMLError:
            # Not every .yaml in the tree is valid YAML (fixtures, templates
            # with placeholders). Those do not count as checked.
            pass

    assert parsed >= (100 if PRIVATE_ROOTS else 60), (
        f"only {parsed} of {len(files)} files parsed -- the test no longer "
        f"measures what it should")
    assert not duplicates, (
        "duplicate YAML keys — the last one wins, the first is "
        "silently gone:\n  " + "\n  ".join(duplicates))


#: A stategraph template that is one machine param and nothing else: ``{{ params.model }}``.
_PARAM_TEMPLATE = re.compile(r"\{\{\s*params\.(\w+)\s*\}\}")


def _profile_names(value: str, machine_params: dict | None) -> list[str]:
    """The profile names a string stands for. In a stategraph machine a template naming one param
    stands for that param's ``enum`` (the values a run may pass); any other template is skipped --
    stategraph checks those itself when the machine loads (SG007)."""
    if machine_params is None or "{{" not in value:
        return [value]
    param = _PARAM_TEMPLATE.fullmatch(value.strip())
    spec = machine_params.get(param.group(1)) if param else None
    enum = spec.get("enum") if isinstance(spec, dict) else None
    return [v for v in enum if isinstance(v, str)] if isinstance(enum, list) else []


def _profile_references(node, where: str, out: list[tuple[str, str]],
                        machine_params: dict | None = None) -> None:
    """Collect every place that names an llm_profile NAME.

    Only strings and string lists count. In the plugins' ``schema.yaml``,
    ``llm_profile`` is a declared config FIELD — its value there is a mapping
    (``type``/``default``/``enum``), not a profile name.

    ``machine_params``: the ``params`` of a stategraph machine (``{}`` without
    any), None for any other file. In a machine ``llm_params`` is a flat param
    map wherever an activity sits (``do``, ``finally``, ``resources``;
    stategraph format.md), never keyed by profile; a machine under
    ``machines:`` has params of its own.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("llm_profile", "llm_profile_advanced",
                       "llm_profile_escalation"):
                if isinstance(value, str):
                    out.extend((v, f"{where}.{key}") for v in _profile_names(value, machine_params))
                elif isinstance(value, list):
                    out.extend((name, f"{where}.{key}") for v in value if isinstance(v, str)
                               for name in _profile_names(v, machine_params))
                else:
                    _profile_references(value, f"{where}.{key}", out, machine_params)
            elif key == "llm_params" and isinstance(value, dict) and machine_params is None:
                # Profile-keyed overrides: the KEY is the profile name.
                out.extend((k, f"{where}.llm_params[{k!r}]")
                           for k in value if k != "*")
            elif key == "machines" and machine_params is not None and isinstance(value, dict):
                for name, child in value.items():
                    params = child.get("params") if isinstance(child, dict) else None
                    _profile_references(child, f"{where}.machines.{name}", out,
                                        params if isinstance(params, dict) else {})
            else:
                _profile_references(value, f"{where}.{key}", out, machine_params)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _profile_references(value, f"{where}[{i}]", out, machine_params)


def _machine_params(data) -> dict | None:
    """A stategraph machine's ``params`` (``{}`` without any), or None when the file is no machine."""
    if not isinstance(data, dict) or "stategraph" not in data:
        return None
    params = data.get("params")
    return params if isinstance(params, dict) else {}


def test_the_profile_collector_reads_stategraph_do_blocks():
    """A machine's templated profile is checked by its param's enum -- a nested machine's by its
    own -- its llm_params (do, finally) are no profile names; elsewhere both stay as strict as before."""
    machine = {"stategraph": 1, "params": {"model": {"type": "string", "enum": ["or-real", "or-typo"]}},
               "finally": {"agent": "x", "llm_params": {"service_tier": "flex"}},
               "states": {"a": {"do": {"agent": "x", "llm_profile": "{{ params.model }}",
                                       "llm_params": {"service_tier": "flex"}}},
                          "b": {"do": {"agent": "x", "llm_profile": "{{ ctx.chosen }}"}}},
               "machines": {"inner": {"params": {"model": {"enum": ["or-inner"]}},
                                      "states": {"c": {"do": {"agent": "x", "llm_profile": "{{ params.model }}"}}}}}}
    found: list[tuple[str, str]] = []
    _profile_references(machine, "m", found, _machine_params(machine))
    assert sorted(name for name, _ in found) == ["or-inner", "or-real", "or-typo"], found
    agent = {"plugins": {"servers": {"a": {"agent_config": {
        "llm_profile": "{{ params.model }}", "llm_params": {"or-keyed": {}, "*": {}},
        "do": {"llm_params": {"or-under-do": {}}}}}}}}
    found = []
    _profile_references(agent, "c", found, _machine_params(agent))
    assert sorted(name for name, _ in found) == ["or-keyed", "or-under-do", "{{ params.model }}"], found


def test_every_profile_reference_resolves():
    from agent_system.config.settings import load_settings

    known = set(load_settings().llm_system.profiles)
    assert len(known) >= 20, (
        f"only {len(known)} profiles loaded — the real configuration did not "
        f"arrive, the test would be vacuous")

    references: list[tuple[str, str]] = []
    for path in _config_files():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        _profile_references(data, str(path.relative_to(REPO_ROOT)), references, _machine_params(data))

    # Far over 200 with further plugin roots, ~91 with src/plugins alone.
    assert len(references) >= (200 if PRIVATE_ROOTS else 50), (
        f"only {len(references)} profile references found -- the collector no "
        f"longer reaches them, the test would be vacuous")

    dangling = sorted({(name, where) for name, where in references
                       if name not in known})
    assert not dangling, (
        "these profile names do not exist (typo or renamed) — as a fallback "
        "entry this may never show up in operation:\n  "
        + "\n  ".join(f"{name!r} <- {where}" for name, where in dangling))


def test_every_allowed_sub_agent_exists():
    """A sub-agent manager may only hand out names that resolve.

    Same failure mode as a dangling profile reference, one layer up: the
    allowlist is not validated at load time, so a deleted or renamed agent
    stays in the list and only fails when something tries to spawn it —
    which, for a rarely used agent, may be never. Found this way:
    `v4_beat_writer_v2`, deleted 2026-08-23 but still listed, and
    `parallel_thinking_agent`, which never existed.
    """
    from agent_system.config.settings import load_settings

    servers = load_settings().plugins.servers
    known = set(servers)
    assert len(known) >= 100, (
        f"only {len(known)} plugin entries — the real configuration did not "
        f"arrive, the test would be vacuous")

    references: list[tuple[str, str]] = []
    for name, server in servers.items():
        dumped = server.model_dump()
        allowed = (dumped.get("allowed_agents")
                   or (dumped.get("config") or {}).get("allowed_agents") or [])
        if isinstance(allowed, list):
            references += [(str(a), name) for a in allowed if isinstance(a, str)]

    assert len(references) >= 20, (
        f"only {len(references)} allowlist entries collected — the collector "
        f"no longer reaches them, the test would be vacuous")

    dangling = sorted({(agent, where) for agent, where in references
                       if agent not in known})
    assert not dangling, (
        "these sub-agents are allowed but not registered — a spawn fails at "
        "runtime, and nothing says so before:\n  "
        + "\n  ".join(f"{agent!r} <- {where}" for agent, where in dangling))


@pytest.mark.parametrize("bad_yaml,expected", [
    ("a: 1\na: 2\n", "duplicate key"),
    ("top:\n  x: 1\n  x: 2\n", "duplicate key"),
])
def test_the_duplicate_detector_actually_detects(bad_yaml, expected):
    """The detector itself — otherwise the scanner stays green because it sees nothing."""
    with pytest.raises(ValueError, match=expected):
        yaml.load(bad_yaml, Loader=_DuplicateKeyLoader)


class TestStaleLlmParamKeysAreDroppedLoudly:
    """A key whose profile is in none of the agent's chains configures nothing.
    It must not become silently ineffective — and it must not cost EVERY server
    its start either, which is what it did until 2026-08-22: one role rename in
    one file, and no service came up."""

    STALE = {"llm_profile": ["a", "b"],
             "llm_params": {"*": {"max_tokens": 100},
                            "gone": {"thinking_level": "high"}}}

    def test_direct_construction_still_raises(self):
        from pydantic import ValidationError

        from agent_system.config.models import AgentConfig
        with pytest.raises(ValidationError, match="profile keys"):
            AgentConfig.model_validate(self.STALE)

    def test_the_config_load_drops_it_and_keeps_the_rest(self):
        from agent_system.config.models import AgentConfig

        cfg = AgentConfig.model_validate(
            self.STALE, context={"drop_stale_llm_params": True})
        assert "gone" not in cfg.llm_params, "stale key survived"
        assert cfg.llm_params["*"] == {"max_tokens": 100}, "valid params taken down with it"

    def test_load_settings_survives_it_and_names_the_agent(self, tmp_path, caplog):
        """The wiring: without the context in the real load path, the same key
        would abort the whole start again."""
        from agent_system.config.settings import load_settings

        (tmp_path / "config.yaml").write_text(
            "plugins:\n"
            "  servers:\n"
            "    kaputt_agent:\n"
            "      type: agent\n"
            "      enabled: true\n"
            "      agent_config:\n"
            "        llm_profile: [a, b]\n"
            "        llm_params:\n"
            "          \"weg\":\n"
            "            thinking_level: high\n",
            encoding="utf-8")

        with caplog.at_level(logging.ERROR):
            cfg = load_settings(str(tmp_path / "config.yaml"))

        assert cfg.plugins.servers["kaputt_agent"].agent_config.llm_params == {}
        assert any("kaputt_agent" in r.getMessage() for r in caplog.records), \
            "the loss went unreported — which is exactly what makes it silent"

    def test_inheritance_does_not_move_the_abort_to_agent_creation(self, tmp_path, caplog):
        """The second validation. A child that overrides the chains inherits
        the parent's keyed params, so the key only turns stale AFTER the merge
        — in get_tool_server_config, not at load time. Tolerating it only at
        load would move the abort from startup to the first spawn."""
        from agent_system.config.settings import (
            _reported_stale_llm_params,
            get_tool_server_config,
            load_settings,
        )

        (tmp_path / "config.yaml").write_text(
            "plugins:\n"
            "  servers:\n"
            "    parent_agent:\n"
            "      type: agent\n"
            "      enabled: true\n"
            "      agent_config:\n"
            "        llm_profile: [a, b]\n"
            "        llm_params:\n"
            "          \"b\":\n"
            "            thinking_level: high\n"
            "    child_agent:\n"
            "      type: parent_agent\n"
            "      enabled: true\n"
            "      agent_config:\n"
            "        llm_profile: [c, d]\n",
            encoding="utf-8")

        cfg = load_settings(str(tmp_path / "config.yaml"))
        _reported_stale_llm_params.clear()   # the load pass may have reported already
        with caplog.at_level(logging.ERROR):
            merged = get_tool_server_config("child_agent", cfg)

        assert merged is not None, "agent creation died on an inherited stale key"
        assert merged.agent_config.llm_params == {}
        assert any("child_agent" in r.getMessage() for r in caplog.records), \
            "dropped without a word at the very place the message warns about"

    INHERITING = ("plugins:\n"
                  "  servers:\n"
                  "    parent_agent:\n"
                  "      type: agent\n"
                  "      enabled: true\n"
                  "      agent_config:\n"
                  "        llm_profile: [a, b]\n"
                  "    child_agent:\n"
                  "      type: parent_agent\n"
                  "      enabled: true\n"
                  "      agent_config:\n"
                  "        llm_params:\n"
                  "          \"*\":\n"
                  "            thinking_level: high\n"
                  "          \"a\":\n"
                  "            max_tokens: 120000\n"
                  "          \"{extra}\":\n"
                  "            max_tokens: 7\n")

    def test_a_child_that_inherits_its_chain_keeps_its_keyed_params(self, tmp_path, caplog):
        """The other direction of inheritance, and the one that went silent.

        The child sets no chain and keys its params to a profile of the chain
        it inherits. The load validated it against the model default instead,
        dropped the key as stale, and the merge afterwards had nothing to keep:
        v4_beat_scorer's output cap was gone from get_tool_server_config, with
        a warning naming the wrong chain. Here `b` is a profile of the parent's
        chain too, so the child's third key is valid as well.
        """
        from agent_system.config.settings import (
            _reported_stale_llm_params,
            get_tool_server_config,
            load_settings,
        )

        (tmp_path / "config.yaml").write_text(self.INHERITING.format(extra="b"), encoding="utf-8")
        _reported_stale_llm_params.clear()
        with caplog.at_level(logging.ERROR):
            cfg = load_settings(str(tmp_path / "config.yaml"))
            merged = get_tool_server_config("child_agent", cfg)

        assert merged.agent_config.available_llm_profiles[:2] == ["a", "b"], (
            "fixture is vacuous: the child did not inherit the parent's chain")
        assert merged.agent_config.llm_params.get("a") == {"max_tokens": 120000}, (
            f"the key for the inherited chain was dropped: {merged.agent_config.llm_params}")
        assert not any("child_agent" in r.getMessage() for r in caplog.records), (
            "a valid key was reported as stale -- against a chain that is not the child's")

    MERGING = ("llm_system:\n"
               "  models:\n"
               "    m:\n"
               "      provider: openai\n"
               "      model: probe\n"
               "  profiles:\n"
               "    a: {{model_ref: m}}\n"
               "    b: {{model_ref: m}}\n"
               "    c: {{model_ref: m}}\n"
               "plugins:\n"
               "  default_config:\n"
               "    type: agent\n"
               "    agent_config:\n"
               "      llm_profile: [a, b]\n"
               "  servers:\n"
               "    child_agent:\n"
               "      type: agent\n"
               "      enabled: true\n"
               "      agent_config:\n"
               "        llm_profile: [\"+{added}\"]\n"
               "        llm_params:\n"
               "          \"{added}\":\n"
               "            max_tokens: 7\n"
               "          \"a\":\n"
               "            max_tokens: 8\n")

    def test_a_chain_that_adds_a_profile_keeps_the_params_keyed_to_it(self, tmp_path, caplog):
        """`llm_profile: ["+c"]` on an agent without a parent agent merges c into
        default_config's chain. The load judged the keys against the raw "+c" --
        no "c", no inherited "a" -- and dropped them, and the merge reads the
        loaded entry: the agent ran without the params written for it. (Under a
        parent agent the merge reads the raw entry again, which hid it.)"""
        from agent_system.config.settings import (
            _reported_stale_llm_params,
            get_tool_server_config,
            load_settings,
        )

        (tmp_path / "config.yaml").write_text(self.MERGING.format(added="c"), encoding="utf-8")
        _reported_stale_llm_params.clear()
        with caplog.at_level(logging.ERROR):
            cfg = load_settings(str(tmp_path / "config.yaml"))
            merged = get_tool_server_config("child_agent", cfg)

        assert merged.agent_config.available_llm_profiles[:3] == ["a", "b", "c"], (
            "fixture is vacuous: the chain did not merge")
        params = merged.agent_config.llm_params or {}
        assert params.get("c") == {"max_tokens": 7}, f"the added profile's params were dropped: {params}"
        assert params.get("a") == {"max_tokens": 8}, f"the inherited profile's params were dropped: {params}"
        assert not any("child_agent" in r.getMessage() for r in caplog.records), (
            "an added profile that exists was reported: "
            + "; ".join(r.getMessage() for r in caplog.records))

    def test_an_unknown_added_profile_is_named_but_does_not_stop_the_agent(self, tmp_path, caplog):
        """`"+zzz"` adds a fallback nobody configured: that is worth an error, but
        the primary is the inherited one -- "THE AGENT WILL NOT START" was false."""
        from agent_system.config.settings import load_settings

        (tmp_path / "config.yaml").write_text(self.MERGING.format(added="zzz"), encoding="utf-8")
        with caplog.at_level(logging.ERROR):
            load_settings(str(tmp_path / "config.yaml"))

        said = [r.getMessage() for r in caplog.records if "child_agent" in r.getMessage()]
        assert said and "'zzz'" in said[0], f"the unknown profile went unnamed: {said}"
        assert "WILL NOT START" not in said[0], said[0]

    def test_a_string_chain_is_a_name_not_a_merge(self, tmp_path, caplog):
        """`llm_profile: "+c"` is a string: nothing merges it, the agent's primary
        is the profile "+c" -- which does not exist. Reading it as c hid that."""
        from agent_system.config.settings import load_settings

        text = self.MERGING.format(added="c").replace('llm_profile: ["+c"]', 'llm_profile: "+c"')
        assert 'llm_profile: "+c"' in text, "fixture did not change"
        (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
        with caplog.at_level(logging.ERROR):
            load_settings(str(tmp_path / "config.yaml"))

        said = [r.getMessage() for r in caplog.records if "child_agent" in r.getMessage()]
        assert any("'+c'" in m and "WILL NOT START" in m for m in said), f"the dead primary went unnamed: {said}"

    def test_a_child_that_inherits_its_chain_still_loses_a_stale_key_loudly(self, tmp_path, caplog):
        """Deferring the judgement must not drop it: a key in none of the
        inherited chains is still stale, and still named -- only now where
        the chain is known."""
        from agent_system.config.settings import (
            _reported_stale_llm_params,
            get_tool_server_config,
            load_settings,
        )

        (tmp_path / "config.yaml").write_text(self.INHERITING.format(extra="zzz"), encoding="utf-8")
        _reported_stale_llm_params.clear()
        with caplog.at_level(logging.ERROR):
            cfg = load_settings(str(tmp_path / "config.yaml"))
            merged = get_tool_server_config("child_agent", cfg)

        assert "zzz" not in merged.agent_config.llm_params, "a stale key survived the merge"
        assert merged.agent_config.llm_params.get("a") == {"max_tokens": 120000}, (
            "the valid keys went down with the stale one")
        assert any("child_agent" in r.getMessage() and "zzz" in r.getMessage()
                   for r in caplog.records), "the stale key was dropped without a word"

    def test_no_real_agent_has_a_stale_profile_key(self):
        """The anti-drift half of the stale-key rule, on the REAL config.

        Every shipped server through load_settings and get_tool_server_config
        -- the path an agent is built on, inheritance included -- and not one
        profile key that its chains do not contain. The per-file scan in
        test_agent_config_rejects_unknown_keys used to carry this and could
        not: a file read on its own does not know a chain it inherits.

        With a control first: one real server given a key no chain has must
        be reported, or a reporting that stopped reporting would pass this
        with nothing measured.
        """
        from agent_system.config.settings import (
            _reported_stale_llm_params,
            get_tool_server_config,
            load_settings,
        )

        control = load_settings()
        name, server = next((n, s) for n, s in control.plugins.servers.items()
                            if getattr(s, "agent_config", None) is not None
                            and not s.agent_config.llm_params)
        server.agent_config.llm_params = {"zzz_in_no_chain": {"max_tokens": 1}}
        _reported_stale_llm_params.clear()
        get_tool_server_config(name, control)
        assert any(n == name for n, _ in _reported_stale_llm_params), (
            f"control failed: a stale key on {name!r} went unreported, so this "
            f"test would measure nothing")

        cfg = load_settings()
        _reported_stale_llm_params.clear()
        servers = list(cfg.plugins.servers)
        for name in servers:
            get_tool_server_config(name, cfg)
        assert len(servers) > 100, f"only {len(servers)} servers loaded -- the scan went blind"
        stale = sorted(_reported_stale_llm_params, key=lambda m: m[0])
        assert not stale, "profile keys in none of the agent's chains:\n  " + "\n  ".join(
            f"{n}: {sorted(keys)}" for n, keys in stale)

    def test_it_reaches_the_logfile_even_though_the_config_loads_first(self, tmp_path):
        """Every entry point loads the config BEFORE configuring logging, so an
        error raised during the load has no handler to go to and never reaches
        the file the operator reads. setup_logging replays it."""
        import agent_system.config.llm_checks as llm_checks
        import agent_system.config.settings as settings_mod
        from agent_system.utils.logging import setup_logging

        root = logging.getLogger()
        saved = root.handlers[:]
        for handler in saved:
            root.removeHandler(handler)
        llm_checks._deferred_config_errors.clear()
        llm_checks._reported_stale_llm_params.clear()
        log_file = tmp_path / "agent.log"
        try:
            (tmp_path / "config.yaml").write_text(
                "plugins:\n"
                "  servers:\n"
                "    late_agent:\n"
                "      type: agent\n"
                "      enabled: true\n"
                "      agent_config:\n"
                "        llm_profile: [a, b]\n"
                "        llm_params:\n"
                "          \"gone\":\n"
                "            thinking_level: high\n",
                encoding="utf-8")
            settings_mod.load_settings(str(tmp_path / "config.yaml"))
            assert llm_checks._deferred_config_errors, \
                "nothing was kept for replay — the error is lost with the handlers"
            setup_logging(enabled=True, level="INFO", file_path=str(log_file),
                          rotation_enabled=False)
        finally:
            for handler in root.handlers[:]:
                handler.close()
                root.removeHandler(handler)
            for handler in saved:
                root.addHandler(handler)

        assert "late_agent" in log_file.read_text(encoding="utf-8"), \
            "the error never made it into the logfile"
