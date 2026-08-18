# CI/CD pipeline evidence

`.github/workflows/deploy.yml`, on push to `main`.

## The green run — `32115509179`

| Job | Result |
| --- | --- |
| `test` | ✅ SUCCESS |
| `secret scan (gitleaks)` | ✅ SUCCESS |
| `build and push to ghcr.io` | ✅ SUCCESS |
| `deploy to Azure` | ✅ SUCCESS |

Full green, end to end: **push to deploy through OIDC, with no stored Azure
credential.** There is no client secret, no service-principal password and no
`creds:` JSON blob in the workflow. GitHub mints a short-lived OIDC token for
this repository, `azure/login@v2` exchanges it, and the token dies with the job.

The deploy job carries its own verification, and either check failing fails the
job:

1. `GET https://<apiFqdn>/healthz` must return **200**, retried for ~3 minutes
   because `minReplicas: 0` means the first request pays a cold start.
2. `az containerapp job show` must report `triggerType == Schedule`.

Both passed. The resources they verified are listed in
[`../azure/deployment.md`](../azure/deployment.md).

The gate order is unchanged and was exercised on the way through: `secret-scan`
**and** `test` both gate `build-and-push`, which gates `deploy`, so nothing
reaches ghcr.io — and therefore nothing reaches Azure — without a clean history
scan and a green test suite.

---

## The three runs before it, and why each failed

This is the more useful half of the record. Three earlier runs failed for three
genuinely different reasons, and none of them was a code defect.

### 1. `AADSTS700213` — the job is environment-gated

Run `32113268465` passed `test`, passed the gitleaks scan, built and pushed the
image, and then failed at `deploy to Azure`:

```
AADSTS700213: No matching federated identity record found for presented assertion.
```

The federated credential on `opsbridge-deploy` had been created with subject
`repo:Alhamwis/OpsBridge365:ref:refs/heads/main` — which reads correctly and
matches the workflow's trigger, and is wrong. The deploy job declares
`environment: production`, and **an environment-gated job presents
`...:environment:production`, not `...:ref:refs/heads/main`.** The branch does
not appear in the subject at all.

Nothing in that error message mentions environments.

### 2. `AADSTS700213` again — the subject is ID-qualified

Fixing the subject to `repo:Alhamwis/OpsBridge365:environment:production` was
still not a match. This account's GitHub default subject claim is
**ID-qualified**:

```
repo:Alhamwis@<ownerId>/OpsBridge365@<repoId>:environment:production
```

`use_default` was already `true` on the repository's OIDC settings, so there was
nothing to normalise away — the token GitHub mints carries the ID-qualified form,
and Entra has to match that exact string.

Worth stating plainly: the ID-qualified form is **rename-proof**. A subject built
from names silently follows a repository or account rename; one built from
immutable ids does not, so a renamed repo fails loudly instead of quietly
inheriting a trust relationship. This is a security improvement, not a
workaround.

### 3. `RequestDisallowedByAzure` — the region is policy-restricted

Azure for Students enforces an allowed-regions policy. Permitted:
`northcentralus`, `mexicocentral`, `westus2`, `westus`, `canadacentral`.
**`eastus` is not allowed**, and `eastus` was the default everywhere. The
resource group was recreated in `westus2`.

### 4. `MissingSubscriptionRegistration` — fresh subscription, no providers

`Microsoft.App`, `Microsoft.KeyVault`, `Microsoft.OperationalInsights`,
`Microsoft.ManagedIdentity` and `Microsoft.Insights` were all unregistered on the
new subscription. Each was registered. ARM names one provider per failure, so
this arrives as a sequence, not a single error.

---

## What this sequence is evidence of

A template that compiles is not a template that deploys. `bicep build` returned
zero diagnostics for every one of these failures — the template was never the
problem. Each failure lived in the space between the repository and a specific
real subscription: an identity provider's subject format, a subscription's policy
assignment, a subscription's provider registration state.

That is the case for deploying rather than reasoning about deploying, and it is
why run `32115509179` is the only run in this repository that is described as a
successful deploy.

Every one of the four is written up as a reproduction-and-fix in
[`../../docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md#things-that-will-bite-you).
