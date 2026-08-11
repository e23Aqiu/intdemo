param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://')]
    [string]$BaseUrl,

    [string]$CaBundle = "",

    [ValidateSet("test", "stable")]
    [string]$Channel = "test",

    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = "1.0.5",
    [string]$InnoCompiler = "",

    [ValidatePattern('^$|^\d+\.\d+\.\d+$')]
    [string]$DeltaFromVersion = "",

    [switch]$SkipPyInstaller
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$distRoot = Join-Path $repoRoot "dist"
$stage = Join-Path $distRoot "installer-stage"
$installerOutput = Join-Path $distRoot "installer"
$languageDirectory = Join-Path $distRoot "installer-language"
$languageFile = Join-Path $languageDirectory "ChineseSimplified.isl"
$languageUrl = "https://raw.githubusercontent.com/jrsoftware/issrc/refs/heads/main/Files/Languages/ChineseSimplified.isl"
$languageSha256 = "6753be2c5e2740d859900fd902824db2ec568da5c5b52486524c9762d778b0b0"

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
    $versionInfo = Get-Content -Raw -Encoding UTF8 `
        (Join-Path $repoRoot "installer\version_info.txt")
    if ($versionInfo -notmatch "StringStruct\(u'FileVersion', u'$([regex]::Escape($Version))'\)") {
        throw "installer/version_info.txt does not match version $Version"
    }

    if (-not $SkipPyInstaller) {
        & .\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm integrated_client.spec
        if ($LASTEXITCODE -ne 0) {
            throw "PyInstaller build failed"
        }
    }
    $buildOutput = Get-ChildItem -LiteralPath $distRoot -Directory |
        Where-Object {
            $_.FullName -ne [System.IO.Path]::GetFullPath($stage) -and
            (Test-Path -LiteralPath (Join-Path $_.FullName "_internal")) -and
            (Get-ChildItem -LiteralPath $_.FullName -Filter "*.exe" -File)
        } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $buildOutput) {
        throw "PyInstaller onedir output was not found in: $distRoot"
    }

    Reset-DirectoryWithinDist $stage
    Copy-Item -Path (Join-Path $buildOutput.FullName "*") -Destination $stage -Recurse -Force

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
        throw "An IP-based installer requires -CaBundle"
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

    if (-not $InnoCompiler) {
        $candidates = @(
            (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
            (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
        )
        $InnoCompiler = $candidates |
            Where-Object { $_ -and (Test-Path -LiteralPath $_) } |
            Select-Object -First 1
    }
    if (-not $InnoCompiler -or -not (Test-Path -LiteralPath $InnoCompiler)) {
        throw "Inno Setup 6 compiler was not found. Install JRSoftware.InnoSetup or pass -InnoCompiler."
    }

    New-Item -ItemType Directory -Force -Path $languageDirectory | Out-Null
    $downloadLanguage = -not (Test-Path -LiteralPath $languageFile)
    if (-not $downloadLanguage) {
        $currentLanguageHash = (
            Get-FileHash -LiteralPath $languageFile -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        $downloadLanguage = $currentLanguageHash -ne $languageSha256
    }
    if ($downloadLanguage) {
        Invoke-WebRequest -UseBasicParsing -Uri $languageUrl -OutFile $languageFile
    }
    $currentLanguageHash = (
        Get-FileHash -LiteralPath $languageFile -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($currentLanguageHash -ne $languageSha256) {
        throw "The Simplified Chinese language file failed SHA-256 verification"
    }

    New-Item -ItemType Directory -Force -Path $installerOutput | Out-Null
    & $InnoCompiler `
        "/DMyAppVersion=$Version" `
        "/DStageDir=$stage" `
        "/DOutputDir=$installerOutput" `
        "/DLanguageFile=$languageFile" `
        (Join-Path $repoRoot "installer\intdemo.iss")
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup build failed"
    }

    $installer = Join-Path $installerOutput "IntDemoOnline-Setup-$Version.exe"
    if (-not (Test-Path -LiteralPath $installer)) {
        throw "Installer output was not found: $installer"
    }
    $hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host "Installer created: $installer"
    Write-Host "SHA-256: $hash"
    if ($DeltaFromVersion) {
        $deltaArguments = @{
            FromVersion = $DeltaFromVersion
            ToVersion = $Version
            StageDir = $stage
            InnoCompiler = $InnoCompiler
            FullInstallerUrl = (
                "$($BaseUrl.TrimEnd('/'))/updates/files/" +
                "IntDemoOnline-Setup-$Version.exe"
            )
        }
        & (Join-Path $PSScriptRoot "build-delta-installer.ps1") @deltaArguments
        if ($LASTEXITCODE -ne 0) {
            throw "Delta installer build failed"
        }
    }
}
finally {
    Pop-Location
}
