# Azure deployment evidence

> **HISTORICAL EVIDENCE — captured 2026-08-18**, against commit `63c4616`,
> deployed by GitHub Actions run `32115509179`. These are point-in-time
> observations, not a live status page, and they have not been re-run. Two
> figures in §3 have since been corrected; the corrections are inline, next to
> what was recorded on the day. For current state and the command behind each
> claim, see [`../../docs/STATUS.md`](../../docs/STATUS.md).

Resource group **`rg-opsbridge365`**, region **`westus2`**. The deploy job
authenticated by OIDC federation, with no stored Azure credential anywhere in the
pipeline.

> Tenant ids, subscription ids, app ids, SharePoint site and list ids, and the
> Key Vault's random name suffix are omitted from this public repository. The API
> FQDN and the GHCR image reference are public by design and appear in full.
> Where a command's raw output would carry an omitted identifier, this file
> records the command and the result rather than pasting the transcript.

---

## 1. What existed in Azure on 2026-08-18

Every resource below was declared in `infra/main.bicep` and created by that
template, except the two monitoring resources at the bottom of the table, which
were created against the deployed workspace afterwards and were **not** in the
template.

| Resource | Name | Configuration observed |
| --- | --- | --- |
| Log Analytics workspace | `opsbridge-logs` | SKU `PerGB2018`, `retentionInDays: 30` |
| User-assigned managed identity | `opsbridge-id` | The only principal that could read the Graph secret |
| Key Vault | `opsbridge-kv-<suffix>` (suffix omitted) | RBAC-authorized, soft-delete enabled, `standard` SKU |
| Container Apps environment | `opsbridge-env` | Consumption, bound to `opsbridge-logs` |
| Container Apps **Job** | `opsbridge-sync` | `triggerType: Schedule`, cron `0 */6 * * *`, `replicaRetryLimit: 1`, `replicaTimeout: 1800` |
| Container **App** | `opsbridge-api` | HTTPS ingress, `minReplicas: 0`, `maxReplicas: 1`, cpu `0.25`, memory `0.5Gi` |
| Action group | `opsbridge-alerts` | Created post-deploy — see [`../monitoring/alerting.md`](../monitoring/alerting.md) |
| Scheduled query rule | `opsbridge-sync-failed` | Created post-deploy — severity 2, 5-minute evaluation, 15-minute window |

Both workloads ran the same image, `ghcr.io/alhamwis/opsbridge365`, deployed by
immutable commit-sha tag. The job overrides the image's `CMD` with
`python -m app.sync`; the API runs the default uvicorn command.

> **Since this capture — template ownership moved.** The Key Vault, the
> user-assigned identity and the identity's **Key Vault Secrets User** role
> assignment are now declared in `infra/bootstrap.bicep`, which a human runs
> once. `infra/main.bicep` references them as `existing` and creates only the
> routine resources: Log Analytics, the Container Apps environment, the Job and
> the App. The eight resources themselves are unchanged; which template owns them
> is not.

## 2. Deployment outputs — and what was deliberately absent

`az deployment group show ... --query properties.outputs` returned exactly five
keys:

```
apiFqdn
identityPrincipalId
jobName
keyVaultName
logAnalyticsWorkspaceId
```

**No secret was among them.** That matters because deployment outputs are
readable by anyone with resource-group access, so an output is effectively a
publication. The Graph client secret arrived as a `@secure()` parameter, landed
in Key Vault, and never left it — see
[`../security/posture.md`](../security/posture.md).

## 3. The API, as measured on 2026-08-18

```
https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io
```

| Check | Result recorded 2026-08-18 |
| --- | --- |
| `GET /healthz` | **HTTP 200** — `{"status":"ok","version":"0.1.0"}` |
| `GET /healthz` — **cold**, described as from zero replicas | **714 ms** — see the correction below |
| `GET /healthz` — warm | **143 ms** |
| `GET /metrics` | **HTTP 200**, computed from live SharePoint (below). The endpoint was public and unauthenticated at the time |
| `http://` (plain) | **HTTP 301** to `https://` — ingress `allowInsecure: false` |
| Replica count while idle | **0** |

```json
{"open_tickets":2,"due_within_30min":0,"sla_compliance_7d_pct":50.0,"resolved_last_7d":2,"sla_measured_last_7d":2}
```

That payload is the scale-to-zero argument in one line: it was served by a
replica that did not exist before the request and did not exist a few minutes
after it.

### Correction — the cold start figure was wrong

**Re-measured 2026-08-29**, with the replica count observed at **0** immediately
before the probe so the scale-from-zero was genuine:

| | 2026-08-18, as recorded | 2026-08-29, re-measured |
| --- | --- | --- |
| Cold, from zero replicas | 714 ms | **20.2 s** (a second run gave 21.3 s) |
| Warm | 143 ms | **248 ms** |

714 ms is not a plausible Container Apps scale-from-zero time, and it cannot have
been measured against a sleeping app — whatever it timed, the replica was already
up. The honest figure is **roughly 20 seconds**, and it is the number to quote.
Two consequences follow. The deploy job's ~3-minute `/healthz` retry window is a
necessity rather than a courtesy. And the price of `minReplicas: 0` is a
twenty-second first request, not a sub-second one — which is still the right
trade for this workload, but it is a different trade from the one this file
originally described.

### Correction — the `/metrics` payload has rolled, and the endpoint has closed

The 7-day window has moved past the two resolutions that were seeded before this
capture. As of **2026-08-29** the endpoint returns:

```json
{"open_tickets":2,"due_within_30min":0,"sla_compliance_7d_pct":null,"resolved_last_7d":0,"sla_measured_last_7d":0}
```

That is not a regression. On 2026-08-18, `sla_compliance_7d_pct: 50.0` with
`sla_measured_last_7d: 2` was the metric carrying its own sample size: one of two
measurable resolutions met its target, and a bare `50%` would have hidden that
the denominator was two. Today the denominator is zero and the field is `null` —
not 0%, not 100%. Both payloads make the same point, and the fact that the first
became the second without anyone touching the code is itself evidence that the
number is computed from the live list rather than pinned somewhere.

`/metrics` also no longer answers an anonymous caller. It requires
`Authorization: Bearer <token>` and returns **401** without one; the payload
above was read with a token. `GET /demo/metrics` is the public endpoint now, and
it serves explicitly synthetic data.

## 4. The sync job

`az containerapp job show -g rg-opsbridge365 -n opsbridge-sync` reported
`triggerType: Schedule` — which is the check the pipeline's own post-deploy
verification step makes, and which proves the job is cron-triggered rather than
merely accepted by ARM.

| Setting | Value | Why |
| --- | --- | --- |
| `triggerType` | `Schedule` | Run-and-exit, not an always-on container with a scheduler inside |
| `cronExpression` | `0 */6 * * *` | Four runs a day |
| `replicaRetryLimit` | `1` | A Graph outage cannot loop and burn free-grant seconds |
| `replicaTimeout` | `1800` | A hung call is killed at 30 minutes |

The job executed in the cloud on its own schedule. Cron was temporarily set to
`*/5`; an execution started at `09:05:00Z`, started by Azure's scheduler with no
human or local involvement, and succeeded. The production cron was then restored
and read back.

Its run and the SharePoint result are in
[`../sharepoint/reconciliation.md`](../sharepoint/reconciliation.md); the Log
Analytics capture of the same run is in
[`../monitoring/alerting.md`](../monitoring/alerting.md).

## 5. Two Azure constraints that shaped this deployment

Both were discovered by the deploy job failing against a real subscription, and
both are documented in full in
[`../../docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md#things-that-will-bite-you).

**`RequestDisallowedByAzure` — the region.** Azure for Students enforces an
allowed-regions policy. The permitted regions at the time were
`northcentralus`, `mexicocentral`, `westus2`, `westus`, `canadacentral`.
**`eastus` was not among them**, and `eastus` was the original default in the
workflow, the runbook and the resource group. The resource group was recreated in
**`westus2`**; nothing in the template changed, because the template already took
`location` from the resource group.

**`MissingSubscriptionRegistration` — the providers.** On a fresh subscription,
`Microsoft.App`, `Microsoft.KeyVault`, `Microsoft.OperationalInsights`,
`Microsoft.ManagedIdentity` and `Microsoft.Insights` were all unregistered. ARM
rejects a template that references an unregistered provider, and the error names
one provider at a time, so this surfaced as a sequence of failures rather than
one. All five were registered.

Neither is a template defect. Both are the kind of thing that only exists when
you deploy to a real subscription, which is why they are worth recording.
