<#
Kill Python processes that look like they belong to this project.
This is conservative: it filters processes whose command-line references the
project path, the venv, or the `agent_system.agent.interface_api` module.

Usage: Run from repository root or pass -RepoRoot to specify location:
    .\kill_project_python_processes.ps1 -RepoRoot "E:\Projects\AgentSystem"

It will list matches and prompt for confirmation unless -Force is supplied.
#>
param(
    [string]$RepoRoot = (Get-Location).Path,
    [switch]$Force
)

function _Get-CandidatePids($repoRoot) {
    try {
        $blocks = (wmic process where "name like '%python%'" get ProcessId,Name,CommandLine /format:list) -split "\n\n"
    } catch {
        return @()
    }

    $candidates = @()
    foreach ($b in $blocks) {
        if (-not $b.Trim()) { continue }
        $pid = $null
        $cmd = ""
        foreach ($line in $b -split "\r?\n") {
            if ($line -like 'ProcessId=*') { $pid = $line -replace 'ProcessId=', '' }
            if ($line -like 'CommandLine=*') { $cmd = $line -replace 'CommandLine=', '' }
        }
        if ($pid -and $cmd) {
            $lc = $cmd.ToLower()
            if ($lc -like "*$($repoRoot.ToLower())*" -or $lc -like "*\\.venv\\*" -or $lc -like "*-m agent_system.agent.interface_api*") {
                $candidates += [PSCustomObject]@{ PID = [int]$pid; CommandLine = $cmd }
            }
        }
    }
    return $candidates
}

$candidates = _Get-CandidatePids $RepoRoot
if (-not $candidates -or $candidates.Count -eq 0) {
    Write-Host "No project-related python processes found."
    exit 0
}

Write-Host "Found the following candidate processes:"
$candidates | Format-Table -AutoSize

if (-not $Force) {
    $confirm = Read-Host "Kill these processes? Type 'yes' to confirm"
    if ($confirm -ne 'yes') { Write-Host 'Aborting.'; exit 1 }
}

foreach ($c in $candidates) {
    try {
        taskkill /F /PID $c.PID /T | Out-Null
        Write-Host "Killed PID $($c.PID)"
    } catch {
        Write-Warning "Failed to kill PID $($c.PID): $_"
    }
}

Write-Host 'Done.'
