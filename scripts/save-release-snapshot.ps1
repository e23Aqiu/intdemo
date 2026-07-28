param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,

    [string]$StageDir = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$distRoot = Join-Path $repoRoot "dist"
if (-not $StageDir) {
    $StageDir = Join-Path $distRoot "installer-stage"
}
$resolvedStage = (Resolve-Path -LiteralPath $StageDir).Path
$snapshotRoot = Join-Path $distRoot "release-snapshots"
$snapshotPath = Join-Path $snapshotRoot "$Version.json"

if (-not (Test-Path -LiteralPath (Join-Path $resolvedStage "_internal"))) {
    throw "The installer stage does not look like a PyInstaller onedir build: $resolvedStage"
}

$files = @(
    Get-ChildItem -LiteralPath $resolvedStage -Recurse -File |
        Sort-Object FullName |
        ForEach-Object {
            $relative = $_.FullName.Substring($resolvedStage.Length).TrimStart("\")
            [ordered]@{
                path = $relative.Replace("\", "/")
                size = $_.Length
                sha256 = (
                    Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256
                ).Hash.ToLowerInvariant()
            }
        }
)

$snapshot = [ordered]@{
    schema_version = 1
    version = $Version
    generated_at = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    files = $files
}
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
New-Item -ItemType Directory -Path $snapshotRoot -Force | Out-Null
[System.IO.File]::WriteAllText(
    $snapshotPath,
    ($snapshot | ConvertTo-Json -Depth 5),
    $utf8WithoutBom
)

$totalBytes = [int64]0
foreach ($entry in $files) {
    $totalBytes += [int64]$entry["size"]
}
Write-Host "Release snapshot created: $snapshotPath"
Write-Host "Files: $($files.Count), bytes: $totalBytes"
