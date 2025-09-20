# List Python processes with PID, User, CommandLine, and WorkingDirectory (where available)
# Usage: .\list_python_processes.ps1
# Runs on Windows PowerShell / PowerShell Core

# Try CIM for richer details
try {
    $procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -or $_.Name -match 'py' }
    $out = $procs | ForEach-Object {
        $cmd = ($_.CommandLine -replace '\\"', '"')
        [PSCustomObject]@{
            PID = $_.ProcessId
            Name = $_.Name
            CommandLine = $cmd
            CreationDate = $_.CreationDate
            ExecutablePath = $_.ExecutablePath
        }
    }
    $out | Sort-Object PID | Format-Table -AutoSize
}
catch {
    # Fallback to Get-Process if CIM fails
    Get-Process | Where-Object { $_.Name -match 'python' -or $_.Name -match 'py' } | Select-Object Id, ProcessName, @{Name='CPU';Expression={$_.CPU}} | Format-Table -AutoSize
}
