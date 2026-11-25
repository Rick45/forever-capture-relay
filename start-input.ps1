param(
    [Parameter(Mandatory = $true)][string]$BindAddress,
    [Parameter(Mandatory = $true)][string]$VmAddress,
    [Parameter(Mandatory = $true)][int]$TargetPid,
    [string]$Cast = '1',
    [string]$Reel = 'F'
)

$ErrorActionPreference = 'Stop'
$secretPath = Join-Path $PSScriptRoot '.relay-secret'
if (-not (Test-Path -LiteralPath $secretPath -PathType Leaf)) {
    $installedLauncher = Join-Path $env:LOCALAPPDATA 'CaptureRelay\start-input.ps1'
    if ($PSCommandPath -ne $installedLauncher -and (Test-Path -LiteralPath $installedLauncher -PathType Leaf)) {
        & $installedLauncher @PSBoundParameters
        exit $LASTEXITCODE
    }
    throw 'Capture Relay could not find its encrypted credential. Check the installation under your local app data.'
}

$secret = Get-Content -LiteralPath $secretPath -Raw | ConvertTo-SecureString
try {
    $env:CAPTURE_RELAY_SECRET = [System.Net.NetworkCredential]::new('', $secret).Password
    & node (Join-Path $PSScriptRoot 'host.mjs') --bind $BindAddress --vm $VmAddress --target-pid $TargetPid --cast $Cast --reel $Reel
    exit $LASTEXITCODE
} finally {
    Remove-Item Env:CAPTURE_RELAY_SECRET -ErrorAction SilentlyContinue
    $secret.Dispose()
}
