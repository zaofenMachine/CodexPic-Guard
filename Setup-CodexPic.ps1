param(
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }),
    [string]$DataDir = $(Join-Path $env:LOCALAPPDATA 'CodexPicGuard'),
    [string]$Proxy = '',
    [string]$Upstream = '',
    [int]$Port = 17993
)
$ErrorActionPreference = 'Stop'
$guardRoot = $PSScriptRoot
$guardPython = Join-Path $guardRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $guardPython)) {
    & python -m venv (Join-Path $guardRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed.' }
}
& $guardPython -m pip install --disable-pip-version-check -r (Join-Path $guardRoot 'requirements-lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
& $guardPython -m pip install --disable-pip-version-check --no-deps -e $guardRoot
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
$guardArgs = @('-m','codexpic_guard','prepare','--codex-home',$CodexHome,'--data-dir',$DataDir,'--port',"$Port")
if ($Proxy) { $guardArgs += @('--proxy',$Proxy) }
if ($Upstream) { $guardArgs += @('--upstream',$Upstream) }
& $guardPython @guardArgs
if ($LASTEXITCODE -ne 0) { throw 'Preparation failed. Codex has not been enabled.' }
Write-Host 'Prepared only. To activate, run Enable-CodexPic.ps1 and then restart Codex.'
