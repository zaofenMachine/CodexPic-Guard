param([string]$Config = $(Join-Path $env:LOCALAPPDATA 'CodexPicGuard\guard.json'))
$ErrorActionPreference = 'Stop'
$guardPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $guardPython -m codexpic_guard disable --config $Config
if ($LASTEXITCODE -ne 0) { throw 'Restore failed. Nothing conflicting was overwritten.' }
Write-Host 'Settings restored. Restart Codex before stopping the guard; existing sessions may still need it.'
