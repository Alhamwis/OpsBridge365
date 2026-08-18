<#
.SYNOPSIS
    Read-only health and acceptance report for OpsBridge365.

.DESCRIPTION
    Runs every check that can be run, reports the rest honestly, and never
    changes cloud state. It is designed to run end to end on a machine with
    nothing set up: no Azure login, no Docker, no dependencies installed.

    The rule that matters: a check that could not be run is reported SKIP, never
    PASS. An unauthenticated run tells you what it did NOT verify.

    Statuses:
      PASS  the check ran and succeeded
      FAIL  the check ran and failed          <- the only status that sets the exit code
      WARN  ran, non-fatal finding (a dirty working tree is not a broken system)
      SKIP  could not run (missing tool, not logged in, nothing deployed)

    The only thing this script may start is a short-lived local container used
    to probe /healthz, and it is always removed again. Use -SkipDocker to
    suppress even that.

.PARAMETER ResourceGroup
    Azure resource group to inspect. Defaults to $env:AZURE_RESOURCE_GROUP, then
    'rg-opsbridge365' (the default used by .github/workflows/deploy.yml).

.PARAMETER SubscriptionId
    Subscription holding that resource group. Defaults to
    $env:AZURE_SUBSCRIPTION_ID, and when neither is given, every subscription
    this login can see is searched for the group.

    Worth being explicit about why this is not just "the current subscription":
    this project spans two tenants, and signing in to do Graph work changes the
    Azure CLI default. A report that trusted the default once concluded
    "nothing is deployed yet" about a resource group that was deployed and
    healthy in the other subscription. Every cloud check below now names the
    subscription it looked in.

.PARAMETER NamePrefix
    Bicep namePrefix, used to derive the app and job names. Default 'opsbridge'.

.PARAMETER ImageTag
    Local image tag to look for or build. Default 'opsbridge365:local'.

.PARAMETER Port
    Host port for the throwaway local container. Default 8000.

.PARAMETER SkipDocker
    Skip the image build and the local container probe.

.PARAMETER SkipTests
    Skip the pytest run.

.EXAMPLE
    powershell -NoProfile -File scripts/verify-opsbridge.ps1

.EXAMPLE
    powershell -NoProfile -File scripts/verify-opsbridge.ps1 -SkipDocker -SkipTests
#>

[CmdletBinding()]
param(
    [string] $ResourceGroup,
    [string] $SubscriptionId,
    [string] $NamePrefix = 'opsbridge',
    [string] $ImageTag = 'opsbridge365:local',
    [int]    $Port = 8000,
    [switch] $SkipDocker,
    [switch] $SkipTests
)

# 1.0 (uninitialised variables), deliberately not 2.0+: az returns JSON whose
# optional properties are legitimately absent, and 2.0 turns every one of those
# into a terminating error.
Set-StrictMode -Version 1.0
$ErrorActionPreference = 'Continue'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VerifyContainerName = 'opsbridge-verify'

# Shared az resolution - see scripts/Resolve-AzCli.ps1 for why 'az' on PATH is
# not a sufficient test.
$AzHelperPath = Join-Path -Path $PSScriptRoot -ChildPath 'Resolve-AzCli.ps1'
if (-not (Test-Path -LiteralPath $AzHelperPath)) {
    Write-Host "Missing helper: $AzHelperPath" -ForegroundColor Red
    Write-Host 'Run this script from a full checkout of the repository.' -ForegroundColor Yellow
    exit 1
}
. $AzHelperPath

# Shared subscription resolution - see scripts/Resolve-AzSubscription.ps1 for
# why the ambient az default is not a safe answer in this project.
$SubHelperPath = Join-Path -Path $PSScriptRoot -ChildPath 'Resolve-AzSubscription.ps1'
if (-not (Test-Path -LiteralPath $SubHelperPath)) {
    Write-Host "Missing helper: $SubHelperPath" -ForegroundColor Red
    Write-Host 'Run this script from a full checkout of the repository.' -ForegroundColor Yellow
    exit 1
}
. $SubHelperPath

if ([string]::IsNullOrWhiteSpace($ResourceGroup)) {
    if (-not [string]::IsNullOrWhiteSpace($env:AZURE_RESOURCE_GROUP)) {
        $ResourceGroup = $env:AZURE_RESOURCE_GROUP
    }
    else {
        $ResourceGroup = 'rg-opsbridge365'
    }
}

$ApiName = "$NamePrefix-api"
$JobName = "$NamePrefix-sync"

# Windows PowerShell 5.1 can still default to TLS 1.0, which every Azure
# endpoint refuses. Opt in to 1.2 before any HTTPS probe.
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
}
catch {
    Write-Verbose "Could not raise TLS version: $($_.Exception.Message)"
}

# ---------------------------------------------------------------- helpers --

$Results = New-Object System.Collections.ArrayList

function Add-Result {
    param(
        [Parameter(Mandatory)][string] $Check,
        [Parameter(Mandatory)][ValidateSet('PASS', 'FAIL', 'WARN', 'SKIP')][string] $Status,
        [string] $Detail = ''
    )
    $null = $Results.Add([pscustomobject]@{
            Check  = $Check
            Status = $Status
            Detail = $Detail
        })

    $color = 'Gray'
    if ($Status -eq 'PASS') { $color = 'Green' }
    if ($Status -eq 'FAIL') { $color = 'Red' }
    if ($Status -eq 'WARN') { $color = 'Yellow' }
    if ($Status -eq 'SKIP') { $color = 'DarkGray' }
    Write-Host ("  [{0}] {1}" -f $Status, $Check) -ForegroundColor $color
    if (-not [string]::IsNullOrWhiteSpace($Detail)) {
        Write-Host ("         {0}" -f $Detail) -ForegroundColor DarkGray
    }
}

function Test-CommandExists {
    param([Parameter(Mandatory)][string] $Name)
    $found = Get-Command -Name $Name -ErrorAction Ignore
    return ($null -ne $found)
}

function Invoke-Native {
    <#
        Runs a native executable and returns its exit code plus trimmed output.
        Exit code is read from $LASTEXITCODE, never from $? - in PowerShell 5.1
        a native command that merely writes to stderr flips $? to false.
    #>
    param(
        [Parameter(Mandatory)][string]   $FilePath,
        [Parameter(Mandatory)][string[]] $CommandArgs
    )
    $output = & $FilePath @CommandArgs 2>$null
    $code = $LASTEXITCODE
    $text = ($output | Out-String)
    if ($null -eq $text) { $text = '' }
    return [pscustomobject]@{
        ExitCode = $code
        Output   = $text.Trim()
    }
}

function Test-HttpEndpoint {
    param(
        [Parameter(Mandatory)][string] $Url,
        [int] $TimeoutSec = 15
    )
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
        return [pscustomobject]@{
            Ok      = ($response.StatusCode -eq 200)
            Code    = [int]$response.StatusCode
            Content = ($response.Content | Out-String).Trim()
        }
    }
    catch {
        return [pscustomobject]@{
            Ok      = $false
            Code    = 0
            Content = $_.Exception.Message
        }
    }
}

function Wait-HttpEndpoint {
    param(
        [Parameter(Mandatory)][string] $Url,
        [int] $Attempts = 10,
        [int] $DelaySeconds = 3,
        [int] $TimeoutSec = 15
    )
    $last = $null
    for ($i = 1; $i -le $Attempts; $i++) {
        $last = Test-HttpEndpoint -Url $Url -TimeoutSec $TimeoutSec
        if ($last.Ok) {
            return $last
        }
        if ($i -lt $Attempts) {
            Start-Sleep -Seconds $DelaySeconds
        }
    }
    return $last
}

function Test-TcpPortFree {
    <#
        True when nothing is listening on the loopback port. Worth checking
        before publishing a container port: Docker Desktop on Windows will
        happily accept -p on a port another process already holds, and then the
        other process answers the probe. That looks like a broken container and
        is not one.
    #>
    param([Parameter(Mandatory)][int] $Number)
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Number)
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $listener) {
            try { $listener.Stop() } catch { }
        }
    }
}

function Get-FreeTcpPort {
    <# Ask the OS for an unused port by binding port 0 and reading it back. #>
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $chosen = $listener.LocalEndpoint.Port
    $listener.Stop()
    return $chosen
}

function Write-Section {
    param([Parameter(Mandatory)][string] $Title)
    Write-Host ''
    Write-Host $Title -ForegroundColor Cyan
    Write-Host ('-' * 72) -ForegroundColor DarkGray
}

# ------------------------------------------------------------------- start --

Write-Host ''
Write-Host '================================================================' -ForegroundColor Cyan
Write-Host ' OpsBridge365 - verification report (read-only)' -ForegroundColor Cyan
Write-Host '================================================================' -ForegroundColor Cyan
Write-Host (" repo           : {0}" -f $RepoRoot)
Write-Host (" resource group : {0}" -f $ResourceGroup)
Write-Host (" api / job      : {0} / {1}" -f $ApiName, $JobName)

# The Azure identity work happens here, before the sections, for one reason:
# the reader has to know which subscription the cloud rows describe before they
# read them. The findings are only recorded as report rows later, in the Azure
# section, so the table keeps its order.
$AzCli = Resolve-AzCli
$azPresent = -not [string]::IsNullOrWhiteSpace($AzCli)
$azVersionText = ''
$loggedIn = $false
$defaultSubscription = ''
$azSkipReason = ''
$Sub = $null
$SubscriptionArgs = @()

if (-not $azPresent) {
    $azSkipReason = Get-AzCliNotFoundDetail
}
else {
    # Also proves the resolved path actually runs. A file that exists but
    # cannot execute is not a working CLI, and reporting PASS on the strength
    # of Test-Path alone would be the same class of lie as reporting "not
    # installed" for something that is installed.
    # -o json, not --query: 'azure-cli' contains a hyphen, and the JMESPath
    # quoting that needs does not survive being passed to az.bat (exit 2).
    $azVersion = Invoke-Native -FilePath $AzCli -CommandArgs @('version', '--only-show-errors', '-o', 'json')
    if ($azVersion.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($azVersion.Output)) {
        try {
            $parsedVersion = $azVersion.Output | ConvertFrom-Json
            $azVersionText = [string]$parsedVersion.'azure-cli'
        }
        catch {
            $azVersionText = ''
        }
    }

    $account = Invoke-Native -FilePath $AzCli -CommandArgs @('account', 'show', '--only-show-errors', '-o', 'json')
    if ($account.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($account.Output)) {
        try {
            $parsedAccount = $account.Output | ConvertFrom-Json
            $loggedIn = $true
            $defaultSubscription = [string]$parsedAccount.name
        }
        catch {
            $azSkipReason = 'az account show returned unparseable JSON'
        }
    }
    else {
        $azSkipReason = 'not logged in - run: az login'
    }
}

if ($loggedIn) {
    Write-Host "  resolving which subscription holds $ResourceGroup ..." -ForegroundColor DarkGray
    $Sub = Resolve-AzSubscription -AzCli $AzCli -ResourceGroup $ResourceGroup -SubscriptionId $SubscriptionId
    if ($Sub.Status -eq 'Resolved') {
        # Every az call below is pinned to this, so the report cannot drift
        # onto another subscription halfway through.
        $SubscriptionArgs = @('--subscription', $Sub.Id)
        Write-Host (" subscription   : {0} ({1})" -f $Sub.Name, $Sub.Id)
    }
    else {
        Write-Host (" subscription   : unresolved - {0}" -f $Sub.Detail) -ForegroundColor Yellow
    }
}
else {
    Write-Host ' subscription   : not determined (no usable az login)'
}

Push-Location $RepoRoot
try {

    # --------------------------------------------------------- local tools --
    Write-Section 'Local toolchain'

    if (Test-CommandExists 'python') {
        $py = Invoke-Native -FilePath 'python' -CommandArgs @('--version')
        $version = $py.Output
        if ($py.ExitCode -ne 0) {
            Add-Result -Check 'python present' -Status 'FAIL' -Detail "python --version exited $($py.ExitCode)"
        }
        elseif ($version -match '(\d+)\.(\d+)') {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 12)) {
                Add-Result -Check 'python >= 3.12' -Status 'PASS' -Detail $version
            }
            else {
                Add-Result -Check 'python >= 3.12' -Status 'FAIL' -Detail "$version (pyproject requires >= 3.12)"
            }
        }
        else {
            Add-Result -Check 'python >= 3.12' -Status 'WARN' -Detail "could not parse: $version"
        }
    }
    else {
        Add-Result -Check 'python present' -Status 'FAIL' -Detail 'python is not on PATH'
    }

    $dockerPresent = Test-CommandExists 'docker'
    $dockerRunning = $false
    if ($dockerPresent) {
        $server = Invoke-Native -FilePath 'docker' -CommandArgs @('version', '--format', '{{.Server.Version}}')
        if ($server.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($server.Output)) {
            $dockerRunning = $true
            Add-Result -Check 'docker present and daemon up' -Status 'PASS' -Detail "server $($server.Output)"
        }
        else {
            Add-Result -Check 'docker present and daemon up' -Status 'WARN' -Detail 'docker CLI found but the daemon is not responding'
        }
    }
    else {
        Add-Result -Check 'docker present' -Status 'SKIP' -Detail 'docker is not on PATH'
    }

    if (Test-CommandExists 'git') {
        $status = Invoke-Native -FilePath 'git' -CommandArgs @('status', '--porcelain')
        if ($status.ExitCode -ne 0) {
            Add-Result -Check 'git working tree' -Status 'SKIP' -Detail 'not a git repository'
        }
        elseif ([string]::IsNullOrWhiteSpace($status.Output)) {
            Add-Result -Check 'git working tree clean' -Status 'PASS'
        }
        else {
            $changed = ($status.Output -split "`n").Count
            Add-Result -Check 'git working tree clean' -Status 'WARN' -Detail "$changed uncommitted change(s) - not a failure, just uncommitted"
        }
    }
    else {
        Add-Result -Check 'git present' -Status 'SKIP' -Detail 'git is not on PATH'
    }

    # -------------------------------------------------------------- tests --
    Write-Section 'Tests'

    if ($SkipTests) {
        Add-Result -Check 'pytest' -Status 'SKIP' -Detail '-SkipTests was passed'
    }
    elseif (-not (Test-CommandExists 'python')) {
        Add-Result -Check 'pytest' -Status 'SKIP' -Detail 'python is not on PATH'
    }
    else {
        Write-Host '  running python -m pytest -q ...' -ForegroundColor DarkGray
        $pytest = Invoke-Native -FilePath 'python' -CommandArgs @('-m', 'pytest', '-q')
        $lines = @($pytest.Output -split "`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        $summary = ''
        if ($lines.Count -gt 0) {
            $summary = $lines[$lines.Count - 1].Trim()
        }
        if ($pytest.ExitCode -eq 0) {
            Add-Result -Check 'pytest passes' -Status 'PASS' -Detail $summary
        }
        elseif ($pytest.ExitCode -eq 5) {
            Add-Result -Check 'pytest passes' -Status 'WARN' -Detail 'no tests were collected'
        }
        elseif ($summary -match 'No module named pytest') {
            Add-Result -Check 'pytest passes' -Status 'SKIP' -Detail 'pytest not installed - pip install ".[dev]"'
        }
        else {
            Add-Result -Check 'pytest passes' -Status 'FAIL' -Detail $summary
        }
    }

    # ------------------------------------------------------------- docker --
    Write-Section 'Container'

    $imageAvailable = $false
    if ($SkipDocker) {
        Add-Result -Check 'container image' -Status 'SKIP' -Detail '-SkipDocker was passed'
    }
    elseif (-not $dockerRunning) {
        Add-Result -Check 'container image' -Status 'SKIP' -Detail 'no working docker daemon'
    }
    else {
        $inspect = Invoke-Native -FilePath 'docker' -CommandArgs @('image', 'inspect', $ImageTag)
        if ($inspect.ExitCode -eq 0) {
            $imageAvailable = $true
            Add-Result -Check "image $ImageTag exists" -Status 'PASS' -Detail 'already built; not rebuilt by this report'
        }
        else {
            Write-Host "  image not found, running docker build (this takes a minute) ..." -ForegroundColor DarkGray
            $build = Invoke-Native -FilePath 'docker' -CommandArgs @('build', '-t', $ImageTag, '.')
            if ($build.ExitCode -eq 0) {
                $imageAvailable = $true
                Add-Result -Check 'docker build succeeds' -Status 'PASS' -Detail "built $ImageTag"
            }
            else {
                # BuildKit writes its log to stderr, which Invoke-Native drops,
                # so point at the command that shows the real error.
                $tail = "run 'docker build -t $ImageTag .' to see the build log"
                $buildLines = @($build.Output -split "`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
                if ($buildLines.Count -gt 0) {
                    $tail = $buildLines[$buildLines.Count - 1].Trim()
                }
                Add-Result -Check 'docker build succeeds' -Status 'FAIL' -Detail $tail
            }
        }
    }

    if ($SkipDocker) {
        Add-Result -Check 'local /healthz responds' -Status 'SKIP' -Detail '-SkipDocker was passed'
    }
    elseif (-not $imageAvailable) {
        Add-Result -Check 'local /healthz responds' -Status 'SKIP' -Detail 'no local image to run'
    }
    else {
        $already = Test-HttpEndpoint -Url "http://localhost:$Port/healthz" -TimeoutSec 5
        $portUsable = $true

        if ($already.Ok) {
            Add-Result -Check 'local /healthz responds' -Status 'PASS' -Detail "already served on port $Port - $($already.Content)"
            $portUsable = $false
        }
        elseif (-not (Test-TcpPortFree -Number $Port)) {
            # Occupied, and whatever holds it is not this service.
            if ($PSBoundParameters.ContainsKey('Port')) {
                Add-Result -Check 'local /healthz responds' -Status 'SKIP' -Detail "port $Port is held by another process - pass -Port with a free port"
                $portUsable = $false
            }
            else {
                $newPort = Get-FreeTcpPort
                Write-Host "  port $Port is held by another process, using $newPort instead" -ForegroundColor DarkGray
                $Port = $newPort
            }
        }

        if ($portUsable) {
            $localUrl = "http://localhost:$Port/healthz"
            $started = $false
            try {
                $portMap = "$($Port):8000"
                $run = Invoke-Native -FilePath 'docker' -CommandArgs @(
                    'run', '--detach', '--rm', '--name', $VerifyContainerName, '--publish', $portMap, $ImageTag)
                if ($run.ExitCode -ne 0) {
                    Add-Result -Check 'local /healthz responds' -Status 'FAIL' -Detail "docker run failed: $($run.Output)"
                }
                else {
                    $started = $true
                    $probe = Wait-HttpEndpoint -Url $localUrl -Attempts 10 -DelaySeconds 2 -TimeoutSec 10
                    if ($probe.Ok) {
                        Add-Result -Check 'local /healthz responds' -Status 'PASS' -Detail "$localUrl -> $($probe.Content)"
                    }
                    else {
                        $logs = Invoke-Native -FilePath 'docker' -CommandArgs @('logs', '--tail', '5', $VerifyContainerName)
                        $detail = "no 200 from $localUrl : $($probe.Content)"
                        if (-not [string]::IsNullOrWhiteSpace($logs.Output)) {
                            $detail = "$detail | container log: $($logs.Output -replace "`r?`n", ' / ')"
                        }
                        Add-Result -Check 'local /healthz responds' -Status 'FAIL' -Detail $detail
                    }
                }
            }
            finally {
                if ($started) {
                    # --rm means stopping is enough to clean up.
                    $null = Invoke-Native -FilePath 'docker' -CommandArgs @('stop', $VerifyContainerName)
                }
            }
        }
    }

    # -------------------------------------------------------------- cloud --
    Write-Section 'Azure (cloud checks)'

    # Two distinct states, never conflated:
    #   az unresolvable  -> cannot check at all
    #   az present, no login -> could check, is not authorised to
    # The SKIP detail says which one was hit. Both were determined above the
    # sections so the header could name the subscription; only the reporting
    # happens here, which keeps the table in reading order.
    if (-not $azPresent) {
        Add-Result -Check 'az CLI present' -Status 'SKIP' -Detail $azSkipReason
    }
    else {
        if ([string]::IsNullOrWhiteSpace($azVersionText)) {
            Add-Result -Check 'az CLI present' -Status 'PASS' -Detail $AzCli
        }
        else {
            Add-Result -Check 'az CLI present' -Status 'PASS' -Detail "azure-cli $azVersionText at $AzCli"
        }

        if ($loggedIn) {
            # Reported as the DEFAULT subscription, not as the one being
            # checked. In this project they are routinely different, and
            # conflating them is precisely the defect this parameter fixes.
            Add-Result -Check 'az logged in' -Status 'PASS' -Detail "default subscription: $defaultSubscription"
        }
        else {
            Add-Result -Check 'az logged in' -Status 'SKIP' -Detail $azSkipReason
        }
    }

    $subResolved = ($null -ne $Sub -and $Sub.Status -eq 'Resolved')

    if (-not $loggedIn) {
        $skipReason = $azSkipReason
        if ([string]::IsNullOrWhiteSpace($skipReason)) {
            $skipReason = 'unauthenticated'
        }
        Add-Result -Check 'subscription resolved' -Status 'SKIP' -Detail $skipReason
        Add-Result -Check 'resource group exists' -Status 'SKIP' -Detail $skipReason
        Add-Result -Check 'deployed API /healthz' -Status 'SKIP' -Detail $skipReason
        Add-Result -Check 'sync job exists' -Status 'SKIP' -Detail $skipReason
        Add-Result -Check 'sync job last run' -Status 'SKIP' -Detail $skipReason
    }
    elseif (-not $subResolved) {
        # Ambiguous is a WARN rather than a SKIP: two resource groups of the
        # same name in two subscriptions is a real finding about the
        # environment, not merely a check that could not run.
        $subStatus = 'SKIP'
        if ($Sub.Status -eq 'Ambiguous') { $subStatus = 'WARN' }
        Add-Result -Check 'subscription resolved' -Status $subStatus -Detail $Sub.Detail

        if ($Sub.Status -eq 'NotFound') {
            # The searched subscriptions did not have it. That is all this run
            # established, and it is all the row claims.
            Add-Result -Check 'resource group exists' -Status 'SKIP' -Detail $Sub.Detail
        }
        else {
            Add-Result -Check 'resource group exists' -Status 'SKIP' -Detail 'no single subscription to look in'
        }
        Add-Result -Check 'deployed API /healthz' -Status 'SKIP' -Detail 'no resolved subscription'
        Add-Result -Check 'sync job exists' -Status 'SKIP' -Detail 'no resolved subscription'
        Add-Result -Check 'sync job last run' -Status 'SKIP' -Detail 'no resolved subscription'
    }
    else {
        Add-Result -Check 'subscription resolved' -Status 'PASS' -Detail $Sub.Detail

        $rg = Invoke-Native -FilePath $AzCli -CommandArgs (@(
                'group', 'exists', '--name', $ResourceGroup, '--only-show-errors', '-o', 'tsv') + $SubscriptionArgs)
        $rgExists = ($rg.ExitCode -eq 0 -and $rg.Output.Trim() -eq 'true')
        if ($rgExists) {
            Add-Result -Check 'resource group exists' -Status 'PASS' -Detail "$ResourceGroup in $($Sub.Name)"
        }
        else {
            Add-Result -Check 'resource group exists' -Status 'SKIP' -Detail "$ResourceGroup not found in subscription $($Sub.Name) ($($Sub.Id))"
        }

        $hasExtension = $false
        if ($rgExists) {
            # Checked explicitly: invoking a containerapp command without the
            # extension makes az prompt to install it, which would hang here.
            $ext = Invoke-Native -FilePath $AzCli -CommandArgs @('extension', 'show', '--name', 'containerapp', '--only-show-errors', '-o', 'json')
            $hasExtension = ($ext.ExitCode -eq 0)
        }

        if (-not $rgExists) {
            $noRg = "no resource group $ResourceGroup in subscription $($Sub.Name)"
            Add-Result -Check 'deployed API /healthz' -Status 'SKIP' -Detail $noRg
            Add-Result -Check 'sync job exists' -Status 'SKIP' -Detail $noRg
            Add-Result -Check 'sync job last run' -Status 'SKIP' -Detail $noRg
        }
        elseif (-not $hasExtension) {
            $hint = 'containerapp extension missing - run: az extension add --name containerapp'
            Add-Result -Check 'deployed API /healthz' -Status 'SKIP' -Detail $hint
            Add-Result -Check 'sync job exists' -Status 'SKIP' -Detail $hint
            Add-Result -Check 'sync job last run' -Status 'SKIP' -Detail $hint
        }
        else {
            $fqdnResult = Invoke-Native -FilePath $AzCli -CommandArgs (@(
                    'containerapp', 'show', '--resource-group', $ResourceGroup, '--name', $ApiName,
                    '--query', 'properties.configuration.ingress.fqdn', '--only-show-errors', '-o', 'tsv') + $SubscriptionArgs)
            $fqdn = $fqdnResult.Output.Trim()

            if ($fqdnResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($fqdn)) {
                Add-Result -Check 'deployed API /healthz' -Status 'SKIP' -Detail "container app $ApiName not found in $ResourceGroup"
            }
            else {
                # minReplicas is 0, so the first request pays a cold start.
                Write-Host "  probing https://$fqdn/healthz (cold start can take ~30s) ..." -ForegroundColor DarkGray
                $cloud = Wait-HttpEndpoint -Url "https://$fqdn/healthz" -Attempts 6 -DelaySeconds 5 -TimeoutSec 30
                if ($cloud.Ok) {
                    Add-Result -Check 'deployed API /healthz' -Status 'PASS' -Detail "$fqdn -> $($cloud.Content)"
                }
                else {
                    Add-Result -Check 'deployed API /healthz' -Status 'FAIL' -Detail "https://$fqdn/healthz did not return 200: $($cloud.Content)"
                }
            }

            $job = Invoke-Native -FilePath $AzCli -CommandArgs (@(
                    'containerapp', 'job', 'show', '--resource-group', $ResourceGroup, '--name', $JobName,
                    '--query', 'properties.configuration.triggerType', '--only-show-errors', '-o', 'tsv') + $SubscriptionArgs)
            $trigger = $job.Output.Trim()

            if ($job.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($trigger)) {
                Add-Result -Check 'sync job exists' -Status 'SKIP' -Detail "job $JobName not found in $ResourceGroup"
                Add-Result -Check 'sync job last run' -Status 'SKIP' -Detail 'no job to inspect'
            }
            else {
                if ($trigger -eq 'Schedule') {
                    Add-Result -Check 'sync job exists' -Status 'PASS' -Detail "$JobName triggerType=Schedule"
                }
                else {
                    Add-Result -Check 'sync job exists' -Status 'FAIL' -Detail "$JobName triggerType=$trigger (expected Schedule)"
                }

                $executions = Invoke-Native -FilePath $AzCli -CommandArgs (@(
                        'containerapp', 'job', 'execution', 'list', '--resource-group', $ResourceGroup,
                        '--name', $JobName, '--only-show-errors', '-o', 'json') + $SubscriptionArgs)
                if ($executions.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($executions.Output)) {
                    Add-Result -Check 'sync job last run' -Status 'SKIP' -Detail 'could not list job executions'
                }
                else {
                    $runs = $null
                    try {
                        $runs = $executions.Output | ConvertFrom-Json
                    }
                    catch {
                        $runs = $null
                    }
                    $runList = @($runs)
                    if ($runList.Count -eq 0) {
                        Add-Result -Check 'sync job last run' -Status 'SKIP' -Detail 'the job has never run - az containerapp job start'
                    }
                    else {
                        $latest = $runList | Sort-Object -Property { $_.properties.startTime } -Descending | Select-Object -First 1
                        $state = $latest.properties.status
                        $when = $latest.properties.startTime
                        if ($state -eq 'Succeeded') {
                            Add-Result -Check 'sync job last run' -Status 'PASS' -Detail "$state at $when"
                        }
                        elseif ($state -eq 'Running' -or $state -eq 'Processing') {
                            Add-Result -Check 'sync job last run' -Status 'WARN' -Detail "$state at $when (still in flight)"
                        }
                        else {
                            Add-Result -Check 'sync job last run' -Status 'FAIL' -Detail "$state at $when"
                        }
                    }
                }
            }
        }
    }
}
finally {
    Pop-Location
}

# ------------------------------------------------------------------ table --

$pass = @($Results | Where-Object { $_.Status -eq 'PASS' }).Count
$fail = @($Results | Where-Object { $_.Status -eq 'FAIL' }).Count
$warn = @($Results | Where-Object { $_.Status -eq 'WARN' }).Count
$skip = @($Results | Where-Object { $_.Status -eq 'SKIP' }).Count

$checkWidth = 4
foreach ($row in $Results) {
    if ($row.Check.Length -gt $checkWidth) { $checkWidth = $row.Check.Length }
}

Write-Host ''
Write-Host '================================================================' -ForegroundColor Cyan
Write-Host ' SUMMARY' -ForegroundColor Cyan
Write-Host '================================================================' -ForegroundColor Cyan
Write-Host (' {0}  {1}  {2}' -f 'STATUS'.PadRight(6), 'CHECK'.PadRight($checkWidth), 'DETAIL')
Write-Host (' {0}  {1}  {2}' -f ('-' * 6), ('-' * $checkWidth), ('-' * 30))

foreach ($row in $Results) {
    $color = 'Gray'
    if ($row.Status -eq 'PASS') { $color = 'Green' }
    if ($row.Status -eq 'FAIL') { $color = 'Red' }
    if ($row.Status -eq 'WARN') { $color = 'Yellow' }
    if ($row.Status -eq 'SKIP') { $color = 'DarkGray' }
    Write-Host (' {0}  {1}  {2}' -f $row.Status.PadRight(6), $row.Check.PadRight($checkWidth), $row.Detail) -ForegroundColor $color
}

Write-Host ''
Write-Host (' {0} passed, {1} failed, {2} warnings, {3} skipped' -f $pass, $fail, $warn, $skip)

if ($skip -gt 0) {
    Write-Host ' SKIP means NOT VERIFIED. It is not a pass.' -ForegroundColor DarkGray
}

if ($fail -gt 0) {
    Write-Host ' RESULT: FAIL' -ForegroundColor Red
    exit 1
}

Write-Host ' RESULT: OK (nothing failed)' -ForegroundColor Green
exit 0
