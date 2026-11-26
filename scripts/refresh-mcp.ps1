param(
    [string]$ServerName = "agy-codex-orbit_codex_orbit"
)

$ErrorActionPreference = "Stop"

$addr = if ($env:ANTIGRAVITY_LS_ADDRESS) { $env:ANTIGRAVITY_LS_ADDRESS } else { "localhost:5387" }
if (-not $env:ANTIGRAVITY_CSRF_TOKEN) {
    throw "ANTIGRAVITY_CSRF_TOKEN is not set. Run this script from an Antigravity terminal."
}

$headers = @{
    "Content-Type" = "application/json"
    "x-codeium-csrf-token" = $env:ANTIGRAVITY_CSRF_TOKEN
}
$body = @{ serverName = $ServerName } | ConvertTo-Json -Compress

$resp = Invoke-RestMethod -Method Post `
    -Uri "http://$addr/exa.language_server_pb.LanguageServerService/RefreshMcpServers" `
    -Headers $headers `
    -Body $body `
    -TimeoutSec 10

Write-Output "Refreshed MCP server '$ServerName' via $addr"
if ($resp) { $resp | ConvertTo-Json -Depth 4 }
