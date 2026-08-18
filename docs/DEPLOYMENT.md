# Deployment

How OpsBridge365's cloud half gets from a `git push` to a running Container Apps
job and a scale-to-zero API — and what has to exist in GitHub and Azure first.

Two workflows:

| Workflow | Trigger | What it does | Token permissions |
|---|---|---|---|
| `.github/workflows/ci.yml` | pull request, manual | gitleaks secret scan + ruff + pytest | `contents: read` |
| `.github/workflows/deploy.yml` | push to `main`, manual | secret scan + test → build/push to ghcr.io → deploy to Azure → verify | `contents: read`, `packages: write`, `id-token: write` |

`deploy.yml` runs four jobs. `secret-scan` and `test` run in parallel and **both**
gate `build-and-push`, which gates `deploy`; `deploy` additionally requires
`github.ref == 'refs/heads/main'` and the `production` environment, so a manual
dispatch from a side branch builds an image but cannot deploy it.

`secret-scan` runs `gitleaks/gitleaks-action@v2` over the **full git history**
(`actions/checkout@v4` with `fetch-depth: 0`, not a shallow clone — a secret
deleted in a later commit is still in the history and still compromised). A
finding fails the job; there is no `continue-on-error`, so nothing reaches ghcr.io
or Azure without a clean scan. Rules are the gitleaks defaults; `.gitleaks.toml`
allowlists exactly one value, the public Azure built-in role definition GUID in
`infra/main.bicep`. See [`SECURITY.md`](SECURITY.md) §6.

---

## Two app registrations, in two tenants, and why

The system uses **two separate Entra app registrations, in two separate
tenants**. They are not interchangeable, and the split is the security design —
not bookkeeping.

| | `opsbridge-deploy` | `opsbridge-graph` |
|---|---|---|
| Lives in | The **school tenant** — the one that owns the Azure subscription | The **Microsoft 365 tenant** — the one that owns the SharePoint data |
| Job | Deploy infrastructure to Azure | Call Microsoft Graph at runtime |
| Auth | OIDC federated credential | Client secret |
| Client secret? | **No. Never. Not one.** | Yes — the only stored password in the system |
| Azure RBAC | Contributor + RBAC-admin, **resource group scope only** | **None at all.** It never touches ARM |
| Graph app permissions | **None** | `User.Read.All`, `Device.Read.All`, `Sites.Selected` |
| Used by | `azure/login@v2` | The containers, via Key Vault |
| Repo secrets | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` |

**The two tenant ids are different values and must never be swapped.**
`AZURE_TENANT_ID` is the directory `azure/login@v2` authenticates the deploy
identity against; it appears in exactly one step of `deploy.yml` and is never
passed to Bicep. `GRAPH_TENANT_ID` is the directory `opsbridge-graph` is
registered in; it becomes the Bicep `graphTenantId` parameter, then the
container's `AZURE_TENANT_ID` env var, then the MSAL authority the runtime asks
for a Graph token. Reuse one for the other and the ARM deployment still succeeds
— the failure surfaces later, as every Graph call rejecting a token from a
directory the app does not exist in. Neither value is a credential; a tenant id
is a public identifier. See
[`ARCHITECTURE.md` § The two-tenant split](ARCHITECTURE.md#the-two-tenant-split)
for why the tenants are split in the first place.

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

### One-time setup — A. `opsbridge-deploy` (school tenant, no secret ever)

Sign in to the tenant that owns the **Azure subscription** before running this
block: `az login --tenant <AZURE_TENANT_ID>`.

```bash
# 1. App registration (in the SCHOOL tenant, alongside the subscription).
#    Note: NO `az ad app credential reset` anywhere below - this app must never
#    have a client secret.
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
#
#    These use `az rest`, not `az role assignment create`, on purpose - see
#    "Workaround: az role assignment fails with MissingSubscription" below.
az group create -n opsbridge365-rg -l eastus
RG=$(az group show -n opsbridge365-rg --query id -o tsv)
SUB=$(az account show --query id -o tsv)

# principalId is the service principal's OBJECT id, not the app id.
DEPLOY_SP_ID=$(az ad sp show --id "$DEPLOY_APP_ID" --query id -o tsv)

# Built-in role definition ids (public, identical in every tenant):
CONTRIBUTOR=b24988ac-6180-42a0-ab88-20f7382dd24c
USER_ACCESS_ADMIN=18d7d88d-d35e-4fb5-a5c3-7773c20a72d9

for ROLE in "$CONTRIBUTOR" "$USER_ACCESS_ADMIN"; do
  az rest --method put \
    --url "https://management.azure.com${RG}/providers/Microsoft.Authorization/roleAssignments/$(uuidgen)?api-version=2022-04-01" \
    --body "{\"properties\":{\"roleDefinitionId\":\"/subscriptions/${SUB}/providers/Microsoft.Authorization/roleDefinitions/${ROLE}\",\"principalId\":\"${DEPLOY_SP_ID}\",\"principalType\":\"ServicePrincipal\"}}"
done

# Read them back (also via REST, for the same reason):
az rest --method get \
  --url "https://management.azure.com${RG}/providers/Microsoft.Authorization/roleAssignments?api-version=2022-04-01&\$filter=assignedTo('${DEPLOY_SP_ID}')" \
  --query "value[].properties.roleDefinitionId" -o tsv

# 5. Grant this app NO Microsoft Graph application permissions. It never calls
#    Graph. Verify the list is empty:
az ad app show --id "$DEPLOY_APP_ID" --query requiredResourceAccess
```

#### Workaround: `az role assignment` fails with `MissingSubscription`

On some machines every `az role assignment` subcommand fails before it reaches
the network:

```
(MissingSubscription) The request did not have a subscription or a valid
tenant level resource provider.
```

It fails even with `--subscription` passed explicitly, so it is not a
missing-context problem — the command group resolves its ARM scope through a
path that the two-tenant login here does not satisfy. The underlying REST API is
unaffected, which is why the block above calls
`https://management.azure.com{scope}/providers/Microsoft.Authorization/roleAssignments/{guid}?api-version=2022-04-01`
directly. `PUT` with a fresh GUID creates an assignment and is idempotent for a
given (scope, role, principal) triple; a repeat with a *different* GUID returns
`RoleAssignmentExists`, which is safe to ignore.

No script in `scripts/` uses `az role assignment` — role assignments are made by
`infra/main.bicep` (the Key Vault one) or by hand here, so this workaround is
documentation only.

Replace `OWNER/OpsBridge365` with the real `owner/repo`. The subject string must
match exactly — a mismatch fails as `AADSTS70021: No matching federated identity
record found`, which is the failure mode you want (it means the trust really is
scoped to one repo and one ref).

### One-time setup — B. `opsbridge-graph` (Microsoft 365 tenant, secret, no ARM)

**Different tenant.** Sign out of the school tenant and in to the Microsoft 365
one first: `az login --allow-no-subscriptions --tenant <GRAPH_TENANT_ID>`. That
tenant has no Azure subscription, hence the flag. Running this block against the
school tenant creates an app that can never be consented.

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
#      User.Read.All     df021288-bdef-4463-88db-98f22de89214
#      Device.Read.All   7438b122-aefc-4978-80ed-43db9fcc7715
#      Sites.Selected    883ea226-0bf2-4a8f-9f9d-92c9162a727d
#    Device.Read.All - NOT the Intune permission. The code reads directory
#    device objects; see "Graph permissions" below.
for PERM in df021288-bdef-4463-88db-98f22de89214 \
            7438b122-aefc-4978-80ed-43db9fcc7715 \
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

# 5. NO role assignment. Do not create one for this app - it has no business
#    touching ARM, and it lives in a tenant with no subscription to touch.
#    Verify it holds nothing (from a shell logged in to the AZURE tenant;
#    `az rest`, not `az role assignment list`, for the MissingSubscription
#    reason documented above):
GRAPH_SP_ID=$(az ad sp show --id "$GRAPH_APP_ID" --query id -o tsv)
az rest --method get \
  --url "https://management.azure.com/subscriptions/${SUB}/providers/Microsoft.Authorization/roleAssignments?api-version=2022-04-01&\$filter=assignedTo('${GRAPH_SP_ID}')" \
  --query "value" -o json    # expect []
```

---

## Repository secrets

`Settings → Secrets and variables → Actions → Secrets`.

Nine secrets in total: three describe the deploy identity, three describe the
Graph identity, three locate the data. Each of the first two groups is scoped to
**one tenant, and not the other**.

### Deploy identity — `opsbridge-deploy`, **school tenant** (no secret exists for this app)

| Secret | Tenant | What it is | Where it is used |
|---|---|---|---|
| `AZURE_CLIENT_ID` | School | App (client) id of the **deploy** app registration — the one holding the federated credential. An identifier, not a password. | `azure/login@v2` **only** |
| `AZURE_TENANT_ID` | School | Directory (tenant) id of the tenant that owns the Azure subscription. Not the Graph tenant. A public identifier. | `azure/login@v2` **only** — never passed to Bicep |
| `AZURE_SUBSCRIPTION_ID` | School | Subscription containing the resource group. | `azure/login@v2` |

There is no `AZURE_CLIENT_SECRET`, and there must never be one.

### Graph runtime identity — `opsbridge-graph`, **Microsoft 365 tenant** (holds the only password)

| Secret | Tenant | What it is | Where it is used |
|---|---|---|---|
| `GRAPH_TENANT_ID` | Microsoft 365 | Directory (tenant) id of the tenant the Graph app is registered in and the SharePoint site lives in. A **different value** from `AZURE_TENANT_ID`. | Bicep `graphTenantId` → container env `AZURE_TENANT_ID` → MSAL authority |
| `GRAPH_CLIENT_ID` | Microsoft 365 | App (client) id of the **Graph** app registration. Never used to authenticate to Azure. | Bicep `clientId` → container env |
| `GRAPH_CLIENT_SECRET` | Microsoft 365 | Client secret of the Graph app. Used by the **containers** to call Microsoft Graph. This app holds no Azure RBAC, so the secret grants Graph access and nothing else. | Bicep `clientSecret` → Key Vault |

### Data locations (identifiers, not credentials — all in the Microsoft 365 tenant)

| Secret | Tenant | What it is | Where it is used |
|---|---|---|---|
| `SHAREPOINT_SITE_ID` | Microsoft 365 | Graph id of the SharePoint site holding both lists. | Bicep `sharePointSiteId` |
| `ASSETS_LIST_ID` | Microsoft 365 | Graph id of the Assets list (written by the sync job). | Bicep `assetsListId` |
| `TICKETS_LIST_ID` | Microsoft 365 | Graph id of the Tickets list (read by `/metrics`). | Bicep `ticketsListId` |

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

- The deploy identity and the Graph identity are separate app registrations in
  separate tenants, so `AZURE_CLIENT_ID` and `AZURE_TENANT_ID` appear in exactly
  one place — the `azure/login@v2` step — and `GRAPH_TENANT_ID`,
  `GRAPH_CLIENT_ID` and `GRAPH_CLIENT_SECRET` appear only in the Bicep step. The
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

---
---

# End-to-end runbook

> Everything above documents the pipeline and the identity model. Everything below
> is the ordered runbook: what a human must do by hand, what happens automatically
> afterwards, and where the process currently stands.

## Current state — read this first

**Nothing is deployed.** As of the last commit:

- This repository has **no git remote**, so neither workflow has ever executed.
  There is no GitHub repository, no Actions run, and no GHCR package.
- There is **no Azure subscription**, no Entra tenant, no app registration, and no
  resource group.
- `infra/main.bicep` **compiles clean** (`bicep build infra/main.bicep`, Bicep CLI
  0.46.1, zero diagnostics) but has never been submitted to ARM — not even with
  `--what-if`, which requires an authenticated subscription.
- The container image builds and runs locally; the test suite passes (57 tests).

Every step in Phase 1 below is blocked on a human creating an account. None of it
can be automated, which is why it is listed separately and first.

## Graph permissions — the authoritative list

These are the three application permissions to consent to, and nothing else.
They are the same three used in the `opsbridge-graph` setup script above.

| Permission | Graph id | For |
| --- | --- | --- |
| `User.Read.All` | `df021288-bdef-4463-88db-98f22de89214` | `GET /users` — the user list, for device-to-owner matching. Also covers the `registeredOwners` expansion |
| `Device.Read.All` | `7438b122-aefc-4978-80ed-43db9fcc7715` | `GET /devices?$expand=registeredOwners` — directory device objects |
| `Sites.Selected` | `883ea226-0bf2-4a8f-9f9d-92c9162a727d` | SharePoint, limited to the one provisioned site: the Assets and Tickets lists |

**`Device.Read.All`, not the Intune permission.** `app/graph.py: list_devices`
calls `GET /devices` — the *directory* device object. It does not call
`/deviceManagement/managedDevices`, and nothing in this service talks to Intune
or Endpoint Manager. Reading Intune device data would be a future change (noted
in [`ARCHITECTURE.md`](ARCHITECTURE.md)); it would need the Intune
managed-devices permission *and* an Intune licence, and neither is in scope here.

Verify any permission id against your own tenant before consenting — `az ad sp
show --id 00000003-0000-0000-c000-000000000000 --query
"appRoles[?value=='Device.Read.All']"` prints the authoritative value.

> **Corrected 2026-08-16.** Earlier revisions of this runbook listed the Intune
> managed-devices permission here and in the setup script above. That was wrong:
> the code reads directory device objects, so consenting to the Intune scope
> would have left `GET /devices` returning `403 Authorization_RequestDenied` at
> runtime while the app appeared correctly configured. Every occurrence has been
> replaced with `Device.Read.All`.

---

## Phase 1 — the human steps (cannot be automated)

Each requires an interactive, signed-in person. Roughly 60–90 minutes total, most
of it waiting for tenant provisioning.

| # | Step | Why a human is required | Unblocks |
| --- | --- | --- | --- |
| H1 | **Azure for Students** signup at `azure.microsoft.com/free/students` with an academic address | Student identity verification. No card, but the check is interactive | The subscription, `az login` |
| H2 | **Create your own Entra tenant** — Azure portal → Microsoft Entra ID → Manage tenants → Create | Portal tenant creation requires a signed-in human. Free, permanent, and you become global admin — which is the whole point | App registrations, admin consent |
| H3 | **Start a Microsoft 365 E5 trial** on that tenant | Adds SharePoint to the tenant. Requires a payment method on file (not charged if cancelled). Start the 30-day clock only when you are ready to use it | SharePoint site, both lists |
| H4 | **`gh auth login`** (device code) and create the GitHub repository | Interactive device-code flow | Push, Actions, GHCR |
| H5 | **`az login`** against the new tenant | Interactive MFA | Everything in Azure |
| H6 | **Grant admin consent** to the Graph app permissions | One global-admin click in the portal, or `az ad app permission admin-consent` as an admin | Any Graph call the service makes |

Order matters: H2 before H3 (the trial attaches to a tenant), H2 before the app
registrations, and H6 last — you cannot consent to permissions on an app that does
not exist.

### Prerequisites on the workstation

Already verified present on this machine: git, Python 3.12, Docker 29.1.3,
Azure CLI 2.89.1, GitHub CLI 2.97.0, Bicep 0.46.1.

The Azure CLI here lives in a pip venv under `%LOCALAPPDATA%\opsbridge-tools`, not
in Program Files. `scripts/Resolve-AzCli.ps1` exists for exactly this reason and
resolves `az` from four sources — including the **live User PATH read from the
registry**, so a shell that was open before the CLI was installed still finds it
rather than reporting a false "not installed".

---

## Phase 2 — one-time setup, once the accounts exist

1. **Create both app registrations** — the `opsbridge-deploy` and `opsbridge-graph`
   scripts earlier in this document. Do not merge them; the reasoning is above and
   in [`SECURITY.md`](SECURITY.md).
2. **Create the SharePoint site** in the trial tenant.
3. **Provision the lists** — idempotent, and safe to run repeatedly:

   ```bash
   python scripts/provision_sharepoint.py --dry-run   # plan only, sends nothing
   python scripts/provision_sharepoint.py --seed      # create what is missing, seed if empty
   ```

   It prints `ASSETS_LIST_ID` and `TICKETS_LIST_ID` at the end — those are the
   values the repository secrets want. It creates only what is missing and never
   modifies or deletes anything that already exists; it seeds only a list that is
   completely empty.
4. **Authorize the app on that one site** — `Sites.Selected` grants nothing until
   you do (`POST /sites/{site-id}/permissions`, `"roles": ["write"]`).
5. **Add the repository secrets and variables** from the tables above.
6. **Push, then mark the GHCR package public** (`Packages → opsbridge365 → Package
   settings → Change visibility`). A public image is what lets Container Apps pull
   with no registry credentials at all.

## Phase 3 — the automated path

After Phase 2, deployment is a `git push`:

```
push to main
  -> test            ruff (advisory) + pytest (hard gate)
  -> build-and-push  docker build -> ghcr.io, tagged :latest and :<sha>
  -> deploy          az deployment group create against infra/main.bicep, via OIDC
  -> verify          GET /healthz must return 200; the sync job must report
                     triggerType == Schedule
```

Either verification failing fails the deploy — a deploy that cannot be verified is
a failed deploy. Both write their result to the run's job summary.

### Or from a laptop

```powershell
$env:GRAPH_CLIENT_SECRET = '<secret>'
powershell -NoProfile -File scripts/deploy-opsbridge.ps1 -WhatIf   # preview
powershell -NoProfile -File scripts/deploy-opsbridge.ps1           # deploy
```

`deploy-opsbridge.ps1` refuses to start anything it cannot finish: the Azure CLI,
an active login, a compiling Bicep file, a green test suite and the required
configuration are all checked **before** the first resource is touched. The failure
mode is "nothing happened, and here is why" — never a half-built resource group.
`-WhatIf` still runs the full preflight and submits the deployment with az's own
`--what-if`, so Azure reports the change set without applying it. No secret is
written to a file, echoed, or logged.

### Verify

```powershell
powershell -NoProfile -File scripts/verify-opsbridge.ps1
```

Read-only, and it never conflates "checked and passed" with "could not check". A
check that could not run is **SKIP**, never PASS. Run it today, with nothing
deployed, and it reports the local toolchain, the test suite and the container as
PASS and every cloud check as SKIP with the reason — which is the honest picture.

Manual equivalents:

```powershell
az deployment group show -g opsbridge365-rg -n <deployment> --query properties.outputs
az containerapp job start -g opsbridge365-rg -n opsbridge-sync        # don't wait for cron
az containerapp job execution list -g opsbridge365-rg -n opsbridge-sync -o table
curl https://<apiFqdn>/healthz
curl https://<apiFqdn>/metrics
```

### Validate without deploying (possible today, offline)

```bash
bicep build infra/main.bicep --stdout > /dev/null   # compiles clean, 0 diagnostics
python -m pytest -q                                 # 57 passed
docker build -t opsbridge365:local .
```

`az deployment group validate` and `--what-if` both need an authenticated
subscription, so neither has been run.

---

## Phase 4 — teardown

```powershell
powershell -NoProfile -File scripts/destroy-cloud.ps1 -WhatIf
powershell -NoProfile -File scripts/destroy-cloud.ps1 -ResourceGroup opsbridge365-rg -ConfirmResourceGroup opsbridge365-rg -PurgeKeyVault
```

Deletes the whole resource group — the entire cloud layer, and nothing outside it.
It prints every resource that will be deleted *before* asking anything, and
requires the resource group name typed back exactly; a bare `-Confirm` is not
accepted and there is no `-Force`. `-PurgeKeyVault` also purges the soft-deleted
vault, which matters because a soft-deleted vault blocks recreating one with the
same name for its 7-day retention window — relevant if you tear down and rebuild
for a demo.

---

## Rollback

Revisions are immutable and tagged by commit sha, so rolling back is redeploying a
known-good sha:

```powershell
az containerapp revision list -g opsbridge365-rg -n opsbridge-api -o table
az containerapp update -g opsbridge365-rg -n opsbridge-api --image ghcr.io/<owner>/opsbridge365:<good-sha>
```

The sync job is idempotent — it PATCHes by list item id — so re-running an older
image cannot corrupt the Assets list; it converges to whatever Graph currently
reports.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `AADSTS70021: No matching federated identity record found` | The federated credential's `subject` does not match the workflow's context | Match it exactly: `repo:OWNER/REPO:ref:refs/heads/main`, plus one for `environment:production` |
| First deployment fails resolving the Key Vault reference | Azure RBAC propagation lag — the role assignment exists but has not taken effect | Re-run the identical command. It is idempotent and the second run succeeds |
| `/healthz` never returns 200 in the verify step | Cold start exceeded the ~3-minute retry window, or the container is crashing | Check the Log Analytics stream; the app logs the reason on startup |
| `/metrics` returns 503 | Configuration is incomplete — the response deliberately does not say which variable | Read the container log; it names every missing variable |
| `/metrics` returns 502 | Graph auth failed, or Graph rejected the request | Usually missing admin consent, or `Sites.Selected` not yet authorized on the site |
| Sync exits `2` | `config_error` — no credentials at all | The container had no environment; check the Key Vault reference resolved |
| Sync exits `1` with `errors` populated | One or more PATCH calls failed | The JSON summary names each failure and the asset id |
| Container Apps cannot pull the image | The GHCR package is still private | Mark it public — that is what removes the need for registry credentials |
| Vault name collision on redeploy | A soft-deleted vault of the same name is inside its 7-day window | Purge it, or deploy to a differently-named resource group |
