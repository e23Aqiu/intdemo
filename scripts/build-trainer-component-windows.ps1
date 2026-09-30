<#
.SYNOPSIS
Build only the local CAPTCHA trainer component for Windows x64.

.DESCRIPTION
Creates or reuses an isolated Python 3.10 environment, validates the pinned
offline dependencies, runs the source and frozen self-tests, and writes one
.inttrainer file plus its SHA-256 file. This script never builds the client,
installer, portable application, or server.

.PARAMETER Wheelhouse
Directory containing the administrator-approved Windows x64 wheels. Required
the first time the managed environment is created. It can also be supplied by
INTDEMO_TRAINER_WHEELHOUSE.

.PARAMETER PythonPath
Use an already-prepared Python 3.10 x64 environment instead of the managed
environment.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\scripts\build-trainer-component-windows.ps1 `
  -Wheelhouse D:\wheelhouse\windows-x86_64

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\scripts\build-trainer-component-windows.ps1
#>

[CmdletBinding()]
param(
    [string]$Wheelhouse = "",
    [string]$PythonPath = "",
    [string]$EnvironmentPath = "",
    [string]$OutputDirectory = "",
    [string]$BuildRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-AbsolutePath([string]$Path, [string]$BasePath) {
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $BasePath $Path))
}

function Resolve-Executable([string]$Value) {
    if (Test-Path -LiteralPath $Value -PathType Leaf) {
        return (Resolve-Path -LiteralPath $Value).Path
    }
    $command = Get-Command $Value -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "Python executable not found: $Value"
    }
    return $command.Source
}

function Invoke-Checked(
    [string]$Executable,
    [string[]]$ArgumentList,
    [string]$FailureMessage
) {
    & $Executable @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit code $LASTEXITCODE)"
    }
}

function Find-Python310 {
    $launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        $candidateOutput = & $launcher.Source -3.10 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $candidateOutput) {
            $candidate = [string]($candidateOutput | Select-Object -Last 1)
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                return (Resolve-Path -LiteralPath $candidate).Path
            }
        }
    }

    foreach ($name in @("python3.10.exe", "python.exe")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -eq $command) {
            continue
        }
        $version = & $command.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -eq 0 -and ([string]$version).Trim() -eq "3.10") {
            return $command.Source
        }
    }
    throw "Python 3.10 x64 was not found. Install 64-bit Python 3.10 first."
}

if ($env:OS -ne "Windows_NT") {
    throw "This script must run on Windows x64."
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$protocolPath = Join-Path $repoRoot "enhanced_trainer\protocol.py"
$protocolSource = Get-Content -LiteralPath $protocolPath -Raw
if ($protocolSource -notmatch '(?m)^COMPONENT_VERSION\s*=\s*"(?<version>\d+(?:\.\d+){2,3})"\s*$') {
    throw "Cannot read the component version from enhanced_trainer/protocol.py."
}
$componentVersion = $Matches["version"]

if (-not $Wheelhouse -and $env:INTDEMO_TRAINER_WHEELHOUSE) {
    $Wheelhouse = $env:INTDEMO_TRAINER_WHEELHOUSE
}

if (-not $EnvironmentPath) {
    if (-not $env:LOCALAPPDATA) {
        throw "LOCALAPPDATA is unavailable; cannot select the isolated build environment."
    }
    $EnvironmentPath = Join-Path $env:LOCALAPPDATA "IntDemoBuilder\trainer-$componentVersion-py310"
}
else {
    $EnvironmentPath = Get-AbsolutePath $EnvironmentPath $repoRoot
}

$managedPython = Join-Path $EnvironmentPath "Scripts\python.exe"
if ($PythonPath) {
    $buildPython = Resolve-Executable $PythonPath
}
elseif (Test-Path -LiteralPath $managedPython -PathType Leaf) {
    $buildPython = (Resolve-Path -LiteralPath $managedPython).Path
}
else {
    if (Test-Path -LiteralPath $EnvironmentPath) {
        throw "The build environment path exists but is not a valid Python environment: $EnvironmentPath"
    }
    if (-not $Wheelhouse) {
        throw (
            "The first run requires -Wheelhouse with approved Windows x64 wheels. " +
            "Later runs reuse $EnvironmentPath."
        )
    }
    $bootstrapPython = Find-Python310
    New-Item -ItemType Directory -Path (Split-Path $EnvironmentPath -Parent) -Force | Out-Null
    Write-Host "=== Creating the component build environment ==="
    Write-Host $EnvironmentPath
    Invoke-Checked $bootstrapPython @("-m", "venv", $EnvironmentPath) "Failed to create the Python 3.10 environment"
    $buildPython = $managedPython
}

$pythonCheck = @'
import platform
import struct
import sys

if sys.version_info[:2] != (3, 10):
    raise SystemExit(f'Python 3.10 is required; found {platform.python_version()}')
if struct.calcsize('P') != 8 or platform.machine().casefold() not in {'amd64', 'x86_64'}:
    raise SystemExit(f'Windows x64 Python is required; found {platform.machine()}')
print('Python executable:', sys.executable)
print('Python version:', platform.python_version(), platform.machine())
'@
Invoke-Checked $buildPython @("-c", $pythonCheck) "Python 3.10 x64 validation failed"

$requirements = Join-Path $repoRoot "enhanced_trainer\requirements-windows-x86_64.txt"
if ($Wheelhouse) {
    $resolvedWheelhouse = Get-AbsolutePath $Wheelhouse $repoRoot
    if (-not (Test-Path -LiteralPath $resolvedWheelhouse -PathType Container)) {
        throw "The offline wheelhouse does not exist: $resolvedWheelhouse"
    }
    Write-Host "=== Installing pinned component dependencies from the offline wheelhouse ==="
    Write-Host $resolvedWheelhouse
    Invoke-Checked $buildPython @(
        "-m", "pip", "install",
        "--no-index",
        "--find-links", $resolvedWheelhouse,
        "--requirement", $requirements
    ) "Failed to install component build dependencies"
}

$dependencyCheck = @'
from importlib.metadata import PackageNotFoundError, version

expected = {
    'torch': '2.7.0+cpu',
    'onnx': '1.18.0',
    'onnxruntime': '1.19.2',
    'ddddocr': '1.5.6',
    'numpy': '2.0.2',
    'Pillow': '11.3.0',
    'cryptography': '45.0.7',
    'pyinstaller': '6.16.0',
}
problems = []
for name, wanted in expected.items():
    try:
        installed = version(name)
    except PackageNotFoundError:
        problems.append(f'{name} is not installed')
    else:
        if installed != wanted:
            problems.append(f'{name}={installed}; expected {wanted}')
if problems:
    raise SystemExit('; '.join(problems))

from enhanced_trainer.package_tool import _require_build_dependencies

_require_build_dependencies()
print('Component dependency validation passed')
'@

Push-Location $repoRoot
try {
    & $buildPython -c $dependencyCheck
    if ($LASTEXITCODE -ne 0) {
        throw (
            "Component dependencies are missing or have incorrect versions. " +
            "Use -Wheelhouse with the approved offline wheels on the first build."
        )
    }
    Invoke-Checked $buildPython @("-m", "pip", "check") "Python dependency consistency check failed"

    Write-Host "=== Running the source component self-test ==="
    Invoke-Checked $buildPython @(
        "-m", "enhanced_trainer", "self-test", "--protocol-version", "2"
    ) "The source component self-test failed"

    if (-not $OutputDirectory) {
        $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $OutputDirectory = Join-Path $repoRoot "output\components\$timestamp"
    }
    else {
        $OutputDirectory = Get-AbsolutePath $OutputDirectory $repoRoot
    }
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

    if (-not $BuildRoot) {
        $BuildRoot = Join-Path $env:LOCALAPPDATA "IntDemoBuilder\trainer-component-build"
    }
    else {
        $BuildRoot = Get-AbsolutePath $BuildRoot $repoRoot
    }

    $packageName = "IntDemo-Trainer-$componentVersion-windows-x86_64.inttrainer"
    $packagePath = Join-Path $OutputDirectory $packageName
    if (Test-Path -LiteralPath $packagePath) {
        throw "The output component already exists; refusing to overwrite it: $packagePath"
    }

    Write-Host "=== Building the Windows x64 CAPTCHA component only ==="
    Invoke-Checked $buildPython @(
        (Join-Path $repoRoot "scripts\package-trainer.py"),
        "--platform", "windows-x86_64",
        "all",
        "--build-root", $BuildRoot,
        "--output", $packagePath
    ) "Windows x64 component packaging failed"

    Invoke-Checked $buildPython @("-m", "zipfile", "-t", $packagePath) "Component ZIP integrity check failed"
    $hash = (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $shaPath = "$packagePath.sha256"
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        $shaPath,
        "$hash  $packageName`n",
        $utf8WithoutBom
    )

    Write-Host "=== Component package completed ==="
    Write-Host "Component: $packagePath"
    Write-Host "Checksum: $shaPath"
    Write-Host "SHA-256: $hash"
    Write-Host "Only the .inttrainer component was built; no client, installer, or server was packaged."
}
finally {
    Pop-Location
}
