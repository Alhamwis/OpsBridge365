# Azure deployment evidence

Resource group **`rg-opsbridge365`**, region **`westus2`**. Deployed by GitHub
Actions run **`32115509179`** — the `deploy to Azure` job — authenticated by OIDC
federation, with no stored Azure credential anywhere in the pipeline.

> Tenant ids, subscription ids, app ids, SharePoint site and list ids, and the
> Key Vault's random name suffix are omitted from this public repository. The API
> FQDN and the GHCR image reference are public by design and appear in full.
> Where a command's raw output would carry an omitted identifier, this file
> records the command and the result rather than pasting the transcript.

---

## 1. What exists in Azure

Every resource below is declared in `infra/main.bicep` and was created by that
template, except the two monitoring resources at the bottom of the table, which
were created against the deployed workspace afterwards and are **not** in the
template.

| Resource | Name | Configuration observed |
| --- | --- | --- |
| Log Analytics workspace | `opsbridge-logs` | SKU `PerGB2018`, `retentionInDays: 30` |
| User-assigned managed identity | `opsbridge-id` | The only principal that can read the Graph secret |
| Key Vault | `opsbridge-kv-<suffix>` (suffix omitted) | RBAC-authorized, soft-delete enabled, `standard` SKU |
| Container Apps environment | `opsbridge-env` | Consumption, bound to `opsbridge-logs` |
| Container Apps **Job** | `opsbridge-sync` | `triggerType: Schedule`, cron `0 */6 * * *`, `replicaRetryLimit: 1`, `replicaTimeout: 1800` |
| Container **App** | `opsbridge-api` | HTTPS ingress, `minReplicas: 0`, `maxReplicas: 1`, cpu `0.25`, memory `0.5Gi` |
| Action group | `opsbridge-alerts` | Created post-deploy — see [`../monitoring/alerting.md`](../monitoring/alerting.md) |
| Scheduled query rule | `opsbridge-sync-failed` | Created post-deploy — severity 2, 5-minute evaluation, 15-minute window |

Both workloads run the same image, `ghcr.io/alhamwis/opsbridge365`, deployed by
immutable commit-sha tag. The job overrides the image's `CMD` with
`python -m app.sync`; the API runs the default uvicorn command.

## 2. Deployment outputs — and what is deliberately absent

`az deployment group show ... --query properties.outputs` returns exactly five
keys:

```
apiFqdn
identityPrincipalId
jobName
keyVaultName
logAnalyticsWorkspaceId
```

**No secret is among them.** That matters because deployment outputs are readable
by anyone with resource-group access, so an output is effectively a publication.
The Graph client secret arrives as a `@secure()` parameter, lands in Key Vault,
and never leaves it — see [`../security/posture.md`](../security/posture.md).

## 3. The live API

```
https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io
```

| Check | Result |
| --- | --- |
| `GET /healthz` | **HTTP 200** — `{"status":"ok","version":"0.1.0"}` |
| `GET /healthz` — **cold**, from zero replicas | **714 ms** |
| `GET /healthz` — warm | **143 ms** |
| `GET /metrics` | **HTTP 200**, computed from live SharePoint (below) |
| `http://` (plain) | **HTTP 301** to `https://` — ingress `allowInsecure: false` |
| Replica count while idle | **0** |

```json
{"open_tickets":2,"due_within_30min":0,"sla_compliance_7d_pct":50.0,"resolved_last_7d":2,"sla_measured_last_7d":2}
```

That payload is the whole scale-to-zero argument in one line: it was served by a
replica that did not exist before the request and did not exist a few minutes
after it. **714 ms** is the price of `minReplicas: 0`, and it is the number to
quote rather than an estimate — it was measured against this deployment, not
derived from documentation.

`sla_compliance_7d_pct: 50.0` with `sla_measured_last_7d: 2` is the metric
carrying its own sample size: one of two measurable resolutions met its target.
A bare `50%` would hide that the denominator is two.

## 4. The sync job

`az containerapp job show -g rg-opsbridge365 -n opsbridge-sync` reports
`triggerType: Schedule` — which is the check the pipeline's own post-deploy
verification step makes, and which proves the job is cron-triggered rather than
merely accepted by ARM.

| Setting | Value | Why |
| --- | --- | --- |
| `triggerType` | `Schedule` | Run-and-exit, not an always-on container with a scheduler inside |
| `cronExpression` | `0 */6 * * *` | Four runs a day |
| `replicaRetryLimit` | `1` | A Graph outage cannot loop and burn free-grant seconds |
| `replicaTimeout` | `1800` | A hung call is killed at 30 minutes |

The job has executed in the cloud. Its run and the SharePoint result are in
[`../sharepoint/reconciliation.md`](../sharepoint/reconciliation.md); the Log
Analytics capture of the same run is in
[`../monitoring/alerting.md`](../monitoring/alerting.md).

## 5. Two Azure constraints that shaped this deployment

Both were discovered by the deploy job failing against a real subscription, and
both are documented in full in
[`../../docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md#things-that-will-bite-you).

**`RequestDisallowedByAzure` — the region.** Azure for Students enforces an
allowed-regions policy. The permitted regions are `northcentralus`,
`mexicocentral`, `westus2`, `westus`, `canadacentral`. **`eastus` is not among
them**, and `eastus` was the original default in the workflow, the runbook and
the resource group. The resource group was recreated in **`westus2`**; nothing in
the template changed, because the template already took `location` from the
resource group.

**`MissingSubscriptionRegistration` — the providers.** On a fresh subscription,
`Microsoft.App`, `Microsoft.KeyVault`, `Microsoft.OperationalInsights`,
`Microsoft.ManagedIdentity` and `Microsoft.Insights` were all unregistered. ARM
rejects a template that references an unregistered provider, and the error names
one provider at a time, so this surfaces as a sequence of failures rather than
one. All five were registered.

Neither is a template defect. Both are the kind of thing that only exists when
you deploy to a real subscription, which is why they are worth recording.
