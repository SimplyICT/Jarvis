#Requires -Version 5.1
<#
.SYNOPSIS
    Install JARVIS into the DeepSeek Harness on Windows.

.DESCRIPTION
    Sets up the three things JARVIS needs to exist:

    1. A `headless` dsh profile -- the one-shot agent runtime the voice shell
       calls. Created under $DSH_HOME\profiles\headless.
    2. The jarvis-second-brain skill, copied into $DSH_HOME\skills so every
       agent session can recall and write memory.
    3. A jarvis.cmd launcher that sets JARVIS_VAULT and starts the voice shell.

    Safe to re-run: existing files are only replaced with -Force, and the
    profile is only rewritten when its contents would change.

.PARAMETER Vault
    Folder of markdown notes to use as the second brain. Defaults to
    <repo>\brain\sample-brain, which is a working demo brain.

.PARAMETER Force
    Overwrite existing skill files and launcher.

.EXAMPLE
    .\install\install.ps1
    .\install\install.ps1 -Vault "D:\notes" -Force
#>
[CmdletBinding()]
param(
    [string] $Vault,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'

function Write-Step  { param([string]$Text) Write-Host "  $Text" }
function Write-Ok    { param([string]$Text) Write-Host "  [ok]   $Text" -ForegroundColor Green }
function Write-Warn  { param([string]$Text) Write-Host "  [warn] $Text" -ForegroundColor Yellow }
function Write-Fail  { param([string]$Text) Write-Host "  [fail] $Text" -ForegroundColor Red }

$repo = Split-Path -Parent $PSScriptRoot
Write-Host ""
Write-Host "  JARVIS installer" -ForegroundColor Cyan
Write-Host "  =============================================="
Write-Host "  repo    $repo"

# --------------------------------------------------------------------------
# 0. Locate DSH_HOME and the harness launcher
# --------------------------------------------------------------------------

$dshHome = $env:DSH_HOME
if (-not $dshHome) {
    # The desktop build keeps its harness home under APPDATA.
    $candidate = Join-Path $env:APPDATA 'dsh-desktop\harness'
    if (Test-Path $candidate) { $dshHome = $candidate }
}

if (-not $dshHome -or -not (Test-Path $dshHome)) {
    Write-Fail "Could not find the DeepSeek Harness home (DSH_HOME)."
    Write-Step "Set it explicitly, for example:"
    Write-Step '  $env:DSH_HOME = "$env:APPDATA\dsh-desktop\harness"'
    exit 1
}
Write-Ok "DSH_HOME  $dshHome"

# The launcher lives in the desktop app bundle, or in a source checkout.
$dshBin = $env:DSH_BIN
if (-not $dshBin) {
    $candidates = @(
        'C:\Program Files\DSH Desktop\resources\app\node_modules\@deepseek-ai\dsh\lib\bin.js',
        (Join-Path $repo 'node_modules\@deepseek-ai\dsh\lib\bin.js')
    )
    foreach ($c in $candidates) { if (Test-Path $c) { $dshBin = $c; break } }
}

if ($dshBin) { Write-Ok "dsh       $dshBin" }
else { Write-Warn "dsh launcher not found -- set DSH_BIN before starting JARVIS" }

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Warn "Node.js not on PATH -- the agent cannot run without it"
}

# --------------------------------------------------------------------------
# 1. Python
# --------------------------------------------------------------------------

$python = $null
foreach ($name in @('python', 'python3', 'py')) {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    if ($found) { $python = $found.Source; break }
}
if ($python) { Write-Ok "python    $python" }
else {
    Write-Fail "Python 3 was not found on PATH. JARVIS needs Python 3.8+."
    exit 1
}

# --------------------------------------------------------------------------
# 2. The headless profile
# --------------------------------------------------------------------------

Write-Host ""
Write-Host "  1. headless profile" -ForegroundColor Cyan

$profileDir = Join-Path $dshHome 'profiles\headless'
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

$profileJson = @'
{
  "name": "dsh-profile-headless",
  "private": true,
  "dsh": {
    "profile": {
      "bundles": [
        "@deepseek-ai/dsh-base",
        "@deepseek-ai/dsh-headless"
      ],
      "patchReload": "live"
    }
  }
}
'@

$profilePath = Join-Path $profileDir 'package.json'
# Windows PowerShell 5.1's `-Encoding UTF8` writes a BOM, and Node's
# JSON.parse rejects a BOM outright -- the profile then fails to boot with a
# cryptic SyntaxError. Always write these files BOM-free.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$existing = if (Test-Path $profilePath) { Get-Content $profilePath -Raw } else { '' }
$existing = if ($existing) { $existing.TrimStart([char]0xFEFF) } else { '' }
if ($existing.Trim() -ne $profileJson.Trim()) {
    [System.IO.File]::WriteAllText($profilePath, $profileJson, $utf8NoBom)
    Write-Ok "wrote $profilePath"
} else {
    Write-Ok "profile already correct"
}

$cordisPath = Join-Path $profileDir 'cordis.yml'
if (-not (Test-Path $cordisPath)) {
    $cordisRoot = @'
# dsh profile root -- an empty entry list. The tree is composed as patches:
# each bundle in package.json's dsh.profile.bundles, then cordis.patch.yml, then any
# --patch overlays. Edit cordis.patch.yml, not this file.
[]
'@
    [System.IO.File]::WriteAllText($cordisPath, $cordisRoot, $utf8NoBom)
    Write-Ok "wrote cordis.yml"
}

$patchPath = Join-Path $profileDir 'cordis.patch.yml'
if (-not (Test-Path $patchPath)) {
    $patchLayer = @'
# Your patch layer for this dsh profile, applied after every bundle layer:
# a top-level YAML array of loader patch entries (id-targeted config
# overrides, disables, and insert lists; `!!js` expressions allowed).
[]
'@
    [System.IO.File]::WriteAllText($patchPath, $patchLayer, $utf8NoBom)
    Write-Ok "wrote cordis.patch.yml"
}

# pnpm needs the store config; inherit it from the web profile when present.
$npmrcPath = Join-Path $profileDir '.npmrc'
if (-not (Test-Path $npmrcPath)) {
    $webNpmrc = Join-Path $dshHome 'profiles\web\.npmrc'
    if (Test-Path $webNpmrc) {
        Copy-Item $webNpmrc $npmrcPath
        Write-Ok "inherited .npmrc from the web profile"
    }
}

# --------------------------------------------------------------------------
# 3. The second-brain skill
# --------------------------------------------------------------------------

Write-Host ""
Write-Host "  2. second-brain skill" -ForegroundColor Cyan

$skillSource = Join-Path $repo '.dsh\skills\jarvis-second-brain'
$skillsRoot = Join-Path $dshHome 'skills'
$skillTarget = Join-Path $skillsRoot 'jarvis-second-brain'

if (-not (Test-Path $skillSource)) {
    Write-Fail "skill source missing: $skillSource"
    exit 1
}

New-Item -ItemType Directory -Force -Path $skillTarget | Out-Null
Copy-Item (Join-Path $skillSource 'SKILL.md') (Join-Path $skillTarget 'SKILL.md') -Force
Write-Ok "installed skill -> $skillTarget"

# --------------------------------------------------------------------------
# 4. Vault
# --------------------------------------------------------------------------

Write-Host ""
Write-Host "  3. second brain" -ForegroundColor Cyan

if (-not $Vault) { $Vault = Join-Path $repo 'brain\sample-brain' }

if (-not (Test-Path $Vault)) {
    New-Item -ItemType Directory -Force -Path $Vault | Out-Null
    Write-Ok "created empty vault $Vault"
}

$Vault = (Resolve-Path $Vault).Path
$brainPy = Join-Path $repo 'brain\brain.py'

Write-Step "indexing $Vault ..."
& $python $brainPy build --vault $Vault
if ($LASTEXITCODE -eq 0) { Write-Ok "brain indexed" }
else { Write-Warn "indexing failed -- run brain\brain.py build manually" }

# --------------------------------------------------------------------------
# 5. Launcher
# --------------------------------------------------------------------------

Write-Host ""
Write-Host "  4. launcher" -ForegroundColor Cyan

$launcherPath = Join-Path $repo 'jarvis.cmd'
$dshBinForLauncher = if ($dshBin) { $dshBin } else { '%DSH_BIN%' }

$launcher = @"
@echo off
REM JARVIS launcher -- generated by install\install.ps1
setlocal
set "JARVIS_VAULT=$Vault"
set "JARVIS_BRAIN=$repo\brain\brain.py"
set "DSH_BIN=$dshBinForLauncher"
set "DSH_PROFILE=headless"
set "JARVIS_PORT=4731"
cd /d "$repo\voice"
python server.py
endlocal
"@

if ((Test-Path $launcherPath) -and -not $Force) {
    Write-Warn "jarvis.cmd exists -- re-run with -Force to overwrite"
} else {
    Set-Content -Path $launcherPath -Value $launcher -Encoding ASCII
    Write-Ok "wrote $launcherPath"
}

# --------------------------------------------------------------------------
# 6. Verify
# --------------------------------------------------------------------------

Write-Host ""
Write-Host "  5. verification" -ForegroundColor Cyan

$suites = @(
    @{ Name = 'brain'; Path = (Join-Path $repo 'brain\test_brain.py') },
    @{ Name = 'voice'; Path = (Join-Path $repo 'voice\test_voice.py') }
)

foreach ($suite in $suites) {
    if (-not (Test-Path $suite.Path)) { continue }

    # unittest writes progress to stderr. Piping a native command's stderr makes
    # PowerShell raise a NativeCommandError that $ErrorActionPreference='Stop'
    # turns fatal, so capture to temp files outside the pipeline instead.
    $testLog = Join-Path $env:TEMP ("jarvis-{0}-tests-{1}.txt" -f $suite.Name, $PID)
    # -ArgumentList joins its array with spaces, so a path containing spaces must
    # carry its own quotes or it is split into several arguments.
    $testProc = Start-Process -FilePath $python -ArgumentList @("`"$($suite.Path)`"") `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $testLog -RedirectStandardError "$testLog.err"

    $testOutput = ''
    foreach ($f in @($testLog, "$testLog.err")) {
        if (Test-Path $f) { $testOutput += (Get-Content $f -Raw) + "`n" }
    }
    Remove-Item $testLog, "$testLog.err" -ErrorAction SilentlyContinue

    if ($testProc.ExitCode -eq 0) {
        $ran = if ($testOutput -match 'Ran (\d+) test') { $Matches[1] } else { '?' }
        Write-Ok "$ran $($suite.Name) tests passed"
    } else {
        Write-Warn "some $($suite.Name) tests failed:"
        $testOutput -split "`n" | Where-Object { $_ -match '\S' } |
            Select-Object -Last 12 | ForEach-Object { Write-Step $_.TrimEnd() }
    }
}

Write-Host ""
Write-Host "  ==============================================" -ForegroundColor Cyan
Write-Host "  JARVIS is installed." -ForegroundColor Green
Write-Host ""
Write-Host "  vault     $Vault"
Write-Host "  launch    $launcherPath"
Write-Host "  or        cd `"$repo\voice`"; python server.py"
Write-Host ""
Write-Host "  Then open  http://127.0.0.1:4731"
Write-Host "  Turn the EAR on and say: Jarvis, what do we charge for EDR?"
Write-Host ""
Write-Warn "Speech recognition needs Chrome or Edge. Put your real notes in the"
Write-Warn "vault and re-run the installer, or edit JARVIS_VAULT in jarvis.cmd."
Write-Host ""
