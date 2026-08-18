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

## Things that will bite you

**If you read one section of this repository, read this one.**

This pipeline now deploys end to end: Actions run **`32115509179`** was green on
all four jobs — `test`, `secret scan (gitleaks)`, `build and push to ghcr.io`,
`deploy to Azure` — through OIDC federation with no stored Azure credential.

It did not work first time. **Three earlier runs failed for three genuinely
different reasons, and four distinct problems had to be fixed.** Not one of them
was a code defect or a template defect: `bicep build` returned zero diagnostics
throughout. Every one lived in the gap between a repository that compiles and one
specific real subscription — an identity provider's subject format, a
subscription's policy assignment, a subscription's provider registration state.

That gap is invisible until you deploy, and it is where the time goes.

---

### 1. `AADSTS700213` — an environment-gated job does not present a branch

```
AADSTS700213: No matching federated identity record found for presented assertion.
```

Three jobs green, then the deploy job rejected at login.

**Cause.** `deploy.yml`'s deploy job declares `environment: production`. That one
line changes the `sub` claim in the OIDC token GitHub mints:

| Job declares | Subject GitHub presents |
| --- | --- |
| no `environment:`, push to `main` | `repo:OWNER/REPO:ref:refs/heads/main` |
| `environment: production` | `repo:OWNER/REPO:environment:production` |
| pull request | `repo:OWNER/REPO:pull_request` |
| tag push | `repo:OWNER/REPO:ref:refs/tags/v1.0.0` |

**The branch does not appear in the subject at all.** The federated credential
had been created for `repo:Alhamwis/OpsBridge365:ref:refs/heads/main` — which
reads correctly, matches the workflow's `on:` trigger, and is wrong.

**Fix.** One federated credential, subject `...:environment:production`.

**Why the error message wastes your time.** Nothing in `AADSTS700213` mentions
environments. It reads like a tenant, app-id or audience problem, and that is
where an hour goes.

---

### 2. `AADSTS700213` again — the subject is **ID-qualified**

Same error, after fixing the subject to
`repo:Alhamwis/OpsBridge365:environment:production`. Still no match.

**Cause.** This account's GitHub default subject claim is **ID-qualified**:

```
repo:Alhamwis@<ownerId>/OpsBridge365@<repoId>:environment:production
```

The owner and repository names each carry an `@<numeric id>` suffix. `use_default`
was already `true` on the repository's OIDC settings, so there was nothing to
normalise away — this *is* the default here, and Entra has to match the
ID-qualified string character for character.

**Fix.** Set the federated credential's subject to the ID-qualified form.

**Do not treat this as a wart.** The ID-qualified form is **rename-proof**. A
subject built from names silently follows a repository or account rename, and a
name that is later re-registered by someone else inherits a live trust
relationship. A subject built from immutable numeric ids cannot do either: a
renamed repo fails loudly, and a recycled name matches nothing. This is a
security improvement, and it is worth keeping rather than working around.

**How to diagnose both of these in thirty seconds.** Compare two strings — what
the credential accepts:

```bash
az ad app federated-credential list --id "$DEPLOY_APP_ID" --query "[].subject" -o tsv
```

against what the job presents. Read the environment/ref half off the table in §1
from the job's own `on:` and `environment:` keys; for the ID-qualified half, take
the subject straight from the failing run's token claims rather than assembling
it by hand. They must match **character for character** — the repository path is
case-sensitive, and the environment name is the string in the workflow file, not
a display name in the UI.

---

### 3. `RequestDisallowedByAzure` — your region may be forbidden by policy

**Cause.** Azure for Students enforces an allowed-regions policy. The permitted
regions are:

```
northcentralus  mexicocentral  westus2  westus  canadacentral
```

**`eastus` is not among them** — and `eastus` was the default in the resource
group, in the runbook, and in `deploy.yml`'s `AZURE_LOCATION` fallback.

**Fix.** The resource group was recreated in **`westus2`**. Nothing in
`infra/main.bicep` changed: the template already takes `location` from
`resourceGroup().location`, so moving regions is a resource-group decision, not a
template edit. That is worth designing in deliberately.

**Set the `AZURE_LOCATION` repository variable** to an allowed region before the
first run. The workflow's built-in fallback is still `eastus`, which this
subscription's policy rejects.

**How to see the policy rather than guess at it:**

```bash
az policy assignment list --query "[].{name:displayName, scope:scope}" -o table
```

A subscription-level policy is not a bug and cannot be argued with from the CLI —
it is a constraint to design around, and every free or education-tier
subscription is likely to have some.

---

### 4. `MissingSubscriptionRegistration` — a fresh subscription registers nothing

**Cause.** On a new subscription, resource providers are unregistered until
something asks for them. All five of these were unregistered:

```
Microsoft.App
Microsoft.KeyVault
Microsoft.OperationalInsights
Microsoft.ManagedIdentity
Microsoft.Insights
```

ARM rejects a template referencing an unregistered provider, and **it names one
provider per failure** — so this arrives as a sequence of near-identical
failures, each costing a full pipeline run, rather than as one error listing
everything missing.

**Fix.** Register them all up front, before the first deploy:

```bash
for NS in Microsoft.App Microsoft.KeyVault Microsoft.OperationalInsights \
          Microsoft.ManagedIdentity Microsoft.Insights; do
  az provider register --namespace "$NS"
done

# Registration is asynchronous. Confirm before deploying:
az provider list --query "[?namespace=='Microsoft.App'||namespace=='Microsoft.KeyVault'||namespace=='Microsoft.OperationalInsights'||namespace=='Microsoft.ManagedIdentity'||namespace=='Microsoft.Insights'].{ns:namespace,state:registrationState}" -o table
```

Wait for `Registered` on all five. Registering is idempotent and safe to re-run.

---

### The general lesson

A green `bicep build` proves the template is syntactically and semantically
valid. It proves nothing about whether *your* subscription will accept it. The
four failures above are, in order: an identity-provider format, an identity-provider
default you did not choose, a policy assignment, and a subscription's own
initial state — and none of them is discoverable by reading code.

Deploy early, deploy to the real thing, and read the error rather than the error
message's first impression.

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

# 2. Federated credential. READ "Things that will bite you" §1 AND §2 FIRST.
#    Two things about this subject, both of which cost a failed deploy here:
#      a) The deploy job is gated on the `production` GitHub environment, so the
#         subject GitHub presents is `environment:production` - NOT `ref:...`.
#         A `ref:refs/heads/main` credential does NOT work for that job.
#      b) GitHub may present an ID-QUALIFIED subject - owner and repo each
#         carrying an `@<numeric id>` suffix. That is what this account gets by
#         default, and `use_default: true` does not normalise it away. Take the
#         subject from the failing run's token claims rather than assembling it
#         from names, then paste it in verbatim.
cat > federated.json <<'JSON'
{
  "name": "github-production",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:OWNER@<ownerId>/OpsBridge365@<repoId>:environment:production",
  "audiences": ["api://AzureADTokenExchange"]
}
JSON
az ad app federated-credential create --id "$DEPLOY_APP_ID" --parameters federated.json

# 3. Confirm this is the ONLY credential on the app - both the only federated
#    one and the only credential of any kind. Expect one subject, and two
#    empty lists.
az ad app federated-credential list --id "$DEPLOY_APP_ID" --query "[].subject" -o tsv
az ad app show --id "$DEPLOY_APP_ID" --query "{passwords:passwordCredentials, certs:keyCredentials}"

# 4. Role assignments - RESOURCE GROUP SCOPE, never the subscription.
#    Contributor deploys the template; User Access Administrator is required
#    only because main.bicep creates a Key Vault "Secrets User" role assignment
#    for the container identity. (Role Based Access Control Administrator is a
#    tighter substitute if your tenant offers it.)
#
#    These use `az rest`, not `az role assignment create`, on purpose - see
#    "Workaround: az role assignment fails with MissingSubscription" below.
#    Check your subscription's allowed regions first - see "Things that will
#    bite you" §3. eastus is NOT permitted on Azure for Students.
az group create -n rg-opsbridge365 -l westus2
RG=$(az group show -n rg-opsbridge365 --query id -o tsv)
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

#### The OIDC subject — the full rule, and why there is only one credential

The two ways this subject goes wrong, and both of their fixes, are in
[Things that will bite you §1 and §2](#things-that-will-bite-you). The short
version: an environment-gated job presents `environment:<name>`, not
`ref:<branch>`, and the subject may be **ID-qualified** with `@<ownerId>` and
`@<repoId>` suffixes that `use_default: true` does not remove.

`opsbridge-deploy` holds **exactly one** federated credential, and its subject is
the ID-qualified, environment-scoped string that run `32115509179` actually
presented.

**Why not just add several credentials and cover every case.** Because the
`ref:refs/heads/main` one would never be used — no job in this pipeline presents
that subject — and an unused federated credential is a second trust path nobody
is checking. If a future job drops the environment gate, the deploy would
silently start working through a credential that was never meant to authorise it.
One credential, one execution context, and a job that changes its gating fails
loudly instead of quietly succeeding.

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

Replace the subject placeholder with the real one — and take it from a failing
run's token claims rather than assembling it from names, because it may be
ID-qualified. The string must match exactly; a mismatch fails as
`AADSTS700213: No matching federated identity record found for presented
assertion`, which is the failure mode you want, and which is exactly what
happened here — twice, for two different reasons. See
[Things that will bite you §1 and §2](#things-that-will-bite-you).

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
| `AZURE_RESOURCE_GROUP` | `rg-opsbridge365` | Resource group the deployment targets |
| `AZURE_LOCATION` | `eastus` | Region used if the group has to be created. **Set this.** The fallback is `eastus`, which Azure for Students' allowed-regions policy rejects — this deployment runs in `westus2`. See [§3](#3-requestdisallowedbyazure--your-region-may-be-forbidden-by-policy) |
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

> **Done — the package is public.** Container Apps pulled
> `ghcr.io/alhamwis/opsbridge365` with no registry credential in run
> `32115509179`, which is the proof that the whole GHCR-over-ACR decision works:
> `infra/main.bicep` has no `registries:` block, no registry password in Key
> Vault, and no image-pull secret to rotate. Note that it cannot be scripted —
> GitHub exposes no REST API for container package visibility, so it is a UI-only
> setting and a step to remember when reproducing this from scratch.

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

Both write their result to the run's job summary. **Both passed in run
`32115509179`** — the API answered 200 and the job reported `Schedule`, so the
green deploy is a verified deploy rather than an accepted ARM submission.

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
az group create -n rg-opsbridge365 -l westus2      # a region your policy allows
az deployment group create \
  -g rg-opsbridge365 \
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

**Deployed, running, and verified.** Actions run **`32115509179`** was green on
all four jobs — `test`, `secret scan (gitleaks)`, `build and push to ghcr.io`,
`deploy to Azure` — a push-to-deploy through OIDC federation with no stored Azure
credential anywhere in the pipeline.

Live in `rg-opsbridge365` (**westus2**):

- Log Analytics `opsbridge-logs` (PerGB2018, 30-day retention), user-assigned
  identity `opsbridge-id`, Key Vault (RBAC-enabled, soft-delete on, standard),
  Container Apps environment `opsbridge-env`, Job `opsbridge-sync`, Container App
  `opsbridge-api`, action group `opsbridge-alerts`, scheduled query rule
  `opsbridge-sync-failed`.
- The API answers at
  `https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io` —
  `/healthz` **200**, `/metrics` **200** computed from live SharePoint, plain
  `http://` **301** to HTTPS. **Cold start from zero replicas: 714 ms.** Warm:
  **143 ms.** Idle replica count observed **0**.
- The sync job is `triggerType: Schedule` on cron `0 */6 * * *`, and **it has run
  in the cloud** — one device matched and PATCHed into the live Assets list,
  three rows left `Unknown`. See
  [`../evidence/sharepoint/reconciliation.md`](../evidence/sharepoint/reconciliation.md).
- The GHCR package is **public**, so Container Apps pulls with no registry
  credential at all.
- Budget `opsbridge-monthly-20` guards the group at $20/month. Observed spend
  **$0.00**.
- **58 offline tests** pass with no credentials; **12 live integration tests**
  pass against the real tenant.

What is still not done is in
[`README.md` § Known gaps](../README.md#known-gaps--not-done) — chiefly that the
cost figure covers hours rather than a billing cycle, the end-to-end run was one
user and one device, and there is no load or uptime measurement. The teardown
script has never been run against the live resource group.

**And read [Things that will bite you](#things-that-will-bite-you) before
reproducing any of this.** Getting here took four separate fixes, none of them in
the code.

---

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

## Phase 1 — the human steps: **all done**

These were the six steps that could not be automated. Every one is complete. The
table is kept because it is the reproduction path for anyone building this from
the repository, and because two of them turned out differently from the plan.

| # | Step | Status | What actually happened |
| --- | --- | --- | --- |
| H1 | **Azure subscription** | ✅ Done | "Azure for Students" on the Tarrant County College tenant. Resource group `rg-opsbridge365` — created in `eastus`, then **recreated in `westus2`** once the allowed-regions policy refused `eastus`. See [§3](#3-requestdisallowedbyazure--your-region-may-be-forbidden-by-policy) |
| H2 | **A tenant that can grant app-level consent** | ✅ Done | A separate OpsBridge365 tenant, where the author is global admin. See [`ARCHITECTURE.md` § The two-tenant split](ARCHITECTURE.md#the-two-tenant-split) |
| H3 | **Microsoft 365 with SharePoint** | ✅ Done | **M365 Business Standard, not the E5 trial.** A paid Business Standard subscription has no 30-day clock, which removes the "capture the evidence before the trial expires" pressure the earlier revision of this runbook warned about |
| H4 | **GitHub repository** | ✅ Done | Public: `github.com/Alhamwis/OpsBridge365`. A disclosure review ran first — zero real identifiers in the working tree or anywhere in the history |
| H5 | **`az login`** | ✅ Done | Both tenants. Note the Graph tenant needs `--allow-no-subscriptions` |
| H6 | **Graph admin consent** | ✅ Done — **and not by clicking** | Granted **programmatically** by creating `appRoleAssignments` on the service principal, then verified by reading the consent state back. Three application permissions, zero delegated grants |

**H6 is worth dwelling on.** Every version of this runbook before now described
admin consent as "one global-admin click." It does not have to be. Creating the
`appRoleAssignment` objects directly makes the grant reproducible, reviewable and
diffable — and reading the consent state back afterwards is what turns "consent
was requested" into "consent exists," which a portal click does not give you.

### What remained after Phase 1, and how it went

| Step | Outcome |
| --- | --- |
| Make the GHCR package public | ✅ Done. A UI-only visibility setting — GitHub exposes no REST API for it. Container Apps now pulls with no registry credential |
| Re-run `deploy.yml` | ✅ Done, after four fixes. Run `32115509179` is green on all four jobs. The four fixes are [Things that will bite you](#things-that-will-bite-you) |

### Prerequisites on the workstation

Already verified present on this machine: git, Python 3.12, Docker 29.1.3,
Azure CLI 2.89.1, GitHub CLI 2.97.0, Bicep 0.46.1.

The Azure CLI here lives in a pip venv under `%LOCALAPPDATA%\opsbridge-tools`, not
in Program Files. `scripts/Resolve-AzCli.ps1` exists for exactly this reason and
resolves `az` from four sources — including the **live User PATH read from the
registry**, so a shell that was open before the CLI was installed still finds it
rather than reporting a false "not installed".

---

## Phase 2 — one-time setup: **done, except step 6**

1. ✅ **Both app registrations created** — `opsbridge-deploy` and
   `opsbridge-graph`, in two different tenants. Do not merge them; the reasoning
   is above and in [`SECURITY.md`](SECURITY.md).
2. ✅ **SharePoint site created** —
   `https://opsbridge365.sharepoint.com/sites/opsbridge365ops`.
3. ✅ **Lists provisioned** — `Assets` and `Tickets`, each seeded with 4 synthetic
   rows, by `scripts/provision_sharepoint.py`. A second run reported **13 EXISTS /
   0 CREATED**, which is the idempotency claim demonstrated rather than asserted.

   ```bash
   python scripts/provision_sharepoint.py --dry-run   # plan only, sends nothing
   python scripts/provision_sharepoint.py --seed      # create what is missing, seed if empty
   ```

   It prints `ASSETS_LIST_ID` and `TICKETS_LIST_ID` at the end — those are the
   values the repository secrets want. It creates only what is missing and never
   modifies or deletes anything that already exists; it seeds only a list that is
   completely empty.

   > **`Sites.Selected` with `write` cannot create lists.** This was an open
   > question in earlier revisions; it is now settled. Creating the schema needed
   > a site-administrative permission, so a **throwaway `opsbridge-bootstrap` app
   > held `Sites.FullControl.All` for exactly that step and was then deleted.** The
   > runtime identity never held FullControl and its permissions were not widened.
   > If you reproduce this, do the same — and delete the app, not just the grant.
   > See [`SECURITY.md` §2a](SECURITY.md#2a-the-bootstrap-app--a-privilege-escalation-documented-and-reversed).

4. ✅ **App authorised on that one site** — `Sites.Selected` grants nothing until
   you do this (`POST /sites/{site-id}/permissions`, `"roles": ["write"]`). Role
   `write`, one site.
5. ✅ **Repository secrets and variables added** from the tables above.
6. ✅ **Mark the GHCR package public** (`Packages → opsbridge365 → Package
   settings → Change visibility`) — done. A public image is what lets Container
   Apps pull with no registry credentials at all, which is the whole reason for
   ghcr.io over ACR: no `registries:` block in the template, no registry password
   in Key Vault, no image-pull secret to rotate. There is no REST API for it; it
   is a UI-only setting, so it is easy to forget when reproducing this.
7. ✅ **Register the resource providers** and **pick an allowed region** — neither
   was in earlier revisions of this list, and both cost a failed run. See
   [§3](#3-requestdisallowedbyazure--your-region-may-be-forbidden-by-policy) and
   [§4](#4-missingsubscriptionregistration--a-fresh-subscription-registers-nothing).

## Phase 3 — the automated path

After Phase 2, deployment is a `git push`. Here is the pipeline with the status
each stage reached in Actions run `32115509179`:

```
push to main
  -> secret-scan     gitleaks over the full history          SUCCESS
  -> test            ruff (advisory) + pytest (hard gate)    SUCCESS
  -> build-and-push  docker build -> ghcr.io, :latest+:<sha> SUCCESS
  -> deploy          az deployment group create via OIDC     SUCCESS
  -> verify          GET /healthz returned 200; the sync job
                     reports triggerType == Schedule         SUCCESS
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
check that could not run is **SKIP**, never PASS. Unauthenticated it still skips
the cloud checks and says so; with an active `az login` against the subscription
it now has real resources to look at.

Manual equivalents — the API needs no credentials at all, because it is public:

```powershell
az deployment group show -g rg-opsbridge365 -n <deployment> --query properties.outputs
az containerapp job start -g rg-opsbridge365 -n opsbridge-sync        # don't wait for cron
az containerapp job execution list -g rg-opsbridge365 -n opsbridge-sync -o table

curl https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io/healthz
curl https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io/metrics
```

### Validate without deploying (possible today, offline)

```bash
bicep build infra/main.bicep --stdout > /dev/null   # compiles clean, 0 diagnostics
python -m pytest -q                                 # 58 passed, 12 deselected
docker build -t opsbridge365:local .
```

With tenant credentials in the environment, the live suite too:

```bash
python -m pytest -m integration -q                  # 12 passed, 58 deselected
```

`bicep build` is worth running before every push, and worth not trusting too far:
it returned zero diagnostics before each of the four deploy failures documented at
the top of this file. It tells you the template is valid. It cannot tell you your
subscription will accept it.

---

## Phase 4 — teardown

```powershell
powershell -NoProfile -File scripts/destroy-cloud.ps1 -WhatIf
powershell -NoProfile -File scripts/destroy-cloud.ps1 -ResourceGroup rg-opsbridge365 -ConfirmResourceGroup rg-opsbridge365 -PurgeKeyVault
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
az containerapp revision list -g rg-opsbridge365 -n opsbridge-api -o table
az containerapp update -g rg-opsbridge365 -n opsbridge-api --image ghcr.io/<owner>/opsbridge365:<good-sha>
```

The sync job is idempotent — it PATCHes by list item id — so re-running an older
image cannot corrupt the Assets list; it converges to whatever Graph currently
reports.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `AADSTS700213: No matching federated identity record found for presented assertion` | The federated credential's `subject` does not match what the job presents. Two distinct causes, and both happened here: **(a)** the job is environment-gated, so it presents `environment:<name>`, not `ref:refs/heads/main`; **(b)** the subject is **ID-qualified** — `repo:OWNER@<ownerId>/REPO@<repoId>:...` — and `use_default: true` does not normalise that away | Take the subject from the failing run's token claims and set it verbatim. Full explanation: [Things that will bite you §1 and §2](#things-that-will-bite-you) |
| `RequestDisallowedByAzure` at `az group create` or at deploy | A subscription-level allowed-regions policy. Azure for Students permits `northcentralus`, `mexicocentral`, `westus2`, `westus`, `canadacentral` — **not `eastus`** | Set the `AZURE_LOCATION` repository variable to a permitted region and recreate the resource group there. [§3](#3-requestdisallowedbyazure--your-region-may-be-forbidden-by-policy) |
| `MissingSubscriptionRegistration` | A fresh subscription has its resource providers unregistered, and ARM names one per failure | `az provider register` for `Microsoft.App`, `Microsoft.KeyVault`, `Microsoft.OperationalInsights`, `Microsoft.ManagedIdentity` and `Microsoft.Insights`, then wait for `Registered`. [§4](#4-missingsubscriptionregistration--a-fresh-subscription-registers-nothing) |
| The failure alert never fires on a real failure | The query matches only one of the app's two failure statuses. `app/sync.py` emits **`config_error`** (exit 2) *and* **`graph_error`** (exit 1); a wrong list id produces the latter | Match both, plus `Traceback`/`CRITICAL` as a catch-all — and test it by failing the job on purpose. [`../evidence/monitoring/alerting.md`](../evidence/monitoring/alerting.md) |
| First deployment fails resolving the Key Vault reference | Azure RBAC propagation lag — the role assignment exists but has not taken effect | Re-run the identical command. It is idempotent and the second run succeeds |
| `/healthz` never returns 200 in the verify step | Cold start exceeded the ~3-minute retry window, or the container is crashing | Check the Log Analytics stream; the app logs the reason on startup |
| `/metrics` returns 503 | Configuration is incomplete — the response deliberately does not say which variable | Read the container log; it names every missing variable |
| `/metrics` returns 502 | Graph auth failed, or Graph rejected the request | Usually missing admin consent, or `Sites.Selected` not yet authorized on the site |
| Sync exits `2` | `config_error` — no credentials at all | The container had no environment; check the Key Vault reference resolved |
| Sync exits `1` with `errors` populated | One or more PATCH calls failed | The JSON summary names each failure and the asset id |
| Container Apps cannot pull the image | The GHCR package is still private | Mark it public — that is what removes the need for registry credentials |
| Vault name collision on redeploy | A soft-deleted vault of the same name is inside its 7-day window | Purge it, or deploy to a differently-named resource group |
