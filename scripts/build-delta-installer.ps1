param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$FromVersion,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$ToVersion,

    [string]$BaselineManifest = "",

    [string]$StageDir = "",

    [ValidatePattern('^$|^https://')]
    [string]$FullInstallerUrl = "",

    [string]$InnoCompiler = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$distRoot = Join-Path $repoRoot "dist"
if (-not $StageDir) {
    $StageDir = Join-Path $distRoot "installer-stage"
}
if (-not $BaselineManifest) {
    $BaselineManifest = Join-Path $distRoot "release-snapshots\$FromVersion.json"
}
$resolvedStage = (Resolve-Path -LiteralPath $StageDir).Path
$resolvedBaseline = (Resolve-Path -LiteralPath $BaselineManifest).Path
$patchName = "$FromVersion-to-$ToVersion"
$patchStage = Join-Path $distRoot "delta-stage-$patchName"
$installerOutput = Join-Path $distRoot "installer"
$deleteInclude = Join-Path $distRoot "delta-delete-$patchName.iss"
$languageFile = Join-Path $distRoot "installer-language\ChineseSimplified.isl"

$resolvedDist = [System.IO.Path]::GetFullPath($distRoot)
$resolvedPatchStage = [System.IO.Path]::GetFullPath($patchStage)
if (-not $resolvedPatchStage.StartsWith(
    $resolvedDist + [System.IO.Path]::DirectorySeparatorChar,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Refusing to clear a path outside dist: $resolvedPatchStage"
}
if (Test-Path -LiteralPath $resolvedPatchStage) {
    Remove-Item -LiteralPath $resolvedPatchStage -Recurse -Force
}
New-Item -ItemType Directory -Path $resolvedPatchStage -Force | Out-Null

$baseline = Get-Content -Raw -Encoding UTF8 $resolvedBaseline | ConvertFrom-Json
if ([string]$baseline.version -ne $FromVersion) {
    throw "Baseline version $($baseline.version) does not match $FromVersion"
}
$baselineFiles = @{}
foreach ($file in $baseline.files) {
    $baselineFiles[[string]$file.path] = $file
}

$currentPaths = New-Object System.Collections.Generic.HashSet[string]
$changedCount = 0
$changedBytes = [int64]0
Get-ChildItem -LiteralPath $resolvedStage -Recurse -File | ForEach-Object {
    $relativeWindows = $_.FullName.Substring($resolvedStage.Length).TrimStart("\")
    $relative = $relativeWindows.Replace("\", "/")
    [void]$currentPaths.Add($relative)
    $hash = (
        Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $old = $baselineFiles[$relative]
    if ($null -eq $old -or [string]$old.sha256 -ne $hash) {
        $destination = Join-Path $resolvedPatchStage $relativeWindows
        New-Item -ItemType Directory -Path (Split-Path $destination) -Force |
            Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
        $changedCount += 1
        $changedBytes += $_.Length
    }
}

$deleteLines = @()
foreach ($relative in $baselineFiles.Keys) {
    if (-not $currentPaths.Contains($relative)) {
        $escaped = $relative.Replace("/", "\").Replace('"', '""')
        $deleteLines += 'Type: files; Name: "{app}\' + $escaped + '"'
    }
}
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($deleteInclude, $deleteLines, $utf8WithoutBom)

if ($changedCount -eq 0 -and $deleteLines.Count -eq 0) {
    throw "No changed or removed files were found for the delta package"
}
if (-not (Test-Path -LiteralPath $languageFile)) {
    throw "Inno Setup language file is missing. Build the full installer first."
}
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
    throw "Inno Setup 6 compiler was not found"
}

New-Item -ItemType Directory -Path $installerOutput -Force | Out-Null
$compilerArguments = @(
    "/DMyAppVersion=$ToVersion"
    "/DStageDir=$resolvedPatchStage"
    "/DOutputDir=$installerOutput"
    "/DLanguageFile=$languageFile"
    "/DPatchMode=1"
    "/DPatchFromVersion=$FromVersion"
    "/DPatchDeleteFile=$deleteInclude"
)
if ($FullInstallerUrl) {
    $compilerArguments += "/DFullInstallerUrl=$FullInstallerUrl"
}
$compilerArguments += (Join-Path $repoRoot "installer\intdemo.iss")
& $InnoCompiler @compilerArguments
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup delta build failed"
}

$installer = Join-Path $installerOutput "IntDemoOnline-Patch-$patchName.exe"
if (-not (Test-Path -LiteralPath $installer)) {
    throw "Delta installer output was not found: $installer"
}
$installerHash = (
    Get-FileHash -LiteralPath $installer -Algorithm SHA256
).Hash.ToLowerInvariant()
Write-Host "Delta installer created: $installer"
Write-Host "Changed files: $changedCount, removed files: $($deleteLines.Count)"
Write-Host "Uncompressed changed bytes: $changedBytes"
Write-Host "SHA-256: $installerHash"
