# Security posture — verified against the live deployment

Every claim below was checked against the deployed resources in
`rg-opsbridge365`, not against the template that declares them. Tenant,
subscription, app and list identifiers, and the Key Vault's random name suffix,
are omitted.

---

## 1. The one secret is a Key Vault reference, not a stored value

| Check | Result |
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

## 2. No secret is emitted as a deployment output

`az deployment group show ... --query properties.outputs` returns exactly:

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
enters as a `@secure()` parameter — which ARM neither logs nor returns — and goes
straight to the vault.

## 3. Key Vault denies the human operator

Attempting to read the secret **as the signed-in human operator** returns:

```
ForbiddenByRbac
```

Only the user-assigned identity `opsbridge-id` holds **Key Vault Secrets User**
on the vault. The operator who created the vault, and who holds Contributor on
the resource group, cannot read what is in it.

**This is the result worth having.** Least privilege is easy to assert and hard
to demonstrate; the demonstration is a denial against yourself. Contributor on a
resource group is a broad role and it is tempting to assume it implies data-plane
access to everything inside — it does not, because the vault is **RBAC-authorized
rather than access-policy based**, so data-plane access is one explicit role
assignment and nothing inherits it from the management plane.

The corollary is operational and worth stating: an operator who needs to rotate
that secret has to grant themselves the role first, visibly, as a role
assignment someone can see. That is the correct shape — privileged access that
leaves a trace, rather than standing authority that does not.

| Principal | Key Vault data-plane access |
| --- | --- |
| `opsbridge-id` (user-assigned managed identity) | **Key Vault Secrets User**, scoped to the vault |
| The human operator (Contributor + RBAC Administrator on the RG) | **None** — `ForbiddenByRbac` |
| `opsbridge-deploy` (the pipeline identity) | Can create the role assignment; holds no data-plane role itself |

## 4. No stored Azure credential in the pipeline

Run `32115509179` deployed to Azure through **OIDC federation** with no client
secret, no service-principal password and no `creds:` JSON blob. `opsbridge-deploy`
holds zero passwords and zero certificates — there is nothing on that principal
to steal.

The federated credential's subject is **ID-qualified**:

```
repo:Alhamwis@<ownerId>/OpsBridge365@<repoId>:environment:production
```

Two properties follow from that string, and both are security properties:

- **`environment:production`** — not `ref:refs/heads/main`. Trust is pinned to a
  GitHub environment, not to a branch name, so a workflow that drops the
  environment gate stops authenticating rather than silently continuing to work.
- **The `@<ownerId>` / `@<repoId>` qualifiers** — trust is pinned to immutable
  numeric ids, so a repository or account rename cannot carry the trust
  relationship with it, and a new repository cannot inherit a retired name.

Both cost a failed deploy to discover. See
[`../github-actions/pipeline.md`](../github-actions/pipeline.md).

## 5. HTTPS only, observed

`http://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io` returns
**HTTP 301** to `https://`. Ingress is configured `allowInsecure: false`, and the
redirect is the observed behaviour, not the configured intent.

## 6. What is still not covered

Unchanged by this deployment, and listed so the wins above are not read as a
complete posture:

- `/metrics` is public and unauthenticated. It exposes counts and a percentage —
  no ticket contents, no personal data, no identifiers — which is acceptable for
  a demo and not for production.
- Key Vault allows public network access. Private endpoints and a vault firewall
  are the production answer and neither is free.
- No dependency or container image scanning: no Dependabot, no CodeQL, no Trivy.
  Secret scanning **is** implemented — gitleaks over the full history, hard-gating
  the image build.
- GitHub push protection is still off.

Full threat model and control list: [`../../docs/SECURITY.md`](../../docs/SECURITY.md).
