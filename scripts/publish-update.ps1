param(
    [Parameter(Mandatory = $true)]
    [string]$Installer,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,

    [string]$Notes = "Maintenance update.",

    [ValidateSet("test", "stable")]
    [string]$Channel = "test",

    [switch]$Mandatory,

    [string]$RemoteHost = "",

    [ValidatePattern('^/[A-Za-z0-9._/-]+$')]
    [string]$RemotePath = "/opt/intdemo/deploy/updates",

    [string]$IdentityFile = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$sourceInstaller = (Resolve-Path -LiteralPath $Installer).Path
$releaseRoot = Join-Path $repoRoot "dist\update-release"
$filesRoot = Join-Path $releaseRoot "files"
$publishedName = "IntDemoOnline-Setup-$Version.exe"
$publishedInstaller = Join-Path $filesRoot $publishedName
$manifestPath = Join-Path $releaseRoot "$Channel.json"

New-Item -ItemType Directory -Force -Path $filesRoot | Out-Null
Copy-Item -LiteralPath $sourceInstaller -Destination $publishedInstaller -Force
$file = Get-Item -LiteralPath $publishedInstaller
$hash = (Get-FileHash -LiteralPath $publishedInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest = [ordered]@{
    schema_version = 1
    channel = $Channel
    version = $Version
    published_at = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    installer_path = "/updates/files/$publishedName"
    sha256 = $hash
    size = $file.Length
    mandatory = [bool]$Mandatory
    notes = $Notes
}
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText(
    $manifestPath,
    ($manifest | ConvertTo-Json),
    $utf8WithoutBom
)

Write-Host "Update release prepared: $releaseRoot"
Write-Host "Manifest: $manifestPath"
Write-Host "Installer: $publishedInstaller"
Write-Host "SHA-256: $hash"

if ($RemoteHost) {
    if ($RemoteHost -notmatch '^[A-Za-z0-9._@:-]+$') {
        throw "RemoteHost contains unsupported characters"
    }
    $sshArgs = @()
    $scpArgs = @()
    if ($IdentityFile) {
        $resolvedIdentity = (Resolve-Path -LiteralPath $IdentityFile).Path
        $sshArgs += @("-i", $resolvedIdentity)
        $scpArgs += @("-i", $resolvedIdentity)
    }
    $incoming = "$RemotePath/.incoming"
    & ssh @sshArgs $RemoteHost "mkdir -p '$RemotePath/files' '$incoming'"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the remote update directories"
    }
    & scp @scpArgs $publishedInstaller "${RemoteHost}:$incoming/$publishedName"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upload the installer"
    }
    & scp @scpArgs $manifestPath "${RemoteHost}:$incoming/$Channel.json"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upload the manifest"
    }
    & ssh @sshArgs $RemoteHost `
        "mv '$incoming/$publishedName' '$RemotePath/files/$publishedName' && mv '$incoming/$Channel.json' '$RemotePath/$Channel.json'"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not publish the remote update atomically"
    }
    Write-Host "Published to ${RemoteHost}:$RemotePath"
}
