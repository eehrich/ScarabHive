## What and why

<!-- What does this change, and why is it needed? Link the issue it addresses (e.g. "Fixes #123"). -->

## How it was tested

<!-- The targeted tests you ran (not the whole suite), and anything you checked by hand. -->

```text
python -m pytest <paths> -q
```

- [ ] New or changed behaviour has a test, and the test fails without the change.

## Checklist

- [ ] `ruff check .` reports nothing (the rule set is in `pyproject.toml`).
- [ ] No new mypy errors in the files I changed (`mypy src/agent_system src/plugins`).
- [ ] Config models changed: schemas regenerated (`python src/scripts/generate_config_schemas.py`).
- [ ] Plugin or core dependencies changed: `python scripts/aggregate_plugin_deps.py` run.
- [ ] Docs, plugin README and agent YAMLs updated where the behaviour changed.
- [ ] No secrets, private host names or personal data in code, config, tests or logs.
- [ ] Commit messages follow CONTRIBUTING.md.
