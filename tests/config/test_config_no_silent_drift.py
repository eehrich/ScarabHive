"""Zwei Zusicherungen gegen still verschluckte Konfiguration.

Beide Prüfungen fanden je zwei Defekte, die keiner der bestehenden Tests
bemerkt hat — weil ein verschluckter Wert nichts kaputt macht, das ein Test
ansieht: die Anwendung startet, die Tabelle ist da, nur der Schalter fehlt.

**Doppelte YAML-Schlüssel.** ``yaml.safe_load`` behält wortlos den letzten.
So verschwand ``mode: pipeline`` aus ``repair_pipeline.yaml`` (der Agent kam
als reiner Tool-Server hoch statt als Orchestrator), ein kompletter
Tool-Server aus ``mcp_servers.yaml`` (sein Block landete in ``localhost`` und
überschrieb dessen URL), und eine Parameterbeschreibung aus
``json_store/schema.yaml``.

**Profilreferenzen ins Leere.** Ein Profil, das es nicht gibt, ist kein
Fehler beim Laden — es fällt erst zur Laufzeit auf, und als Fallback-Glied
womöglich nie. So blieb ``or-gmini-flash-unlimited`` (ein fehlendes „e")
unbemerkt und nahm zwei Panel-Agenten ihren Fallback.

Beide Tests sichern ihre GRUNDMENGE ab: eine leere Menge erfüllt jede
All-Aussage, ein Scanner der nichts mehr findet wäre sonst von einem
funktionierenden nicht zu unterscheiden.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[2]
#: Further plugin roots (src/plugins_<name>/) are not part of the open-source checkout; the fixture
#: counts below are calibrated for both.
PRIVATE_ROOTS = any(d.is_dir() and d.name.isidentifier() for d in (REPO_ROOT / "src").glob("plugins_*"))
sys.path.insert(0, str(REPO_ROOT / "src"))

#: Wo Konfiguration lebt. `docs` ist ausgenommen — dort stehen Beispiele.
SCAN_ROOTS = ("config", "src")

#: Verzeichnisse, die nie Betriebs-Konfiguration enthalten.
SKIP_PARTS = frozenset({"docs", "node_modules", "__pycache__", ".venv"})


class _DuplicateKeyLoader(yaml.SafeLoader):
    """SafeLoader, der doppelte Mapping-Schluessel meldet statt sie zu schlucken."""


def _mapping_without_duplicates(loader, node, deep=False):
    seen: dict = {}
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError:
            # YAML erlaubt zusammengesetzte Schluessel (`? {a: b}`). Selten,
            # aber sie kollidieren genauso — ueber ihre Darstellung vergleichen.
            key = repr(key)
        if key in seen:
            raise ValueError(
                f"doppelter Schluessel {key!r} in Zeile "
                f"{key_node.start_mark.line + 1} (zuerst in Zeile {seen[key]})")
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
            # Nicht jede .yaml im Baum ist gueltiges YAML (Fixtures, Vorlagen
            # mit Platzhaltern). Die zaehlen nicht als geprueft.
            pass

    assert parsed >= (100 if PRIVATE_ROOTS else 60), (
        f"only {parsed} of {len(files)} files parsed -- the test no longer "
        f"measures what it should")
    assert not duplicates, (
        "doppelte YAML-Schluessel — der letzte gewinnt, der erste ist "
        "wortlos weg:\n  " + "\n  ".join(duplicates))


def _profile_references(node, where: str, out: list[tuple[str, str]]) -> None:
    """Jede Stelle einsammeln, die einen llm_profile-NAMEN nennt.

    Nur Strings und String-Listen zaehlen. In den ``schema.yaml`` der Plugins
    ist ``llm_profile`` ein deklariertes Config-FELD — sein Wert ist dort ein
    Mapping (``type``/``default``/``enum``), kein Profilname.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("llm_profile", "llm_profile_advanced",
                       "llm_profile_escalation"):
                if isinstance(value, str):
                    out.append((value, f"{where}.{key}"))
                elif isinstance(value, list):
                    out.extend((v, f"{where}.{key}")
                               for v in value if isinstance(v, str))
                else:
                    _profile_references(value, f"{where}.{key}", out)
            elif key == "llm_params" and isinstance(value, dict):
                # Profil-gekeyte Overrides: der SCHLUESSEL ist der Profilname.
                out.extend((k, f"{where}.llm_params[{k!r}]")
                           for k in value if k != "*")
            else:
                _profile_references(value, f"{where}.{key}", out)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _profile_references(value, f"{where}[{i}]", out)


def test_every_profile_reference_resolves():
    from agent_system.config.settings import load_settings

    known = set(load_settings().llm_system.profiles)
    assert len(known) >= 20, (
        f"nur {len(known)} Profile geladen — die echte Konfiguration kam "
        f"nicht an, der Test waere gegenstandslos")

    references: list[tuple[str, str]] = []
    for path in _config_files():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        _profile_references(data, str(path.relative_to(REPO_ROOT)), references)

    # Far over 200 with further plugin roots, ~91 with src/plugins alone.
    assert len(references) >= (200 if PRIVATE_ROOTS else 50), (
        f"only {len(references)} profile references found -- the collector no "
        f"longer reaches them, the test would be vacuous")

    dangling = sorted({(name, where) for name, where in references
                       if name not in known})
    assert not dangling, (
        "diese Profilnamen gibt es nicht (Tippfehler oder umbenannt) — als "
        "Fallback-Glied faellt das im Betrieb womoeglich nie auf:\n  "
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
    ("a: 1\na: 2\n", "doppelter Schluessel"),
    ("top:\n  x: 1\n  x: 2\n", "doppelter Schluessel"),
])
def test_the_duplicate_detector_actually_detects(bad_yaml, expected):
    """Der Detektor selbst — sonst gruent der Scanner, weil er nichts sieht."""
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
        import agent_system.config.settings as settings_mod
        from agent_system.utils.logging import setup_logging

        root = logging.getLogger()
        saved = root.handlers[:]
        for handler in saved:
            root.removeHandler(handler)
        settings_mod._deferred_config_errors.clear()
        settings_mod._reported_stale_llm_params.clear()
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
            assert settings_mod._deferred_config_errors, \
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
