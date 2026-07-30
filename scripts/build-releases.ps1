param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://')]
    [string]$BaseUrl,

    [string]$CaBundle = "",

    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = "0.2.6",

    [ValidatePattern('^$|^\d+\.\d+\.\d+$')]
    [string]$DeltaFromVersion = "",

    [string]$InnoCompiler = ""
)

$ErrorActionPreference = "Stop"

$portableArguments = @{
    BaseUrl = $BaseUrl
    CaBundle = $CaBundle
    Version = $Version
}
& (Join-Path $PSScriptRoot "build-portable.ps1") @portableArguments
if (-not $?) {
    throw "Portable package build failed"
}

$installerArguments = @{
    BaseUrl = $BaseUrl
    CaBundle = $CaBundle
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
