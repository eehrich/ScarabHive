# Installation

This gets ScarabHive running on one machine: the web UI at `http://127.0.0.1:8000` and the
`agent-cli` command. Everything you can configure after that is in
[docs/configuration.md](docs/configuration.md).

## 1. What you need

- **Python 3.11 or newer** (3.12 recommended; the Docker image uses it), and git. Check with
  `python --version` (`python3 --version` on Linux and macOS).
- **A few GB of disk**: the dependencies include PyTorch. The first `pip install` takes many
  minutes.
- **Linux**: a compiler and the headers `pycairo` builds against (it has no Linux wheels):

  ```bash
  sudo apt-get install python3-venv python3-dev build-essential libcairo2-dev pkg-config
  ```

  On Linux and macOS, type `python3` wherever this guide says `python` until the virtual environment is
  active.
- **macOS**: `brew install cairo pkg-config`.
- **An OpenRouter API key** ([openrouter.ai/keys](https://openrouter.ai/keys)): the default
  chat agent runs on it. Keys for other providers are optional.

## 2. Install

```bash
git clone https://github.com/eehrich/ScarabHive.git ScarabHive
cd ScarabHive
python -m venv .venv

source .venv/Scripts/activate      # Windows, Git Bash
# .venv\Scripts\Activate.ps1       # Windows, PowerShell
# source .venv/bin/activate        # Linux/macOS

pip install -U pip
pip install -e .
agent-cli --help                   # the install worked if this prints the commands
```

- **PowerShell refuses `Activate.ps1`** ("running scripts is disabled on this system"): run
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then activate again.
- **Linux without an NVIDIA GPU:** install the CPU build of PyTorch before `pip install -e .`,
  otherwise pip downloads the much larger CUDA build:
  `pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu`

## 3. Put in your API key

API keys live in `config/secrets.env`, which is read at every start. A real environment
variable of the same name takes precedence over the file.

Start from a fresh copy of the template. If `config/secrets.env` is already there and you did
not write it, replace it:

```bash
cp config/secrets.env.example config/secrets.env                    # Git Bash, Linux, macOS
# Copy-Item config/secrets.env.example config/secrets.env -Force    # PowerShell
```

Open `config/secrets.env`, remove the `#` in front of `OPENROUTER_API_KEY=` and replace
`sk-or-v1-...` with your key. Leave the other lines alone unless you use that service.

- **One line per name.** If a name appears twice, the **first** line counts.
- **A template value such as `sk-or-v1-...` counts as set**, so no warning appears when the
  config loads; the provider simply refuses it. The Setup panel (step 5) lists it as
  *placeholder*.
- **Never commit this file.** It is still tracked in the repository for now: `git status`
  shows it as changed, and `git pull` may ask you to stash it.

## 4. Make your own signing key

Logins are signed with `auth.secret_key` from `config/config.yaml`. The value shipped there
is public: it is in the repository, so anyone could sign an admin login for your
installation. The API logs an error about it at every start. Make your own key:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
# or: openssl rand -hex 32
```

1. Add the printed value to `config/secrets.env` as a line of its own, for example
   `AUTH_SECRET_KEY=3f9c...` (the whole value, without quotes or brackets).
2. `config/config.yaml` already has an `auth:` block (near line 100) with a line
   `secret_key: "..."`. Change only that line's value:

   ```yaml
     secret_key: "${AUTH_SECRET_KEY}"
   ```

   Do **not** add a second `auth:` block. YAML keeps only the last one, and the rest of the
   first block would be lost, login included.

- **A key that is too short stops the start.** The API refuses to start with an empty key
  (for example an unset `AUTH_SECRET_KEY`) or one under 32 characters.
- **Where the key must go:** `auth` is read from `config/config.yaml` only. `config/local.yaml`
  cannot set it. Your change stays a local change of `config/config.yaml`; `git pull` may ask
  you to stash it.
- **Optional:** `auth.reject_default_secret_key: true` makes a published key a start error
  instead of a log line.

## 5. Start it and log in

```bash
agent-api
```

Open `http://127.0.0.1:8000` and log in. On the very first start, while the user database
(`data/users.db`) is still empty, the API creates the admin `admin` with the password
`admin123` (`auth.default_admin_username` / `default_admin_password` in `config/config.yaml`).

Then open the **Setup** panel: click the grid icon in the top bar (tooltip *Panels*) and type
`setup`. It shows:

- **API keys:** every key the configuration uses, each as *set*, *missing* or *placeholder*.
- **Admin password:** whether the admin still has the known password, with a form to change
  it while it does.
- **Signing key:** whether logins are signed with a known key, and whether a restart is still
  needed for a new one.
- **Test the chat:** sends one short request through the default chat model and shows the
  answer or the provider's error.

The API reads `config/secrets.env` only when it starts. After editing it, stop `agent-api`
(Ctrl+C) and start it again.

The command line works too:

```bash
agent-cli chat
```

## Docker

Instead of installing and starting with Python (steps 1, 2 and 5): do steps 3 and 4, then

```bash
docker compose up -d --build
docker compose logs -f scarabhive
```

Log in and open the Setup panel as in step 5. The web UI is at `http://127.0.0.1:8000`,
published on the loopback interface only.

- **Signing key without Python on the host:** `openssl rand -hex 32`.
- **`config/secrets.env` must be readable by the container user** (uid 10001 by default). A
  file only your own user can read (mode 600) is not read: the log says
  `Could not read .../config/secrets.env: [Errno 13] Permission denied`, and every key then shows
  as unset. Details are in [docker-compose.yml](docker-compose.yml).
- **Reaching it from other machines:** set `SCARABHIVE_BIND=0.0.0.0` in the shell or in `.env`.
  `network.host` in `config/local.yaml` has no effect in the container. The precautions of the
  next section apply all the same.
- **Configuration:** `config/` is mounted read-only.
- **Your data:** sessions, `users.db`, plugin data, logs and downloaded models live in named
  volumes.
- **Build arguments:** CPU or CUDA PyTorch, extra system packages and the container user's ids
  are described in [docker-compose.yml](docker-compose.yml) and the [Dockerfile](Dockerfile).

## Reaching it from other machines

By default the API listens on `127.0.0.1` only. To open it to your network, add this to
`config/local.yaml` (create the file if it is not there). It is this machine's own file and
never goes into the repository:

```yaml
network:
  host: 0.0.0.0
```

Then restart `agent-api`. Do this only after step 4 and after changing the admin password:
until then, anyone on the network can log in with `admin123` or sign their own login.

- **Registration:** anyone who reaches the login page can register an account. With the
  shipped `auth.registration` settings, a new account stays inactive until an admin activates
  it.
- **`network.remote_paths`:** if this is set, other machines reach only the paths it lists.

Two environment variables override host and port for a single start: `HOST` and `PORT`, for
example `PORT=8001 agent-api` (PowerShell: `$env:PORT = "8001"; agent-api`, which keeps the
value for the rest of that PowerShell window).

## The first semantic search

Memory, file search and related plugins use the embedding model all-MiniLM-L6-v2. It is
downloaded once, on first use, into `~/.cache/chroma`, so that first use needs network access.

## If something goes wrong

| What you see | What it means |
|---|---|
| `pip install` fails building `pycairo`, or asks for a C compiler | Install the build packages (step 1). |
| `running scripts is disabled on this system` | PowerShell's execution policy (step 2). |
| `agent-cli: command not found`, or `The term 'agent-cli' is not recognized` | The virtual environment is not activated (step 2). |
| `Config references N unset variable(s): ...` at every start | Normal for the services you do not use. Only `OPENROUTER_API_KEY` must not be in the list. |
| `Refusing to start: auth.secret_key is empty ...` or `... has N characters, at least 32 are needed` | Step 4: the key is missing (`AUTH_SECRET_KEY` not in `config/secrets.env`) or too short. |
| `auth.secret_key is a published default ...` in the log | Step 4 is not done yet. The API runs, but anyone can sign a login. |
| The chat test fails with 401, or the provider says the user is unknown | The API key is missing, or still the template value. The Setup panel shows which. After fixing it, restart the API. |
| `address already in use`, or on Windows `[Errno 10048] ... only one usage of each socket address` | Another program uses port 8000: start with another `PORT` (see above), or set `network.port`. |
| `The embedding model all-MiniLM-L6-v2 could not be readied ...` | Its first use had no network access, or `~/.cache/chroma` is not writable. |

The logs are in `logs/api.log` (the API) and `logs/cli.log` (`agent-cli`).

Working on the code: [CONTRIBUTING.md](CONTRIBUTING.md) covers the development extras and which
tests to run.
