#Requires -Version 7.0
<#
.SYNOPSIS
  Operator helper for Windows Sandbox evidence bundle-verify, review, and certify.

.DESCRIPTION
  Wraps `python -I -m neil_agent.sandbox_evidence` for post-CI certification.
  See docs/sandbox-certification-runbook.md for the full procedure.

.PARAMETER BundleRoot
  Absolute path to the extracted evidence bundle root.

.PARAMETER Phase
  Verify | Review | Certify | All

.EXAMPLE
  .\scripts\windows-sandbox-certify.ps1 -BundleRoot C:\evidence\bundle -Phase Verify
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $BundleRoot,

    [ValidateSet('Verify', 'Review', 'Certify', 'All')]
    [string] $Phase = 'All',

    [string] $Python = '',

    [string] $ReviewerId = 'independent-security-reviewer',

    [string] $ReviewId = '',

    [string] $ReviewedAt = '',

    [string] $TrustedReviewer = '',

    [string] $TrustedReviewSha256 = '',

    [string] $IssuedAt = '',

    [string] $ExpiresAt = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-Python {
    param([string] $Override)
    if ($Override) {
        return (Resolve-Path -LiteralPath $Override).Path
    }
    if ($env:VIRTUAL_ENV) {
        $candidate = Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe'
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    return 'python'
}

function Invoke-EvidenceCli {
    param([string[]] $Arguments)
    $python = Resolve-Python -Override $Python
    Write-Host ">> $python -I -m neil_agent.sandbox_evidence $($Arguments -join ' ')"
    & $python -I -m neil_agent.sandbox_evidence @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "sandbox_evidence exited with code $LASTEXITCODE"
    }
}

function Get-ReviewSha256 {
    param([string] $ReviewPath)
    $raw = Get-Content -LiteralPath $ReviewPath -Raw -Encoding utf8 | ConvertFrom-Json
    return [string]$raw.review_sha256
}

$bundlePath = [System.IO.Path]::GetFullPath($BundleRoot)
if (-not (Test-Path -LiteralPath $bundlePath)) {
    throw "Bundle root does not exist: $bundlePath"
}
if (-not [System.IO.Path]::IsPathRooted($bundlePath)) {
    throw 'BundleRoot must be an absolute path.'
}

$reviewPath = Join-Path $bundlePath 'independent-review.json'
$certPath = Join-Path $bundlePath 'certification.json'

if ($Phase -in @('Verify', 'All')) {
    Write-Host '=== Phase: bundle-verify ===' -ForegroundColor Cyan
    Invoke-EvidenceCli @(
        'bundle-verify',
        '--bundle-root', $bundlePath
    )
}

if ($Phase -in @('Review', 'All')) {
    Write-Host '=== Phase: independent review ===' -ForegroundColor Cyan
    if (-not $ReviewId) {
        $ReviewId = ([guid]::NewGuid().ToString('N')).ToLowerInvariant()
    }
    if (-not $ReviewedAt) {
        $ReviewedAt = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    }
    Invoke-EvidenceCli @(
        'review',
        '--bundle-root', $bundlePath,
        '--review-id', $ReviewId,
        '--reviewer-id', $ReviewerId,
        '--reviewed-at', $ReviewedAt,
        '--output', $reviewPath
    )
    $digest = Get-ReviewSha256 -ReviewPath $reviewPath
    Write-Host ''
    Write-Host 'PIN THIS REVIEW SHA-256 OUT OF BAND:' -ForegroundColor Yellow
    Write-Host $digest
    Write-Host ''
}

if ($Phase -in @('Certify', 'All')) {
    Write-Host '=== Phase: certification ===' -ForegroundColor Cyan
    if (-not (Test-Path -LiteralPath $reviewPath)) {
        throw "Missing $reviewPath ; run Review phase first."
    }
    if (-not $TrustedReviewer) {
        $TrustedReviewer = $ReviewerId
    }
    if (-not $TrustedReviewSha256) {
        $TrustedReviewSha256 = Get-ReviewSha256 -ReviewPath $reviewPath
        Write-Host "Using review digest from file: $TrustedReviewSha256"
    }
    if (-not $IssuedAt) {
        $IssuedAt = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    }
    if (-not $ExpiresAt) {
        $ExpiresAt = [DateTime]::UtcNow.AddDays(30).ToString('yyyy-MM-ddTHH:mm:ssZ')
    }
    Invoke-EvidenceCli @(
        'certify',
        '--bundle-root', $bundlePath,
        '--review', $reviewPath,
        '--trusted-reviewer', $TrustedReviewer,
        '--trusted-review-sha256', $TrustedReviewSha256,
        '--issued-at', $IssuedAt,
        '--expires-at', $ExpiresAt,
        '--output', $certPath
    )
    Write-Host "Wrote certification: $certPath" -ForegroundColor Green
}

Write-Host 'Done.' -ForegroundColor Green
