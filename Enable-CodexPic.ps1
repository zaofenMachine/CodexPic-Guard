param(
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }),
    [string]$Config = $(Join-Path $env:LOCALAPPDATA 'CodexPicGuard\guard.json')
)
$ErrorActionPreference = 'Stop'
$guardPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $guardPython)) { throw 'Run Setup-CodexPic.ps1 first.' }
& $guardPython -m codexpic_guard enable --codex-home $CodexHome --config $Config
if ($LASTEXITCODE -ne 0) { throw 'Enable failed. Read the error above.' }
Write-Host 'Restart Codex when your running tasks can be interrupted. This script does not close the app.'
