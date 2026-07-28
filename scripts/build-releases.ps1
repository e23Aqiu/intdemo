param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://')]
    [string]$BaseUrl,

    [string]$CaBundle = "",

    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = "0.2.5",

    [ValidatePattern('^$|^\d+\.\d+\.\d+$')]
    [string]$DeltaFromVersion = "",

    [string]$InnoCompiler = ""
)

$ErrorActionPreference = "Stop"

$portableArguments = @{
    BaseUrl = $BaseUrl
    CaBundle = $CaBundle
    Version = $Version
    DeltaFromVersion = $DeltaFromVersion
}
& (Join-Path $PSScriptRoot "build-portable.ps1") @portableArguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$installerArguments = @{
    BaseUrl = $BaseUrl
    CaBundle = $CaBundle
    Version = $Version
}
if ($InnoCompiler) {
    $installerArguments.InnoCompiler = $InnoCompiler
}
& (Join-Path $PSScriptRoot "build-installer.ps1") @installerArguments
exit $LASTEXITCODE
