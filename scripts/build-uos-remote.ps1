param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9._@:-]+$')]
    [string]$BuilderHost,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^/[A-Za-z0-9._/-]+$')]
    [string]$BuilderRepoPath,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://')]
    [string]$BaseUrl,

    [ValidateSet("test", "stable")]
    [string]$Channel = "test",

    [string]$CaBundle = "",

    [string]$IdentityFile = "",

    [switch]$ExportResult
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$normalizedBuilderRepoPath = $BuilderRepoPath.TrimEnd("/")
$builderPathParts = @(
    $normalizedBuilderRepoPath.Split("/") | Select-Object -Skip 1
)
if (
    -not $normalizedBuilderRepoPath -or
    $normalizedBuilderRepoPath -eq "/" -or
    $builderPathParts.Count -eq 0 -or
    @($builderPathParts | Where-Object { $_ -in @("", ".", "..") }).Count -gt 0
) {
    throw "BuilderRepoPath must be a specific safe absolute path"
}
$BuilderRepoPath = $normalizedBuilderRepoPath
if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "ssh is required for the UOS ARM64 remote build"
}
if (-not (Get-Command scp -ErrorAction SilentlyContinue)) {
    throw "scp is required for the UOS ARM64 remote build"
}

$sourceCommit = [string](& git -C $repoRoot rev-parse HEAD)
if ($LASTEXITCODE -ne 0 -or -not $sourceCommit.Trim()) {
    throw "Could not resolve the local source commit"
}
$sourceCommit = $sourceCommit.Trim()
$sourceChanges = @(& git -C $repoRoot status --porcelain=v1)
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect the local Git worktree"
}
if ($sourceChanges.Count -gt 0) {
    throw "The UOS remote build requires a clean local Git worktree"
}

$sshArgs = @()
$scpArgs = @()
if ($IdentityFile) {
    $resolvedIdentity = (Resolve-Path -LiteralPath $IdentityFile).Path
    $sshArgs += @("-i", $resolvedIdentity)
    $scpArgs += @("-i", $resolvedIdentity)
}

function Quote-Posix([string]$Value) {
    if ($Value.Contains("'")) {
        throw "A remote build argument contains an unsupported apostrophe"
    }
    return "'$Value'"
}

$quotedRepo = Quote-Posix $BuilderRepoPath
$quotedCommit = Quote-Posix $sourceCommit
$quotedBaseUrl = Quote-Posix $BaseUrl.TrimEnd("/")
$quotedChannel = Quote-Posix $Channel
$remoteResultArchive = $null
$quotedResultArchive = $null
if ($ExportResult) {
    $remoteResultArchive = (
        "$BuilderRepoPath/dist/uos-build-results/$Version/" +
        "uos-build-result-$Version.zip"
    )
    $quotedResultArchive = Quote-Posix $remoteResultArchive
}
$remoteCaArgument = " --no-ca-bundle"
if ($CaBundle) {
    $resolvedCaBundle = (Resolve-Path -LiteralPath $CaBundle).Path
    $remoteInputDirectory = (
        "$BuilderRepoPath/dist/uos-arm64/build-input-$sourceCommit"
    )
    $remoteCaBundle = "$remoteInputDirectory/intdemo-caddy-root.crt"
    & ssh @sshArgs $BuilderHost (
        "mkdir -p " + (Quote-Posix $remoteInputDirectory)
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the UOS builder input directory"
    }
    & scp @scpArgs $resolvedCaBundle "${BuilderHost}:$remoteCaBundle"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upload the CA bundle to the UOS build host"
    }
    $remoteCaArgument = " --ca-bundle " + (Quote-Posix $remoteCaBundle)
}
$remoteCommand = (
    "cd $quotedRepo" +
    " && test -z `"`$(git status --porcelain=v1)`"" +
    " && git fetch --prune origin" +
    " && git cat-file -e $quotedCommit^{commit}" +
    " && git checkout --detach $quotedCommit" +
    " && test `"`$(git rev-parse HEAD)`" = $quotedCommit" +
    " && bash scripts/uos-arm64/build.sh" +
    " --base-url $quotedBaseUrl --channel $quotedChannel" +
    $remoteCaArgument +
    $(
        if ($ExportResult) {
            " && ./.conda-uos-arm64/bin/python -m release_publisher.release_tasks" +
            " export-uos-result --version " + (Quote-Posix $Version) +
            " --base-url $quotedBaseUrl --channel $quotedChannel" +
            $remoteCaArgument +
            " --output $quotedResultArchive --allow-detached"
        } else {
            ""
        }
    )
)

Write-Host "Building UOS ARM64 package on $BuilderHost at commit $sourceCommit"
& ssh @sshArgs $BuilderHost $remoteCommand
if ($LASTEXITCODE -ne 0) {
    throw (
        "UOS ARM64 remote build failed. Confirm that the builder checkout is " +
        "clean and that commit $sourceCommit has been pushed to its origin."
    )
}

$outputRoot = Join-Path $repoRoot "dist\uos-arm64"
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
$artifactName = "IntDemo-UOS-arm64-$Version.deb"
$artifact = Join-Path $outputRoot $artifactName
$checksum = "$artifact.sha256"
$remoteArtifact = "$BuilderRepoPath/dist/uos-arm64/$artifactName"
& scp @scpArgs "${BuilderHost}:$remoteArtifact" $artifact
if ($LASTEXITCODE -ne 0) {
    throw "Could not download the UOS ARM64 package from the build host"
}
& scp @scpArgs "${BuilderHost}:$remoteArtifact.sha256" $checksum
if ($LASTEXITCODE -ne 0) {
    throw "Could not download the UOS ARM64 checksum from the build host"
}

$expectedHash = ((Get-Content -LiteralPath $checksum -Raw).Trim() -split '\s+')[0]
$actualHash = (
    Get-FileHash -LiteralPath $artifact -Algorithm SHA256
).Hash.ToLowerInvariant()
if ($expectedHash -notmatch '^[0-9a-fA-F]{64}$' -or
    $actualHash -ne $expectedHash.ToLowerInvariant()) {
    throw "The downloaded UOS ARM64 package failed SHA-256 verification"
}

Write-Host "UOS ARM64 package downloaded: $artifact"
Write-Host "SHA-256: $actualHash"
if ($ExportResult) {
    $resultRoot = Join-Path $repoRoot "dist\uos-build-results\$Version"
    New-Item -ItemType Directory -Force -Path $resultRoot | Out-Null
    $resultArchive = Join-Path $resultRoot "uos-build-result-$Version.zip"
    $resultChecksum = "$resultArchive.sha256"
    & scp @scpArgs "${BuilderHost}:$remoteResultArchive" $resultArchive
    if ($LASTEXITCODE -ne 0) {
        throw "Could not download the UOS build result archive"
    }
    & scp @scpArgs "${BuilderHost}:$remoteResultArchive.sha256" $resultChecksum
    if ($LASTEXITCODE -ne 0) {
        throw "Could not download the UOS build result checksum"
    }
    $expectedResultHash = (
        (Get-Content -LiteralPath $resultChecksum -Raw).Trim() -split '\s+'
    )[0]
    $actualResultHash = (
        Get-FileHash -LiteralPath $resultArchive -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($expectedResultHash -notmatch '^[0-9a-fA-F]{64}$' -or
        $actualResultHash -ne $expectedResultHash.ToLowerInvariant()) {
        throw "The downloaded UOS build result archive failed SHA-256 verification"
    }
    Write-Host "UOS build result downloaded: $resultArchive"
    Write-Host "SHA-256: $actualResultHash"
}
