param([string]$Config = $(Join-Path $env:LOCALAPPDATA 'CodexPicGuard\guard.json'))
$ErrorActionPreference = 'Stop'
$guardPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $guardPython -m codexpic_guard stop --config $Config
if ($LASTEXITCODE -ne 0) { throw 'Stop refused or failed; disable and restart Codex first.' }
