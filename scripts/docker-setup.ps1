# ScarabHive in Docker (Docker Desktop on Windows), set up and started with one command:
#
#   powershell -ExecutionPolicy Bypass -File scripts\docker-setup.ps1
#
# Gives this installation its own signing key (config\local.env, named in config\local.yaml), asks for an
# OpenRouter key if none is set, builds and starts the container, and shows the admin's first password.
# Run it again at any time: what is already there is never overwritten.
#
# Port and interface: SCARABHIVE_PORT (default 8000) and SCARABHIVE_BIND (default 127.0.0.1), in the shell or in
# .env, e.g.  $env:SCARABHIVE_PORT = "8040"; powershell -ExecutionPolicy Bypass -File scripts\docker-setup.ps1
#
# Continue, not Stop: Windows PowerShell 5.1 turns a native command's redirected stderr line into a terminating
# error -- the first health probe of a container still starting would end the script. Exit codes are checked instead.
$ErrorActionPreference = "Continue"

Set-Location (Join-Path $PSScriptRoot "..")
$envFile = "config\local.env"
$yamlFile = "config\local.yaml"
$minKey = 32  # agent_system.auth.security.MIN_SECRET_KEY_LENGTH
# UTF-8 without a BOM: PowerShell 5.1's own encodings are not what the loader reads best.
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Fail($message) { Write-Host "docker-setup: $message" -ForegroundColor Red; exit 1 }
# A line appended as a line of its own, also to a file that does not end in a line break.
function Add-Line($path, $line) {
    $full = Join-Path (Get-Location) $path
    $text = if (Test-Path $path) { [IO.File]::ReadAllText($full) } else { "" }
    if ($text -and -not $text.EndsWith("`n")) { $text += "`n" }
    [IO.File]::WriteAllText($full, $text + $line + "`n", $utf8)
}
# A name set in a file the container reads: local.env, else secrets.env. The shell's environment never reaches it.
function Has-Var($name) {
    foreach ($file in @($envFile, "config\secrets.env")) {
        if ((Test-Path $file) -and (Select-String -Path $file -Pattern "^$name=." -Quiet)) { return $true }
    }
    return $false
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail "docker is not installed (Docker Desktop)" }
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Fail "docker compose (v2) is not available" }
if (-not (Test-Path docker-compose.yml)) { Fail "run this from a ScarabHive checkout" }

# 1. The signing key: the value in local.env, the reference in local.yaml -- config.yaml ships a published one.
$held = ""
if (Test-Path $envFile) {
    $line = Select-String -Path $envFile -Pattern "^AUTH_SECRET_KEY=(.*)$" | Select-Object -First 1
    if ($line) { $held = $line.Matches[0].Groups[1].Value }
}
if ($held.Length -ge $minKey) {
    Write-Host "Signing key: already in $envFile"
} elseif ($held) {
    Fail "AUTH_SECRET_KEY in $envFile has $($held.Length) characters, the API needs $minKey or more. Delete that line, then run this again: it makes a new one."
} else {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    Add-Line $envFile ("AUTH_SECRET_KEY=" + (($bytes | ForEach-Object { $_.ToString("x2") }) -join ""))
    Write-Host "Signing key: made, in $envFile"
}
if ((Test-Path $yamlFile) -and (Select-String -Path $yamlFile -Pattern "AUTH_SECRET_KEY" -SimpleMatch -Quiet)) {
    Write-Host "Signing key: already named in $yamlFile"
} elseif ((Test-Path $yamlFile) -and (Select-String -Path $yamlFile -Pattern "^auth:" -Quiet)) {
    Fail "$yamlFile has an auth: section of its own. Add this line under it, then run this again:`n  secret_key: `"`${AUTH_SECRET_KEY}`""
} else {
    Add-Line $yamlFile "auth:`n  secret_key: `"`${AUTH_SECRET_KEY}`""
    Write-Host "Signing key: named in $yamlFile"
}

# 2. A key for the chat. Optional: the Setup panel shows what is missing, and keys are added to local.env later.
if (Has-Var "OPENROUTER_API_KEY") {
    Write-Host "OpenRouter key: already set"
} else {
    $secure = Read-Host "OpenRouter API key (https://openrouter.ai/keys; Enter to skip)" -AsSecureString
    $key = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    if ($key) {
        Add-Line $envFile "OPENROUTER_API_KEY=$key"
        Write-Host "OpenRouter key: saved in $envFile"
    } else {
        Write-Host "OpenRouter key: skipped -- add OPENROUTER_API_KEY=... to $envFile, then: docker compose restart"
    }
}

# 3. Build and start.
Write-Host "Building and starting (the first build takes several minutes) ..."
docker compose up -d --build
if ($LASTEXITCODE -ne 0) {
    Fail "the start failed. If the port is taken, choose another: `$env:SCARABHIVE_PORT = `"8040`", then run this again"
}

Write-Host "Waiting for the API ..."
$probe = "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8000/api/health', timeout=3)"
for ($try = 0; ; $try++) {
    docker compose exec -T scarabhive python -c $probe *> $null
    if ($LASTEXITCODE -eq 0) { break }
    if ($try -ge 90) {
        docker compose logs --tail 30 scarabhive
        Fail "the API did not come up in three minutes; the log above says why"
    }
    Start-Sleep -Seconds 2
}

Write-Host ""
# where compose published it (SCARABHIVE_PORT/BIND from the shell or .env), e.g. 127.0.0.1:8040
$address = docker compose port scarabhive 8000 2>$null | Select-Object -Last 1
if (-not $address) { $address = "127.0.0.1:8000" }
Write-Host "ScarabHive runs: http://$address"
# The first start makes the admin and prints its login once, on stderr: "Default admin created -- ...",
# then "  Username: <name>" and "  Password: <password>".
$log = docker compose logs --no-log-prefix scarabhive 2>&1 | ForEach-Object { "$_" }
$created = $log | Select-String -Pattern "^Default admin created" -Context 0,2 | Select-Object -Last 1
$after = if ($created) { $created.Context.PostContext } else { @() }
$admin = if ($after.Count -ge 1 -and $after[0] -match "Username:\s*(\S+)") { $Matches[1] } else { "" }
$password = if ($after.Count -ge 2 -and $after[1] -match "Password:\s*(\S+)") { $Matches[1] } else { "" }
if ($password) {
    Write-Host "Log in as $admin with: $password"
    Write-Host "(made at the first start; change it after logging in: user menu, Settings)"
} else {
    Write-Host "Log in with the admin account you set up before; the Setup panel shows what is still missing."
}
