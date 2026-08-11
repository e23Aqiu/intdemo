param(
    [Parameter(Mandatory = $true)]
    [string]$RequestArchive,

    [string]$Output = "",

    [string]$InnoCompiler = "",

    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$request = (Resolve-Path -LiteralPath $RequestArchive).Path

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "The Windows build computer must run 64-bit Windows"
}

$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    $python = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($python) {
        & $python.Source -3.9-64 -m venv (Join-Path $repoRoot ".venv")
    } else {
        $python = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $python) {
            throw "Python 3.9 x64 was not found"
        }
        & $python.Source -m venv (Join-Path $repoRoot ".venv")
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the Windows build environment"
    }
}

if (-not $SkipDependencyInstall) {
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Could not update pip"
    }
    & $venvPython -m pip install -r (Join-Path $repoRoot "requirements-dev.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install Windows build dependencies"
    }
}

if (-not $Output) {
    $outputRoot = Join-Path $repoRoot "dist\windows-manual-results"
    New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
    $timestamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $Output = Join-Path $outputRoot "windows-build-result-$timestamp.zip"
}

$arguments = @(
    (Join-Path $repoRoot "release_publisher\release_tasks.py")
    "windows-build"
    "--request-archive"
    $request
    "--output"
    $Output
    "--run-tests"
)
if ($InnoCompiler) {
    $arguments += @("--inno-compiler", $InnoCompiler)
}

Push-Location $repoRoot
try {
    & $venvPython @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Windows build request failed"
    }
}
finally {
    Pop-Location
}

Write-Host "Windows build result is ready: $Output"
Write-Host "Return this ZIP to the UOS release publisher."
