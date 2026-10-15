# GitLab test instance for the live tests

GitLab CE 19.4 with a Docker runner, tuned to be frugal (Puma in single mode,
no Prometheus, no registry, no KAS; `mem_limit: 4g`).

## Setup

On a Docker host, in a folder of its own with `compose.yaml` and
`bootstrap.py` from here:

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

`bootstrap.py` (standard library only) creates: the private group `forge-test`,
the private project `forge-test/app` with a `.gitlab-ci.yml` (job `unit` fails
if a file `FAIL` is in the tree; `lint` with `allow_failure`), the user
`scarabhive-bot` as Developer with a token of scope `api`, `main` protected
(push Maintainer, merge Developer) and, when there is none yet, an instance
runner with the Docker executor. It writes `CREDENTIALS` (0600):
`FORGE_TEST_GITLAB_URL`, `FORGE_TEST_GITLAB_PROJECT`,
`FORGE_TEST_GITLAB_ROOT_TOKEN`, `FORGE_TEST_GITLAB_BOT_TOKEN`. It is safe to run
again; each run makes a new bot token.

## Running the live tests

```sh
eval "$(ssh <host> cat <folder>/CREDENTIALS | sed 's/^/export /')"
FORGE_LIVE=1 .venv/Scripts/python.exe -m pytest src/plugins/forge/tests/test_plugin_forge_live.py -o timeout=900
```

Without `FORGE_LIVE=1` the tests are skipped. They need `git` on the machine
that runs them, and `git-lfs` for the LFS test; your own git config is not
read (an empty global config, no system config).

Each run creates an issue and two merge requests: one is merged (its branch
goes with the merge, and the issue closes), the other is closed. The other
branches -- line comments, LFS, the red pipeline -- are deleted at the end.
The red pipeline's job is retried once.
