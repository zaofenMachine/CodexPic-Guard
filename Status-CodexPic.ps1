param([string]$Config = $(Join-Path $env:LOCALAPPDATA 'CodexPicGuard\guard.json'))
$guardPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $guardPython -m codexpic_guard status --config $Config
exit $LASTEXITCODE
