#!/bin/sh
# ScarabHive in Docker, set up and started with one command, run from anywhere in the checkout:
#
#   sh scripts/docker-setup.sh
#
# Gives this installation its own signing key (config/local.env, named in config/local.yaml), asks for an
# OpenRouter key if none is set, builds and starts the container, and shows the admin's first password.
# Run it again at any time: what is already there is never overwritten.
#
# Port and interface: SCARABHIVE_PORT (default 8000) and SCARABHIVE_BIND (default 127.0.0.1), in the shell or in
# .env, e.g.  SCARABHIVE_PORT=8040 sh scripts/docker-setup.sh
set -eu

cd "$(dirname "$0")/.."
ENV_FILE=config/local.env
YAML_FILE=config/local.yaml
MIN_KEY=32  # agent_system.auth.security.MIN_SECRET_KEY_LENGTH

say() { printf '%s\n' "$*"; }
fail() { printf 'docker-setup: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "docker is not installed"
docker compose version >/dev/null 2>&1 || fail "docker compose (v2) is not available"
[ -f docker-compose.yml ] || fail "run this from a ScarabHive checkout"

# A name set in a file the container reads: local.env, else secrets.env. The shell's environment never reaches it.
has_var() {
  for file in "$ENV_FILE" config/secrets.env; do
    if [ -f "$file" ] && grep -q "^$1=." "$file"; then return 0; fi
  done
  return 1
}

# A line appended as a line of its own, also to a file that does not end in a line break.
append_line() {
  if [ -s "$1" ] && [ -n "$(tail -c1 "$1")" ]; then printf '\n' >> "$1"; fi
  printf '%s\n' "$2" >> "$1"
}

random_hex() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32
  else od -An -tx1 -N32 /dev/urandom | tr -d ' \n'
  fi
}

# 1. The signing key: the value in local.env, the reference in local.yaml -- config.yaml ships a published one.
touch "$ENV_FILE"
held=$(grep '^AUTH_SECRET_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)
if [ "${#held}" -ge "$MIN_KEY" ]; then
  say "Signing key: already in $ENV_FILE"
elif [ -n "$held" ]; then
  fail "AUTH_SECRET_KEY in $ENV_FILE has ${#held} characters, the API needs $MIN_KEY or more. Delete that line, then run this again: it makes a new one."
else
  append_line "$ENV_FILE" "AUTH_SECRET_KEY=$(random_hex)"
  say "Signing key: made, in $ENV_FILE"
fi
if [ -f "$YAML_FILE" ] && grep -q 'AUTH_SECRET_KEY' "$YAML_FILE"; then
  say "Signing key: already named in $YAML_FILE"
elif [ -f "$YAML_FILE" ] && grep -q '^auth:' "$YAML_FILE"; then
  fail "$YAML_FILE has an auth: section of its own. Add this line under it, then run this again:
  secret_key: \"\${AUTH_SECRET_KEY}\""
else
  append_line "$YAML_FILE" 'auth:'
  append_line "$YAML_FILE" '  secret_key: "${AUTH_SECRET_KEY}"'
  say "Signing key: named in $YAML_FILE"
fi

# 2. A key for the chat. Optional: the Setup panel shows what is missing, and keys are added to local.env later.
if has_var OPENROUTER_API_KEY; then
  say "OpenRouter key: already set"
elif [ -t 0 ]; then
  printf 'OpenRouter API key (https://openrouter.ai/keys; Enter to skip): '
  trap 'stty echo 2>/dev/null' EXIT INT TERM  # an interrupted prompt gives the terminal its echo back
  stty -echo 2>/dev/null || true
  read -r key || key=""
  stty echo 2>/dev/null || true
  trap - EXIT INT TERM
  printf '\n'
  if [ -n "$key" ]; then
    append_line "$ENV_FILE" "OPENROUTER_API_KEY=$key"
    say "OpenRouter key: saved in $ENV_FILE"
  else
    say "OpenRouter key: skipped -- add OPENROUTER_API_KEY=... to $ENV_FILE, then: docker compose restart"
  fi
else
  say "OpenRouter key: not set (no terminal to ask on) -- add OPENROUTER_API_KEY=... to $ENV_FILE, then: docker compose restart"
fi

# The container user (uid 10001 unless built otherwise) reads these through the read-only mount.
chmod 644 "$ENV_FILE" "$YAML_FILE"

# 3. Build and start.
say "Building and starting (the first build takes several minutes) ..."
if ! docker compose up -d --build; then
  fail "the start failed. If the port is taken, choose another: SCARABHIVE_PORT=8040 sh scripts/docker-setup.sh"
fi

say "Waiting for the API ..."
tries=0
until docker compose exec -T scarabhive python -c "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8000/api/health', timeout=3)" >/dev/null 2>&1; do
  tries=$((tries + 1))
  if [ "$tries" -ge 90 ]; then
    docker compose logs --tail 30 scarabhive
    fail "the API did not come up in three minutes; the log above says why"
  fi
  sleep 2
done

say ""
# where compose published it (SCARABHIVE_PORT/BIND from the shell or .env), e.g. 127.0.0.1:8040
address=$(docker compose port scarabhive 8000 2>/dev/null | tail -1 || true)
say "ScarabHive runs: http://${address:-127.0.0.1:8000}"
# The first start makes the admin and prints its login once, on stderr: "Default admin created -- ...",
# then "  Username: <name>" and "  Password: <password>".
created=$(docker compose logs --no-log-prefix scarabhive 2>&1 | grep -A2 "^Default admin created" | tail -3 || true)
admin=$(printf '%s\n' "$created" | sed -n 's/^ *Username: *//p')
password=$(printf '%s\n' "$created" | sed -n 's/^ *Password: *//p')
if [ -n "$password" ]; then
  say "Log in as $admin with: $password"
  say "(made at the first start; change it after logging in: user menu, Settings)"
else
  say "Log in with the admin account you set up before; the Setup panel shows what is still missing."
fi
