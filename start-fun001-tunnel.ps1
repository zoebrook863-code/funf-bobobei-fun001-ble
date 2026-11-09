param([switch]$Check)
$ErrorActionPreference = 'Stop'
$configPath = Join-Path $PSScriptRoot 'private\tunnel.json'
$config = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
if (@($config.PSObject.Properties.Name).Count -ne 2 -or
    -not ($config.PSObject.Properties.Name -contains 'ssh_target') -or
    -not ($config.PSObject.Properties.Name -contains 'remote_port')) {
    throw 'Expected ssh_target and remote_port in private/tunnel.json.'
}
if ($config.ssh_target -isnot [string] -or
    $config.ssh_target -notmatch '^(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*$') {
    throw 'Use an SSH host alias or user@hostname.'
}
if ((($config.remote_port -isnot [int]) -and ($config.remote_port -isnot [long])) -or
    $config.remote_port -lt 1024 -or $config.remote_port -gt 65535) {
    throw 'remote_port must be an integer from 1024 to 65535.'
}
$forward = "127.0.0.1:$($config.remote_port):127.0.0.1:8769"
if ($Check) {
    @{ target = $config.ssh_target; forward = $forward } | ConvertTo-Json
    exit 0
}
& ssh -NT -o BatchMode=yes -o StrictHostKeyChecking=yes `
    -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=2 `
    -R $forward $config.ssh_target
exit $LASTEXITCODE
