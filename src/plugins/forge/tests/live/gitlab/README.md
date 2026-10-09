# GitLab test instance for the live tests

GitLab CE with a Docker runner, tuned to be frugal (Puma in single mode,
no Prometheus, no registry; `mem_limit: 4g`, uses about 2.6 GB).

## Setup

On a Docker host, in a folder of its own with the `compose.yaml` from here:

```sh
cat > .env <<EOF
GITLAB_EXTERNAL_URL=http://<host>:8929
GITLAB_HTTP_PORT=8929
GITLAB_ROOT_PASSWORD=<random>
EOF
chmod 600 .env
docker compose up -d          # the first start takes a few minutes
```

Then an admin token for root (stays on the host):

```sh
docker exec <gitlab-container> gitlab-rails runner "t = User.find_by_username('root').personal_access_tokens.create!(scopes: ['api','admin_mode','sudo'], name: 'forge-admin', expires_at: 360.days.from_now); puts \"ROOT_TOKEN=#{t.token}\"" > root_token.out
chmod 600 root_token.out
python3 bootstrap.py
```

`bootstrap.py` creates: the group `forge-test`, the project `forge-test/app` with
a `.gitlab-ci.yml` (job `unit` fails if a file `FAIL` is in the tree;
`lint` with `allow_failure`), the user `scarabhive-bot` as Developer with its
own token, `main` protected (push Maintainer, merge Developer) and an
instance runner. It writes `CREDENTIALS` (0600).

## Running the live tests

```sh
eval "$(ssh <host> cat <folder>/CREDENTIALS | sed 's/^/export /')"
FORGE_LIVE=1 .venv/Scripts/python.exe -m pytest src/plugins/forge/tests/test_plugin_forge_live.py -o timeout=900
```

Each run creates an issue and a merge request and merges it; the red
branch is deleted at the end.
