# GitLab-Testinstanz für die Live-Tests

GitLab CE mit einem Docker-Runner, sparsam eingestellt (Puma im Single-Modus,
kein Prometheus, keine Registry; `mem_limit: 4g`, belegt etwa 2,6 GB).

## Aufsetzen

Auf einem Docker-Host, in einem eigenen Ordner mit `compose.yaml` von hier:

```sh
cat > .env <<EOF
GITLAB_EXTERNAL_URL=http://<host>:8929
GITLAB_HTTP_PORT=8929
GITLAB_ROOT_PASSWORD=<zufällig>
EOF
chmod 600 .env
docker compose up -d          # der erste Start braucht einige Minuten
```

Dann ein Admin-Token für root (bleibt auf dem Host):

```sh
docker exec <gitlab-container> gitlab-rails runner "t = User.find_by_username('root').personal_access_tokens.create!(scopes: ['api','admin_mode','sudo'], name: 'forge-admin', expires_at: 360.days.from_now); puts \"ROOT_TOKEN=#{t.token}\"" > root_token.out
chmod 600 root_token.out
python3 bootstrap.py
```

`bootstrap.py` legt an: Gruppe `forge-test`, Projekt `forge-test/app` mit
`.gitlab-ci.yml` (Job `unit` scheitert, wenn eine Datei `FAIL` im Baum liegt;
`lint` mit `allow_failure`), den Nutzer `scarabhive-bot` als Developer mit
eigenem Token, `main` geschützt (Push Maintainer, Merge Developer) und einen
Instanz-Runner. Es schreibt `CREDENTIALS` (0600).

## Live-Tests fahren

```sh
eval "$(ssh <host> cat <ordner>/CREDENTIALS | sed 's/^/export /')"
FORGE_LIVE=1 .venv/Scripts/python.exe -m pytest src/plugins/forge/tests/test_plugin_forge_live.py -o timeout=900
```

Jeder Lauf legt ein Issue und einen Merge Request an und mergt ihn; der rote
Branch wird am Ende gelöscht.
