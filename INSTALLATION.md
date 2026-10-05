# Installation

This gets ScarabHive running on one machine: the web UI at `http://127.0.0.1:8000` and the
`agent-cli` command. Everything you can configure after that is in
[docs/configuration.md](docs/configuration.md).

## 1. What you need

- **Python 3.11 or newer** (3.12 recommended; the Docker image uses it), and git. Check with
  `python --version` (`python3 --version` on Linux and macOS; on Windows `py --version` if
  `python` opens the Microsoft Store).
- **A few GB of disk**: the dependencies include PyTorch. The first installation takes many
  minutes.
- **Linux**: a compiler and the headers `pycairo` builds against (it has no Linux wheels):

  ```bash
  sudo apt-get install python3-venv python3-dev build-essential libcairo2-dev pkg-config
  ```

- **macOS**: `brew install cairo pkg-config`.
- **An OpenRouter API key** ([openrouter.ai/keys](https://openrouter.ai/keys)): the default
  chat agent runs on it. Keys for other providers are optional.

## 2. Install and start

```bash
git clone https://github.com/eehrich/ScarabHive.git ScarabHive
cd ScarabHive
```

Then run the install script:

```bash
powershell -ExecutionPolicy Bypass -File install.ps1    # Windows, PowerShell
sh install.sh                                           # Linux, macOS, Git Bash on Windows
```

The script:

1. Creates the virtual environment `.venv` and installs ScarabHive into it. On Linux without an
   NVIDIA GPU it takes the CPU build of PyTorch, which is several GB smaller.
2. Gives this installation its own signing key for logins. The key goes into
   `config/local.env`, and `config/local.yaml` points to it. Both files belong to this machine
   and never go into the repository.
3. Asks for a password for the `admin` account, twice; Enter generates one. Where it cannot ask
   (an unattended run; Git Bash's own window unless its pseudo console is on) it generates one and
   shows it once. A publicly known password is refused. Only its hash
   goes into the user database. Run again, it leaves an admin with a password of its own alone;
   an admin still on a password the repository has printed (`admin123` of older versions) is
   asked for a new one, which also revokes that account's API key. If you aborted this step, the script stops
   and does not start the API.
4. Starts the API and opens `http://127.0.0.1:8000` in the browser as soon as it answers.

Running the script again installs what a `git pull` added and starts the API; stop a running
API first. To start the API later without the script, run `.venv\Scripts\agent-api.exe` (Windows) or `.venv/bin/agent-api`
(Linux/macOS). Stop it with Ctrl+C.

## 3. Log in and enter your key

Log in as `admin` with the password the script asked for or showed. Installed without the
script, the API creates `admin` on its very first start, while the user database
(`data/users.db`) is still empty, with a generated password it shows once on the console;
run `python -m agent_system.auth.first_admin` before that start to choose one instead.

Then open the **Setup** panel: click the grid icon in the top bar (tooltip *Panels*) and type
`setup`.

- **API keys:** Paste your OpenRouter key into the field next to `OPENROUTER_API_KEY` and click
  *Save*. It goes into `config/local.env`, and the chat uses it from the next message on.
  Sub-agents, background jobs, fallback models and plugins that read their key at start (a web
  search, for example) use it after a restart. Each key shows as *set*, *missing* or
  *placeholder*. You only need the keys for the services you use.
- **Test the chat:** Sends one short request through the default chat model and shows the
  answer or the provider's error.
- **Admin password:** Shows whether the admin still opens with a publicly known password such
  as `admin123` (an installation from before 10/2026); while it does, that admin can change it
  right there. Any other password change: user menu, *Settings*.
- **Signing key:** Shows whether logins are signed with this installation's own key. If you
  installed without the script, *Make an own signing key* creates one, and the next restart
  applies it.

The command line works too, in the activated virtual environment (see the next section):

```bash
agent-cli chat
```

## Installing by hand

This is what the script does, step by step:

```bash
python -m venv .venv

source .venv/Scripts/activate      # Windows, Git Bash
# .venv\Scripts\Activate.ps1       # Windows, PowerShell
# source .venv/bin/activate        # Linux/macOS

pip install -U pip
pip install -e .
python -m agent_system.config.local_layer signing-key
python -m agent_system.auth.first_admin
agent-api
```

- **On Linux and macOS**, type `python3` instead of `python` until the virtual environment is
  active. **On Windows**, type `py` if `python` opens the Microsoft Store.
- **PowerShell refuses `Activate.ps1`** ("running scripts is disabled on this system"): run
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then activate again.
- **Linux without an NVIDIA GPU:** before `pip install -e .`, run
  `pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu`.

Keys can also be written into `config/local.env` by hand, one `NAME=value` line each, for
example `OPENROUTER_API_KEY=sk-or-v1-...`. The template `config/secrets.env.example` lists
every name. The API reads the file only when it starts, so restart it after editing.

- **`config/local.env` wins over `config/secrets.env`.** Both are read at every start, and for
  a name in both, `local.env` counts.
- **A real environment variable wins over both files.** The Setup panel then shows the key as
  set by the environment and offers no field for it.
- **If your checkout came with a filled `config/secrets.env`**, its keys are not yours. Empty
  the file: your own keys belong in `config/local.env`.

## Docker

Docker replaces the install script. Before the first start:

1. Make the signing key by hand. Create `config/local.env` with the line
   `AUTH_SECRET_KEY=` followed by the output of `openssl rand -hex 32` (without openssl:
   `python -c "import secrets; print(secrets.token_hex(32))"`).
2. Create `config/local.yaml` with:

   ```yaml
   auth:
     secret_key: "${AUTH_SECRET_KEY}"
   ```

3. Add your `OPENROUTER_API_KEY=...` line to `config/local.env`.

Then start the container:

```bash
docker compose up -d --build
docker compose logs -f scarabhive
```

The first start creates `admin` with a generated password and shows it in that output, not in
`logs/api.log`. It stays in the container's log, so change it after the first login (user menu,
*Settings*).
Log in and open the Setup panel as in section 3. The web UI is at `http://127.0.0.1:8000`,
published on the loopback interface only.

- **The panel cannot save keys here:** `config/` is mounted read-only. Keys go into
  `config/local.env` on the host, followed by `docker compose restart`.
- **`config/local.env` must be readable by the container user** (uid 10001 by default). If only
  your own user can read it (mode 600), it is not read: the log says
  `Could not read .../config/local.env: [Errno 13] Permission denied`, then
  `Refusing to start: auth.secret_key is empty`, and the container restarts over and over.
  Details are in [docker-compose.yml](docker-compose.yml).
- **Reaching it from other machines:** Set `SCARABHIVE_BIND=0.0.0.0` in the shell or in `.env`.
  `network.host` in `config/local.yaml` has no effect in the container. The precautions of the
  next section apply all the same.
- **Your data:** Sessions, `users.db`, plugin data, logs and downloaded models live in named
  volumes.
- **Build arguments:** CPU or CUDA PyTorch, extra system packages and the container user's ids
  are described in [docker-compose.yml](docker-compose.yml) and the [Dockerfile](Dockerfile).

## Reaching it from other machines

By default the API listens on `127.0.0.1` only. To open it to your network, add this to
`config/local.yaml` (the script created the file; otherwise create it):

```yaml
network:
  host: 0.0.0.0
```

Then restart the API. Do this only once logins use this installation's own signing key and the
admin has a password of its own (the Setup panel shows both). Until then, anyone on the network
can sign their own login, or log in with a publicly known password such as the `admin123` of
older versions.

- **Registration:** Anyone who reaches the login page can register an account. With the
  shipped `auth.registration` settings, a new account stays inactive until an admin activates
  it.
- **`network.remote_paths`:** If this is set, other machines reach only the paths it lists.

Two environment variables override host and port for a single start: `HOST` and `PORT`. For
example, `PORT=8001 agent-api`, or in PowerShell `$env:PORT = "8001"; agent-api`, which keeps
the value for the rest of that PowerShell window.

## The first semantic search

Memory, file search and related plugins use the embedding model all-MiniLM-L6-v2. It is
downloaded once, on first use, into `~/.cache/chroma`, so that first use needs network access.

## If something goes wrong

| What you see | What it means |
|---|---|
| `Python 3.11 or newer is needed` | Install Python from [python.org](https://www.python.org/downloads/), then run the script again. |
| `Could not create the virtual environment` | Debian/Ubuntu: `sudo apt-get install python3-venv`. |
| `pip install` fails building `pycairo`, or asks for a C compiler | Install the build packages (step 1). |
| `running scripts is disabled on this system` | PowerShell's execution policy: start the script as shown in step 2. |
| `agent-cli: command not found`, or `The term 'agent-cli' is not recognized` | The virtual environment is not activated (see *Installing by hand*). |
| `Config references N unset variable(s): ...` at every start | Normal for the services you do not use. Only `OPENROUTER_API_KEY` must not be in the list. |
| `Refusing to start: auth.secret_key is empty ...` or `... has N characters, at least 32 are needed` | The signing key is missing or too short: run `python -m agent_system.config.local_layer signing-key` in the activated virtual environment. |
| `auth.secret_key is a published default ...` in the log | The installation still signs logins with the key from the repository: use *Make an own signing key* in the Setup panel, then restart. |
| The admin password is lost | In the activated virtual environment: `agent-cli users update admin -p <new password>`. That also ends the admin's logins and revokes its API key; whatever used the key needs a new one (`agent-cli users generate-api-key admin`). Running the install script again does not reset the password. |
| The chat test fails with 401, or the provider says the user is unknown | The API key is missing or wrong, or it is still the template value. Enter it again in the Setup panel. |
| `address already in use`, or on Windows `[Errno 10048] ... only one usage of each socket address` | Another program uses port 8000, or an API is still running: start with another `PORT` (see above). `network.port` in `config/local.yaml` works too, but the install scripts only see `PORT`. |
| `The embedding model all-MiniLM-L6-v2 could not be readied ...` | Its first use had no network access, or `~/.cache/chroma` is not writable. |

The logs are in `logs/api.log` (the API) and `logs/cli.log` (`agent-cli`).

Working on the code: [CONTRIBUTING.md](CONTRIBUTING.md) covers the development extras and which
tests to run.
