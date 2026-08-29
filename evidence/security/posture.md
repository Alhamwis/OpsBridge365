# Security posture — as checked against the live deployment

> **HISTORICAL EVIDENCE — captured 2026-08-18.** Every claim below was checked
> against the deployed resources in `rg-opsbridge365` on that date, not against
> the template that declares them. It is a point-in-time capture, not a current
> posture statement; §6 records the gaps as they stood then, with the ones closed
> since marked. One check — the Key Vault denial in §3 — was re-verified on
> 2026-08-29 and is marked as such. For current state, see
> [`../../docs/STATUS.md`](../../docs/STATUS.md); for the full control list, see
> [`../../docs/SECURITY.md`](../../docs/SECURITY.md).

Tenant, subscription, app and list identifiers, and the Key Vault's random name
suffix, are omitted.

---

## 1. The Graph secret is a Key Vault reference, not a stored value

| Check | Result on 2026-08-18 |
| --- | --- |
| Container App secret `graph-client-secret` | `keyVaultUrl` is set; **there is no inline `value`** |
| How the container receives it | `AZURE_CLIENT_SECRET` is a **`secretRef`**, resolved at replica start |
| Where the plaintext lives | Key Vault only |

The distinction is the whole control. A Container Apps secret can hold a literal
string, in which case the plaintext sits on the app resource and anyone who can
read that resource's definition can read the credential. Here the app resource
carries a *pointer* — a vault URL — and the platform dereferences it using the
user-assigned managed identity when a replica starts. Reading the app definition
tells you where the secret is, not what it is.

The same mechanism now carries a second secret, `metrics-api-token`, added with
the authentication on `/metrics` after this capture.

## 2. No secret was emitted as a deployment output

`az deployment group show ... --query properties.outputs` returned exactly:

```
apiFqdn
identityPrincipalId
jobName
keyVaultName
logAnalyticsWorkspaceId
```

An FQDN, a principal id, two resource names, and a workspace id. **No secret.**

This matters more than it looks: deployment outputs are readable by anyone with
resource-group access and are retained in deployment history, so emitting a
secret as an output is a durable publication, not a transient one. The secret
entered as a `@secure()` parameter — which ARM neither logs nor returns — and
went straight to the vault.

## 3. Key Vault denies the human operator

Attempting to read the secret **as the signed-in human operator** returned:

```
ForbiddenByRbac
```

**Re-verified 2026-08-29: the denial still holds.**

Only the user-assigned identity `opsbridge-id` holds **Key Vault Secrets User**
on the vault. The operator who created the vault, and who holds Contributor on
the resource group, cannot read what is in it.

Least privilege is easy to assert and hard to demonstrate; the demonstration is a
denial against yourself. Contributor on a resource group is a broad role and it is
tempting to assume it implies data-plane access to everything inside — it does
not, because the vault is **RBAC-authorized rather than access-policy based**, so
data-plane access is one explicit role assignment and nothing inherits it from
the management plane.

The corollary is operational and worth stating: an operator who needs to rotate
that secret has to grant themselves the role first, visibly, as a role
assignment someone can see. That is the correct shape — privileged access that
leaves a trace, rather than standing authority that does not.

| Principal | Key Vault data-plane access |
| --- | --- |
| `opsbridge-id` (user-assigned managed identity) | **Key Vault Secrets User**, scoped to the vault |
| The human operator (Contributor on the resource group) | **None** — `ForbiddenByRbac` |
| `opsbridge-deploy` (the pipeline identity) | Could create the role assignment; held no data-plane role itself |

> **Since this capture — the pipeline identity no longer needs that power.** The
> role assignment and the secret values moved into `infra/bootstrap.bicep`, which
> a human runs once. `infra/main.bicep` references the vault and the identity as
> `existing` and creates no role assignment, so the routine deploy identity needs
> only **Contributor**. The grant itself has not yet been trimmed: as of
> 2026-08-29 the deploy service principal still holds both Contributor and **Role
> Based Access Control Administrator** at resource-group scope. Removing the
> second is a one-line `az role assignment delete` and is outstanding.

## 4. No stored Azure credential in the pipeline

The 2026-08-18 run `32115509179` deployed to Azure through **OIDC federation**
with no client secret, no service-principal password and no `creds:` JSON blob.
`opsbridge-deploy` held zero passwords and zero certificates — there was nothing
on that principal to steal.

The federated credential's subject is **ID-qualified**:

```
repo:Alhamwis@<ownerId>/OpsBridge365@<repoId>:environment:production
```

Two properties follow from that string:

- **`environment:production`** — not `ref:refs/heads/main`. The deploy job
  declares an environment, which changes the OIDC subject GitHub presents, so a
  workflow that drops the `environment:` line stops authenticating rather than
  silently continuing to work. Note what this is *not*: on the capture date the
  `production` environment carried no reviewers and no protection rules, so it
  shaped the subject string and gated nothing. See §6.
- **The `@<ownerId>` / `@<repoId>` qualifiers** — trust is pinned to immutable
  numeric ids, so a repository or account rename cannot carry the trust
  relationship with it, and a new repository cannot inherit a retired name.

Both cost a failed deploy to discover. See
[`../github-actions/pipeline.md`](../github-actions/pipeline.md).

## 5. HTTPS only, observed

`http://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io` returned
**HTTP 301** to `https://`. Ingress is configured `allowInsecure: false`, and the
redirect was the observed behaviour, not the configured intent.

## 6. What was not covered on 2026-08-18

The gaps as they stood on the capture date, listed so the sections above are not
read as a complete posture. Each carries its status as of **2026-08-29**.

| Gap on 2026-08-18 | Status now |
| --- | --- |
| `/metrics` was **public and unauthenticated**. It exposed counts and a percentage — no ticket contents, no personal data, no identifiers — which was acceptable for a demo and not for production | **Closed.** Bearer token required (401 without), rate limited 30/min per caller, served from a 45-second cache, bounded by a 25-second deadline, and it fails **closed** with 503 if no token is configured. A public `/demo/metrics` serving explicitly synthetic data took over the "show me the shape" job |
| **Key Vault allowed public network access.** Private endpoints and a vault firewall are the production answer and neither is free | **Open.** Unchanged, and still a deliberate cost decision |
| **No dependency or container image scanning** — no Dependabot, no CodeQL, no Trivy. Secret scanning *was* implemented: gitleaks over the full history, hard-gating the image build | **Closed.** CodeQL (python, `security-extended`), Trivy filesystem and image scans, an SPDX SBOM, and Dependabot for pip / docker / github-actions. Every action is now pinned to a commit SHA and `ruff` is a hard gate rather than advisory |
| **`main` was not a protected branch.** Anyone with write access could push straight to the branch that triggers the deploy | **Being applied in this release.** Branch protection on `main`: pull request required, required status checks, no force push, no deletion. Configured state — this file does not claim to have verified it |
| **The `production` environment carried zero protection rules**, and admins could bypass it. It scoped the OIDC subject; it did not gate the deploy | **Being applied in this release.** Environment protection limited to `main`. Same caveat: configured, not captured here |
| **GitHub-native secret scanning and push protection were disabled**, so the only secret gate was gitleaks inside the workflow — which runs after a push has already landed | **Being applied in this release.** Secret scanning and push protection enabled. Same caveat |

The three GitHub-hardening rows were absent from this file's original gap list,
which presented itself as complete. They were real gaps on the capture date and
are recorded now rather than quietly dropped once fixed.

Full threat model and control list: [`../../docs/SECURITY.md`](../../docs/SECURITY.md).
