param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("test", "stable")]
    [string]$Channel,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9._@:-]+$')]
    [string]$RemoteHost,

    [ValidatePattern('^/[A-Za-z0-9._/-]+$')]
    [string]$RemotePath = "/opt/intdemo/deploy/updates",

    [string]$IdentityFile = ""
)

$ErrorActionPreference = "Stop"
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

$sshArgs = @()
$scpArgs = @()
if ($IdentityFile) {
    $resolvedIdentity = (Resolve-Path -LiteralPath $IdentityFile).Path
    $sshArgs += @("-i", $resolvedIdentity)
    $scpArgs += @("-i", $resolvedIdentity)
}

$remoteManifestPath = "$RemotePath/$Channel.json"
$hashCommand = (
    "if [ -f '$remoteManifestPath' ]; then " +
    "sha256sum '$remoteManifestPath' | cut -d ' ' -f 1; " +
    "else exit 44; fi"
)
$hashBeforeOutput = @(& ssh @sshArgs $RemoteHost $hashCommand)
if ($LASTEXITCODE -ne 0) {
    throw "No active $Channel update manifest was found on the remote server"
}
$hashBefore = ($hashBeforeOutput -join "").Trim().ToLowerInvariant()
if ($hashBefore -notmatch '^[0-9a-f]{64}$') {
    throw "Could not verify the active remote manifest"
}

$remoteManifestOutput = @(
    & ssh @sshArgs $RemoteHost "cat '$remoteManifestPath'"
)
if ($LASTEXITCODE -ne 0) {
    throw "Could not read the active remote manifest"
}
$remoteManifestText = ($remoteManifestOutput -join "`n").Trim()
try {
    $remoteManifest = $remoteManifestText | ConvertFrom-Json
}
catch {
    throw "The active remote manifest is not valid JSON"
}
if (
    [int]$remoteManifest.schema_version -ne 1 -or
    [string]$remoteManifest.channel -ne $Channel
) {
    throw "The active remote manifest does not match channel $Channel"
}

$hashAfterOutput = @(& ssh @sshArgs $RemoteHost $hashCommand)
if ($LASTEXITCODE -ne 0) {
    throw "Could not recheck the active remote manifest"
}
$hashAfter = ($hashAfterOutput -join "").Trim().ToLowerInvariant()
if ($hashAfter -ne $hashBefore) {
    throw "The active remote manifest changed during inspection; retry the pause"
}

if ($remoteManifest.paused -eq $true) {
    $pausedVersion = [string]$remoteManifest.paused_version
    if ($pausedVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw "The remote pause marker has an invalid paused version"
    }
    Write-Host (
        "Distribution is already paused for channel $Channel " +
        "at version $pausedVersion"
    )
    exit 0
}

$activeVersion = [string]$remoteManifest.version
if ($activeVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "The active remote manifest has an invalid version"
}

$pausedAt = [DateTime]::UtcNow
$timestamp = $pausedAt.ToString("yyyyMMddTHHmmssZ")
$uniqueId = [Guid]::NewGuid().ToString("N")
$archiveRoot = "$RemotePath-paused"
$archiveName = (
    "$Channel-$activeVersion-$timestamp-" +
    $hashBefore.Substring(0, 12) +
    ".json"
)
$remoteArchivePath = "$archiveRoot/$archiveName"
$incomingRoot = "$RemotePath/.incoming"
$incomingName = "$Channel-pause-$timestamp-$uniqueId.json"
$remoteIncomingPath = "$incomingRoot/$incomingName"
$localMarkerPath = Join-Path (
    [System.IO.Path]::GetTempPath()
) "intdemo-$incomingName"
$marker = [ordered]@{
    schema_version = 1
    channel = $Channel
    version = "0.0.0"
    paused = $true
    paused_version = $activeVersion
    paused_at = $pausedAt.ToString("yyyy-MM-ddTHH:mm:ssZ")
}
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)

try {
    [System.IO.File]::WriteAllText(
        $localMarkerPath,
        ($marker | ConvertTo-Json -Depth 3),
        $utf8WithoutBom
    )

    & ssh @sshArgs $RemoteHost "mkdir -p '$incomingRoot' '$archiveRoot'"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the remote pause directories"
    }

    & scp @scpArgs $localMarkerPath "${RemoteHost}:$remoteIncomingPath"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upload the remote pause marker"
    }

    $pauseCommand = (
        "echo '$hashBefore  $remoteManifestPath' | " +
        "sha256sum -c - >/dev/null" +
        " && cp '$remoteManifestPath' '$remoteArchivePath'" +
        " && mv '$remoteIncomingPath' '$remoteManifestPath'"
    )
    & ssh @sshArgs $RemoteHost $pauseCommand
    if ($LASTEXITCODE -ne 0) {
        & ssh @sshArgs $RemoteHost "rm -f '$remoteIncomingPath'" | Out-Null
        throw (
            "Could not pause distribution atomically. The active manifest " +
            "may have changed; inspect the remote channel and retry."
        )
    }
}
finally {
    Remove-Item -LiteralPath $localMarkerPath -Force -ErrorAction SilentlyContinue
}

Write-Host "Paused channel: $Channel"
Write-Host "Paused version: $activeVersion"
Write-Host "Archived manifest: $remoteArchivePath"
Write-Host "Installer files were retained"
