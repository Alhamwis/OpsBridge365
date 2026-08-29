# CI/CD pipeline evidence

> **HISTORICAL EVIDENCE — captured 2026-08-18.** This file documents the **first**
> fully green deploy — run `32115509179`, commit `63c4616` — and the four
> failures that preceded it. It is not a statement about the current pipeline.
> Later runs have deployed since; the most recent successful deploy at the time
> of writing was run `32122134218` at commit `550f737`. For what is deployed now,
> read the
> [Actions run list](https://github.com/Alhamwis/OpsBridge365/actions/workflows/deploy.yml)
> and [`../../docs/STATUS.md`](../../docs/STATUS.md) rather than a run id pinned
> in a markdown file.

`.github/workflows/deploy.yml`, on push to `main`.

## The first green run — 2026-08-18, run `32115509179` (commit `63c4616`)

| Job | Result |
| --- | --- |
| `test` | ✅ SUCCESS |
| `secret scan (gitleaks)` | ✅ SUCCESS |
| `build and push to ghcr.io` | ✅ SUCCESS |
| `deploy to Azure` | ✅ SUCCESS |

Full green, end to end: **push to deploy through OIDC, with no stored Azure
credential.** There was no client secret, no service-principal password and no
`creds:` JSON blob in the workflow. GitHub minted a short-lived OIDC token for
this repository, `azure/login` exchanged it, and the token died with the job.

The deploy job carries its own verification, and either check failing fails the
job:

1. `GET https://<apiFqdn>/healthz` must return **200**, retried for ~3 minutes
   because `minReplicas: 0` means the first request pays a cold start.
2. `az containerapp job show` must report `triggerType == Schedule`.

Both passed. The resources they verified are listed in
[`../azure/deployment.md`](../azure/deployment.md).

> The retry window in check 1 turned out to be load-bearing rather than generous.
> A cold start re-measured on 2026-08-29 against a genuinely idle app took
> **20.2 s**, not the 714 ms recorded on 2026-08-18. A single un-retried probe
> would be a coin flip.

The gate order was exercised on the way through: `secret-scan` **and** `test`
both gate `build-and-push`, which gates `deploy`, so nothing reaches ghcr.io —
and therefore nothing reaches Azure — without a clean history scan and a green
test suite.

---

## The four failures before it, and why each was different

This is the more useful half of the record. Four deploy attempts failed for four
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
`environment: production`, and **a job that declares an environment presents
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

Azure for Students enforces an allowed-regions policy. Permitted at the time:
`northcentralus`, `mexicocentral`, `westus2`, `westus`, `canadacentral`.
**`eastus` was not allowed**, and `eastus` was the default everywhere. The
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
why run `32115509179` is the **first** run in this repository that deployed
successfully. It is not the last: run `32122134218` at commit `550f737` deployed
too, and the run list linked at the top of this file is the only place that
answers "what deployed the thing that is running now".

Every one of the four is written up as a reproduction-and-fix in
[`../../docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md#things-that-will-bite-you).

---

## How the pipeline has changed since this run

The run above is a record of a pipeline that no longer exists in that form.
Changes made since, none of them captured as evidence here — the workflow files
are the record:

- Every third-party action is pinned to a **full 40-character commit SHA** with
  the version in a trailing comment. At the time of this run they were tags,
  which are movable refs.
- `ruff` no longer carries `continue-on-error: true`. Lint is a **hard gate**: it
  blocks the image build and therefore the deploy.
- Added: **CodeQL** (python, `security-extended`), **Trivy** filesystem and image
  scans, and an **SPDX SBOM** uploaded as a build artifact. The Trivy policy is
  that CRITICAL/HIGH findings *with a fix available* fail the build; findings with
  no fix are reported and do not block.
- Added **Dependabot** for pip, docker and github-actions — weekly, grouped —
  plus `dependency-review` on pull requests.
- Dependencies install from a hash-pinned lock with
  `pip install --require-hashes --no-deps -r ...`.
- `GRAPH_CLIENT_SECRET` is no longer a GitHub secret. Secret values and the Key
  Vault role assignment moved to `infra/bootstrap.bicep`, which a human runs once,
  so the routine deploy identity needs only Contributor.
- A separate scheduled workflow, `.github/workflows/health.yml`, probes
  `/healthz` every four hours. It never redeploys on failure — it leaves the run
  red for a human to look at.
