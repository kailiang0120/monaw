<#
.SYNOPSIS
    Check every commit in a range, not just the tip.

.DESCRIPTION
    The Verify workflow proves that the tip of a branch passes. It cannot prove
    that each commit on the way there is buildable: a commit that imports a
    module which only lands in a later commit merges cleanly and still passes at
    the tip, but it breaks `git bisect`, a partial revert, and anyone who checks
    it out.

    This script walks the range one commit at a time in a scratch worktree and
    runs the fast integrity checks on each: the backend compiles and every
    module imports, and the frontend type-checks. It deliberately does not run
    the test suites -- that is the tip's job, and running them per commit would
    cost hours.

    The scratch worktree means the checkout under test is never your working
    tree, so this is safe to run with uncommitted changes in progress.

.PARAMETER Base
    Exclusive start of the range. Defaults to $env:PR_BASE, then
    $env:PUSH_BEFORE, then the merge base with origin/main.

.PARAMETER Head
    Inclusive end of the range. Defaults to $env:PR_HEAD, then HEAD.

.PARAMETER MaxCommits
    Cap on commits checked. When the range is longer, the newest are checked and
    the rest are reported as skipped.

.EXAMPLE
    ./scripts/check-commits.ps1
    ./scripts/check-commits.ps1 -Base main -Head HEAD
    ./scripts/check-commits.ps1 -SkipFrontend
#>
param(
    [string]$Base = "",
    [string]$Head = "",
    [int]$MaxCommits = 30,
    [switch]$SkipBackend,
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

function Remove-DirectoryLink {
    <#
        Delete a junction/symlink itself, never its target. Remove-Item -Recurse
        on a tree containing a directory link follows the link and deletes the
        real files behind it, which would wipe the linked node_modules.
    #>
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.LinkType) { return $true }
    try {
        [System.IO.Directory]::Delete($item.FullName, $false)
        return -not (Test-Path -LiteralPath $Path)
    } catch {
        Write-Warning "Could not unlink $Path : $($_.Exception.Message)"
        return $false
    }
}

function Test-CommitExists {
    param([string]$Ref)
    if ([string]::IsNullOrWhiteSpace($Ref)) { return $false }
    if ($Ref -match '^0{7,40}$') { return $false }
    git -C $repoRoot cat-file -e "$Ref^{commit}" 2>$null
    return $LASTEXITCODE -eq 0
}

function Resolve-Head {
    foreach ($candidate in @($Head, $env:PR_HEAD, "HEAD")) {
        if (Test-CommitExists $candidate) {
            return (git -C $repoRoot rev-parse "$candidate^{commit}")
        }
    }
    throw "Could not resolve a head commit to check."
}

function Resolve-Base {
    param([string]$HeadSha)
    foreach ($candidate in @($Base, $env:PR_BASE, $env:PUSH_BEFORE)) {
        if (Test-CommitExists $candidate) {
            return (git -C $repoRoot rev-parse "$candidate^{commit}")
        }
    }
    # No range was supplied: fall back to whatever this branch adds on top of
    # the default branch, then to the single head commit.
    foreach ($upstream in @("origin/main", "main")) {
        if (Test-CommitExists $upstream) {
            $mergeBase = git -C $repoRoot merge-base $upstream $HeadSha 2>$null
            if ($LASTEXITCODE -eq 0 -and $mergeBase -and $mergeBase -ne $HeadSha) {
                return $mergeBase.Trim()
            }
        }
    }
    return ""
}

$headSha = Resolve-Head
$baseSha = Resolve-Base -HeadSha $headSha

if ($baseSha) {
    $range = "$baseSha..$headSha"
    $commits = @(git -C $repoRoot rev-list --reverse $range)
} else {
    Write-Host "No base commit resolved; checking only $($headSha.Substring(0, 8))."
    $commits = @($headSha)
}

if ($commits.Count -eq 0) {
    Write-Host "No commits in range; nothing to check."
    exit 0
}

$skipped = 0
if ($commits.Count -gt $MaxCommits) {
    $skipped = $commits.Count - $MaxCommits
    $commits = $commits[-$MaxCommits..-1]
}

$python = Join-Path $repoRoot "backend/.venv/Scripts/python.exe"
if (-not (Test-Path $python)) {
    $python = Join-Path $repoRoot "backend/.venv/bin/python"
}
if (-not (Test-Path $python)) {
    $python = "python"
}

# Run the checker from a copy: commits older than this script must still be
# checked, and their trees do not contain it.
$stagingDir = Join-Path ([System.IO.Path]::GetTempPath()) "monaw-commit-check-$([guid]::NewGuid().ToString('N'))"
$worktree = Join-Path $stagingDir "tree"
New-Item -ItemType Directory -Path $stagingDir -Force | Out-Null
$checker = Join-Path $stagingDir "check-imports.py"
Copy-Item (Join-Path $PSScriptRoot "check-imports.py") $checker
$linkPath = Join-Path $worktree "frontend/node_modules"

Write-Host "Checking $($commits.Count) commit(s)$(if ($skipped) { " (skipping $skipped older)" })"
Write-Host ""

$results = [System.Collections.Generic.List[object]]::new()
try {
    git -C $repoRoot worktree add --detach --quiet $worktree $commits[0]
    if ($LASTEXITCODE -ne 0) { throw "Could not create a scratch worktree." }

    # The frontend type-check needs the dependencies that are already installed
    # in the real tree; a link avoids a second npm ci per commit.
    $frontendReady = $false
    if (-not $SkipFrontend) {
        $sourceModules = Join-Path $repoRoot "frontend/node_modules"
        $tsc = Join-Path $sourceModules "typescript/bin/tsc"
        if (Test-Path $tsc) {
            $linkType = if ($IsWindows -or $env:OS -eq "Windows_NT") { "Junction" } else { "SymbolicLink" }
            try {
                New-Item -ItemType $linkType -Path $linkPath -Target $sourceModules -ErrorAction Stop | Out-Null
                $frontendReady = $true
            } catch {
                Write-Warning "Could not link frontend/node_modules into the scratch worktree: $($_.Exception.Message)"
            }
        } else {
            Write-Warning "frontend/node_modules/typescript is missing; run 'npm ci' in frontend to enable the per-commit type-check."
        }
    }

    foreach ($commit in $commits) {
        $short = $commit.Substring(0, 8)
        $subject = git -C $repoRoot log -1 --format=%s $commit
        Write-Host "=== $short $subject"

        git -C $worktree checkout --detach --quiet $commit
        if ($LASTEXITCODE -ne 0) { throw "Could not check out $short in the scratch worktree." }

        $failures = [System.Collections.Generic.List[string]]::new()

        if (-not $SkipBackend) {
            & $python $checker --tree $worktree
            if ($LASTEXITCODE -ne 0) { $failures.Add("backend imports") }
        }

        if ($frontendReady) {
            Push-Location (Join-Path $worktree "frontend")
            try {
                # Invoke the entry points directly; npx resolves inconsistently
                # through a junction.
                & node (Join-Path $repoRoot "frontend/node_modules/typescript/bin/tsc") --noEmit
                if ($LASTEXITCODE -ne 0) { $failures.Add("frontend typecheck") }

                # A tree can type-check and still not bundle, and a broken
                # bundle is what the end user would actually hit.
                & node (Join-Path $repoRoot "frontend/node_modules/vite/bin/vite.js") build --logLevel warn
                if ($LASTEXITCODE -ne 0) { $failures.Add("renderer build") }
            } finally {
                Pop-Location
            }
        }

        $results.Add([pscustomobject]@{
            Commit  = $short
            Subject = $subject
            Failed  = ($failures -join ", ")
        })
        Write-Host ""
    }
} finally {
    # Unlink before any recursive delete. If the link survives, leave the
    # scratch directory in place: stale temp files are recoverable, the real
    # node_modules behind the link is not.
    $unlinked = Remove-DirectoryLink $linkPath
    if ($unlinked) {
        git -C $repoRoot worktree remove --force $worktree 2>$null | Out-Null
        git -C $repoRoot worktree prune 2>$null | Out-Null
        Remove-Item -LiteralPath $stagingDir -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Write-Warning "Left $stagingDir in place because frontend/node_modules is still linked into it."
    }
}

Write-Host "--- Summary ---"
foreach ($result in $results) {
    $status = if ($result.Failed) { "FAIL ($($result.Failed))" } else { "ok" }
    Write-Host ("{0}  {1,-24} {2}" -f $result.Commit, $status, $result.Subject)
}
if ($skipped) {
    Write-Host "$skipped older commit(s) skipped by -MaxCommits $MaxCommits."
}

$broken = @($results | Where-Object { $_.Failed })
if ($broken.Count -gt 0) {
    Write-Host ""
    Write-Host "$($broken.Count) of $($results.Count) commit(s) are not independently buildable."
    exit 1
}
Write-Host ""
Write-Host "All $($results.Count) commit(s) are independently buildable."
