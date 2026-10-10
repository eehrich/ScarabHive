# Deploying n8n for ScarabHive

This guide sets up an n8n instance of your own in Docker and connects it to the ScarabHive plugin `n8n`. It applies to every ScarabHive installation. The examples use `http://localhost:5678`; substitute your own address everywhere.

The setup is tested with n8n 2.39.9 (Community, Docker, SQLite). The facts are in `../n8n_facts.md`, the design in `../design.md`.

## What is here

| File | Purpose |
|---|---|
| `docker-compose.yml` | n8n container with a pinned image version, volume `n8n_data`, instance MCP switched on via env |
| `.env.example` | Template for your `.env` |
| `setup_owner.sh` | One-time setup: owner, public API key, MCP key, everything into `CREDENTIALS` |
| `.gitignore` | keeps `.env` and `CREDENTIALS` out of Git |

## Prerequisites

- Docker with the Compose plugin (`docker compose version` must respond).
- On the machine that runs the setup: `sh`, `curl`, `python3` and `openssl`. On Windows this works in WSL or Git Bash.
- `setup_owner.sh` talks to n8n at `http://127.0.0.1:<N8N_PORT>`. So it must run **on the Docker host**.
- A fresh n8n instance. If the instance already has an owner, the script needs its password in `CREDENTIALS` (see "Existing instance").

## 1. Choose a directory

You have two options:
- **Run in place:** directly in `src/plugins/n8n/docs/deploy/`. `.env` and `CREDENTIALS` are git-ignored there and do not end up in a commit.
- **Copy:** copy the folder `docs/deploy/` to a directory of your choice, e.g. on a server. Then the secrets do not lie in the repository tree. Copy the `.gitignore` along if the target directory itself is under Git.

All following commands run in this directory.

## 2. Create `.env`

```sh
cp .env.example .env
chmod 600 .env
```

Then generate `N8N_ENCRYPTION_KEY` and enter it:

```sh
openssl rand -hex 32
```

Set the printed value after `N8N_ENCRYPTION_KEY=` in `.env`. Without this value the container does not start.

**Important:** n8n uses this key to encrypt all stored credentials. Keep it safe and do not change it after the first start; otherwise n8n can no longer decrypt the existing credentials.

The remaining values:

| Variable | Meaning | Default |
|---|---|---|
| `N8N_PUBLIC_URL` | The address at which browsers and webhook callers reach n8n, with a trailing `/`. n8n builds editor links and webhook URLs from it. | `http://localhost:5678/` |
| `N8N_PORT` | Port on the Docker host | `5678` |
| `GENERIC_TIMEZONE` | Time zone for schedule triggers and timestamps, e.g. `Europe/Berlin` | `UTC` |
| `N8N_SECURE_COOKIE` | Whether the login cookie gets the `Secure` flag | `true` |

**On `N8N_SECURE_COOKIE`:** browsers send a `Secure` cookie only over HTTPS (exception: `localhost`).
- If you open n8n over **plain HTTP via an address other than localhost**, e.g. `http://<server>:5678`, set `N8N_SECURE_COOKIE=false`. Otherwise login in the editor does not work.
- If n8n sits **behind HTTPS** (reverse proxy with TLS), leave the value at `true`.

Note: without TLS, password and API keys travel over the network in plain text. For anything except a trusted local network, a TLS reverse proxy belongs in front of n8n.

Optionally you can also set `N8N_OWNER_EMAIL=<your address>` in `.env`. The setup script then creates the owner with this address; otherwise it uses a placeholder.

## 3. Start n8n

```sh
docker compose up -d
```

Wait until n8n is ready:

```sh
curl -fsS http://localhost:5678/healthz
```

The response must be `{"status":"ok"}`.

The compose file switches the **instance MCP** on via env (`N8N_MCP_MANAGED_BY_ENV=true`, `N8N_MCP_ACCESS_ENABLED=true`). It is therefore on after every start, and the switch in the editor is locked. New workflows that a human creates in the editor are still **not** automatically released for the MCP; that remains a decision in the editor.

The container starts with `restart: unless-stopped` and comes back up when the Docker host restarts.

## 4. Run `setup_owner.sh`

```sh
sh setup_owner.sh
```

The script works step by step and can be repeated. Each step checks whether its result is already there.

1. **Create owner:** generates a random password, creates the owner account and logs in.
2. **Create public API key** with **minimal, read-only scopes**: `workflow:read`, `workflow:list`, `execution:read`, `execution:list`. ScarabHive needs nothing more; writing happens exclusively via the MCP.
3. **Check instance MCP:** it must be on (see step 3). If it is off, the script aborts with a pointer to the compose variables.
4. **Create MCP key:** rotates the owner's MCP key and stores the new key. n8n shows it in plain text only in exactly this response, afterwards only masked.

Everything ends up in the file `CREDENTIALS` with permissions 0600 (only your user may read):

```
N8N_URL=…
N8N_OWNER_EMAIL=…
N8N_OWNER_PASSWORD=…
N8N_API_KEY=…
N8N_MCP_KEY=…
```

The script prints no key and no password on the console, only HTTP status codes.

## 5. Enter the values in ScarabHive

Exactly three values belong in the file `config/secrets.env` of your ScarabHive installation:

```
N8N_BASE_URL=http://localhost:5678
N8N_API_KEY=<N8N_API_KEY from CREDENTIALS>
N8N_MCP_KEY=<N8N_MCP_KEY from CREDENTIALS>
```

- `N8N_BASE_URL` is the address at which **ScarabHive** reaches n8n, without a trailing `/`. It can differ from `N8N_PUBLIC_URL`, e.g. when both run on the same host.
- If it differs, `N8N_PUBLIC_URL=<public address>` also belongs in `config/secrets.env`. The plugin builds editor links and the webhook URLs the agent names after publishing from it; without it, from `N8N_BASE_URL`, which is then no good for outside callers.
- The **owner password does not belong** in `config/secrets.env`. ScarabHive does not need it at runtime; it stays in `CREDENTIALS` for you.

Then restart ScarabHive. The plugin instance is already switched on (`enabled: true` in `agents/n8n.yaml`) and offers no tools without the values. Without `N8N_MCP_KEY` the plugin provides no tools; without `N8N_API_KEY` only the read-only node-knowledge tools.

You can store `CREDENTIALS` itself in a safe place after transferring the values. If it lies in the deploy directory, it is git-ignored.

## Rotating keys

Rotate a key if it may have ended up somewhere it does not belong, or regularly according to your own policy.

**MCP key:**
1. Delete the line `N8N_MCP_KEY=…` from `CREDENTIALS`.
2. Run `sh setup_owner.sh` again. The script rotates the key; the old one is no longer valid afterwards.
3. Enter the new value in `config/secrets.env` and restart ScarabHive.

As long as ScarabHive still has the old key, the n8n tools fail with a pointer to `N8N_MCP_KEY`.

**Public API key:**
1. Delete the line `N8N_API_KEY=…` from `CREDENTIALS`.
2. Run `sh setup_owner.sh` again. It creates a new key with the same minimal scopes.
3. Enter the new value in `config/secrets.env` and restart ScarabHive.
4. Delete the **old** key in the n8n editor under Settings → "n8n API". The script does not delete it; a new key does not automatically replace the old one.

**Owner password:** change it in the n8n editor under personal settings and add the new value to `CREDENTIALS`. The setup script needs it for later rotations.

**`N8N_ENCRYPTION_KEY`** is not rotated (see step 2).

## Existing instance

If you already set up n8n with an earlier version of this script, your public API key probably has **all** scopes. Then, in this order:
1. Bring the compose file up to date (MCP variables) and run `docker compose up -d`. Otherwise the script aborts at the MCP step.
2. Delete the line `N8N_API_KEY=…` from `CREDENTIALS` and run the script again; it creates a minimal key and the MCP key.
3. Delete the old full key in the editor.

## Updating n8n

The image version is pinned in `docker-compose.yml` (`docker.n8n.io/n8nio/n8n:<version>`). To update:

1. **Back up.** The data lies in the Docker volume `n8n_n8n_data` (project name `n8n` plus volume `n8n_data`):
   ```sh
   docker compose stop
   docker run --rm -v n8n_n8n_data:/data -v "$PWD":/backup alpine \
     tar czf /backup/n8n_data_backup.tgz -C /data .
   ```
   The backup contains the database with encrypted credentials. Treat it like `CREDENTIALS`.
2. **Change the version:** replace the version number in the `image` entry in `docker-compose.yml`. Read n8n's release notes for breaking changes first.
3. **Start:**
   ```sh
   docker compose pull
   docker compose up -d
   curl -fsS http://localhost:5678/healthz
   ```
4. **Plugin configuration:** ScarabHive does not check the n8n version at runtime; without an owner login n8n does not give it out (M-MCP-51). `tested_n8n_version` in `agents/n8n.yaml` is a note: raise it only once the checks below pass.

### What to re-check in `n8n_facts.md` afterwards

The plugin relies on measured behavior of a specific n8n version. After an upgrade any of it can flip.

**The plugin's live tests check these points:**

| What | Facts | Why it matters |
|---|---|---|
| A whole run-through: create, mark, test, read the result | M-MCP-4, M-MCP-5, M-MCP-6 | Result evaluation and error normalization |
| Pins are honored; unpinned nodes run live | M-MCP-3, M-MCP-30, M-MCP-39 | The safety of every test run rests on this. |
| A pinned sub-workflow call does not start the sub-workflow | M-MCP-53 | That is why sub-workflows must never run live in a test. |
| Code has network access via `helpers.httpRequest` | M-MCP-38 | Explains why code never runs live in a test. |
| `validate_workflow` lets version 99 and an empty webhook path through | M-MCP-H9 | If n8n catches this itself by now, our own check is no longer needed. |
| Publishing by `versionId`, triggering by webhook, finding the running execution, taking back, archiving | M-MCP-65 to M-MCP-68, M-MCP-70 | The version check and finding a triggered execution again rest on this. |

**To check by hand** (with the scripts from the sources in `n8n_facts.md`):

| What | Facts | Why it matters |
|---|---|---|
| List and schemas of the MCP tools | M-MCP-H4, M-MCP-13, M-MCP-25, M-MCP-27 | The plugin forwards tools; changed names or parameters break it. |
| The remaining gaps of the validators | M-MCP-H8, M-MCP-8 to M-MCP-11, M-MCP-32, M-MCP-44 | as above |
| Release gate per workflow | M-MCP-20, M-MCP-21 | second bolt of the plugin |
| MCP env variables | M-MCP-22, M-MCP-23 | Without them the MCP is off after the start. |
| Rate limit; the MCP issues no session ID | M-MCP-24, M-MCP-54 | The plugin's budget |
| Scopes of the public API | F-AUTH6, F-AUTH7, F-AUTH8 | The minimal key must still be sufficient. |
| Storage settings | M-MCP-41, M-MCP-55 | Test evidence |
| Agent tool variants and hidden nodes, MCP client nodes | M-MCP-42, M-MCP-59, F-NOD11 | Block and check list of the plugin |

### Running the live tests

The tests create workflows with the prefix `zz-probe-live-` and delete them again at the end. The runtime key may not delete, so they need a **second key**:

1. In the n8n editor under Settings → "n8n API" create a key, at least with `workflow:read`, `workflow:list` and `workflow:delete`. If the editor offers no choice of scopes, the key has all rights: then create it only for the test run and delete it afterwards.
2. **Never** enter this key in `config/secrets.env`; it belongs only in the environment of the test run.
3. In the ScarabHive directory, with the Python of the ScarabHive environment:
   ```sh
   N8N_LIVE=1 N8N_BASE_URL=http://<host>:5678 N8N_API_KEY=<runtime key> \
   N8N_MCP_KEY=<MCP key> N8N_TEST_API_KEY=<second key> \
     python -m pytest src/plugins/n8n/tests/test_plugin_n8n_live.py -q
   ```
   Without `N8N_LIVE=1` or one of the variables, the tests are skipped, not passed.

If a result deviates: correct the fact in `n8n_facts.md` with a new measurement, then adapt design and plugin. Only then raise `tested_n8n_version`.
