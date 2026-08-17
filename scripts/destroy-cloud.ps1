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
    'opsbridge365-rg'.

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
    powershell -NoProfile -File scripts/destroy-cloud.ps1 -ResourceGroup opsbridge365-rg -ConfirmResourceGroup opsbridge365-rg
#>

[CmdletBinding()]
param(
    [string] $ResourceGroup,
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

if ([string]::IsNullOrWhiteSpace($ResourceGroup)) {
    if (-not [string]::IsNullOrWhiteSpace($env:AZURE_RESOURCE_GROUP)) {
        $ResourceGroup = $env:AZURE_RESOURCE_GROUP
    }
    else {
        $ResourceGroup = 'opsbridge365-rg'
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

$subscriptionName = '(unknown)'
$subscriptionId = '(unknown)'
try {
    $parsedAccount = $account.Output | ConvertFrom-Json
    $subscriptionName = $parsedAccount.name
    $subscriptionId = $parsedAccount.id
}
catch {
    Stop-WithReason -Message 'az account show returned output this script could not parse.' -Remedy @(
        'Run: az account show')
}

Write-Host " subscription          : $subscriptionName"
Write-Host " subscription id       : $subscriptionId"

$rgCheck = Invoke-Native -FilePath $AzCli -CommandArgs @('group', 'exists', '--name', $ResourceGroup, '--only-show-errors', '-o', 'tsv')
if ($rgCheck.ExitCode -ne 0) {
    Stop-WithReason -Message "could not query resource group $ResourceGroup." -Remedy @(
        "Run: az group exists --name $ResourceGroup")
}
if ($rgCheck.Output.Trim() -ne 'true') {
    Write-Host ''
    Write-Host "Resource group '$ResourceGroup' does not exist in this subscription." -ForegroundColor Green
    Write-Host 'There is nothing to delete, and nothing is being billed for it.' -ForegroundColor Green
    exit 0
}

# ------------------------------------------------- what would be deleted ----

Write-Host ''
Write-Host 'The following will be PERMANENTLY DELETED:' -ForegroundColor Yellow
Write-Host ''

$resources = Invoke-Native -FilePath $AzCli -CommandArgs @(
    'resource', 'list', '--resource-group', $ResourceGroup, '--only-show-errors', '-o', 'json')

$resourceList = @()
if ($resources.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($resources.Output)) {
    try {
        $resourceList = @($resources.Output | ConvertFrom-Json)
    }
    catch {
        $resourceList = @()
    }
}

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
    Write-Host "Re-run with: -ConfirmResourceGroup $ResourceGroup" -ForegroundColor Yellow
    exit 0
}

# ----------------------------------------------------------- confirmation --

Write-Host ''
Write-Host 'This cannot be undone.' -ForegroundColor Red

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
            "Re-run with: -ConfirmResourceGroup $ResourceGroup")
    }

    Write-Host ''
    Write-Host "Type the resource group name to confirm deletion (expected: $ResourceGroup)" -ForegroundColor Yellow
    $ConfirmResourceGroup = Read-Host -Prompt 'Resource group name'
}

if ($ConfirmResourceGroup -cne $ResourceGroup) {
    Stop-WithReason -Message "confirmation did not match. Expected '$ResourceGroup', got '$ConfirmResourceGroup'." -Remedy @(
        'The name must match exactly, including case.',
        "Re-run with: -ConfirmResourceGroup $ResourceGroup")
}

# --------------------------------------------------------------- teardown --

Write-Host ''
Write-Host "Deleting resource group $ResourceGroup ..." -ForegroundColor Red

$deleteArgs = @('group', 'delete', '--name', $ResourceGroup, '--yes', '--only-show-errors')
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
    Write-Host "  az group show --name $ResourceGroup" -ForegroundColor Yellow
    Write-Host "  az resource list --resource-group $ResourceGroup -o table" -ForegroundColor Yellow
    exit 1
}

if ($NoWait) {
    Write-Host 'Delete accepted by Azure and running in the background.' -ForegroundColor Green
    Write-Host "Check progress with: az group exists --name $ResourceGroup" -ForegroundColor DarkGray
}
else {
    Write-Host "Resource group $ResourceGroup deleted." -ForegroundColor Green
}

# ------------------------------------------------------------ vault purge --

if ($PurgeKeyVault -and $vaultNames.Count -gt 0) {
    if ($NoWait) {
        Write-Host ''
        Write-Host 'Skipping the vault purge: -NoWait means the group delete is still' -ForegroundColor Yellow
        Write-Host 'in flight, and a vault cannot be purged until it is soft-deleted.' -ForegroundColor Yellow
        foreach ($vault in $vaultNames) {
            Write-Host "  Purge later with: az keyvault purge --name $vault" -ForegroundColor Yellow
        }
    }
    else {
        Write-Host ''
        foreach ($vault in $vaultNames) {
            Write-Host "Purging soft-deleted Key Vault $vault ..." -ForegroundColor Red
            $purge = Invoke-Native -FilePath $AzCli -CommandArgs @(
                'keyvault', 'purge', '--name', $vault, '--only-show-errors')
            if ($purge.ExitCode -eq 0) {
                Write-Host "  purged $vault" -ForegroundColor Green
            }
            else {
                Write-Host "  could not purge $vault (az exited $($purge.ExitCode))" -ForegroundColor Yellow
                Write-Host "  it stays recoverable until its retention window expires" -ForegroundColor DarkGray
                Write-Host "  retry with: az keyvault purge --name $vault" -ForegroundColor DarkGray
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
