#!/bin/sh
set -eu
cd "$(dirname "$0")"
umask 077
[ -f .env ] && . ./.env

BASE="http://127.0.0.1:${N8N_PORT:-5678}"
SCOPES='["workflow:read","workflow:list","execution:read","execution:list"]'
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

touch CREDENTIALS
get() { sed -n "s/^$1=//p" CREDENTIALS; }
field() { python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));d=d.get("data",d)
for k in sys.argv[2].split("."): d=d.get(k) if isinstance(d,dict) else None
print("" if d is None else d)' "$1" "$2"; }

EMAIL=$(get N8N_OWNER_EMAIL)
EMAIL="${EMAIL:-${N8N_OWNER_EMAIL:-admin@n8n.local}}"
PASS=$(get N8N_OWNER_PASSWORD)
if [ -z "$PASS" ]; then
  PASS="N8n-$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 20)-7"
  printf 'N8N_URL=%s\nN8N_OWNER_EMAIL=%s\nN8N_OWNER_PASSWORD=%s\n' \
    "${N8N_PUBLIC_URL:-http://localhost:5678/}" "$EMAIL" "$PASS" >> CREDENTIALS
fi

SETUP=$(printf '{"email":"%s","firstName":"ScarabHive","lastName":"Admin","password":"%s"}' "$EMAIL" "$PASS" |
  curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/rest/owner/setup" \
  -H 'Content-Type: application/json' --data-binary @-)
echo "owner/setup: HTTP $SETUP (400 = owner exists already)"

LOGIN=$(printf '{"emailOrLdapLoginId":"%s","password":"%s"}' "$EMAIL" "$PASS" |
  curl -s -c "$TMP/jar" -o "$TMP/login.json" -w '%{http_code}' -X POST "$BASE/rest/login" \
  -H 'Content-Type: application/json' --data-binary @-)
echo "login: HTTP $LOGIN"
[ "$LOGIN" = "200" ] || { head -c 300 "$TMP/login.json"; echo; exit 1; }

if [ -n "$(get N8N_API_KEY)" ]; then
  echo "api key: already in CREDENTIALS"
else
  KEY=$(curl -s -b "$TMP/jar" -o "$TMP/key.json" -w '%{http_code}' -X POST "$BASE/rest/api-keys" \
    -H 'Content-Type: application/json' \
    -d "{\"label\":\"scarabhive\",\"expiresAt\":null,\"scopes\":$SCOPES}")
  RAW=$(field "$TMP/key.json" rawApiKey)
  [ "$KEY" = "200" ] && [ -n "$RAW" ] || { echo "api-keys: HTTP $KEY"; head -c 400 "$TMP/key.json"; echo; exit 1; }
  printf 'N8N_API_KEY=%s\n' "$RAW" >> CREDENTIALS
  echo "api key: created (read-only scopes), stored in CREDENTIALS"
fi

curl -s -b "$TMP/jar" "$BASE/rest/module-settings" > "$TMP/modules.json"
if [ "$(field "$TMP/modules.json" mcp.mcpAccessEnabled)" != "True" ]; then
  echo "instance MCP is off: N8N_MCP_MANAGED_BY_ENV and N8N_MCP_ACCESS_ENABLED must be \"true\" in docker-compose.yml, then docker compose up -d"
  exit 1
fi
echo "instance MCP: on"

if [ -n "$(get N8N_MCP_KEY)" ]; then
  echo "mcp key: already in CREDENTIALS"
else
  ROT=$(curl -s -b "$TMP/jar" -o "$TMP/mcp.json" -w '%{http_code}' -X POST "$BASE/rest/mcp/api-key/rotate")
  RAW=$(field "$TMP/mcp.json" apiKey)
  [ "$ROT" = "200" ] && [ -n "$RAW" ] || { echo "mcp key rotate: HTTP $ROT"; exit 1; }
  printf 'N8N_MCP_KEY=%s\n' "$RAW" >> CREDENTIALS
  echo "mcp key: rotated, stored in CREDENTIALS"
fi
