# ScarabHive: install into .venv, give this installation its own signing key and its admin a password of its own,
# start the API, open the browser.
#   powershell -ExecutionPolicy Bypass -File install.ps1
# Running it again installs what a git pull added and starts the API (stop a running one first).
# INSTALLATION.md has the steps by hand.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Invoke-Checked([string]$exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "failed ($LASTEXITCODE): $exe $arguments" }
}

function Test-Python([string]$exe, [string[]]$prefix) {
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { return $false }
    try { & $exe @prefix -c "import sys; sys.exit(sys.version_info < (3, 11))" *> $null } catch { return $false }
    return $LASTEXITCODE -eq 0
}

# The py launcher first: "python" may be the Microsoft Store's stand-in.
$pyExe = $null
$pyPrefix = @()
if (Test-Python "py" @("-3")) { $pyExe = "py"; $pyPrefix = @("-3") }
elseif (Test-Python "python" @()) { $pyExe = "python" }
if (-not $pyExe) {
    Write-Host "Python 3.11 or newer is needed: https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path .venv)) { Invoke-Checked $pyExe ($pyPrefix + @("-m", "venv", ".venv")) }
$bin = Join-Path $PSScriptRoot ".venv\Scripts"
$python = Join-Path $bin "python.exe"
if (-not (Test-Path $python)) {
    Write-Host ".venv holds no Python: remove the folder and run this again." -ForegroundColor Red
    exit 1
}

Invoke-Checked $python @("-m", "pip", "install", "-U", "pip")
Invoke-Checked $python @("-m", "pip", "install", "-e", ".")

# requirements/optional.txt: what may fail without failing the install (today reportlab's cairo
# backend, which draws SVG layers in images; on Windows pycairo comes as a wheel).
& $python -m pip install -r requirements/optional.txt
$svgOff = $LASTEXITCODE -ne 0
if ($svgOff) {
    Write-Host "Going on without SVG layers in images (requirements/optional.txt, see above). To add them: run install.ps1 again." -ForegroundColor Yellow
}

& $python -m agent_system.config.local_layer signing-key
if ($LASTEXITCODE -ne 0) { Write-Host "Going on: the Setup panel shows the signing key." -ForegroundColor Yellow }
# Not past a failed or aborted step: an admin of an older install may still open with admin123.
& $python -m agent_system.auth.first_admin
if ($LASTEXITCODE -ne 0) {
    Write-Host "The admin password was not set (see above), so the API is not started. Run this script again." -ForegroundColor Red
    exit 1
}

$port = if ($env:PORT) { $env:PORT } else { "8000" }
$url = "http://127.0.0.1:$port"
Write-Host ""
Write-Host "Starting ScarabHive at $url -- the browser opens when it answers."
Write-Host "Log in with the admin account. Then open the Setup panel (grid icon, type 'setup')"
Write-Host "and enter your OpenRouter key. Stop the API with Ctrl+C;"
Write-Host "start it again with: .venv\Scripts\agent-api.exe"
if ($svgOff) { Write-Host "SVG layers in images are off: run install.ps1 again." -ForegroundColor Yellow }
Write-Host ""

$null = Start-Job -ArgumentList $url -ScriptBlock {
    param($url)
    for ($i = 0; $i -lt 180; $i++) {
        try {
            Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null
            break
        } catch {
            if ($_.Exception.Response) { break }  # it answers, if only with a login
            Start-Sleep -Seconds 1
        }
    }
    if ($i -lt 180) { Start-Process $url }
}
& (Join-Path $bin "agent-api.exe")
