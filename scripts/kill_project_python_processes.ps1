<#
Kill Python processes that look like they belong to this project.
This script is conservative: it filters processes whose command-line references the
project path, a local venv, uvicorn, or the `agent_system.agent.interface_api` module.

Usage: Run from repository root or pass -RepoRoot to specify location:
    .\kill_project_python_processes.ps1 -RepoRoot "E:\Projects\AgentSystem"

It will list matches and prompt for confirmation unless -Force is supplied.
By default the script performs a dry-run (lists candidates). Use -Force to actually
terminate the processes.
#>

param(
    [string]$RepoRoot = (Get-Location).Path,
    [switch]$Force
)

function Get-ProjectPythonPids($repoRoot) {
    $candidates = @()
    try {
        $procs = Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object { $_.Name -match 'python(\.exe|w\.exe)?' }
    } catch {
        Write-Host "Failed to enumerate processes via CIM: $_" -ForegroundColor Yellow
        return $candidates
    }

    foreach ($proc in $procs) {
        $cmd = $proc.CommandLine
        if (-not $cmd) { continue }
        $lc = $cmd.ToLower()
        if ($lc -like "*$($repoRoot.ToLower())*" -or $lc -like "*.venv\\*" -or $lc -like "*uvicorn*" -or $lc -like "*-m agent_system.agent.interface_api*" -or $lc -like "*agent_system*") {
            $candidates += [PSCustomObject]@{ PID = [int]$proc.ProcessId; CommandLine = $cmd }
        }
    }
    return $candidates
}

$candidates = Get-ProjectPythonPids $RepoRoot
if (-not $candidates -or $candidates.Count -eq 0) {
    Write-Host "No project-related python processes found." -ForegroundColor Green
    exit 0
}

Write-Host "Found the following candidate processes:" -ForegroundColor Cyan
$candidates | Format-Table -AutoSize

if (-not $Force) {
    $confirm = Read-Host "Kill these processes? Type 'yes' to confirm (or run with -Force to skip prompt)"
    if ($confirm -ne 'yes') { Write-Host 'Aborting.'; exit 1 }
}

foreach ($c in $candidates) {
    try {
        taskkill /F /PID $c.PID /T | Out-Null
        Write-Host "Killed PID $($c.PID)" -ForegroundColor Green
    } catch {
        Write-Warning "Failed to kill PID $($c.PID): $_"
    }
}

Write-Host 'Done.' -ForegroundColor Cyan
