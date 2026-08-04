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

    [string]$IdentityFile = ""
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
    $remoteCaArgument
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
