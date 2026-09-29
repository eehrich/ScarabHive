# GitHub-Testrepo für die Live-Tests

Ein privates Wegwerf-Repo mit einem Actions-Workflow; ein Konto spielt beide
Seiten (legt Issues an, kommentiert, arbeitet sie als Bot ab).

## Aufsetzen

```sh
gh repo create <konto>/scarabhive-forge-live --private --add-readme
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
gh api -X PUT repos/<konto>/scarabhive-forge-live/contents/.github/workflows/ci.yml \
  -f message="ci: unit job fails on a FAIL marker" -f content="$(base64 -w0 ci.yml)"
```

Das Token braucht die Scopes `repo` und `workflow` (ein `gh`-Login hat beide).

## Live-Tests fahren

```sh
FORGE_LIVE_GITHUB=1 FORGE_TEST_GITHUB_REPO=<konto>/scarabhive-forge-live \
FORGE_TEST_GITHUB_TOKEN="$(gh auth token)" \
.venv/Scripts/python.exe -m pytest src/plugins/forge/tests/test_plugin_forge_live_github.py -o timeout=900
```

Jeder Lauf legt ein Issue und zwei Pull Requests an; einer wird gemergt, der
andere geschlossen, alle Branches werden gelöscht. Der Test läuft nur gegen
ein privates Repo, dessen Name auf `-forge-live` endet — jeder Lauf mergt in
den Default-Branch. Actions kostet im
privaten Repo Minuten des Kontingents (ein Lauf etwa zehn).
