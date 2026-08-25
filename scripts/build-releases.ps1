param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://')]
    [string]$BaseUrl,

    [string]$CaBundle = "",

    [ValidateSet("test", "stable")]
    [string]$Channel = "test",

    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = "1.2.0",
    [ValidatePattern('^$|^\d+\.\d+\.\d+$')]
    [string]$DeltaFromVersion = "",

    [string]$InnoCompiler = ""
)

$ErrorActionPreference = "Stop"

$portableArguments = @{
    BaseUrl = $BaseUrl
    CaBundle = $CaBundle
    Channel = $Channel
    Version = $Version
}
& (Join-Path $PSScriptRoot "build-portable.ps1") @portableArguments
if (-not $?) {
    throw "Portable package build failed"
}

$installerArguments = @{
    BaseUrl = $BaseUrl
    CaBundle = $CaBundle
    Channel = $Channel
    Version = $Version
    DeltaFromVersion = $DeltaFromVersion
}
if ($InnoCompiler) {
    $installerArguments.InnoCompiler = $InnoCompiler
}
& (Join-Path $PSScriptRoot "build-installer.ps1") @installerArguments
if (-not $?) {
    throw "Installer package build failed"
}
