param(
    [string]$PythonBin = "python",
    [string]$PluginName = "agy-codex-orbit"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command $PythonBin -CommandType Application -ErrorAction SilentlyContinue)) {
    throw "Python 3.11 or newer is required. Pass -PythonBin with the path to your Python interpreter."
}

& $PythonBin (Join-Path $PSScriptRoot "setup_plugin.py") --plugin-name $PluginName
exit $LASTEXITCODE
