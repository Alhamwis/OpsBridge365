<#
.SYNOPSIS
    Shared resolver for the Azure CLI executable. Dot-source it; do not run it.

.DESCRIPTION
    Every script in this directory needs the same answer to one question: what
    do I actually invoke to run az? "az" is not a safe answer.

    The Azure CLI installer writes its directory into the *User* PATH, and a
    process tree that started before that write never sees it - the environment
    block was copied at process creation and is not refreshed. So a shell that
    was already open when az was installed reports "az is not installed" while
    az sits on disk, fully working. That is a false negative, and a report that
    under-states what it could have checked is worse than one that checks less.

    Resolve-AzCli therefore tries four sources, in order:

      1. Get-Command az            - the current process PATH
      2. the User PATH read live   - [Environment]::GetEnvironmentVariable
                                     reads the registry, not this process's
                                     stale copy, so an install that happened
                                     after this shell started is still found
      3. the known OpsBridge tools path (literal, see below)
      4. $env:LOCALAPPDATA\opsbridge-tools\azcli-venv\Scripts\az.bat

    It returns the full path to invoke, or an empty string when all four fail -
    and only then is "the Azure CLI is not installed" a true statement.

    Why the literal path in step 3: this project's az lives in a pip venv under
    %LOCALAPPDATA%\opsbridge-tools, not in Program Files, so there is no
    registry key or standard location to probe. Step 4 covers the same install
    for any other user profile; step 3 covers the case where LOCALAPPDATA is
    unset or points somewhere else (a service account, a sudo-style elevation).
    Neither step invents a path - both are checked with Test-Path first.

.OUTPUTS
    System.String - the az path, or '' when it could not be resolved.

.EXAMPLE
    . (Join-Path $PSScriptRoot 'Resolve-AzCli.ps1')
    $azCli = Resolve-AzCli
    if ([string]::IsNullOrWhiteSpace($azCli)) { Write-Host (Get-AzCliNotFoundDetail) }
#>

# The install this repo provisions. Kept as a constant so the two places that
# reference it (the probe and the not-found message) can never drift apart.
$script:AzCliFallbackPath = "$env:LOCALAPPDATA\opsbridge-tools\azcli-venv\Scripts\az.bat"
$script:AzCliRelativePath = 'opsbridge-tools\azcli-venv\Scripts\az.bat'

function Resolve-AzCli {
    [CmdletBinding()]
    [OutputType([string])]
    param()

    # 1. On the PATH this process inherited. Fastest and the common case.
    $onPath = @(Get-Command -Name 'az' -ErrorAction Ignore)
    if ($onPath.Count -gt 0) {
        $first = $onPath[0]
        # Aliases and functions have no .Path; fall back to the bare name,
        # which is by definition invokable if Get-Command resolved it.
        if (-not [string]::IsNullOrWhiteSpace($first.Path)) {
            return $first.Path
        }
        return $first.Name
    }

    $candidates = New-Object System.Collections.ArrayList

    # 2. The User PATH as it is on disk right now, not as this process cached
    #    it at startup. This is the branch that fixes the false negative.
    try {
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        if (-not [string]::IsNullOrWhiteSpace($userPath)) {
            foreach ($entry in ($userPath -split ';')) {
                if ([string]::IsNullOrWhiteSpace($entry)) { continue }
                $directory = $entry.Trim().Trim('"')
                if ([string]::IsNullOrWhiteSpace($directory)) { continue }
                foreach ($leaf in @('az.cmd', 'az.bat', 'az.exe')) {
                    try {
                        $null = $candidates.Add((Join-Path -Path $directory -ChildPath $leaf))
                    }
                    catch {
                        # An invalid PATH entry is not this script's problem.
                        Write-Verbose "Skipping unusable PATH entry: $directory"
                    }
                }
            }
        }
    }
    catch {
        Write-Verbose "Could not read the User PATH: $($_.Exception.Message)"
    }

    # 3. The known install location for this project's tooling.
    $null = $candidates.Add($script:AzCliFallbackPath)

    # 4. The same install, resolved through the current profile.
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $null = $candidates.Add((Join-Path -Path $env:LOCALAPPDATA -ChildPath $script:AzCliRelativePath))
    }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }

    return ''
}

function Get-AzCliNotFoundDetail {
    <#
        The message to print when Resolve-AzCli returns ''. It names every
        place that was searched, so "not installed" is a conclusion the reader
        can check rather than an assertion they have to trust.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param()

    $localAppData = $env:LOCALAPPDATA
    if ([string]::IsNullOrWhiteSpace($localAppData)) { $localAppData = '%LOCALAPPDATA%' }
    $profilePath = Join-Path -Path $localAppData -ChildPath $script:AzCliRelativePath

    return ("az not found on PATH, in the live User PATH, at {0}, or at {1} - install it: https://aka.ms/installazurecli" -f
        $script:AzCliFallbackPath, $profilePath)
}
