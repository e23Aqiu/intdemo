param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://')]
    [string]$BaseUrl,

    [string]$CaBundle = "",

    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = "1.1.0",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
Write-Warning "This compatibility wrapper now builds the standard installer."
$arguments = @{
    BaseUrl = $BaseUrl
    CaBundle = $CaBundle
    Version = $Version
}
if ($SkipBuild) {
    $arguments.SkipPyInstaller = $true
}
& (Join-Path $PSScriptRoot "build-installer.ps1") @arguments
exit $LASTEXITCODE
