# Contributing to ScarabHive

Thanks for taking the time to contribute. This guide covers the development
setup, how to run the checks CI runs, and what a pull request should contain.

Security issues are not reported through issues or pull requests; see
[SECURITY.md](SECURITY.md).

## Development setup

ScarabHive needs Python 3.11 or newer; 3.12 is recommended (the Docker image
uses it; CI tests 3.11, 3.12 and 3.14). The server resolves `config/`, `templates/` and
`static/` relative to the checkout, so install it in **editable** mode.

```bash
git clone https://github.com/eehrich/ScarabHive.git
cd ScarabHive
python -m venv .venv
source .venv/bin/activate        # Windows (Git Bash): source .venv/Scripts/activate
pip install -U pip
pip install -e ".[dev,test]"
```

Two things to know about a fresh install:

- **Cairo, for SVG layers.** `pycairo` (reportlab's cairo backend, which draws
  the `image_compose` plugin's SVG layers) publishes wheels for Windows only and
  is compiled everywhere else, so it is not part of `pip install -e .`: it sits
  in `requirements/optional.txt`. To work on SVG layers, install the headers and
  then that file:
  Debian/Ubuntu `sudo apt-get install build-essential libcairo2-dev pkg-config`
  (plus `python3.X-dev` if your Python 3.X lacks its headers),
  macOS `brew install cairo pkg-config`, then
  `pip install -r requirements/optional.txt`. Without it the tests that draw an
  SVG skip.
- **PyTorch.** Only `requirements/private.txt` (further plugin roots, not part
  of this release) pulls in `torch`. On Linux x86_64 the PyPI build brings
  several GB of CUDA libraries. If you do not need a GPU, install the CPU build
  before the rest:
  `pip install --index-url https://download.pytorch.org/whl/cpu torch torchaudio`.

Dependencies are aggregated: `pyproject.toml` reads `requirements/all.txt`,
which `scripts/aggregate_plugin_deps.py` generates from `requirements/core.txt`
and every plugin's `plugin.toml`; a plugin's `optional_dependencies` go to
`requirements/optional.txt`, which the install scripts install best effort.
Never edit either file by hand.

API keys go into `config/secrets.env` (if it does not exist yet, copy
`config/secrets.env.example` and uncomment the keys you use) or into the
environment; a real environment variable wins over the file. The file
is read by the config loader and must never be committed. Start the server
with `agent-api` (web UI on <http://127.0.0.1:8000>) or use `agent-cli`.
[docs/configuration.md](docs/configuration.md) covers configuration in detail.

## Tests

The full suite takes 20 minutes and more. **Run the tests for what you
changed**, not the whole suite:

```bash
python -m pytest tests/config -q
python -m pytest tests/llm/test_llm_pricing.py -q
python -m pytest src/plugins/file_ops/tests -q
python -m pytest tests/session -q -k "archive"
```

Things to know before you run them:

- **pytest cleans up only after itself.** The root `conftest.py` ends only
  processes that carry its own session's marker (children its tests started),
  and orphans of test sessions that provably ended -- never an `agent-api` you
  started or another project's processes. A test that starts a pytest of its
  own sets `AGENT_SYSTEM_TEST_NO_REAP=1` for that run.
- **Warnings are errors** (`pytest.ini`). The Python 3.14 deprecations that
  fastapi, starlette, chromadb and google-genai raise are ignored there, limited to
  those modules; the same deprecated call in this code base still fails
  (outside the test modules that ignore it themselves, such as those using the
  stategraph test kit's `FASTAPI_PY314` marker).
- **No real LLM calls.** `conftest.py` replaces the LLM client factory with a
  fake client. A test must not need API keys or the network.
- Tests for a plugin live next to it in `src/plugins/<name>/tests/`
  (`test_plugin_<name>_*.py`); framework tests live under `tests/<area>/`.
- A new test should fail without your change. Break the code it covers once
  and check that the test goes red; a test that stays green then checks
  nothing.

CI runs a fixed, network-free subset of the suite. The list is at the top of
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) (`CI_TEST_PATHS`); run
the same command locally before you open a pull request that touches the core.

## Lint and type checks

CI runs ruff on the whole tree with the rule set named in `pyproject.toml`
(`[tool.ruff.lint]`), so the local command is the same:

```bash
ruff check .
```

Mypy is reported in CI but does not fail the build yet, because the code base
still has several hundred errors:

```bash
mypy src/agent_system src/plugins
```

Please do not add new mypy errors in the files you change.

## Generated files

Some files are generated, and a test fails when they are stale. Regenerate them
in the same pull request:

| After changing | Run | Guarded by |
| --- | --- | --- |
| the config models in `src/agent_system/config/models.py` | `python src/scripts/generate_config_schemas.py` (updates `schemas/*.json`) | `tests/config/test_config_schemas.py` |
| `requirements/core.txt` or a plugin's `dependencies` in `plugin.toml` | `python scripts/aggregate_plugin_deps.py` (updates `requirements/all.txt`) | `tests/pluginsystem/test_plugin_deps_aggregation.py` |

## Writing plugins

Most contributions are plugins. Start here instead of reading the core:

- [`src/plugins/CONTRIBUTING.md`](src/plugins/CONTRIBUTING.md) -- the short
  checklist (folder layout, `plugin.toml`, tests, enabling a plugin)
- [`docs/plugin_authoring.md`](docs/plugin_authoring.md) -- the full guide
- [`docs/plugin_hooks.md`](docs/plugin_hooks.md) -- lifecycle hooks
- `python src/scripts/validate_plugin.py <plugin dir>` checks a manifest

For changes to the framework itself, read
[`docs/_arch_agent_system_architecture.md`](docs/_arch_agent_system_architecture.md)
first, and [`docs/_arch_plugin_architecture.md`](docs/_arch_plugin_architecture.md)
for the plugin system.

## Code style

- Code, comments, docstrings, log and assertion messages are in English.
- Keep changes focused. A pull request that fixes one thing is reviewed
  faster than one that also reformats the files around it.
- When a change alters a config model, a plugin contract or a tool schema,
  update its callers, the agent YAMLs that use it and the docs in the same
  pull request.

## Commit messages

English, one subject line naming the area first, then what holds after the
change, in the present tense:

```text
hooks: a hook knows whose call it is
stategraph panel: breakpoints first in the state inspector
task_switch, sub_agent_manager: write a session in turn with its saves
```

Use the body for the *why*: the bug or measurement that motivated the change,
and anything a reviewer would otherwise have to rediscover.

## Pull requests

Fill in the pull request template: what changed and why, which tests you ran,
the ruff result, and whether docs, schemas or generated files needed updating.
Link the issue it addresses, if there is one.

## License

ScarabHive is licensed under the [Apache License 2.0](LICENSE). By submitting a
contribution you agree that it is licensed under the same terms, as described
in section 5 of the license.
