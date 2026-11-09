param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('control', 'gateway', 'tunnel')]
    [string]$Component
)
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$runtime = Join-Path $PSScriptRoot 'private\startup'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$mutex = New-Object System.Threading.Mutex($false, "Local\FUN001-$identity-$Component")
$owned = $false
try {
    try { $owned = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) { exit 0 }
    if (-not (Test-Path -LiteralPath $runtime)) { throw 'Run fun001-startup.ps1 install first.' }
    $recordPath = Join-Path $runtime "$Component.json"
    $previous = $null
    if (Test-Path -LiteralPath $recordPath) {
        $previous = Get-Content -Raw -LiteralPath $recordPath | ConvertFrom-Json
    }
    $record = [ordered]@{
        component = $Component
        supervisor_pid = $PID
        supervisor_started = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('o')
        child_pid = $null
        child_started = $null
    }
    $record | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding UTF8
    if ($Component -eq 'control') {
        $Host.UI.RawUI.WindowTitle = 'FUNF owner console'
        while ($true) {
            & $python -B (Join-Path $PSScriptRoot 'fun001_service.py') serve
            if ($LASTEXITCODE -eq 0) { break }
            Write-Host 'Control service exited. Restarting in 5 seconds with a fresh authorization state.'
            Start-Sleep -Seconds 5
        }
        exit 0
    }
    # Adopt the two processes created during URL setup without interrupting them.
    $child = $null
    $candidates = @()
    if ($previous -and $previous.child_pid) {
        $candidates += @{ id = $previous.child_pid; started = $previous.child_started }
    }
    $legacyPath = Join-Path $PSScriptRoot 'private\http-mcp\processes.json'
    if (Test-Path -LiteralPath $legacyPath) {
        $legacy = Get-Content -Raw -LiteralPath $legacyPath | ConvertFrom-Json
        $pidField = $Component + '_pid'
        $startField = $Component + '_started'
        $candidates += @{ id = $legacy.$pidField; started = $legacy.$startField }
    }
    foreach ($item in $candidates) {
        $candidate = Get-Process -Id $item.id -ErrorAction SilentlyContinue
        if ($candidate -and $candidate.StartTime.ToUniversalTime() -eq ([datetime]$item.started).ToUniversalTime()) {
            $child = $candidate
            break
        }
    }
    while ($true) {
        try {
            if (-not $child) {
                $outputLog = Join-Path $runtime "$Component-out.log"
                $errorLog = Join-Path $runtime "$Component-error.log"
                if ($Component -eq 'gateway') {
                    $child = Start-Process -FilePath $python -ArgumentList @('-B', 'fun001_http.py', 'serve') `
                        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
                        -RedirectStandardOutput $outputLog -RedirectStandardError $errorLog
                } else {
                    $tunnelScript = Join-Path $PSScriptRoot 'start-fun001-tunnel.ps1'
                    $child = Start-Process -FilePath 'powershell.exe' -ArgumentList @(
                        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                        ('"' + $tunnelScript + '"')) -WindowStyle Hidden -PassThru `
                        -RedirectStandardOutput $outputLog -RedirectStandardError $errorLog
                }
            }
            $record.child_pid = $child.Id
            $record.child_started = $null
            if (-not $child.HasExited) {
                $record.child_started = $child.StartTime.ToUniversalTime().ToString('o')
            }
            $record | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding UTF8
            $child.WaitForExit()
        } catch {
            $eventLine = (Get-Date).ToUniversalTime().ToString('o') + ' ' + $_.Exception.GetType().Name
            Add-Content -LiteralPath (Join-Path $runtime "$Component-supervisor.log") -Value $eventLine
        }
        if ($child -and $child.HasExited) {
            $child.Dispose()
            $child = $null
        }
        Start-Sleep -Seconds 5
    }
} finally {
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
