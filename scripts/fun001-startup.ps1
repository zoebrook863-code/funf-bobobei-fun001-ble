param(
    [ValidateSet('install', 'start', 'status', 'disable')]
    [string]$Action = 'status'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $projectRoot 'private\startup'
$launcher = Join-Path $projectRoot 'scripts\launch-fun001-stack.vbs'
$startupEntry = Join-Path ([Environment]::GetFolderPath('Startup')) 'launch-fun001-stack.vbs'
if ($Action -eq 'disable') {
    if (Test-Path -LiteralPath $startupEntry) {
        if ((Get-Content -Raw -LiteralPath $startupEntry) -notmatch 'FUN001 automatic startup') {
            throw 'Startup entry ownership could not be verified.'
        }
        Remove-Item -LiteralPath $startupEntry
    }
    Write-Host 'FUNF login startup disabled. Running components can finish in this login session.'
    exit 0
}
if ($Action -eq 'install') {
    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
    Push-Location $projectRoot
    try {
        $prepareDirectory = @'
import sys
from pathlib import Path
sys.path.insert(0, 'src')
from local_runtime import secure_directory
secure_directory(Path('private/startup'))
'@
        & $python -B -c $prepareDirectory
        if ($LASTEXITCODE -ne 0) { throw 'Cannot secure startup runtime directory.' }
    } finally { Pop-Location }
    if (Test-Path -LiteralPath $startupEntry) {
        if ((Get-Content -Raw -LiteralPath $startupEntry) -notmatch 'FUN001 automatic startup') {
            throw 'A different startup entry already uses this name.'
        }
    }
    $installedLauncher = (Get-Content -Raw -LiteralPath $launcher).Replace(
        'root = ""', ('root = "' + $projectRoot.Replace('"', '""') + '"'))
    Set-Content -LiteralPath $startupEntry -Value $installedLauncher -Encoding Unicode
    Write-Host 'FUNF login startup installed.'
}
if ($Action -eq 'install' -or $Action -eq 'start') {
    Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + $launcher + '"') -WindowStyle Hidden
    Write-Host 'FUNF component launch requested.'
    exit 0
}
foreach ($component in @('control', 'gateway', 'tunnel')) {
    $statePath = Join-Path $runtime "$component.json"
    $running = $false
    if (Test-Path -LiteralPath $statePath) {
        $state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
        $supervisor = Get-Process -Id $state.supervisor_pid -ErrorAction SilentlyContinue
        $running = $supervisor -and $supervisor.StartTime.ToUniversalTime() -eq ([datetime]$state.supervisor_started).ToUniversalTime()
    }
    $configured = switch ($component) {
        'control' { Test-Path -LiteralPath (Join-Path $projectRoot 'private\device\binding.json') }
        'gateway' { Test-Path -LiteralPath (Join-Path $projectRoot 'private\http-mcp\.env') }
        'tunnel' { Test-Path -LiteralPath (Join-Path $projectRoot 'private\tunnel.json') }
    }
    [PSCustomObject]@{ Component = $component; Configured = [bool]$configured; SupervisorRunning = [bool]$running }
}
