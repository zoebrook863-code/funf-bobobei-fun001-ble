param(
    [ValidateSet('status', 'audit', 'guide', 'plan', 'mcp', 'test', 'bluetooth-check', 'inspect', 'bench-stop', 'bench-pulse', 'bench-disconnect', 'mock', 'mock-status', 'mock-mcp', 'control', 'control-status', 'control-mcp', 'control-check', 'control-check-stop', 'diagnose', 'observe', 'sentinel', 'phase2', 'http-init', 'http-mcp', 'startup-install', 'startup-start', 'startup-status', 'startup-disable', 'setup')]
    [string]$Action = 'status'
)
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Project venv missing. Run python -m venv .venv from this project.'
}
if ($Action -eq 'setup') {
    & $python -B (Join-Path $PSScriptRoot 'src\setup_binding.py')
} elseif ($Action -eq 'test') {
    & $python -m unittest discover -s (Join-Path $PSScriptRoot 'tests') -v
} elseif ($Action -eq 'mock' -or $Action -eq 'mock-status') {
    $mockAction = if ($Action -eq 'mock') { 'serve' } else { 'status' }
    if ($MyInvocation.ExpectingInput) {
        $input | & $python (Join-Path $PSScriptRoot 'src\mock_relay.py') $mockAction
    } else {
        & $python (Join-Path $PSScriptRoot 'src\mock_relay.py') $mockAction
    }
} elseif ($Action -eq 'mock-mcp') {
    if ($MyInvocation.ExpectingInput) {
        $input | & $python (Join-Path $PSScriptRoot 'src\mock_mcp.py')
    } else {
        & $python (Join-Path $PSScriptRoot 'src\mock_mcp.py')
    }
} elseif ($Action -eq 'control') {
    & $python (Join-Path $PSScriptRoot 'src\fun001_service.py') serve
} elseif ($Action -eq 'control-status') {
    & $python (Join-Path $PSScriptRoot 'src\fun001_service.py') status
} elseif ($Action -eq 'control-mcp') {
    if ($MyInvocation.ExpectingInput) {
        $input | & $python (Join-Path $PSScriptRoot 'src\fun001_mcp.py')
    } else {
        & $python (Join-Path $PSScriptRoot 'src\fun001_mcp.py')
    }
} elseif ($Action -eq 'http-init' -or $Action -eq 'http-mcp') {
    $httpAction = if ($Action -eq 'http-init') { 'init' } else { 'serve' }
    & $python (Join-Path $PSScriptRoot 'src\fun001_http.py') $httpAction
} elseif ($Action.StartsWith('startup-')) {
    & (Join-Path $PSScriptRoot 'scripts\fun001-startup.ps1') $Action.Substring(8)
} elseif ($Action -eq 'control-check') {
    & $python (Join-Path $PSScriptRoot 'src\fun001_check.py')
} elseif ($Action -eq 'control-check-stop') {
    & $python (Join-Path $PSScriptRoot 'src\fun001_check.py') --stop
} elseif ($Action -eq 'diagnose') {
    & $python (Join-Path $PSScriptRoot 'src\diagnostics_phase1.py') measure
} elseif ($Action -eq 'observe') {
    & $python (Join-Path $PSScriptRoot 'src\diagnostics_phase1.py') observe --seconds 60
} elseif ($Action -eq 'sentinel') {
    & $python (Join-Path $PSScriptRoot 'src\diagnostics_sentinel.py') sentinel
} elseif ($Action -eq 'phase2') {
    & $python (Join-Path $PSScriptRoot 'src\phase2_bench.py') phase2
} elseif ($Action -eq 'bluetooth-check') {
    & $python (Join-Path $PSScriptRoot 'src\windows_ble.py')
} elseif ($Action -eq 'inspect') {
    & $python (Join-Path $PSScriptRoot 'src\windows_ble.py') inspect
} elseif ($Action -eq 'bench-stop') {
    if ($MyInvocation.ExpectingInput) {
        $input | & $python (Join-Path $PSScriptRoot 'src\bench_stop.py')
    } else {
        & $python (Join-Path $PSScriptRoot 'src\bench_stop.py')
    }
} elseif ($Action -eq 'bench-pulse') {
    if ($MyInvocation.ExpectingInput) {
        $input | & $python (Join-Path $PSScriptRoot 'src\bench_stop.py') pulse
    } else {
        & $python (Join-Path $PSScriptRoot 'src\bench_stop.py') pulse
    }
} elseif ($Action -eq 'bench-disconnect') {
    if ($MyInvocation.ExpectingInput) {
        $input | & $python (Join-Path $PSScriptRoot 'src\bench_stop.py') disconnect
    } else {
        & $python (Join-Path $PSScriptRoot 'src\bench_stop.py') disconnect
    }
} elseif ($MyInvocation.ExpectingInput) {
    $input | & $python (Join-Path $PSScriptRoot 'src\readonly.py') $Action
} else {
    & $python (Join-Path $PSScriptRoot 'src\readonly.py') $Action
}
exit $LASTEXITCODE
