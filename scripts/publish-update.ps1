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

    [string]$DeltaInstaller = "",

    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$DeltaFromVersion = "",

    [switch]$LegacyDeltaPrimary,

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

$sourceCommit = [string](& git -C $repoRoot rev-parse HEAD)
if ($LASTEXITCODE -ne 0 -or -not $sourceCommit.Trim()) {
    throw "Could not resolve the source Git commit"
}
$sourceCommit = $sourceCommit.Trim()
$sourceChanges = @(& git -C $repoRoot status --porcelain=v1)
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect the source Git worktree"
}
$sourceDirty = $sourceChanges.Count -gt 0
if ($RemoteHost -and $sourceDirty) {
    throw "Remote publishing requires a clean Git worktree"
}

New-Item -ItemType Directory -Force -Path $filesRoot | Out-Null
Copy-Item -LiteralPath $sourceInstaller -Destination $publishedInstaller -Force
$file = Get-Item -LiteralPath $publishedInstaller
$hash = (Get-FileHash -LiteralPath $publishedInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest = [ordered]@{
    schema_version = 1
    channel = $Channel
    version = $Version
    published_at = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    source_commit = $sourceCommit
    source_dirty = $sourceDirty
    installer_path = "/updates/files/$publishedName"
    sha256 = $hash
    size = $file.Length
    primary_kind = "full"
    mandatory = [bool]$Mandatory
    notes = $Notes
}
$publishedDelta = $null
$deltaPublishedName = ""
if ($DeltaInstaller -or $DeltaFromVersion) {
    if (-not $DeltaInstaller -or -not $DeltaFromVersion) {
        throw "DeltaInstaller and DeltaFromVersion must be provided together"
    }
    $sourceDelta = (Resolve-Path -LiteralPath $DeltaInstaller).Path
    $deltaPublishedName = "IntDemoOnline-Patch-$DeltaFromVersion-to-$Version.exe"
    $publishedDelta = Join-Path $filesRoot $deltaPublishedName
    Copy-Item -LiteralPath $sourceDelta -Destination $publishedDelta -Force
    $deltaFile = Get-Item -LiteralPath $publishedDelta
    $deltaHash = (
        Get-FileHash -LiteralPath $publishedDelta -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $manifest.full = [ordered]@{
        installer_path = "/updates/files/$publishedName"
        sha256 = $hash
        size = $file.Length
    }
    $manifest.deltas = @(
        [ordered]@{
            from_version = $DeltaFromVersion
            installer_path = "/updates/files/$deltaPublishedName"
            sha256 = $deltaHash
            size = $deltaFile.Length
        }
    )
    if ($LegacyDeltaPrimary) {
        Write-Warning (
            "LegacyDeltaPrimary is deprecated. The canonical manifest now " +
            "keeps the full installer at the top level; the update endpoint " +
            "selects the exact delta from the client version header."
        )
    }
} elseif ($LegacyDeltaPrimary) {
    throw "LegacyDeltaPrimary requires a delta installer"
}
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText(
    $manifestPath,
    ($manifest | ConvertTo-Json -Depth 5),
    $utf8WithoutBom
)

Write-Host "Update release prepared: $releaseRoot"
Write-Host "Manifest: $manifestPath"
Write-Host "Installer: $publishedInstaller"
Write-Host "SHA-256: $hash"
if ($publishedDelta) {
    Write-Host "Delta installer: $publishedDelta"
    Write-Host "Delta SHA-256: $deltaHash"
}

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
    $remoteManifestOutput = @(
        & ssh @sshArgs $RemoteHost `
            "if [ -f '$RemotePath/$Channel.json' ]; then cat '$RemotePath/$Channel.json'; fi"
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect the currently published manifest"
    }
    $remoteManifestText = ($remoteManifestOutput -join "`n").Trim()
    $remoteWasPaused = $false
    if ($remoteManifestText) {
        try {
            $remoteManifest = $remoteManifestText | ConvertFrom-Json
            if ($remoteManifest.paused -eq $true) {
                $remoteVersion = [string]$remoteManifest.paused_version
                $remoteWasPaused = $true
            } else {
                $remoteVersion = [string]$remoteManifest.version
            }
            $parsedRemoteVersion = [version]$remoteVersion
            $parsedTargetVersion = [version]$Version
        }
        catch {
            throw "The currently published manifest has an invalid version"
        }
        if ($parsedRemoteVersion -ge $parsedTargetVersion) {
            throw (
                "Remote channel $Channel already publishes version " +
                "$remoteVersion. Published versions cannot be replaced or downgraded."
            )
        }
        if ($remoteWasPaused) {
            Write-Host (
                "Remote channel $Channel is paused at version $remoteVersion; " +
                "publishing $Version will resume distribution"
            )
        }
    }
    $remoteFullHashOutput = @(
        & ssh @sshArgs $RemoteHost `
            "if [ -f '$RemotePath/files/$publishedName' ]; then sha256sum '$RemotePath/files/$publishedName' | cut -d ' ' -f 1; fi"
    )
    $remoteFullHash = ($remoteFullHashOutput -join "").Trim()
    $fullUploaded = $remoteFullHash -ne $hash
    if ($fullUploaded) {
        & scp @scpArgs $publishedInstaller "${RemoteHost}:$incoming/$publishedName"
        if ($LASTEXITCODE -ne 0) {
            throw "Could not upload the installer"
        }
    } else {
        Write-Host "Remote full installer already matches; upload skipped"
    }
    $deltaUploaded = $false
    if ($publishedDelta) {
        $remoteDeltaHashOutput = @(
            & ssh @sshArgs $RemoteHost `
                "if [ -f '$RemotePath/files/$deltaPublishedName' ]; then sha256sum '$RemotePath/files/$deltaPublishedName' | cut -d ' ' -f 1; fi"
        )
        $remoteDeltaHash = ($remoteDeltaHashOutput -join "").Trim()
        $deltaUploaded = $remoteDeltaHash -ne $deltaHash
        if ($deltaUploaded) {
            & scp @scpArgs $publishedDelta "${RemoteHost}:$incoming/$deltaPublishedName"
            if ($LASTEXITCODE -ne 0) {
                throw "Could not upload the delta installer"
            }
        } else {
            Write-Host "Remote delta installer already matches; upload skipped"
        }
    }
    & scp @scpArgs $manifestPath "${RemoteHost}:$incoming/$Channel.json"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upload the manifest"
    }
    $publishSteps = @()
    if ($fullUploaded) {
        $publishSteps += "mv '$incoming/$publishedName' '$RemotePath/files/$publishedName'"
    }
    if ($publishedDelta -and $deltaUploaded) {
        $publishSteps += "mv '$incoming/$deltaPublishedName' '$RemotePath/files/$deltaPublishedName'"
    }
    $publishSteps += "mv '$incoming/$Channel.json' '$RemotePath/$Channel.json'"
    $publishCommand = $publishSteps -join " && "
    & ssh @sshArgs $RemoteHost $publishCommand
    if ($LASTEXITCODE -ne 0) {
        throw "Could not publish the remote update atomically"
    }
    Write-Host "Published to ${RemoteHost}:$RemotePath"
    if ($remoteWasPaused) {
        Write-Host "Distribution resumed for channel $Channel"
    }
}
