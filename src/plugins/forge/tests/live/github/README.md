# GitHub test repo for the live tests

A private throwaway repo with an Actions workflow; one account plays both
sides (creates issues, comments, works them off as the bot).

## Setup

```sh
gh repo create <account>/scarabhive-forge-live --private --add-readme
cat > ci.yml <<'EOF'
name: ci
on:
  push:
  pull_request:
jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: |
          if [ -f FAIL ]; then echo "ERROR: FAIL marker present"; exit 1; fi
          echo "unit ok"
EOF
gh api -X PUT repos/<account>/scarabhive-forge-live/contents/.github/workflows/ci.yml \
  -f message="ci: unit job fails on a FAIL marker" -f content="$(base64 -w0 ci.yml)"
```

The token needs the scopes `repo` and `workflow` (a `gh` login has both).

## Running the live tests

```sh
FORGE_LIVE_GITHUB=1 FORGE_TEST_GITHUB_REPO=<account>/scarabhive-forge-live \
FORGE_TEST_GITHUB_TOKEN="$(gh auth token)" \
.venv/Scripts/python.exe -m pytest src/plugins/forge/tests/test_plugin_forge_live_github.py -o timeout=900
```

Each run creates one issue and two pull requests; one is merged, the
other closed, and all branches are deleted. The test only runs against
a private repo whose name ends in `-forge-live` — every run merges into
the default branch. Actions costs minutes of the quota in a
private repo (about ten per run).
