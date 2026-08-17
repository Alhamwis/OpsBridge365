# Deployment

How OpsBridge365's cloud half gets from a `git push` to a running Container Apps
job and a scale-to-zero API — and what has to exist in GitHub and Azure first.

Two workflows:

| Workflow | Trigger | What it does | Token permissions |
|---|---|---|---|
| `.github/workflows/ci.yml` | pull request, manual | ruff + pytest | `contents: read` |
| `.github/workflows/deploy.yml` | push to `main`, manual | test → build/push to ghcr.io → deploy to Azure → verify | `contents: read`, `packages: write`, `id-token: write` |

`deploy.yml` runs three jobs in strict order. `test` gates `build-and-push`,
which gates `deploy`; `deploy` additionally requires `github.ref ==
'refs/heads/main'` and the `production` environment, so a manual dispatch from a
side branch builds an image but cannot deploy it.

---

## Two app registrations, and why

The system uses **two separate Entra app registrations**. They are not
interchangeable, and the split is the security design — not bookkeeping.

| | `opsbridge-deploy` | `opsbridge-graph` |
|---|---|---|
| Job | Deploy infrastructure to Azure | Call Microsoft Graph at runtime |
| Auth | OIDC federated credential | Client secret |
| Client secret? | **No. Never. Not one.** | Yes — the only stored password in the system |
| Azure RBAC | Contributor + RBAC-admin, **resource group scope only** | **None at all.** It never touches ARM |
| Graph app permissions | **None** | `User.Read.All`, `DeviceManagementManagedDevices.Read.All`, `Sites.Selected` |
| Used by | `azure/login@v2` | The containers, via Key Vault |
| Repo secrets | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` |

Both live in the same tenant, so `AZURE_TENANT_ID` is reused for the Bicep
`tenantId` parameter. That is fine: a tenant id is a public identifier, not a
credential.

> **Do not merge these into one app registration "for convenience."** One app
> would mean a long-lived Graph client secret attached to a principal that also
> holds ARM rights on the resource group. Leak that secret and the attacker gets
> the Azure resource group, not just Graph read access — and the mere existence
> of a stored credential on the deploying principal defeats the reason for using
> OIDC at all. Two apps means the credential that can leak (`opsbridge-graph`'s
> secret) has no ARM authority, and the identity with ARM authority
> (`opsbridge-deploy`) has no credential to leak.

### Azure authentication: OIDC only

The workflow authenticates to Azure with **workload identity federation**. There
is no client secret, no service-principal password, and no `creds:` JSON blob in
`deploy.yml`. GitHub mints a short-lived OIDC token scoped to this repository
(that is what `id-token: write` buys), `azure/login@v2` exchanges it for an
access token, and the token dies with the job.

`GRAPH_CLIENT_SECRET` is a secret, but it is *not* used to log in to Azure — it
belongs to the other app registration entirely. It is passed as a `@secure()`
Bicep parameter, lands in Key Vault, and is read at container start by the
user-assigned managed identity. The workflow never sees a password that grants
it access to the subscription.

### One-time setup — A. `opsbridge-deploy` (no secret, ever)

```bash
# 1. App registration (in YOUR tenant). Note: NO `az ad app credential reset`
#    anywhere below - this app must never have a client secret.
az ad app create --display-name opsbridge-deploy
DEPLOY_APP_ID=$(az ad app list --display-name opsbridge-deploy --query '[0].appId' -o tsv)
az ad sp create --id "$DEPLOY_APP_ID"

# 2. Federated credential: trust *this repo's main branch*, nothing else
cat > federated.json <<'JSON'
{
  "name": "github-main",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:OWNER/OpsBridge365:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}
JSON
az ad app federated-credential create --id "$DEPLOY_APP_ID" --parameters federated.json

# 3. The deploy job uses a GitHub environment (`production`), so add a second
#    credential for that subject too:
#    "repo:OWNER/OpsBridge365:environment:production"

# 4. Role assignments - RESOURCE GROUP SCOPE, never the subscription.
#    Contributor deploys the template; User Access Administrator is required
#    only because main.bicep creates a Key Vault "Secrets User" role assignment
#    for the container identity. (Role Based Access Control Administrator is a
#    tighter substitute if your tenant offers it.)
az group create -n opsbridge365-rg -l eastus
RG=$(az group show -n opsbridge365-rg --query id -o tsv)
az role assignment create --assignee "$DEPLOY_APP_ID" --role Contributor --scope "$RG"
az role assignment create --assignee "$DEPLOY_APP_ID" --role "User Access Administrator" --scope "$RG"

# 5. Grant this app NO Microsoft Graph application permissions. It never calls
#    Graph. Verify the list is empty:
az ad app show --id "$DEPLOY_APP_ID" --query requiredResourceAccess
```

Replace `OWNER/OpsBridge365` with the real `owner/repo`. The subject string must
match exactly — a mismatch fails as `AADSTS70021: No matching federated identity
record found`, which is the failure mode you want (it means the trust really is
scoped to one repo and one ref).

### One-time setup — B. `opsbridge-graph` (secret, no ARM)

```bash
# 1. Separate app registration for the runtime Graph caller
az ad app create --display-name opsbridge-graph
GRAPH_APP_ID=$(az ad app list --display-name opsbridge-graph --query '[0].appId' -o tsv)
az ad sp create --id "$GRAPH_APP_ID"

# 2. Client secret -> goes straight into the GRAPH_CLIENT_SECRET repo secret.
#    Print once, store once, never commit.
az ad app credential reset --id "$GRAPH_APP_ID" --years 1 --query password -o tsv

# 3. Microsoft Graph APPLICATION permissions, then admin consent.
#    Graph resource id: 00000003-0000-0000-c000-000000000000
#      User.Read.All                            df021288-bdef-4463-88db-98f22de89214
#      DeviceManagementManagedDevices.Read.All  2f51be20-0bb4-4fed-bf7b-db946066c75e
#      Sites.Selected                           883ea226-0bf2-4a8f-9f9d-92c9162a727d
for PERM in df021288-bdef-4463-88db-98f22de89214 \
            2f51be20-0bb4-4fed-bf7b-db946066c75e \
            883ea226-0bf2-4a8f-9f9d-92c9162a727d; do
  az ad app permission add --id "$GRAPH_APP_ID" \
    --api 00000003-0000-0000-c000-000000000000 \
    --api-permissions "${PERM}=Role"
done
az ad app permission admin-consent --id "$GRAPH_APP_ID"

# 4. Sites.Selected grants nothing on its own - authorise the ONE site the sync
#    job uses (Graph call, needs Sites.FullControl.All by an admin):
#    POST /sites/{SHAREPOINT_SITE_ID}/permissions
#      { "roles": ["write"],
#        "grantedToIdentities": [ { "application":
#          { "id": "<GRAPH_APP_ID>", "displayName": "opsbridge-graph" } } ] }

# 5. NO role assignment. Do not run `az role assignment create` for this app.
#    It has no business touching ARM. Verify it holds nothing:
az role assignment list --assignee "$GRAPH_APP_ID" --all -o table   # expect empty
```

---

## Repository secrets

`Settings → Secrets and variables → Actions → Secrets`.

### Deploy identity — `opsbridge-deploy` (no secret exists for this app)

| Secret | What it is | Where it is used |
|---|---|---|
| `AZURE_CLIENT_ID` | App (client) id of the **deploy** app registration — the one holding the federated credential. An identifier, not a password. | `azure/login@v2` **only** |
| `AZURE_TENANT_ID` | Directory (tenant) id. Shared by both apps; a public identifier. | `azure/login@v2`; Bicep `tenantId` |
| `AZURE_SUBSCRIPTION_ID` | Subscription containing the resource group. | `azure/login@v2` |

There is no `AZURE_CLIENT_SECRET`, and there must never be one.

### Graph runtime identity — `opsbridge-graph` (holds the only password)

| Secret | What it is | Where it is used |
|---|---|---|
| `GRAPH_CLIENT_ID` | App (client) id of the **Graph** app registration. Never used to authenticate to Azure. | Bicep `clientId` → container env |
| `GRAPH_CLIENT_SECRET` | Client secret of the Graph app. Used by the **containers** to call Microsoft Graph. This app holds no Azure RBAC, so the secret grants Graph access and nothing else. | Bicep `clientSecret` → Key Vault |

### Data locations (identifiers, not credentials)

| Secret | What it is | Where it is used |
|---|---|---|
| `SHAREPOINT_SITE_ID` | Graph id of the SharePoint site holding both lists. | Bicep `sharePointSiteId` |
| `ASSETS_LIST_ID` | Graph id of the Assets list (written by the sync job). | Bicep `assetsListId` |
| `TICKETS_LIST_ID` | Graph id of the Tickets list (read by `/metrics`). | Bicep `ticketsListId` |

`GITHUB_TOKEN` is **not** in these tables — it is injected by Actions and is the
only credential used to push to ghcr.io. No PAT to create, rotate, or leak.

## Repository variables (optional)

`Settings → Secrets and variables → Actions → Variables`. Each has a fallback,
so the workflow runs with none of them set.

| Variable | Default | Meaning |
|---|---|---|
| `AZURE_RESOURCE_GROUP` | `opsbridge365-rg` | Resource group the deployment targets |
| `AZURE_LOCATION` | `eastus` | Region used if the group has to be created |
| `NAME_PREFIX` | `opsbridge` | Bicep `namePrefix` — prefixes every resource name |

---

## The image

`ghcr.io/<owner>/<repo>`, tagged `:latest` and `:<commit sha>`. The deploy job
passes the **sha tag** to Bicep, so every revision points at an immutable
reference; `:latest` exists for humans and `docker run`.

The build uses `docker/build-push-action@v6` with GitHub Actions layer caching
(`type=gha`), so an unchanged dependency layer is not rebuilt on every push.

**Mark the GHCR package public** after the first push
(`Packages → opsbridge365 → Package settings → Change visibility`). A public
image is what lets Container Apps pull with no registry credentials at all —
that is the whole reason for ghcr.io over ACR.

---

## Post-deploy verification

A deploy that cannot be verified is a failed deploy, so two checks run after
`az deployment group create` and either one failing fails the job:

1. **`GET https://<apiFqdn>/healthz` must return HTTP 200.** Retried for ~3
   minutes because `minReplicas: 0` means the first request pays a cold start.
   Anything other than a 200 inside that window fails the job.
2. **`az containerapp job show` must report `triggerType == Schedule`.** Proves
   the sync job exists and is cron-triggered, not merely that ARM accepted the
   template.

Both write their result to the run's job summary.

---

## Secret hygiene in the pipeline

- The deploy identity and the Graph identity are separate app registrations, so
  `AZURE_CLIENT_ID` appears in exactly one place — the `azure/login@v2` step —
  and `GRAPH_CLIENT_ID`/`GRAPH_CLIENT_SECRET` appear only in the Bicep step. The
  principal with ARM rights has no secret; the secret has no ARM rights.
- Secrets are referenced only as `${{ secrets.NAME }}` and reach the Azure CLI
  through the step `env:` block — never interpolated into a command line.
- No `set -x` anywhere near a secret; `set -euo pipefail` only.
- No step echoes a secret, and the Bicep template emits no secret as an output
  (deployment outputs are readable by anyone with resource-group access).
- Every action is pinned to a major version tag (`@v4`, `@v5`, `@v6`); nothing
  tracks `@master`.

---

## Manual deploy (same thing, from a laptop)

```bash
az login
az group create -n opsbridge365-rg -l eastus
az deployment group create \
  -g opsbridge365-rg \
  -f infra/main.bicep \
  -p @infra/main.parameters.example.json \
  -p containerImage='ghcr.io/OWNER/opsbridge365:latest'
```

The `clientId`/`clientSecret` parameters are `opsbridge-graph`'s, not the
identity you just `az login`'d as. Keep the real parameter file out of git —
`main.parameters.example.json` carries placeholders only, and the client secret
belongs on the command line or in Key Vault, never in a committed file.
