#!/usr/bin/env bash
set -euo pipefail

server_name=${1-agy-codex-orbit_codex_orbit}
addr=${ANTIGRAVITY_LS_ADDRESS:-localhost:5387}
if [[ -z ${ANTIGRAVITY_CSRF_TOKEN:-} ]]; then
    printf '%s\n' 'ANTIGRAVITY_CSRF_TOKEN is not set. Run this script from an Antigravity terminal.' >&2
    exit 1
fi

body=$("${PYTHON_BIN:-python3}" -c '
import json
import sys

print(json.dumps({"serverName": sys.argv[1]}, separators=(",", ":")))
' "$server_name")

response=$(curl --fail --silent --show-error --max-time 10 \
    --request POST \
    --header "Content-Type: application/json" \
    --header "x-codeium-csrf-token: $ANTIGRAVITY_CSRF_TOKEN" \
    --data "$body" \
    -- "http://$addr/exa.language_server_pb.LanguageServerService/RefreshMcpServers")

printf "Refreshed MCP server '%s' via %s\n" "$server_name" "$addr"
if [[ -n $response ]]; then
    printf '%s\n' "$response"
fi
