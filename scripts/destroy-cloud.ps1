<#
.SYNOPSIS
    Tear down the OpsBridge365 Azure resources so the bill returns to zero.

.DESCRIPTION
    Deletes the whole resource group, which is the entire cloud layer: the
    Container Apps environment, the sync job, the API app, Key Vault, Log
    Analytics, and the managed identity. Nothing outside that group is touched,
    and nothing local is touched at all.

    This is destructive and irreversible, so it is deliberately hard to trigger
    by accident:

      1. It prints every resource that will be deleted BEFORE asking anything.
      2. It requires the resource group NAME to be typed back, exactly. A bare
         -Confirm is not accepted and there is no -Force shortcut.
      3. -WhatIf lists the resources and exits without deleting anything.

    Key Vault soft delete: deleting the group leaves the vault recoverable for
    its retention window (7 days in this template). A soft-deleted vault costs
    nothing but blocks recreating a vault with the same name. Pass -PurgeKeyVault
    to purge it as well, which makes the teardown genuinely complete.

.PARAMETER ResourceGroup
    Resource group to delete. Defaults to $env:AZURE_RESOURCE_GROUP, then
    'rg-opsbridge365'.

.PARAMETER SubscriptionId
    Subscription to delete it from. Defaults to $env:AZURE_SUBSCRIPTION_ID, and
    when neither is given every subscription this login can see is searched for
    the group.

    This project spans two tenants, and signing in to do Graph work silently
    changes the Azure CLI default subscription. Deleting from whichever
    subscription happened to be selected last is unrecoverable, so: the
    resolved subscription is named in the confirmation prompt, it is passed
    explicitly to every az call, and a resource group of this name found in
    more than one subscription stops the script instead of picking one.

.PARAMETER ConfirmResourceGroup
    The typed confirmation. Must equal -ResourceGroup exactly, or the script
    stops. If omitted, an interactive session prompts for it; a non-interactive
    one refuses.

.PARAMETER PurgeKeyVault
    Also purge the soft-deleted Key Vault after the group is gone.

.PARAMETER NoWait
    Return as soon as Azure accepts the delete instead of waiting for it to
    finish. Faster, but you do not see the teardown complete.

.PARAMETER WhatIf
    List what would be deleted and exit. Deletes nothing.

.EXAMPLE
    powershell -NoProfile -File scripts/destroy-cloud.ps1 -WhatIf

.EXAMPLE
    powershell -NoProfile -File scripts/destroy-cloud.ps1 -ResourceGroup rg-opsbridge365 -ConfirmResourceGroup rg-opsbridge365
#>

[CmdletBinding()]
param(
    [string] $ResourceGroup,
    [string] $SubscriptionId,
    [string] $ConfirmResourceGroup,
    [switch] $PurgeKeyVault,
    [switch] $NoWait,
    [switch] $WhatIf
)

Set-StrictMode -Version 1.0
$ErrorActionPreference = 'Continue'

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

# Appended to every az call once resolved. Nothing here relies on the ambient
# default subscription: a delete aimed at the wrong one cannot be undone.
$SubscriptionArgs = @()

if ([string]::IsNullOrWhiteSpace($ResourceGroup)) {
    if (-not [string]::IsNullOrWhiteSpace($env:AZURE_RESOURCE_GROUP)) {
        $ResourceGroup = $env:AZURE_RESOURCE_GROUP
    }
    else {
        $ResourceGroup = 'rg-opsbridge365'
    }
}

# ---------------------------------------------------------------- helpers --

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

function Stop-WithReason {
    param(
        [Parameter(Mandatory)][string] $Message,
        [string[]] $Remedy = @()
    )
    Write-Host ''
    Write-Host "STOPPING: $Message" -ForegroundColor Red
    foreach ($line in $Remedy) {
        Write-Host "  $line" -ForegroundColor Yellow
    }
    Write-Host ''
    Write-Host 'Nothing was deleted.' -ForegroundColor Yellow
    exit 1
}

# ------------------------------------------------------------------ header --

Write-Host ''
Write-Host '================================================================' -ForegroundColor Red
if ($WhatIf) {
    Write-Host ' OpsBridge365 - DESTROY  [WHAT-IF: preview only]' -ForegroundColor Red
}
else {
    Write-Host ' OpsBridge365 - DESTROY cloud resources' -ForegroundColor Red
}
Write-Host '================================================================' -ForegroundColor Red
Write-Host " target resource group : $ResourceGroup"

# ---------------------------------------------------------------- checks --

$AzCli = Resolve-AzCli
if ([string]::IsNullOrWhiteSpace($AzCli)) {
    Stop-WithReason -Message 'the Azure CLI (az) could not be resolved.' -Remedy @(
        (Get-AzCliNotFoundDetail))
}
Write-Host " azure cli             : $AzCli"

$account = Invoke-Native -FilePath $AzCli -CommandArgs @('account', 'show', '--only-show-errors', '-o', 'json')
if ($account.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($account.Output)) {
    Stop-WithReason -Message 'the Azure CLI is not logged in.' -Remedy @(
        'Run: az login --tenant <your-tenant-id>')
}

$defaultSubscriptionName = '(unknown)'
$defaultSubscriptionId = '(unknown)'
try {
    $parsedAccount = $account.Output | ConvertFrom-Json
    $defaultSubscriptionName = [string]$parsedAccount.name
    $defaultSubscriptionId = [string]$parsedAccount.id
}
catch {
    Stop-WithReason -Message 'az account show returned output this script could not parse.' -Remedy @(
        'Run: az account show')
}

# Shown only so the reader can see it is NOT what gets deleted from unless it
# also turns out to be the resolved one.
Write-Host " az default sub        : $defaultSubscriptionName ($defaultSubscriptionId)"

Write-Host ''
Write-Host "Looking for $ResourceGroup across the subscriptions this login can see ..." -ForegroundColor DarkGray
$sub = Resolve-AzSubscription -AzCli $AzCli -ResourceGroup $ResourceGroup -SubscriptionId $SubscriptionId

if ($sub.Status -eq 'Ambiguous') {
    Stop-WithReason -Message "the target subscription is ambiguous: $($sub.Detail)" -Remedy @(
        'A resource group with this name exists in more than one subscription.',
        'Deleting the wrong one cannot be undone, so nothing is guessed here.',
        'Re-run with: -SubscriptionId <id>')
}
if ($sub.Status -eq 'NotFound') {
    Write-Host ''
    Write-Host $sub.Detail -ForegroundColor Green
    Write-Host 'There is nothing here to delete, and nothing is being billed for it.' -ForegroundColor Green
    Write-Host 'If you expected to find it, this session may not be logged in to the' -ForegroundColor DarkGray
    Write-Host 'tenant that owns it: az login --tenant <tenant-id>' -ForegroundColor DarkGray
    exit 0
}
if ($sub.Status -ne 'Resolved') {
    Stop-WithReason -Message "the target subscription could not be resolved: $($sub.Detail)" -Remedy @(
        'Run: az account list -o table',
        'Then re-run with: -SubscriptionId <id>')
}

$TargetSubscriptionName = $sub.Name
$TargetSubscriptionId = $sub.Id
$SubscriptionArgs = @('--subscription', $TargetSubscriptionId)

Write-Host ''
Write-Host " deleting from sub     : $TargetSubscriptionName ($TargetSubscriptionId)" -ForegroundColor Yellow
Write-Host " chosen by             : $($sub.Source)" -ForegroundColor DarkGray

$rgCheck = Invoke-Native -FilePath $AzCli -CommandArgs (@(
        'group', 'exists', '--name', $ResourceGroup, '--only-show-errors', '-o', 'tsv') + $SubscriptionArgs)
if ($rgCheck.ExitCode -ne 0) {
    Stop-WithReason -Message "could not query resource group $ResourceGroup." -Remedy @(
        "Run: az group exists --name $ResourceGroup --subscription $TargetSubscriptionId")
}
if ($rgCheck.Output.Trim() -ne 'true') {
    Write-Host ''
    Write-Host "Resource group '$ResourceGroup' does not exist in subscription $TargetSubscriptionName ($TargetSubscriptionId)." -ForegroundColor Green
    Write-Host 'There is nothing to delete there, and nothing is being billed for it.' -ForegroundColor Green
    exit 0
}

# ------------------------------------------------- what would be deleted ----

Write-Host ''
Write-Host 'The following will be PERMANENTLY DELETED:' -ForegroundColor Yellow
Write-Host ''

$resources = Invoke-Native -FilePath $AzCli -CommandArgs (@(
        'resource', 'list', '--resource-group', $ResourceGroup, '--only-show-errors', '-o', 'json') + $SubscriptionArgs)

$resourceList = @()
if ($resources.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($resources.Output)) {
    try {
        # Same PowerShell 5.1 trap as Resolve-AzSubscription.ps1: ConvertFrom-Json
        # emits a JSON array as ONE pipeline object, so `@(... | ConvertFrom-Json)`
        # collapses N resources into a single element. Here that would print
        # "1 resource" - or an empty group - immediately before asking the operator
        # to confirm a permanent deletion. Unroll explicitly.
        $parsedResources = ConvertFrom-Json -InputObject $resources.Output
        if ($null -eq $parsedResources)              { $resourceList = @() }
        elseif ($parsedResources -is [System.Array]) { $resourceList = $parsedResources }
        else                                         { $resourceList = @($parsedResources) }
    }
    catch {
        $resourceList = @()
    }
}

Write-Host "  subscription  : $TargetSubscriptionName ($TargetSubscriptionId)" -ForegroundColor Yellow
Write-Host "  resource group: $ResourceGroup" -ForegroundColor Yellow
if ($resourceList.Count -eq 0) {
    Write-Host '    (the group is empty, or its contents could not be listed)' -ForegroundColor DarkGray
}
else {
    foreach ($resource in ($resourceList | Sort-Object -Property type)) {
        Write-Host ("    {0,-52} {1}" -f $resource.type, $resource.name) -ForegroundColor Yellow
    }
}

Write-Host ''
Write-Host ("  {0} resource(s) in total." -f $resourceList.Count) -ForegroundColor Yellow

# Vaults are soft-deleted rather than removed, so name them explicitly.
$vaultNames = @($resourceList | Where-Object { $_.type -eq 'Microsoft.KeyVault/vaults' } | ForEach-Object { $_.name })
if ($vaultNames.Count -gt 0) {
    Write-Host ''
    Write-Host '  Key Vault soft delete:' -ForegroundColor Yellow
    foreach ($vault in $vaultNames) {
        Write-Host "    $vault stays recoverable for its retention window (7 days)." -ForegroundColor Yellow
    }
    if ($PurgeKeyVault) {
        Write-Host '    -PurgeKeyVault was passed: the vault will also be PURGED (unrecoverable).' -ForegroundColor Red
    }
    else {
        Write-Host '    A soft-deleted vault costs nothing but blocks reusing its name.' -ForegroundColor DarkGray
        Write-Host '    Pass -PurgeKeyVault to remove it completely.' -ForegroundColor DarkGray
    }
}

Write-Host ''
Write-Host '  Nothing outside this resource group is touched. Local files, the git' -ForegroundColor DarkGray
Write-Host '  repo, the GHCR image, and the SharePoint lists all survive.' -ForegroundColor DarkGray

if ($WhatIf) {
    Write-Host ''
    Write-Host 'WHAT-IF complete. Nothing was deleted.' -ForegroundColor Yellow
    Write-Host "Re-run with: -SubscriptionId $TargetSubscriptionId -ConfirmResourceGroup $ResourceGroup" -ForegroundColor Yellow
    exit 0
}

# ----------------------------------------------------------- confirmation --

Write-Host ''
Write-Host 'This cannot be undone.' -ForegroundColor Red
Write-Host ("About to delete resource group '{0}' from subscription {1} ({2})." -f
    $ResourceGroup, $TargetSubscriptionName, $TargetSubscriptionId) -ForegroundColor Red

if ([string]::IsNullOrWhiteSpace($ConfirmResourceGroup)) {
    $interactive = $true
    try {
        $interactive = [Environment]::UserInteractive
    }
    catch {
        $interactive = $false
    }

    if (-not $interactive) {
        Stop-WithReason -Message 'no typed confirmation, and this session cannot prompt for one.' -Remedy @(
            "Re-run with: -SubscriptionId $TargetSubscriptionId -ConfirmResourceGroup $ResourceGroup")
    }

    Write-Host ''
    Write-Host ("Type the resource group name to confirm deleting it from {0} (expected: {1})" -f
        $TargetSubscriptionName, $ResourceGroup) -ForegroundColor Yellow
    $ConfirmResourceGroup = Read-Host -Prompt 'Resource group name'
}

if ($ConfirmResourceGroup -cne $ResourceGroup) {
    Stop-WithReason -Message "confirmation did not match. Expected '$ResourceGroup', got '$ConfirmResourceGroup'." -Remedy @(
        'The name must match exactly, including case.',
        "Re-run with: -SubscriptionId $TargetSubscriptionId -ConfirmResourceGroup $ResourceGroup")
}

# --------------------------------------------------------------- teardown --

Write-Host ''
Write-Host "Deleting resource group $ResourceGroup from $TargetSubscriptionName ..." -ForegroundColor Red

$deleteArgs = @('group', 'delete', '--name', $ResourceGroup, '--yes', '--only-show-errors') + $SubscriptionArgs
if ($NoWait) {
    $deleteArgs += '--no-wait'
}
else {
    Write-Host 'Waiting for Azure to finish. This usually takes a few minutes.' -ForegroundColor DarkGray
}

$deleted = Invoke-Native -FilePath $AzCli -CommandArgs $deleteArgs
if ($deleted.ExitCode -ne 0) {
    Write-Host ''
    Write-Host "DELETE FAILED (az exited $($deleted.ExitCode))." -ForegroundColor Red
    Write-Host 'The resource group may be partially deleted. Check with:' -ForegroundColor Yellow
    Write-Host "  az group show --name $ResourceGroup --subscription $TargetSubscriptionId" -ForegroundColor Yellow
    Write-Host "  az resource list --resource-group $ResourceGroup --subscription $TargetSubscriptionId -o table" -ForegroundColor Yellow
    exit 1
}

if ($NoWait) {
    Write-Host 'Delete accepted by Azure and running in the background.' -ForegroundColor Green
    Write-Host "Check progress with: az group exists --name $ResourceGroup --subscription $TargetSubscriptionId" -ForegroundColor DarkGray
}
else {
    Write-Host "Resource group $ResourceGroup deleted from $TargetSubscriptionName." -ForegroundColor Green
}

# ------------------------------------------------------------ vault purge --

if ($PurgeKeyVault -and $vaultNames.Count -gt 0) {
    if ($NoWait) {
        Write-Host ''
        Write-Host 'Skipping the vault purge: -NoWait means the group delete is still' -ForegroundColor Yellow
        Write-Host 'in flight, and a vault cannot be purged until it is soft-deleted.' -ForegroundColor Yellow
        foreach ($vault in $vaultNames) {
            Write-Host "  Purge later with: az keyvault purge --name $vault --subscription $TargetSubscriptionId" -ForegroundColor Yellow
        }
    }
    else {
        Write-Host ''
        foreach ($vault in $vaultNames) {
            Write-Host "Purging soft-deleted Key Vault $vault ..." -ForegroundColor Red
            $purge = Invoke-Native -FilePath $AzCli -CommandArgs (@(
                    'keyvault', 'purge', '--name', $vault, '--only-show-errors') + $SubscriptionArgs)
            if ($purge.ExitCode -eq 0) {
                Write-Host "  purged $vault" -ForegroundColor Green
            }
            else {
                Write-Host "  could not purge $vault (az exited $($purge.ExitCode))" -ForegroundColor Yellow
                Write-Host "  it stays recoverable until its retention window expires" -ForegroundColor DarkGray
                Write-Host "  retry with: az keyvault purge --name $vault --subscription $TargetSubscriptionId" -ForegroundColor DarkGray
            }
        }
    }
}

# ---------------------------------------------------------------- summary --

Write-Host ''
Write-Host '================================================================' -ForegroundColor Green
Write-Host ' TEARDOWN COMPLETE' -ForegroundColor Green
Write-Host '================================================================' -ForegroundColor Green
Write-Host "  $ResourceGroup and everything in it is gone."
Write-Host '  Ongoing Azure cost for this project: zero.'
Write-Host ''
Write-Host '  Confirm for yourself:'
Write-Host "    az group exists --name $ResourceGroup      # expect: false"
Write-Host '    az consumption usage list -o table         # expect: nothing new'
Write-Host ''
Write-Host '  Rebuild it whenever you want:'
Write-Host '    powershell -NoProfile -File scripts/deploy-opsbridge.ps1'
exit 0
