<#
.SYNOPSIS
    One-command deploy of the OpsBridge365 cloud layer.

.DESCRIPTION
    Preflight -> resource group -> az deployment group create -> verify.

    It refuses to start anything it cannot finish. Every precondition (Azure
    CLI, an active login, a compiling Bicep file, a green test suite, the
    required configuration) is checked BEFORE the first resource is touched, so
    the failure mode is "nothing happened and here is why", never a half-built
    resource group.

    -WhatIf previews everything: preflight still runs, and the deployment is
    submitted with az's own --what-if so Azure reports the change set without
    applying it.

    This script handles NO secret at all. It used to read the Graph client
    secret from $env:GRAPH_CLIENT_SECRET and pass it to a @secure() Bicep
    parameter; infra/main.bicep no longer declares that parameter. The secret
    value is written to Key Vault once by infra/bootstrap.bicep, and the routine
    deployment only references the vault.

    So there is nothing here to leak: no secret is read, written to a file,
    echoed, logged, or placed on a command line.

.PARAMETER ResourceGroup
    Target resource group. Defaults to $env:AZURE_RESOURCE_GROUP, then
    'rg-opsbridge365' (the same default as .github/workflows/deploy.yml).

.PARAMETER SubscriptionId
    Subscription to deploy into. Defaults to $env:AZURE_SUBSCRIPTION_ID, and
    when neither is given the resource group is searched for across every
    subscription this login can see.

    This project spans two tenants, and signing in to do Graph work silently
    changes the Azure CLI default subscription. Deploying into whichever
    subscription happened to be selected last is not something to leave to
    chance, so the resolved subscription is printed and then passed explicitly
    to every az call below.

.PARAMETER Location
    Region used only if the resource group has to be created. Defaults to
    $env:AZURE_LOCATION, then 'eastus'.

.PARAMETER NamePrefix
    Bicep namePrefix. Also derives the app and job names. Default 'opsbridge'.

.PARAMETER ContainerImage
    Full image reference. Defaults to $env:OPSBRIDGE_IMAGE, then a ghcr.io
    reference derived from the git 'origin' remote.

.PARAMETER SyncCron
    Cron expression (UTC, 5 fields) for the sync job. Default '0 */6 * * *'.

.PARAMETER SkipTests
    Skip the pytest preflight gate. Use only when tests were just run.

.PARAMETER WhatIf
    Preview only. Nothing is created, updated, or deleted.

.EXAMPLE
    powershell -NoProfile -File scripts/deploy-opsbridge.ps1 -WhatIf

.EXAMPLE
    powershell -NoProfile -File scripts/deploy-opsbridge.ps1

.NOTES
    Required environment variables (identifiers, except the last one). All of
    them describe the MICROSOFT 365 tenant - the one holding the Graph app
    registration and the SharePoint site - not the Azure tenant that owns the
    subscription. The Azure side comes from the `az login` context, narrowed to
    a single subscription by -SubscriptionId or $env:AZURE_SUBSCRIPTION_ID.

      GRAPH_TENANT_ID       tenant the Graph app registration lives in; becomes
                            the Bicep graphTenantId parameter and, at runtime,
                            the container's MSAL authority. NOT the Azure tenant
      GRAPH_CLIENT_ID       app id of the runtime Graph app registration
      SHAREPOINT_SITE_ID    Graph id of the SharePoint site
      ASSETS_LIST_ID        Graph id of the Assets list
      TICKETS_LIST_ID       Graph id of the Tickets list

    GRAPH_CLIENT_SECRET is NOT in that list, and that is the point: the routine
    deployment never sees it. Set it only when running infra/bootstrap.bicep,
    which is a separate, deliberate act - see docs/DEPLOYMENT.md.

    This script makes no role assignments, and no longer needs the permission to
    make one: that moved to infra/bootstrap.bicep. If you add one anyway, use
    `az rest` against
    .../providers/Microsoft.Authorization/roleAssignments/{guid}?api-version=2022-04-01
    rather than `az role assignment`: that command group fails with
    (MissingSubscription) on some machines even when --subscription is passed
    explicitly, while the same operation over REST succeeds. See docs/DEPLOYMENT.md.
#>

[CmdletBinding()]
param(
    [string] $ResourceGroup,
    [string] $SubscriptionId,
    [string] $Location,
    [string] $NamePrefix = 'opsbridge',
    [string] $ContainerImage,
    [string] $SyncCron = '0 */6 * * *',
    [switch] $SkipTests,
    [switch] $WhatIf
)

Set-StrictMode -Version 1.0
$ErrorActionPreference = 'Continue'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$TemplateFile = Join-Path $RepoRoot 'infra/main.bicep'

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

$AzCli = ''
# Appended to every az call once resolved, so a deploy cannot start in one
# subscription and finish in another.
$SubscriptionArgs = @()

if ([string]::IsNullOrWhiteSpace($ResourceGroup)) {
    if (-not [string]::IsNullOrWhiteSpace($env:AZURE_RESOURCE_GROUP)) {
        $ResourceGroup = $env:AZURE_RESOURCE_GROUP
    }
    else {
        $ResourceGroup = 'rg-opsbridge365'
    }
}

if ([string]::IsNullOrWhiteSpace($Location)) {
    if (-not [string]::IsNullOrWhiteSpace($env:AZURE_LOCATION)) {
        $Location = $env:AZURE_LOCATION
    }
    else {
        $Location = 'eastus'
    }
}

$ApiName = "$NamePrefix-api"
$JobName = "$NamePrefix-sync"

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
}
catch {
    Write-Verbose "Could not raise TLS version: $($_.Exception.Message)"
}

# ---------------------------------------------------------------- helpers --

function Write-Step {
    param([Parameter(Mandatory)][string] $Message)
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([Parameter(Mandatory)][string] $Message)
    Write-Host "    OK   $Message" -ForegroundColor Green
}

function Write-Info {
    param([Parameter(Mandatory)][string] $Message)
    Write-Host "         $Message" -ForegroundColor DarkGray
}

function Stop-WithReason {
    <# Refuse loudly and exit non-zero. Nothing has been deployed at this point. #>
    param(
        [Parameter(Mandatory)][string] $Message,
        [string[]] $Remedy = @()
    )
    Write-Host ''
    Write-Host "REFUSING TO DEPLOY: $Message" -ForegroundColor Red
    foreach ($line in $Remedy) {
        Write-Host "  $line" -ForegroundColor Yellow
    }
    Write-Host ''
    Write-Host 'Nothing was created or changed.' -ForegroundColor Yellow
    exit 1
}

function Test-CommandExists {
    param([Parameter(Mandatory)][string] $Name)
    $found = Get-Command -Name $Name -ErrorAction Ignore
    return ($null -ne $found)
}

function Invoke-Native {
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
        [int] $TimeoutSec = 30
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
        return [pscustomobject]@{ Ok = $false; Code = 0; Content = $_.Exception.Message }
    }
}

function Get-DefaultContainerImage {
    <#
        ghcr.io/<owner>/<repo>:latest derived from the origin remote, matching
        what .github/workflows/deploy.yml pushes. GHCR rejects uppercase path
        segments, so the whole reference is lowercased.
    #>
    if (-not (Test-CommandExists 'git')) { return '' }
    $remote = Invoke-Native -FilePath 'git' -CommandArgs @('remote', 'get-url', 'origin')
    if ($remote.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($remote.Output)) { return '' }

    $url = $remote.Output.Trim()
    if ($url -match 'github\.com[:/](?<owner>[^/]+)/(?<repo>[^/]+?)(\.git)?$') {
        $owner = $Matches['owner']
        $repo = $Matches['repo']
        return ("ghcr.io/$owner/$repo" + ':latest').ToLowerInvariant()
    }
    return ''
}

# ------------------------------------------------------------------ header --

Write-Host ''
Write-Host '================================================================' -ForegroundColor Cyan
if ($WhatIf) {
    Write-Host ' OpsBridge365 - deploy  [WHAT-IF: preview only]' -ForegroundColor Cyan
}
else {
    Write-Host ' OpsBridge365 - deploy' -ForegroundColor Cyan
}
Write-Host '================================================================' -ForegroundColor Cyan

Push-Location $RepoRoot
try {

    # --------------------------------------------------------- preflight ----
    Write-Step 'Preflight 1/7 - Azure CLI present'
    $AzCli = Resolve-AzCli
    if ([string]::IsNullOrWhiteSpace($AzCli)) {
        Stop-WithReason -Message 'the Azure CLI (az) could not be resolved.' -Remedy @(
            (Get-AzCliNotFoundDetail),
            'Then: az login')
    }
    Write-Ok "az found: $AzCli"

    Write-Step 'Preflight 2/7 - Azure login'
    $account = Invoke-Native -FilePath $AzCli -CommandArgs @('account', 'show', '--only-show-errors', '-o', 'json')
    if ($account.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($account.Output)) {
        Stop-WithReason -Message 'the Azure CLI is not logged in.' -Remedy @(
            'Run: az login --tenant <azure-tenant-id>   # the tenant owning the subscription,',
            '                                           # NOT $env:GRAPH_TENANT_ID',
            'Then: az account set --subscription <subscription-id>',
            'A deploy that starts without an authenticated session can only fail halfway.')
    }
    # Named "default" throughout: it is where az would go if nothing else were
    # said, which is not necessarily where this deploy belongs. Step 3 decides
    # that.
    $defaultSubscriptionName = '(unknown)'
    $defaultSubscriptionId = '(unknown)'
    try {
        $parsedAccount = $account.Output | ConvertFrom-Json
        $defaultSubscriptionName = [string]$parsedAccount.name
        $defaultSubscriptionId = [string]$parsedAccount.id
    }
    catch {
        Stop-WithReason -Message 'az account show returned output this script could not parse.' -Remedy @(
            'Run: az account show',
            'If that fails, re-authenticate with: az login')
    }
    Write-Ok "logged in - default subscription: $defaultSubscriptionName"
    Write-Info "default subscription id: $defaultSubscriptionId"

    Write-Step 'Preflight 3/7 - target subscription'
    Write-Info "looking for $ResourceGroup across the subscriptions this login can see ..."
    $sub = Resolve-AzSubscription -AzCli $AzCli -ResourceGroup $ResourceGroup -SubscriptionId $SubscriptionId

    $TargetSubscriptionId = ''
    $TargetSubscriptionName = ''
    if ($sub.Status -eq 'Resolved') {
        $TargetSubscriptionId = $sub.Id
        $TargetSubscriptionName = $sub.Name
        Write-Ok "deploying into: $($sub.Name) ($($sub.Id))"
        Write-Info $sub.Detail
    }
    elseif ($sub.Status -eq 'Ambiguous') {
        Stop-WithReason -Message "the target subscription is ambiguous: $($sub.Detail)" -Remedy @(
            'Two subscriptions hold a resource group with this name.',
            'Deploying into the wrong one would update the wrong environment.',
            "Re-run with: -SubscriptionId <id>")
    }
    elseif ($sub.Status -eq 'NotFound') {
        # A first deploy: nothing to match against, so the default is the only
        # reasonable choice - but it is stated, not assumed silently.
        $TargetSubscriptionId = $defaultSubscriptionId
        $TargetSubscriptionName = $defaultSubscriptionName
        Write-Info $sub.Detail
        Write-Ok "first deploy - $ResourceGroup will be CREATED in the default subscription: $defaultSubscriptionName ($defaultSubscriptionId)"
        Write-Info 'If that is the wrong subscription, stop now and re-run with -SubscriptionId <id>.'
    }
    else {
        Stop-WithReason -Message "the target subscription could not be resolved: $($sub.Detail)" -Remedy @(
            'Run: az account list -o table',
            'Then re-run with: -SubscriptionId <id>')
    }
    $SubscriptionArgs = @('--subscription', $TargetSubscriptionId)

    Write-Step 'Preflight 4/7 - Bicep compiles'
    if (-not (Test-Path $TemplateFile)) {
        Stop-WithReason -Message "template not found: $TemplateFile" -Remedy @(
            'Run this script from a full checkout of the repository.')
    }
    $bicep = Invoke-Native -FilePath $AzCli -CommandArgs @('bicep', 'build', '--file', $TemplateFile, '--stdout')
    if ($bicep.ExitCode -ne 0) {
        Stop-WithReason -Message 'infra/main.bicep does not compile.' -Remedy @(
            "Run: az bicep build --file $TemplateFile",
            'Fix the template before deploying.')
    }
    Write-Ok 'infra/main.bicep compiles'

    Write-Step 'Preflight 5/7 - tests'
    if ($SkipTests) {
        Write-Info 'skipped (-SkipTests)'
    }
    elseif (-not (Test-CommandExists 'python')) {
        Stop-WithReason -Message 'python is not on PATH, so the test gate cannot run.' -Remedy @(
            'Install Python 3.12+, or re-run with -SkipTests if you have just run the suite.')
    }
    else {
        Write-Info 'running python -m pytest -q ...'
        $pytest = Invoke-Native -FilePath 'python' -CommandArgs @('-m', 'pytest', '-q')
        if ($pytest.ExitCode -ne 0) {
            $lines = @($pytest.Output -split "`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
            $summary = ''
            if ($lines.Count -gt 0) { $summary = $lines[$lines.Count - 1].Trim() }
            Stop-WithReason -Message "the test suite is not green: $summary" -Remedy @(
                'Run: python -m pytest',
                'Deploying a red build to a live environment is how a demo breaks on stage.')
        }
        $passLines = @($pytest.Output -split "`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        $passSummary = ''
        if ($passLines.Count -gt 0) { $passSummary = $passLines[$passLines.Count - 1].Trim() }
        Write-Ok "pytest green - $passSummary"
    }

    Write-Step 'Preflight 6/7 - configuration'
    if ([string]::IsNullOrWhiteSpace($ContainerImage)) {
        if (-not [string]::IsNullOrWhiteSpace($env:OPSBRIDGE_IMAGE)) {
            $ContainerImage = $env:OPSBRIDGE_IMAGE
        }
        else {
            $ContainerImage = Get-DefaultContainerImage
        }
    }
    if ([string]::IsNullOrWhiteSpace($ContainerImage)) {
        Stop-WithReason -Message 'no container image reference.' -Remedy @(
            'Pass -ContainerImage ghcr.io/<owner>/opsbridge365:latest',
            'or set $env:OPSBRIDGE_IMAGE.')
    }

    # GRAPH_TENANT_ID, not AZURE_TENANT_ID: this value ends up as the container's
    # MSAL authority, so it must be the Microsoft 365 tenant where the Graph app
    # is registered. The Azure tenant is whatever `az login` established above and
    # is deliberately not read from the environment.
    $required = @(
        @{ Name = 'GRAPH_TENANT_ID'; Value = $env:GRAPH_TENANT_ID; Secret = $false },
        @{ Name = 'GRAPH_CLIENT_ID'; Value = $env:GRAPH_CLIENT_ID; Secret = $false },
        @{ Name = 'SHAREPOINT_SITE_ID'; Value = $env:SHAREPOINT_SITE_ID; Secret = $false },
        @{ Name = 'ASSETS_LIST_ID'; Value = $env:ASSETS_LIST_ID; Secret = $false },
        @{ Name = 'TICKETS_LIST_ID'; Value = $env:TICKETS_LIST_ID; Secret = $false }
        # GRAPH_CLIENT_SECRET is deliberately NOT required any more. The routine
        # deployment does not carry the secret at all: infra/bootstrap.bicep
        # writes it to Key Vault once, and main.bicep only references it. If you
        # are setting up a new environment, or rotating, run bootstrap.bicep -
        # see docs/DEPLOYMENT.md.
    )
    $missing = @()
    foreach ($entry in $required) {
        if ([string]::IsNullOrWhiteSpace($entry.Value)) {
            $missing += $entry.Name
        }
    }
    if ($missing.Count -gt 0) {
        Stop-WithReason -Message ("missing environment variable(s): " + ($missing -join ', ')) -Remedy @(
            'Set them in this shell, for example:',
            '  $env:GRAPH_TENANT_ID = "<microsoft-365-tenant-guid>"',
            '  $env:GRAPH_CLIENT_ID  = "<graph-app-client-id>"',
            'None of these is a secret - they are identifiers. The Graph client',
            'secret is not needed here at all; it lives in Key Vault, placed',
            'there once by infra/bootstrap.bicep.',
            'Never commit these anyway. The repo is public and .env is gitignored.',
            'The list ids come from: python scripts/provision_sharepoint.py')
    }
    foreach ($entry in $required) {
        if ($entry.Secret) {
            Write-Ok ("{0} = (set, {1} chars, never printed)" -f $entry.Name, $entry.Value.Length)
        }
        else {
            Write-Ok ("{0} = {1}" -f $entry.Name, $entry.Value)
        }
    }
    Write-Ok "containerImage = $ContainerImage"

    Write-Step 'Preflight 7/7 - plan'
    Write-Host "    subscription   : $TargetSubscriptionName ($TargetSubscriptionId)"
    Write-Host "    resource group : $ResourceGroup ($Location)"
    Write-Host "    template       : infra/main.bicep"
    Write-Host "    name prefix    : $NamePrefix"
    Write-Host "    sync cron      : $SyncCron (UTC)"
    Write-Host "    will ensure    : $ApiName (container app), $JobName (scheduled job),"
    Write-Host "                     Key Vault, Log Analytics, managed identity, environment"

    # ---------------------------------------------------- resource group ----
    Write-Step 'Resource group'
    $rgCheck = Invoke-Native -FilePath $AzCli -CommandArgs (@(
            'group', 'exists', '--name', $ResourceGroup, '--only-show-errors', '-o', 'tsv') + $SubscriptionArgs)
    $rgExists = ($rgCheck.ExitCode -eq 0 -and $rgCheck.Output.Trim() -eq 'true')

    if ($rgExists) {
        Write-Ok "$ResourceGroup already exists in $TargetSubscriptionName"
    }
    elseif ($WhatIf) {
        Write-Info "WHAT-IF: would create resource group $ResourceGroup in $Location ($TargetSubscriptionName)"
        Write-Info 'WHAT-IF: the deployment preview below cannot run without it, so it is skipped.'
        Write-Host ''
        Write-Host 'WHAT-IF complete. Nothing was created or changed.' -ForegroundColor Yellow
        Write-Host 'Re-run without -WhatIf to deploy.' -ForegroundColor Yellow
        exit 0
    }
    else {
        $created = Invoke-Native -FilePath $AzCli -CommandArgs (@(
                'group', 'create', '--name', $ResourceGroup, '--location', $Location,
                '--tags', 'project=OpsBridge365', 'managedBy=deploy-opsbridge.ps1',
                '--only-show-errors', '-o', 'none') + $SubscriptionArgs)
        if ($created.ExitCode -ne 0) {
            Stop-WithReason -Message "could not create resource group $ResourceGroup." -Remedy @(
                "Run: az group create --name $ResourceGroup --location $Location --subscription $TargetSubscriptionId",
                'Check that the signed-in identity has Contributor on the subscription.')
        }
        Write-Ok "created $ResourceGroup in $Location ($TargetSubscriptionName)"
    }

    # ---------------------------------------------------------- deployment --
    $deploymentName = "opsbridge-" + (Get-Date -Format 'yyyyMMdd-HHmmss')

    $deployArgs = @(
        'deployment', 'group', 'create',
        '--resource-group', $ResourceGroup,
        '--name', $deploymentName,
        '--template-file', $TemplateFile,
        '--parameters',
        "namePrefix=$NamePrefix",
        "containerImage=$ContainerImage",
        "graphTenantId=$($env:GRAPH_TENANT_ID)",
        "clientId=$($env:GRAPH_CLIENT_ID)",
        # No clientSecret. main.bicep no longer declares that parameter - the
        # secret VALUE is written once by infra/bootstrap.bicep and main.bicep
        # only references the Key Vault secret. Passing it here would fail with
        # "the following parameters were supplied but not declared".
        "sharePointSiteId=$($env:SHAREPOINT_SITE_ID)",
        "assetsListId=$($env:ASSETS_LIST_ID)",
        "ticketsListId=$($env:TICKETS_LIST_ID)",
        "syncCron=$SyncCron",
        '--only-show-errors'
    ) + $SubscriptionArgs

    if ($WhatIf) {
        Write-Step 'Deployment preview (az deployment group create --what-if)'
        Write-Info 'Azure reports the change set; nothing is applied.'
        # Printed, never the parameter values: the secret is in that array.
        Write-Info "az deployment group create --resource-group $ResourceGroup --subscription $TargetSubscriptionId --template-file infra/main.bicep --what-if"
        $preview = & $AzCli @deployArgs --what-if
        $previewCode = $LASTEXITCODE
        if ($null -ne $preview) {
            $preview | ForEach-Object { Write-Host "    $_" }
        }
        Write-Host ''
        if ($previewCode -ne 0) {
            Write-Host "WHAT-IF failed (az exited $previewCode). Nothing was created or changed." -ForegroundColor Red
            exit 1
        }
        Write-Host 'WHAT-IF complete. Nothing was created or changed.' -ForegroundColor Yellow
        Write-Host 'Re-run without -WhatIf to deploy.' -ForegroundColor Yellow
        exit 0
    }

    Write-Step "Deploying ($deploymentName)"
    Write-Info 'This takes a few minutes on a first run.'
    $deploy = Invoke-Native -FilePath $AzCli -CommandArgs ($deployArgs + @('--query', 'properties.outputs', '-o', 'json'))
    if ($deploy.ExitCode -ne 0) {
        Write-Host ''
        Write-Host "DEPLOYMENT FAILED (az exited $($deploy.ExitCode))." -ForegroundColor Red
        Write-Host 'Inspect it with:' -ForegroundColor Yellow
        Write-Host "  az deployment group show -g $ResourceGroup -n $deploymentName --subscription $TargetSubscriptionId" -ForegroundColor Yellow
        Write-Host "  az deployment operation group list -g $ResourceGroup -n $deploymentName --subscription $TargetSubscriptionId" -ForegroundColor Yellow
        Write-Host 'Role assignment propagation can lose a first run; the template is' -ForegroundColor Yellow
        Write-Host 'idempotent, so re-running the exact same command usually succeeds.' -ForegroundColor Yellow
        exit 1
    }
    Write-Ok 'deployment succeeded'

    $fqdn = ''
    try {
        $outputs = $deploy.Output | ConvertFrom-Json
        $fqdn = $outputs.apiFqdn.value
    }
    catch {
        Write-Info 'could not parse deployment outputs; falling back to az containerapp show'
    }

    # ------------------------------------------------------------- verify --
    Write-Step 'Post-deploy verification 1/2 - GET /healthz'
    if ([string]::IsNullOrWhiteSpace($fqdn)) {
        $fqdnResult = Invoke-Native -FilePath $AzCli -CommandArgs (@(
                'containerapp', 'show', '--resource-group', $ResourceGroup, '--name', $ApiName,
                '--query', 'properties.configuration.ingress.fqdn', '--only-show-errors', '-o', 'tsv') + $SubscriptionArgs)
        if ($fqdnResult.ExitCode -eq 0) {
            $fqdn = $fqdnResult.Output.Trim()
        }
    }

    $healthOk = $false
    if ([string]::IsNullOrWhiteSpace($fqdn)) {
        Write-Host '    FAIL could not determine the API FQDN' -ForegroundColor Red
    }
    else {
        $url = "https://$fqdn/healthz"
        Write-Info "probing $url - minReplicas is 0, so the first call pays a cold start"
        for ($attempt = 1; $attempt -le 12; $attempt++) {
            $probe = Test-HttpEndpoint -Url $url -TimeoutSec 30
            if ($probe.Ok) {
                $healthOk = $true
                Write-Ok "200 on attempt $attempt - $($probe.Content)"
                break
            }
            Write-Info "attempt $attempt : not ready yet, retrying in 10s"
            Start-Sleep -Seconds 10
        }
        if (-not $healthOk) {
            Write-Host "    FAIL $url never returned 200" -ForegroundColor Red
        }
    }

    Write-Step 'Post-deploy verification 2/2 - sync job'
    $jobOk = $false
    $job = Invoke-Native -FilePath $AzCli -CommandArgs (@(
            'containerapp', 'job', 'show', '--resource-group', $ResourceGroup, '--name', $JobName,
            '--query', 'properties.configuration.triggerType', '--only-show-errors', '-o', 'tsv') + $SubscriptionArgs)
    $trigger = $job.Output.Trim()
    if ($job.ExitCode -eq 0 -and $trigger -eq 'Schedule') {
        $jobOk = $true
        Write-Ok "$JobName exists, triggerType=Schedule, cron '$SyncCron' (UTC)"
    }
    else {
        Write-Host "    FAIL $JobName is missing or not schedule-triggered (got '$trigger')" -ForegroundColor Red
    }

    # ------------------------------------------------------------ summary --
    Write-Host ''
    Write-Host '================================================================' -ForegroundColor Cyan
    Write-Host ' RESULT' -ForegroundColor Cyan
    Write-Host '================================================================' -ForegroundColor Cyan
    Write-Host "  subscription   : $TargetSubscriptionName ($TargetSubscriptionId)"
    Write-Host "  resource group : $ResourceGroup"
    Write-Host "  deployment     : $deploymentName"
    if (-not [string]::IsNullOrWhiteSpace($fqdn)) {
        Write-Host "  api            : https://$fqdn/healthz"
        Write-Host "  metrics        : https://$fqdn/metrics"
    }
    Write-Host "  sync job       : $JobName"
    Write-Host ''
    Write-Host '  Run the sync now instead of waiting for the cron:'
    Write-Host "    az containerapp job start -g $ResourceGroup -n $JobName --subscription $TargetSubscriptionId"
    Write-Host '  Full report:'
    Write-Host "    powershell -NoProfile -File scripts/verify-opsbridge.ps1 -ResourceGroup $ResourceGroup -SubscriptionId $TargetSubscriptionId"
    Write-Host '  Tear it all down:'
    Write-Host "    powershell -NoProfile -File scripts/destroy-cloud.ps1 -ResourceGroup $ResourceGroup -SubscriptionId $TargetSubscriptionId"

    if ($healthOk -and $jobOk) {
        Write-Host ''
        Write-Host ' DEPLOYED AND VERIFIED' -ForegroundColor Green
        exit 0
    }

    Write-Host ''
    Write-Host ' DEPLOYED BUT NOT VERIFIED - see the FAIL lines above.' -ForegroundColor Red
    Write-Host ' The resources exist; something about them did not answer as expected.' -ForegroundColor Red
    exit 1
}
finally {
    Pop-Location
}
