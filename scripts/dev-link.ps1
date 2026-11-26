param(
    [string]$PluginName = "agy-codex-orbit"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoRootSlash = $repoRoot -replace "\\", "/"
$pluginsDir = Join-Path $env:USERPROFILE ".gemini\config\plugins"
$targetDir = Join-Path $pluginsDir $PluginName

if (-not (Test-Path $pluginsDir)) {
    New-Item -ItemType Directory -Path $pluginsDir -Force | Out-Null
}

# Windows Go directory scanners may skip NTFS junctions (ReparsePoint), so stage a real directory.
if (Test-Path $targetDir) {
    $item = Get-Item $targetDir -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        [System.IO.Directory]::Delete($targetDir)
    }
}

if (-not (Test-Path $targetDir)) {
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
}

Copy-Item (Join-Path $repoRoot "plugin.json") (Join-Path $targetDir "plugin.json") -Force

$assetsSrc = Join-Path $repoRoot "assets"
if (Test-Path $assetsSrc) {
    $assetsDst = Join-Path $targetDir "assets"
    New-Item -ItemType Directory -Path $assetsDst -Force | Out-Null
    Copy-Item (Join-Path $assetsSrc "*") $assetsDst -Recurse -Force
}

$skillsSrc = Join-Path $repoRoot "skills"
if (Test-Path $skillsSrc) {
    $skillsDst = Join-Path $targetDir "skills"
    if (Test-Path $skillsDst) { Remove-Item $skillsDst -Recurse -Force }
    Copy-Item $skillsSrc $skillsDst -Recurse -Force
}

# Write dev mcp_config.json pointing live at this repo's .venv, server/main.py, and dist/index.html
# while keeping the repository's own mcp_config.json portable (${PLUGIN_ROOT} / ${PLUGIN_DATA}).
$devMcpConfig = @{
    mcpServers = @{
        codex_orbit = @{
            command = "$repoRootSlash/.venv/Scripts/python.exe"
            args = @(
                "-u",
                "$repoRootSlash/server/main.py",
                "--data-dir=`${PLUGIN_DATA}"
            )
            env = @{
                PYTHONUTF8 = "1"
                PYTHONIOENCODING = "utf-8"
                CODEX_ORBIT_APP_HTML = "$repoRootSlash/dist/index.html"
            }
        }
    }
}
$devMcpJson = $devMcpConfig | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText(
    (Join-Path $targetDir "mcp_config.json"),
    $devMcpJson + "`n",
    [System.Text.UTF8Encoding]::new($false)
)

Write-Output "Deployed dev plugin to $targetDir (live server & UI from $repoRoot)"
