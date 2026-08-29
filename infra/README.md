# `infra/` — the cloud layer as code

Two Bicep files build the cloud half of OpsBridge365 into a single resource
group. Delete the group and it is gone; run the two commands below and **six of
the eight live resources** come back identically.

The other two — the action group `opsbridge-alerts` and the scheduled query rule
`opsbridge-sync-failed` — are in neither template and were created against the
workspace afterwards, so a rebuild silently restores the service **without its
failure alerting**. That gap is described at the bottom of this file, and it is
the one thing to remember before treating "delete and redeploy" as lossless.

| File | Who runs it | What it owns |
| --- | --- | --- |
| `bootstrap.bicep` | A human, **once** | Key Vault, the user-assigned managed identity, the `Key Vault Secrets User` role assignment, and the secret **values** |
| `main.bicep` | GitHub Actions, **on every push to `main`** | Log Analytics, the Container Apps environment, the sync Job and the API App. References the two resources above as `existing` |
| `main.parameters.example.json` | — | Placeholder parameter file for `main.bicep`. Copy, fill in, do not commit |

Both target scope: resource group. The resource group used throughout this
repository is **`rg-opsbridge365`**, in **`westus2`** — the same default as
`.github/workflows/deploy.yml` and every script in `scripts/`.

---

## Why it is two templates and not one

Everything in `bootstrap.bicep` needs either a privilege the routine deployment
identity deliberately does **not** have, or a secret value that deliberately does
**not** live in GitHub.

- **The role assignment is the main reason.**
  `Microsoft.Authorization/roleAssignments` cannot be created by Contributor. As
  long as that assignment lived in `main.bicep`, the GitHub deployment identity
  had to hold *Role Based Access Control Administrator* permanently, on every
  push, to create one assignment that only ever needs creating once. Splitting it
  out lets the routine identity drop to **Contributor and nothing more**.
- **The secret values are the second reason.** `main.bicep` used to take the
  Graph client secret as a `@secure()` parameter, so GitHub had to store it and
  hand it over on every deployment. The value is now supplied once by an
  operator; from then on `main.bicep` only names the secret. `GRAPH_CLIENT_SECRET`
  is no longer a GitHub secret at all.

Stated plainly, because it is the failure a reproducer will hit first:
**`bootstrap.bicep` is a prerequisite.** Deploying `main.bicep` into a resource
group that has not been bootstrapped fails at the `existing` lookups, before
anything is created. That is the intended, loud failure — the alternative is a
template that silently builds a second Key Vault.

The vault name is derived as `take('<namePrefix>kv<uniqueString(rg.id)>', 24)` in
**both** files, from the same `namePrefix`. The expression is duplicated on
purpose: a shared module would make the routine template depend on the bootstrap
one at compile time. If the two prefixes ever disagree, the `existing` lookup
fails rather than quietly creating something new.

---

## What gets created, and what it costs

Prices are East US list rates at time of writing and are here for the shape of
the bill, not as a quote — this deployment runs in `westus2`, and regional list
prices differ. The design goal is **$0 for an idle month**.

| # | Resource | Template | Type | What it is for | Cost |
| --- | --- | --- | --- | --- | --- |
| 1 | `opsbridge<hash>` | bootstrap | Key Vault (standard, **RBAC-enabled**, soft-delete on) | Holds exactly two secrets: `graph-client-secret` (the Entra app registration's client secret) and `metrics-api-token` (the bearer token `GET /metrics` requires). Nothing else. | **~$0** — $0.03 per 10,000 operations. Two containers reading two secrets a handful of times a day rounds to zero. |
| 2 | `opsbridge-id` | bootstrap | User-assigned managed identity | The single workload identity shared by the job and the app. Its only permission anywhere in Azure is *read secret values from vault #1*. | **$0** — managed identities are free. |
| 3 | `opsbridge-logs` | main | Log Analytics workspace (`PerGB2018`, 30-day retention) | Every container's stdout/stderr, plus job execution history. This is where you read a sync run that happened at 3am with the laptop closed. | **$0** — 5 GB/month ingestion is free, and this workload produces kilobytes. Beyond that, ~$2.76/GB. Retention of 30 days is inside the free 31-day window. |
| 4 | `opsbridge-env` | main | Container Apps managed environment | The runtime both containers live in — networking, log routing, revision management. | **$0** — the environment itself is not billed. You pay for replica seconds. |
| 5 | `opsbridge-sync` | main | Container Apps **Job** (`triggerType: Schedule`) | Runs `python -m app.sync` on the cron, writes Graph data into the SharePoint Assets list, exits. No scheduler in the image — the cron lives in Azure. | **$0 while idle.** Billed only for the seconds a replica is alive. A 30-second sync every 6 hours at 0.25 vCPU is 120 runs × 30 s × 0.25 vCPU = **~900 vCPU-seconds/month**, or 0.5% of the 180,000 vCPU-second free grant. |
| 6 | `opsbridge-api` | main | Container App (external ingress, **`minReplicas: 0`**) | Serves `/healthz`, `/demo/metrics` and `/metrics`. Wakes on an HTTP request, sleeps again when traffic stops. | **$0 while idle.** Scale-to-zero means no replica, no bill. The first request after a sleep pays the cold start — **20.2 s, measured 2026-08-29** against an app observed at zero replicas. |

The live resource group holds **8** resources, not 6 — measured 2026-08-29,
re-check with `az resource list -g rg-opsbridge365 -o table`. An action group
(`opsbridge-alerts`) and a scheduled query rule (`opsbridge-sync-failed`) were
created against the workspace afterwards and are **not** in either template. See
[`../evidence/monitoring/alerting.md`](../evidence/monitoring/alerting.md) — that
rule is also where the best failure write-up in this repository lives.

Free grant, per subscription per month: **180,000 vCPU-seconds and 360,000
GiB-seconds**, plus 2 million requests. Both workloads run at 0.25 vCPU /
0.5 GiB, so the realistic monthly draw is a rounding error against it — the sync
job's ~900 vCPU-seconds is 0.5% of the grant, and the API adds only cold starts.
The full arithmetic, and the assumptions the $0 depends on, are in
[`../docs/COST.md`](../docs/COST.md).

Set a budget alert anyway — expected spend is $0, but *knowing where the
guardrail is* is the point. `--start-date` and `--end-date` are required, and
`--category` and `--time-grain` take the lowercase values the CLI advertises
(`cost|usage`, `monthly|quarterly|annual`):

```powershell
az consumption budget create `
  --budget-name opsbridge-monthly-20 `
  --amount 20 `
  --category cost `
  --time-grain monthly `
  --start-date 2026-09-01 `
  --end-date 2027-09-01
```

---

## How secrets are handled

There are exactly two secrets in this system and they live in exactly one place.

| Secret name | What it is | Consumed as |
| --- | --- | --- |
| `graph-client-secret` | Client secret of the `opsbridge-graph` app registration | Container env `AZURE_CLIENT_SECRET` — the job **and** the app |
| `metrics-api-token` | Bearer token that authenticates `GET /metrics` | Container env `METRICS_API_TOKEN` — the **app only** |

The sync job has no HTTP surface, so it never receives the metrics token: giving
it one would hand a credential to a workload that cannot use it.

- Both are `@secure()` parameters of `bootstrap.bicep` — ARM will not log them,
  will not echo them into deployment history, and will not show them in
  `az deployment group show`.
- Both parameters are **optional and independent**, guarded by `if (!empty(...))`.
  An omitted parameter leaves the existing secret untouched rather than
  overwriting it with an empty string, so one can be rotated without touching the
  other.
- Neither value **ever appears in a container definition**. The job and the app
  declare a Key Vault *reference*:

  ```bicep
  secrets: [ { name: 'graph-client-secret', keyVaultUrl: '...', identity: <uami> } ]
  ```

  and consume it as `{ name: 'AZURE_CLIENT_SECRET', secretRef: 'graph-client-secret' }`.
  The value is fetched by the platform, using the managed identity, at replica
  start. Nothing plaintext is stored on the app resource.
- The URIs are **versionless**, so a rotated secret is picked up on the next
  revision restart rather than requiring a redeployment.
- The identity's access is a single `roleAssignment` of the built-in
  **Key Vault Secrets User** role (`4633458b-17de-408a-b874-0445c86b69e6`),
  **scoped to the vault** — not the resource group, not the subscription. It is
  created by `bootstrap.bicep` and by nothing else.
- **No secret is emitted as an output** by either template. The outputs are an
  FQDN, a vault *name*, a workspace id, a job name, an identity name and a
  principal id — all safe to print, which matters because deployment outputs are
  readable by anyone with resource-group access.
- The non-secret identifiers (Graph tenant id, client id, site id, list ids) are
  plain environment variables on purpose. They are public identifiers, not
  credentials; putting them in Key Vault would add operations and indirection
  without adding protection.
- No subscription id, tenant id, or secret value is hardcoded in either file.
  Every environment-specific value is a parameter.
- The vault's own `tenantId` is `subscription().tenantId` — the **Azure** tenant,
  deliberately not `graphTenantId`. The container env var `AZURE_TENANT_ID`, by
  contrast, is fed from `graphTenantId`: it is what MSAL turns into an authority,
  so it has to be the Microsoft 365 tenant. Two tenants, two values, two
  templates that never confuse them.

Rotation is two steps, because Container Apps resolves the secret at replica
start: write the new value with `bootstrap.bicep`, then restart the active
revision. The commands are in
[`../docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md).

---

## API versions used

All stable — no preview APIs in either template.

| Resource type | API version | Template |
| --- | --- | --- |
| `Microsoft.KeyVault/vaults`, `.../vaults/secrets` | `2023-07-01` | bootstrap (`existing` in main) |
| `Microsoft.ManagedIdentity/userAssignedIdentities` | `2023-01-31` | bootstrap (`existing` in main) |
| `Microsoft.Authorization/roleAssignments` | `2022-04-01` | bootstrap only |
| `Microsoft.OperationalInsights/workspaces` | `2023-09-01` | main |
| `Microsoft.App/managedEnvironments` | `2024-03-01` | main |
| `Microsoft.App/jobs` | `2024-03-01` | main |
| `Microsoft.App/containerApps` | `2024-03-01` | main |

`2024-03-01` is the recent stable GA version of the `Microsoft.App` resource
provider and is the first choice here deliberately: newer `2024-xx`/`2025-xx`
versions of Container Apps are preview, and preview API versions are not
something to pin portfolio infrastructure to. Every feature these templates need
— schedule-triggered jobs, Key Vault secret references bound to a user-assigned
identity, and scale-to-zero — is GA in `2024-03-01`.

---

## Deploy it

### Prerequisites

- Azure CLI logged in to the tenant that **owns the subscription**
  (`az login --tenant <azure-tenant-id>`). The Graph app registration lives in a
  different tenant and is referenced only by id, via `graphTenantId`; no login to
  it is needed to deploy
- For `bootstrap.bicep` only: rights to create a role assignment on the resource
  group (Owner, User Access Administrator, or Role Based Access Control
  Administrator). The routine identity that deploys `main.bicep` needs
  **Contributor** and nothing more
- The GHCR package marked **public** — a public image needs no registry
  credentials, which is why there is no `registries:` block in `main.bicep`
- Entra app registration with admin consent for exactly three Graph
  **application** permissions:
  - `User.Read.All` — read the user list, for device-to-owner matching
  - `Device.Read.All` — read directory device objects via `GET /devices`. This
    is the directory permission, not the Intune one; the code does not call
    Intune. See [`../docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md#graph-permissions--the-authoritative-list)
  - `Sites.Selected` — SharePoint access limited to the one provisioned site
    (`Sites.ReadWrite.All` would grant write on every site in the tenant;
    `Sites.Selected` is the stricter choice and the one specified)

### 1. Resource group

```powershell
az group create --name rg-opsbridge365 --location westus2
```

`westus2`, not `eastus`: the Azure for Students subscription this was built on
enforces an allowed-regions policy that refuses `eastus`. Neither template cares
— both take `location` from `resourceGroup().location`.

### 2. Bootstrap — once, by a human

```powershell
az deployment group create `
  --resource-group rg-opsbridge365 `
  --name opsbridge-bootstrap `
  --template-file infra/bootstrap.bicep `
  --parameters graphClientSecret=$env:GRAPH_CLIENT_SECRET metricsApiToken=$env:METRICS_API_TOKEN
```

Prefer a parameters file over the command line, so the secrets never reach `az`'s
argv where a concurrent `ps` can read them. Both parameters are optional: pass
only the one you are rotating. The full walkthrough, including when this has to
be re-run, is in [`../docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md).

### 3. Parameters for `main.bicep`

```powershell
Copy-Item infra/main.parameters.example.json infra/main.parameters.json
# edit main.parameters.json: containerImage, graphTenantId, clientId, site id, list ids
# graphTenantId is the Microsoft 365 tenant holding the Graph app registration -
# NOT the Azure tenant that owns the subscription you are deploying into
```

`main.bicep` declares exactly these parameters: `location`, `namePrefix`,
`containerImage`, `graphTenantId`, `clientId`, `sharePointSiteId`,
`assetsListId`, `ticketsListId`, `syncCron`. **There is no `clientSecret`
parameter any more** — if your copy of the parameter file still carries one from
an earlier revision, delete the entry, because ARM rejects a parameters file that
names a parameter the template does not declare.

`namePrefix` must match the value `bootstrap.bicep` was run with; the vault name
is derived from it. `main.parameters.json` (the filled-in copy) is gitignored.

### 4. Deploy

```powershell
az deployment group create `
  --resource-group rg-opsbridge365 `
  --name opsbridge-cloud `
  --template-file infra/main.bicep `
  --parameters infra/main.parameters.json
```

Bash equivalent:

```bash
az deployment group create \
  --resource-group rg-opsbridge365 \
  --name opsbridge-cloud \
  --template-file infra/main.bicep \
  --parameters infra/main.parameters.json
```

No secret is passed. Preview the change set first with `--what-if` appended to
either form.

### 5. Verify

```powershell
# The API URL, the vault, the job name
az deployment group show -g rg-opsbridge365 -n opsbridge-cloud --query properties.outputs

# Don't wait for the cron - run the sync now
az containerapp job start -g rg-opsbridge365 -n opsbridge-sync

# Watch it
az containerapp job execution list -g rg-opsbridge365 -n opsbridge-sync -o table

# Wake the API from zero. The first call after idle pays the cold start
# (20.2 s, measured 2026-08-29); the second was 248 ms.
#
# curl.exe, not curl: in Windows PowerShell 5.1 `curl` is an alias for
# Invoke-WebRequest, which rejects -H with "Cannot bind parameter 'Headers'".
curl.exe https://<apiFqdn>/healthz
curl.exe https://<apiFqdn>/demo/metrics

# /metrics needs the bearer token. Without one it returns 401, which is the
# behaviour to check for rather than a fault.
curl.exe -H "Authorization: Bearer $env:METRICS_API_TOKEN" https://<apiFqdn>/metrics
```

### Validate without deploying

```powershell
az bicep build --file infra/bootstrap.bicep     # compile only
az bicep build --file infra/main.bicep          # compile only
az deployment group validate -g rg-opsbridge365 --template-file infra/main.bicep --parameters ...
```

A clean `bicep build` proves the template, not the subscription. It returned zero
diagnostics before every one of the four deploy failures written up in
[`../docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md#things-that-will-bite-you).

---

## Notes and gotchas

- **Bootstrap first, always.** `main.bicep` resolves the Key Vault and the
  identity with `existing`. Without them the deployment fails immediately — no
  half-built resource group, but also no useful hint unless you know to look
  here.
- **RBAC propagation.** Azure RBAC can take a minute to take effect. Deploying
  `main.bicep` immediately after the bootstrap can fail resolving the Key Vault
  reference; re-run the identical command, which is idempotent, and the second
  run succeeds.
- **Secrets written by ARM.** Creating `Microsoft.KeyVault/vaults/secrets`
  through a deployment goes over the control plane, so it works even though the
  vault is RBAC-only and the operator holds no data-plane role. Reading them back
  from an operator shell does **not** work: `az keyvault secret list` returns
  `ForbiddenByRbac` (re-verified 2026-08-29). That is the control working.
- **Vault naming.** Key Vault names are globally unique and capped at 24
  characters, hence `take('<namePrefix>kv<uniqueString(rg.id)>', 24)`. It is
  stable for a given resource group and changes if you deploy to a new one —
  which means a new resource group needs its own bootstrap run.
- **Soft delete is 7 days**, the minimum. A purged-and-redeployed vault of the
  same name will collide until the retention window expires — and a purged vault
  takes its secrets with it, so rebuilding after a purge means running
  `bootstrap.bicep` again.
- **Cron is UTC**, standard 5-field syntax. The default `0 */6 * * *` is every
  six hours on the hour.
- **One image, two entrypoints.** The app runs the image's default CMD
  (uvicorn); the job overrides it with `python -m app.sync`. One build, one
  push, two workloads.
- **`replicaTimeout` is 1800s** with `replicaRetryLimit: 1`. A hung Graph call
  cannot bill for more than 30 minutes, and a failed sync waits for the next
  tick rather than retrying in a loop.
- **No container probes are declared.** Azure Container Apps does not honour the
  image's Docker `HEALTHCHECK`, and neither template defines a liveness,
  readiness or startup probe. Health gating in Azure is the deploy workflow's
  post-deploy `/healthz` check and the scheduled `health.yml` probe, not the
  platform restarting an unhealthy replica.
- **Public network access is enabled on the vault.** A private endpoint and a
  vault firewall are the production answer; neither is free, and both are listed
  as gaps rather than quietly omitted. See [`../docs/SECURITY.md`](../docs/SECURITY.md).
