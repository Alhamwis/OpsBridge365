<#
.SYNOPSIS
    Shared resolver for the subscription a script should act on. Dot-source it;
    do not run it.

.DESCRIPTION
    This project spans two tenants. The Azure resources live in one
    subscription, while the Graph and SharePoint work is done signed in to a
    different tenant that also owns a subscription. Signing in for Graph work
    silently changes the Azure CLI default, so "the current subscription" is not
    a safe answer to "where does rg-opsbridge365 live".

    A script that trusts the ambient default reports on whatever subscription
    happened to be selected last. That is how a verification run concluded
    "nothing is deployed yet" about a resource group that was deployed, healthy,
    and sitting in the other subscription.

    Resolve-AzSubscription answers the question properly:

      1. An explicit -SubscriptionId (or $env:AZURE_SUBSCRIPTION_ID) wins. It is
         validated against the current login rather than taken on trust.
      2. Otherwise every subscription this login can see is searched for the
         resource group, and the one that has it is used.
      3. Exactly one match  -> Resolved, and the caller is told which and why.
         No match           -> NotFound. Note the wording: not found in the N
                               subscriptions searched. That is a statement about
                               the search, not about the world. The group may
                               exist in a tenant this session is not logged in
                               to, and this function cannot know that.
         More than one      -> Ambiguous. Two groups with the same name in two
                               subscriptions is exactly the situation where
                               guessing is most expensive, so it refuses.

    Callers must pass the resolved Id to every subsequent az call with
    --subscription. Resolving once and then relying on the default again would
    reintroduce the same drift this function exists to remove.

.OUTPUTS
    PSCustomObject with:
      Status       Resolved | NotFound | Ambiguous | Error
      Id           subscription id, populated only when Status is Resolved
      Name         subscription display name, likewise
      Source       how it was chosen: -SubscriptionId, AZURE_SUBSCRIPTION_ID,
                   or search
      Detail       one line fit to print in a report
      Searched     how many subscriptions were probed
      Unqueryable  how many could not be probed (expired tenant token, no
                   permission); counted so a zero-match result is never
                   over-stated
      Candidates   the matching subscriptions when Status is Ambiguous

.EXAMPLE
    . (Join-Path $PSScriptRoot 'Resolve-AzSubscription.ps1')
    $sub = Resolve-AzSubscription -AzCli $azCli -ResourceGroup 'rg-opsbridge365'
    if ($sub.Status -eq 'Resolved') { & $azCli group show -n rg --subscription $sub.Id }
#>

function Invoke-AzCapture {
    <#
        Deliberately not named Invoke-Native: the scripts that dot-source this
        file define their own Invoke-Native, and a name collision would make
        which implementation runs depend on dot-source order.

        Exit code comes from $LASTEXITCODE, never $? - in PowerShell 5.1 a
        native command that merely writes to stderr flips $? to false.
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

function Resolve-AzSubscription {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $AzCli,
        [Parameter(Mandatory)][string] $ResourceGroup,
        [string] $SubscriptionId
    )

    $result = [pscustomobject]@{
        Status      = 'Error'
        Id          = ''
        Name        = ''
        Source      = ''
        Detail      = ''
        Searched    = 0
        Unqueryable = 0
        Candidates  = @()
    }

    # ------------------------------------------------- explicit selection ----
    $source = ''
    if (-not [string]::IsNullOrWhiteSpace($SubscriptionId)) {
        $source = '-SubscriptionId'
    }
    elseif (-not [string]::IsNullOrWhiteSpace($env:AZURE_SUBSCRIPTION_ID)) {
        $SubscriptionId = $env:AZURE_SUBSCRIPTION_ID
        $source = 'AZURE_SUBSCRIPTION_ID'
    }

    if (-not [string]::IsNullOrWhiteSpace($SubscriptionId)) {
        $result.Source = $source
        # az accepts an id or a display name here. Whichever was given, the id
        # that comes back is the canonical one to pass on.
        $show = Invoke-AzCapture -FilePath $AzCli -CommandArgs @(
            'account', 'show', '--subscription', $SubscriptionId, '--only-show-errors', '-o', 'json')
        if ($show.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($show.Output)) {
            $result.Detail = "subscription '$SubscriptionId' (from $source) is not one this login can see - list them with: az account list -o table"
            return $result
        }
        $parsed = $null
        try {
            $parsed = $show.Output | ConvertFrom-Json
        }
        catch {
            $parsed = $null
        }
        if ($null -eq $parsed -or [string]::IsNullOrWhiteSpace([string]$parsed.id)) {
            $result.Detail = "az account show --subscription $SubscriptionId returned output this script could not parse"
            return $result
        }
        $result.Status = 'Resolved'
        $result.Id = [string]$parsed.id
        $result.Name = [string]$parsed.name
        $result.Detail = ("{0} ({1}) from {2}" -f $result.Name, $result.Id, $source)
        return $result
    }

    # ------------------------------------------------------------ search ----
    $result.Source = 'search'

    # --all also returns subscriptions from other tenants this profile knows
    # about, which is the whole point here. Older CLIs reject the flag, so fall
    # back rather than treat it as a hard failure.
    $list = Invoke-AzCapture -FilePath $AzCli -CommandArgs @(
        'account', 'list', '--all', '--only-show-errors', '-o', 'json')
    if ($list.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($list.Output)) {
        $list = Invoke-AzCapture -FilePath $AzCli -CommandArgs @(
            'account', 'list', '--only-show-errors', '-o', 'json')
    }
    if ($list.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($list.Output)) {
        $result.Detail = 'az account list failed, so no subscription could be searched - run: az login'
        return $result
    }

    # PowerShell 5.1 trap: ConvertFrom-Json hands a JSON array to the pipeline as
    # ONE object, so `@($json | ConvertFrom-Json)` yields a single element that is
    # itself the array. Every property access then returns a collection - $sub.state
    # comes back as @('Enabled','Enabled'), which is never -eq 'Enabled', so every
    # subscription gets skipped and the search reports "0 subscriptions searched"
    # while sitting on two perfectly good ones. Unroll explicitly instead.
    $subscriptions = $null
    try {
        $parsed = ConvertFrom-Json -InputObject $list.Output
        if ($null -eq $parsed)              { $subscriptions = @() }
        elseif ($parsed -is [System.Array]) { $subscriptions = $parsed }
        else                                { $subscriptions = @($parsed) }
    }
    catch {
        $subscriptions = $null
    }
    if ($null -eq $subscriptions -or $subscriptions.Count -eq 0) {
        $result.Detail = 'az account list returned no subscriptions for this login'
        return $result
    }

    $hits = New-Object System.Collections.ArrayList
    foreach ($subscription in $subscriptions) {
        $id = [string]$subscription.id
        if ([string]::IsNullOrWhiteSpace($id)) { continue }
        # A disabled subscription cannot hold a live deployment and probing it
        # only produces noise.
        $state = [string]$subscription.state
        if (-not [string]::IsNullOrWhiteSpace($state) -and $state -ne 'Enabled') { continue }

        $result.Searched = $result.Searched + 1
        $probe = Invoke-AzCapture -FilePath $AzCli -CommandArgs @(
            'group', 'exists', '--name', $ResourceGroup, '--subscription', $id, '--only-show-errors', '-o', 'tsv')
        if ($probe.ExitCode -ne 0) {
            # Usually an expired token for that subscription's tenant. Counted,
            # not silently folded into "no".
            $result.Unqueryable = $result.Unqueryable + 1
            continue
        }
        if ($probe.Output.Trim() -eq 'true') {
            $null = $hits.Add([pscustomobject]@{
                    Id   = $id
                    Name = [string]$subscription.name
                })
        }
    }

    $unqueryableNote = ''
    if ($result.Unqueryable -gt 0) {
        $unqueryableNote = (" ({0} could not be queried - re-run az login for those tenants)" -f $result.Unqueryable)
    }

    if ($hits.Count -eq 1) {
        $result.Status = 'Resolved'
        $result.Id = $hits[0].Id
        $result.Name = $hits[0].Name
        $result.Detail = ("{0} ({1}) - the only one of {2} subscriptions searched that holds {3}{4}" -f
            $result.Name, $result.Id, $result.Searched, $ResourceGroup, $unqueryableNote)
        return $result
    }

    if ($hits.Count -eq 0) {
        $result.Status = 'NotFound'
        $result.Detail = ("resource group {0} not found in any of the {1} subscriptions you are logged into{2}" -f
            $ResourceGroup, $result.Searched, $unqueryableNote)
        return $result
    }

    $result.Status = 'Ambiguous'
    $result.Candidates = @($hits)
    $names = @($hits | ForEach-Object { "{0} ({1})" -f $_.Name, $_.Id })
    $result.Detail = ("resource group {0} exists in {1} subscriptions: {2} - pass -SubscriptionId to choose one" -f
        $ResourceGroup, $hits.Count, ($names -join ' and '))
    return $result
}
