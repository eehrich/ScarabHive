# systemd Service Templates

## agent-system.service.template

A systemd unit for running the ScarabHive API server in production: as its own
unprivileged user, listening on `127.0.0.1:8000` only, with its secrets in an
environment file outside the checkout.

### Installation

```bash
# 1. A service user; the checkout and its .venv in its home, created by that user
sudo useradd --system --user-group --home-dir /opt/scarabhive --create-home --shell /usr/sbin/nologin scarabhive
sudo -u scarabhive git clone <repository> /opt/scarabhive/agentsystem
# ...create the venv and install as described in INSTALLATION.md, as that user

# 2. Secrets: read by systemd before it switches to the service user
sudo install -d -m 700 /etc/scarabhive
sudo install -m 600 /dev/null /etc/scarabhive/env
sudo nano /etc/scarabhive/env      # AUTH_SECRET_KEY=<openssl rand -hex 32>, OPENROUTER_API_KEY=..., ...

# 3. The unit
sudo cp agent-system.service.template /etc/systemd/system/scarabhive.service
sudo nano /etc/systemd/system/scarabhive.service   # adjust paths if yours differ
sudo systemctl daemon-reload
sudo systemctl enable --now scarabhive.service
sudo systemctl status scarabhive.service
```

Then make the config read the signing key from there -- `config/config.yaml`
ships a published development key, and the environment alone does not replace
it:

```yaml
auth:
  secret_key: "${AUTH_SECRET_KEY}"
  reject_default_secret_key: true
```

A new key signs out everyone who is logged in; API keys keep working. Provider
keys referenced as `${VAR}` come from the environment file the same way -- a
real environment variable wins over `config/secrets.env`, so keep the keys out
of the checkout.

### Access from other machines: a reverse proxy

The server binds to loopback. Put a reverse proxy with TLS in front of it
instead of binding it to a network address -- the login, the API and the
agents' tools would otherwise be reachable unencrypted. The event streams
(Server-Sent Events) need buffering off. An nginx example:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;          # SSE: deliver events as they come
    proxy_read_timeout 1h;        # long agent runs keep a stream open
    client_max_body_size 50m;     # uploads (images, audio, files) go to /run
}
```

uvicorn trusts `X-Forwarded-*` from `127.0.0.1` by default, so the security
log records the client's address, not the proxy's.

### Moving an existing installation from root

An installation that ran as `root` from `/root/...` changes in these places:

- the old unit has another name: stop and disable it, or both run
  (`sudo systemctl disable --now agent-system.service`);
- create the venv anew as the service user in the new place: an editable
  install points at the old checkout's `src/`;
- the checkout moves into the service user's home and becomes that user's --
  all of it, not only `data/` and `logs/`: the agent editor writes
  `config/agents/`, the venv lives in the checkout
  (`sudo chown -R scarabhive: /opt/scarabhive/agentsystem`);
- every other process of the installation runs as the service user against
  the same checkout: a worker unit, cron jobs, and `agent-cli` on the host
  (`sudo -u scarabhive ...`, with the keys from the environment file). One
  started as root leaves root-owned databases and files in `data/` that the
  service can no longer write; units or scripts that name `/root/agentsystem`
  need the new path;
- `~` is now the service user's home: SSH keys the `ssh_control` machines name
  as `~/.ssh/...` must be there, and their `known_hosts` entries (host keys are
  checked);
- agents' shell commands (`terminal`) run without root rights, and `sudo`/`su`
  fail under `NoNewPrivileges`;
- `/tmp` is private to the service and emptied on every restart (`PrivateTmp`):
  lock files there are no longer shared with processes outside the unit;
- the log level is `info` instead of `debug` (the task text of every request is
  logged at `info` too -- the logs hold users' prompts either way);
- clients on other machines connect through the proxy, not to port 80 of the
  server.

### Important Fix

The unit sets two environment variables to prevent **onnxruntime
pthread_setaffinity_np crashes** (Signal 11 SEGV):

```ini
Environment="OMP_NUM_THREADS=1"
Environment="ORT_DISABLE_THREAD_AFFINITY=1"
```

These variables are **required** in containerized/virtualized environments
(Docker, LXC, VMs with CPU restrictions) to prevent ChromaDB/onnxruntime from
crashing when trying to set thread affinity.

### Update Service Without Restart

```bash
sudo nano /etc/systemd/system/scarabhive.service
sudo systemctl daemon-reload     # does NOT restart the service
# Changes apply on the next restart
```

### Troubleshooting

**If you see crashes with `pthread_setaffinity_np failed`:**
- Ensure `OMP_NUM_THREADS=1` and `ORT_DISABLE_THREAD_AFFINITY=1` are set
- Run `systemctl daemon-reload` after editing
- Restart the service: `sudo systemctl restart scarabhive.service`

**If the service does not start with "auth.secret_key is empty":** the config
reads the key from `${AUTH_SECRET_KEY}` and the environment file does not set
it. After five failed starts in five minutes systemd stops restarting it and
refuses `systemctl restart` for a while; `sudo systemctl reset-failed
scarabhive.service` lets it start again right away.

**View logs:**
```bash
sudo journalctl -u scarabhive.service -n 100
sudo journalctl -u scarabhive.service -f
sudo journalctl -u scarabhive.service --since "2026-01-07 14:00"
```

### Environment Variables

| Variable | Default in the unit | Description |
|----------|---------------------|-------------|
| `AGENT_LOG_LEVEL` | `info` | Logging level (debug, info, warning, error). At `info` and below the logs contain the task text of every request |
| `OMP_NUM_THREADS` | `1` | Limit OpenMP threads (prevents affinity crashes) |
| `ORT_DISABLE_THREAD_AFFINITY` | `1` | Disable onnxruntime thread affinity (prevents SEGV) |
| `AGENT_ENABLE_PROFILING` | unset | Enable performance profiling |
| `AGENT_ENABLE_MEMORY_PROFILING` | unset | Enable memory profiling |
| `AGENT_MEMORY_SNAPSHOT_INTERVAL` | `3600` | Memory snapshot interval (seconds) |
| `AUTH_SECRET_KEY` etc. | from `/etc/scarabhive/env` | Secrets, never in the unit file |
