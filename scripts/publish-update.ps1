param(
    [string]$Installer = "",

    [string]$WindowsInstaller = "",

    [string]$UosInstaller = "",

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

    [string]$UosDeltaInstaller = "",

    [string]$UosDeltaReport = "",

    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$UosDeltaFromVersion = "",

    [switch]$LegacyDeltaPrimary,

    [string]$RemoteHost = "",

    [ValidatePattern('^/[A-Za-z0-9._/-]+$')]
    [string]$RemotePath = "/opt/intdemo/deploy/updates",

    [string]$IdentityFile = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$normalizedRemotePath = $RemotePath.TrimEnd("/")
$remotePathParts = @($normalizedRemotePath.Split("/") | Select-Object -Skip 1)
if (
    -not $normalizedRemotePath -or
    $normalizedRemotePath -eq "/" -or
    $remotePathParts.Count -eq 0 -or
    @($remotePathParts | Where-Object { $_ -in @("", ".", "..") }).Count -gt 0
) {
    throw "RemotePath must be a safe, non-root absolute Linux path"
}
$RemotePath = $normalizedRemotePath
if (-not $WindowsInstaller) {
    $WindowsInstaller = $Installer
}
if (-not $WindowsInstaller) {
    throw "WindowsInstaller is required for legacy Windows update compatibility"
}
$sourceInstaller = (Resolve-Path -LiteralPath $WindowsInstaller).Path
if ([System.IO.Path]::GetExtension($sourceInstaller) -ne ".exe") {
    throw "WindowsInstaller must be an .exe package"
}
$sourceUosInstaller = $null
if ($UosInstaller) {
    $sourceUosInstaller = (Resolve-Path -LiteralPath $UosInstaller).Path
    if ([System.IO.Path]::GetExtension($sourceUosInstaller) -ne ".deb") {
        throw "UosInstaller must be a .deb package"
    }
}
$releaseRoot = Join-Path $repoRoot "dist\update-release"
$filesRoot = Join-Path $releaseRoot "files"
$publishedName = "IntDemoOnline-Setup-$Version.exe"
$publishedInstaller = Join-Path $filesRoot $publishedName
$uosPublishedName = "IntDemo-UOS-arm64-$Version.deb"
$publishedUosInstaller = Join-Path $filesRoot $uosPublishedName
$uosDeltaPublishedName = (
    "IntDemo-UOS-arm64-Patch-$UosDeltaFromVersion-to-$Version.intdelta"
)
$publishedUosDelta = Join-Path $filesRoot $uosDeltaPublishedName
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
$uosFile = $null
$uosHash = ""
if ($sourceUosInstaller) {
    Copy-Item -LiteralPath $sourceUosInstaller -Destination $publishedUosInstaller -Force
    $uosFile = Get-Item -LiteralPath $publishedUosInstaller
    $uosHash = (
        Get-FileHash -LiteralPath $publishedUosInstaller -Algorithm SHA256
    ).Hash.ToLowerInvariant()
}
$uosDeltaFile = $null
$uosDeltaHash = ""
$uosDeltaPayload = $null
if ($UosDeltaInstaller -or $UosDeltaReport -or $UosDeltaFromVersion) {
    if (-not $UosDeltaInstaller -or -not $UosDeltaReport -or
        -not $UosDeltaFromVersion -or -not $sourceUosInstaller) {
        throw (
            "UosDeltaInstaller, UosDeltaReport, UosDeltaFromVersion and " +
            "UosInstaller must be provided together"
        )
    }
    if ([version]$UosDeltaFromVersion -ge [version]$Version) {
        throw "UosDeltaFromVersion must be lower than Version"
    }
    $sourceUosDelta = (Resolve-Path -LiteralPath $UosDeltaInstaller).Path
    $sourceUosDeltaReport = (Resolve-Path -LiteralPath $UosDeltaReport).Path
    if ([System.IO.Path]::GetExtension($sourceUosDelta) -ne ".intdelta") {
        throw "UosDeltaInstaller must be an .intdelta package"
    }
    $uosDeltaPayload = Get-Content -LiteralPath $sourceUosDeltaReport -Raw |
        ConvertFrom-Json
    $sourceUosDeltaFile = Get-Item -LiteralPath $sourceUosDelta
    $sourceUosDeltaHash = (
        Get-FileHash -LiteralPath $sourceUosDelta -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($uosDeltaPayload.format -ne "uos-deb-xdelta-v1" -or
        $uosDeltaPayload.algorithm -ne "xdelta3" -or
        $uosDeltaPayload.from_version -ne $UosDeltaFromVersion -or
        $uosDeltaPayload.target_version -ne $Version -or
        $uosDeltaPayload.eligible -ne $true -or
        $uosDeltaPayload.byte_identical -ne $true -or
        [long]$uosDeltaPayload.patch_size -ne $sourceUosDeltaFile.Length -or
        ([string]$uosDeltaPayload.patch_sha256).ToLowerInvariant() -ne
            $sourceUosDeltaHash -or
        [long]$uosDeltaPayload.target_size -ne $uosFile.Length -or
        ([string]$uosDeltaPayload.target_sha256).ToLowerInvariant() -ne $uosHash -or
        $sourceUosDeltaFile.Length * 2 -ge $uosFile.Length) {
        throw "UOS delta report, target DEB or 50% threshold verification failed"
    }
    $releasedSource = Join-Path (
        Join-Path $releaseRoot "files"
    ) "IntDemo-UOS-arm64-$UosDeltaFromVersion.deb"
    $sourceReceipt = Join-Path (
        Join-Path $repoRoot "dist\release-results\$UosDeltaFromVersion"
    ) "publish-receipt.json"
    if (-not (Test-Path -LiteralPath $releasedSource -PathType Leaf) -or
        -not (Test-Path -LiteralPath $sourceReceipt -PathType Leaf)) {
        throw "The real released UOS base DEB or publish receipt is missing"
    }
    $sourceReceiptPayload = Get-Content -LiteralPath $sourceReceipt -Raw |
        ConvertFrom-Json
    $releasedSourceFile = Get-Item -LiteralPath $releasedSource
    $releasedSourceHash = (
        Get-FileHash -LiteralPath $releasedSource -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ([string]$sourceReceiptPayload.version -ne $UosDeltaFromVersion -or
        $sourceReceiptPayload.artifacts.uos_installer.name -ne
            $releasedSourceFile.Name -or
        [long]$sourceReceiptPayload.artifacts.uos_installer.size -ne
            $releasedSourceFile.Length -or
        ([string]$sourceReceiptPayload.artifacts.uos_installer.sha256).ToLowerInvariant() -ne
            $releasedSourceHash -or
        [long]$uosDeltaPayload.base_size -ne $releasedSourceFile.Length -or
        ([string]$uosDeltaPayload.base_sha256).ToLowerInvariant() -ne
            $releasedSourceHash) {
        throw "UOS delta base is not the receipt-verified released DEB"
    }
    Copy-Item -LiteralPath $sourceUosDelta -Destination $publishedUosDelta -Force
    $uosDeltaFile = Get-Item -LiteralPath $publishedUosDelta
    $uosDeltaHash = (
        Get-FileHash -LiteralPath $publishedUosDelta -Algorithm SHA256
    ).Hash.ToLowerInvariant()
}
$windowsFull = [ordered]@{
    installer_path = "/updates/files/$publishedName"
    sha256 = $hash
    size = $file.Length
}
$windowsPlatform = [ordered]@{
    full = $windowsFull
}
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
    platforms = [ordered]@{
        "windows-x86_64" = $windowsPlatform
    }
}
if ($uosFile) {
    $uosPlatform = [ordered]@{
        full = [ordered]@{
            installer_path = "/updates/files/$uosPublishedName"
            sha256 = $uosHash
            size = $uosFile.Length
        }
    }
    if ($uosDeltaFile) {
        $uosPlatform.deltas = @(
            [ordered]@{
                format = "uos-deb-xdelta-v1"
                algorithm = "xdelta3"
                from_version = $UosDeltaFromVersion
                base_sha256 = ([string]$uosDeltaPayload.base_sha256).ToLowerInvariant()
                base_size = [long]$uosDeltaPayload.base_size
                installer_path = "/updates/files/$uosDeltaPublishedName"
                sha256 = $uosDeltaHash
                size = $uosDeltaFile.Length
                target_sha256 = $uosHash
                target_size = $uosFile.Length
            }
        )
    }
    $manifest.platforms["linux-aarch64"] = $uosPlatform
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
    $windowsPlatform.deltas = @(
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
if ($uosFile) {
    Write-Host "UOS ARM64 installer: $publishedUosInstaller"
    Write-Host "UOS ARM64 SHA-256: $uosHash"
}
if ($publishedDelta) {
    Write-Host "Delta installer: $publishedDelta"
    Write-Host "Delta SHA-256: $deltaHash"
}
if ($uosDeltaFile) {
    Write-Host "UOS ARM64 delta: $publishedUosDelta"
    Write-Host "UOS ARM64 delta SHA-256: $uosDeltaHash"
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
    $uosUploaded = $false
    if ($uosFile) {
        $remoteUosHashOutput = @(
            & ssh @sshArgs $RemoteHost `
                "if [ -f '$RemotePath/files/$uosPublishedName' ]; then sha256sum '$RemotePath/files/$uosPublishedName' | cut -d ' ' -f 1; fi"
        )
        $remoteUosHash = ($remoteUosHashOutput -join "").Trim()
        $uosUploaded = $remoteUosHash -ne $uosHash
        if ($uosUploaded) {
            & scp @scpArgs $publishedUosInstaller "${RemoteHost}:$incoming/$uosPublishedName"
            if ($LASTEXITCODE -ne 0) {
                throw "Could not upload the UOS ARM64 installer"
            }
        } else {
            Write-Host "Remote UOS ARM64 installer already matches; upload skipped"
        }
    }
    $uosDeltaUploaded = $false
    if ($uosDeltaFile) {
        $remoteUosDeltaHashOutput = @(
            & ssh @sshArgs $RemoteHost `
                "if [ -f '$RemotePath/files/$uosDeltaPublishedName' ]; then sha256sum '$RemotePath/files/$uosDeltaPublishedName' | cut -d ' ' -f 1; fi"
        )
        $remoteUosDeltaHash = ($remoteUosDeltaHashOutput -join "").Trim()
        $uosDeltaUploaded = $remoteUosDeltaHash -ne $uosDeltaHash
        if ($uosDeltaUploaded) {
            & scp @scpArgs $publishedUosDelta (
                "${RemoteHost}:$incoming/$uosDeltaPublishedName"
            )
            if ($LASTEXITCODE -ne 0) {
                throw "Could not upload the UOS ARM64 delta"
            }
        } else {
            Write-Host "Remote UOS ARM64 delta already matches; upload skipped"
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
    if ($uosFile -and $uosUploaded) {
        $publishSteps += "mv '$incoming/$uosPublishedName' '$RemotePath/files/$uosPublishedName'"
    }
    if ($uosDeltaFile -and $uosDeltaUploaded) {
        $publishSteps += (
            "mv '$incoming/$uosDeltaPublishedName' " +
            "'$RemotePath/files/$uosDeltaPublishedName'"
        )
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
