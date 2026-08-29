# Deployment

How OpsBridge365's cloud half gets from a `git push` to a running Container Apps
job and a scale-to-zero API — and what has to exist in GitHub and Azure first.

Three workflows:

| Workflow | Trigger | What it does | Token permissions |
|---|---|---|---|
| `.github/workflows/ci.yml` | pull request, manual | gitleaks over full history · ruff · pytest · CodeQL · Trivy filesystem scan · dependency review | `contents: read`, plus `security-events: write` for CodeQL and `pull-requests: write` for the dependency-review comment |
| `.github/workflows/deploy.yml` | push to `main`, manual | secret scan + test + CodeQL → build/push to ghcr.io → Trivy image scan + SBOM → deploy to Azure → verify | `contents: read`, `packages: write`, `id-token: write` |
| `.github/workflows/health.yml` | every 4 hours, manual | `GET /healthz`, checks `/demo/metrics` is synthetic, checks `/metrics` still refuses an unauthenticated caller | `contents: read`, no secrets |

`deploy.yml` runs six jobs. `secret-scan`, `test` and `codeql` run in parallel and
**all three** gate `build-and-push`, which gates `scan-image`, which gates
`deploy`; `deploy` additionally requires `github.ref == 'refs/heads/main'` and the
`production` environment, so a manual dispatch from a side branch builds an image
but cannot deploy it.

Every gate is hard. There is no `continue-on-error` in any of the three files —
the ruff step used to carry one, which meant a lint failure was reported and then
ignored all the way through to a production deployment.

`secret-scan` runs gitleaks over the **full git history** (`actions/checkout` with
`fetch-depth: 0`, not a shallow clone — a secret deleted in a later commit is
still in the history and still compromised). Rules are the gitleaks defaults;
`.gitleaks.toml` allowlists **seven exact values** — four Azure built-in role
definition GUIDs and three Microsoft Graph app-role ids, each by exact value,
never by pattern, so a real 36-character credential that merely looks like a GUID
is still caught. See [`SECURITY.md`](SECURITY.md).

Every third-party action is pinned to a **full 40-character commit SHA** with the
version in a trailing comment. A tag is a movable ref, and the deploy job holds
`id-token: write`; whoever can move a tag in an action's repository could
otherwise change what runs inside that job. Dependabot proposes the SHA bumps
weekly.

`health.yml` is the only thing here that observes the service on an ongoing
basis, and its run history is the evidence. It never redeploys or remediates: a
red run is an outage that was really observed and is left red. It publishes **no
uptime percentage** — six probes a day cannot support one.

---

## Things that will bite you

**If you read one section of this repository, read this one.**

This pipeline deploys end to end: a push to `main` runs the gates, publishes the
image, and deploys to Azure through OIDC federation with no stored Azure
credential. The current state of that is the
[deploy workflow's run list](https://github.com/Alhamwis/OpsBridge365/actions/workflows/deploy.yml),
not a run id written down here.

It did not work first time. **Four distinct problems had to be fixed before a run
went green**, and not one of them was a code defect or a template defect:
`bicep build` returned zero diagnostics throughout. Every one lived in the gap
between a repository that compiles and one specific real subscription — an
identity provider's subject format, a subscription's policy assignment, a
subscription's provider registration state.

That gap is invisible until you deploy, and it is where the time goes.

The first green run was **`32115509179`, on 2026-08-18 at commit `63c4616`** —
that is the run the four write-ups below were captured from, and it is quoted
here as history, not as current state.

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
regions at the time were:

```
northcentralus  mexicocentral  westus2  westus  canadacentral
```

**`eastus` is not among them** — and `eastus` was the default in the resource
group, in the runbook, and in `deploy.yml`'s `AZURE_LOCATION` fallback.

**Fix.** The resource group was recreated in **`westus2`**. Nothing in
`infra/main.bicep` changed: the template already takes `location` from
`resourceGroup().location`, so moving regions is a resource-group decision, not a
template edit. That is worth designing in deliberately.

`deploy.yml`'s `AZURE_LOCATION` fallback is now `westus2`. Set the repository
variable anyway if your subscription's policy differs — re-read the policy rather
than trusting this list, which was true for one subscription on 2026-08-18.

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
| Lives in | The **college tenant** — the one that owns the Azure subscription | The **Microsoft 365 tenant** — the one that owns the SharePoint data |
| Job | Deploy infrastructure to Azure | Call Microsoft Graph at runtime |
| Auth | OIDC federated credential | Client secret |
| Client secret? | **No. Never. Not one.** | Yes — the only stored password in the system |
| Azure RBAC | **Contributor, resource group scope only** — that is all `infra/main.bicep` now requires. Measured 2026-08-29 the principal *also* still carries `Role Based Access Control Administrator` from the old design; removing it is the last step of the split, see [Contributor and nothing more](#contributor-and-nothing-more--and-how-to-remove-the-elevated-role) | **None at all.** It never touches ARM |
| Graph app permissions | **None** | `User.Read.All`, `Device.Read.All`, `Sites.Selected` |
| Used by | `azure/login` | The containers, via Key Vault |
| Repo secrets | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID` — **the secret is not one of them** |

**The two tenant ids are different values and must never be swapped.**
`AZURE_TENANT_ID` is the directory `azure/login` authenticates the deploy
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
(that is what `id-token: write` buys), `azure/login` exchanges it for an access
token, and the token dies with the job.

The Graph client secret is not in this picture at all any more. It used to be
passed to Bicep as a `@secure()` parameter on every deployment, which meant
GitHub stored it and handed it over on every push. It is now written to Key Vault
**once**, by `infra/bootstrap.bicep`, run by a human — see
[One-time bootstrap](#one-time-bootstrap--infrabootstrapbicep) below. The routine
pipeline never sees a credential of any kind.

### One-time setup — A. `opsbridge-deploy` (college tenant, no secret ever)

Sign in to the tenant that owns the **Azure subscription** before running this
block: `az login --tenant <AZURE_TENANT_ID>`.

```bash
# 1. App registration (in the tenant that owns the subscription).
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

# 4. ONE role assignment: Contributor, RESOURCE GROUP SCOPE, never the
#    subscription. That is the whole standing privilege of the pipeline.
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

# Built-in role definition id (public, identical in every tenant):
CONTRIBUTOR=b24988ac-6180-42a0-ab88-20f7382dd24c

az rest --method put \
  --url "https://management.azure.com${RG}/providers/Microsoft.Authorization/roleAssignments/$(uuidgen)?api-version=2022-04-01" \
  --body "{\"properties\":{\"roleDefinitionId\":\"/subscriptions/${SUB}/providers/Microsoft.Authorization/roleDefinitions/${CONTRIBUTOR}\",\"principalId\":\"${DEPLOY_SP_ID}\",\"principalType\":\"ServicePrincipal\"}}"

# Read it back (also via REST, for the same reason). Expect exactly one role.
az rest --method get \
  --url "https://management.azure.com${RG}/providers/Microsoft.Authorization/roleAssignments?api-version=2022-04-01&\$filter=assignedTo('${DEPLOY_SP_ID}')" \
  --query "value[].properties.roleDefinitionId" -o tsv

# 5. Grant this app NO Microsoft Graph application permissions. It never calls
#    Graph. Verify the list is empty:
az ad app show --id "$DEPLOY_APP_ID" --query requiredResourceAccess
```

#### Contributor and nothing more — and how to remove the elevated role

Earlier revisions of this runbook granted a second role alongside Contributor,
because `main.bicep` created the Key Vault "Secrets User" role assignment for the
container identity, and `Microsoft.Authorization/roleAssignments` cannot be
created by Contributor. That meant the pipeline carried the privilege to hand out
roles **on every push, forever**, to create one assignment that is created once.

That assignment now lives in `infra/bootstrap.bicep`, which a human runs
deliberately. The routine identity needs **Contributor only**.

Two things, stated plainly because this file used to get both of them wrong:

- **The script and the reality disagreed.** The setup block above used to grant
  **User Access Administrator**, while the identity actually deployed here was
  granted **Role Based Access Control Administrator**. Measured on
  **2026-08-29**, the live deploy service principal still holds Contributor
  **and** Role Based Access Control Administrator, both at resource-group scope
  — one role more than the current templates need.
- **Neither elevated role is needed any more.** Remove whichever one your
  deployment has, once you are on the split templates (a `main.bicep` with no
  `roleAssignments` resource in it):

```bash
# What the deploy identity actually holds, at resource-group scope:
az rest --method get \
  --url "https://management.azure.com${RG}/providers/Microsoft.Authorization/roleAssignments?api-version=2022-04-01&\$filter=assignedTo('${DEPLOY_SP_ID}')" \
  --query "value[].{assignment:name, role:properties.roleDefinitionId}" -o tsv

# Remove the elevated role. Both ids below are public built-in role definitions:
#   f58310d9-a9f6-439a-9e8d-f62e7b41a168  Role Based Access Control Administrator
#   18d7d88d-d35e-4fb5-a5c3-7773c20a72d9  User Access Administrator
for ELEVATED in f58310d9-a9f6-439a-9e8d-f62e7b41a168 \
                18d7d88d-d35e-4fb5-a5c3-7773c20a72d9; do
  az rest --method get \
    --url "https://management.azure.com${RG}/providers/Microsoft.Authorization/roleAssignments?api-version=2022-04-01&\$filter=assignedTo('${DEPLOY_SP_ID}')" \
    --query "value[?ends_with(properties.roleDefinitionId, '${ELEVATED}')].id" -o tsv |
  while read -r ASSIGNMENT_ID; do
    [ -n "$ASSIGNMENT_ID" ] || continue
    echo "removing ${ASSIGNMENT_ID}"
    az rest --method delete \
      --url "https://management.azure.com${ASSIGNMENT_ID}?api-version=2022-04-01"
  done
done

# Re-read. Expect Contributor and nothing else.
az rest --method get \
  --url "https://management.azure.com${RG}/providers/Microsoft.Authorization/roleAssignments?api-version=2022-04-01&\$filter=assignedTo('${DEPLOY_SP_ID}')" \
  --query "value[].properties.roleDefinitionId" -o tsv
```

Then push once and watch the deploy job stay green. If it fails with
`AuthorizationFailed` on a role assignment, something still creates one in
`main.bicep` — fix that rather than putting the role back.

The privilege did not vanish; it moved to a human. Whoever runs
`infra/bootstrap.bicep` needs rights to create a role assignment on the resource
group (Owner, User Access Administrator, or Role Based Access Control
Administrator). That is a person, once, not a pipeline, forever.

#### The OIDC subject — the full rule, and why there is only one credential

The two ways this subject goes wrong, and both of their fixes, are in
[Things that will bite you §1 and §2](#things-that-will-bite-you). The short
version: an environment-gated job presents `environment:<name>`, not
`ref:<branch>`, and the subject may be **ID-qualified** with `@<ownerId>` and
`@<repoId>` suffixes that `use_default: true` does not remove.

`opsbridge-deploy` holds **exactly one** federated credential, and its subject is
the ID-qualified, environment-scoped string the deploy job actually presents.

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
unaffected, which is why the blocks above call
`https://management.azure.com{scope}/providers/Microsoft.Authorization/roleAssignments/{guid}?api-version=2022-04-01`
directly. `PUT` with a fresh GUID creates an assignment and is idempotent for a
given (scope, role, principal) triple; a repeat with a *different* GUID returns
`RoleAssignmentExists`, which is safe to ignore.

No script in `scripts/` uses `az role assignment`. The only role assignment the
system needs is made by `infra/bootstrap.bicep`, so this workaround applies to
the by-hand commands above and nothing else.

### One-time setup — B. `opsbridge-graph` (Microsoft 365 tenant, secret, no ARM)

**Different tenant.** Sign out of the college tenant and in to the Microsoft 365
one first: `az login --allow-no-subscriptions --tenant <GRAPH_TENANT_ID>`. That
tenant has no Azure subscription, hence the flag. Running this block against the
college tenant creates an app that can never be consented.

```bash
# 1. Separate app registration for the runtime Graph caller
az ad app create --display-name opsbridge-graph
GRAPH_APP_ID=$(az ad app list --display-name opsbridge-graph --query '[0].appId' -o tsv)
az ad sp create --id "$GRAPH_APP_ID"

# 2. Client secret. Print once, store once, never commit. It does NOT become a
#    GitHub secret - it is passed to infra/bootstrap.bicep, which writes it to
#    Key Vault, and that is the only place it lives afterwards.
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

## One-time bootstrap — `infra/bootstrap.bicep`

**Run this once, by hand, before the first routine deployment.** It is a
prerequisite, not an optional extra: `infra/main.bicep` references the Key Vault
and the managed identity as `existing`, so deploying it into a resource group
that has not been bootstrapped fails at the lookup. That failure is the intended
one — loud, immediate, and before anything is created.

### What it creates, and why it is a separate template

| Resource | Why it cannot live in `main.bicep` |
| --- | --- |
| Key Vault (standard, RBAC-enabled, soft-delete 7 days) | It holds the secret values below, and it should outlive any single deployment |
| `opsbridge-id` — user-assigned managed identity | Shared by the job and the app; the only principal allowed to read the vault |
| Role assignment: **Key Vault Secrets User**, scoped to the vault | `Microsoft.Authorization/roleAssignments` **cannot be created by Contributor**. While this lived in `main.bicep`, the GitHub deployment identity had to hold Role Based Access Control Administrator permanently, on every push, to create one assignment that only ever needs creating once |
| Secret values: `graph-client-secret`, `metrics-api-token` | `main.bicep` used to take the Graph client secret as a parameter, so GitHub had to store it and hand it over on every deployment. Here the value is supplied once by an operator; afterwards `main.bicep` only names the secret and never sees it |

Two things follow directly, and they are the point of the split:

- The routine GitHub deployment identity needs **Contributor and nothing more**.
- **`GRAPH_CLIENT_SECRET` is no longer a GitHub secret.** Neither is the metrics
  token. The pipeline handles no credential values at all.

### Running it

```bash
az deployment group create -g rg-opsbridge365 \
  --template-file infra/bootstrap.bicep \
  --parameters graphClientSecret=<value> metricsApiToken=<value>
```

**Prefer a parameters file** so the secret never reaches `az`'s argv, where any
concurrent `ps` on the machine can read it. Write that file **outside the
repository** — nothing in `.gitignore` covers a bootstrap parameters file, and
the safest place for a file holding two live credentials is not the working tree:

```bash
PARAMS="$(mktemp -d)/bootstrap.parameters.json"
cat > "$PARAMS" <<'JSON'
{
  "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
    "graphClientSecret": { "value": "" },
    "metricsApiToken":   { "value": "" }
  }
}
JSON
# fill the two values in, then:
az deployment group create -g rg-opsbridge365 \
  --template-file infra/bootstrap.bicep \
  --parameters "@$PARAMS"
shred -u "$PARAMS" 2>/dev/null || rm -f "$PARAMS"
```

Generate the metrics token with something that is not a password:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The minimum length is 32 characters — `MIN_TOKEN_LENGTH` in `app/security.py`,
and stated in `.env.example`. Thirty-two characters of base64url is roughly 192
bits; the floor exists to reject a placeholder like `changeme`, not to certify a
strong token.

### Both secret parameters are optional, and independent

`graphClientSecret` and `metricsApiToken` both default to `''` and each secret
resource is guarded by `if (!empty(...))`. An omitted parameter leaves the
existing secret untouched rather than overwriting it with an empty string, so
**you can rotate one without touching the other**:

```bash
# rotate only the metrics token: graphClientSecret is simply not passed, and
# the existing graph-client-secret version is left alone.
az deployment group create -g rg-opsbridge365 \
  --template-file infra/bootstrap.bicep \
  --parameters metricsApiToken=<new-value>
```

The template is otherwise idempotent — the role assignment name is a
deterministic `guid()`, so a re-run re-asserts the same vault, identity and
assignment and changes nothing else.

### Rotation is two steps, not one

A new secret version does not reach a running container by itself. Container Apps
resolves the versionless Key Vault URI at **replica start**, so:

```bash
# 1. write the new value (above), then 2. restart the active revision
REV=$(az containerapp revision list -g rg-opsbridge365 -n opsbridge-api \
        --query "[?properties.active].name | [0]" -o tsv)
az containerapp revision restart -g rg-opsbridge365 -n opsbridge-api --revision "$REV"
```

Then confirm the old token is dead and the new one works:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $OLD" "$BASE/metrics"   # expect 401
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $NEW" "$BASE/metrics"   # expect 200
```

### When it must be re-run

- **Any time the Key Vault is recreated.** Tearing down with
  `destroy-cloud.ps1 -PurgeKeyVault`, purging a soft-deleted vault, or deploying
  into a *different* resource group all produce a vault that has no secrets in it
  — and the vault name is derived from `uniqueString(resourceGroup().id)`, so a
  new resource group means a new vault name.
- **Any time `namePrefix` changes.** Both templates derive the vault name from
  it. If they disagree, `main.bicep` fails on the `existing` lookup instead of
  quietly building a second vault, which is the failure mode worth having.
- **Any time either credential is rotated** — see above.

A note on what this looks like from the operator's side: the vault is RBAC-only
and the human operator holds no data-plane role on it. `az keyvault secret list`
from an operator shell returns **`ForbiddenByRbac`** — re-verified 2026-08-29.
That is the control working, not a misconfiguration. Writing secrets *through a
deployment* still succeeds because ARM does it over the control plane. Only the
managed identity can read the values back.

---

## Repository secrets

`Settings → Secrets and variables → Actions → Secrets`.

**Eight secrets in total**: three describe the deploy identity, two describe the
Graph identity, three locate the data. Every one of them is an identifier —
there is no credential value left in GitHub's secret store at all.

### Deploy identity — `opsbridge-deploy`, **college tenant** (no secret exists for this app)

| Secret | Tenant | What it is | Where it is used |
|---|---|---|---|
| `AZURE_CLIENT_ID` | College | App (client) id of the **deploy** app registration — the one holding the federated credential. An identifier, not a password. | `azure/login` **only** |
| `AZURE_TENANT_ID` | College | Directory (tenant) id of the tenant that owns the Azure subscription. Not the Graph tenant. A public identifier. | `azure/login` **only** — never passed to Bicep |
| `AZURE_SUBSCRIPTION_ID` | College | Subscription containing the resource group. | `azure/login` |

There is no `AZURE_CLIENT_SECRET`, and there must never be one.

### Graph runtime identity — `opsbridge-graph`, **Microsoft 365 tenant**

| Secret | Tenant | What it is | Where it is used |
|---|---|---|---|
| `GRAPH_TENANT_ID` | Microsoft 365 | Directory (tenant) id of the tenant the Graph app is registered in and the SharePoint site lives in. A **different value** from `AZURE_TENANT_ID`. | Bicep `graphTenantId` → container env `AZURE_TENANT_ID` → MSAL authority |
| `GRAPH_CLIENT_ID` | Microsoft 365 | App (client) id of the **Graph** app registration. Never used to authenticate to Azure. | Bicep `clientId` → container env |

### Data locations (identifiers, not credentials — all in the Microsoft 365 tenant)

| Secret | Tenant | What it is | Where it is used |
|---|---|---|---|
| `SHAREPOINT_SITE_ID` | Microsoft 365 | Graph id of the SharePoint site holding both lists. | Bicep `sharePointSiteId` |
| `ASSETS_LIST_ID` | Microsoft 365 | Graph id of the Assets list (written by the sync job). | Bicep `assetsListId` |
| `TICKETS_LIST_ID` | Microsoft 365 | Graph id of the Tickets list (read by `/metrics`). | Bicep `ticketsListId` |

### Removed: `GRAPH_CLIENT_SECRET`

It used to be the ninth secret. `deploy.yml` read it and passed it to Bicep as
`clientSecret=` on every push, so a live Graph credential sat in GitHub's secret
store and travelled through every deployment — and `main.bicep` rewrote the Key
Vault secret with it each time, which meant a rotation performed directly in Key
Vault was silently reverted by the next push.

`infra/bootstrap.bicep` now writes that value to Key Vault once, and
`infra/main.bicep` only references the secret by name. Nothing in GitHub needs
the value.

If you are migrating an existing repository, delete it rather than leaving it
behind — an unused secret is a credential nobody is rotating:

```bash
gh secret delete GRAPH_CLIENT_SECRET
gh secret list      # expect the eight above, and no other
```

`METRICS_API_TOKEN` is not a GitHub secret either, for the same reason: it is
written to Key Vault by the bootstrap template and read by the container's
managed identity.

`GITHUB_TOKEN` is **not** in these tables — it is injected by Actions and is the
only credential used to push to ghcr.io. No PAT to create, rotate, or leak.

## Repository variables (optional)

`Settings → Secrets and variables → Actions → Variables`. Each has a fallback,
so the workflow runs with none of them set.

| Variable | Default | Meaning |
|---|---|---|
| `AZURE_RESOURCE_GROUP` | `rg-opsbridge365` | Resource group the deployment targets |
| `AZURE_LOCATION` | `westus2` | Region used if the group has to be created. The fallback used to be `eastus`, which this subscription's allowed-regions policy rejects — see [§3](#3-requestdisallowedbyazure--your-region-may-be-forbidden-by-policy). Set it explicitly if your policy differs |
| `NAME_PREFIX` | `opsbridge` | Bicep `namePrefix` — prefixes every resource name, **and must match the prefix `bootstrap.bicep` was run with** |

---

## Repository hardening

Configured on the repository in this release. This is the intended state as
configured, not a re-verified measurement — check it in
`Settings → Branches` and `Settings → Code security`:

- Branch protection on `main`: pull request required, required status checks, no
  force push, no branch deletion. The required checks are the **job names** from
  `ci.yml`; renaming a job silently disables its rule, so treat those names as an
  API.
- The `production` environment is limited to the `main` branch.
- Secret scanning and push protection enabled.

`deploy.yml`'s `environment: production` was originally there only to shape the
OIDC subject (see
[Things that will bite you §1](#things-that-will-bite-you)). With the
environment restricted to `main`, it now also gates the deploy, which is what a
reader would have assumed all along.

---

## The image

`ghcr.io/<owner>/<repo>`, tagged `:latest` and `:<commit sha>`. The deploy job
passes the **sha tag** to Bicep, so every revision points at an immutable
reference; `:latest` exists for humans and `docker run`.

The build uses `docker/build-push-action` (SHA-pinned) with GitHub Actions layer
caching (`type=gha`), build provenance (`mode=max`) and an attached SBOM. The
`scan-image` job then scans the exact digest just published with Trivy and
uploads an SPDX SBOM as a run artifact. The vulnerability policy is stated once
and enforced in both workflows: a **CRITICAL or HIGH with a fix available fails
the build**; unfixable findings are reported and do not block, because failing on
those only produces ignore files.

The base image is pinned by digest —
`python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217`,
with the human-readable version kept in a comment — and dependencies install from
the hash-pinned lock with `--require-hashes`, so the same commit produces the
same image.

**The GHCR package is public** (verified 2026-08-29). That is what lets Container
Apps pull with no registry credential at all, and it is the whole reason for
ghcr.io over ACR: no `registries:` block in the template, no registry password in
Key Vault, no image-pull secret to rotate. Making it public is a **UI-only step**
(`Packages → opsbridge365 → Package settings → Change visibility`) — GitHub
exposes no REST API for container package visibility, so it is easy to forget
when reproducing this from scratch.

---

## Post-deploy verification

A deploy that cannot be verified is a failed deploy, so four checks run after
`az deployment group create` and any one of them failing fails the job:

1. **`GET /healthz` must return 200.** Retried for ~3 minutes, because
   `minReplicas: 0` means the first request pays a cold start. Measured cold
   start on 2026-08-29 was **20.2 s**, so the window has real headroom — but it
   is a bound, not a courtesy.
2. **Unauthenticated `GET /metrics` must return 401.** A 200 here means live
   tenant data has become public again, which is the regression this exists to
   catch. A 503 would mean the token secret is missing from Key Vault — also a
   refusal, but an unconfigured one, and the operator needs to know.
3. **`GET /demo/metrics` must return 200 with `synthetic: true` in the body.**
   The public demo surface is what a reader of the README actually opens.
4. **`az containerapp job show` must report `triggerType == Schedule`.** Proves
   the sync job exists and is cron-triggered, not merely that ARM accepted the
   template.

All four write their result to the run's job summary, so a green run carries its
own evidence rather than an assertion that it was checked.

---

## Secret hygiene in the pipeline

- The deploy identity and the Graph identity are separate app registrations in
  separate tenants, so `AZURE_CLIENT_ID` and `AZURE_TENANT_ID` appear in exactly
  one place — the `azure/login` step — and `GRAPH_TENANT_ID`/`GRAPH_CLIENT_ID`
  appear only in the Bicep step. The principal with ARM rights has no secret; the
  secret has no ARM rights.
- **The pipeline passes no credential value at all.** Every value that reaches
  `az`'s command line is a public identifier — a tenant id, a client id, a site
  id, two list ids. The one credential it used to pass, the Graph client secret,
  is now placed in Key Vault by the bootstrap template and never travels through
  a workflow. This matters mechanically: a value interpolated into a `run:` block
  becomes an element of the process's argv, readable from `/proc/<pid>/cmdline`
  by anything else on the runner, and no amount of `${{ secrets.* }}` masking
  changes that. The fix was to stop passing it, not to hide it better.
- Repository secrets are referenced only as `${{ secrets.NAME }}` and reach the
  CLI through the step `env:` block, never inlined into the YAML.
- No `set -x` anywhere near a secret; `set -euo pipefail` only.
- No step echoes a secret, and neither Bicep template emits a secret as an output
  (deployment outputs are readable by anyone with resource-group access).
- Every action is pinned to a full commit SHA; nothing tracks a tag or `@master`.

---

## Manual deploy (same thing, from a laptop)

Bootstrap first, if the resource group has never been bootstrapped — see
[One-time bootstrap](#one-time-bootstrap--infrabootstrapbicep).

```bash
az login
az group create -n rg-opsbridge365 -l westus2      # a region your policy allows

az deployment group create \
  -g rg-opsbridge365 \
  -f infra/main.bicep \
  -p containerImage='ghcr.io/OWNER/opsbridge365:latest' \
     graphTenantId="$GRAPH_TENANT_ID" \
     clientId="$GRAPH_CLIENT_ID" \
     sharePointSiteId="$SHAREPOINT_SITE_ID" \
     assetsListId="$ASSETS_LIST_ID" \
     ticketsListId="$TICKETS_LIST_ID"
```

Those are the parameters `deploy.yml` passes, minus `namePrefix`, which defaults
to `opsbridge`. None of them is a credential — `clientId` is `opsbridge-graph`'s
public application id, not the identity you just `az login`'d as. There is no
`clientSecret` parameter: the Graph secret is in Key Vault, put there by the
bootstrap template. Add `--what-if` to see the change set without applying it.

---
---

# End-to-end runbook

> Everything above documents the pipeline and the identity model. Everything below
> is the ordered runbook: what a human must do by hand, what happens automatically
> afterwards, and where the process currently stands.

## Current state — verified 2026-08-29

Deployed, running, and re-measured on the date in the heading. For the pipeline's
current state, open the
[deploy workflow's run list](https://github.com/Alhamwis/OpsBridge365/actions/workflows/deploy.yml)
rather than trusting a run id written down here — that is the mistake the
previous revision of this file made.

Live in `rg-opsbridge365` (**westus2**) — 8 resources:

- Log Analytics `opsbridge-logs`, user-assigned identity `opsbridge-id`, a Key
  Vault (RBAC-enabled, soft-delete on, standard SKU, public network access
  enabled), Container Apps environment `opsbridge-env`, Job `opsbridge-sync`,
  Container App `opsbridge-api`, action group `opsbridge-alerts`, scheduled query
  rule `opsbridge-sync-failed`.

The API answers at
`https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io`:

| Check | Result, 2026-08-29 |
| --- | --- |
| `GET /healthz` | **200** `{"status":"ok","version":"0.1.0"}` — public, no auth |
| `GET /demo/metrics` | **200**, body carries `"synthetic": true`, no upstream call |
| `GET /metrics`, no token | **401** |
| `GET /metrics`, with token | **200** `{"open_tickets":2,"due_within_30min":0,"sla_compliance_7d_pct":null,"resolved_last_7d":0,"sla_measured_last_7d":0}` |
| Cold start from zero replicas | **20.2 s** (a second run gave 21.3 s) |
| Warm request | **248 ms** |
| Idle replica count | **0** |

Two of those numbers need a sentence each, because earlier revisions of this file
got both wrong:

- **Cold start is ~20 seconds, not 714 ms.** The replica count was observed at 0
  immediately before the probe, so this is a genuine scale-from-zero. The
  previously published 714 ms cannot have been measured against a sleeping app —
  it was a warm or partially-warm request labelled as a cold one. Twenty seconds
  is the honest price of `minReplicas: 0`, and it is why the deploy job's
  ~3-minute retry window exists.
- **`sla_compliance_7d_pct: null` is correct behaviour, not a fault.** The 7-day
  window has rolled past the seeded resolutions, so the denominator is zero. The
  service returns `null` rather than 0% or 100%, which is the same rule that
  makes the sync job write `Unknown` instead of guessing. The older documented
  payload (`50.0`, `resolved_last_7d: 2`) was captured on 2026-08-18 and is
  historical.

Also true today:

- The sync job is `triggerType: Schedule` on cron `0 */6 * * *`, and **it has run
  in the cloud** — one device matched and PATCHed into the live Assets list,
  three rows left `Unknown` (2026-08-18). See
  [`../evidence/sharepoint/reconciliation.md`](../evidence/sharepoint/reconciliation.md).
- The GHCR package is **public**, so Container Apps pulls with no registry
  credential at all.
- **106 offline tests** pass with no credentials and no network; the 12
  integration tests remain deselected by default.
- Key Vault denies the human operator with `ForbiddenByRbac`. Only the managed
  identity holds Key Vault Secrets User.
- Budget `opsbridge-monthly-20` guards the group at $20/month. The last observed
  spend was **$0.00** on 2026-08-18; it has **not** been re-measured since, so
  treat it as a dated observation rather than a current figure.

### ⚠️ The Microsoft 365 subscription is a trial, and it has a date

`O365_BUSINESS_PREMIUM`, `isTrial: true`, `nextLifecycleDateTime`
**2026-09-16** — read from Graph on 2026-08-29.

Earlier revisions of this runbook said the opposite ("Business Standard, not the
E5 trial", "a paid subscription has no 30-day clock") and drew a conclusion from
it: that the evidence in this repository does not need capturing before a
deadline. **That conclusion was false.** The clock is real:

- Capture any live-tenant evidence you still need **before 2026-09-16**.
- Before that date, decide whether to convert or cancel in the Microsoft 365
  admin center. Microsoft exposes no supported Graph or CLI API for it, so it
  cannot be scripted or alerted on from here. The exact path, checked against
  Microsoft's current documentation (last updated 2026-06-24):

  1. Microsoft 365 admin center → **Billing** (Simplified view), or
     **Billing → Your products** (Dashboard view)
  2. Select the OpsBridge365 subscription
  3. **Edit recurring billing** → **Off** → **Save**

  Note the distinction, because picking the wrong control tears the tenant down
  early: **Edit recurring billing → Off** leaves the subscription active until
  it expires. **Cancel subscription** ends it immediately. The first is what is
  wanted here.

If it lapses: `/healthz` and `/demo/metrics` keep working (no tenant dependency);
`/metrics` returns **502** — an honest upstream failure, never stale numbers
served as if they were live; the sync job fails and the `opsbridge-sync-failed`
alert rule catches it; the Azure side is unaffected, because it lives in a
different tenant entirely.

What is still not done is in
[`README.md` § Known gaps](../README.md#known-gaps--not-done), and
[`STATUS.md`](STATUS.md) carries the same current/historical split with the exact
command behind each claim.

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

## Phase 1 — the human steps

These were the six steps that could not be automated. The table is the
reproduction path for anyone building this from the repository, and two of them
turned out differently from the plan.

| # | Step | Status | What actually happened |
| --- | --- | --- | --- |
| H1 | **Azure subscription** | ✅ Done | "Azure for Students" in a college-managed tenant. Resource group `rg-opsbridge365` — created in `eastus`, then **recreated in `westus2`** once the allowed-regions policy refused `eastus`. See [§3](#3-requestdisallowedbyazure--your-region-may-be-forbidden-by-policy) |
| H2 | **A tenant that can grant app-level consent** | ✅ Done | A separate OpsBridge365 tenant, where the author is global admin. The tenant that owns the subscription does not permit application-level Graph consent, which is the whole reason for the split. See [`ARCHITECTURE.md` § The two-tenant split](ARCHITECTURE.md#the-two-tenant-split) |
| H3 | **Microsoft 365 with SharePoint** | ⚠️ Trial, expires **2026-09-16** | `O365_BUSINESS_PREMIUM`, `isTrial: true` (read from Graph 2026-08-29). Every live-tenant artefact — the SharePoint site, both lists, the integration suite — stops being reproducible after that date unless the trial is converted. Capture evidence before it, and see the warning under [Current state](#current-state--verified-2026-08-29) |
| H4 | **GitHub repository** | ✅ Done | Public: `github.com/Alhamwis/OpsBridge365`. A disclosure review ran first — zero real identifiers in the working tree or anywhere in the history |
| H5 | **`az login`** | ✅ Done | Both tenants. Note the Graph tenant needs `--allow-no-subscriptions` |
| H6 | **Graph admin consent** | ✅ Done — **and not by clicking** | Granted **programmatically** by creating `appRoleAssignments` on the service principal, then reading the consent state back. Three application permissions, zero delegated grants |

**On H6.** Admin consent is usually described as "one global-admin click."
Creating the `appRoleAssignment` objects directly makes the grant reproducible,
reviewable and diffable — and reading the consent state back afterwards is what
turns "consent was requested" into "consent exists," which a portal click does
not give you.

### Prerequisites on the workstation

Required to reproduce this end to end:

| Tool | Requirement |
| --- | --- |
| git | any current release |
| Python | **≥ 3.12** (`pyproject.toml`), and the locks are resolved for `linux`/`py3.12` |
| Docker | any current release, for the local image build |
| Azure CLI | any current release, with the `containerapp` extension — `deploy.yml` installs it on demand |
| Bicep | any current release (`az bicep install`) |
| GitHub CLI | optional — only for `gh secret` management |
| `uv` | optional — only to regenerate the dependency locks |

Version floors above 3.12 are not asserted because none was tested. The versions
this was built and verified against, on 2026-08-18: Docker 29.1.3, Azure CLI
2.89.1, GitHub CLI 2.97.0, Bicep 0.46.1.

> **Site-specific note, not a requirement.** On the author's machine the Azure
> CLI lives in a pip venv under `%LOCALAPPDATA%\opsbridge-tools` rather than in
> Program Files. That is why `scripts/Resolve-AzCli.ps1` exists: it resolves `az`
> from four sources, including the **live User PATH read from the registry**, so
> a shell that was open before the CLI was installed still finds it rather than
> reporting a false "not installed". If your `az` is on `PATH`, none of this
> affects you.

---

## Phase 2 — one-time setup

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
   > held `Sites.FullControl.All` for exactly that step and was then deleted.**
   > (That app registration has nothing to do with `infra/bootstrap.bicep`; one
   > is a deleted identity in the Microsoft 365 tenant, the other is a template.)
   > The runtime identity never held FullControl and its permissions were not
   > widened.
   > If you reproduce this, do the same — and delete the app, not just the grant.
   > See [`SECURITY.md`](SECURITY.md).

4. ✅ **App authorised on that one site** — `Sites.Selected` grants nothing until
   you do this (`POST /sites/{site-id}/permissions`, `"roles": ["write"]`). Role
   `write`, one site.
5. ✅ **`infra/bootstrap.bicep` run once** — Key Vault, managed identity, the
   Key Vault Secrets User role assignment, and the two secret values. This is a
   **prerequisite** for every routine deployment; `main.bicep` fails on its
   `existing` lookups without it. See
   [One-time bootstrap](#one-time-bootstrap--infrabootstrapbicep).
6. ✅ **Repository secrets and variables added** from the tables above — and
   `GRAPH_CLIENT_SECRET` deleted if you are migrating from an older revision.
7. ✅ **GHCR package marked public** (`Packages → opsbridge365 → Package
   settings → Change visibility`). A public image is what lets Container Apps
   pull with no registry credentials at all. There is no REST API for it; it is a
   UI-only setting, so it is easy to forget when reproducing this.
8. ✅ **Resource providers registered** and **an allowed region picked** — neither
   was in earlier revisions of this list, and both cost a failed run. See
   [§3](#3-requestdisallowedbyazure--your-region-may-be-forbidden-by-policy) and
   [§4](#4-missingsubscriptionregistration--a-fresh-subscription-registers-nothing).

## Phase 3 — the automated path

After Phase 2, deployment is a `git push`:

```
push to main
  -> secret-scan     gitleaks over the full history
  -> test            ruff + pytest, both hard gates
  -> codeql          python, security-extended
     (all three gate everything below)
  -> build-and-push  docker build -> ghcr.io, :latest + :<sha>
  -> scan-image      trivy against the published digest + SPDX SBOM artifact
  -> deploy          az deployment group create via OIDC
  -> verify          /healthz 200; /metrics 401 unauthenticated;
                     /demo/metrics synthetic; job triggerType == Schedule
```

Any verification failing fails the deploy — a deploy that cannot be verified is a
failed deploy. All four write their result to the run's job summary.

### Or from a laptop

```powershell
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

> **Check this before you rely on it.** The script predates the bootstrap split:
> it reads `$env:GRAPH_CLIENT_SECRET` and passes `clientSecret=` to `main.bicep`,
> which no longer declares that parameter. Verify with
> `grep -n clientSecret scripts/deploy-opsbridge.ps1` — if it still matches, use
> the two `az deployment group create` commands
> ([bootstrap](#one-time-bootstrap--infrabootstrapbicep) and
> [manual deploy](#manual-deploy-same-thing-from-a-laptop)) until the script is
> brought in line.

### Verify

```powershell
powershell -NoProfile -File scripts/verify-opsbridge.ps1
```

Read-only, and it never conflates "checked and passed" with "could not check". A
check that could not run is **SKIP**, never PASS. Unauthenticated it still skips
the cloud checks and says so; with an active `az login` against the subscription
it has real resources to look at.

Manual equivalents:

```powershell
az deployment group show -g rg-opsbridge365 -n <deployment> --query properties.outputs
az containerapp job start -g rg-opsbridge365 -n opsbridge-sync        # don't wait for cron
az containerapp job execution list -g rg-opsbridge365 -n opsbridge-sync -o table

$BASE = 'https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io'

# curl.exe, not curl. In Windows PowerShell 5.1 `curl` is an ALIAS for
# Invoke-WebRequest, which has no -H parameter and fails with
# "Cannot bind parameter 'Headers'". The .exe suffix bypasses the alias.
curl.exe "$BASE/healthz"        # public
curl.exe "$BASE/demo/metrics"   # public, synthetic
curl.exe -H "Authorization: Bearer $env:METRICS_API_TOKEN" "$BASE/metrics"

# Or stay native to PowerShell:
Invoke-RestMethod "$BASE/metrics" -Headers @{ Authorization = "Bearer $env:METRICS_API_TOKEN" }
```

`/healthz` and `/demo/metrics` need no credential. `/metrics` does — without one
it returns 401, which is the point.

### Validate without deploying (possible today, offline)

```bash
az bicep build --file infra/bootstrap.bicep   # compiles clean, 0 diagnostics
az bicep build --file infra/main.bicep        # compiles clean, 0 diagnostics
python -m pytest -q                           # 106 passed, 12 deselected
ruff check .
docker build -t opsbridge365:local .
```

With tenant credentials in the environment, the live suite too:

```bash
python -m pytest -m integration -q            # the 12 live tenant tests
```

`bicep build` is worth running before every push, and worth not trusting too far:
it returned zero diagnostics before each of the four deploy failures documented at
the top of this file. It tells you the template is valid. It cannot tell you your
subscription will accept it.

---

## Dependency locks

`requirements.txt` and `requirements-dev.txt` are fully resolved, **hash-pinned**
locks generated from `pyproject.toml` for `linux` / `py3.12` — 31 runtime
packages, 39 with dev. They are not hand-edited.

CI, the Docker build and a local checkout all install the same way, so all three
resolve to the same set:

```bash
pip install --require-hashes --no-deps -r requirements-dev.txt
pip install --no-deps -e .
```

`--require-hashes` makes pip refuse anything whose artifact hash does not match;
`--no-deps` stops it re-resolving transitively and quietly reintroducing an
unpinned package. `pip install .[dev]` would re-resolve every `>=` floor at
install time, which means the test suite could pass against a dependency set no
release ever ships.

Regenerate after any change to `pyproject.toml`'s dependencies:

```bash
uv pip compile pyproject.toml --python-version 3.12 --python-platform linux \
  --generate-hashes --no-header -o requirements.txt

uv pip compile pyproject.toml --extra dev --python-version 3.12 --python-platform linux \
  --generate-hashes --no-header -o requirements-dev.txt
```

The `--python-platform linux` flag matters: the container and the CI runner are
both Linux, and a lock generated on Windows resolves platform-specific wheels
that will not install there. Commit both files in the same change as the
`pyproject.toml` edit — Dependabot proposes updates weekly (pip, docker and
github-actions, grouped), and a lock that lags `pyproject.toml` makes those pull
requests unreadable.

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

**Rebuilding after a teardown means running `infra/bootstrap.bicep` again.** The
vault and its secrets are gone with the group, and `main.bicep` will fail on the
`existing` lookup until they are back.

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
| `RequestDisallowedByAzure` at `az group create` or at deploy | A subscription-level allowed-regions policy. Azure for Students permitted `northcentralus`, `mexicocentral`, `westus2`, `westus`, `canadacentral` — **not `eastus`** | Set the `AZURE_LOCATION` repository variable to a permitted region and recreate the resource group there. [§3](#3-requestdisallowedbyazure--your-region-may-be-forbidden-by-policy) |
| `MissingSubscriptionRegistration` | A fresh subscription has its resource providers unregistered, and ARM names one per failure | `az provider register` for `Microsoft.App`, `Microsoft.KeyVault`, `Microsoft.OperationalInsights`, `Microsoft.ManagedIdentity` and `Microsoft.Insights`, then wait for `Registered`. [§4](#4-missingsubscriptionregistration--a-fresh-subscription-registers-nothing) |
| `main.bicep` fails resolving an `existing` Key Vault or identity | The resource group has never been bootstrapped, or `namePrefix` differs between the two templates — the vault name is derived from it | Run [`infra/bootstrap.bicep`](#one-time-bootstrap--infrabootstrapbicep) with the same `namePrefix`. This failure is deliberate: the alternative is silently building a second vault |
| Deploy fails with `AuthorizationFailed` creating a role assignment | Something still creates a `roleAssignments` resource in `main.bicep`, and the deploy identity is Contributor only | Move the assignment to `bootstrap.bicep`. Do not re-grant the elevated role to the pipeline |
| `az keyvault secret list` returns `ForbiddenByRbac` for you, the operator | The vault is RBAC-only and only the managed identity holds Key Vault Secrets User | Not a fault — it is the control working. Write secrets through a deployment (control plane), which is what `bootstrap.bicep` does |
| The failure alert never fires on a real failure | The query matches only one of the app's two failure statuses. `app/sync.py` emits **`config_error`** (exit 2) *and* **`graph_error`** (exit 1); a wrong list id produces the latter | Match both, plus `Traceback`/`CRITICAL` as a catch-all — and test it by failing the job on purpose. [`../evidence/monitoring/alerting.md`](../evidence/monitoring/alerting.md) |
| First deployment fails resolving the Key Vault reference | Azure RBAC propagation lag — the role assignment exists but has not taken effect | Re-run the identical command. It is idempotent and the second run succeeds |
| `/healthz` never returns 200 in the verify step | Cold start exceeded the ~3-minute retry window (a real cold start is ~20 s), or the container is crashing | Check the Log Analytics stream; the app logs the reason on startup |
| `/metrics` returns 401 | No bearer token, or the wrong one. The response is deliberately identical for both | Send `Authorization: Bearer <token>` with the value in the `metrics-api-token` Key Vault secret. After a rotation, restart the revision — the container reads the secret at replica start |
| `/metrics` returns 429 | 30 requests per minute per caller, sliding window | Back off; `Retry-After` says how long. The limit is per replica, which is exact at `maxReplicas: 1` |
| `/metrics` returns 503 | Configuration is incomplete, or `METRICS_API_TOKEN` is unset — the endpoint **fails closed** rather than serving data unauthenticated. The response deliberately does not say which | Read the container log; it names what is missing. If the token is the problem, the Key Vault secret or its reference is not resolving |
| `/metrics` returns 502 | Graph auth failed, or Graph rejected the request | Usually missing admin consent, `Sites.Selected` not yet authorised on the site, or a lapsed M365 subscription |
| `/metrics` returns 504 | One refresh exceeded the 25-second wall-clock deadline | Upstream is slow or a list has grown; check Graph throttling in the container log |
| Sync exits `2` | `config_error` — no credentials at all | The container had no environment; check the Key Vault reference resolved |
| Sync exits `1` with `errors` populated | One or more PATCH calls failed | The JSON summary names each failure and the asset id |
| Container Apps cannot pull the image | The GHCR package is private. It is public here (verified 2026-08-29), but a fork starts private | Mark it public — that is what removes the need for registry credentials |
| Vault name collision on redeploy | A soft-deleted vault of the same name is inside its 7-day window | Purge it, or deploy to a differently-named resource group — then re-run `bootstrap.bicep`, because a purged vault takes its secrets with it |
