"""An unknown keyword on AgentConfig must be loud, not silent.

Pydantic's default is `extra='ignore'`. Under that default,
`AgentConfig(default_llm_profile="turbo")` dropped the keyword without a word
— `default_llm_profile` is a read-only property, not a field — and two plugins
built their LLM that way, running for months on a model nobody chose. The
misconfiguration was invisible precisely because nothing failed.

Measured before the switch: zero agent_config blocks in config/ or src/ carry
an unknown key, so no real deployment is refused. The 28 places that DID pass
unknown keywords were all test fixtures believing they configured something.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_system.config.models import AgentConfig


class TestUnknownKeysAreRejected:
    def test_the_historic_bug_now_fails_at_construction(self):
        with pytest.raises(ValidationError, match="default_llm_profile"):
            AgentConfig(default_llm_profile="turbo")

    def test_a_typo_fails_at_construction(self):
        with pytest.raises(ValidationError, match="llm_profil"):
            AgentConfig(llm_profil="turbo")

    def test_known_fields_still_work(self):
        """Counter-check: the rejections above must come from the unknown key,
        not from a model that rejects everything."""
        config = AgentConfig(llm_profile="turbo", max_steps=5)
        assert config.llm_profile == "turbo"
        assert config.default_llm_profile == "turbo", \
            "the property must keep deriving from llm_profile"

    def test_every_real_agent_yaml_still_loads(self):
        """The measurement that justified the switch, pinned: no shipped
        agent_config block carries an unknown key. If one ever should, this
        names the file instead of letting bootstrap explode."""
        import yaml

        import re

        rejected = []
        scanned = 0
        declaring: set[Path] = set()   # files whose TEXT declares an agent_config
        reached: set[Path] = set()     # files where the walk actually parsed one
        for root in ("config", "src"):
            for path in (Path(__file__).parents[2] / root).rglob("*.yaml"):
                if "docs" in path.parts:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except Exception:
                    continue
                if re.search(r"^\s*agent_config\s*:", text, re.M):
                    declaring.add(path)
                try:
                    data = yaml.safe_load(text)
                except Exception:
                    continue

                def walk(node):
                    nonlocal scanned
                    if isinstance(node, dict):
                        block = node.get("agent_config")
                        if isinstance(block, dict):
                            scanned += 1
                            reached.add(path)
                            try:
                                # Unknown FIELDS are this test's question. The
                                # profile keys of llm_params are not: they are
                                # judged against the agent's chains, and a file
                                # read on its own does not have them when the
                                # chain is inherited through `type:`. Judged
                                # here, a child keying its params to its
                                # parent's chain was rejected although the
                                # loader runs it correctly, and the only way to
                                # pass was a copy of the parent's chain, free to
                                # drift from it. They are judged where the chain
                                # is known: test_no_real_agent_has_a_stale_
                                # profile_key, through the real load path.
                                AgentConfig.model_validate(
                                    block, context={"drop_stale_llm_params": True})
                            except ValidationError as exc:
                                rejected.append(f"{path}: {exc}")
                        for value in node.values():
                            walk(value)
                    elif isinstance(node, list):
                        for value in node:
                            walk(value)

                walk(data)

        # The instrument checks itself against the repo instead of against a
        # number: every file that DECLARES an agent_config must also have been
        # parsed into one. A walk that stops descending, or a file that fails to
        # parse and is skipped, shows up here as a difference -- while agents
        # being added or deleted (v5b and the old book_architect generation went
        # on 04.09.2026, 207 blocks -> 124) does not age the test.
        assert declaring, "no agent_config anywhere in config/ or src/ — the scan went blind"
        assert declaring == reached, (
            "the scan did not reach every file that declares an agent_config:\n  "
            + "\n  ".join(str(p) for p in sorted(declaring ^ reached)))
        assert scanned >= len(declaring), f"{scanned} blocks from {len(declaring)} files"
        assert not rejected, "\n".join(rejected)
