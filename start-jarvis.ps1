#Requires -Version 5.1
<#
.SYNOPSIS
    Start the JARVIS voice shell in its own window.

.DESCRIPTION
    Starts voice\server.py detached from whatever launched this script, so the
    shell keeps running after the terminal or agent session that started it goes
    away. Opens the browser at the right origin afterwards.

    Use this instead of running `python server.py` by hand when you want JARVIS
    to stay up.

.PARAMETER Vault
    Notes folder to use as the second brain. Defaults to the sample brain.

.PARAMETER Port
    Port to listen on. Defaults to 4731.

.PARAMETER NoBrowser
    Do not open a browser window.

.EXAMPLE
    .\start-jarvis.ps1
    .\start-jarvis.ps1 -Vault "D:\notes" -Port 4740
#>
[CmdletBinding()]
param(
    [string] $Vault,
    [int]    $Port = 4731,
    [switch] $NoBrowser
)

$ErrorActionPreference = 'Stop'

function Write-Ok   { param([string]$t) Write-Host "  [ok]   $t" -ForegroundColor Green }
function Write-Warn { param([string]$t) Write-Host "  [warn] $t" -ForegroundColor Yellow }
function Write-Fail { param([string]$t) Write-Host "  [fail] $t" -ForegroundColor Red }

$repo = $PSScriptRoot
$voice = Join-Path $repo 'voice'
$server = Join-Path $voice 'server.py'
$url = "http://127.0.0.1:$Port"

Write-Host ""
Write-Host "  Starting JARVIS" -ForegroundColor Cyan
Write-Host "  =============================================="

if (-not (Test-Path $server)) {
    Write-Fail "voice\server.py not found next to this script."
    exit 1
}

# --------------------------------------------------------------------------
# Already running?
# --------------------------------------------------------------------------

$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Ok "JARVIS is already listening on $url"
    if (-not $NoBrowser) { Start-Process $url }
    Write-Host ""
    Write-Host "  Open:  $url"
    Write-Host "  To restart: stop the python process, then run this again."
    Write-Host ""
    exit 0
}

# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------

$python = $null
foreach ($name in @('python', 'python3', 'py')) {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    if ($found) { $python = $found.Source; break }
}
if (-not $python) {
    Write-Fail "Python 3 was not found on PATH."
    exit 1
}

# --------------------------------------------------------------------------
# Vault
# --------------------------------------------------------------------------

if (-not $Vault) { $Vault = Join-Path $repo 'brain\sample-brain' }
if (-not (Test-Path $Vault)) {
    Write-Warn "vault does not exist yet: $Vault"
    New-Item -ItemType Directory -Force -Path $Vault | Out-Null
}
$Vault = (Resolve-Path $Vault).Path

# Keep the brain index fresh so recall is not answering from a stale snapshot.
$brainPy = Join-Path $repo 'brain\brain.py'
if (Test-Path $brainPy) {
    Write-Host "  indexing the brain ..."
    $log = Join-Path $env:TEMP "jarvis-index-$PID.txt"
    Start-Process -FilePath $python `
        -ArgumentList @("`"$brainPy`"", 'build', '--vault', "`"$Vault`"") `
        -NoNewWindow -Wait -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    Remove-Item $log, "$log.err" -ErrorAction SilentlyContinue
    Write-Ok "brain indexed"
}

# --------------------------------------------------------------------------
# Launch detached, then wait for it to answer
# --------------------------------------------------------------------------

$env:JARVIS_VAULT = $Vault
$env:JARVIS_BRAIN = $brainPy
$env:JARVIS_PORT = "$Port"
$env:DSH_PROFILE = 'headless'

Write-Host "  launching server ..."
$proc = Start-Process -FilePath $python -ArgumentList @("`"$server`"") `
    -WorkingDirectory $voice -PassThru `
    -RedirectStandardOutput (Join-Path $env:TEMP 'jarvis-server.log') `
    -RedirectStandardError  (Join-Path $env:TEMP 'jarvis-server.err')

# Poll until it answers, so we never report success on a server that died.
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    if ($proc.HasExited) { break }
    try {
        $r = Invoke-WebRequest -Uri "$url/api/status" -TimeoutSec 3 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
}

Write-Host ""
if (-not $ready) {
    Write-Fail "the server did not come up on $url"
    $errFile = Join-Path $env:TEMP 'jarvis-server.err'
    if (Test-Path $errFile) {
        Write-Host "  ---- server stderr ----"
        Get-Content $errFile -Tail 15 | ForEach-Object { Write-Host "  $_" }
    }
    Write-Host ""
    Write-Host "  The port may be in use. Try: .\start-jarvis.ps1 -Port 4740"
    exit 1
}

Write-Ok "JARVIS is up (pid $($proc.Id))"
Write-Host ""
Write-Host "  ==============================================" -ForegroundColor Cyan
Write-Host "  OPEN THIS:" -ForegroundColor Green
Write-Host ""
Write-Host "      $url" -ForegroundColor Cyan
Write-Host ""
Write-Host "  browser   Chrome or Edge (speech recognition needs one)"
Write-Host "  vault     $Vault"
Write-Host "  logs      $env:TEMP\jarvis-server.log"
Write-Host "  stop      Stop-Process -Id $($proc.Id)"
Write-Host ""
Write-Warn "Keep this window open only if you want to watch the log. The server"
Write-Warn "runs detached, so closing it will not stop JARVIS."
Write-Host ""

if (-not $NoBrowser) { Start-Process $url }
