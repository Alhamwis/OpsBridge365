# `infra/` — the cloud layer as code

One Bicep file builds the entire cloud half of OpsBridge365 into a single
resource group. Delete the group and it is gone; run the one command below and
it is back, identically.

| File | Purpose |
| --- | --- |
| `main.bicep` | The whole deployment. Target scope: resource group. |
| `main.parameters.example.json` | Placeholder parameter file. Copy, fill in, do not commit. |

---

## What gets created, and what it costs

Prices are the East US list rate at time of writing and are here for the shape
of the bill, not as a quote. The design goal is **$0 for an idle month**.

| # | Resource | Type | What it is for | Cost |
| --- | --- | --- | --- | --- |
| 1 | `opsbridge-logs` | Log Analytics workspace (`PerGB2018`, 30-day retention) | Every container's stdout/stderr, plus job execution history. This is where you watch a sync run at 3am with the laptop closed. | **$0** — 5 GB/month ingestion is free, and this workload produces kilobytes. Beyond that, ~$2.76/GB. Retention of 30 days is inside the free 31-day window. |
| 2 | `opsbridge-env` | Container Apps managed environment | The runtime both containers live in — networking, log routing, revision management. | **$0** — the environment itself is not billed. You pay for replica seconds, not for the environment. |
| 3 | `opsbridge<hash>` | Key Vault (standard, **RBAC-enabled**, soft-delete on) | Holds exactly one secret, `graph-client-secret`: the Entra app registration's client secret. Nothing else. | **~$0** — $0.03 per 10,000 operations. Two containers reading one secret a handful of times a day rounds to zero. |
| 4 | `opsbridge-id` | User-assigned managed identity | The single workload identity shared by the job and the app. Its only permission anywhere in Azure is *read secret values from vault #3*. | **$0** — managed identities are free. |
| 5 | `opsbridge-sync` | Container Apps **Job** (`triggerType: Schedule`) | Runs `python -m app.sync` on the cron, writes Graph data into the SharePoint Assets list, exits. No scheduler in the image — the cron lives in Azure. | **$0 while idle.** Billed only for the seconds a replica is alive. A 30-second sync every 6 hours at 0.25 vCPU is ~30 vCPU-seconds/month against a 180,000 vCPU-second free grant. |
| 6 | `opsbridge-api` | Container App (external ingress, **`minReplicas: 0`**) | Serves `/healthz` and `/metrics`. Wakes on an HTTP request, sleeps again when traffic stops. | **$0 while idle.** Scale-to-zero means no replica, no bill. The first request after a sleep pays a few seconds of cold start — narrate that as the feature it is. |

Free grant, per subscription per month: **180,000 vCPU-seconds and 360,000
GiB-seconds**, plus 2 million requests. Both workloads run at 0.25 vCPU /
0.5 GiB, so the realistic monthly draw is a rounding error against it.

Set a budget alert anyway — expected spend is $0, but *knowing where the
guardrail is* is the point:

```powershell
az consumption budget create --budget-name opsbridge-guard --amount 20 --time-grain Monthly --category Cost
```

---

## How secrets are handled

There is exactly one secret in this system and it lives in exactly one place.

- `clientSecret` is a `@secure()` parameter — ARM will not log it, will not
  echo it into deployment history, and will not show it in
  `az deployment group show`.
- It is written to Key Vault as `graph-client-secret` and **never appears in a
  container definition**. The job and the app declare it as a Key Vault
  *reference*:

  ```bicep
  secrets: [ { name: 'graph-client-secret', keyVaultUrl: '...', identity: <uami> } ]
  ```

  and consume it as `{ name: 'AZURE_CLIENT_SECRET', secretRef: 'graph-client-secret' }`.
  The value is fetched by the platform, using the managed identity, at replica
  start. Nothing plaintext is stored on the app resource.
- The identity's access is a single `roleAssignment` of the built-in
  **Key Vault Secrets User** role (`4633458b-17de-408a-b874-0445c86b69e6`),
  **scoped to the vault** — not the resource group, not the subscription.
- **No secret is emitted as an output.** The outputs are an FQDN, a vault
  *name*, a workspace id, a job name and a principal id — all safe to print.
- The non-secret identifiers (tenant id, client id, site id, list ids) are plain
  environment variables on purpose. They are public identifiers, not
  credentials; putting them in Key Vault would add operations and indirection
  without adding protection.
- No subscription id, tenant id, or secret value is hardcoded anywhere in
  `main.bicep`. Every environment-specific value is a parameter.

`main.parameters.json` (the filled-in copy) is gitignored. The recommended
practice is to leave `clientSecret` out of the file entirely and pass it on the
command line, as shown below.

---

## API versions used

All stable — no preview APIs in this template.

| Resource type | API version |
| --- | --- |
| `Microsoft.OperationalInsights/workspaces` | `2023-09-01` |
| `Microsoft.ManagedIdentity/userAssignedIdentities` | `2023-01-31` |
| `Microsoft.KeyVault/vaults`, `.../vaults/secrets` | `2023-07-01` |
| `Microsoft.Authorization/roleAssignments` | `2022-04-01` |
| `Microsoft.App/managedEnvironments` | `2024-03-01` |
| `Microsoft.App/jobs` | `2024-03-01` |
| `Microsoft.App/containerApps` | `2024-03-01` |

`2024-03-01` is the recent stable GA version of the `Microsoft.App` resource
provider and is the first choice here deliberately: newer `2024-xx`/`2025-xx`
versions of Container Apps are preview, and preview API versions are not
something to pin portfolio infrastructure to. Every feature this template needs
— schedule-triggered jobs, Key Vault secret references bound to a user-assigned
identity, and scale-to-zero — is GA in `2024-03-01`.

---

## Deploy it

### Prerequisites

- Azure CLI logged in to the **trial tenant** (`az login --tenant <your-tenant>`)
- The GHCR package marked **public** — a public image needs no registry
  credentials, which is why there is no `registries:` block in the template
- Entra app registration with admin consent for `User.Read.All`,
  `Device.Read.All`, and `Sites.ReadWrite.All` (or `Sites.Selected` scoped to
  the one site — the stricter choice)

### 1. Resource group

```powershell
az group create --name opsbridge-rg --location eastus
```

### 2. Parameters

```powershell
Copy-Item infra/main.parameters.example.json infra/main.parameters.json
# edit main.parameters.json: containerImage, tenantId, clientId, site id, list ids
# leave clientSecret as "" - it is passed separately below
```

### 3. Deploy

```powershell
az deployment group create `
  --resource-group opsbridge-rg `
  --name opsbridge-cloud `
  --template-file infra/main.bicep `
  --parameters infra/main.parameters.json `
  --parameters clientSecret=$env:GRAPH_CLIENT_SECRET
```

Bash equivalent:

```bash
az deployment group create \
  --resource-group opsbridge-rg \
  --name opsbridge-cloud \
  --template-file infra/main.bicep \
  --parameters infra/main.parameters.json \
  --parameters clientSecret="$GRAPH_CLIENT_SECRET"
```

Preview the change set first with `--what-if` appended to either form.

### 4. Verify

```powershell
# The API URL, the vault, the job name
az deployment group show -g opsbridge-rg -n opsbridge-cloud --query properties.outputs

# Don't wait for the cron - run the sync now
az containerapp job start -g opsbridge-rg -n opsbridge-sync

# Watch it
az containerapp job execution list -g opsbridge-rg -n opsbridge-sync -o table

# Wake the API from zero (the first call is slow on purpose)
curl https://<apiFqdn>/healthz
curl https://<apiFqdn>/metrics
```

### Validate without deploying

```powershell
az bicep build --file infra/main.bicep    # compile only
az deployment group validate -g opsbridge-rg --template-file infra/main.bicep --parameters ...
```

---

## Notes and gotchas

- **Role assignment propagation.** The job and the app `dependsOn` the role
  assignment, so ordering is correct — but Azure RBAC can take a minute to
  propagate. If a first deployment fails resolving the Key Vault reference,
  re-run the exact same command; it is idempotent and the second run succeeds.
- **Secrets written by ARM.** Creating `Microsoft.KeyVault/vaults/secrets`
  through a deployment goes over the control plane, so it works even though the
  vault is RBAC-only and you may hold no data-plane role yourself.
- **Vault naming.** Key Vault names are globally unique and capped at 24
  characters, so the name is `take('<namePrefix>kv<uniqueString(rg.id)>', 24)`.
  It is stable for a given resource group and changes if you deploy to a new one.
- **Soft delete is 7 days**, the minimum. A purged-and-redeployed vault of the
  same name will collide until the retention window expires — relevant if you
  tear this down and rebuild it for a demo.
- **Cron is UTC**, standard 5-field syntax. The default `0 */6 * * *` is every
  six hours on the hour.
- **One image, two entrypoints.** The app runs the image's default CMD
  (uvicorn); the job overrides it with `python -m app.sync`. One build, one
  push, two workloads.
- **`replicaTimeout` is 1800s** with `replicaRetryLimit: 1`. A hung Graph call
  cannot bill for more than 30 minutes, and a failed sync waits for the next
  tick rather than retrying in a loop.
