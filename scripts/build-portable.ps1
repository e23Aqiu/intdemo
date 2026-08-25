param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://')]
    [string]$BaseUrl,

    [string]$CaBundle = "",

    [ValidateSet("test", "stable")]
    [string]$Channel = "test",

    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = "1.2.0",
    [switch]$SkipPyInstaller
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$distRoot = Join-Path $repoRoot "dist"
$buildOutput = Join-Path $distRoot "portable-build"
$stage = Join-Path $distRoot "portable-stage"
$portableOutput = Join-Path $distRoot "portable"
$archive = Join-Path $portableOutput "IntDemoOnline-Portable-$Version.zip"

function Reset-DirectoryWithinDist([string]$Path) {
    $resolvedDist = [System.IO.Path]::GetFullPath($distRoot)
    $resolvedTarget = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolvedTarget.StartsWith(
        $resolvedDist + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to clear a path outside dist: $resolvedTarget"
    }
    if (Test-Path -LiteralPath $resolvedTarget) {
        Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
    }
    New-Item -ItemType Directory -Path $resolvedTarget -Force | Out-Null
}

Push-Location $repoRoot
try {
    $codeVersion = (
        & .\.venv\Scripts\python.exe -c "from integrated_client.config import APP_VERSION; print(APP_VERSION)"
    ).Trim()
    if ($codeVersion -ne $Version) {
        throw "Requested version $Version does not match APP_VERSION $codeVersion"
    }

    if (-not $SkipPyInstaller) {
        Reset-DirectoryWithinDist $buildOutput
        & .\.venv\Scripts\python.exe -m PyInstaller `
            --clean `
            --noconfirm `
            --distpath $buildOutput `
            --workpath (Join-Path $repoRoot "build\portable") `
            integrated_client_onefile.spec
        if ($LASTEXITCODE -ne 0) {
            throw "Portable PyInstaller build failed"
        }
    }

    $executables = @(
        Get-ChildItem -LiteralPath $buildOutput -Filter "*.exe" -File
    )
    if ($executables.Count -ne 1) {
        throw "Expected exactly one portable executable in: $buildOutput"
    }
    $executable = $executables[0].FullName

    Reset-DirectoryWithinDist $stage
    Copy-Item -LiteralPath $executable -Destination $stage -Force

    $relativeCa = $null
    if ($CaBundle) {
        $resolvedCa = (Resolve-Path -LiteralPath $CaBundle).Path
        $certDirectory = Join-Path $stage "certs"
        New-Item -ItemType Directory -Force -Path $certDirectory | Out-Null
        Copy-Item -LiteralPath $resolvedCa `
            -Destination (Join-Path $certDirectory "intdemo-caddy-root.crt") `
            -Force
        $relativeCa = "certs/intdemo-caddy-root.crt"
    }

    $parsedIp = $null
    $hostName = ([Uri]$BaseUrl).Host
    if (
        [System.Net.IPAddress]::TryParse($hostName, [ref]$parsedIp) -and
        -not $relativeCa
    ) {
        throw "An IP-based portable package requires -CaBundle"
    }

    $config = [ordered]@{
        base_url = $BaseUrl.TrimEnd("/")
        ca_bundle = $relativeCa
        channel = $Channel
        connect_timeout = 5
        read_timeout = 20
    }
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        (Join-Path $stage "client-online.json"),
        ($config | ConvertTo-Json),
        $utf8WithoutBom
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $stage "README.txt"),
        "IntDemo Online v$Version`r`n`r`n" +
        "This is the portable package. Extract the complete ZIP before running the EXE.`r`n" +
        "Keep client-online.json and the certs directory beside the executable.`r`n" +
        "Local data is stored under %LOCALAPPDATA%\IntDemoClientOnlineTest.`r`n",
        $utf8WithoutBom
    )

    New-Item -ItemType Directory -Force -Path $portableOutput | Out-Null
    if (Test-Path -LiteralPath $archive) {
        Remove-Item -LiteralPath $archive -Force
    }
    Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $archive
    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host "Portable package created: $archive"
    Write-Host "SHA-256: $hash"
}
finally {
    Pop-Location
}
